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

import hashlib
import json
import logging
import re
from pathlib import Path

import whisper

from content_factory.config.settings import (
    ANTHROPIC_API_KEY,
    BLOG_CLIP_MAX_DURATION,
    BLOG_CLIP_MIN_DURATION,
    CLAUDE_MODEL,
    CLIP_COUNT,
    CLIP_MAX_DURATION,
    CLIP_MIN_DURATION,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GROQ_API_KEY,
    GROQ_MODEL,
    OPENAI_API_KEY,
    OPENAI_MODEL,
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

# ── General (split-screen / top_video) ───────────────────────────────────────

_SYSTEM = (
    "You are a professional video editor specializing in social media content "
    "for YouTube Shorts and TikTok Reels. "
    "Your expertise is identifying engaging, self-contained moments in long-form interview videos."
)

_USER_TMPL = """\
Analyze the following video transcript and find the {count} most engaging moments \
suitable for social media Shorts.

Selection criteria for each clip:
• Duration: strictly {min_sec}–{max_sec} seconds (end - start must be in this range)
• Starts and ends at a natural speech boundary (complete sentence, never mid-word)
• Fully self-contained — makes sense without watching the rest of the video
• Contains a complete insight, story, emotional moment, or useful tip
• Clips must NOT overlap each other

Transcript (format: [start_seconds - end_seconds] text):
{transcript}

STRICT OUTPUT RULES:
- Reply with ONLY a valid JSON array — no markdown fences, no explanation, nothing else
- Every "start" and "end" must be exact float values copied from the transcript timestamps
- (end - start) MUST be between {min_sec} and {max_sec} seconds — violating this disqualifies the clip
- "title" must be max 60 characters, in the same language as the transcript

[
  {{"start": 0.0, "end": 0.0, "title": "Concise engaging title max 60 chars"}},
  ...
]

Sort by start time.\
"""

# ── Blog / vlog (single-video mode) ──────────────────────────────────────────

_BLOG_SYSTEM = (
    "You are a viral content strategist specializing in YouTube Shorts and TikTok. "
    "Your job is to find the most scroll-stopping, hook-driven moments in blog and vlog videos "
    "that will make viewers stop scrolling within the first 2 seconds and watch until the end."
)

_BLOG_USER_TMPL = """\
Analyze the following vlog/blog video transcript. \
Find the {count} best moments to cut as viral Shorts.

SELECTION RULES (follow strictly):
1. Duration: each clip must be between {min_sec} and {max_sec} seconds (end - start). \
Vary the lengths naturally — do NOT make all clips the same duration. \
Let the natural end of a thought determine where the clip ends (within the allowed range).
2. Hook first — the clip must start with a sentence that immediately grabs attention: \
a surprising fact, a bold claim, an emotional peak, a question, or a punchline
3. Complete thought — the clip must end at a natural sentence boundary, never mid-word
4. Self-contained — a viewer who has never seen this video must fully understand the clip
5. No overlaps — clips must not overlap each other
6. Prefer moments with: conflict, emotion, humour, revelation, controversy, or a clear takeaway

Transcript (format: [start_seconds - end_seconds] text):
{transcript}

STRICT OUTPUT FORMAT — return ONLY this JSON array, nothing else, no markdown fences:
[
  {{"start": 12.5, "end": 47.3, "title": "Hook-driven title in transcript language, max 60 chars"}},
  {{"start": 63.0, "end": 98.2, "title": "..."}},
  ...
]

VALIDATION CHECKLIST before responding:
- ✅ Each (end - start) is between {min_sec} and {max_sec} seconds
- ✅ Clip durations vary naturally (not all the same length)
- ✅ "start" and "end" are exact floats from the transcript
- ✅ No two clips overlap
- ✅ Output is a raw JSON array only — no other text\
"""


# ─── Heuristic fallback (no API needed) ──────────────────────────────────────

def _find_clips_heuristic(
    segments: list,
    count: int,
    min_duration: float = CLIP_MIN_DURATION,
    max_duration: float = CLIP_MAX_DURATION,
) -> list[dict]:
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

            if duration < min_duration:
                continue
            if duration > max_duration:
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


def _ask_groq(system: str, user_text: str) -> str:
    from groq import Groq  # lazy import — optional dependency
    client = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    )
    logger.info("[clip_finder] Groq done | model=%s", GROQ_MODEL)
    return response.choices[0].message.content.strip()


def _ask_openai(system: str, user_text: str) -> str:
    from openai import OpenAI  # lazy import — optional dependency
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    )
    logger.info("[clip_finder] OpenAI done | model=%s", OPENAI_MODEL)
    return response.choices[0].message.content.strip()


def _ask_gemini(user_text: str, system: str = _SYSTEM) -> str:
    from google import genai
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"{system}\n\n{user_text}",
    )
    logger.info("[clip_finder] Gemini done | model=%s", GEMINI_MODEL)
    return response.text.strip()


def _ask_claude(user_text: str, system: str = _SYSTEM) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
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


