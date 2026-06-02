"""
clip_finder.py — Find the best short clips in a long video.

Pipeline
--------
1. Whisper  — transcribe audio → timestamped segment list
2. Claude API — analyze transcript → JSON [{start, end, title}]
3. Return   — validated, sorted clip list ready for FFmpeg

Usage
-----
    from content_factory.core.clip_finder import find_best_clips
    clips = find_best_clips("interview.mp4")
    # → [{"start": 12.5, "end": 48.3, "title": "О детстве"}]
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import anthropic
import whisper

from content_factory.config.settings import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLIP_COUNT,
    CLIP_MAX_DURATION,
    CLIP_MIN_DURATION,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
)

logger = logging.getLogger(__name__)

_whisper_cache: dict = {}


def _get_whisper(name: str):
    if name not in _whisper_cache:
        logger.info("Loading Whisper model '%s'…", name)
        _whisper_cache[name] = whisper.load_model(name)
    return _whisper_cache[name]


# ─── Transcript formatting ────────────────────────────────────────────────────

def _format_transcript(segments: list) -> str:
    """
    Convert Whisper segments into a compact timestamped text block.

    Example line:
        [12.5 - 48.3] И вот тогда я понял, что всё изменилось.
    """
    lines: list[str] = []
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        start = seg["start"]
        end = seg["end"]
        lines.append(f"[{start:.1f} - {end:.1f}] {text}")
    return "\n".join(lines)


# ─── Prompts ──────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a professional video editor specializing in social media content for YouTube Shorts and TikTok Reels.
Your expertise is identifying engaging, self-contained moments in long-form interview videos.\
"""

_USER_TMPL = """\
Analyze the following video transcript and find the {count} most engaging moments \
suitable for social media Shorts.

Selection criteria for each clip:
• Duration: {min_sec}–{max_sec} seconds
• Starts and ends at a natural speech boundary (complete sentence, never mid-word)
• Fully self-contained — makes sense without watching the rest of the video
• Contains a complete insight, story, emotional moment, or useful tip
• Clips must NOT overlap each other

Transcript (format: [start_seconds - end_seconds] text):
{transcript}

Reply with ONLY a valid JSON array — no markdown, no explanation, nothing else:
[
  {{"start": 0.0, "end": 0.0, "title": "Concise engaging title max 60 chars"}},
  ...
]

Sort by start time. Use the exact numeric values from the transcript timestamps.\
"""


# ─── Main function ────────────────────────────────────────────────────────────

def find_best_clips(video_path: str | Path) -> list[dict]:
    """
    Transcribe *video_path* with Whisper, then ask Claude API to pick the
    best {CLIP_COUNT} clips of {CLIP_MIN_DURATION}–{CLIP_MAX_DURATION} seconds.

    Returns
    -------
    list[dict]  sorted by start time, each item: {"start", "end", "title"}

    Raises
    ------
    ValueError  if the API returns unparseable JSON
    RuntimeError if ANTHROPIC_API_KEY is not set
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. "
            "Add it to your .env file: ANTHROPIC_API_KEY=sk-ant-..."
        )

    video_path = Path(video_path)

    # ── 1. Transcribe ─────────────────────────────────────────────────────────
    logger.info("[clip_finder] Transcribing '%s'…", video_path.name)
    wmodel = _get_whisper(WHISPER_MODEL)
    result = wmodel.transcribe(
        str(video_path),
        language=WHISPER_LANGUAGE,
        word_timestamps=False,
        verbose=False,
    )
    transcript = _format_transcript(result["segments"])
    logger.info(
        "[clip_finder] Transcript ready: %d segments, %d chars",
        len(result["segments"]),
        len(transcript),
    )

    # ── 2. Ask Claude API ─────────────────────────────────────────────────────
    logger.info("[clip_finder] Calling %s for clip analysis…", CLAUDE_MODEL)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_text = _USER_TMPL.format(
        count=CLIP_COUNT,
        min_sec=int(CLIP_MIN_DURATION),
        max_sec=int(CLIP_MAX_DURATION),
        transcript=transcript,
    )

    # Use prompt caching: system prompt + long transcript are stable → cache both
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": _SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    )

    raw = message.content[0].text.strip()
    usage = message.usage
    logger.info(
        "[clip_finder] API done | in=%d out=%d cache_read=%d cache_created=%d",
        usage.input_tokens,
        usage.output_tokens,
        getattr(usage, "cache_read_input_tokens", 0),
        getattr(usage, "cache_creation_input_tokens", 0),
    )

    # ── 3. Parse & validate ───────────────────────────────────────────────────
    # Strip any accidental markdown code block
    json_match = re.search(r"\[.*?\]", raw, re.DOTALL)
    if not json_match:
        raise ValueError(
            f"Claude did not return a JSON array.\nRaw response:\n{raw[:800]}"
        )

    clips_raw: list[dict] = json.loads(json_match.group())

    valid: list[dict] = []
    for clip in clips_raw:
        try:
            start = float(clip["start"])
            end = float(clip["end"])
            duration = end - start
            title = str(clip.get("title", "Клип")).strip()[:80]
            # Accept clips within [min, max + 10s tolerance]
            if CLIP_MIN_DURATION <= duration <= CLIP_MAX_DURATION + 10:
                valid.append({"start": start, "end": end, "title": title})
        except (KeyError, TypeError, ValueError):
            continue

    valid.sort(key=lambda c: c["start"])
    logger.info(
        "[clip_finder] %d valid clips selected (Claude returned %d)",
        len(valid),
        len(clips_raw),
    )
    return valid
