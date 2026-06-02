"""
server.py — FastAPI upload server for Content Factory.

Run via:  python main.py api
Swagger:  http://0.0.0.0:8001/docs

Authentication: X-API-Key header (set API_SECRET_KEY in .env).

category values: top_video | bottom_video | banner_image | banner_video
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path as FilePath
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Path, UploadFile
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field

from content_factory.config.settings import API_SECRET_KEY
from content_factory.db.library import (
    CATEGORIES,
    VIDEO_CATEGORIES,
    add_file,
    delete_file,
    delete_source_cascade,
    get_file,
    get_storage_path,
    init_db,
    list_files,
)

CategoryLiteral = Literal["top_video", "bottom_video", "banner_image", "banner_video"]

# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Content Factory API",
    description="""
Media library management API for the Content Factory short-form video pipeline.

## Authentication
Every request must include the header:
```
X-API-Key: <your API_SECRET_KEY>
```
Set `API_SECRET_KEY` in your `.env` file (default: `changeme-set-in-env`).

## Categories
| Value | Description |
|-------|-------------|
| `top_video` | Top-panel video (speech / main content) |
| `bottom_video` | Bottom-panel video (gameplay / background) |
| `banner_image` | Static banner overlay (PNG / JPG) |
| `banner_video` | Animated banner overlay (MP4 / MOV) |