def _ask_ai(system: str, user_text: str) -> str:
    """Try providers in order: Groq → OpenAI → Gemini → Claude."""
    if GROQ_API_KEY:
        logger.info("[clip_finder] Using Groq (%s)", GROQ_MODEL)
        return _ask_groq(system, user_text)
    if OPENAI_API_KEY:
        logger.info("[clip_finder] Using OpenAI (%s)", OPENAI_MODEL)
        return _ask_openai(system, user_text)
    if GEMINI_API_KEY:
        logger.info("[clip_finder] Using Gemini (%s)", GEMINI_MODEL)
        return _ask_gemini(user_text, system)
    if ANTHROPIC_API_KEY:
        logger.info("[clip_finder] Using Claude (%s)", CLAUDE_MODEL)
        return _ask_claude(user_text, system)
    raise RuntimeError("no_api_key")


# ─── Main function ────────────────────────────────────────────────────────────

def find_best_clips(video_path: str | Path, category: str = "top_video") -> list[dict]:
    """
    Transcribe *video_path* with Whisper, then ask an AI to pick the
    best {CLIP_COUNT} clips of {CLIP_MIN_DURATION}–{CLIP_MAX_DURATION} seconds.

    Returns
    -------
    list[dict]  sorted by start time, each item: {"start", "end", "title"}
    """
    video_path = Path(video_path)

    # Use blog-specific duration limits for blog_video category
    min_dur = BLOG_CLIP_MIN_DURATION if category == "blog_video" else CLIP_MIN_DURATION
    max_dur = BLOG_CLIP_MAX_DURATION if category == "blog_video" else CLIP_MAX_DURATION

    # ── 1. Transcribe (with disk cache) ──────────────────────────────────────
    cache_file = video_path.with_suffix(".whisper_cache.json")
    file_hash = hashlib.md5(video_path.read_bytes()).hexdigest()[:12]

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("hash") == file_hash and cached.get("model") == WHISPER_MODEL:
                logger.info("[clip_finder] Using cached transcript for '%s'", video_path.name)
                segments = cached["segments"]
            else:
                segments = None
        except Exception:
            segments = None
    else:
        segments = None

    if segments is None:
        logger.info("[clip_finder] Transcribing '%s'…", video_path.name)
        wmodel = _get_whisper(WHISPER_MODEL)
        result = wmodel.transcribe(
            str(video_path),
            language=WHISPER_LANGUAGE,
            word_timestamps=False,
            verbose=False,
        )
        segments = result["segments"]
        try:
            cache_file.write_text(
                json.dumps({"hash": file_hash, "model": WHISPER_MODEL, "segments": segments},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("[clip_finder] Transcript cached → %s", cache_file.name)
        except Exception as e:
            logger.warning("[clip_finder] Could not write transcript cache: %s", e)

    transcript = _format_transcript(segments)
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        lines = transcript.splitlines()
        truncated = []
        total = 0
        for line in lines:
            if total + len(line) + 1 > _MAX_TRANSCRIPT_CHARS:
                break
            truncated.append(line)
            total += len(line) + 1
        transcript = "\n".join(truncated)
        logger.warning(
            "[clip_finder] Transcript truncated to %d lines / %d chars to fit token limits",
            len(truncated), len(transcript),
        )
    logger.info(
        "[clip_finder] Transcript ready: %d segments, %d chars",
        len(segments), len(transcript),
    )

    # ── 2. Ask AI (with heuristic fallback) ──────────────────────────────────
    is_blog = category == "blog_video"
    system_prompt = _BLOG_SYSTEM if is_blog else _SYSTEM
    user_tmpl     = _BLOG_USER_TMPL if is_blog else _USER_TMPL

    user_text = user_tmpl.format(
        count=CLIP_COUNT,
        min_sec=int(min_dur),
        max_sec=int(max_dur),
        transcript=transcript,
    )

    logger.info("[clip_finder] Using %s prompt (min=%ss max=%ss)",
                "BLOG" if is_blog else "STANDARD", int(min_dur), int(max_dur))

    try:
        raw = _ask_ai(system_prompt, user_text)

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
                if min_dur <= duration <= max_dur + 10:
                    valid.append({"start": start, "end": end, "title": title})
            except (KeyError, TypeError, ValueError):
                continue

        valid.sort(key=lambda c: c["start"])
        if not valid and clips_raw:
            logger.warning(
                "[clip_finder] 0 valid clips — AI returned these (all failed duration check %s–%ss): %s",
                min_dur, max_dur + 10,
                [(c.get("start"), c.get("end")) for c in clips_raw],
            )
            logger.warning("[clip_finder] Falling back to heuristic mode after AI returned 0 valid clips")
            heuristic = _find_clips_heuristic(segments, CLIP_COUNT, min_dur, max_dur)
            logger.info("[clip_finder] Heuristic selected %d clips", len(heuristic))
            return heuristic
        logger.info("[clip_finder] %d valid clips selected (AI returned %d)", len(valid), len(clips_raw))
        return valid

    except Exception as exc:
        err = str(exc)
        if "429" in err or "RESOURCE_EXHAUSTED" in err or "no_api_key" in err:
            logger.warning(
                "[clip_finder] AI unavailable (%s) — switching to heuristic mode",
                err[:120],
            )
            valid = _find_clips_heuristic(segments, CLIP_COUNT, min_dur, max_dur)
            logger.info("[clip_finder] Heuristic selected %d clips", len(valid))
            return valid
        raise
