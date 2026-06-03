"""
web_routes.py — Web UI API endpoints (no auth, single WEB_USER_ID).

All paths are under /ui prefix.
  GET  /ui                         → SPA index.html
  GET  /ui/api/sources/{category}  → sources list with clip_count / unused_count
  GET  /ui/api/sources/{id}/clips  → clips for a source
  GET  /ui/api/banners             → all banners (images + videos)
  POST /ui/api/upload/{category}   → multipart upload
  DELETE /ui/api/sources/{id}      → cascade delete source
  DELETE /ui/api/clips/{id}        → delete single clip
  POST /ui/api/jobs/cut            → start AI/split cut job
  POST /ui/api/jobs/generate       → start generate-short job
  GET  /ui/api/jobs                → list all jobs
  GET  /ui/api/jobs/{id}           → single job status
  GET  /ui/api/download/{id}       → FileResponse for completed generate job
"""
from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from content_factory.api import jobs as job_store
from content_factory.config.settings import (
    BANNER_ANIMATION,
    BOTTOM_CLIP_DURATION,
    OUTPUT_DIR,
    WEB_USER_ID,
)
from content_factory.db.library import (
    add_file,
    count_unused_clips,
    delete_file,
    delete_source_cascade,
    get_file,
    get_storage_path,
    init_db,
    list_clips,
    list_files,
    list_sources,
    mark_used,
    pick_random_clip,
    pick_random_unused_clip,
)

router = APIRouter(prefix="/ui", include_in_schema=False)

_WEB_DIR = Path(__file__).parent.parent / "ui" / "web"


# ─── SPA ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def spa_root() -> FileResponse:
    index = _WEB_DIR / "index.html"
    if not index.exists():
        raise HTTPException(503, "Web UI not found")
    return FileResponse(str(index), media_type="text/html")


# ─── Library: sources ────────────────────────────────────────────────────────

@router.get("/api/sources/{category}")
async def api_sources(category: str) -> list[dict]:
    _valid_category(category)
    rows = list_sources(WEB_USER_ID, category)
    result = []
    for r in rows:
        d = dict(r)
        if category in ("top_video", "bottom_video"):
            d["unused_count"] = count_unused_clips(WEB_USER_ID, r["id"])
        result.append(d)
    return result


@router.get("/api/sources/{source_id}/clips")
async def api_clips(source_id: int) -> list[dict]:
    rows = list_clips(WEB_USER_ID, source_id)
    return [dict(r) for r in rows]


# ─── Library: banners ────────────────────────────────────────────────────────

@router.get("/api/banners")
async def api_banners() -> list[dict]:
    images = [dict(r) for r in list_files(WEB_USER_ID, "banner_image")]
    videos = [dict(r) for r in list_files(WEB_USER_ID, "banner_video")]
    return sorted(images + videos, key=lambda r: r["created_at"], reverse=True)


# ─── Upload ──────────────────────────────────────────────────────────────────

@router.post("/api/upload/{category}")
async def api_upload(
    category: str,
    name: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    _valid_category(category)
    init_db()

    storage_dir = get_storage_path(WEB_USER_ID, category, subtype="source")
    suffix = Path(file.filename or "file").suffix or ".mp4"
    dest = storage_dir / f"{uuid.uuid4().hex}{suffix}"

    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)

    file_id = add_file(WEB_USER_ID, name.strip()[:120], category, dest, subtype="source")
    return {
        "id": file_id,
        "name": name,
        "category": category,
        "filename": dest.name,
        "size_bytes": dest.stat().st_size,
    }


# ─── Delete ──────────────────────────────────────────────────────────────────

@router.delete("/api/sources/{source_id}")
async def api_delete_source(source_id: int) -> dict:
    row = get_file(source_id, WEB_USER_ID)
    if row is None:
        raise HTTPException(404, "Not found")
    if row["subtype"] != "source":
        raise HTTPException(400, "Not a source file")
    delete_source_cascade(source_id, WEB_USER_ID)
    return {"deleted": True, "id": source_id}


@router.delete("/api/clips/{clip_id}")
async def api_delete_clip(clip_id: int) -> dict:
    row = get_file(clip_id, WEB_USER_ID)
    if row is None:
        raise HTTPException(404, "Not found")
    delete_file(clip_id, WEB_USER_ID)
    return {"deleted": True, "id": clip_id}


@router.delete("/api/banners/{banner_id}")
async def api_delete_banner(banner_id: int) -> dict:
    row = get_file(banner_id, WEB_USER_ID)
    if row is None:
        raise HTTPException(404, "Not found")
    delete_file(banner_id, WEB_USER_ID)
    return {"deleted": True, "id": banner_id}


# ─── Jobs: cut ───────────────────────────────────────────────────────────────

@router.post("/api/jobs/cut")
async def api_job_cut(body: dict) -> dict:
    """
    body: { source_id: int, category: str }
    Starts cut pipeline in background.
    """
    source_id = int(body.get("source_id", 0))
    category = body.get("category", "")
    _valid_category(category)

    row = get_file(source_id, WEB_USER_ID)
    if row is None:
        raise HTTPException(404, "Source not found")

    job = job_store.create("cut")
    asyncio.create_task(_run_cut(job.id, row, category))
    return job_store.as_dict(job)


