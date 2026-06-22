"""
subtitle_generator.py
---------------------
Transcribes audio via Whisper and produces a Shorts-style .ass subtitle file:
- Word-level timestamps (word_timestamps=True)
- Karaoke fill: current word highlighted in yellow, upcoming in white
- Max SUBTITLE_MAX_WORDS words per cue (punchy, readable on phone)
- Centred at the split line between top and bottom panels
"""
from __future__ import annotations

import logging
from pathlib import Path

from content_factory.core._sync import HEAVY_LOCK
from content_factory.core.whisper_cache import get_model as _get_model
from content_factory.config.settings import (
    OUTPUT_HEIGHT,
    SUBTITLE_ALIGNMENT,
    SUBTITLE_BACK_COLOR,
    SUBTITLE_BOLD,
    SUBTITLE_FONT_NAME,
    SUBTITLE_FONT_SIZE,
    SUBTITLE_MARGIN_V,
    SUBTITLE_MAX_WORDS,
    SUBTITLE_OUTLINE,
    SUBTITLE_OUTLINE_COLOR,
    SUBTITLE_PRIMARY_COLOR,
    SUBTITLE_SECONDARY_COLOR,
    SUBTITLE_SHADOW,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
)

logger = logging.getLogger(__name__)


_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: {play_res_y}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{primary},{secondary},{outline},{back},{bold},0,0,0,100,100,0,0,1,{outline_px},{shadow},{alignment},10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(seconds: float) -> str:
    """Float seconds → ASS timestamp H:MM:SS.cc"""
    cs = int(round((seconds % 1) * 100))
    s = int(seconds)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}.{cs:02d}"


def _build_events(segments: list[dict]) -> str:
    """
    Build ASS dialogue events with word-level karaoke timing.

    Each cue shows SUBTITLE_MAX_WORDS words. Words use \\kf tags so the
    current word fills yellow (PrimaryColour) left-to-right while upcoming
    words stay white (SecondaryColour).

    Falls back to interpolated timing if Whisper didn't return word timestamps.
    """
    lines = []

    for seg in segments:
        words = seg.get("words") or []

        if words:
            # Word-level path: accurate per-word start/end from Whisper
            chunks = [words[i:i + SUBTITLE_MAX_WORDS] for i in range(0, len(words), SUBTITLE_MAX_WORDS)]
            for chunk in chunks:
                t_start = chunk[0]["start"]
                t_end = chunk[-1]["end"]
                parts = []
                for w in chunk:
                    dur_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
                    parts.append(f"{{\\kf{dur_cs}}}{w['word'].strip().upper()}")
                text = " ".join(parts)
                lines.append(f"Dialogue: 0,{_ts(t_start)},{_ts(t_end)},Default,,0,0,0,,{text}")
        else:
            # Fallback: no word timestamps — interpolate across the segment
            word_list = seg["text"].strip().split()
            if not word_list:
                continue
            seg_start, seg_end = seg["start"], seg["end"]
            duration = seg_end - seg_start
            chunks = [word_list[i:i + SUBTITLE_MAX_WORDS] for i in range(0, len(word_list), SUBTITLE_MAX_WORDS)]
            n = len(chunks)
            for idx, chunk in enumerate(chunks):
                t_start = seg_start + (duration * idx / n)
                t_end = seg_start + (duration * (idx + 1) / n)
                word_dur_cs = max(1, int(round((t_end - t_start) / len(chunk) * 100)))
                parts = [f"{{\\kf{word_dur_cs}}}{w.upper()}" for w in chunk]
                lines.append(f"Dialogue: 0,{_ts(t_start)},{_ts(t_end)},Default,,0,0,0,,{' '.join(parts)}")

    return "\n".join(lines)


def generate_subtitles(
    video_path: str | Path,
    output_dir: str | Path,
    model_name: str | None = None,
    margin_v: int | None = None,
) -> Path:
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ass_path = output_dir / f"{video_path.stem}_subtitles.ass"

    model = _get_model(model_name or WHISPER_MODEL)

    logger.info("Transcribing '%s'…", video_path)
    # Serialize the RAM-heavy transcription process-wide (cron + web + bot share
    # one lock) so two Whisper runs never coexist — critical on a 2 GB VPS.
    with HEAVY_LOCK:
        result = model.transcribe(
            str(video_path),
            language=WHISPER_LANGUAGE,
            word_timestamps=True,
            verbose=False,
        )

    header = _ASS_HEADER.format(
        play_res_y=OUTPUT_HEIGHT,
        font_name=SUBTITLE_FONT_NAME,
        font_size=SUBTITLE_FONT_SIZE,
        primary=SUBTITLE_PRIMARY_COLOR,
        secondary=SUBTITLE_SECONDARY_COLOR,
        outline=SUBTITLE_OUTLINE_COLOR,
        back=SUBTITLE_BACK_COLOR,
        bold=SUBTITLE_BOLD,
        outline_px=SUBTITLE_OUTLINE,
        shadow=SUBTITLE_SHADOW,
        alignment=SUBTITLE_ALIGNMENT,
        margin_v=margin_v if margin_v is not None else SUBTITLE_MARGIN_V,
    )

    ass_path.write_text(header + _build_events(result["segments"]), encoding="utf-8")
    logger.info("Subtitles → '%s'", ass_path)
    return ass_path
