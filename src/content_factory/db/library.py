"""library.py — SQLite CRUD for the per-user media library.

Folder layout
-------------
    storage/library/{user_id}/
        top_videos/
            sources/                 ← long source videos (split-screen top)
            clips/
                {source_stem}/       ← clips cut from that source
        bottom_videos/
            sources/
            clips/
                {source_stem}/
        blog_videos/
            sources/                 ← long source videos (single-video / blog mode)
            clips/
                {source_stem}/
        banners/
            images/
            videos/

DB schema
---------
    media_files(id, user_id, name, category, subtype, parent_id,
                filename, file_path, size_bytes, created_at)

    subtype  : "source" (long upload) | "clip" (AI-cut fragment)
    parent_id: NULL for sources; FK → media_files.id for clips
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from content_factory.config.settings import LIBRARY_DB, STORAGE_DIR

CATEGORIES = {"top_video", "bottom_video", "blog_video", "banner_image", "banner_video"}
VIDEO_CATEGORIES = {"top_video", "bottom_video", "blog_video"}

_VIDEO_DIR = {
    "top_video":    "top_videos",
    "bottom_video": "bottom_videos",
    "blog_video":   "blog_videos",
}


# ─── Connection ──────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    LIBRARY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(LIBRARY_DB))
    conn.row_factory = sqlite3.Row
    return conn


# ─── Schema ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media_files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                category    TEXT    NOT NULL,
                subtype     TEXT    NOT NULL DEFAULT 'source',
                parent_id   INTEGER DEFAULT NULL,
                filename    TEXT    NOT NULL,
                file_path   TEXT    NOT NULL,
                size_bytes  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_cat ON media_files(user_id, category)"
        )
        # Migrate first — adds subtype / parent_id / used to existing tables
        _migrate(conn)
        # Indexes on migrated columns come AFTER migration
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_parent ON media_files(parent_id)"
        )


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after initial schema deployment."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(media_files)")}
    if "subtype" not in existing:
        conn.execute(
            "ALTER TABLE media_files ADD COLUMN subtype TEXT NOT NULL DEFAULT 'source'"
        )
    if "parent_id" not in existing:
        conn.execute(
            "ALTER TABLE media_files ADD COLUMN parent_id INTEGER DEFAULT NULL"
        )
    if "used" not in existing:
        conn.execute(
            "ALTER TABLE media_files ADD COLUMN used INTEGER NOT NULL DEFAULT 0"
        )
    if "in_progress" not in existing:
        conn.execute(
            "ALTER TABLE media_files ADD COLUMN in_progress INTEGER NOT NULL DEFAULT 0"
        )


# ─── Path helpers ────────────────────────────────────────────────────────────

def get_storage_path(
    user_id: int,
    category: str,
    subtype: str = "source",
    source_stem: str | None = None,
) -> Path:
    """
    Return (and create) the storage directory for a file.

    Video categories
    ~~~~~~~~~~~~~~~~
    subtype="source"  →  .../top_videos/sources/
    subtype="clip"    →  .../top_videos/clips/{source_stem}/

    Banner categories
    ~~~~~~~~~~~~~~~~~
    banner_image  →  .../banners/images/
    banner_video  →  .../banners/videos/
    """
    base = STORAGE_DIR / "library" / str(user_id)
    if category in VIDEO_CATEGORIES:
        vdir = _VIDEO_DIR[category]
        if subtype == "clip" and source_stem:
            path = base / vdir / "clips" / source_stem
        else:
            path = base / vdir / "sources"
    elif category == "banner_image":
        path = base / "banners" / "images"
    elif category == "banner_video":
        path = base / "banners" / "videos"
    else:
        path = base / category
    path.mkdir(parents=True, exist_ok=True)
    return path


# ─── CRUD ────────────────────────────────────────────────────────────────────

def add_file(
    user_id: int,
    name: str,
    category: str,
    file_path: Path,
    subtype: str = "source",
    parent_id: int | None = None,
) -> int:
    """Insert a new file record. Returns the new row id."""
    size = file_path.stat().st_size if file_path.exists() else 0
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO media_files
               (user_id, name, category, subtype, parent_id,
                filename, file_path, size_bytes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                user_id, name, category, subtype, parent_id,
                file_path.name, str(file_path), size, now,
            ),
        )
        return cur.lastrowid


def list_files(user_id: int, category: str) -> list[sqlite3.Row]:
    """Return ALL files (sources + clips) for a category — used by API."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM media_files WHERE user_id=? AND category=? ORDER BY created_at DESC",
            (user_id, category),
        ).fetchall()