async def _run_cut(job_id: str, row, category: str) -> None:
    import asyncio as _aio
    loop = _aio.get_event_loop()

    job_store.update(job_id, state="running", message="Нарезка запущена…")
    try:
        source_path = Path(row["file_path"])
        source_stem = source_path.stem
        clips_dir = get_storage_path(
            WEB_USER_ID, category, subtype="clip", source_stem=source_stem
        )

        if category == "bottom_video":
            from content_factory.core.video_cutter import split_by_duration

            job_store.update(job_id, message="Нарезка по времени…")
            saved = await loop.run_in_executor(
                None,
                lambda: split_by_duration(
                    source_path, clips_dir,
                    chunk_sec=BOTTOM_CLIP_DURATION,
                    source_stem=source_stem,
                ),
            )
            for i, p in enumerate(saved, 1):
                add_file(
                    WEB_USER_ID,
                    f"Часть {i}",
                    category,
                    p,
                    subtype="clip",
                    parent_id=row["id"],
                )
        else:
            from content_factory.core.clip_finder import find_best_clips
            from content_factory.core.video_cutter import cut_clips

            job_store.update(job_id, message="Транскрипция Whisper…")
            clips_meta = await loop.run_in_executor(None, find_best_clips, source_path)

            job_store.update(job_id, message=f"Найдено {len(clips_meta)} клипов, нарезаю…")
            saved = await loop.run_in_executor(
                None,
                lambda: cut_clips(source_path, clips_meta, clips_dir, source_stem=source_stem),
            )
            for i, p in enumerate(saved):
                title = clips_meta[i]["title"] if i < len(clips_meta) else p.stem
                add_file(
                    WEB_USER_ID,
                    title,
                    category,
                    p,
                    subtype="clip",
                    parent_id=row["id"],
                )

        job_store.update(
            job_id,
            state="done",
            message=f"Готово: {len(saved)} клипов",
            result={"clip_count": len(saved), "source_id": row["id"]},
        )
    except Exception as exc:
        job_store.update(job_id, state="error", error=str(exc), message="Ошибка нарезки")


# ─── Jobs: generate ──────────────────────────────────────────────────────────

@router.post("/api/jobs/generate")
async def api_job_generate(body: dict) -> dict:
    """
    body: {
      top_source_id: int,
      bottom_source_id: int,
      banner_id: int,
      banner_animation: str  (optional)
    }
    """
    top_source_id = int(body.get("top_source_id", 0))
    bottom_source_id = int(body.get("bottom_source_id", 0))
    banner_id = int(body.get("banner_id", 0))
    animation = body.get("banner_animation", BANNER_ANIMATION)

    top_row = get_file(top_source_id, WEB_USER_ID)
    bot_row = get_file(bottom_source_id, WEB_USER_ID)
    ban_row = get_file(banner_id, WEB_USER_ID)

    if top_row is None:
        raise HTTPException(404, "Top source not found")
    if bot_row is None:
        raise HTTPException(404, "Bottom source not found")
    if ban_row is None:
        raise HTTPException(404, "Banner not found")

    # Pick random clips
    top_clip = pick_random_unused_clip(WEB_USER_ID, top_source_id)
    if top_clip is None:
        raise HTTPException(409, "No unused top clips left — cut more or reset")

    bot_clip = pick_random_clip(WEB_USER_ID, bottom_source_id)
    if bot_clip is None:
        raise HTTPException(409, "No bottom clips found — cut the source first")

    job = job_store.create("generate")
    asyncio.create_task(_run_generate(
        job_id=job.id,
        top_clip_id=top_clip["id"],
        top_path=Path(top_clip["file_path"]),
        bottom_path=Path(bot_clip["file_path"]),
        banner_path=Path(ban_row["file_path"]),
        banner_animation=animation,
    ))
    return job_store.as_dict(job)


async def _run_generate(
    *,
    job_id: str,
    top_clip_id: int,
    top_path: Path,
    bottom_path: Path,
    banner_path: Path,
    banner_animation: str,
) -> None:
    import asyncio as _aio
    loop = _aio.get_event_loop()

    job_store.update(job_id, state="running", message="Генерация шортса…")
    work_dir = OUTPUT_DIR / f"web_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        from content_factory.core.subtitle_generator import generate_subtitles
        from content_factory.core.video_composer import compose

        job_store.update(job_id, message="Генерация субтитров…")
        ass_path = await loop.run_in_executor(
            None, generate_subtitles, top_path, work_dir
        )

        job_store.update(job_id, message="Сборка видео FFmpeg…")
        output_path = work_dir / "output.mp4"
        await loop.run_in_executor(
            None,
            lambda: compose(
                top_path, bottom_path, banner_path, ass_path, output_path,
                banner_animation=banner_animation,
            ),
        )

        mark_used(top_clip_id)

        job_store.update(
            job_id,
            state="done",
            message="Шортс готов!",
            result={"output_path": str(output_path), "work_dir": str(work_dir)},
        )
    except Exception as exc:
        job_store.update(job_id, state="error", error=str(exc), message="Ошибка генерации")


# ─── Jobs: list / get ────────────────────────────────────────────────────────

@router.get("/api/jobs")
async def api_jobs_list() -> list[dict]:
    return [job_store.as_dict(j) for j in job_store.all_jobs()]


@router.get("/api/jobs/{job_id}")
async def api_job_get(job_id: str) -> dict:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job_store.as_dict(job)


# ─── Download ────────────────────────────────────────────────────────────────

@router.get("/api/download/{job_id}")
async def api_download(job_id: str) -> FileResponse:
    job = job_store.get(job_id)
    if job is None or job.state != "done" or job.result is None:
        raise HTTPException(404, "No downloadable result")
    path = Path(job.result.get("output_path", ""))
    if not path.exists():
        raise HTTPException(410, "File no longer available")
    return FileResponse(
        str(path),
        media_type="video/mp4",
        filename=f"short_{job_id}.mp4",
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

_VALID_CATEGORIES = {"top_video", "bottom_video", "banner_image", "banner_video"}


def _valid_category(cat: str) -> None:
    if cat not in _VALID_CATEGORIES:
        raise HTTPException(400, f"Invalid category '{cat}'")
