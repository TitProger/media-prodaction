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
SUBTITLE_PRIMARY_COLOR = "&H0050F5FF"   # slightly warm yellow-white (AABBGGRR: R=FF, G=F5, B=50)
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
