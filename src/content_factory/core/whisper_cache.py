"""
whisper_cache.py — single shared Whisper model cache for the whole process.

Both subtitle_generator and clip_finder need a Whisper model. Without a shared
cache each module loads its own copy, so a blog cron run (clip_finder →
subtitle_generator) keeps **two** copies of the model in RAM at once — fatal on
a 2 GB VPS. This module is the one place a model is loaded and cached.
"""
from __future__ import annotations

import logging

import whisper

logger = logging.getLogger(__name__)

_cache: dict[str, object] = {}


def get_model(name: str):
    """Return a cached Whisper model, loading it once per process."""
    if name not in _cache:
        logger.info("Loading Whisper model '%s' (shared cache)…", name)
        _cache[name] = whisper.load_model(name)
    return _cache[name]