## Workflow
1. Upload files via **POST /api/upload/{category}** (or through the Telegram bot).
2. Browse your library via **GET /api/library/{user_id}**.
3. Delete unwanted files via **DELETE /api/library/{user_id}/{file_id}**.
""",
    version="1.0.0",
    contact={"name": "Content Factory"},
)


# ─── Pydantic schemas ────────────────────────────────────────────────────────

class MediaFileOut(BaseModel):
    id: int = Field(..., examples=[42])
    user_id: int = Field(..., examples=[123456789])
    name: str = Field(..., examples=["Minecraft parkour night"])
    category: str = Field(..., examples=["bottom_video"])
    subtype: str = Field("source", examples=["source", "clip"])
    parent_id: int | None = Field(None, examples=[None, 7])
    filename: str = Field(..., examples=["a1b2c3d4e5f6.mp4"])
    file_path: str = Field(..., examples=["/app/storage/library/123456789/bottom_videos/sources/a1b2c3.mp4"])
    size_bytes: int = Field(..., examples=[52428800])
    created_at: str = Field(..., examples=["2026-06-01T14:30:00+00:00"])

    model_config = {"from_attributes": True}


class UploadOut(BaseModel):
    id: int = Field(..., examples=[42])
    user_id: int = Field(..., examples=[123456789])
    name: str = Field(..., examples=["Minecraft parkour night"])
    category: str = Field(..., examples=["bottom_video"])
    filename: str = Field(..., examples=["a1b2c3d4e5f6.mp4"])
    size_bytes: int = Field(..., examples=[52428800])


class DeleteOut(BaseModel):
    deleted: bool = Field(..., examples=[True])
    file_id: int = Field(..., examples=[42])


class ErrorOut(BaseModel):
    detail: str = Field(..., examples=["Invalid or missing X-API-Key header"])


# ─── Auth ────────────────────────────────────────────────────────────────────

def _auth(x_api_key: Annotated[str, Header(description="Your API secret key")] = "") -> None:
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


Auth = Annotated[None, Depends(_auth)]

# ─── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    init_db()


# ─── Upload ──────────────────────────────────────────────────────────────────

@app.post(
    "/api/upload/{category}",
    response_model=UploadOut,
    summary="Upload a media file",
    description=(
        "Upload a video or image file to the library under the specified category.\n\n"
        "Use this endpoint to bypass Telegram's 50 MB upload limit — "
        "send large files directly from Postman or curl, then reference them in the bot."
    ),
    tags=["Library"],
    responses={
        200: {"description": "File uploaded successfully", "model": UploadOut},
        400: {"description": "Invalid category", "model": ErrorOut},
        401: {"description": "Missing or invalid API key", "model": ErrorOut},
    },
)
async def upload_file(
    category: CategoryLiteral,
    _: Auth,
    user_id: Annotated[int, Form(description="Telegram user_id of the file owner", examples=[123456789])],
    name: Annotated[str, Form(description="Human-readable label (max 120 chars)", examples=["Minecraft parkour night"])],
    file: Annotated[UploadFile, File(description="The media file to upload")],
) -> UploadOut:
    storage_dir = get_storage_path(user_id, category, subtype="source")
    suffix = FilePath(file.filename or "").suffix or ".mp4"
    dest = storage_dir / f"{uuid.uuid4().hex}{suffix}"

    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)

    file_id = add_file(user_id, name.strip()[:120], category, dest, subtype="source")
    return UploadOut(
        id=file_id,
        user_id=user_id,
        name=name,
        category=category,
        filename=dest.name,
        size_bytes=dest.stat().st_size,
    )


# ─── List all ────────────────────────────────────────────────────────────────

@app.get(
    "/api/library/{user_id}",
    response_model=list[MediaFileOut],
    summary="List all files for a user",
    description="Returns every file in the library for the given user, sorted newest-first.",
    tags=["Library"],
    responses={
        200: {"description": "File list", "model": list[MediaFileOut]},
        401: {"description": "Missing or invalid API key", "model": ErrorOut},
    },
)
async def list_all(
    user_id: Annotated[int, Path(description="Telegram user_id", examples=[123456789])],  # type: ignore[valid-type]
    _: Auth,
) -> list[dict]:
    rows: list[dict] = []
    for cat in CATEGORIES:
        rows.extend(dict(r) for r in list_files(user_id, cat))
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)


# ─── List by category ────────────────────────────────────────────────────────

@app.get(
    "/api/library/{user_id}/{category}",
    response_model=list[MediaFileOut],
    summary="List files in one category",
    description="Returns files for the given user filtered by category, sorted newest-first.",
    tags=["Library"],
    responses={
        200: {"description": "File list", "model": list[MediaFileOut]},
        400: {"description": "Invalid category", "model": ErrorOut},
        401: {"description": "Missing or invalid API key", "model": ErrorOut},
    },
)
async def list_category(
    user_id: Annotated[int, Path(description="Telegram user_id", examples=[123456789])],  # type: ignore[valid-type]
    category: CategoryLiteral,
    _: Auth,
) -> list[dict]:
    return [dict(r) for r in list_files(user_id, category)]


# ─── Delete ──────────────────────────────────────────────────────────────────

@app.delete(
    "/api/library/{user_id}/{file_id}",
    response_model=DeleteOut,
    summary="Delete a file from the library",
    description=(
        "Permanently removes the file from disk and the database. "
        "Returns 404 if the file does not exist or does not belong to the specified user."
    ),
    tags=["Library"],
    responses={
        200: {"description": "File deleted", "model": DeleteOut},
        401: {"description": "Missing or invalid API key", "model": ErrorOut},
        404: {"description": "File not found", "model": ErrorOut},
    },
)
async def remove_file(
    user_id: Annotated[int, Path(description="Telegram user_id", examples=[123456789])],  # type: ignore[valid-type]
    file_id: Annotated[int, Path(description="File ID from the library", examples=[42])],  # type: ignore[valid-type]
    _: Auth,
) -> DeleteOut:
    row = get_file(file_id, user_id)
    if row is None:
        raise HTTPException(404, "File not found or does not belong to this user")
    # For source video files — cascade-delete all clips too
    if row["category"] in VIDEO_CATEGORIES and row["subtype"] == "source":
        delete_source_cascade(file_id, user_id)
    else:
        delete_file(file_id, user_id)
    return DeleteOut(deleted=True, file_id=file_id)


# ─── Custom OpenAPI schema (adds securitySchemes) ────────────────────────────

def _custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        contact=app.contact,
        routes=app.routes,
    )
    schema.setdefault("components", {})
    schema["components"]["securitySchemes"] = {
        "ApiKeyHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Set API_SECRET_KEY in .env, then paste it here.",
        }
    }
    schema["security"] = [{"ApiKeyHeader": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]
