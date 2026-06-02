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
OUTPUT_CRF = 18          # quality: lower = better (18-23 is sane range)
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
# Claude API  (used for smart clip-finding in long videos)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

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
# Banner overlay
# ---------------------------------------------------------------------------
BANNER_APPEAR_AT_SEC = 3.0    # when the banner fades in
BANNER_DURATION_SEC = 5.0     # how long it stays visible
BANNER_FADE_SEC = 0.4         # fade-in / fade-out duration
BANNER_MARGIN_TOP = 60        # px from the top of the frame
BANNER_MARGIN_LEFT = 20       # px from the left edge
