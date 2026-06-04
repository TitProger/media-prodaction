"""
bot.py — Content Factory Telegram bot.

State machine via context.user_data['_state'] instead of ConversationHandler
so inline-keyboard callbacks and message handlers never conflict.

States
------
None                — idle, show menu
'lib_await_file'    — library upload: waiting for the media file
'lib_await_name'    — library upload: waiting for a text name
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import subprocess
import traceback
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode

H = ParseMode.HTML  # shortcut used in every send/edit call

def _e(text: str) -> str:
    """Escape user-supplied content for HTML parse mode."""
    return _html.escape(str(text))
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from content_factory.config.settings import (
    BANNER_ANIMATION,
    BANNER_APPEAR_AT_SEC,
    BANNER_DURATION_SEC,
    BANNER_FADE_SEC,
    BOTTOM_CLIP_DURATION,
    OUTPUT_DIR,
    SINGLE_SUBTITLE_MARGIN_V,
    TELEGRAM_BOT_TOKEN,
    WHISPER_MODEL,
)
from content_factory.core.subtitle_generator import generate_subtitles
from content_factory.core.video_composer import compose, compose_single
from content_factory.core.clip_finder import find_best_clips
from content_factory.core.video_cutter import cut_clips, split_by_duration
from content_factory.db.library import (
    VIDEO_CATEGORIES as _VIDEO_CATEGORIES_DB,
    add_file,
    count_unused_clips,
    delete_file,
    delete_source_cascade,
    get_file,
    get_storage_path,
    init_db,
    list_clips,
    list_files,
    list_sources,
    mark_used,
    pick_random_clip,
    pick_random_unused_clip,
)

logger = logging.getLogger(__name__)

# Keep strong references to background tasks so GC doesn't collect them early
_background_tasks: set[asyncio.Task] = set()


def _create_task(coro) -> asyncio.Task:
    """Create a background task and keep a strong reference until it finishes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ─── user_data keys ──────────────────────────────────────────────────────────
_ST      = "_state"          # current state string
_LIB_CAT = "_lib_category"   # category being uploaded to
_LIB_FILE = "_lib_file_path" # temp path of uploaded file
_WORK_DIR = "_work_dir"

# ─── Video categories that support AI clip cutting ───────────────────────────
_VIDEO_CATEGORIES = _VIDEO_CATEGORIES_DB  # {"top_video", "bottom_video"}

# ─── Category metadata ───────────────────────────────────────────────────────
CATEGORY_LABEL = {
    "top_video":    "🎬 Верхние видео (сплит)",
    "bottom_video": "🎮 Нижние видео (сплит)",
    "blog_video":   "📱 Блог видео (один клип)",
    "banner_image": "🖼 Фото-баннеры",
    "banner_video": "📹 Видео-баннеры",
}
CATEGORY_HINT = {
    "top_video":    "видео-файл (MP4, MOV, AVI…)",
    "bottom_video": "видео-файл (MP4, MOV, AVI…)",
    "blog_video":   "видео-файл (MP4, MOV, AVI…)",
    "banner_image": "фото (PNG, JPG…)",
    "banner_video": "видео-файл (MP4, MOV…)",
}


# ─── Keyboards ───────────────────────────────────────────────────────────────

def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Сплит-экран шортс",  callback_data="menu:create")],
        [InlineKeyboardButton("📱 Один клип (блог)",   callback_data="menu:single")],
        [InlineKeyboardButton("📚 Библиотека медиа",   callback_data="menu:library")],
    ])


def _kb_library() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Верхние (сплит)",  callback_data="lib:top_video"),
            InlineKeyboardButton("🎮 Нижние (сплит)",   callback_data="lib:bottom_video"),
        ],
        [
            InlineKeyboardButton("📱 Блог видео",        callback_data="lib:blog_video"),
        ],
        [
            InlineKeyboardButton("🖼 Фото-баннеры",     callback_data="lib:banner_image"),
            InlineKeyboardButton("📹 Видео-баннеры",    callback_data="lib:banner_video"),
        ],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:main")],
    ])


def _kb_sources(category: str, rows: list) -> InlineKeyboardMarkup:
    """Keyboard for the list of SOURCE videos (top_video / bottom_video)."""
    kb = []
    for row in rows:
        n = row["clip_count"]
        clips_btn = f"📋 {n}" if n else "📋 0"
        kb.append([
            InlineKeyboardButton(
                f"✂️ {row['name'][:20]}",
                callback_data=f"lib_cut:{row['id']}:{category}",
            ),
            InlineKeyboardButton(
                clips_btn,
                callback_data=f"lib_clips:{row['id']}:{category}",
            ),
            InlineKeyboardButton(
                "🗑",
                callback_data=f"lib_del_src:{row['id']}:{category}",
            ),
        ])
    kb.append([
        InlineKeyboardButton("➕ Загрузить", callback_data=f"lib_upload:{category}"),
        InlineKeyboardButton("⬅️ Назад",    callback_data="menu:library"),
    ])
    return InlineKeyboardMarkup(kb)


def _kb_clips(source_id: int, category: str, rows: list) -> InlineKeyboardMarkup:
    """Keyboard for the list of CLIPS belonging to one source."""
    kb = []
    for row in rows:
        kb.append([InlineKeyboardButton(
            f"🗑 {row['name'][:30]}",
            callback_data=f"lib_del_clip:{row['id']}:{source_id}:{category}",
        )])
    kb.append([InlineKeyboardButton(
        "⬅️ К источникам", callback_data=f"lib:{category}",
    )])
    return InlineKeyboardMarkup(kb)


def _kb_banners(category: str, rows: list) -> InlineKeyboardMarkup:
    """Keyboard for banner categories (no sources/clips hierarchy)."""
    kb = []
    for row in rows:
        kb.append([InlineKeyboardButton(
            f"🗑 {row['name'][:30]}",
            callback_data=f"lib_del_src:{row['id']}:{category}",
        )])
    kb.append([
        InlineKeyboardButton("➕ Загрузить", callback_data=f"lib_upload:{category}"),
        InlineKeyboardButton("⬅️ Назад",    callback_data="menu:library"),
    ])
    return InlineKeyboardMarkup(kb)


