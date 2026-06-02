"""
video_cutter.py — Cut a video into multiple short clips using FFmpeg.

Usage
-----
    from content_factory.core.video_cutter import cut_clips
    paths = cut_clips(
        source_path="interview.mp4",
        clips=[{"start": 12.5, "end": 48.3, "title": "О детстве"}],
        output_dir=Path("storage/library/123/top_videos"),
    )
"""
from __future__ import annotations

import logging
import math
import re
import subprocess
from pathlib import Path

from content_factory.config.settings import OUTPUT_CRF, OUTPUT_PRESET

logger = logging.getLogger(__name__)

_SAFE_RE = re.compile(r"[^\w\s\-]", re.UNICODE)


def _safe_filename(text: str, max_len: int = 40) -> str:
    """Turn arbitrary text into a safe ASCII-ish filename fragment."""
    s = _SAFE_RE.sub("_", text).strip()
    s = re.sub(r"\s+", "_", s)
    return s[:max_len].rstrip("_") or "clip"


def cut_clips(
    source_path: str | Path,
    clips: list[dict],
    output_dir: str | Path,
    source_stem: str | None = None,
) -> list[Path]:
    """
    Cut *source_path* into clips defined by ``[{start, end, title}]``.

    Parameters
    ----------
    source_path : path to the source video
    clips       : list of dicts with ``start`` (s), ``end`` (s), ``title``
    output_dir  : directory where clip files are written
    source_stem : base name prefix for clip filenames (default: source stem)

    Returns
    -------
    list[Path]  paths of successfully created clips (failed clips are skipped)
    """
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = source_stem or source_path.stem

    results: list[Path] = []

    for i, clip in enumerate(clips, 1):
        start: float = clip["start"]
        duration: float = clip["end"] - clip["start"]
        title_safe = _safe_filename(clip.get("title", "clip"))
        out_path = output_dir / f"{stem}_clip{i:02d}_{title_safe}.mp4"

        # -ss before -i  → fast keyframe seek
        # -t              → duration (more reliable than -to for seeking)
        # -avoid_negative_ts 1 → fix DTS issues after seek
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(source_path),
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-crf", str(OUTPUT_CRF),
            "-preset", OUTPUT_PRESET,
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-avoid_negative_ts", "1",
            str(out_path),
        ]

        logger.info(
            "[video_cutter] Clip %d/%d | %.1fs–%.1fs (%.0fs) → %s",
            i, len(clips), start, clip["end"], duration, out_path.name,
        )
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(
                "[video_cutter] FFmpeg failed for clip %d:\n%s",
                i, result.stderr[-1500:],
            )
            continue  # skip failed clip, carry on with the rest

        results.append(out_path)

    logger.info(
        "[video_cutter] Done: %d/%d clips created in '%s'",
        len(results), len(clips), output_dir,
    )
    return results


# ─── Time-based splitter ─────────────────────────────────────────────────────

def _get_duration(path: Path) -> float:
    """Return video duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-800:]}")
    return float(result.stdout.strip())


def split_by_duration(
    source_path: str | Path,
    output_dir: str | Path,
    chunk_sec: float = 60.0,
    source_stem: str | None = None,
) -> list[Path]:
    """
    Split *source_path* into equal chunks of *chunk_sec* seconds.

    Parameters
    ----------
    source_path : path to the source video
    output_dir  : directory where chunk files are written
    chunk_sec   : target duration of each chunk in seconds
    source_stem : base name prefix for filenames (default: source stem)

    Returns
    -------
    list[Path]  paths of successfully created chunks (failed chunks skipped)
    """
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = source_stem or source_path.stem

    total = _get_duration(source_path)
    n_chunks = math.ceil(total / chunk_sec)

    logger.info(
        "[video_cutter] Splitting %.0fs video into %d × %.0fs chunks",
        total, n_chunks, chunk_sec,
    )

    results: list[Path] = []
    for i in range(n_chunks):
        start = i * chunk_sec
        duration = min(chunk_sec, total - start)
        if duration < 1.0:
            break  # skip tiny tail

        out_path = output_dir / f"{stem}_part{i + 1:02d}.mp4"

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(source_path),
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-crf", str(OUTPUT_CRF),
            "-preset", OUTPUT_PRESET,
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-avoid_negative_ts", "1",
            str(out_path),
        ]

        logger.info(
            "[video_cutter] Part %d/%d | %.0fs–%.0fs → %s",
            i + 1, n_chunks, start, start + duration, out_path.name,
        )
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(
                "[video_cutter] FFmpeg failed for part %d:\n%s",
                i + 1, result.stderr[-1500:],
            )
            continue

        results.append(out_path)

    logger.info(
        "[video_cutter] Done: %d/%d parts created in '%s'",
        len(results), n_chunks, output_dir,
    )
    return results
