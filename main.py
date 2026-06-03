"""
main.py — entry point for Content Factory.

Usage
-----
    python main.py              # launch Gradio web UI (default)
    python main.py bot          # launch Telegram bot
    python main.py compose ...  # run pipeline from CLI
    python main.py --help       # show all options
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

load_dotenv()  # reads .env if present, no-op otherwise

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

# Убираем спам getUpdates из httpx — оставляем только полезные запросы
class _SuppressGetUpdates(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "getUpdates" not in record.getMessage()

logging.getLogger("httpx").addFilter(_SuppressGetUpdates())


def _launch_ui(host: str, port: int, share: bool) -> None:
    from content_factory.ui.app import build_ui

    demo = build_ui()
    demo.launch(server_name=host, server_port=port, share=share)


def _launch_bot(notify_chat_id: int | None = None) -> None:
    from content_factory.bot.bot import run_bot

    run_bot(notify_chat_id=notify_chat_id)


def _auth_youtube() -> None:
    """One-time OAuth browser flow to authorise YouTube uploads."""
    from content_factory.config.settings import YOUTUBE_CLIENT_SECRET, YOUTUBE_TOKEN_FILE
    from content_factory.core.youtube_uploader import authenticate

    if not YOUTUBE_CLIENT_SECRET:
        print(
            "❌  YOUTUBE_CLIENT_SECRET is not set in .env\n"
            "    Download client_secret_*.json from Google Cloud Console\n"
            "    and set YOUTUBE_CLIENT_SECRET=/path/to/that/file.json"
        )
        return
    print(f"🌐  Opening browser for Google OAuth…\n    Token will be saved to: {YOUTUBE_TOKEN_FILE}")
    authenticate(YOUTUBE_CLIENT_SECRET, YOUTUBE_TOKEN_FILE)
    print("✅  YouTube authorisation complete!")


def _launch_api(host: str, port: int) -> None:
    import uvicorn
    from content_factory.api.server import app

    uvicorn.run(app, host=host, port=port)


def _launch_all(api_host: str, api_port: int, notify_chat_id: int | None = None) -> None:
    """Run FastAPI (background thread) + Telegram bot (main thread) together."""
    import threading
    import uvicorn
    from content_factory.api.server import app as api_app
    from content_factory.bot.bot import run_bot

    api_thread = threading.Thread(
        target=lambda: uvicorn.run(api_app, host=api_host, port=api_port),
        daemon=True,
        name="api-server",
    )
    api_thread.start()
    logging.info("API server → http://%s:%s  |  Swagger → http://%s:%s/docs", api_host, api_port, api_host, api_port)

    run_bot(notify_chat_id=notify_chat_id)  # blocks until CTRL+C; daemon thread dies with it


def _cli_compose(args: argparse.Namespace) -> None:
    """Run the pipeline from the command line without a UI."""
    from content_factory.core.subtitle_generator import generate_subtitles
    from content_factory.core.video_composer import compose
    from content_factory.config.settings import OUTPUT_DIR

    work_dir = OUTPUT_DIR / "cli_job"
    ass_file = generate_subtitles(args.top, work_dir)
    compose(
        top_video=args.top,
        bottom_video=args.bottom,
        banner_image=args.banner,
        subtitle_file=ass_file,
        output_path=args.output or work_dir / "output.mp4",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="content-factory",
        description="Automated short-form video factory for Shorts & Reels",
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- UI sub-command (default) ---
    ui_parser = subparsers.add_parser("ui", help="Launch Gradio web UI")
    ui_parser.add_argument("--host", default="127.0.0.1")
    ui_parser.add_argument("--port", type=int, default=7860)
    ui_parser.add_argument("--share", action="store_true", help="Create public Gradio link")

    # --- Bot sub-command ---
    subparsers.add_parser("bot", help="Launch Telegram bot")

    # --- API sub-command ---
    api_parser = subparsers.add_parser("api", help="Launch FastAPI upload server only (port 8001)")
    api_parser.add_argument("--host", default="0.0.0.0")
    api_parser.add_argument("--port", type=int, default=8001)

    # --- All-in-one sub-command (default when no sub-command given for bot mode) ---
    all_parser = subparsers.add_parser("start", help="Launch Telegram bot + FastAPI server together")
    all_parser.add_argument("--api-host", default="0.0.0.0")
    all_parser.add_argument("--api-port", type=int, default=8001)
    all_parser.add_argument(
        "--notify-chat-id", type=int, default=None,
        help="Telegram chat_id to send cron job results to (optional)",
    )

    # --- YouTube auth sub-command ---
    subparsers.add_parser("auth-youtube", help="One-time OAuth login for YouTube auto-upload")

    # --- CLI sub-command ---
    cli_parser = subparsers.add_parser("compose", help="Run pipeline from CLI (no UI)")
    cli_parser.add_argument("--top", required=True, help="Top video path")
    cli_parser.add_argument("--bottom", required=True, help="Bottom video path")
    cli_parser.add_argument("--banner", required=True, help="Banner image path")
    cli_parser.add_argument("--output", help="Output video path (optional)")

    args = parser.parse_args()

    if args.command == "compose":
        _cli_compose(args)
    elif args.command == "bot":
        _launch_bot()
    elif args.command == "api":
        _launch_api(args.host, args.port)
    elif args.command == "auth-youtube":
        _auth_youtube()
    elif args.command == "start":
        _launch_all(args.api_host, args.api_port, notify_chat_id=args.notify_chat_id)
    else:
        # Default: launch UI (even if no sub-command given)
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 7860)
        share = getattr(args, "share", False)
        _launch_ui(host, port, share)


if __name__ == "__main__":
    main()
