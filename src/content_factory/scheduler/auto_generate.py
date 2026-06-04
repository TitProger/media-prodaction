"""
auto_generate.py — Cron job: pick random clips → generate Short → upload to YouTube.

Flow
----
1. Pick a random top_video source that still has unused clips.
2. Pick a random unused clip from it.
3. Pick a random bottom_video source + any clip from it.
4. Pick a random banner (image or video).
5. Generate subtitles + compose the Short via FFmpeg.
6. Upload to YouTube.
7. Mark the top clip as used (done ONLY after successful upload).
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from pathlib import Path

from content_factory.config.settings import (
    ANTHROPIC_API_KEY,
    BANNER_ANIMATION,
    CLAUDE_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    OUTPUT_DIR,
    WEB_USER_ID,
    YOUTUBE_AI_DESCRIPTION,
    YOUTUBE_CLIENT_SECRET,
    YOUTUBE_CRON_INTERVAL_HOURS,
    YOUTUBE_DESCRIPTION,
    YOUTUBE_PRIVACY_STATUS,
    YOUTUBE_TAGS,
    YOUTUBE_TOKEN_FILE,
)
from content_factory.db.library import (
    count_unused_clips,
    list_files,
    list_sources,
    mark_used,
    pick_random_clip,
    pick_random_unused_clip,
)

logger = logging.getLogger(__name__)


# ─── AI meta generation ───────────────────────────────────────────────────────

_META_SYSTEM = (
    "You are a YouTube Shorts content manager. "
    "Your goal is to maximise views by writing SEO-optimised, trend-matching metadata."
)

_META_PROMPT = """\
Generate YouTube Shorts metadata for a video clip.

Clip title: {clip_title}

