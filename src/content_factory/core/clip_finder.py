"""
clip_finder.py — Find the best short clips in a long video.

Pipeline
--------
1. Whisper  — transcribe audio → timestamped segment list
2. AI API   — analyze transcript → JSON [{start, end, title}]
             Provider priority: Gemini (free tier) → Claude (paid)
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

import whisper

from content_factory.config.settings import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLIP_COUNT,
    CLIP_MAX_DURATION,
    CLIP_MIN_DURATION,
    GEMINI_API_KEY,
    GEMINI_MODEL,
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
    lines: list[str] = []
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        lines.append(f"[{seg['start']:.1f} - {seg['end']:.1f}] {text}")
    return "\n".join(lines)


# ─── Prompts ──────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a professional video editor specializing in social media content "
    "for YouTube Shorts and TikTok Reels. "
    "Your expertise is identifying engaging, self-contained moments in long-form interview videos."
)

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


# ─── Heuristic fallback (no API needed) ──────────────────────────────────────

def _find_clips_heuristic(segments: list, count: int) -> list[dict]:
    """
    Pick best clips using only Whisper segments — no AI API required.

    Algorithm:
    1. Slide a window across segments collecting chunks of CLIP_MIN–CLIP_MAX duration.
    2. Score each chunk: longer = better, bonus for ending at a sentence boundary.
    3. Remove overlapping chunks (greedy), return top `count`.
    """
    candidates = []

    for i, seg in enumerate(segments):
        start = seg["start"]
        text_acc = []
        end = start

        for j in range(i, len(segments)):
            s = segments[j]
            end = s["end"]
            text_acc.append(s["text"].strip())
            duration = end - start

            if duration < CLIP_MIN_DURATION:
                continue
            if duration > CLIP_MAX_DURATION:
                break

            # Score: prefer longer clips that end on sentence boundary
            text = " ".join(text_acc)
            sentence_bonus = 1.2 if text.rstrip().endswith((".", "!", "?", "…")) else 1.0
            score = duration * sentence_bonus

            # Build a short title from first ~6 words
            words = text.split()
            title = " ".join(words[:6]) + ("…" if len(words) > 6 else "")
            candidates.append({"start": start, "end": end, "title": title, "score": score})

    if not candidates:
        return []

    # Sort by score descending, then remove overlaps greedily
    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected = []
    for c in candidates:
        overlap = any(
            not (c["end"] <= s["start"] or c["start"] >= s["end"])
            for s in selected
        )
        if not overlap:
            selected.append(c)
        if len(selected) >= count:
            break

    # Return sorted by start time, strip internal score field
    selected.sort(key=lambda c: c["start"])
    return [{"start": c["start"], "end": c["end"], "title": c["title"]} for c in selected]


# ─── AI backends ─────────────────────────────────────────────────────────────

_MAX_TRANSCRIPT_CHARS = 12_000  # ~3k tokens — fits comfortably in free tier


def _ask_gemini(user_text: str) -> str:
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"{_SYSTEM}\n\n{user_text}",
    )
    logger.info("[clip_finder] Gemini done | model=%s", GEMINI_MODEL)
    return response.text.strip()


def _ask_claude(user_text: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [{"type": "text", "text": user_text, "cache_control": {"type": "ephemeral"}}]}],
    )
    usage = message.usage
    logger.info(
        "[clip_finder] Claude done | in=%d out=%d cache_read=%d cache_created=%d",
        usage.input_tokens, usage.output_tokens,
        getattr(usage, "cache_read_input_tokens", 0),
        getattr(usage, "cache_creation_input_tokens", 0),
    )
    return message.content[0].text.strip()


def _ask_ai(user_text: str) -> str:
    """Call Gemini if key is set, otherwise fall back to Claude."""
    if GEMINI_API_KEY:
        logger.info("[clip_finder] Using Gemini (%s)", GEMINI_MODEL)
        return _ask_gemini(user_text)
    if ANTHROPIC_API_KEY:
        logger.info("[clip_finder] Using Claude (%s)", CLAUDE_MODEL)
        return _ask_claude(user_text)
    raise RuntimeError("no_api_key")


# ─── Main function ────────────────────────────────────────────────────────────

def find_best_clips(video_path: str | Path) -> list[dict]:
    """
    Transcribe *video_path* with Whisper, then ask an AI to pick the
    best {CLIP_COUNT} clips of {CLIP_MIN_DURATION}–{CLIP_MAX_DURATION} seconds.

    Returns
    -------
    list[dict]  sorted by start time, each item: {"start", "end", "title"}
    """
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
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:_MAX_TRANSCRIPT_CHARS]
        logger.warning(
            "[clip_finder] Transcript truncated to %d chars to fit token limits",
            _MAX_TRANSCRIPT_CHARS,
        )
    logger.info(
        "[clip_finder] Transcript ready: %d segments, %d chars",
        len(result["segments"]), len(transcript),
    )

    # ── 2. Ask AI (with heuristic fallback) ──────────────────────────────────
    user_text = _USER_TMPL.format(
        count=CLIP_COUNT,
        min_sec=int(CLIP_MIN_DURATION),
        max_sec=int(CLIP_MAX_DURATION),
        transcript=transcript,
    )

    try:
        raw = _ask_ai(user_text)

        # ── 3. Parse & validate ───────────────────────────────────────────────
        json_match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not json_match:
            raise ValueError(f"AI did not return a JSON array.\nRaw response:\n{raw[:800]}")

        clips_raw: list[dict] = json.loads(json_match.group())
        valid: list[dict] = []
        for clip in clips_raw:
            try:
                start = float(clip["start"])
                end = float(clip["end"])
                duration = end - start
                title = str(clip.get("title", "Клип")).strip()[:80]
                if CLIP_MIN_DURATION <= duration <= CLIP_MAX_DURATION + 10:
                    valid.append({"start": start, "end": end, "title": title})
            except (KeyError, TypeError, ValueError):
                continue

        valid.sort(key=lambda c: c["start"])
        logger.info("[clip_finder] %d valid clips selected (AI returned %d)", len(valid), len(clips_raw))
        return valid

    except Exception as exc:
        err = str(exc)
        if "429" in err or "RESOURCE_EXHAUSTED" in err or "no_api_key" in err:
            logger.warning(
                "[clip_finder] AI unavailable (%s) — switching to heuristic mode",
                err[:120],
            )
            valid = _find_clips_heuristic(result["segments"], CLIP_COUNT)
            logger.info("[clip_finder] Heuristic selected %d clips", len(valid))
            return valid
        raise
