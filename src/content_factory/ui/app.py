"""
app.py
------
Gradio web interface for content-factory.

Run via:  python main.py
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path

import gradio as gr

from content_factory.config.settings import (
    WEB_USER_ID,
    BANNER_APPEAR_AT_SEC,
    BANNER_DURATION_SEC,
    BANNER_FADE_SEC,
    BANNER_MARGIN_LEFT,
    BANNER_MARGIN_TOP,
    OUTPUT_DIR,
    SINGLE_BANNER_MARGIN_TOP,
    SINGLE_SUBTITLE_MARGIN_V,
    VIDEO_EQ_BRIGHTNESS,
    VIDEO_EQ_CONTRAST,
    VIDEO_EQ_SATURATION,
    WHISPER_MODEL,
)
from content_factory.core.subtitle_generator import generate_subtitles
from content_factory.core.video_composer import compose, compose_single
from content_factory.db.library import (
    add_file, count_unused_clips, delete_source_cascade,
    get_file, get_storage_path, init_db, list_clips, list_sources,
    mark_used,
)

logger = logging.getLogger(__name__)

_OUTPUT_TTL_HOURS = 24
_PAGE_SIZE = 4  # videos per page in gallery

_LIB_CATEGORIES = {
    "📱 Блог видео (один клип)":    "blog_video",
    "🎬 Верхние видео (сплит)":     "top_video",
    "🎮 Нижние видео (сплит)":      "bottom_video",
    "🖼 Фото-баннеры":              "banner_image",
    "📹 Видео-баннеры":             "banner_video",
}


def _upload_to_library(file_obj, name: str, category_label: str) -> str:
    """Upload a file to the library. Returns status message."""
    if file_obj is None:
        return "❌ Выбери файл"
    name = (name or "").strip()
    if not name:
        return "❌ Введи название"
    category = _LIB_CATEGORIES.get(category_label)
    if not category:
        return "❌ Неверная категория"
    init_db()
    src = Path(file_obj)
    storage_dir = get_storage_path(WEB_USER_ID, category, subtype="source")
    dest = storage_dir / f"{uuid.uuid4().hex}{src.suffix}"
    import shutil as _sh
    _sh.copy2(src, dest)
    add_file(WEB_USER_ID, name[:120], category, dest, subtype="source")
    return f"✅ Загружено: «{name}» → {category_label}"


def _list_sources_text(category_label: str) -> str:
    """Return formatted list of sources for a category."""
    category = _LIB_CATEGORIES.get(category_label)
    if not category:
        return ""
    rows = list_sources(WEB_USER_ID, category)
    if not rows:
        return "📭 Пусто — загрузи первое видео"
    lines = []
    for r in rows:
        clips = r["clip_count"]
        unused = count_unused_clips(WEB_USER_ID, r["id"]) if category in ("top_video", "blog_video") else clips
        lines.append(f"**[{r['id']}]** {r['name']}  —  клипов: {clips}  (свободных: {unused})")
    return "\n".join(lines)


def _cut_source(source_id_str: str, category_label: str, use_ai: bool) -> str:
    """Trigger clip cutting for a source. Returns status."""
    try:
        source_id = int(source_id_str.strip())
    except (ValueError, AttributeError):
        return "❌ Введи числовой ID источника"
    category = _LIB_CATEGORIES.get(category_label)
    if not category:
        return "❌ Неверная категория"

    from content_factory.db.library import get_file
    row = get_file(source_id, WEB_USER_ID)
    if row is None:
        return f"❌ Источник #{source_id} не найден"

    source_path = Path(row["file_path"])
    source_stem = source_path.stem
    clips_dir = get_storage_path(WEB_USER_ID, category, subtype="clip", source_stem=source_stem)

    try:
        if category == "bottom_video":
            from content_factory.core.video_cutter import split_by_duration
            from content_factory.config.settings import BOTTOM_CLIP_DURATION
            saved = split_by_duration(source_path, clips_dir,
                                      chunk_sec=BOTTOM_CLIP_DURATION, source_stem=source_stem)
            for i, p in enumerate(saved, 1):
                add_file(WEB_USER_ID, f"Часть {i}", category, p,
                         subtype="clip", parent_id=source_id)
        else:
            if use_ai:
                from content_factory.core.clip_finder import find_best_clips
                clips_meta = find_best_clips(source_path, category=category)
            else:
                from content_factory.core.clip_finder import _find_clips_heuristic
                from content_factory.config.settings import (
                    WHISPER_MODEL, WHISPER_LANGUAGE, CLIP_COUNT,
                    BLOG_CLIP_MIN_DURATION, BLOG_CLIP_MAX_DURATION,
                    CLIP_MIN_DURATION, CLIP_MAX_DURATION,
                )
                import whisper as _whisper
                m = _whisper.load_model(WHISPER_MODEL)
                result = m.transcribe(str(source_path), language=WHISPER_LANGUAGE,
                                      word_timestamps=False, verbose=False)
                min_d = BLOG_CLIP_MIN_DURATION if category == "blog_video" else CLIP_MIN_DURATION
                max_d = BLOG_CLIP_MAX_DURATION if category == "blog_video" else CLIP_MAX_DURATION
                clips_meta = _find_clips_heuristic(result["segments"], CLIP_COUNT, min_d, max_d)

            from content_factory.core.video_cutter import cut_clips
            saved = cut_clips(source_path, clips_meta, clips_dir, source_stem=source_stem)
            for i, p in enumerate(saved):
                title = clips_meta[i]["title"] if i < len(clips_meta) else p.stem
                add_file(WEB_USER_ID, title, category, p,
                         subtype="clip", parent_id=source_id)

        return f"✅ Нарезано {len(saved)} клипов из «{row['name']}»"
    except Exception as exc:
        return f"❌ Ошибка: {exc}"


def _delete_source(source_id_str: str) -> str:
    try:
        source_id = int(source_id_str.strip())
    except (ValueError, AttributeError):
        return "❌ Введи числовой ID"
    ok = delete_source_cascade(source_id, WEB_USER_ID)
    return f"✅ Удалён #{source_id}" if ok else f"❌ #{source_id} не найден"

# ─── Gallery helpers ──────────────────────────────────────────────────────────

_GALLERY_CATEGORIES = {
    "🎬 Верхние клипы (сплит)": "top_video",
    "🎮 Нижние клипы (сплит)": "bottom_video",
    "📱 Блог клипы": "blog_video",
}


def _get_library_clips(category_label: str) -> list[dict]:
    """Return all clips for a category from the DB."""
    category = _GALLERY_CATEGORIES.get(category_label)
    if not category:
        return []
    clips = []
    for src in list_sources(WEB_USER_ID, category):
        for clip in list_clips(WEB_USER_ID, src["id"]):
            clips.append({
                "path": clip["file_path"],
                "name": clip["name"],
                "used": bool(clip["used"]),
            })
    return clips


def _get_output_videos() -> list[dict]:
    """Return all generated output.mp4 files from output/ sorted by newest first."""
    if not OUTPUT_DIR.exists():
        return []
    videos = []
    for job_dir in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not job_dir.is_dir():
            continue
        out = job_dir / "output.mp4"
        if out.exists():
            videos.append({
                "path": str(out),
                "name": job_dir.name,
                "size": out.stat().st_size,
            })
    return videos


def _page_slice(items: list, page: int) -> tuple[list, int, int]:
    """Return (page_items, total_pages, current_page)."""
    total = len(items)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    return items[start:start + _PAGE_SIZE], total_pages, page


def _load_library_page(category_label: str, page: int):
    clips = _get_library_clips(category_label)
    page_clips, total_pages, page = _page_slice(clips, page)

    paths = [c["path"] for c in page_clips]
    labels = [
        f"{'✅' if not c['used'] else '☑️'} {c['name']}"
        for c in page_clips
    ]
    # Pad to _PAGE_SIZE so Gradio components don't shift
    while len(paths) < _PAGE_SIZE:
        paths.append(None)
        labels.append("")

    status = f"Страница {page + 1} / {total_pages}  •  Всего клипов: {len(clips)}"
    return (*paths, *labels, page, status)


def _load_output_page(page: int):
    videos = _get_output_videos()
    page_videos, total_pages, page = _page_slice(videos, page)

    paths = [v["path"] for v in page_videos]
    labels = [
        f"{v['name']}  ({v['size'] / 1024 / 1024:.1f} МБ)"
        for v in page_videos
    ]
    while len(paths) < _PAGE_SIZE:
        paths.append(None)
        labels.append("")

    status = f"Страница {page + 1} / {total_pages}  •  Всего шортсов: {len(videos)}"
    return (*paths, *labels, page, status)


def _get_clip_choices(category: str) -> list[tuple[str, str]]:
    """Return (label, file_path) choices for all clips in a category."""
    init_db()
    choices = []
    for src in list_sources(WEB_USER_ID, category):
        for clip in list_clips(WEB_USER_ID, src["id"]):
            used_mark = "☑️" if clip["used"] else "✅"
            label = f"{used_mark} [{clip['id']}] {src['name']} / {clip['name']}"
            choices.append((label, str(clip["file_path"])))
    return choices


def _get_banner_choices() -> list[tuple[str, str]]:
    """Return (label, file_path) choices for all banners."""
    init_db()
    choices = [("— без баннера —", "")]
    for cat in ("banner_image", "banner_video"):
        for row in list_sources(WEB_USER_ID, cat):
            icon = "🖼" if cat == "banner_image" else "📹"
            label = f"{icon} [{row['id']}] {row['name']}"
            choices.append((label, str(row["file_path"])))
    return choices


def _refresh_lib_dropdowns():
    top_choices = _get_clip_choices("top_video")
    bottom_choices = _get_clip_choices("bottom_video")
    blog_choices = _get_clip_choices("blog_video")
    banner_choices = _get_banner_choices()
    top_val = top_choices[0][1] if top_choices else None
    bottom_val = bottom_choices[0][1] if bottom_choices else None
    blog_val = blog_choices[0][1] if blog_choices else None
    return (
        gr.update(choices=top_choices, value=top_val),
        gr.update(choices=bottom_choices, value=bottom_val),
        gr.update(choices=blog_choices, value=blog_val),
        gr.update(choices=banner_choices, value=""),
        gr.update(choices=banner_choices, value=""),
    )


def _run_lib_blog_pipeline(
    video_path: str,
    banner_path: str,
    whisper_model: str,
    banner_appear_at: float,
    banner_fade: float,
    banner_margin_top: int,
    banner_margin_left: int,
    banner_animation: str,
    eq_brightness: float,
    eq_contrast: float,
    eq_saturation: float,
    subtitle_margin_v: int,
):
    if not video_path:
        yield None, "❌ Выбери блог-клип"
        return
    try:
        job_id = uuid.uuid4().hex[:8]
        work_dir = OUTPUT_DIR / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        yield None, "⏳ Транскрибирую аудио (Whisper)…"
        ass_file = generate_subtitles(
            video_path, work_dir,
            model_name=whisper_model,
            margin_v=int(subtitle_margin_v),
        )

        yield None, "⏳ Рендерю видео (FFmpeg)…"
        output_path = work_dir / "output.mp4"
        compose_single(
            video=video_path,
            subtitle_file=ass_file,
            output_path=output_path,
            banner_image=banner_path if banner_path else None,
            banner_appear_at=banner_appear_at,
            banner_fade=banner_fade,
            banner_margin_top=int(banner_margin_top),
            banner_margin_left=int(banner_margin_left),
            banner_animation=banner_animation,
            eq_brightness=eq_brightness,
            eq_contrast=eq_contrast,
            eq_saturation=eq_saturation,
        )

        yield str(output_path), f"✅ Готово! Файл: {output_path}"

    except Exception as exc:
        logger.exception("Lib blog pipeline failed")
        yield None, f"❌ Ошибка: {exc}"


def _run_lib_split_pipeline(
    top_path: str,
    bottom_path: str,
    banner_path: str,
    whisper_model: str,
    fit_mode: str,
    banner_appear_at: float,
    banner_duration: float,
    banner_fade: float,
    banner_margin_top: int,
    banner_margin_left: int,
):
    if not top_path:
        yield None, "❌ Выбери верхний клип"
        return
    if not bottom_path:
        yield None, "❌ Выбери нижний клип"
        return
    try:
        job_id = uuid.uuid4().hex[:8]
        work_dir = OUTPUT_DIR / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        yield None, "⏳ Транскрибирую аудио (Whisper)…"
        ass_file = generate_subtitles(top_path, work_dir, model_name=whisper_model)

        yield None, "⏳ Рендерю видео (FFmpeg)…"
        output_path = work_dir / "output.mp4"
        compose(
            top_video=top_path,
            bottom_video=bottom_path,
            banner_image=banner_path if banner_path else None,
            subtitle_file=ass_file,
            output_path=output_path,
            fit_mode=fit_mode,
            banner_appear_at=banner_appear_at,
            banner_duration=banner_duration,
            banner_fade=banner_fade,
            banner_margin_top=int(banner_margin_top),
            banner_margin_left=int(banner_margin_left),
        )

        yield str(output_path), f"✅ Готово! Файл: {output_path}"

    except Exception as exc:
        logger.exception("Lib split pipeline failed")
        yield None, f"❌ Ошибка: {exc}"


def _cleanup_old_jobs() -> str:
    """Delete job folders older than _OUTPUT_TTL_HOURS. Returns a status string."""
    if not OUTPUT_DIR.exists():
        return "Папка output/ не существует."
    cutoff = time.time() - _OUTPUT_TTL_HOURS * 3600
    removed, kept = 0, 0
    for job_dir in OUTPUT_DIR.iterdir():
        if job_dir.is_dir() and job_dir.stat().st_mtime < cutoff:
            shutil.rmtree(job_dir, ignore_errors=True)
            removed += 1
        else:
            kept += 1
    return f"Удалено: {removed} папок старше {_OUTPUT_TTL_HOURS}ч. Осталось: {kept}."


# ─── Split-screen pipeline ────────────────────────────────────────────────────

def _run_split_pipeline(
    top_video_path: str,
    bottom_video_path: str,
    banner_path: str,
    whisper_model: str,
    fit_mode: str,
    banner_appear_at: float,
    banner_duration: float,
    banner_fade: float,
    banner_margin_top: int,
    banner_margin_left: int,
):
    try:
        job_id = uuid.uuid4().hex[:8]
        work_dir = OUTPUT_DIR / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        yield None, "⏳ Транскрибирую аудио (Whisper)…"
        ass_file = generate_subtitles(top_video_path, work_dir, model_name=whisper_model)

        yield None, "⏳ Рендерю видео (FFmpeg)…"
        output_path = work_dir / "output.mp4"
        compose(
            top_video=top_video_path,
            bottom_video=bottom_video_path,
            banner_image=banner_path,
            subtitle_file=ass_file,
            output_path=output_path,
            fit_mode=fit_mode,
            banner_appear_at=banner_appear_at,
            banner_duration=banner_duration,
            banner_fade=banner_fade,
            banner_margin_top=int(banner_margin_top),
            banner_margin_left=int(banner_margin_left),
        )

        yield str(output_path), f"✅ Готово! Файл: {output_path}"

    except Exception as exc:
        logger.exception("Split pipeline failed")
        yield None, f"❌ Ошибка: {exc}"


# ─── Single-video pipeline ────────────────────────────────────────────────────

def _run_single_pipeline(
    video_path: str,
    banner_path: str | None,
    whisper_model: str,
    banner_appear_at: float,
    banner_fade: float,
    banner_margin_top: int,
    banner_margin_left: int,
    banner_animation: str,
    eq_brightness: float,
    eq_contrast: float,
    eq_saturation: float,
    subtitle_margin_v: int,
):
    try:
        job_id = uuid.uuid4().hex[:8]
        work_dir = OUTPUT_DIR / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        yield None, "⏳ Транскрибирую аудио (Whisper)…"
        ass_file = generate_subtitles(
            video_path, work_dir,
            model_name=whisper_model,
            margin_v=int(subtitle_margin_v),
        )

        yield None, "⏳ Рендерю видео (FFmpeg)…"
        output_path = work_dir / "output.mp4"
        compose_single(
            video=video_path,
            subtitle_file=ass_file,
            output_path=output_path,
            banner_image=banner_path if banner_path else None,
            banner_appear_at=banner_appear_at,
            banner_fade=banner_fade,
            banner_margin_top=int(banner_margin_top),
            banner_margin_left=int(banner_margin_left),
            banner_animation=banner_animation,
            eq_brightness=eq_brightness,
            eq_contrast=eq_contrast,
            eq_saturation=eq_saturation,
        )

        yield str(output_path), f"✅ Готово! Файл: {output_path}"

    except Exception as exc:
        logger.exception("Single pipeline failed")
        yield None, f"❌ Ошибка: {exc}"


# ─── UI ───────────────────────────────────────────────────────────────────────

def build_ui() -> gr.Blocks:
    with gr.Blocks(title="Content Factory 🎬", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# 🎬 Content Factory")

        with gr.Tabs():

            # ── Tab 1: Split-screen ───────────────────────────────────────────
            with gr.Tab("🎬 Сплит-экран шортс"):
                gr.Markdown("Загрузи два видео и баннер — получи готовый шортс с субтитрами.")

                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### 📥 Входные файлы")
                        top_video = gr.Video(label="Верхнее видео (основное + аудио для субтитров)")
                        bottom_video = gr.Video(label="Нижнее видео (геймплей / фон)")
                        banner_split = gr.Image(
                            label="Рекламный баннер (PNG / JPG)",
                            type="filepath",
                        )

                    with gr.Column():
                        gr.Markdown("### ⚙️ Настройки")
                        whisper_split = gr.Dropdown(
                            choices=["tiny", "base", "small", "medium", "large"],
                            value=WHISPER_MODEL,
                            label="Модель Whisper",
                            info="tiny — быстро, large — точнее",
                        )
                        fit_mode = gr.Radio(
                            choices=[("Обрезка (crop)", "crop"), ("Чёрные полосы (pad)", "pad")],
                            value="crop",
                            label="Режим вписывания видео",
                        )
                        gr.Markdown("**Баннер**")
                        banner_appear_split = gr.Slider(0, 30, value=BANNER_APPEAR_AT_SEC, step=0.5, label="Появляется на (сек)")
                        banner_duration_split = gr.Slider(1, 30, value=min(BANNER_DURATION_SEC, 30), step=0.5, label="Длительность (сек)")
                        banner_fade_split = gr.Slider(0.1, 2.0, value=BANNER_FADE_SEC, step=0.1, label="Fade (сек)")
                        banner_mt_split = gr.Slider(0, 1900, value=BANNER_MARGIN_TOP, step=5, label="Отступ сверху (px)")
                        banner_ml_split = gr.Slider(0, 200, value=BANNER_MARGIN_LEFT, step=5, label="Отступ слева (px)")

                with gr.Row():
                    run_split_btn = gr.Button("🚀 Создать сплит-шортс", variant="primary", size="lg")
                    clean_btn = gr.Button(f"🗑 Очистить output/ (>{_OUTPUT_TTL_HOURS}ч)", size="lg")
                status_split = gr.Textbox(label="Статус", interactive=False)
                output_split = gr.Video(label="Результат", interactive=False)

                clean_btn.click(fn=_cleanup_old_jobs, inputs=[], outputs=[status_split])
                run_split_btn.click(
                    fn=_run_split_pipeline,
                    inputs=[
                        top_video, bottom_video, banner_split,
                        whisper_split, fit_mode,
                        banner_appear_split, banner_duration_split,
                        banner_fade_split, banner_mt_split, banner_ml_split,
                    ],
                    outputs=[output_split, status_split],
                )

            # ── Tab 2: Single-video (blog) ────────────────────────────────────
            with gr.Tab("📱 Один клип (блог)"):
                gr.Markdown("Загрузи одно видео — получи вертикальный шортс с субтитрами и опциональным баннером.")

                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### 📥 Входные файлы")
                        single_video = gr.Video(label="Видео (MP4, MOV…)")
                        banner_single = gr.Image(
                            label="Рекламный баннер (PNG / JPG) — необязательно",
                            type="filepath",
                        )

                    with gr.Column():
                        gr.Markdown("### ⚙️ Настройки")
                        whisper_single = gr.Dropdown(
                            choices=["tiny", "base", "small", "medium", "large"],
                            value=WHISPER_MODEL,
                            label="Модель Whisper",
                        )
                        banner_anim = gr.Radio(
                            choices=[
                                ("◀️ Слева", "slide_left"),
                                ("▶️ Справа", "slide_right"),
                                ("✨ Фейд", "fade"),
                            ],
                            value="slide_left",
                            label="Анимация баннера",
                        )
                        gr.Markdown("**Баннер**")
                        banner_appear_single = gr.Slider(0, 30, value=BANNER_APPEAR_AT_SEC, step=0.5, label="Появляется на (сек)")
                        banner_fade_single = gr.Slider(0.1, 2.0, value=BANNER_FADE_SEC, step=0.1, label="Fade (сек)")
                        banner_mt_single = gr.Slider(0, 1900, value=SINGLE_BANNER_MARGIN_TOP, step=5, label="Отступ баннера сверху (px)")
                        banner_ml_single = gr.Slider(0, 200, value=BANNER_MARGIN_LEFT, step=5, label="Отступ баннера слева (px)")

                        gr.Markdown("**Цветокоррекция**")
                        eq_brightness = gr.Slider(-0.5, 0.5, value=VIDEO_EQ_BRIGHTNESS, step=0.01, label="Яркость (brightness)")
                        eq_contrast = gr.Slider(0.5, 2.0, value=VIDEO_EQ_CONTRAST, step=0.01, label="Контраст (contrast)")
                        eq_saturation = gr.Slider(0.0, 3.0, value=VIDEO_EQ_SATURATION, step=0.05, label="Насыщенность (saturation)")

                        gr.Markdown("**Субтитры**")
                        subtitle_mv = gr.Slider(0, 1900, value=SINGLE_SUBTITLE_MARGIN_V, step=10, label="Позиция субтитров (MarginV, px от низа)")

                with gr.Row():
                    run_single_btn = gr.Button("🚀 Создать клип", variant="primary", size="lg")
                status_single = gr.Textbox(label="Статус", interactive=False)
                output_single = gr.Video(label="Результат", interactive=False)

                run_single_btn.click(
                    fn=_run_single_pipeline,
                    inputs=[
                        single_video, banner_single,
                        whisper_single,
                        banner_appear_single, banner_fade_single,
                        banner_mt_single, banner_ml_single,
                        banner_anim,
                        eq_brightness, eq_contrast, eq_saturation,
                        subtitle_mv,
                    ],
                    outputs=[output_single, status_single],
                )

            # ── Tab 3: Library management ─────────────────────────────────
            with gr.Tab("📚 Библиотека"):
                gr.Markdown("Загружай видео, нарезай на клипы, управляй библиотекой.")

                with gr.Row():
                    with gr.Column(scale=2):
                        gr.Markdown("### 📤 Загрузить файл")
                        lib_upload_cat = gr.Dropdown(
                            choices=list(_LIB_CATEGORIES.keys()),
                            value=list(_LIB_CATEGORIES.keys())[0],
                            label="Категория",
                        )
                        lib_upload_name = gr.Textbox(label="Название", placeholder="Например: Дзю-Дзю Ито")
                        lib_upload_file = gr.File(label="Файл (MP4, MOV, JPG, PNG…)", file_count="single")
                        lib_upload_btn = gr.Button("⬆️ Загрузить", variant="primary")
                        lib_upload_status = gr.Textbox(label="Статус", interactive=False)

                        lib_upload_btn.click(
                            fn=_upload_to_library,
                            inputs=[lib_upload_file, lib_upload_name, lib_upload_cat],
                            outputs=[lib_upload_status],
                        )

                    with gr.Column(scale=3):
                        gr.Markdown("### 📋 Источники в библиотеке")
                        lib_view_cat = gr.Dropdown(
                            choices=list(_LIB_CATEGORIES.keys()),
                            value=list(_LIB_CATEGORIES.keys())[0],
                            label="Категория",
                        )
                        lib_refresh_btn = gr.Button("🔄 Обновить список")
                        lib_sources_text = gr.Markdown("Нажми «Обновить список»")

                        lib_refresh_btn.click(
                            fn=_list_sources_text,
                            inputs=[lib_view_cat],
                            outputs=[lib_sources_text],
                        )
                        lib_view_cat.change(
                            fn=_list_sources_text,
                            inputs=[lib_view_cat],
                            outputs=[lib_sources_text],
                        )

                gr.Markdown("---")
                gr.Markdown("### ✂️ Нарезать на клипы")
                with gr.Row():
                    cut_cat = gr.Dropdown(
                        choices=list(_LIB_CATEGORIES.keys())[:3],  # only video cats
                        value=list(_LIB_CATEGORIES.keys())[0],
                        label="Категория",
                        scale=2,
                    )
                    cut_id = gr.Textbox(label="ID источника (из списка выше)", placeholder="42", scale=1)
                    cut_ai = gr.Checkbox(label="🤖 AI анализ (медленно, но лучше)", value=False)
                    cut_btn = gr.Button("✂️ Нарезать", variant="primary", scale=1)
                cut_status = gr.Textbox(label="Статус нарезки", interactive=False)

                cut_btn.click(
                    fn=_cut_source,
                    inputs=[cut_id, cut_cat, cut_ai],
                    outputs=[cut_status],
                )

                gr.Markdown("---")
                gr.Markdown("### 🗑 Удалить источник (и все его клипы)")
                with gr.Row():
                    del_id = gr.Textbox(label="ID источника", placeholder="42", scale=2)
                    del_btn = gr.Button("🗑 Удалить", variant="stop", scale=1)
                del_status = gr.Textbox(label="Статус", interactive=False)

                del_btn.click(fn=_delete_source, inputs=[del_id], outputs=[del_status])

            # ── Tab 4: Create from library ───────────────────────────────
            with gr.Tab("📂 Из библиотеки"):
                gr.Markdown("Выбери клипы и баннер из библиотеки — создай шортс без загрузки файлов.")

                with gr.Row():
                    lib_reload_btn = gr.Button("🔄 Обновить списки", variant="secondary")

                with gr.Tabs():

                    # ── Sub-tab: Split-screen from library ────────────────────
                    with gr.Tab("🎬 Сплит-экран"):
                        with gr.Row():
                            with gr.Column():
                                gr.Markdown("### 🎬 Верхний клип")
                                lib_top_dd = gr.Dropdown(
                                    choices=[], label="Верхний клип (основное видео + аудио)", interactive=True
                                )
                                gr.Markdown("### 🎮 Нижний клип")
                                lib_bottom_dd = gr.Dropdown(
                                    choices=[], label="Нижний клип (геймплей / фон)", interactive=True
                                )
                                gr.Markdown("### 🖼 Баннер")
                                lib_banner_split_dd = gr.Dropdown(
                                    choices=[], label="Баннер (необязательно)", interactive=True
                                )

                            with gr.Column():
                                gr.Markdown("### ⚙️ Настройки")
                                lib_whisper = gr.Dropdown(
                                    choices=["tiny", "base", "small", "medium", "large"],
                                    value=WHISPER_MODEL,
                                    label="Модель Whisper",
                                )
                                lib_fit_mode = gr.Radio(
                                    choices=[("Обрезка (crop)", "crop"), ("Чёрные полосы (pad)", "pad")],
                                    value="crop",
                                    label="Режим вписывания видео",
                                )
                                gr.Markdown("**Баннер**")
                                lib_banner_appear = gr.Slider(0, 30, value=BANNER_APPEAR_AT_SEC, step=0.5, label="Появляется на (сек)")
                                lib_banner_dur = gr.Slider(1, 30, value=min(BANNER_DURATION_SEC, 30), step=0.5, label="Длительность (сек)")
                                lib_banner_fade = gr.Slider(0.1, 2.0, value=BANNER_FADE_SEC, step=0.1, label="Fade (сек)")
                                lib_banner_mt = gr.Slider(0, 1900, value=BANNER_MARGIN_TOP, step=5, label="Отступ сверху (px)")
                                lib_banner_ml = gr.Slider(0, 200, value=BANNER_MARGIN_LEFT, step=5, label="Отступ слева (px)")

                        with gr.Row():
                            lib_split_run_btn = gr.Button("🚀 Создать сплит-шортс", variant="primary", size="lg")
                        lib_split_status = gr.Textbox(label="Статус", interactive=False)
                        lib_split_output = gr.Video(label="Результат", interactive=False)

                        lib_split_run_btn.click(
                            fn=_run_lib_split_pipeline,
                            inputs=[
                                lib_top_dd, lib_bottom_dd, lib_banner_split_dd,
                                lib_whisper, lib_fit_mode,
                                lib_banner_appear, lib_banner_dur,
                                lib_banner_fade, lib_banner_mt, lib_banner_ml,
                            ],
                            outputs=[lib_split_output, lib_split_status],
                        )

                    # ── Sub-tab: Blog from library ────────────────────────────
                    with gr.Tab("📱 Один клип (блог)"):
                        with gr.Row():
                            with gr.Column():
                                gr.Markdown("### 📱 Блог-клип")
                                lib_blog_dd = gr.Dropdown(
                                    choices=[], label="Блог-клип", interactive=True
                                )
                                gr.Markdown("### 🖼 Баннер")
                                lib_banner_blog_dd = gr.Dropdown(
                                    choices=[], label="Баннер (необязательно)", interactive=True
                                )

                            with gr.Column():
                                gr.Markdown("### ⚙️ Настройки")
                                lib_blog_whisper = gr.Dropdown(
                                    choices=["tiny", "base", "small", "medium", "large"],
                                    value=WHISPER_MODEL,
                                    label="Модель Whisper",
                                )
                                lib_blog_anim = gr.Radio(
                                    choices=[
                                        ("◀️ Слева", "slide_left"),
                                        ("▶️ Справа", "slide_right"),
                                        ("✨ Фейд", "fade"),
                                    ],
                                    value="slide_left",
                                    label="Анимация баннера",
                                )
                                gr.Markdown("**Баннер**")
                                lib_blog_appear = gr.Slider(0, 30, value=BANNER_APPEAR_AT_SEC, step=0.5, label="Появляется на (сек)")
                                lib_blog_fade = gr.Slider(0.1, 2.0, value=BANNER_FADE_SEC, step=0.1, label="Fade (сек)")
                                lib_blog_mt = gr.Slider(0, 1900, value=SINGLE_BANNER_MARGIN_TOP, step=5, label="Отступ баннера сверху (px)")
                                lib_blog_ml = gr.Slider(0, 200, value=BANNER_MARGIN_LEFT, step=5, label="Отступ баннера слева (px)")
                                gr.Markdown("**Цветокоррекция**")
                                lib_blog_brightness = gr.Slider(-0.5, 0.5, value=VIDEO_EQ_BRIGHTNESS, step=0.01, label="Яркость")
                                lib_blog_contrast = gr.Slider(0.5, 2.0, value=VIDEO_EQ_CONTRAST, step=0.01, label="Контраст")
                                lib_blog_saturation = gr.Slider(0.0, 3.0, value=VIDEO_EQ_SATURATION, step=0.05, label="Насыщенность")
                                gr.Markdown("**Субтитры**")
                                lib_blog_mv = gr.Slider(0, 1900, value=SINGLE_SUBTITLE_MARGIN_V, step=10, label="Позиция субтитров (px от низа)")

                        with gr.Row():
                            lib_blog_run_btn = gr.Button("🚀 Создать блог-клип", variant="primary", size="lg")
                        lib_blog_status = gr.Textbox(label="Статус", interactive=False)
                        lib_blog_output = gr.Video(label="Результат", interactive=False)

                        lib_blog_run_btn.click(
                            fn=_run_lib_blog_pipeline,
                            inputs=[
                                lib_blog_dd, lib_banner_blog_dd,
                                lib_blog_whisper,
                                lib_blog_appear, lib_blog_fade,
                                lib_blog_mt, lib_blog_ml,
                                lib_blog_anim,
                                lib_blog_brightness, lib_blog_contrast, lib_blog_saturation,
                                lib_blog_mv,
                            ],
                            outputs=[lib_blog_output, lib_blog_status],
                        )

                lib_reload_btn.click(
                    fn=_refresh_lib_dropdowns,
                    inputs=[],
                    outputs=[lib_top_dd, lib_bottom_dd, lib_blog_dd, lib_banner_split_dd, lib_banner_blog_dd],
                )

            # ── Tab 5: Video Gallery ──────────────────────────────────────
            with gr.Tab("📂 Видеотека"):
                gr.Markdown("Просмотр нарезанных клипов и готовых шортсов.")

                with gr.Tabs():

                    # ── Sub-tab: Library clips ────────────────────────────────
                    with gr.Tab("🎞 Клипы из библиотеки"):
                        with gr.Row():
                            lib_cat = gr.Dropdown(
                                choices=list(_GALLERY_CATEGORIES.keys()),
                                value=list(_GALLERY_CATEGORIES.keys())[0],
                                label="Категория",
                                scale=3,
                            )
                            lib_refresh = gr.Button("🔄 Обновить", scale=1)

                        lib_status = gr.Textbox(label="", interactive=False, max_lines=1)
                        lib_page_state = gr.State(0)

                        # 2×2 grid of videos
                        with gr.Row():
                            lib_v = [gr.Video(label="", interactive=False) for _ in range(_PAGE_SIZE)]
                        lib_labels = [gr.Markdown("") for _ in range(_PAGE_SIZE)]

                        with gr.Row():
                            lib_prev = gr.Button("◀ Назад", scale=1)
                            lib_page_info = gr.Textbox("", interactive=False, scale=3, max_lines=1)
                            lib_next = gr.Button("Вперёд ▶", scale=1)

                        def _lib_load(cat, page):
                            result = _load_library_page(cat, page)
                            paths = list(result[:_PAGE_SIZE])
                            labels = list(result[_PAGE_SIZE:_PAGE_SIZE*2])
                            new_page = result[_PAGE_SIZE*2]
                            status = result[_PAGE_SIZE*2+1]
                            return (*paths, *[f"**{l}**" if l else "" for l in labels], new_page, status, status)

                        def _lib_prev(cat, page):
                            return _lib_load(cat, page - 1)

                        def _lib_next(cat, page):
                            return _lib_load(cat, page + 1)

                        _lib_outputs = [*lib_v, *lib_labels, lib_page_state, lib_page_info, lib_status]

                        lib_refresh.click(_lib_load, [lib_cat, lib_page_state], _lib_outputs)
                        lib_cat.change(lambda cat: _lib_load(cat, 0), [lib_cat], _lib_outputs)
                        lib_prev.click(_lib_prev, [lib_cat, lib_page_state], _lib_outputs)
                        lib_next.click(_lib_next, [lib_cat, lib_page_state], _lib_outputs)

                    # ── Sub-tab: Generated shorts ─────────────────────────────
                    with gr.Tab("🎬 Готовые шортсы"):
                        with gr.Row():
                            out_refresh = gr.Button("🔄 Обновить список")
                        out_status = gr.Textbox(label="", interactive=False, max_lines=1)
                        out_page_state = gr.State(0)

                        with gr.Row():
                            out_v = [gr.Video(label="", interactive=False) for _ in range(_PAGE_SIZE)]
                        out_labels = [gr.Markdown("") for _ in range(_PAGE_SIZE)]

                        with gr.Row():
                            out_prev = gr.Button("◀ Назад", scale=1)
                            out_page_info = gr.Textbox("", interactive=False, scale=3, max_lines=1)
                            out_next = gr.Button("Вперёд ▶", scale=1)

                        def _out_load(page):
                            result = _load_output_page(page)
                            paths = list(result[:_PAGE_SIZE])
                            labels = list(result[_PAGE_SIZE:_PAGE_SIZE*2])
                            new_page = result[_PAGE_SIZE*2]
                            status = result[_PAGE_SIZE*2+1]
                            return (*paths, *[f"**{l}**" if l else "" for l in labels], new_page, status, status)

                        _out_outputs = [*out_v, *out_labels, out_page_state, out_page_info, out_status]

                        out_refresh.click(lambda: _out_load(0), [], _out_outputs)
                        out_prev.click(lambda p: _out_load(p - 1), [out_page_state], _out_outputs)
                        out_next.click(lambda p: _out_load(p + 1), [out_page_state], _out_outputs)

    return demo
