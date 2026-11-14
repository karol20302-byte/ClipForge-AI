from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from threading import BoundedSemaphore
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Literal

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, HttpUrl
import yt_dlp

from app.highlight_detector import detect_highlights
from app.storage import get_last_activity, get_media, init_storage, load_state, put_media, reset_persistent_data, save_clip, save_job, touch_activity
DATA_DIR = Path(os.getenv("CLIPFORGE_DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "renders"
for folder in (UPLOAD_DIR, OUTPUT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = int(os.getenv("CLIPFORGE_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
API_KEY = os.getenv("CLIPFORGE_API_KEY", "").strip()

app = FastAPI(title="ClipForge AI API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in os.getenv(
        "CLIPFORGE_ALLOWED_ORIGINS",
        "https://clipforge-ai-db2.pages.dev",
    ).split(",") if item.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)

jobs: dict[str, dict] = {}
clip_candidates: dict[str, dict] = {}
_FFMPEG_LOCK = BoundedSemaphore(1)


def _run_ffmpeg(command: list[str], timeout: int, task: str):
    if not _FFMPEG_LOCK.acquire(blocking=False):
        raise HTTPException(503, "Another video task is using the renderer. Please retry in a moment.")
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    finally:
        _FFMPEG_LOCK.release()


def _detect_scene_cuts(source: Path, start: float, duration: float) -> list[float]:
    """Return scene-change timestamps relative to the requested clip."""
    if duration <= 1.0:
        return []
    command = [
        "ffmpeg", "-hide_banner", "-ss", str(start), "-i", str(source),
        "-t", str(duration), "-vf", "select='gt(scene,0.28)',metadata=print",
        "-an", "-f", "null", "-",
    ]
    try:
        result = _run_ffmpeg(command, timeout=45, task="scene analysis")
        stamps = [float(value) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr)]
        cuts: list[float] = []
        for stamp in stamps:
            if 0.35 < stamp < duration - 0.35 and (not cuts or stamp - cuts[-1] > 0.45):
                cuts.append(round(stamp, 3))
            if len(cuts) >= 8:
                break
        return cuts
    except Exception:
        return []


def _creator_punch_filter(dimensions: str, scene_cuts: list[float],
                          focus_x: float = 0.5, focus_y: float = 0.5) -> str:
    """Apply a noticeable Shorts-style crop zoom with quick scene-beat punches."""
    focus_x = max(0.1, min(0.9, float(focus_x)))
    focus_y = max(0.1, min(0.9, float(focus_y)))
    if scene_cuts:
        pulses = [
            f"if(between(t\\,{cut:.3f}\\,{cut + 0.55:.3f})\\,sin((t-{cut:.3f})*5.71)\\,0)"
            for cut in scene_cuts[:18]
        ]
        zoom = "1.075+min(0.105\\,0.075*(" + "+".join(pulses) + "))"
    else:
        zoom = "1.075"
    x = f"max(0\\,min(iw-ow\\,iw*{focus_x:.3f}-ow/2))"
    y = f"max(0\\,min(ih-oh\\,ih*{focus_y:.3f}-oh/2))"
    return f"crop=iw/({zoom}):ih/({zoom}):{x}:{y},scale={dimensions}"


@app.on_event("startup")
def restore_persistent_state():
    init_storage()
    restored_jobs, restored_clips = load_state()
    jobs.update(restored_jobs)
    clip_candidates.update(restored_clips)
    # Render instances can restart while a background URL import is running.
    # Mark interrupted tasks clearly instead of leaving the browser polling forever.
    for job in jobs.values():
        if job.get("status") in {"queued", "downloading"}:
            job.update(
                status="failed",
                error="The server restarted during this import. Please retry the URL.",
                detail="Import interrupted by server restart.",
            )
            try:
                save_job(job)
            except Exception:
                pass


def local_video_path(job: dict) -> Path:
    location = job["path"]
    if location.startswith("b2://"):
        suffix = Path(job.get("filename", "source.mp4")).suffix or ".mp4"

        # The upload endpoint keeps the original file on this Render instance
        # after sending a copy to B2. Reuse it before attempting a cloud download.
        original = UPLOAD_DIR / (job["id"] + suffix)
        if original.is_file() and original.stat().st_size > 0:
            return original

        # Reuse a previously downloaded copy for transcription, previews and renders.
        cached = UPLOAD_DIR / (job["id"] + "-cached" + suffix)
        if cached.is_file() and cached.stat().st_size > 0:
            return cached
        return get_media(location, cached)
    return Path(location)
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
ALLOWED_GOOGLE_EMAIL = os.getenv("CLIPFORGE_ALLOWED_EMAIL", "").strip().lower()
SESSION_SECRET = os.getenv("CLIPFORGE_SESSION_SECRET", "").strip() or API_KEY
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
_google_auth_request = GoogleAuthRequest()


def verify_google_identity(credential: str) -> dict | None:
    """Verify a Google Identity Services ID token and enforce the single-account allowlist."""
    if not GOOGLE_CLIENT_ID or not ALLOWED_GOOGLE_EMAIL or not credential:
        return None
    try:
        claims = google_id_token.verify_oauth2_token(
            credential, _google_auth_request, GOOGLE_CLIENT_ID
        )
        email = str(claims.get("email", "")).strip().lower()
        if claims.get("email_verified") is not True or not hmac.compare_digest(email, ALLOWED_GOOGLE_EMAIL):
            return None
        return {"email": email, "sub": str(claims.get("sub", ""))}
    except Exception:
        return None


def create_session_token(identity: dict) -> str:
    if not SESSION_SECRET:
        raise HTTPException(503, "Persistent login is not configured. Set CLIPFORGE_SESSION_SECRET in Render.")
    payload = {
        "email": identity["email"],
        "sub": identity["sub"],
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(SESSION_SECRET.encode(), encoded.encode(), "sha256").hexdigest()
    return encoded + "." + signature


def verify_session_token(token: str) -> dict | None:
    if not SESSION_SECRET or not token or "." not in token:
        return None
    encoded, supplied_signature = token.rsplit(".", 1)
    expected_signature = hmac.new(SESSION_SECRET.encode(), encoded.encode(), "sha256").hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if payload.get("exp", 0) <= int(time.time()):
            return None
        email = str(payload.get("email", "")).strip().lower()
        if not hmac.compare_digest(email, ALLOWED_GOOGLE_EMAIL):
            return None
        return {"email": email, "sub": str(payload.get("sub", ""))}
    except Exception:
        return None


@app.middleware("http")
async def protect_api_and_add_headers(request: Request, call_next):
    if request.method != "OPTIONS" and request.url.path.startswith("/api/") and request.url.path != "/api/health":
        supplied_key = request.headers.get("x-api-key", "")
        key_is_valid = bool(API_KEY and hmac.compare_digest(supplied_key, API_KEY))
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not key_is_valid and verify_google_identity(bearer) is None and verify_session_token(bearer) is None:
            detail = "Google sign-in is not configured on the server" if not GOOGLE_CLIENT_ID or not ALLOWED_GOOGLE_EMAIL else "Sign in with the approved Google account to continue"
            status = 503 if not GOOGLE_CLIENT_ID or not ALLOWED_GOOGLE_EMAIL else 401
            return JSONResponse(status_code=status, content={"detail": detail})
    if request.method != "OPTIONS" and request.url.path.startswith("/api/") and request.url.path != "/api/health":
        try:
            now = int(time.time())
            last_activity = get_last_activity()
            if last_activity is not None and now - last_activity >= 24 * 60 * 60:
                reset_persistent_data()
                for folder in (UPLOAD_DIR, OUTPUT_DIR):
                    folder.mkdir(parents=True, exist_ok=True)
                    for item in folder.iterdir():
                        if item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink(missing_ok=True)
                jobs.clear()
                clip_candidates.clear()
            touch_activity(now)
        except Exception as exc:
            return JSONResponse(status_code=503, content={"detail": f"24-hour workspace cleanup/check failed: {exc}"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


class ResetRequest(BaseModel):
    confirmation: Literal["RESET ALL CLIPFORGE DATA"]


class ApprovalRequest(BaseModel):
    approved: bool


class RenderRequest(BaseModel):
    start: float
    end: float
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    captions: bool = True
    caption_style: Literal["bold-pop", "creator-yellow", "clean"] = "bold-pop"
    caption_position: Literal["bottom", "top"] = "bottom"
    quality: Literal["standard", "high", "maximum"] = "high"
    edit_style: Literal["creator", "clean"] = "creator"
    focus_x: float = 0.5
    focus_y: float = 0.5


class ImportUrlRequest(BaseModel):
    url: str


class TranscriptionRequest(BaseModel):
    language: Literal["en", "ml", "hi", "ta", "kn", "te", "bn", "mr", "ur", "es", "fr", "de", "ar", "pt", "ru", "ja", "ko", "zh", "auto"] = "auto"
    task: Literal["transcribe", "translate"] = "transcribe"


@app.post("/api/admin/reset")
def reset_all_data(request: ResetRequest):
    # The API middleware has already authenticated this request with Google
    # or the private API key, so Google-only users may also clear their workspace.
    try:
        deleted = reset_persistent_data()
    except Exception as exc:
        raise HTTPException(503, f"Cloud reset failed: {exc}") from exc

    for folder in (UPLOAD_DIR, OUTPUT_DIR):
        folder.mkdir(parents=True, exist_ok=True)
        for item in folder.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink(missing_ok=True)
            except OSError as exc:
                raise HTTPException(500, f"Cloud data was cleared, but local cleanup failed: {exc}") from exc

    jobs.clear()
    clip_candidates.clear()
    return {
        "status": "reset",
        "deleted_cloud_objects": deleted,
        "cleared_database_tables": ["jobs", "clips"],
        "local_upload_and_render_files": "cleared",
    }


@app.post("/api/admin/prepare-session")
def prepare_temporary_session():
    """Start a fresh browser workspace by clearing the previous temporary session."""
    return reset_all_data(ResetRequest(confirmation="RESET ALL CLIPFORGE DATA"))


class GoogleCredentialRequest(BaseModel):
    credential: str


@app.get("/auth/config")
def google_auth_config():
    return {"configured": bool(GOOGLE_CLIENT_ID and ALLOWED_GOOGLE_EMAIL), "client_id": GOOGLE_CLIENT_ID or None, "session_configured": bool(SESSION_SECRET)}


@app.post("/auth/verify")
def verify_google_login(payload: GoogleCredentialRequest):
    identity = verify_google_identity(payload.credential)
    if identity is None:
        raise HTTPException(401, "This Google account is not authorized for ClipForge.")
    return {"ok": True, "email": identity["email"], "session_token": create_session_token(identity)}


@app.get("/auth/session")
def validate_persistent_session(request: Request):
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    identity = verify_session_token(bearer)
    if identity is None:
        raise HTTPException(401, "Your saved session has expired. Please sign in again.")
    return {"ok": True, "email": identity["email"]}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "clipforge-api",
        "yt_dlp_version": yt_dlp.version.__version__,
        "deno_available": bool(shutil.which("deno")),
    }


@app.post("/api/videos")
async def upload_video(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, "Unsupported video format")

    content_length = file.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")

    video_id = str(uuid.uuid4())
    destination = UPLOAD_DIR / f"{video_id}{suffix}"
    total = 0
    try:
        with destination.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")
                out.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if total == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, "Uploaded file is empty")

    jobs[video_id] = {
        "id": video_id,
        "filename": Path(file.filename or "video").name,
        "path": put_media(destination, f"uploads/{video_id}{suffix}"),
        "status": "uploaded",
        "transcript": None,
    }
    save_job(jobs[video_id])
    return {"id": video_id, "filename": jobs[video_id]["filename"], "status": "uploaded"}


class _PublicHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects to private/local hosts during direct media downloads."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_public_https_url(raw_url: str):
    from urllib.parse import urlparse

    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTPS video URL.")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("This host is not allowed.")
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("The video host could not be reached. Check the URL and try again.") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("The video host must resolve to a public internet address.")
    return parsed


def _download_direct_mp4(video_id: str, raw_url: str, update_job) -> Path:
    """Fetch a direct MP4 response with redirect, size and content validation."""
    from urllib.request import Request, build_opener

    _validate_public_https_url(raw_url)
    destination = UPLOAD_DIR / f"{video_id}.mp4"
    request = Request(raw_url, headers={"User-Agent": "ClipForgeAI/1.0", "Accept": "video/mp4,video/*,application/octet-stream"})
    opener = build_opener(_PublicHttpsRedirectHandler())
    total = 0
    last_progress_write = 0.0
    try:
        with opener.open(request, timeout=30) as response:
            content_type = response.headers.get_content_type().lower()
            if not (content_type.startswith("video/") or content_type in {"application/octet-stream", "application/binary", "binary/octet-stream"}):
                raise ValueError("This link did not return a video file. Use a direct MP4 download URL.")
            declared_size = response.headers.get("Content-Length")
            if declared_size and declared_size.isdigit() and int(declared_size) > MAX_UPLOAD_BYTES:
                raise ValueError(f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB import limit.")
            update_job(detail="Direct MP4 link found. Downloading video…", progress=0)
            with destination.open("wb") as output:
                first = response.read(1024 * 1024)
                if len(first) < 12 or first[4:8] != b"ftyp":
                    raise ValueError("The download response is not a valid MP4 file.")
                output.write(first)
                total += len(first)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise ValueError(f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB import limit.")
                    output.write(chunk)
                    now = time.time()
                    if now - last_progress_write >= 2:
                        last_progress_write = now
                        percent = min(99, int(total * 100 / int(declared_size))) if declared_size and declared_size.isdigit() else None
                        update_job(progress=percent, detail="Downloading direct MP4…" if percent is None else f"Downloading direct MP4… {percent}%")
        if total == 0:
            raise ValueError("The MP4 download was empty.")
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _import_video_url_task(video_id: str, raw_url: str):
    """Download a user-authorized public video/VOD using yt-dlp."""
    from urllib.parse import urlparse

    def update_job(**values):
        job = jobs.get(video_id)
        if job is not None:
            job.update(values)
            try:
                save_job(job)
            except Exception:
                pass

    try:
        parsed = urlparse(raw_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
            raise ValueError("Use a public HTTPS video or VOD URL.")
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("This host is not allowed.")
        try:
            addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("The video host could not be reached. Check the URL and try again.") from exc
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise ValueError("The video host must resolve to a public internet address.")

        update_job(status="downloading", progress=0, error=None, detail="Connecting to the video source…")
        destination = UPLOAD_DIR / f"{video_id}.%(ext)s"
        last_error = None
        downloaded = []

        # Direct MP4 endpoints often use extensionless paths such as /stream/123?d=true.
        # Route these through a bounded HTTP file download instead of a media extractor.
        direct_mp4_hint = parsed.path.lower().endswith(".mp4") or (
            "/stream/" in parsed.path.lower() and parsed.query.lower().find("d=true") >= 0
        )
        if direct_mp4_hint:
            direct_file = _download_direct_mp4(video_id, raw_url, update_job)
            downloaded = [direct_file]

        if not downloaded:
            # Prefer yt-dlp's default client selection for supported platform VODs.
            format_choices = (
                "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/bv*[height<=720]+ba/b[height<=720]",
            )
            for format_choice in format_choices:
                for partial in UPLOAD_DIR.glob(video_id + ".*"):
                    if partial.is_file():
                        partial.unlink(missing_ok=True)

                last_progress_write = [0.0]

                def progress_hook(data):
                    if data.get("status") == "downloading":
                        total_bytes = data.get("total_bytes") or data.get("total_bytes_estimate")
                        received = data.get("downloaded_bytes") or 0
                        percent = min(99, int(received * 100 / total_bytes)) if total_bytes else None
                        now = time.time()
                        if now - last_progress_write[0] >= 2 or percent == 99:
                            last_progress_write[0] = now
                            update_job(
                                progress=percent,
                                detail="Downloading video…" if percent is None else f"Downloading video… {percent}%",
                                filename=str(data.get("info_dict", {}).get("title") or "Video download"),
                            )
                    elif data.get("status") == "finished":
                        update_job(progress=99, detail="Download complete. Preparing video…")

                ydl_options = {
                    "outtmpl": str(destination),
                    "format": format_choice,
                    "merge_output_format": "mp4",
                    "noplaylist": True,
                    "quiet": False,
                    "no_warnings": False,
                    "socket_timeout": 30,
                    "retries": 3,
                    "fragment_retries": 3,
                    "extractor_retries": 2,
                    "file_access_retries": 2,
                    # Prefer IPv4 on hosts whose IPv6 route is unreliable.
                    "source_address": "0.0.0.0",
                    # Avoid repeated cache writes on ephemeral server storage.
                    "cachedir": False,
                    "concurrent_fragment_downloads": 2,
                    "max_filesize": MAX_UPLOAD_BYTES,
                    "restrictfilenames": True,
                    "overwrites": True,
                    "remote_components": ["ejs:github"],
                    "verbose": True,
                    "noprogress": True,
                    "progress_hooks": [progress_hook],
                }
                try:
                    with yt_dlp.YoutubeDL(ydl_options) as ydl:
                        ydl.download([raw_url])
                    downloaded = [
                        path for path in UPLOAD_DIR.glob(video_id + ".*")
                        if path.is_file()
                        and path.suffix.lower() in ALLOWED_EXTENSIONS
                        and not path.name.endswith((".part", ".ytdl"))
                        and path.stat().st_size > 0
                    ]
                    if downloaded:
                        break
                    last_error = "The source did not produce a downloadable video file."
                except Exception as exc:
                    last_error = str(exc)[:700]
                    update_job(detail="Trying a compatible download format…")

        if not downloaded:
            raise ValueError(last_error or "yt-dlp could not find a downloadable format.")

        source = max(downloaded, key=lambda path: path.stat().st_size)
        if source.stat().st_size == 0:
            raise ValueError("The downloaded video is empty.")
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError(f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB import limit.")

        title = jobs[video_id].get("filename") or "imported-video"
        safe_title = re.sub(r"[^A-Za-z0-9._ -]", "", str(title)).strip()[:90] or "imported-video"
        update_job(
            filename=f"{safe_title}{source.suffix}",
            path=put_media(source, f"uploads/{video_id}{source.suffix}"),
            status="uploaded",
            progress=100,
            detail="Video imported and ready.",
            transcript=None,
            error=None,
        )
    except Exception as exc:
        for item in UPLOAD_DIR.glob(video_id + ".*"):
            if item.is_file():
                item.unlink(missing_ok=True)
        update_job(status="failed", progress=0, error=str(exc)[:700], detail="Import failed.")


@app.post("/api/videos/import-url")
def import_video_url(request: ImportUrlRequest, background_tasks: BackgroundTasks):
    """Queue a URL import and return immediately; poll the import status endpoint."""
    raw_url = request.url.strip()
    video_id = str(uuid.uuid4())
    jobs[video_id] = {
        "id": video_id,
        "filename": "Checking video link…",
        "path": "",
        "status": "queued",
        "transcript": None,
        "source": "video-url",
    }
    save_job(jobs[video_id])
    background_tasks.add_task(_import_video_url_task, video_id, raw_url)
    return {"id": video_id, "filename": jobs[video_id]["filename"], "status": "queued", "source": "video-url"}


@app.get("/api/videos/{video_id}/import-status")
def import_video_status(video_id: str):
    job = jobs.get(video_id)
    if not job:
        raise HTTPException(404, "Import job not found")
    return {
        "id": video_id,
        "filename": job.get("filename"),
        "status": job.get("status"),
        "progress": job.get("progress"),
        "detail": job.get("detail"),
        "error": job.get("error"),
    }

@app.get("/api/videos")
def list_videos():
    return [{k: v for k, v in job.items() if k != "path"} for job in jobs.values()]


def _vtt_timestamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = "0", parts[0], parts[1]
    else:
        return 0.0
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _segments_from_vtt(vtt: str) -> list[dict]:
    segments = []
    for block in re.split(r"\n\s*\n", vtt.replace("\r", "").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split("-->", 1)
        start_match = re.search(r"[0-9:.,]+", timing[0])
        end_match = re.search(r"[0-9:.,]+", timing[1])
        if not start_match or not end_match:
            continue
        text = " ".join(lines[timing_index + 1:]).strip()
        if text:
            segments.append({
                "start": _vtt_timestamp(start_match.group()),
                "end": _vtt_timestamp(end_match.group()),
                "text": text,
            })
    return segments


@app.post("/api/videos/{video_id}/transcribe")
def transcribe(video_id: str, options: TranscriptionRequest = TranscriptionRequest()):
    job = jobs.get(video_id)
    if not job:
        raise HTTPException(404, "Video not found")
    if shutil.which("ffmpeg") is None:
        raise HTTPException(503, "FFmpeg is not installed on the server")

    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    api_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not api_token:
        raise HTTPException(503, "Cloudflare Workers AI credentials are not configured")

    audio_path = None
    try:
        source_video = local_video_path(job)
        with tempfile.NamedTemporaryFile(suffix=".mp3", dir=str(UPLOAD_DIR), delete=False) as audio_file:
            audio_path = Path(audio_file.name)
        extract = [
            "ffmpeg", "-y", "-i", str(source_video), "-vn", "-ac", "1",
            "-ar", "16000", "-b:a", "48k", "-threads", "1",
            "-f", "mp3", str(audio_path),
        ]
        _run_ffmpeg(extract, timeout=600, task="audio extraction")
        audio_bytes = audio_path.read_bytes()
        if not audio_bytes:
            raise HTTPException(422, "No audio track was found in this video")

        endpoint = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            "/ai/run/@cf/openai/whisper-large-v3-turbo"
        )
        model_input = {
            "audio": base64.b64encode(audio_bytes).decode("ascii"),
            "task": options.task,
            "vad_filter": True,
        }
        if options.language != "auto":
            model_input["language"] = options.language
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(model_input).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload.get("result", payload)
        transcript_text = str(result.get("text", "")).strip()
        vtt = str(result.get("vtt", ""))
        info = result.get("transcription_info", {}) or {}
        detected_language = info.get("language") or result.get("language")
        segments = _segments_from_vtt(vtt)
        if not segments and isinstance(result.get("segments"), list):
            segments = [
                {"start": float(item.get("start", 0)), "end": float(item.get("end", 0)),
                 "text": str(item.get("text", "")).strip()}
                for item in result["segments"] if isinstance(item, dict) and item.get("text")
            ]
        if not segments and transcript_text:
            segments = [{"start": 0.0, "end": 0.0, "text": transcript_text}]
        if not transcript_text:
            raise HTTPException(502, "Cloudflare returned an empty transcription")

    except HTTPException:
        raise
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise HTTPException(502, f"Cloudflare transcription error ({exc.code}): {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Audio extraction timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(422, f"Could not extract audio: {exc.stderr[-800:]}") from exc
    except Exception as exc:
        raise HTTPException(502, f"Cloudflare transcription failed: {exc}") from exc
    finally:
        if audio_path:
            audio_path.unlink(missing_ok=True)

    job["transcript"] = segments
    job["status"] = "transcribed"
    job["transcription_language"] = detected_language or options.language
    job["transcription_task"] = options.task
    save_job(job)
    return {
        "video_id": video_id,
        "language": detected_language or (None if options.language == "auto" else options.language),
        "task": options.task,
        "text": transcript_text,
        "segments": segments,
    }


def _analyze_candidate_media(source: Path, start: float, end: float) -> dict:
    """Measure scene-change density and audio dynamics for one candidate window."""
    duration = max(0.1, min(float(end) - float(start), 180.0))
    command = [
        "ffmpeg", "-hide_banner", "-ss", str(max(0.0, float(start))),
        "-i", str(source), "-t", str(duration),
        "-vf", "select='gt(scene,0.25)',metadata=print",
        "-af", "astats=metadata=1:reset=0",
        "-f", "null", "-",
    ]
    try:
        result = _run_ffmpeg(command, timeout=20, task="highlight media analysis")
        stamps = re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr)
        scene_count = len(stamps)
        rms_values = re.findall(r"RMS level dB:\s*(-?[0-9]+(?:\.[0-9]+)?)", result.stderr)
        peak_values = re.findall(r"Peak level dB:\s*(-?[0-9]+(?:\.[0-9]+)?)", result.stderr)
        rms_db = float(rms_values[-1]) if rms_values else None
        peak_db = float(peak_values[-1]) if peak_values else None
        # A scene-change rate is only a visual activity cue, not object/action understanding.
        visual_score = min(100.0, (scene_count / max(duration / 12.0, 1.0)) * 55.0)
        audio_score = None
        if rms_db is not None and peak_db is not None and rms_db > -90 and peak_db > -90:
            dynamic_range = max(0.0, peak_db - rms_db)
            activity = max(0.0, min(1.0, (rms_db + 48.0) / 32.0))
            dynamics = max(0.0, min(1.0, (dynamic_range - 5.0) / 18.0))
            audio_score = round(100.0 * (0.6 * activity + 0.4 * dynamics), 1)
        return {
            "visual_scene_changes": scene_count,
            "visual_activity_score": round(visual_score, 1),
            "audio_rms_db": round(rms_db, 1) if rms_db is not None else None,
            "audio_peak_db": round(peak_db, 1) if peak_db is not None else None,
            "audio_activity_score": audio_score,
        }
    except Exception:
        # Media analysis is supplementary; transcript detection still works if it fails.
        return {
            "visual_scene_changes": 0,
            "visual_activity_score": None,
            "audio_rms_db": None,
            "audio_peak_db": None,
            "audio_activity_score": None,
        }


@app.post("/api/videos/{video_id}/suggest-clips")
def suggest_clips(video_id: str, min_seconds: float = 30, max_seconds: float = 60, aspect: Literal["9:16", "16:9", "1:1"] = "9:16"):
    job = jobs.get(video_id)
    if not job:
        raise HTTPException(404, "Video not found")
    if not job.get("transcript"):
        raise HTTPException(409, "Transcribe the video before suggesting clips")
    if min_seconds <= 0 or max_seconds < min_seconds or max_seconds > 180:
        raise HTTPException(422, "Invalid clip duration limits")

    ranked, detection_method = detect_highlights(job["transcript"], min_seconds, max_seconds)
    # Run bounded FFmpeg media analysis only on the strongest transcript candidates.
    # This keeps CPU use predictable on the existing small Render instance.
    try:
        source_video = local_video_path(job)
    except Exception as exc:
        raise HTTPException(503, f"Could not retrieve source video for media analysis: {exc}") from exc
    ranked = ranked[:6]
    for item in ranked:
        media = _analyze_candidate_media(source_video, item["start"], item["end"])
        item.update(media)
        visual = media.get("visual_activity_score")
        audio = media.get("audio_activity_score")
        available = [value for value in (visual, audio) if value is not None]
        if available:
            media_score = sum(available) / len(available)
            item["transcript_score"] = item["score"]
            item["score"] = round(0.60 * item["score"] + 0.40 * media_score)
            item["reason"] = (item.get("reason", "") + "; balanced audio/visual activity signals")[:180]
            item["detection_method"] = "transcript+audio_visual"
    ranked.sort(key=lambda item: item["score"], reverse=True)
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    if any(item.get("audio_activity_score") is not None or item.get("visual_activity_score") is not None for item in ranked):
        detection_method = "transcript+audio_visual"
    candidates = []
    for item in ranked:
        candidate_id = str(uuid.uuid4())
        candidate = {
            "id": candidate_id,
            "video_id": video_id,
            **item,
            "status": "approved",
            "approval_method": "automatic",
            "aspect": "9:16",
        }
        clip_candidates[candidate_id] = candidate
        save_clip(candidate)
        candidates.append(candidate)
    job["status"] = "clips_approved"
    save_job(job)
    return {"video_id": video_id, "detection_method": detection_method, "candidates": candidates}


@app.get("/api/clips")
def list_clips():
    return list(clip_candidates.values())


@app.post("/api/clips/{clip_id}/approval")
def approve_clip(clip_id: str, request: ApprovalRequest):
    candidate = clip_candidates.get(clip_id)
    if not candidate:
        raise HTTPException(404, "Clip candidate not found")
    candidate["status"] = "approved" if request.approved else "rejected"
    save_clip(candidate)
    return candidate


@app.post("/api/clips/{clip_id}/preview")
def preview_clip(clip_id: str):
    candidate = clip_candidates.get(clip_id)
    if not candidate:
        raise HTTPException(404, "Clip candidate not found")
    if candidate.get("status") != "approved":
        raise HTTPException(409, "Only approved highlights can be previewed")
    job = jobs.get(candidate["video_id"])
    if not job:
        raise HTTPException(404, "Source video record not found")
    try:
        source_video = local_video_path(job)
    except Exception as exc:
        raise HTTPException(503, f"Could not retrieve source video: {exc}") from exc
    duration = float(candidate["end"]) - float(candidate["start"])
    if duration <= 0 or duration > 180:
        raise HTTPException(422, "Invalid preview duration")
    aspect = candidate.get("aspect", "9:16")
    dimensions = {"9:16": "720:1280", "16:9": "1280:720", "1:1": "720:720"}
    preview_path = OUTPUT_DIR / f"preview-{uuid.uuid4().hex}.mp4"
    target_size = dimensions.get(aspect, "720:1280")
    scene_cuts = _detect_scene_cuts(source_video, float(candidate["start"]), duration)
    preview_filters = (
        f"scale={target_size}:force_original_aspect_ratio=decrease,"
        f"pad={target_size}:(ow-iw)/2:(oh-ih)/2:color=0x171713,setsar=1,"
        + _creator_punch_filter(target_size, scene_cuts)
    )
    caption_file = OUTPUT_DIR / f"preview-{uuid.uuid4().hex}.ass"
    try:
        transcript_segments = job.get("transcript") or []
        if transcript_segments:
            _build_caption_ass(
                transcript_segments, float(candidate["start"]), float(candidate["end"]),
                aspect, "bold-pop", "bottom", caption_file, creator_overlays=True
            )
            escaped_ass_path = str(caption_file).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            preview_filters += f",subtitles='{escaped_ass_path}'"
        command = [
            "ffmpeg", "-y", "-ss", str(candidate["start"]), "-i", str(source_video),
            "-t", str(duration), "-vf", preview_filters,
            "-threads", "1", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "24", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(preview_path),
        ]
        _run_ffmpeg(command, timeout=240, task="highlight preview")
    except subprocess.TimeoutExpired as exc:
        preview_path.unlink(missing_ok=True)
        raise HTTPException(504, "Highlight preview timed out. Try rendering the clip directly.") from exc
    except subprocess.CalledProcessError as exc:
        preview_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Could not create highlight preview: {exc.stderr[-900:]}") from exc
    except Exception as exc:
        preview_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Could not prepare preview captions: {exc}") from exc
    finally:
        caption_file.unlink(missing_ok=True)
    return FileResponse(
        preview_path, media_type="video/mp4",
        filename=f"highlight-preview-{clip_id}.mp4",
        background=BackgroundTask(lambda: preview_path.unlink(missing_ok=True)),
    )


@app.get("/api/clips/{clip_id}/download")
def download_clip(clip_id: str):
    candidate = clip_candidates.get(clip_id)
    if not candidate:
        raise HTTPException(404, "Clip candidate not found")
    output = OUTPUT_DIR / f"{clip_id}.mp4"
    if candidate.get("render_path"):
        try:
            output = get_media(candidate["render_path"], output)
        except Exception as exc:
            raise HTTPException(503, f"Could not retrieve clip from cloud storage: {exc}") from exc
    if not output.is_file():
        raise HTTPException(404, "Rendered clip not found")
    return FileResponse(output, media_type="video/mp4", filename=f"clipforge-{clip_id}.mp4")


def _ass_time(seconds: float) -> str:
    value = max(0.0, float(seconds))
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    whole = int(value % 60)
    centiseconds = int((value - int(value)) * 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{centiseconds:02d}"


def _ass_escape_word(value: str) -> str:
    return value.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").replace("\r", " ").strip()


def _build_caption_ass(segments: list[dict], clip_start: float, clip_end: float,
                       aspect: str, style: str, position: str, destination: Path,
                       creator_overlays: bool = False) -> None:
    themes = {
        "bold-pop": ("&H0000DFFF", "&H00FFFFFF", 1, 5, 3),
        "creator-yellow": ("&H0000FFFF", "&H00FFFFFF", 1, 5, 3),
        "clean": ("&H00FFFFFF", "&H00FFFFFF", 0, 3, 2),
    }
    primary, secondary, bold, outline, shadow = themes[style]
    # Large, phone-readable Shorts captions with room for the platform UI.
    font_size = 98 if style != "clean" and aspect == "9:16" else (82 if aspect == "9:16" else 58)
    alignment = 8 if position == "top" else 2
    margin_v = 245 if position == "top" else (430 if aspect == "9:16" else 145)
    play_width, play_height = (1920, 1080) if aspect == "16:9" else (1080, 1920)
    header = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {play_width}
PlayResY: {play_height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{font_size},{primary},{secondary},1,&H99000000,1,0,1,{outline},{shadow},{alignment},65,65,{margin_v},1
Style: PopA,DejaVu Sans,{font_size},{primary},{secondary},1,&H99000000,1,0,1,6,3,{alignment},55,55,{margin_v},1
Style: PopB,DejaVu Sans Condensed,{int(font_size * 1.06)},&H0000DFFF,&H00FFFFFF,1,&H99000000,1,0,1,6,3,{alignment},55,55,{margin_v},1
Style: Punch,Liberation Sans,{int(font_size * 1.10)},&H0000FFFF,&H00FFFFFF,1,&H99000000,1,0,1,7,4,{alignment},45,45,{margin_v},1
Style: Callout,DejaVu Sans,58,&H0000FFFFFF,&H0000FFFFFF,&H00000000,&HAA183D18,1,0,3,2,1,8,90,90,230,1
Style: EndCard,DejaVu Sans,82,&H0000FFFFFF,&H0000FFFFFF,&H00000000,&HAA183D18,1,0,3,3,2,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues = []
    phrase_index = 0
    emphasis_words = {
        "wow", "what", "why", "no", "yes", "never", "best", "worst",
        "crazy", "amazing", "insane", "stop", "wait", "look", "secret",
        "important", "really", "exactly", "impossible", "finally",
    }
    for segment in segments:
        try:
            absolute_start = float(segment.get("start", 0))
            absolute_end = float(segment.get("end", 0))
        except (TypeError, ValueError):
            continue
        start = max(absolute_start, clip_start)
        end = min(absolute_end, clip_end)
        words = [_ass_escape_word(word) for word in str(segment.get("text", "")).split()]
        words = [word for word in words if word]
        if end <= start or not words:
            continue

        # Whisper provides phrase timestamps here. Approximate word timing inside
        # each phrase and use compact 1–3 word bursts for the vertical short format.
        duration_cs = max(len(words), int(round((end - start) * 100)))
        weights = [max(1, len(re.sub(r"[^\w]", "", word))) for word in words]
        total_weight = sum(weights)
        remaining_cs = duration_cs - len(words)
        word_durations = [1 + int(remaining_cs * weight / total_weight) for weight in weights]
        word_durations[-1] += duration_cs - sum(word_durations)
        timed_words = []
        cursor = 0
        for word, word_cs in zip(words, word_durations):
            timed_words.append((word, word_cs, cursor))
            cursor += word_cs

        for offset in range(0, len(timed_words), 2):
            chunk = timed_words[offset:offset + 2]
            chunk_start_cs = chunk[0][2]
            chunk_end_cs = chunk[-1][2] + chunk[-1][1]
            phrase_text = " ".join(item[0] for item in chunk)
            is_emphasis = any(re.sub(r"[^a-z]", "", word.lower()) in emphasis_words for word, _, _ in chunk)
            style_name = "Punch" if is_emphasis else ("PopB" if phrase_index % 2 else "PopA")
            karaoke = " ".join(f"{{\\k{word_cs}}}{word}" for word, word_cs, _ in chunk)
            # A short entrance scale makes every caption burst feel synced to speech.
            entrance = "{\\fscx82\\fscy82\\t(0,110,\\fscx108\\fscy108)\\t(110,170,\\fscx100\\fscy100)}"
            event_start = max(0.0, start - clip_start + chunk_start_cs / 100)
            event_end = min(clip_end - clip_start, start - clip_start + chunk_end_cs / 100)
            if event_end > event_start:
                dialogues.append(
                    f"Dialogue: 0,{_ass_time(event_start)},{_ass_time(event_end)},{style_name},,0,0,0,,{entrance}{{\\q2}}{karaoke}"
                )
            phrase_index += 1

    if creator_overlays:
        hook_words = {
            "why", "secret", "never", "best", "because", "important", "truth",
            "mistake", "finally", "imagine", "result", "tip", "win", "wrong",
            "discover", "impossible", "lesson", "crazy", "amazing",
        }
        callouts = []
        for segment in segments:
            try:
                seg_start = max(float(segment.get("start", 0)), clip_start)
                seg_end = min(float(segment.get("end", 0)), clip_end)
            except (TypeError, ValueError):
                continue
            words = str(segment.get("text", "")).split()
            if seg_end <= seg_start or not words:
                continue
            if not any(re.sub(r"[^a-z]", "", word.lower()) in hook_words for word in words):
                continue
            phrase = " ".join(_ass_escape_word(word) for word in words[:5])[:72].strip()
            if phrase:
                callouts.append((seg_start - clip_start, min(seg_end - clip_start, seg_start - clip_start + 1.8), phrase))
            if len(callouts) >= 4:
                break
        for callout_start, callout_end, phrase in callouts:
            if callout_end > callout_start:
                dialogues.append(
                    f"Dialogue: 1,{_ass_time(callout_start)},{_ass_time(callout_end)},Callout,,0,0,0,,{{\\fad(100,140)\\an8}}{phrase}"
                )
        clip_duration = max(0.0, clip_end - clip_start)
        if clip_duration >= 2.2:
            dialogues.append(
                f"Dialogue: 2,{_ass_time(clip_duration - 2.0)},{_ass_time(clip_duration)},EndCard,,0,0,0,,{{\\fad(180,180)}}FOLLOW FOR MORE"
            )
    destination.write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8")


@app.post("/api/clips/{clip_id}/render")
def render_clip(clip_id: str, request: RenderRequest):
    candidate = clip_candidates.get(clip_id)
    if not candidate:
        raise HTTPException(404, "Clip candidate not found")
    if candidate["status"] != "approved":
        raise HTTPException(409, "A clip must be approved before rendering")
    if request.start < 0 or request.end <= request.start or request.end - request.start > 180:
        raise HTTPException(422, "Invalid clip time range")
    job = jobs.get(candidate["video_id"])
    if not job:
        raise HTTPException(404, "Source video record not found")
    try:
        source_video = local_video_path(job)
    except Exception as exc:
        raise HTTPException(503, f"Could not retrieve source video from cloud storage: {exc}") from exc
    output = OUTPUT_DIR / f"{clip_id}.mp4"
    # ClipForge exports every final clip as vertical 9:16, regardless of client input.
    export_aspect = "9:16"
    dimensions = {"9:16": "1080:1920"}
    scene_cuts = _detect_scene_cuts(source_video, request.start, request.end - request.start)
    # Preserve the entire source frame instead of center-cropping faces and on-screen text.
    filters = (
        f"scale={dimensions[export_aspect]}:force_original_aspect_ratio=decrease,"
        f"pad={dimensions[export_aspect]}:(ow-iw)/2:(oh-ih)/2:color=0x171713,setsar=1"
    )
    if request.edit_style == "creator":
        # A restrained punch-in gives the energetic creator-edit feel without
        # cutting off the source frame or tracking a subject incorrectly.
        filters += "," + _creator_punch_filter(
            dimensions[export_aspect], scene_cuts, request.focus_x, request.focus_y
        )

    # Give each clip a repeatable, randomly varied creator-grade treatment.
    # Scene-cut punch-ins above act as micro-transitions; fades soften clip edges.
    duration = request.end - request.start
    presets = [
        "eq=contrast=1.08:saturation=1.18:brightness=0.01",
        "eq=contrast=1.12:saturation=0.92,vignette=PI/5",
        "eq=contrast=1.04:saturation=1.25,unsharp=5:5:0.45:3:3:0",
        "eq=contrast=1.06:saturation=1.08:gamma=1.02",
    ]
    preset_index = sum(ord(char) for char in clip_id) % len(presets)
    filters += "," + presets[preset_index]
    if duration > 0.5:
        fade_out_start = max(0.18, duration - 0.18)
        filters += f",fade=t=in:st=0:d=0.18,fade=t=out:st={fade_out_start:.3f}:d=0.18"

    caption_file = None
    if request.captions:
        transcript_segments = job.get("transcript") or []
        if not transcript_segments:
            raise HTTPException(409, "Transcribe this source video before rendering captions.")
        caption_file = OUTPUT_DIR / f"{clip_id}-captions.ass"
        try:
            _build_caption_ass(
                transcript_segments, request.start, request.end, export_aspect,
                request.caption_style, request.caption_position, caption_file,
                creator_overlays=request.edit_style == "creator"
            )
            escaped_ass_path = str(caption_file).replace("\\\\", "\\\\\\\\").replace(":", "\\\\:").replace("'", "\\\\'")
            filters += f",subtitles='{escaped_ass_path}'"
        except Exception as exc:
            caption_file.unlink(missing_ok=True)
            raise HTTPException(500, f"Could not prepare animated captions: {exc}") from exc
    command = [
        "ffmpeg", "-y", "-ss", str(request.start), "-i", str(source_video),
        "-t", str(request.end - request.start),
        "-vf", filters,
        "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", {"standard": "25", "high": "22", "maximum": "20"}[request.quality],
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-threads", "1",
        "-movflags", "+faststart", str(output),
    ]
    # Add a very subtle, locally synthesized opening whoosh when the source has audio.
    # This avoids external copyrighted SFX files and leaves silent videos untouched.
    try:
        audio_probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(source_video)],
            capture_output=True, text=True, timeout=15,
        )
        if audio_probe.returncode == 0 and audio_probe.stdout.strip():
            command[command.index("-t"):command.index("-t")] = [
                "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=48000:duration=0.65"
            ]
            audio_graph = (
                "[0:a]aresample=48000[base];"
                "[1:a]highpass=f=450,lowpass=f=6500,"
                "afade=t=in:st=0:d=0.05,afade=t=out:st=0.16:d=0.40,"
                "volume=0.035,adelay=120|120[sfx];"
                "[base][sfx]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            )
            command[command.index("-threads"):command.index("-threads")] = [
                "-filter_complex", audio_graph, "-map", "0:v:0", "-map", "[aout]"
            ]
    except (subprocess.TimeoutExpired, OSError):
        # SFX are optional; a probe failure must not block the actual render.
        pass

    try:
        _run_ffmpeg(command, timeout=900, task="final render")
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Rendering timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(500, f"FFmpeg render failed: {exc.stderr[-1200:]}") from exc
    finally:
        if caption_file:
            caption_file.unlink(missing_ok=True)
    try:
        candidate["render_path"] = put_media(output, f"renders/{clip_id}.mp4")
        candidate["rendered"] = True
        save_clip(candidate)
    except Exception as exc:
        raise HTTPException(503, f"Clip rendered but cloud storage upload failed: {exc}") from exc
    return {
        "clip_id": clip_id,
        "status": "rendered",
        "file": str(output),
        "scene_count": len(scene_cuts) + 1,
        "scene_changes": scene_cuts,
        "edit_features": ["scene-change analysis", "adaptive punch-in", "transcript captions", "creator callouts", "end card"] if request.edit_style == "creator" else ["safe-fit framing", "transcript captions"],
        "subject_tracking": "not_available",
    }
