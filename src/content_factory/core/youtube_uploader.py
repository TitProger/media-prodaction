"""
youtube_uploader.py — Upload videos to YouTube via Data API v3.

First-time setup
----------------
1. Go to https://console.cloud.google.com/
2. Create a project → Enable "YouTube Data API v3"
3. Create OAuth 2.0 credentials (Desktop app) → Download client_secret_*.json
4. Set YOUTUBE_CLIENT_SECRET=/path/to/client_secret.json in .env
5. Run:  python main.py auth-youtube
   → opens browser, you log in once, token saved to YOUTUBE_TOKEN_FILE

After that everything works automatically.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _get_credentials(client_secret_path: str, token_path: str):
    """Load saved credentials or run the OAuth browser flow."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_file = Path(token_path)
    creds = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("[youtube] Refreshing access token…")
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, _SCOPES)
            creds = flow.run_local_server(port=0)
            logger.info("[youtube] OAuth completed — token saved to %s", token_path)

        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())

    return creds


def authenticate(client_secret_path: str, token_path: str) -> None:
    """Run the one-time OAuth browser flow and save the token."""
    _get_credentials(client_secret_path, token_path)
    logger.info("[youtube] Authentication successful ✅")


def is_authenticated(token_path: str) -> bool:
    """Return True if a valid (or refreshable) token exists."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_file = Path(token_path)
    if not token_file.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)
        if creds.valid:
            return True
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
            return True
    except Exception as exc:
        logger.warning("[youtube] Token check failed: %s", exc)
    return False


def upload_video(
    video_path: str | Path,
    title: str,
    *,
    description: str = "#Shorts",
    tags: list[str] | None = None,
    privacy: str = "private",
    category_id: str = "22",   # 22 = People & Blogs
    client_secret_path: str,
    token_path: str,
) -> str:
    """
    Upload *video_path* to YouTube.

    Returns the YouTube video ID (e.g. "dQw4w9WgXcQ").
    The video URL will be: https://youtube.com/shorts/{video_id}
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds   = _get_credentials(client_secret_path, token_path)
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title":       title[:100],
            "description": description,
            "tags":        tags or ["shorts"],
            "categoryId":  category_id,
        },
        "status": {
            "privacyStatus":          privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=4 * 1024 * 1024,  # 4 MB chunks
    )

    request  = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info("[youtube] Upload progress: %.0f%%", status.progress() * 100)

    video_id = response["id"]
    logger.info("[youtube] ✅ Uploaded → https://youtube.com/shorts/%s", video_id)
    return video_id
