"""
app.py
------
Gradio web interface for content-factory.

Run via:  python main.py
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path

import gradio as gr

from content_factory.config.settings import (
    BANNER_APPEAR_AT_SEC,
    BANNER_DURATION_SEC,
    BANNER_FADE_SEC,
    BANNER_MARGIN_LEFT,
    BANNER_MARGIN_TOP,
    OUTPUT_DIR,
    WHISPER_MODEL,
)
from content_factory.core.subtitle_generator import generate_subtitles
from content_factory.core.video_composer import compose

logger = logging.getLogger(__name__)


def _run_pipeline(
    top_video_path: str,
    bottom_video_path: str,
    banner_path: str,
    whisper_model: str,
    banner_appear_at: float,
    banner_duration: float,
    banner_fade: float,
    banner_margin_top: int,
    banner_margin_left: int,
) -> tuple[str, str]:
    """
    Full pipeline: transcribe → compose → return output path.
    Returns (output_video_path, status_message).
    """
    try:
        job_id = uuid.uuid4().hex[:8]
        work_dir = OUTPUT_DIR / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        # --- 1. Generate subtitles from top video ---
        yield None, "⏳ Транскрибирую аудио (Whisper)…"

        # Swap model if user changed it in UI
        import content_factory.config.settings as _cfg
        _cfg.WHISPER_MODEL = whisper_model

        ass_file = generate_subtitles(top_video_path, work_dir)

        yield None, "⏳ Рендерю видео (FFmpeg)…"

        # --- 2. Compose final video ---
        output_path = work_dir / "output.mp4"
        compose(
            top_video=top_video_path,
            bottom_video=bottom_video_path,
            banner_image=banner_path,
            subtitle_file=ass_file,
            output_path=output_path,
            banner_appear_at=banner_appear_at,
            banner_duration=banner_duration,
            banner_fade=banner_fade,
            banner_margin_top=int(banner_margin_top),
            banner_margin_left=int(banner_margin_left),
        )

        yield str(output_path), f"✅ Готово! Файл: {output_path}"

    except Exception as exc:
        logger.exception("Pipeline failed")
        yield None, f"❌ Ошибка: {exc}"


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="Content Factory 🎬", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            """
            # 🎬 Content Factory
            Загрузи два видео и баннер — получи готовый шортс с субтитрами и рекламой.
            """
        )

        with gr.Row():
            with gr.Column():
                gr.Markdown("### 📥 Входные файлы")
                top_video = gr.Video(label="Верхнее видео (основное + аудио для субтитров)")
                bottom_video = gr.Video(label="Нижнее видео (геймплей / фон)")
                banner = gr.Image(
                    label="Рекламный баннер (PNG / JPG)",
                    type="filepath",
                )

            with gr.Column():
                gr.Markdown("### ⚙️ Настройки")

                whisper_model = gr.Dropdown(
                    choices=["tiny", "base", "small", "medium", "large"],
                    value=WHISPER_MODEL,
                    label="Модель Whisper (качество субтитров)",
                    info="tiny — быстро, large — точнее",
                )

                gr.Markdown("**Баннер**")
                banner_appear_at = gr.Slider(
                    0, 30, value=BANNER_APPEAR_AT_SEC, step=0.5,
                    label="Появляется на (сек)",
                )
                banner_duration = gr.Slider(
                    1, 30, value=BANNER_DURATION_SEC, step=0.5,
                    label="Длительность показа (сек)",
                )
                banner_fade = gr.Slider(
                    0.1, 2.0, value=BANNER_FADE_SEC, step=0.1,
                    label="Fade-in / fade-out (сек)",
                )
                banner_margin_top = gr.Slider(
                    0, 500, value=BANNER_MARGIN_TOP, step=5,
                    label="Отступ сверху (px)",
                )
                banner_margin_left = gr.Slider(
                    0, 200, value=BANNER_MARGIN_LEFT, step=5,
                    label="Отступ слева (px)",
                )

        run_btn = gr.Button("🚀 Создать шортс", variant="primary", size="lg")
        status = gr.Textbox(label="Статус", interactive=False)
        output_video = gr.Video(label="Результат", interactive=False)

        run_btn.click(
            fn=_run_pipeline,
            inputs=[
                top_video,
                bottom_video,
                banner,
                whisper_model,
                banner_appear_at,
                banner_duration,
                banner_fade,
                banner_margin_top,
                banner_margin_left,
            ],
            outputs=[output_video, status],
        )

    return demo
