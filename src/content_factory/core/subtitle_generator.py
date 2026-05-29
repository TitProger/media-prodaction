"""
subtitle_generator.py
---------------------
Transcribes audio via Whisper and produces a Shorts-style .ass subtitle file:
- Large bold white text with thick black outline
- Max SUBTITLE_MAX_WORDS words per cue (punchy, readable on phone)
- Centred at the split line between top and bottom panels
"""
from __future__ import annotations

import logging
from pathlib import Path

import whisper

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
    Split Whisper segments into short word-group cues.
    Each cue has at most SUBTITLE_MAX_WORDS words, with interpolated timestamps.
    """
    lines = []
    for seg in segments:
        words = seg["text"].strip().split()
        if not words:
            continue

        seg_start = seg["start"]
        seg_end = seg["end"]
        duration = seg_end - seg_start
        total_words = len(words)

        # Chunk into groups of SUBTITLE_MAX_WORDS
        chunks = [words[i:i + SUBTITLE_MAX_WORDS] for i in range(0, total_words, SUBTITLE_MAX_WORDS)]
        n = len(chunks)

        for idx, chunk in enumerate(chunks):
            # Interpolate start/end time for each chunk
            t_start = seg_start + (duration * idx / n)
            t_end   = seg_start + (duration * (idx + 1) / n)
            text = " ".join(chunk).upper()   # ALL CAPS — Shorts style
            lines.append(f"Dialogue: 0,{_ts(t_start)},{_ts(t_end)},Default,,0,0,0,,{text}")

    return "\n".join(lines)


def generate_subtitles(video_path: str | Path, output_dir: str | Path) -> Path:
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ass_path = output_dir / f"{video_path.stem}_subtitles.ass"

    logger.info("Loading Whisper model '%s'…", WHISPER_MODEL)
    model = whisper.load_model(WHISPER_MODEL)

    logger.info("Transcribing '%s'…", video_path)
    result = model.transcribe(str(video_path), language=WHISPER_LANGUAGE, verbose=False)

    header = _ASS_HEADER.format(
        play_res_y=OUTPUT_HEIGHT,
        font_name=SUBTITLE_FONT_NAME,
        font_size=SUBTITLE_FONT_SIZE,
        primary=SUBTITLE_PRIMARY_COLOR,
        secondary=SUBTITLE_PRIMARY_COLOR,
        outline=SUBTITLE_OUTLINE_COLOR,
        back=SUBTITLE_BACK_COLOR,
        bold=SUBTITLE_BOLD,
        outline_px=SUBTITLE_OUTLINE,
        shadow=SUBTITLE_SHADOW,
        alignment=SUBTITLE_ALIGNMENT,
        margin_v=SUBTITLE_MARGIN_V,
    )

    ass_path.write_text(header + _build_events(result["segments"]), encoding="utf-8")
    logger.info("Subtitles → '%s'", ass_path)
    return ass_path
