"""bot.py — Content Factory Telegram bot (no inline buttons, auto-run after banner)."""
from __future__ import annotations
import asyncio, logging, uuid
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)
from content_factory.config.settings import (
    BANNER_APPEAR_AT_SEC, BANNER_DURATION_SEC, BANNER_FADE_SEC,
    OUTPUT_DIR, TELEGRAM_BOT_TOKEN, WHISPER_MODEL,
)
from content_factory.core.subtitle_generator import generate_subtitles
from content_factory.core.video_composer import compose

logger = logging.getLogger(__name__)
STEP_TOP, STEP_BOTTOM, STEP_BANNER = range(3)
_KEY_TOP="top_video"; _KEY_BOTTOM="bottom_video"; _KEY_BANNER="banner_image"
_KEY_WORKDIR="work_dir"

def _work_dir(uid):
    d = OUTPUT_DIR / f"tg_{uid}_{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    return d

async def _dl_video(update, context, key):
    msg = update.message
    wd = context.user_data[_KEY_WORKDIR]
    if msg.video:
        fo = await msg.video.get_file(); ext=".mp4"
    elif msg.document:
        fo = await msg.document.get_file()
        ext = Path(msg.document.file_name).suffix or ".mp4"
    else:
        await msg.reply_text("Пришли видео-файл."); return None
    dest = wd / f"{key}{ext}"
    await fo.download_to_drive(dest)
    return dest

async def _dl_banner(update, context):
    msg = update.message
    wd = context.user_data[_KEY_WORKDIR]
    if msg.photo:
        fo = await msg.photo[-1].get_file(); dest = wd/"banner.jpg"
    elif msg.video:
        fo = await msg.video.get_file(); dest = wd/"banner.mp4"
    elif msg.document:
        fo = await msg.document.get_file()
        ext = Path(msg.document.file_name).suffix or ".mp4"
        dest = wd/f"banner{ext}"
    else:
        await msg.reply_text("Пришли баннер (фото или видео)."); return None
    await fo.download_to_drive(dest)
    return dest

async def cmd_start(update, context):
    context.user_data.clear()
    context.user_data[_KEY_WORKDIR] = _work_dir(update.effective_user.id)
    await update.message.reply_text(
        "👋 Привет! Шаг 1/3 — отправь *верхнее видео* (с речью).",
        parse_mode=ParseMode.MARKDOWN)
    return STEP_TOP

async def cmd_cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. /start чтобы начать заново.")
    return ConversationHandler.END

async def recv_top(update, context):
    await update.message.reply_text("⏬ Скачиваю верхнее видео…")
    p = await _dl_video(update, context, _KEY_TOP)
    if p is None: return STEP_TOP
    context.user_data[_KEY_TOP] = p
    print(f"[BOT] top={p}", flush=True)
    await update.message.reply_text(
        "✅ Принято! Шаг 2/3 — *нижнее видео* (геймплей).",
        parse_mode=ParseMode.MARKDOWN)
    return STEP_BOTTOM

async def recv_bottom(update, context):
    await update.message.reply_text("⏬ Скачиваю нижнее видео…")
    p = await _dl_video(update, context, _KEY_BOTTOM)
    if p is None: return STEP_BOTTOM
    context.user_data[_KEY_BOTTOM] = p
    print(f"[BOT] bottom={p}", flush=True)
    await update.message.reply_text(
        "✅ Принято! Шаг 3/3 — *рекламный баннер* (PNG/JPG/MP4).",
        parse_mode=ParseMode.MARKDOWN)
    return STEP_BANNER

async def recv_banner(update, context):
    await update.message.reply_text("⏬ Скачиваю баннер…")
    p = await _dl_banner(update, context)
    if p is None: return STEP_BANNER
    context.user_data[_KEY_BANNER] = p
    print(f"[BOT] banner={p}", flush=True)
    await update.message.reply_text("✅ Все файлы получены! Запускаю обработку…")
    asyncio.create_task(_run_pipeline(update, context))
    return ConversationHandler.END

async def _run_pipeline(update, context):
    ud = context.user_data
    wd = ud[_KEY_WORKDIR]
    chat_id = update.message.chat_id
    bot = update.message.get_bot()
    try:
        loop = asyncio.get_running_loop()
        await bot.send_message(chat_id, "🎙 Транскрибирую аудио (Whisper)…")
        print(f"[PIPELINE] whisper start {ud[_KEY_TOP]}", flush=True)
        ass = await loop.run_in_executor(None, generate_subtitles, ud[_KEY_TOP], wd)
        print(f"[PIPELINE] whisper done {ass}", flush=True)

        await bot.send_message(chat_id, "🎬 Рендерю видео (FFmpeg)…")
        print("[PIPELINE] ffmpeg start", flush=True)
        _t=ud[_KEY_TOP]; _b=ud[_KEY_BOTTOM]; _bn=ud[_KEY_BANNER]; _o=wd/"output.mp4"
        out = await loop.run_in_executor(None, lambda: compose(
            top_video=_t, bottom_video=_b, banner_image=_bn, subtitle_file=ass,
            output_path=_o, banner_appear_at=BANNER_APPEAR_AT_SEC,
            banner_duration=BANNER_DURATION_SEC, banner_fade=BANNER_FADE_SEC))
        print(f"[PIPELINE] ffmpeg done {out}", flush=True)

        await bot.send_message(chat_id, "✅ Готово! Отправляю видео…")
        size_mb = out.stat().st_size / 1024 / 1024
        with open(out, "rb") as f:
            if size_mb <= 50:
                await bot.send_video(chat_id, video=f,
                    caption="🎬 Шортс готов! Напиши /start чтобы сделать новый.",
                    supports_streaming=True)
            else:
                await bot.send_document(chat_id, document=f,
                    caption=f"🎬 Шортс готов ({size_mb:.0f} МБ — отправлен как файл).\nНапиши /start чтобы сделать новый.")
    except Exception as exc:
        import traceback
        print(f"[PIPELINE] ERROR:\n{traceback.format_exc()}", flush=True)
        await bot.send_message(chat_id,
            f"❌ Ошибка:\n<code>{exc}</code>\n\nПопробуй /start заново.",
            parse_mode=ParseMode.HTML)

async def _err(update, context):
    import traceback
    print(f"[ERROR] {''.join(traceback.format_exception(type(context.error),context.error,context.error.__traceback__))}", flush=True)

def build_bot():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            STEP_TOP:    [MessageHandler(filters.VIDEO | filters.Document.VIDEO, recv_top)],
            STEP_BOTTOM: [MessageHandler(filters.VIDEO | filters.Document.VIDEO, recv_bottom)],
            STEP_BANNER: [MessageHandler(
                filters.PHOTO | filters.VIDEO | filters.Document.IMAGE | filters.Document.VIDEO,
                recv_banner)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_error_handler(_err)
    return app

def run_bot():
    logger.info("Starting Telegram bot…")
    build_bot().run_polling(drop_pending_updates=True)
