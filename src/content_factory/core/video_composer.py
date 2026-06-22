"""video_composer.py — FFmpeg split-screen + subtitles + banner overlay."""
from __future__ import annotations
import logging, subprocess
from pathlib import Path

from content_factory.config.settings import (
    BANNER_ANIMATION, BANNER_APPEAR_AT_SEC, BANNER_DURATION_SEC,
    BANNER_FADE_SEC, BANNER_LOOP_INTERVAL,
    BANNER_MARGIN_LEFT, BANNER_MARGIN_TOP,
    FFMPEG_TIMEOUT,
    HALF_HEIGHT, OUTPUT_CRF, OUTPUT_FPS, OUTPUT_HEIGHT, OUTPUT_PRESET, OUTPUT_WIDTH,
    SINGLE_BANNER_MARGIN_TOP, SINGLE_SUBTITLE_MARGIN_V,
    SPLIT_EQ_ENABLED, VIDEO_EQ_BRIGHTNESS, VIDEO_EQ_CONTRAST, VIDEO_EQ_SATURATION,
)
from content_factory.core._sync import HEAVY_LOCK

logger = logging.getLogger(__name__)

_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _slot_filter(index: int, fit_mode: str) -> str:
    """
    Build the FFmpeg filter chain for one video slot (top or bottom).

    fit_mode="crop" — scale up + center-crop to fill the slot (default).
    fit_mode="pad"  — scale down + black bars to fit the slot.
    """
    w, h = OUTPUT_WIDTH, HALF_HEIGHT
    label = "top" if index == 0 else "bot"
    if fit_mode == "pad":
        return (
            f"[{index}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black[{label}]"
        )
    return (
        f"[{index}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}[{label}]"
    )


def _fc_nobanner(subtitle_file, fit_mode):
    """Split-screen filter with subtitles but no banner overlay."""
    ap = str(subtitle_file).replace("\\", "/").replace(":", "\\:")
    return (
        _slot_filter(0, fit_mode) + ";"
        + _slot_filter(1, fit_mode) + ";"
        + "[top][bot]vstack=inputs=2[stacked];"
        + f"[stacked]subtitles='{ap}'[out]"
    )


