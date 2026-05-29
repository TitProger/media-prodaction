"""video_composer.py — FFmpeg split-screen + subtitles + banner overlay."""
from __future__ import annotations
import logging, subprocess
from pathlib import Path

from content_factory.config.settings import (
    BANNER_APPEAR_AT_SEC, BANNER_DURATION_SEC, BANNER_FADE_SEC,
    BANNER_MARGIN_LEFT, BANNER_MARGIN_TOP,
    HALF_HEIGHT, OUTPUT_CRF, OUTPUT_FPS, OUTPUT_PRESET, OUTPUT_WIDTH,
)

logger = logging.getLogger(__name__)


def _fc(subtitle_file, banner_appear_at, banner_duration, banner_fade,
        banner_margin_top, banner_margin_left, banner_is_video):
    fade_out_start = banner_appear_at + banner_duration - banner_fade
    banner_end = banner_appear_at + banner_duration
    bw = OUTPUT_WIDTH - banner_margin_left * 2

    # Escape Windows path for ASS filter
    ap = str(subtitle_file).replace("\\", "/").replace(":", "\\:")

    if banner_is_video:
        # Video banner: trim to duration, offset PTS to appear at the right time
        bp = (
            f"[2:v]scale={bw}:-2,"
            f"trim=duration={banner_duration},"
            f"setpts=PTS+{banner_appear_at}/TB,"
            f"format=rgba[bscaled];"
        )
    else:
        # Static image: loop single frame at output fps, then limit to banner_end seconds
        bp = (
            f"[2:v]"
            f"loop=loop=-1:size=1:start=0,"   # loop the one frame forever
            f"fps={OUTPUT_FPS},"               # at output framerate (no crazy fps)
            f"trim=duration={banner_end},"     # cut at banner end time
            f"scale={bw}:-2,"
            f"format=rgba[bscaled];"
        )

    return (
        f"[0:v]scale={OUTPUT_WIDTH}:{HALF_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{HALF_HEIGHT}[top];"
        f"[1:v]scale={OUTPUT_WIDTH}:{HALF_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{HALF_HEIGHT}[bot];"
        "[top][bot]vstack=inputs=2[stacked];"
        f"[stacked]subtitles='{ap}'[subbed];"
        + bp +
        f"[bscaled]"
        f"fade=t=in:st={banner_appear_at}:d={banner_fade}:alpha=1,"
        f"fade=t=out:st={fade_out_start}:d={banner_fade}:alpha=1"
        f"[banim];"
        f"[subbed][banim]overlay={banner_margin_left}:{banner_margin_top}"
        f":enable='between(t,{banner_appear_at},{banner_end})'[out]"
    )


def compose(
    top_video, bottom_video, banner_image, subtitle_file, output_path, *,
    banner_appear_at=BANNER_APPEAR_AT_SEC,
    banner_duration=BANNER_DURATION_SEC,
    banner_fade=BANNER_FADE_SEC,
    banner_margin_top=BANNER_MARGIN_TOP,
    banner_margin_left=BANNER_MARGIN_LEFT,
    banner_is_video=False,
):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fc = _fc(subtitle_file, banner_appear_at, banner_duration, banner_fade,
             banner_margin_top, banner_margin_left, banner_is_video)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(top_video),
        "-i", str(bottom_video),
        "-i", str(banner_image),   # plain -i, no -loop (looping handled in filter)
        "-filter_complex", fc,
        "-map", "[out]",
        "-map", "0:a",
        "-c:v", "libx264", "-crf", str(OUTPUT_CRF), "-preset", OUTPUT_PRESET,
        "-r", str(OUTPUT_FPS), "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    logger.info("Running FFmpeg…")
    print(f"[FFMPEG] filter_complex:\n{fc}\n", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[FFMPEG] stderr:\n{result.stderr[-3000:]}", flush=True)
        raise RuntimeError(f"FFmpeg failed ({result.returncode}):\n{result.stderr[-2000:]}")

    logger.info("Output → '%s'", output_path)
    return output_path
