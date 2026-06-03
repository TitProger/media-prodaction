"""
Global settings and template configuration for content-factory.
All tuneable parameters live here — never hardcode values in core modules.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parents[3]  # project root
ASSETS_DIR = ROOT_DIR / "assets"
TEMPLATES_DIR = ASSETS_DIR / "templates"
OUTPUT_DIR = ROOT_DIR / "output"

# ---------------------------------------------------------------------------
# Output video format  (9:16 vertical — Shorts / Reels standard)
# ---------------------------------------------------------------------------
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
OUTPUT_FPS = 30
OUTPUT_CRF = 26          # quality: lower = better (23-28 is good for Shorts/Reels)
OUTPUT_PRESET = "fast"   # ffmpeg preset: ultrafast/fast/medium/slow

# ---------------------------------------------------------------------------
# Split-screen layout
# ---------------------------------------------------------------------------
HALF_HEIGHT = OUTPUT_HEIGHT // 2   # 960 px each panel

# ---------------------------------------------------------------------------
# Subtitle style  (ASS format values)
# Reels/Shorts style: large bold white text with thick black outline,
# centred at the split line between the two video panels.
# ---------------------------------------------------------------------------
SUBTITLE_FONT_NAME = "Arial"
SUBTITLE_FONT_SIZE = 90                 # large — visible on phone screens
SUBTITLE_PRIMARY_COLOR = "&H0050F5FF"    # yellow (word being spoken — karaoke fill)
SUBTITLE_SECONDARY_COLOR = "&H00FFFFFF" # white (upcoming words)
SUBTITLE_OUTLINE_COLOR = "&H00000000"   # black outline
SUBTITLE_BACK_COLOR = "&H00000000"      # no background box
SUBTITLE_BOLD = 1
SUBTITLE_OUTLINE = 6                    # thick outline for readability
SUBTITLE_SHADOW = 0
SUBTITLE_ALIGNMENT = 2                  # ASS: bottom-centre (we use MarginV to push to centre)
SUBTITLE_MARGIN_V = OUTPUT_HEIGHT // 2 - 60   # sits right at the split line
# Max words per subtitle line (shorter = punchier)
SUBTITLE_MAX_WORDS = 4

# Whisper model size: tiny | base | small | medium | large
WHISPER_MODEL = "base"
WHISPER_LANGUAGE = None  # None = auto-detect

# ---------------------------------------------------------------------------
# Storage — media library (separate from per-job output/)
# ---------------------------------------------------------------------------
STORAGE_DIR = ROOT_DIR / "storage"
LIBRARY_DB = STORAGE_DIR / "library.db"

# ---------------------------------------------------------------------------
# REST API (FastAPI upload server for large files)
# ---------------------------------------------------------------------------
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "changeme-set-in-env")
API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", "8001"))

# ---------------------------------------------------------------------------
# AI provider for clip-finding  (Gemini by default, Claude as fallback)
# ---------------------------------------------------------------------------
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL    = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-lite")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL    = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# ---------------------------------------------------------------------------
# Clip-finding parameters  (top_video — AI-powered)
# ---------------------------------------------------------------------------
CLIP_COUNT        = int(os.environ.get("CLIP_COUNT", "10"))   # target clips per video
CLIP_MIN_DURATION = float(os.environ.get("CLIP_MIN_DURATION", "15"))  # seconds
CLIP_MAX_DURATION = float(os.environ.get("CLIP_MAX_DURATION", "60"))  # seconds

# ---------------------------------------------------------------------------
# Bottom-video split  (bottom_video — simple time-based)
# ---------------------------------------------------------------------------
BOTTOM_CLIP_DURATION = float(os.environ.get("BOTTOM_CLIP_DURATION", "60"))  # seconds per chunk

# ---------------------------------------------------------------------------
# Telegram bot
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ---------------------------------------------------------------------------
# Web UI — single fixed user (no auth)
# ---------------------------------------------------------------------------
WEB_USER_ID = int(os.environ.get("WEB_USER_ID", "1"))

# ---------------------------------------------------------------------------
# YouTube auto-upload (cron)
# ---------------------------------------------------------------------------
# Path to client_secret_*.json downloaded from Google Cloud Console.
# Leave empty to disable YouTube auto-upload.
YOUTUBE_CLIENT_SECRET  = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
# Where to cache the OAuth token after first login (never commit this file!)
YOUTUBE_TOKEN_FILE     = os.environ.get(
    "YOUTUBE_TOKEN_FILE",
    str(STORAGE_DIR / "youtube_token.json"),
)
# How often to auto-generate+upload a Short (hours). 0 = disabled.
YOUTUBE_CRON_INTERVAL_HOURS = float(os.environ.get("YOUTUBE_CRON_INTERVAL_HOURS", "6"))
# Privacy of uploaded videos: private | unlisted | public
YOUTUBE_PRIVACY_STATUS = os.environ.get("YOUTUBE_PRIVACY_STATUS", "private")
# Comma-separated tags added to every upload
YOUTUBE_TAGS           = os.environ.get("YOUTUBE_TAGS", "shorts")
# Description added to every upload (can be multiline via \n in .env)
YOUTUBE_DESCRIPTION    = os.environ.get("YOUTUBE_DESCRIPTION", "#Shorts")

# ---------------------------------------------------------------------------
# Banner overlay
# ---------------------------------------------------------------------------
BANNER_APPEAR_AT_SEC  = 3.0    # when the banner first appears
BANNER_DURATION_SEC   = 300.0  # fade mode: how long banner stays (300s >> any short)
BANNER_FADE_SEC       = 0.4    # slide / fade transition duration (seconds)
BANNER_MARGIN_TOP     = OUTPUT_HEIGHT // 2 + 40   # 1000 px — just below subtitle split line
BANNER_MARGIN_LEFT    = 20    # px from the left (and right) edge

# Animation mode: "slide_left" | "slide_right" | "fade"
#   slide_left  — enters from left,  exits to right, loops forever
#   slide_right — enters from right, exits to left,  loops forever
#   fade        — classic fade-in/fade-out (disappears after BANNER_DURATION_SEC)
BANNER_ANIMATION      = os.environ.get("BANNER_ANIMATION", "slide_left")

# How many seconds one full slide cycle lasts (in + stay + out)
BANNER_LOOP_INTERVAL  = float(os.environ.get("BANNER_LOOP_INTERVAL", "7.0"))