# ─── Generation wizard keyboards ─────────────────────────────────────────────

def _kb_gen_top(data: list[tuple]) -> InlineKeyboardMarkup:
    """data: [(source_row, unused_count)] — only rows with unused > 0."""
    kb = [
        [InlineKeyboardButton(
            f"🎬 {row['name'][:22]}  ({n} св.)",
            callback_data=f"gen_top:{row['id']}",
        )]
        for row, n in data if n > 0
    ]
    kb.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(kb)


def _kb_gen_bot(sources: list) -> InlineKeyboardMarkup:
    """sources: source rows with clip_count > 0."""
    kb = [
        [InlineKeyboardButton(
            f"🎮 {s['name'][:22]}  ({s['clip_count']} кл.)",
            callback_data=f"gen_bot:{s['id']}",
        )]
        for s in sources
    ]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="gen_step:top")])
    return InlineKeyboardMarkup(kb)


def _kb_gen_banner(banners: list) -> InlineKeyboardMarkup:
    kb = []
    for b in banners:
        icon = "🖼" if b["category"] == "banner_image" else "📹"
        kb.append([InlineKeyboardButton(
            f"{icon} {b['name'][:28]}",
            callback_data=f"gen_ban:{b['id']}",
        )])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="gen_step:bottom")])
    return InlineKeyboardMarkup(kb)


