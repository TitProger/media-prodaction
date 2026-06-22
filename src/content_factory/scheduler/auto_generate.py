"""
auto_generate.py — Cron job: pick random clips → generate Short → upload to YouTube.

Split-screen flow
-----------------
1. Pick a random top_video source with unused clips.
2. Pick a random unused clip from it.
3. Pick a random bottom_video clip.
4. Pick a random banner (image or video).
5. Generate subtitles + compose via FFmpeg (compose).
6. Upload to YouTube.
7. Mark the top clip as used.

Blog flow (single-clip, no banner)
-----------------------------------
1. Pick a random blog_video source with unused clips.
2. Pick a random unused clip from it.
3. Generate subtitles + compose via FFmpeg (compose_single, no banner).
4. Upload to YouTube.
5. Mark the blog clip as used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from pathlib import Path

import shutil
import time

from content_factory.config.settings import (
    LIBRARY_DB,
    ANTHROPIC_API_KEY,
    BANNER_ANIMATION,
    CLAUDE_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GROQ_API_KEY,
    GROQ_MODEL,
    OPENAI_API_KEY,
    OPENAI_MODEL,
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
    release_clip,
)

logger = logging.getLogger(__name__)


# ─── AI meta generation ───────────────────────────────────────────────────────

_META_SYSTEM = (
    "You are a YouTube Shorts content manager. "
    "Your goal is to maximise views by writing SEO-optimised, viral-trend-matching metadata."
)

_META_PROMPT = """\
Generate YouTube Shorts metadata for a video clip.

Clip title: {clip_title}
Mode: {mode}