Rules (STRICTLY follow):
- Reply ONLY with a valid JSON object — no markdown, no explanation.
- "title": max 80 chars, catchy, in the SAME language as the clip title, add 1-2 relevant emojis
- "description": 200-350 chars, match the clip language, end with 6-9 trending hashtags \
(mix native-language + English: #Shorts #viral #trending etc.)
- "tags": list of 12-18 strings — mix native keywords + English trending tags for Shorts/Reels

JSON format:
{{"title": "...", "description": "...", "tags": ["...", "..."]}}
"""


def _generate_meta(clip_title: str) -> dict:
    """
    Ask Gemini (or Claude as fallback) to generate YouTube title, description, and tags.
    Returns a dict with keys: title, description, tags (list[str]).
    Falls back to static defaults if no AI key is configured or on any error.
    """
    prompt = _META_PROMPT.format(clip_title=clip_title)

    raw: str | None = None
    try:
        if GEMINI_API_KEY:
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=f"{_META_SYSTEM}\n\n{prompt}",
            )
            raw = response.text.strip()
            logger.info("[cron] Meta generated via Gemini")
        elif ANTHROPIC_API_KEY:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            message = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=512,
                system=_META_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            logger.info("[cron] Meta generated via Claude")
        else:
            logger.info("[cron] No AI key — using static description")
            return {}
    except Exception as exc:
        logger.warning("[cron] Meta generation failed (%s) — using static defaults", exc)
        return {}

    # Strip optional markdown code fences
    import re
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")

    try:
        meta = json.loads(raw)
        return {
            "title":       str(meta.get("title", "")).strip()[:90] or "",
            "description": str(meta.get("description", "")).strip() or "",
            "tags":        [str(t).strip() for t in meta.get("tags", []) if str(t).strip()],
        }
    except Exception as exc:
        logger.warning("[cron] Meta JSON parse error (%s) — using static defaults", exc)
        return {}


async def run_once() -> str:
    """
    Run one auto-generate cycle.

    Returns the YouTube video URL on success.
    Raises RuntimeError with a human-readable reason if skipped.
    """
    from content_factory.core.youtube_uploader import is_authenticated, upload_video
    from content_factory.core.subtitle_generator import generate_subtitles
    from content_factory.core.video_composer import compose

    # ── 0. Pre-flight checks ──────────────────────────────────────────────────
    if not YOUTUBE_CLIENT_SECRET:
        raise RuntimeError("YOUTUBE_CLIENT_SECRET not set in .env")
    if not is_authenticated(YOUTUBE_TOKEN_FILE):
        raise RuntimeError(
            "YouTube token missing — run: python main.py auth-youtube"
        )

    # ── 1. Pick top clip ──────────────────────────────────────────────────────
    top_sources = list_sources(WEB_USER_ID, "top_video")
    top_with_unused = [
        s for s in top_sources
        if count_unused_clips(WEB_USER_ID, s["id"]) > 0
    ]
    if not top_with_unused:
        raise RuntimeError("No unused top clips left — upload & cut new videos")

    top_source = random.choice(top_with_unused)
    top_clip   = pick_random_unused_clip(WEB_USER_ID, top_source["id"])
    if top_clip is None:
        raise RuntimeError("Race condition: top clip disappeared, retry later")

    # ── 2. Pick bottom clip ───────────────────────────────────────────────────
    bot_sources  = list_sources(WEB_USER_ID, "bottom_video")
    bot_with_clips = [s for s in bot_sources if s["clip_count"] > 0]
    if not bot_with_clips:
        raise RuntimeError("No bottom video clips — upload & cut bottom videos")

    bot_source = random.choice(bot_with_clips)
    bot_clip   = pick_random_clip(WEB_USER_ID, bot_source["id"])
    if bot_clip is None:
        raise RuntimeError("No bottom clip found")

    # ── 3. Pick banner ────────────────────────────────────────────────────────
    banners = [
        *list_files(WEB_USER_ID, "banner_image"),
        *list_files(WEB_USER_ID, "banner_video"),
    ]
    if not banners:
        raise RuntimeError("No banners — upload at least one banner")

    banner = random.choice(banners)

    logger.info(
        "[cron] Selected → top: %s | bottom: %s | banner: %s",
        top_clip["name"], bot_clip["name"], banner["name"],
    )

    # ── 4. Generate ───────────────────────────────────────────────────────────
    work_dir = OUTPUT_DIR / f"cron_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_event_loop()

    logger.info("[cron] Generating subtitles…")
    ass_path = await loop.run_in_executor(
        None, generate_subtitles, top_clip["file_path"], work_dir
    )

    logger.info("[cron] Composing video (FFmpeg)…")
    output_path = work_dir / "output.mp4"
    await loop.run_in_executor(
        None,
        lambda: compose(
            top_clip["file_path"],
            bot_clip["file_path"],
            banner["file_path"],
            ass_path,
            output_path,
            banner_animation=BANNER_ANIMATION,
        ),
    )

    # ── 5. Build metadata (AI or static) ─────────────────────────────────────
    static_tags = [t.strip() for t in YOUTUBE_TAGS.split(",") if t.strip()]

    if YOUTUBE_AI_DESCRIPTION:
        logger.info("[cron] Generating AI metadata for: %s", top_clip["name"])
        meta = await loop.run_in_executor(None, _generate_meta, top_clip["name"])
    else:
        meta = {}

    title       = meta.get("title") or (top_clip["name"][:87] + " #Shorts")
    description = meta.get("description") or YOUTUBE_DESCRIPTION
    tags        = meta.get("tags") or static_tags

    logger.info("[cron] Title: %s", title)

    # ── 6. Upload ─────────────────────────────────────────────────────────────
    logger.info("[cron] Uploading to YouTube (privacy=%s)…", YOUTUBE_PRIVACY_STATUS)
    video_id = await loop.run_in_executor(
        None,
        lambda: upload_video(
            output_path,
            title,
            description=description,
            tags=tags,
            privacy=YOUTUBE_PRIVACY_STATUS,
            client_secret_path=YOUTUBE_CLIENT_SECRET,
            token_path=YOUTUBE_TOKEN_FILE,
        ),
    )

    # ── 7. Mark used (only after confirmed upload) ────────────────────────────
    mark_used(top_clip["id"])

    url = f"https://youtube.com/shorts/{video_id}"
    logger.info("[cron] ✅ Done → %s", url)
    return url


async def cron_job(context) -> None:
    """
    Entry point for python-telegram-bot JobQueue.
    Runs run_once() and sends result/error to the bot owner if chat_id is set.
    """
    try:
        url = await run_once()
        logger.info("[cron] Cycle complete: %s", url)
        # Optional: notify bot owner
        if context.job.data and context.job.data.get("chat_id"):
            await context.bot.send_message(
                context.job.data["chat_id"],
                f"🤖 Авто-шортс загружен!\n{url}",
            )
    except RuntimeError as exc:
        logger.warning("[cron] Skipped: %s", exc)
    except Exception as exc:
        logger.error("[cron] Failed: %s", exc, exc_info=True)


def register(app, notify_chat_id: int | None = None) -> None:
    """
    Register the cron job with the bot's JobQueue.
    Call this inside build_bot() after the app is created.
    """
    if not YOUTUBE_CLIENT_SECRET:
        logger.info("[cron] YOUTUBE_CLIENT_SECRET not set — auto-upload disabled")
        return

    if YOUTUBE_CRON_INTERVAL_HOURS <= 0:
        logger.info("[cron] YOUTUBE_CRON_INTERVAL_HOURS=0 — auto-upload disabled")
        return

    interval = YOUTUBE_CRON_INTERVAL_HOURS * 3600
    app.job_queue.run_repeating(
        cron_job,
        interval=interval,
        first=60,  # first run 60 s after bot starts
        name="auto_generate",
        data={"chat_id": notify_chat_id},
    )
    logger.info(
        "[cron] Auto-upload scheduled every %.1f h (privacy=%s)",
        YOUTUBE_CRON_INTERVAL_HOURS,
        YOUTUBE_PRIVACY_STATUS,
    )
