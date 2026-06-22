"""
_sync.py — process-wide serialization of heavy media work.

On `python main.py start` the FastAPI server and the Telegram bot/cron run in
**different event loops** (uvicorn in its own thread, PTB in the main thread),
so an asyncio.Semaphore cannot coordinate them. The heavy work (Whisper
transcription, FFmpeg encoding) always runs in worker threads via
`run_in_executor`, so a plain threading lock acquired inside those blocking
functions serializes everything across the whole process — one heavy job at a
time. This is what keeps a 2 GB VPS from running two Whisper models at once and
hitting OOM.

Not reentrant: never call one HEAVY_LOCK-guarded function from inside another
while holding the lock (current call sites are all sequential, never nested).
"""
from __future__ import annotations

import threading

# One Whisper/FFmpeg-heavy operation at a time, process-wide.
HEAVY_LOCK = threading.Lock()