def _kb_gen_confirm() -> InlineKeyboardMarkup:
    """Confirm screen — choose banner animation style before generating."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎞 Анимация баннера:", callback_data="noop")],
        [
            InlineKeyboardButton("◀️ Слева",   callback_data="gen_run:slide_left"),
            InlineKeyboardButton("▶️ Справа",  callback_data="gen_run:slide_right"),
            InlineKeyboardButton("✨ Фейд",    callback_data="gen_run:fade"),
        ],
        [
            InlineKeyboardButton("✏️ Изменить", callback_data="gen_step:top"),
            InlineKeyboardButton("⬅️ Меню",     callback_data="menu:main"),
        ],
    ])


# ─── Single-video keyboards ──────────────────────────────────────────────────

def _kb_sv_sources(rows: list) -> InlineKeyboardMarkup:
    kb = []
    for src in rows:
        kb.append([InlineKeyboardButton(
            f"🎬 {src['name']} ({src['clip_count']} клипов)",
            callback_data=f"sv_src:{src['id']}",
        )])
    kb.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(kb)


def _kb_sv_ai() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 С AI анализом",   callback_data="sv_ai:yes"),
            InlineKeyboardButton("⚡ Без AI (быстро)", callback_data="sv_ai:no"),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:single")],
    ])


def _kb_sv_banner(banners: list) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton("🚫 Без баннера", callback_data="sv_ban:none")]]
    for b in banners:
        icon = "🖼" if b["category"] == "banner_image" else "📹"
        kb.append([InlineKeyboardButton(
            f"{icon} {b['name']}", callback_data=f"sv_ban:{b['id']}",
        )])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:single")])
    return InlineKeyboardMarkup(kb)


def _kb_sv_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎞 Анимация баннера:", callback_data="noop")],
        [
            InlineKeyboardButton("◀️ Слева",   callback_data="sv_run:slide_left"),
            InlineKeyboardButton("▶️ Справа",  callback_data="sv_run:slide_right"),
            InlineKeyboardButton("✨ Фейд",    callback_data="sv_run:fade"),
        ],
        [
            InlineKeyboardButton("✏️ Изменить", callback_data="menu:single"),
            InlineKeyboardButton("⬅️ Меню",     callback_data="menu:main"),
        ],
    ])


def _kb_sv_confirm_no_banner() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Генерировать", callback_data="sv_run:none")],
        [
            InlineKeyboardButton("✏️ Изменить", callback_data="menu:single"),
            InlineKeyboardButton("⬅️ Меню",     callback_data="menu:main"),
        ],
    ])


# ─── Generation wizard text helpers ──────────────────────────────────────────

def _gen_top_text(data: list[tuple]) -> str:
    available = [(r, n) for r, n in data if n > 0]
    if not data:
        return (
            "🚀 <b>Создать шортс</b>\n\n"
            "❌ Нет источников для верхнего видео.\n"
            "<i>Загрузи видео через 📚 Библиотека → 🎬 Верхние видео.</i>"
        )
    if not available:
        return (
            "🚀 <b>Создать шортс</b>\n\n"
            "😕 Все клипы уже использованы.\n"
            "<i>Нарежи новые клипы (✂️) или загрузи новые видео.</i>"
        )
    lines = ["🚀 <b>Создать шортс</b>", "", "Шаг <b>1/3</b> — выбери источник верхнего видео:\n"]
    for r, n in available:
        lines.append(f"▸ <code>{_e(r['name'])}</code> — {n} свободных клипов")
    return "\n".join(lines)


def _gen_bot_text(sources: list) -> str:
    lines = ["🚀 <b>Создать шортс</b>", "", "Шаг <b>2/3</b> — выбери источник нижнего видео:\n"]
    for s in sources:
        lines.append(f"▸ <code>{_e(s['name'])}</code> — {s['clip_count']} клипов")
    return "\n".join(lines)


def _gen_banner_text(banners: list) -> str:
    lines = ["🚀 <b>Создать шортс</b>", "", "Шаг <b>3/3</b> — выбери баннер:\n"]
    for b in banners:
        icon = "🖼" if b["category"] == "banner_image" else "📹"
        lines.append(f"▸ {icon} <code>{_e(b['name'])}</code>")
    return "\n".join(lines)


def _gen_confirm_text(top_src, bot_src, banner, top_unused: int) -> str:
    return (
        "🚀 <b>Создать шортс</b>\n\n"
        "✅ Всё выбрано:\n\n"
        f"▸ Верхнее: <code>{_e(top_src['name'])}</code> ({top_unused} св. кл.)\n"
        f"▸ Нижнее:  <code>{_e(bot_src['name'])}</code>\n"
        f"▸ Баннер:  <code>{_e(banner['name'])}</code>\n\n"
        "<i>Будет взят случайный неиспользованный клип из верхней папки.</i>"
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"


def _make_work_dir(uid: int) -> Path:
    d = OUTPUT_DIR / f"tg_{uid}_{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sources_text(category: str, rows: list) -> str:
    """Text for the sources list of a video category."""
    label = CATEGORY_LABEL[category]
    if not rows:
        return (
            f"📁 <b>{_e(label)}</b>\n\n"
            "<i>Нет загруженных видео.\nЗагрузи первое через ➕ или API.</i>"
        )
    lines = [f"📁 <b>{_e(label)}</b>\n"]
    for i, r in enumerate(rows, 1):
        clips_info = f" · {r['clip_count']} кл." if r["clip_count"] else ""
        lines.append(
            f"{i}. <code>{_e(r['name'])}</code>"
            f" — {_fmt_size(r['size_bytes'])}{clips_info}"
        )
    lines.append("\n<i>✂️ нарезать   📋 клипы   🗑 удалить источник</i>")
    return "\n".join(lines)


def _clips_text(source_name: str, rows: list) -> str:
    """Text for the clips list of a source video."""
    if not rows:
        return (
            f"📋 <b>Клипы: «{_e(source_name)}»</b>\n\n"
            "<i>Клипов ещё нет. Нажми ✂️ у источника, чтобы нарезать.</i>"
        )
    lines = [f"📋 <b>Клипы: «{_e(source_name)}»</b>\n"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. <code>{_e(r['name'])}</code> — {_fmt_size(r['size_bytes'])}")
    lines.append("\n<i>🗑 — удалить клип</i>")
    return "\n".join(lines)


def _category_text(category: str, rows: list) -> str:
    """Text for banner categories (flat, no hierarchy)."""
    label = CATEGORY_LABEL[category]
    if not rows:
        return f"📁 <b>{_e(label)}</b>\n\n<i>Пусто — загрузи первый файл.</i>"
    lines = [f"📁 <b>{_e(label)}</b>\n"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. <code>{_e(r['name'])}</code> — {_fmt_size(r['size_bytes'])}")
    lines.append("\n<i>🗑 — удалить файл</i>")
    return "\n".join(lines)


def _clear_state(ud: dict) -> None:
    for k in (_ST, _LIB_CAT, _LIB_FILE, _WORK_DIR):
        ud.pop(k, None)


# ─── Generation wizard state keys (split-screen) ─────────────────────────────
_GEN_TOP = "_gen_top"  # int: selected top_video source_id
_GEN_BOT = "_gen_bot"  # int: selected bottom_video source_id
_GEN_BAN = "_gen_ban"  # int: selected banner file_id


def _gen_clear(ud: dict) -> None:
    for k in (_GEN_TOP, _GEN_BOT, _GEN_BAN):
        ud.pop(k, None)


# ─── Single-video wizard state keys ──────────────────────────────────────────
_SV_SRC = "_sv_src"   # int: selected top_video source_id (single-video mode)
_SV_BAN = "_sv_ban"   # int | None: selected banner file_id (optional)
_SV_AI  = "_sv_ai"    # bool: use AI clip finding


def _sv_clear(ud: dict) -> None:
    for k in (_SV_SRC, _SV_BAN, _SV_AI):
        ud.pop(k, None)


# ─── /start ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_state(context.user_data)
    await update.message.reply_text(
        "👋 Добро пожаловать в <b>Content Factory</b>!\n\nВыбери действие:",
        parse_mode=H,
        reply_markup=_kb_main(),
    )


# ─── /cancel ─────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_state(context.user_data)
    await update.message.reply_text("❌ Отменено.", reply_markup=_kb_main())


# ─── Callback: main menu ─────────────────────────────────────────────────────

async def cb_menu_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    _clear_state(context.user_data)
    await q.edit_message_text("👋 Главное меню:", reply_markup=_kb_main())


# ─── Callback: library menu ──────────────────────────────────────────────────

async def cb_menu_library(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "📚 <b>Библиотека медиа</b>\n\nВыбери категорию:",
        parse_mode=H,
        reply_markup=_kb_library(),
    )


# ─── Callback: show category ─────────────────────────────────────────────────

async def cb_lib_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    category = q.data.split(":")[1]
    user_id = q.from_user.id
    if category in _VIDEO_CATEGORIES:
        rows = list_sources(user_id, category)
        await q.edit_message_text(
            _sources_text(category, rows),
            parse_mode=H,
            reply_markup=_kb_sources(category, rows),
        )
    else:
        rows = list_files(user_id, category)
        await q.edit_message_text(
            _category_text(category, rows),
            parse_mode=H,
            reply_markup=_kb_banners(category, rows),
        )


# ─── Callback: delete source (cascade) ──────────────────────────────────────

async def cb_lib_del_src(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a source file and ALL its clips, then refresh the sources list."""
    q = update.callback_query
    await q.answer()
    _, file_id_str, category = q.data.split(":")
    user_id = q.from_user.id
    if category in _VIDEO_CATEGORIES:
        delete_source_cascade(int(file_id_str), user_id)
        rows = list_sources(user_id, category)
        await q.edit_message_text(
            _sources_text(category, rows),
            parse_mode=H,
            reply_markup=_kb_sources(category, rows),
        )
    else:
        delete_file(int(file_id_str), user_id)
        rows = list_files(user_id, category)
        await q.edit_message_text(
            _category_text(category, rows),
            parse_mode=H,
            reply_markup=_kb_banners(category, rows),
        )


# ─── Callback: show clips of a source ────────────────────────────────────────

