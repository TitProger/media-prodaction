# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Content Factory is an automated short-form video pipeline (YouTube Shorts / TikTok Reels). It stacks two input videos into a 1080×1920 split-screen frame, burns auto-generated subtitles (via OpenAI Whisper), and composites a banner overlay with fade effects — all via FFmpeg. Three interfaces share the same core pipeline: a Gradio web UI, a Telegram bot, and a CLI.

## Setup

```bash
pip install -e ".[dev]"   # installs all runtime + dev deps (black, ruff, pytest)
```

FFmpeg must be installed and available on `PATH` separately.

## Running

```bash
# Web UI (default) — opens http://127.0.0.1:7860
python main.py
python main.py ui --host 0.0.0.0 --port 8000 --share

# Telegram bot
python main.py bot

# CLI (headless)
python main.py compose --top top.mp4 --bottom bottom.mp4 --banner banner.png --output result.mp4
```

## Development Commands

```bash
black src/ tests/          # format
ruff check src/ tests/     # lint
pytest                     # run all tests
pytest tests/test_foo.py::test_bar  # single test
```

## Architecture

```
main.py                            ← CLI dispatcher (routes to ui / bot / compose)
src/content_factory/
  config/settings.py               ← ALL tunable parameters (video, subtitle, banner, paths)
  core/
    subtitle_generator.py          ← Whisper transcription → .ass subtitle file
    video_composer.py              ← FFmpeg filter_complex graph builder + encoder
  ui/app.py                        ← Gradio interface (calls core directly)
  bot/bot.py                       ← python-telegram-bot conversational flow (async)
output/                            ← per-job working directories created at runtime
```

### Core Pipeline (shared by all interfaces)

1. `subtitle_generator.generate_subtitles(video_path, output_dir)` — loads a Whisper model, transcribes audio, emits a `.ass` file with all caps, ≤4 words/cue, positioned at the split line.
2. `video_composer.compose(...)` — builds one `filter_complex` string that scales/crops both inputs to 1080×960, vstacks them, burns subtitles, loops or trims the banner, applies fade-in/out, and overlays it — then encodes with libx264 CRF 18 + AAC.

### Output Directory Layout

Each job gets its own subdirectory:
- Web UI: `output/{8-char-uuid}/`
- Telegram: `output/tg_{user_id}_{uuid}/`
- CLI: `output/cli_job/`

Each job directory contains `*.ass` (subtitles) and `output.mp4` (final video).

## Key Configuration

Everything is in `src/content_factory/config/settings.py`. Frequently tuned values:

| Setting | Default | Notes |
|---------|---------|-------|
| `WHISPER_MODEL` | `base` | `tiny`/`base`/`small`/`medium`/`large` |
| `WHISPER_LANGUAGE` | `None` | `None` = auto-detect |
| `OUTPUT_CRF` | `18` | Lower = higher quality, larger file |
| `OUTPUT_PRESET` | `fast` | `fast`/`medium`/`slow` |
| `SUBTITLE_MAX_WORDS` | `4` | Words per subtitle cue |
| `BANNER_APPEAR_AT_SEC` | `3.0` | When banner fades in |
| `BANNER_DURATION_SEC` | `5.0` | How long banner stays visible |
| `TELEGRAM_BOT_TOKEN` | `""` | Set before running bot mode |

## FFmpeg Filter Graph

`video_composer._fc()` builds the entire filter_complex as a string. When debugging encoding errors, print the filter string before passing it to ffmpeg. Windows paths must use forward slashes and escape colons (`C:/path` → `C\:/path`) inside filter strings — this is handled in `_escape_path()`.

## Banner Input Types

The composer auto-detects whether the banner is a static image or a video file. Static images are looped for the banner duration; video banners have their PTS offset so they start playing at `BANNER_APPEAR_AT_SEC`.