Rules (STRICTLY follow):
- Reply ONLY with a valid JSON object — no markdown, no explanation.
- "title": max 80 chars, catchy and intriguing, in the SAME language as the clip title, add 1-2 relevant emojis
- "description": 80-150 chars ONLY — ultra-brief, punchy, hook-style. \
End with 3-5 trending hashtags (#Shorts #viral #trending etc.). NO long text.
- "tags": list of 25-35 strings — mix native-language keywords + English trending tags. \
Include: topic-specific, emotion tags, format tags (Shorts, Reels, viral, trending, fyp, foryou, \
foryoupage, explore), niche tags, and broad discovery tags. More tags = better reach.

JSON format:
{{"title": "...", "description": "...", "tags": ["...", "..."]}}
"""


def _generate_meta(clip_title: str, mode: str = "split") -> dict:
    """
    Ask AI to generate YouTube title, description, and tags.
    Returns dict with keys: title, description, tags (list[str]).
    Falls back to static defaults if no AI key or on error.
    """
    prompt = _META_PROMPT.format(clip_title=clip_title, mode=mode)

    raw: str | None = None
    try:
        if GROQ_API_KEY:
            from groq import Groq
            client = Groq(api_key=GROQ_API_KEY)
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=700,
                messages=[
                    {"role": "system", "content": _META_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = response.choices[0].message.content.strip()
            logger.info("[cron] Meta generated via Groq (%s)", GROQ_MODEL)
        elif OPENAI_API_KEY:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=700,
                messages=[
                    {"role": "system", "content": _META_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = response.choices[0].message.content.strip()
            logger.info("[cron] Meta generated via OpenAI (%s)", OPENAI_MODEL)
        elif GEMINI_API_KEY:
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
                max_tokens=700,
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


# ─── Split-screen pipeline ────────────────────────────────────────────────────

async def run_once_split() -> str:
    """
    One split-screen auto-generate cycle.
    Returns the YouTube video URL on success.
    Raises RuntimeError with a human-readable reason if skipped.
    """
    from content_factory.core.youtube_uploader import upload_video
    from content_factory.core.subtitle_generator import generate_subtitles
    from content_factory.core.video_composer import compose

    # 1. Pick top clip
    top_sources = list_sources(WEB_USER_ID, "top_video")
    top_with_unused = [s for s in top_sources if count_unused_clips(WEB_USER_ID, s["id"]) > 0]
    if not top_with_unused:
        raise RuntimeError("No unused top clips — upload & cut new videos")

    top_source = random.choice(top_with_unused)
    top_clip   = pick_random_unused_clip(WEB_USER_ID, top_source["id"])
    if top_clip is None:
        raise RuntimeError("Race condition: top clip disappeared, retry later")

    # 2. Pick bottom clip
    bot_sources    = list_sources(WEB_USER_ID, "bottom_video")
    bot_with_clips = [s for s in bot_sources if s["clip_count"] > 0]
    if not bot_with_clips:
        raise RuntimeError("No bottom video clips — upload & cut bottom videos")

    bot_source = random.choice(bot_with_clips)
    bot_clip   = pick_random_clip(WEB_USER_ID, bot_source["id"])
    if bot_clip is None:
        raise RuntimeError("No bottom clip found")

    # 3. Pick banner
    banners = [
        *list_files(WEB_USER_ID, "banner_image"),
        *list_files(WEB_USER_ID, "banner_video"),
    ]
    if not banners:
        raise RuntimeError("No banners — upload at least one banner")

    banner = random.choice(banners)

    logger.info(
        "[cron/split] top=%s | bottom=%s | banner=%s",
        top_clip["name"], bot_clip["name"], banner["name"],
    )

    # 4. Generate subtitles + compose
    work_dir = OUTPUT_DIR / f"cron_split_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    logger.info("[cron/split] Generating subtitles…")
    ass_path = await loop.run_in_executor(
        None, generate_subtitles, top_clip["file_path"], work_dir
    )

    logger.info("[cron/split] Composing video…")
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

    # 5. Build metadata
    static_tags = [t.strip() for t in YOUTUBE_TAGS.split(",") if t.strip()]
    if YOUTUBE_AI_DESCRIPTION:
        logger.info("[cron/split] Generating AI metadata for: %s", top_clip["name"])
        meta = await loop.run_in_executor(None, _generate_meta, top_clip["name"], "split-screen gaming shorts")
    else:
        meta = {}

    title       = meta.get("title") or (top_clip["name"][:87] + " #Shorts")
    description = meta.get("description") or YOUTUBE_DESCRIPTION
    tags        = meta.get("tags") or static_tags

    logger.info("[cron/split] Title: %s | Tags: %d", title, len(tags))

    # 6. Upload
    size_mb = output_path.stat().st_size / 1024 / 1024
    logger.info("[cron/split] Uploading %.1f MB (privacy=%s)…", size_mb, YOUTUBE_PRIVACY_STATUS)
    try:
        video_id = await loop.run_in_executor(
            None,
            lambda: upload_video(
                output_path, title,
                description=description,
                tags=tags,
                privacy=YOUTUBE_PRIVACY_STATUS,
                client_secret_path=YOUTUBE_CLIENT_SECRET,
                token_path=YOUTUBE_TOKEN_FILE,
            ),
        )
    except Exception as exc:
        release_clip(top_clip["id"])
        exc_str = str(exc)
        if "uploadLimitExceeded" in exc_str:
            raise RuntimeError("YouTube upload limit — verify at youtube.com/verify")
        if "quotaExceeded" in exc_str or "forbidden" in exc_str.lower():
            raise RuntimeError(f"YouTube quota/permission error: {exc_str[:200]}")
        raise

    mark_used(top_clip["id"])
    shutil.rmtree(work_dir, ignore_errors=True)  # free disk — the video is on YouTube now
    url = f"https://youtube.com/shorts/{video_id}"
    logger.info("[cron/split] ✅ Done → %s", url)
    return url


# ─── Blog pipeline (single-clip, with optional banner) ───────────────────────

async def run_once_blog() -> str:
    """
    One blog-video auto-generate cycle (single clip + optional banner).
    Picks a random banner from the library if available.
    Returns the YouTube video URL on success.
    Raises RuntimeError with a human-readable reason if skipped.
    """
    from content_factory.core.youtube_uploader import upload_video
    from content_factory.core.subtitle_generator import generate_subtitles
    from content_factory.core.video_composer import compose_single

    # 1. Pick blog clip
    blog_sources = list_sources(WEB_USER_ID, "blog_video")
    blog_with_unused = [s for s in blog_sources if count_unused_clips(WEB_USER_ID, s["id"]) > 0]
    if not blog_with_unused:
        raise RuntimeError("No unused blog clips — upload & cut new blog videos")

    blog_source = random.choice(blog_with_unused)
    blog_clip   = pick_random_unused_clip(WEB_USER_ID, blog_source["id"])
    if blog_clip is None:
        raise RuntimeError("Race condition: blog clip disappeared, retry later")

    logger.info("[cron/blog] clip=%s", blog_clip["name"])

    # 2. Pick banner (optional — blog works fine without one)
    banners = [
        *list_files(WEB_USER_ID, "banner_image"),
        *list_files(WEB_USER_ID, "banner_video"),
    ]
    banner = random.choice(banners) if banners else None
    if banner:
        logger.info("[cron/blog] banner=%s", banner["name"])
    else:
        logger.info("[cron/blog] no banner found — composing without banner")

    # 3. Generate subtitles + compose
    work_dir = OUTPUT_DIR / f"cron_blog_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    logger.info("[cron/blog] Generating subtitles...")
    ass_path = await loop.run_in_executor(
        None, generate_subtitles, blog_clip["file_path"], work_dir
    )

    logger.info("[cron/blog] Composing video...")
    output_path = work_dir / "output.mp4"
    banner_path = banner["file_path"] if banner else None
    await loop.run_in_executor(
        None,
        lambda: compose_single(
            video=blog_clip["file_path"],
            subtitle_file=ass_path,
            output_path=output_path,
            banner_image=banner_path,
            banner_animation=BANNER_ANIMATION,
        ),
    )

    # 4. Build metadata
    static_tags = [t.strip() for t in YOUTUBE_TAGS.split(",") if t.strip()]
    if YOUTUBE_AI_DESCRIPTION:
        logger.info("[cron/blog] Generating AI metadata for: %s", blog_clip["name"])
        meta = await loop.run_in_executor(None, _generate_meta, blog_clip["name"], "blog talking-head vertical video")
    else:
        meta = {}

    title       = meta.get("title") or (blog_clip["name"][:87] + " #Shorts")
    description = meta.get("description") or YOUTUBE_DESCRIPTION
    tags        = meta.get("tags") or static_tags

    logger.info("[cron/blog] Title: %s | Tags: %d", title, len(tags))

    # 5. Upload
    size_mb = output_path.stat().st_size / 1024 / 1024
    logger.info("[cron/blog] Uploading %.1f MB (privacy=%s)…", size_mb, YOUTUBE_PRIVACY_STATUS)
    try:
        video_id = await loop.run_in_executor(
            None,
            lambda: upload_video(
                output_path, title,
                description=description,
                tags=tags,
                privacy=YOUTUBE_PRIVACY_STATUS,
                client_secret_path=YOUTUBE_CLIENT_SECRET,
                token_path=YOUTUBE_TOKEN_FILE,
            ),
        )
    except Exception as exc:
        release_clip(blog_clip["id"])
        exc_str = str(exc)
        if "uploadLimitExceeded" in exc_str:
            raise RuntimeError("YouTube upload limit — verify at youtube.com/verify")
        if "quotaExceeded" in exc_str or "forbidden" in exc_str.lower():
            raise RuntimeError(f"YouTube quota/permission error: {exc_str[:200]}")
        raise

    mark_used(blog_clip["id"])
    shutil.rmtree(work_dir, ignore_errors=True)  # free disk — the video is on YouTube now
    url = f"https://youtube.com/shorts/{video_id}"
    logger.info("[cron/blog] ✅ Done → %s", url)
    return url


# ─── Unified cron entry point ─────────────────────────────────────────────────

async def run_once() -> str:
    """
    Run one auto-generate cycle — randomly picks split-screen or blog mode
    based on what clips are available.
    """
    from content_factory.core.youtube_uploader import is_authenticated

    if not YOUTUBE_CLIENT_SECRET:
        raise RuntimeError("YOUTUBE_CLIENT_SECRET not set in .env")
    if not is_authenticated(YOUTUBE_TOKEN_FILE):
        raise RuntimeError("YouTube token missing — run: python main.py auth-youtube")

    # Check what's available
    blog_sources   = list_sources(WEB_USER_ID, "blog_video")
    blog_available = any(count_unused_clips(WEB_USER_ID, s["id"]) > 0 for s in blog_sources)

    top_sources   = list_sources(WEB_USER_ID, "top_video")
    split_available = any(count_unused_clips(WEB_USER_ID, s["id"]) > 0 for s in top_sources)

    if not blog_available and not split_available:
        raise RuntimeError("No unused clips in any category — upload & cut new videos")

    # Choose mode
    if blog_available and split_available:
        mode = random.choice(["blog", "split"])
    elif blog_available:
        mode = "blog"
    else:
        mode = "split"

    logger.info("[cron] Selected mode: %s", mode)

    if mode == "blog":
        return await run_once_blog()
    else:
        return await run_once_split()


async def weekly_reset_used(context) -> None:
    """Reset used=0 and in_progress=0 for all clips so they re-enter rotation."""
    from content_factory.db.library import _connect
    with _connect() as conn:
        result = conn.execute(
            "UPDATE media_files SET used=0, in_progress=0 WHERE subtype='clip'"
        )
    logger.info("[cron] Weekly reset: %d clips returned to rotation", result.rowcount)


async def hourly_backup_db(context) -> None:
    """Copy library.db → library.db.backup for disaster recovery."""
    db_path = LIBRARY_DB
    if not db_path.exists():
        return
    backup = db_path.with_suffix(".db.backup")
    shutil.copy2(db_path, backup)
    size_kb = backup.stat().st_size // 1024
    logger.info("[cron] DB backup → %s (%d KB)", backup.name, size_kb)


async def cron_job(context) -> None:
    """Entry point for python-telegram-bot JobQueue."""
    try:
        url = await run_once()
        logger.info("[cron] Cycle complete: %s", url)
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
    """Register the cron job with the bot's JobQueue."""
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
        first=60,
        name="auto_generate",
        data={"chat_id": notify_chat_id},
    )
    logger.info(
        "[cron] Auto-upload scheduled every %.1f h (privacy=%s)",
        YOUTUBE_CRON_INTERVAL_HOURS,
        YOUTUBE_PRIVACY_STATUS,
    )

    # Hourly DB backup
    app.job_queue.run_repeating(
        hourly_backup_db,
        interval=3600,
        first=120,
        name="db_backup",
    )
    logger.info("[cron] DB backup scheduled every 1 h")

    # Weekly reset of used clips (every 7 days)
    app.job_queue.run_repeating(
        weekly_reset_used,
        interval=7 * 24 * 3600,
        first=300,
        name="weekly_reset",
    )
    logger.info("[cron] Weekly clip reset scheduled every 7 days")