async def cb_lib_clips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the clips belonging to one source video."""
    q = update.callback_query
    await q.answer()
    _, source_id_str, category = q.data.split(":")
    source_id = int(source_id_str)
    user_id = q.from_user.id
    source = get_file(source_id, user_id)
    source_name = source["name"] if source else "?"
    rows = list_clips(user_id, source_id)
    await q.edit_message_text(
        _clips_text(source_name, rows),
        parse_mode=H,
        reply_markup=_kb_clips(source_id, category, rows),
    )


# ─── Callback: delete single clip ────────────────────────────────────────────

async def cb_lib_del_clip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a single clip and refresh the clips list."""
    q = update.callback_query
    await q.answer()
    # pattern: lib_del_clip:{clip_id}:{source_id}:{category}
    parts = q.data.split(":")
    clip_id   = int(parts[1])
    source_id = int(parts[2])
    category  = parts[3]
    user_id = q.from_user.id
    delete_file(clip_id, user_id)
    source = get_file(source_id, user_id)
    source_name = source["name"] if source else "?"
    rows = list_clips(user_id, source_id)
    await q.edit_message_text(
        _clips_text(source_name, rows),
        parse_mode=H,
        reply_markup=_kb_clips(source_id, category, rows),
    )


# ─── Callback: start library upload ──────────────────────────────────────────

async def cb_lib_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    category = q.data.split(":")[1]
    label = CATEGORY_LABEL[category]
    hint = CATEGORY_HINT[category]

    context.user_data[_ST] = "lib_await_file"
    context.user_data[_LIB_CAT] = category

    await q.edit_message_text(
        f"📤 <b>Загрузка в «{_e(label)}»</b>\n\nОтправь {_e(hint)}.\n<i>/cancel для отмены</i>",
        parse_mode=H,
    )


# ─── Callback: start create-shorts (generation wizard) ───────────────────────

async def cb_menu_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    _clear_state(context.user_data)
    _gen_clear(context.user_data)
    user_id = q.from_user.id

    top_sources = list_sources(user_id, "top_video")
    data = [(src, count_unused_clips(user_id, src["id"])) for src in top_sources]
    available = [(r, n) for r, n in data if n > 0]

    if not available:
        await q.edit_message_text(
            _gen_top_text(data),
            parse_mode=H,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:main"),
            ]]),
        )
        return

    await q.edit_message_text(
        _gen_top_text(data), parse_mode=H, reply_markup=_kb_gen_top(data),
    )


# ─── Callback: wizard step — select top source ───────────────────────────────

async def cb_gen_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data[_GEN_TOP] = int(q.data.split(":")[1])
    user_id = q.from_user.id

    bot_sources = list_sources(user_id, "bottom_video")
    available = [s for s in bot_sources if s["clip_count"] > 0]

    if not available:
        await q.edit_message_text(
            "🚀 <b>Создать шортс</b>\n\n"
            "❌ Нет клипов для нижнего видео.\n"
            "<i>Загрузи видео через 📚 Библиотека → 🎮 Нижние видео.</i>",
            parse_mode=H,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="gen_step:top"),
            ]]),
        )
        return

    await q.edit_message_text(
        _gen_bot_text(available), parse_mode=H, reply_markup=_kb_gen_bot(available),
    )


# ─── Callback: wizard step — select bottom source ────────────────────────────

async def cb_gen_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data[_GEN_BOT] = int(q.data.split(":")[1])
    user_id = q.from_user.id

    banners = [*list_files(user_id, "banner_image"), *list_files(user_id, "banner_video")]

    if not banners:
        await q.edit_message_text(
            "🚀 <b>Создать шортс</b>\n\n"
            "❌ Нет баннеров.\n"
            "<i>Загрузи баннер через 📚 Библиотека → 🖼 Фото-баннеры.</i>",
            parse_mode=H,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="gen_step:bottom"),
            ]]),
        )
        return

    await q.edit_message_text(
        _gen_banner_text(banners), parse_mode=H, reply_markup=_kb_gen_banner(banners),
    )


# ─── Callback: wizard step — select banner ───────────────────────────────────

async def cb_gen_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data[_GEN_BAN] = int(q.data.split(":")[1])
    user_id = q.from_user.id

    top_src_id = context.user_data.get(_GEN_TOP)
    bot_src_id = context.user_data.get(_GEN_BOT)
    ban_id     = context.user_data[_GEN_BAN]

    top_src = get_file(top_src_id, user_id) if top_src_id else None
    bot_src = get_file(bot_src_id, user_id) if bot_src_id else None
    banner  = get_file(ban_id, user_id)

    if not top_src or not bot_src or not banner:
        await q.edit_message_text("❌ Ошибка выбора. Начни заново.", reply_markup=_kb_main())
        return

    unused = count_unused_clips(user_id, top_src_id)
    await q.edit_message_text(
        _gen_confirm_text(top_src, bot_src, banner, unused),
        parse_mode=H,
        reply_markup=_kb_gen_confirm(),
    )


# ─── Callback: navigate back between wizard steps ────────────────────────────

async def cb_gen_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    step = q.data.split(":")[1]
    user_id = q.from_user.id

    if step == "top":
        _gen_clear(context.user_data)
        top_sources = list_sources(user_id, "top_video")
        data = [(src, count_unused_clips(user_id, src["id"])) for src in top_sources]
        await q.edit_message_text(
            _gen_top_text(data), parse_mode=H, reply_markup=_kb_gen_top(data),
        )

    elif step == "bottom":
        context.user_data.pop(_GEN_BOT, None)
        context.user_data.pop(_GEN_BAN, None)
        bot_sources = list_sources(user_id, "bottom_video")
        available = [s for s in bot_sources if s["clip_count"] > 0]
        await q.edit_message_text(
            _gen_bot_text(available), parse_mode=H, reply_markup=_kb_gen_bot(available),
        )

    elif step == "banner":
        context.user_data.pop(_GEN_BAN, None)
        banners = [*list_files(user_id, "banner_image"), *list_files(user_id, "banner_video")]
        await q.edit_message_text(
            _gen_banner_text(banners), parse_mode=H, reply_markup=_kb_gen_banner(banners),
        )


