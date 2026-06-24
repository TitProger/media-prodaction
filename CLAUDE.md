# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ Production server — read first

This same checkout also runs on the **production VPS** that serves live users at
**https://lk.passim-vpn.ru**. You can tell you are on prod when: the OS is **Linux**
and the working directory is **`/root/media-prodaction`** (locally it is Windows under
`D:\developer\PassimX\media-prodaction`). Pushing to `main` auto-deploys to this server,
so a bad commit ships to production within ~1–2 min.

When working **on the prod server**, be conservative — there is real user data here:

- **Never delete or reset the database.** `storage/library.db` holds the whole media
  library + cron config (`app_settings`). Do **not** `rm` it, `DROP`/`DELETE` rows, or
  re-init it. An hourly backup exists at `storage/library.db.backup` — never overwrite it
  with an empty/broken DB. Read with `SELECT` only unless explicitly asked to mutate.
- **`storage/` is sacred.** It contains user media (`storage/library/{user_id}/…`) **and
  secrets** that are NOT in git: `.env`, `youtube_token.json`, `client_secret.json`.
  Never delete the tree, and never print/echo secret contents into logs or chat.
- **No destructive git on the server.** Do not run `git clean -fdx` / `-fdX` (it wipes
  gitignored `storage/` and `.env`) or `git checkout -- storage`. `git reset --hard` only
  touches tracked files but still confirm first.
- **The YouTube token is live in prod too.** Any `python main.py …` that runs the real
  pipeline (cron tick, `auth-youtube`, the upload endpoint) actually publishes to the real
  channel. Don't trigger real uploads to "test" — mock or use guards.
- **`media-factory` systemd service serves live traffic.** Prefer reading logs
  (`journalctl -u media-factory -n 100`) over restarting. Only `systemctl restart
  media-factory` when a deploy/config change requires it.
- **`output/` is the only throwaway dir** — per-job working folders, safe to clean.
- When unsure whether an action is reversible on prod, **stop and ask** instead of guessing.

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

# FastAPI upload server — http://0.0.0.0:8001/docs
python main.py api
python main.py api --host 127.0.0.1 --port 9000

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
main.py                            ← CLI dispatcher (routes to ui / bot / api / compose)
src/content_factory/
  config/settings.py               ← ALL tunable parameters (video, subtitle, banner, paths, API key)
  core/
    subtitle_generator.py          ← Whisper transcription → .ass subtitle file
    video_composer.py              ← FFmpeg filter_complex graph builder + encoder
  db/
    library.py                     ← SQLite CRUD for the per-user media library
  api/
    server.py                      ← FastAPI upload/list/delete endpoints (port 8001)
  ui/app.py                        ← Gradio interface (calls core directly)
  bot/bot.py                       ← Telegram bot: inline-menu + library + create-shorts flow
output/                            ← per-job working directories created at runtime
storage/
  library.db                       ← SQLite metadata (names, paths, sizes)
  library/{user_id}/
    top_videos/                    ← верхние видео
    bottom_videos/                 ← нижние видео
    banners/images/                ← фото-баннеры
    banners/videos/                ← видео-баннеры
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
| `API_SECRET_KEY` | `"changeme-set-in-env"` | `X-API-Key` header for FastAPI |
| `API_PORT` | `8001` | Port for the upload API server |

## FFmpeg Filter Graph

`video_composer._fc()` builds the entire filter_complex as a string. When debugging encoding errors, print the filter string before passing it to ffmpeg. Windows paths must use forward slashes and escape colons (`C:/path` → `C\:/path`) inside filter strings — this is handled in `_escape_path()`.

## Banner Input Types

The composer auto-detects whether the banner is a static image or a video file. Static images are looped for the banner duration; video banners have their PTS offset so they start playing at `BANNER_APPEAR_AT_SEC`.