def _fc(subtitle_file, banner_appear_at, banner_duration, banner_fade,
        banner_margin_top, banner_margin_left, banner_is_video, fit_mode,
        banner_animation, banner_loop_interval):
    bw  = OUTPUT_WIDTH - banner_margin_left * 2
    ap  = str(subtitle_file).replace("\\", "/").replace(":", "\\:")

    # ── shared: two video slots + vstack + subtitles ─────────────────────────
    base = (
        _slot_filter(0, fit_mode) + ";"
        + _slot_filter(1, fit_mode) + ";"
        + "[top][bot]vstack=inputs=2[stacked];"
        + f"[stacked]subtitles='{ap}'[subbed];"
    )

    # ── SLIDE mode (slide_left / slide_right) — banner loops forever ─────────
    if banner_animation in ("slide_left", "slide_right"):
        period = banner_loop_interval          # full cycle: in + stay + out
        fade   = banner_fade
        start  = banner_appear_at
        ml     = banner_margin_left

        # relative time inside one period
        trel = f"mod(t-{start},{period})"

        if banner_animation == "slide_left":
            # Enters from LEFT (-overlay_w), parks at ml, exits to RIGHT (main_w)
            x_expr = (
                f"if(lt(t,{start}),-overlay_w,"
                f"if(lt({trel},{fade}),-overlay_w+({ml}+overlay_w)*{trel}/{fade},"
                f"if(lt({trel},{period}-{fade}),{ml},"
                f"{ml}+(main_w-{ml})*({trel}-({period}-{fade}))/{fade})))"
            )
        else:
            # Enters from RIGHT (main_w), parks at ml, exits to LEFT (-overlay_w)
            x_expr = (
                f"if(lt(t,{start}),main_w,"
                f"if(lt({trel},{fade}),main_w-(main_w-{ml})*{trel}/{fade},"
                f"if(lt({trel},{period}-{fade}),{ml},"
                f"{ml}-({ml}+overlay_w)*({trel}-({period}-{fade}))/{fade})))"
            )

        if banner_is_video:
            # Video banner: looped via -stream_loop -1 on the input (set in compose())
            bp = (
                f"[2:v]scale={bw}:-2,"
                f"fps={OUTPUT_FPS},"
                f"format=rgba[bscaled];"
            )
        else:
            # Image banner: loop single frame indefinitely via filter
            bp = (
                f"[2:v]"
                f"loop=loop=-1:size=1:start=0,"
                f"fps={OUTPUT_FPS},"
                f"scale={bw}:-2,"
                f"format=rgba[bscaled];"
            )

        return (
            base + bp
            + f"[subbed][bscaled]overlay="
              f"x='{x_expr}':y={banner_margin_top}"
              f":enable='gte(t,{start})':shortest=1[out]"
        )

    # ── FADE mode (original) ──────────────────────────────────────────────────
    fade_out_start = banner_appear_at + banner_duration - banner_fade
    banner_end     = banner_appear_at + banner_duration

    if banner_is_video:
        bp = (
            f"[2:v]scale={bw}:-2,"
            f"trim=duration={banner_duration},"
            f"setpts=PTS+{banner_appear_at}/TB,"
            f"format=rgba[bscaled];"
        )
    else:
        bp = (
            f"[2:v]"
            f"loop=loop=-1:size=1:start=0,"
            f"fps={OUTPUT_FPS},"
            f"trim=duration={banner_end},"
            f"scale={bw}:-2,"
            f"format=rgba[bscaled];"
        )

    return (
        base + bp
        + f"[bscaled]"
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
    banner_is_video=None,
    fit_mode: str = "crop",
    banner_animation: str = BANNER_ANIMATION,
    banner_loop_interval: float = BANNER_LOOP_INTERVAL,
):
    """
    Compose two videos into a 1080×1920 split-screen short with subtitles and banner.

    banner_animation    : "slide_left" | "slide_right" | "fade"
    banner_loop_interval: seconds per slide cycle (only in slide modes)
    fit_mode            : "crop" (default) | "pad"
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for label, p in [("top_video", top_video), ("bottom_video", bottom_video),
                     ("subtitle_file", subtitle_file), ("banner_image", banner_image)]:
        if p and not Path(p).exists():
            raise FileNotFoundError(f"{label} not found on disk: {p}")

    has_banner = banner_image is not None

    if has_banner:
        if banner_is_video is None:
            banner_is_video = Path(banner_image).suffix.lower() in _VIDEO_EXTENSIONS
        fc = _fc(
            subtitle_file, banner_appear_at, banner_duration, banner_fade,
            banner_margin_top, banner_margin_left, banner_is_video, fit_mode,
            banner_animation, banner_loop_interval,
        )
        # For slide-mode video banners: loop at demuxer level (fast, no frame buffering)
        banner_input_flags = []
        if banner_is_video and banner_animation in ("slide_left", "slide_right"):
            banner_input_flags = ["-stream_loop", "-1"]
        banner_inputs = [*banner_input_flags, "-i", str(banner_image)]
    else:
        fc = _fc_nobanner(subtitle_file, fit_mode)
        banner_inputs = []

    cmd = [
        "ffmpeg", "-y",
        "-i", str(top_video),
        "-i", str(bottom_video),
        *banner_inputs,
        "-filter_complex", fc,
        "-map", "[out]",
        "-map", "0:a",
        "-c:v", "libx264", "-crf", str(OUTPUT_CRF), "-preset", OUTPUT_PRESET,
        "-r", str(OUTPUT_FPS), "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    logger.info("Running FFmpeg… (animation=%s fit_mode=%s)", banner_animation, fit_mode)
    print(f"[FFMPEG] filter_complex:\n{fc}\n", flush=True)
    with HEAVY_LOCK:  # serialize heavy encode process-wide (2 GB / 2-core VPS)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)

    if result.returncode != 0:
        print(f"[FFMPEG] stderr:\n{result.stderr[-3000:]}", flush=True)
        raise RuntimeError(f"FFmpeg failed ({result.returncode}):\n{result.stderr[-2000:]}")

    logger.info("Output → '%s'", output_path)
    return output_path


# ─── Single-video composer ────────────────────────────────────────────────────

def _eq_filter(brightness: float, contrast: float, saturation: float) -> str:
    """Build FFmpeg eq filter string for color correction."""
    return f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}"


def _fc_single(subtitle_file, banner_image, banner_appear_at, banner_duration,
               banner_fade, banner_margin_top, banner_margin_left,
               banner_is_video, banner_animation, banner_loop_interval,
               eq_brightness, eq_contrast, eq_saturation):
    """
    Build filter_complex for single-video 1080×1920 shorts.
    Video is scaled/cropped to full frame, subtitles overlaid, optional banner.
    """
    ap  = str(subtitle_file).replace("\\", "/").replace(":", "\\:")
    bw  = OUTPUT_WIDTH - banner_margin_left * 2
    eq  = _eq_filter(eq_brightness, eq_contrast, eq_saturation)

    # Scale + crop + color correction
    base = (
        f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
        f":force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
        f"{eq}[corrected];"
        f"[corrected]subtitles='{ap}'[subbed];"
    )

    if banner_image is None:
        # No banner — direct output
        return base.rstrip(";") + "[out]".replace("[subbed];", "[subbed][out]") \
               if False else base + "[subbed]copy[out]"

    # ── SLIDE animation ──────────────────────────────────────────────────────
    if banner_animation in ("slide_left", "slide_right"):
        period = banner_loop_interval
        fade   = banner_fade
        start  = banner_appear_at
        ml     = banner_margin_left

        trel = f"mod(t-{start},{period})"

        if banner_animation == "slide_left":
            x_expr = (
                f"if(lt(t,{start}),-overlay_w,"
                f"if(lt({trel},{fade}),-overlay_w+({ml}+overlay_w)*{trel}/{fade},"
                f"if(lt({trel},{period}-{fade}),{ml},"
                f"{ml}+(main_w-{ml})*({trel}-({period}-{fade}))/{fade})))"
            )
        else:
            x_expr = (
                f"if(lt(t,{start}),main_w,"
                f"if(lt({trel},{fade}),main_w-(main_w-{ml})*{trel}/{fade},"
                f"if(lt({trel},{period}-{fade}),{ml},"
                f"{ml}-({ml}+overlay_w)*({trel}-({period}-{fade}))/{fade})))"
            )

        if banner_is_video:
            bp = f"[1:v]scale={bw}:-2,fps={OUTPUT_FPS},format=rgba[bscaled];"
        else:
            bp = f"[1:v]loop=loop=-1:size=1:start=0,fps={OUTPUT_FPS},scale={bw}:-2,format=rgba[bscaled];"

        return (
            base + bp
            + f"[subbed][bscaled]overlay="
              f"x='{x_expr}':y={banner_margin_top}"
              f":enable='gte(t,{start})':shortest=1[out]"
        )

    # ── FADE animation ───────────────────────────────────────────────────────
    fade_out_start = banner_appear_at + banner_duration - banner_fade
    banner_end     = banner_appear_at + banner_duration

    if banner_is_video:
        bp = (
            f"[1:v]scale={bw}:-2,"
            f"trim=duration={banner_duration},"
            f"setpts=PTS+{banner_appear_at}/TB,"
            f"format=rgba[bscaled];"
        )
    else:
        bp = (
            f"[1:v]loop=loop=-1:size=1:start=0,fps={OUTPUT_FPS},"
            f"trim=duration={banner_end},"
            f"scale={bw}:-2,format=rgba[bscaled];"
        )

    return (
        base + bp
        + f"[bscaled]"
          f"fade=t=in:st={banner_appear_at}:d={banner_fade}:alpha=1,"
          f"fade=t=out:st={fade_out_start}:d={banner_fade}:alpha=1"
          f"[banim];"
          f"[subbed][banim]overlay={banner_margin_left}:{banner_margin_top}"
          f":enable='between(t,{banner_appear_at},{banner_end})'[out]"
    )


def compose_single(
    video, subtitle_file, output_path, *,
    banner_image=None,
    banner_appear_at=BANNER_APPEAR_AT_SEC,
    banner_duration=BANNER_DURATION_SEC,
    banner_fade=BANNER_FADE_SEC,
    banner_margin_top=SINGLE_BANNER_MARGIN_TOP,
    banner_margin_left=BANNER_MARGIN_LEFT,
    banner_is_video=None,
    banner_animation: str = BANNER_ANIMATION,
    banner_loop_interval: float = BANNER_LOOP_INTERVAL,
    eq_brightness: float = VIDEO_EQ_BRIGHTNESS,
    eq_contrast: float = VIDEO_EQ_CONTRAST,
    eq_saturation: float = VIDEO_EQ_SATURATION,
):
    """
    Compose a single-video 1080×1920 short with subtitles and optional banner.

    Unlike compose(), this takes ONE video (not split-screen).
    The video is scaled/cropped to fill the full 1080×1920 frame.
    Color correction (slight darkening + contrast) applied by default.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for label, p in [("video", video), ("subtitle_file", subtitle_file)]:
        if p and not Path(p).exists():
            raise FileNotFoundError(f"{label} not found on disk: {p}")
    if banner_image and not Path(banner_image).exists():
        raise FileNotFoundError(f"banner_image not found on disk: {banner_image}")

    has_banner = banner_image is not None

    if has_banner and banner_is_video is None:
        banner_is_video = Path(banner_image).suffix.lower() in _VIDEO_EXTENSIONS

    fc = _fc_single(
        subtitle_file, banner_image,
        banner_appear_at, banner_duration, banner_fade,
        banner_margin_top, banner_margin_left,
        banner_is_video or False, banner_animation, banner_loop_interval,
        eq_brightness, eq_contrast, eq_saturation,
    )

    banner_input_flags = []
    banner_inputs = []
    if has_banner:
        if banner_is_video and banner_animation in ("slide_left", "slide_right"):
            banner_input_flags = ["-stream_loop", "-1"]
        banner_inputs = [*banner_input_flags, "-i", str(banner_image)]

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        *banner_inputs,
        "-filter_complex", fc,
        "-map", "[out]",
        "-map", "0:a",
        "-c:v", "libx264", "-crf", str(OUTPUT_CRF), "-preset", OUTPUT_PRESET,
        "-r", str(OUTPUT_FPS), "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    logger.info("Running FFmpeg single… (animation=%s banner=%s)", banner_animation, has_banner)
    logger.debug("[FFMPEG] filter_complex:\n%s", fc)
    with HEAVY_LOCK:  # serialize heavy encode process-wide (2 GB / 2-core VPS)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)

    if result.returncode != 0:
        logger.error("[FFMPEG] stderr:\n%s", result.stderr[-3000:])
        raise RuntimeError(f"FFmpeg failed ({result.returncode}):\n{result.stderr[-2000:]}")

    logger.info("Output → '%s'", output_path)
    return output_path