# ─── Callback: run generation ─────────────────────────────────────────────────

async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """No-op handler for label-only buttons."""
    await update.callback_query.answer()


async def cb_gen_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    # callback_data format: "gen_run:{animation}" e.g. "gen_run:slide_left"
    parts = q.data.split(":")
    banner_animation = parts[1] if len(parts) > 1 else BANNER_ANIMATION
    await q.answer("🎬 Запускаю!")
    user_id = q.from_user.id

    top_src_id = context.user_data.get(_GEN_TOP)
    bot_src_id = context.user_data.get(_GEN_BOT)
    ban_id     = context.user_data.get(_GEN_BAN)

    if not all([top_src_id, bot_src_id, ban_id]):
        await q.edit_message_text("❌ Выбор утерян — начни заново.", reply_markup=_kb_main())
        return

    # Pick clips
    top_clip = pick_random_unused_clip(user_id, top_src_id)
    bot_clip = pick_random_clip(user_id, bot_src_id)
    banner   = get_file(ban_id, user_id)

    if not top_clip:
        await q.edit_message_text(
            "😕 Все клипы верхнего видео уже использованы. Нарежи новые!",
            reply_markup=_kb_main(),
        )
        return
    if not bot_clip:
        await q.edit_message_text(
            "😕 Нет клипов нижнего видео. Нарежи новые!",
            reply_markup=_kb_main(),
        )
        return

    _gen_clear(context.user_data)
    work_dir = _make_work_dir(user_id)

    top_src  = get_file(top_src_id, user_id)
    bot_src  = get_file(bot_src_id, user_id)

    await q.edit_message_text(
        f"🎬 <b>Генерирую шортс…</b>\n\n"
        f"▸ Верхнее: <code>{_e(top_clip['name'])}</code>\n"
        f"▸ Нижнее:  <code>{_e(bot_clip['name'])}</code>\n"
        f"▸ Баннер:  <code>{_e(banner['name'])}</code>",
        parse_mode=H,
    )

    _create_task(_run_gen_pipeline(
        chat_id=q.message.chat_id,
        bot=q.get_bot(),
        top_clip_id=top_clip["id"],
        top_path=top_clip["file_path"],
        bottom_path=bot_clip["file_path"],
        banner_path=banner["file_path"],
        work_dir=work_dir,
        banner_animation=banner_animation,
    ))


# ─── Message dispatcher ───────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get(_ST)

    if state == "lib_await_file":
        await _lib_recv_file(update, context)
    elif state == "lib_await_name":
        await _lib_recv_name(update, context)
    # else: no active state, silently ignore


# ─── Library upload steps ─────────────────────────────────────────────────────

async def _lib_recv_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    category = context.user_data[_LIB_CAT]
    storage_dir = get_storage_path(msg.from_user.id, category, subtype="source")

    fo = ext = None
    if msg.video:
        fo = await msg.video.get_file(); ext = ".mp4"
    elif msg.document:
        fo = await msg.document.get_file()
        ext = Path(msg.document.file_name or "").suffix or ".mp4"
    elif msg.photo:
        fo = await msg.photo[-1].get_file(); ext = ".jpg"
    else:
        await msg.reply_text("Пришли файл нужного типа или /cancel.")
        return

    dest = storage_dir / f"{uuid.uuid4().hex}{ext}"
    await msg.reply_text("⏬ Скачиваю файл…")
    await fo.download_to_drive(dest)

    context.user_data[_LIB_FILE] = str(dest)
    context.user_data[_ST] = "lib_await_name"
    await msg.reply_text(
        "✅ Файл получен!\n\nВведи <b>название</b> для этого файла:",
        parse_mode=H,
    )


async def _lib_recv_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    name = (msg.text or "").strip()[:120]
    if not name:
        await msg.reply_text("Название не может быть пустым. Введи ещё раз:")
        return

    category = context.user_data[_LIB_CAT]
    file_path = Path(context.user_data[_LIB_FILE])
    label = CATEGORY_LABEL[category]

    add_file(msg.from_user.id, name, category, file_path, subtype="source")
    _clear_state(context.user_data)

    await msg.reply_text(
        f"✅ <b>«{_e(name)}»</b> сохранён в «{_e(label)}»!\n\nЧто дальше?",
        parse_mode=H,
        reply_markup=_kb_main(),
    )




# ─── Callback: AI clip cut ───────────────────────────────────────────────────

async def cb_lib_cut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("⏳ Запускаю нарезку…")
    _, file_id_str, category = q.data.split(":")
    source_id = int(file_id_str)
    user_id = q.from_user.id
    row = get_file(source_id, user_id)
    if row is None:
        await q.edit_message_text("❌ Файл не найден.", reply_markup=_kb_main())
        return

    if category == "bottom_video":
        status = (
            f"✂️ <b>Нарезка: «{_e(row['name'])}»</b>\n\n"
            f"⏳ Режу на части по {int(BOTTOM_CLIP_DURATION)} с…"
        )
    else:
        status = (
            f"✂️ <b>Нарезка: «{_e(row['name'])}»</b>\n\n"
            "⏳ Транскрибирую аудио (Whisper) — это займёт несколько минут…"
        )
    await q.edit_message_text(status, parse_mode=H)

    _create_task(_run_cut_pipeline(
        chat_id=q.message.chat_id,
        bot=q.get_bot(),
        source_id=source_id,
        file_name=row["name"],
        file_path=row["file_path"],
        user_id=user_id,
        category=category,
    ))