def list_sources(user_id: int, category: str) -> list[sqlite3.Row]:
    """
    Return source files for a video category, enriched with clip_count.

    Each row has all media_files columns plus ``clip_count`` (int).
    """
    with _connect() as conn:
        return conn.execute(
            """SELECT m.*,
                   (SELECT COUNT(*) FROM media_files c
                    WHERE c.parent_id = m.id) AS clip_count
               FROM media_files m
               WHERE m.user_id=? AND m.category=? AND m.subtype='source'
               ORDER BY m.created_at DESC""",
            (user_id, category),
        ).fetchall()


def list_clips(user_id: int, parent_id: int) -> list[sqlite3.Row]:
    """Return all clips belonging to a source file, sorted by start (name)."""
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM media_files
               WHERE user_id=? AND parent_id=?
               ORDER BY created_at ASC""",
            (user_id, parent_id),
        ).fetchall()


def get_file(file_id: int, user_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM media_files WHERE id=? AND user_id=?",
            (file_id, user_id),
        ).fetchone()


def delete_file(file_id: int, user_id: int) -> bool:
    """Remove a single DB row and its file from disk. Returns True if existed."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT file_path FROM media_files WHERE id=? AND user_id=?",
            (file_id, user_id),
        ).fetchone()
        if row is None:
            return False
        Path(row["file_path"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM media_files WHERE id=?", (file_id,))
        return True


def delete_source_cascade(file_id: int, user_id: int) -> bool:
    """
    Delete a source file AND all its clips (DB rows + disk files).
    Removes the clips subdirectory if it becomes empty.
    Returns True if the source row existed.
    """
    with _connect() as conn:
        source = conn.execute(
            "SELECT * FROM media_files WHERE id=? AND user_id=? AND subtype='source'",
            (file_id, user_id),
        ).fetchone()
        if source is None:
            return False

        # Delete clip files from disk + collect clip dir
        clips = conn.execute(
            "SELECT file_path FROM media_files WHERE parent_id=?",
            (file_id,),
        ).fetchall()

        clip_dir: Path | None = None
        for clip in clips:
            p = Path(clip["file_path"])
            p.unlink(missing_ok=True)
            if clip_dir is None:
                clip_dir = p.parent

        # Delete clip rows
        conn.execute("DELETE FROM media_files WHERE parent_id=?", (file_id,))

        # Remove the now-empty clips subfolder
        if clip_dir and clip_dir.exists():
            try:
                clip_dir.rmdir()
            except OSError:
                pass  # not empty — leave it

        # Delete source file from disk
        Path(source["file_path"]).unlink(missing_ok=True)

        # Delete source row
        conn.execute("DELETE FROM media_files WHERE id=?", (file_id,))
        return True


def mark_used(file_id: int) -> None:
    """Mark a clip as used — it will no longer appear in random picks."""
    with _connect() as conn:
        conn.execute("UPDATE media_files SET used=1 WHERE id=?", (file_id,))


def count_unused_clips(user_id: int, parent_id: int) -> int:
    """Count clips that are free (not used and not in_progress)."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM media_files
               WHERE user_id=? AND parent_id=? AND subtype='clip' AND used=0 AND in_progress=0""",
            (user_id, parent_id),
        ).fetchone()
        return row[0] if row else 0


def pick_random_unused_clip(user_id: int, parent_id: int) -> sqlite3.Row | None:
    """Return a random unused clip and atomically mark it in_progress to prevent races."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM media_files
               WHERE user_id=? AND parent_id=? AND subtype='clip' AND used=0 AND in_progress=0
               ORDER BY RANDOM() LIMIT 1""",
            (user_id, parent_id),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE media_files SET in_progress=1 WHERE id=?", (row["id"],)
            )
        return row


def release_clip(file_id: int) -> None:
    """Release in_progress lock without marking as used (call on pipeline failure)."""
    with _connect() as conn:
        conn.execute("UPDATE media_files SET in_progress=0 WHERE id=?", (file_id,))


def pick_random_clip(user_id: int, parent_id: int) -> sqlite3.Row | None:
    """Return any random clip for a source (ignores used flag — for bottom video)."""
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM media_files
               WHERE user_id=? AND parent_id=? AND subtype='clip'
               ORDER BY RANDOM() LIMIT 1""",
            (user_id, parent_id),
        ).fetchone()


def file_exists(file_id: int, user_id: int) -> bool:
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM media_files WHERE id=? AND user_id=?",
            (file_id, user_id),
        ).fetchone() is not None
