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


def _launch_ui(host: str, port: int, share: bool) -> None:
    from content_factory.ui.app import build_ui

    demo = build_ui()
    demo.launch(server_name=host, server_port=port, share=share)


def _launch_bot() -> None:
    from content_factory.bot.bot import run_bot

    run_bot()


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
    else:
        # Default: launch UI (even if no sub-command given)
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 7860)
        share = getattr(args, "share", False)
        _launch_ui(host, port, share)


if __name__ == "__main__":
    main()