async def _run_cut_pipeline(
    *, chat_id: int, bot, source_id: int, file_name: str, file_path: str,
    user_id: int, category: str,
) -> None:
    try:
        loop = asyncio.get_running_loop()
        video_path = Path(file_path)
        source_stem = video_path.stem
        output_dir = get_storage_path(
            user_id, category, subtype="clip", source_stem=source_stem,
        )

        if category == "bottom_video":
            # ── Simple time-based split (no Whisper, no AI) ──────────────────
            saved_paths: list[Path] = await loop.run_in_executor(
                None,
                lambda: split_by_duration(
                    video_path, output_dir,
                    chunk_sec=BOTTOM_CLIP_DURATION,
                    source_stem=source_stem,
                ),
            )
            if not saved_paths:
                await bot.send_message(
                    chat_id,
                    "❌ FFmpeg не смог нарезать. Проверьте логи.",
                    reply_markup=_kb_main(),
                )
                return

            for i, path in enumerate(saved_paths, 1):
                add_file(
                    user_id, f"{file_name} — Часть {i}"[:120],
                    category, path, subtype="clip", parent_id=source_id,
                )

            await bot.send_message(
                chat_id,
                f"✅ <b>Нарезано {len(saved_paths)} частей</b>"
                f" по {int(BOTTOM_CLIP_DURATION)} с\n\n"
                f"📁 Сохранено в «{_e(CATEGORY_LABEL[category])}»"
                f" → клипы/{_e(source_stem)}/",
                parse_mode=H,
                reply_markup=_kb_main(),
            )

        else:
            # ── AI-powered cut: Whisper + Claude API (top_video) ─────────────
            clips = await loop.run_in_executor(None, find_best_clips, video_path)
            if not clips:
                await bot.send_message(
                    chat_id,
                    "😕 Не удалось найти подходящие клипы в этом видео.",
                    reply_markup=_kb_main(),
                )
                return

            await bot.send_message(
                chat_id,
                f"✂️ Найдено <b>{len(clips)}</b> клипов. Нарезаю…",
                parse_mode=H,
            )

            saved_paths = await loop.run_in_executor(
                None,
                lambda: cut_clips(video_path, clips, output_dir, source_stem=source_stem),
            )
            if not saved_paths:
                await bot.send_message(
                    chat_id,
                    "❌ FFmpeg не смог нарезать ни одного клипа. Проверьте логи.",
                    reply_markup=_kb_main(),
                )
                return

            for clip_info, path in zip(clips[: len(saved_paths)], saved_paths):
                add_file(
                    user_id,
                    f"{file_name} — {clip_info['title']}"[:120],
                    category, path, subtype="clip", parent_id=source_id,
                )

            lines = [
                f"✅ <b>Нарезка завершена!</b>"
                f" Создано <b>{len(saved_paths)}</b> клипов:\n"
            ]
            for i, (clip_info, _p) in enumerate(
                zip(clips[: len(saved_paths)], saved_paths), 1
            ):
                dur = clip_info["end"] - clip_info["start"]
                lines.append(
                    f"{i}. <code>{_e(clip_info['title'])}</code> — {dur:.0f} с"
                )
            lines.append(
                f"\n📁 Сохранено в «{_e(CATEGORY_LABEL[category])}»"
                f" → клипы/{_e(source_stem)}/"
            )
            await bot.send_message(
                chat_id, "\n".join(lines), parse_mode=H, reply_markup=_kb_main(),
            )

    except Exception as exc:
        logger.error("Cut pipeline failed:\n%s", traceback.format_exc())
        await bot.send_message(
            chat_id,
            f"❌ Ошибка нарезки:\n<code>{_e(str(exc))}</code>\n\nНапиши /start заново.",
            parse_mode=H,
            reply_markup=_kb_main(),
        )


# ─── Generation pipeline (library-based) ─────────────────────────────────────

async def _run_gen_pipeline(
    *, chat_id: int, bot,
    top_clip_id: int, top_path: str, bottom_path: str,
    banner_path: str, work_dir: Path,
    banner_animation: str = BANNER_ANIMATION,
) -> None:
    try:
        loop = asyncio.get_running_loop()

        await bot.send_message(chat_id, "🎙 Транскрибирую аудио (Whisper)…")
        ass = await loop.run_in_executor(None, generate_subtitles, top_path, work_dir)

        await bot.send_message(chat_id, "🎬 Рендерю видео (FFmpeg)…")
        out: Path = await loop.run_in_executor(
            None,
            lambda: compose(
                top_video=top_path,
                bottom_video=bottom_path,
                banner_image=banner_path,
                subtitle_file=ass,
                output_path=work_dir / "output.mp4",
                banner_appear_at=BANNER_APPEAR_AT_SEC,
                banner_duration=BANNER_DURATION_SEC,
                banner_fade=BANNER_FADE_SEC,
                banner_animation=banner_animation,
            ),
        )

        # Mark top clip as used only AFTER successful render
        mark_used(top_clip_id)

        await bot.send_message(chat_id, "✅ Готово! Отправляю видео…")
        size_mb = out.stat().st_size / 1024 / 1024

        # Auto-compress if file is too large for Telegram (50 MB limit)
        if size_mb > 45:
            await bot.send_message(chat_id, f"⚙️ Файл {size_mb:.0f} МБ — сжимаю для отправки…")
            compressed = out.with_stem(out.stem + "_compressed")
            subprocess.run([
                "ffmpeg", "-y", "-i", str(out),
                "-c:v", "libx264", "-crf", "32", "-preset", "fast",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(compressed),
            ], capture_output=True)
            if compressed.exists() and compressed.stat().st_size < out.stat().st_size:
                out = compressed
                size_mb = out.stat().st_size / 1024 / 1024

        with open(out, "rb") as f:
            if size_mb <= 50:
                await bot.send_video(
                    chat_id, video=f,
                    caption="🎬 Шортс готов!", supports_streaming=True,
                )
            else:
                await bot.send_document(
                    chat_id, document=f,
                    caption=f"🎬 Шортс готов ({size_mb:.0f} МБ — отправлен как файл).",
                )
        await bot.send_message(chat_id, "Что дальше?", reply_markup=_kb_main())

    except Exception as exc:
        logger.error("Gen pipeline failed:\n%s", traceback.format_exc())
        await bot.send_message(
            chat_id,
            f"❌ Ошибка:\n<code>{_e(str(exc))}</code>\n\nНапиши /start заново.",
            parse_mode=H,
            reply_markup=_kb_main(),
        )




# ─── Debug: log every incoming update (group -1, runs before all handlers) ────

async def _log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        q = update.callback_query
        logger.info(
            "[UPDATE] CallbackQuery | data=%r | user=%s | chat=%s",
            q.data, q.from_user.id, q.message.chat_id if q.message else "?",
        )
    elif update.message:
        msg = update.message
        kind = (
            "text" if msg.text else
            "video" if msg.video else
            "photo" if msg.photo else
            "document" if msg.document else
            "other"
        )
        logger.info(
            "[UPDATE] Message | kind=%s | user=%s | chat=%s",
            kind, msg.from_user.id if msg.from_user else "?", msg.chat_id,
        )


# ─── Single-video wizard callbacks ───────────────────────────────────────────

async def cb_menu_single(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for single-video mode."""
    q = update.callback_query
    await q.answer()
    _clear_state(context.user_data)
    _sv_clear(context.user_data)
    user_id = q.from_user.id

    sources = list_sources(user_id, "blog_video")
    available = [s for s in sources if s["clip_count"] > 0]

    if not available:
        await q.edit_message_text(
            "📱 <b>Один клип (блог)</b>\n\n"
            "❌ Нет нарезанных клипов.\n"
            "<i>Загрузи видео через 📚 Библиотека → 📱 Блог видео и нарежь их.</i>",
            parse_mode=H,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:main"),
            ]]),
        )
        return

    await q.edit_message_text(
        "📱 <b>Один клип (блог)</b>\n\nВыбери источник видео:",
        parse_mode=H,
        reply_markup=_kb_sv_sources(available),
    )


async def cb_sv_src(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User selected a source — ask if AI analysis is needed."""
    q = update.callback_query
    await q.answer()
    context.user_data[_SV_SRC] = int(q.data.split(":")[1])

    await q.edit_message_text(
        "📱 <b>Один клип (блог)</b>\n\n"
        "🤖 Использовать AI для поиска лучших моментов?\n\n"
        "<i>С AI — анализирует транскрипт и выбирает самые интересные фрагменты.\n"
        "Без AI — быстрый эвристический выбор.</i>",
        parse_mode=H,
        reply_markup=_kb_sv_ai(),
    )


async def cb_sv_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User chose AI mode — now pick banner."""
    q = update.callback_query
    await q.answer()
    context.user_data[_SV_AI] = q.data.split(":")[1] == "yes"
    user_id = q.from_user.id

    banners = [
        *list_files(user_id, "banner_image"),
        *list_files(user_id, "banner_video"),
    ]

    await q.edit_message_text(
        "📱 <b>Один клип (блог)</b>\n\nВыбери баннер (необязательно):",
        parse_mode=H,
        reply_markup=_kb_sv_banner(banners),
    )


async def cb_sv_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User picked banner (or 'none') — show confirm screen."""
    q = update.callback_query
    await q.answer()
    val = q.data.split(":")[1]
    context.user_data[_SV_BAN] = None if val == "none" else int(val)
    user_id = q.from_user.id

    src_id  = context.user_data[_SV_SRC]
    ban_id  = context.user_data[_SV_BAN]
    use_ai  = context.user_data.get(_SV_AI, False)

    src  = get_file(src_id, user_id)
    ban  = get_file(ban_id, user_id) if ban_id else None

    clip_count = count_unused_clips(user_id, src_id)
    lines = [
        "📱 <b>Один клип (блог)</b>\n",
        f"✅ <b>Всё выбрано:</b>\n",
        f"▸ Видео: <code>{_e(src['name'])}</code>",
        f"▸ Баннер: <code>{_e(ban['name']) if ban else 'Нет'}</code>",
        f"▸ AI анализ: <code>{'Да' if use_ai else 'Нет (быстро)'}</code>",
        f"\n<i>Будет взят случайный неиспользованный клип ({clip_count} доступно).</i>",
    ]
    text = "\n".join(lines)

    if ban:
        await q.edit_message_text(text, parse_mode=H, reply_markup=_kb_sv_confirm())
    else:
        await q.edit_message_text(text, parse_mode=H, reply_markup=_kb_sv_confirm_no_banner())


async def cb_sv_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start single-video generation."""
    q = update.callback_query
    await q.answer("🎬 Запускаю…")
    user_id = q.from_user.id

    animation = q.data.split(":")[1]  # "slide_left" | "slide_right" | "fade" | "none"
    src_id    = context.user_data.get(_SV_SRC)
    ban_id    = context.user_data.get(_SV_BAN)
    use_ai    = context.user_data.get(_SV_AI, False)

    if not src_id:
        await q.edit_message_text("❌ Выбор утерян — начни заново.", reply_markup=_kb_main())
        return

    clip = pick_random_unused_clip(user_id, src_id)
    if not clip:
        await q.edit_message_text(
            "😕 Все клипы уже использованы. Нарежи новые в 📱 Блог видео!",
            reply_markup=_kb_main(),
        )
        return

    banner = get_file(ban_id, user_id) if ban_id else None
    _sv_clear(context.user_data)
    work_dir = _make_work_dir(user_id)

    await q.edit_message_text(
        f"📱 <b>Генерирую клип…</b>\n\n"
        f"▸ Клип:   <code>{_e(clip['name'])}</code>\n"
        f"▸ Баннер: <code>{_e(banner['name']) if banner else 'Нет'}</code>\n"
        f"▸ AI:     <code>{'Да' if use_ai else 'Нет'}</code>",
        parse_mode=H,
    )

    _create_task(_run_single_pipeline(
        chat_id=q.message.chat_id,
        bot=q.get_bot(),
        clip_id=clip["id"],
        clip_path=clip["file_path"],
        banner_path=banner["file_path"] if banner else None,
        work_dir=work_dir,
        use_ai=use_ai,
        banner_animation=animation if animation != "none" else "fade",
    ))


async def _run_single_pipeline(
    *, chat_id: int, bot,
    clip_id: int, clip_path: str,
    banner_path: str | None,
    work_dir: Path,
    use_ai: bool = False,
    banner_animation: str = BANNER_ANIMATION,
) -> None:
    try:
        loop = asyncio.get_running_loop()

        await bot.send_message(chat_id, "🎙 Транскрибирую аудио (Whisper)…")
        ass = await loop.run_in_executor(
            None,
            lambda: generate_subtitles(
                clip_path, work_dir,
                margin_v=SINGLE_SUBTITLE_MARGIN_V,
            ),
        )

        await bot.send_message(chat_id, "🎬 Рендерю видео (FFmpeg)…")
        out: Path = await loop.run_in_executor(
            None,
            lambda: compose_single(
                video=clip_path,
                subtitle_file=ass,
                output_path=work_dir / "output.mp4",
                banner_image=banner_path,
                banner_animation=banner_animation,
            ),
        )

        mark_used(clip_id)

        await bot.send_message(chat_id, "✅ Готово! Отправляю видео…")
        size_mb = out.stat().st_size / 1024 / 1024

        if size_mb > 45:
            await bot.send_message(chat_id, f"⚙️ Файл {size_mb:.0f} МБ — сжимаю…")
            compressed = out.with_stem(out.stem + "_compressed")
            subprocess.run([
                "ffmpeg", "-y", "-i", str(out),
                "-c:v", "libx264", "-crf", "32", "-preset", "fast",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(compressed),
            ], capture_output=True)
            if compressed.exists() and compressed.stat().st_size < out.stat().st_size:
                out = compressed
                size_mb = out.stat().st_size / 1024 / 1024

        with open(out, "rb") as f:
            if size_mb <= 50:
                await bot.send_video(chat_id, video=f,
                    caption="📱 Клип готов!", supports_streaming=True)
            else:
                await bot.send_document(chat_id, document=f,
                    caption=f"📱 Клип готов ({size_mb:.0f} МБ — как файл).")

        await bot.send_message(chat_id, "Что дальше?", reply_markup=_kb_main())

    except Exception as exc:
        logger.error("Single pipeline failed:\n%s", traceback.format_exc())
        await bot.send_message(
            chat_id,
            f"❌ Ошибка:\n<code>{_e(str(exc))}</code>\n\nНапиши /start заново.",
            parse_mode=H,
            reply_markup=_kb_main(),
        )


# ─── Catch-all callback (runs last — catches unmatched callbacks) ─────────────

async def _cb_catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    logger.warning("[CALLBACK] No handler matched! data=%r", q.data if q else None)
    if q:
        await q.answer("⚠️ Неизвестная команда")


# ─── Error handler ────────────────────────────────────────────────────────────

async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:\n%s",
        "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__)))


# ─── App builder ──────────────────────────────────────────────────────────────

def build_bot(notify_chat_id: int | None = None) -> Application:
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Group -1: log every incoming update BEFORE any handler touches it
    app.add_handler(TypeHandler(Update, _log_update), group=-1)

    # Commands
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Inline-keyboard callbacks (specific patterns first)
    app.add_handler(CallbackQueryHandler(cb_menu_main,        pattern=r"^menu:main$"))
    app.add_handler(CallbackQueryHandler(cb_menu_library,     pattern=r"^menu:library$"))
    app.add_handler(CallbackQueryHandler(cb_menu_create,      pattern=r"^menu:create$"))
    app.add_handler(CallbackQueryHandler(cb_lib_category,     pattern=r"^lib:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(cb_lib_del_src,      pattern=r"^lib_del_src:\d+:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(cb_lib_clips,        pattern=r"^lib_clips:\d+:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(cb_lib_del_clip,     pattern=r"^lib_del_clip:\d+:\d+:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(cb_lib_upload_start, pattern=r"^lib_upload:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(cb_lib_cut,          pattern=r"^lib_cut:\d+:[a-z_]+$"))

    # Generation wizard (split-screen)
    app.add_handler(CallbackQueryHandler(cb_gen_top,  pattern=r"^gen_top:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_gen_bot,  pattern=r"^gen_bot:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_gen_ban,  pattern=r"^gen_ban:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_gen_run,  pattern=r"^gen_run:(slide_left|slide_right|fade)$"))
    app.add_handler(CallbackQueryHandler(cb_gen_step, pattern=r"^gen_step:(top|bottom|banner)$"))
    app.add_handler(CallbackQueryHandler(cb_noop,     pattern=r"^noop$"))

    # Single-video wizard
    app.add_handler(CallbackQueryHandler(cb_menu_single, pattern=r"^menu:single$"))
    app.add_handler(CallbackQueryHandler(cb_sv_src,      pattern=r"^sv_src:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sv_ai,       pattern=r"^sv_ai:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(cb_sv_ban,      pattern=r"^sv_ban:(\d+|none)$"))
    app.add_handler(CallbackQueryHandler(cb_sv_run,      pattern=r"^sv_run:(slide_left|slide_right|fade|none)$"))

    # Catch-all callback — fires if nothing above matched (diagnostic)
    app.add_handler(CallbackQueryHandler(_cb_catch_all))

    # Single message handler — dispatches by user_data state
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

    app.add_error_handler(_on_error)

    # ── Auto-upload cron (optional, requires YOUTUBE_CLIENT_SECRET in .env) ──
    from content_factory.scheduler.auto_generate import register as _register_cron
    _register_cron(app, notify_chat_id=notify_chat_id)

    return app


def run_bot(notify_chat_id: int | None = None) -> None:
    import os
    logger.info("Starting Content Factory bot… (PID=%s)", os.getpid())
    app = build_bot(notify_chat_id=notify_chat_id)
    app.run_polling(
        drop_pending_updates=False,  # keep callbacks pressed during restart
        allowed_updates=Update.ALL_TYPES,  # explicitly receive ALL update types
    )
