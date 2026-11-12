from __future__ import annotations

import hmac
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Literal

import whisper
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

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
    allow_origins=os.getenv(
        "CLIPFORGE_ALLOWED_ORIGINS",
        "https://clipforge-ai-db2.pages.dev",
    ).split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# Demo-only in-memory state. Replace with PostgreSQL before production.
jobs: dict[str, dict] = {}
clip_candidates: dict[str, dict] = {}
whisper_model = None
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


@app.middleware("http")
async def protect_api_and_add_headers(request: Request, call_next):
    if request.url.path.startswith("/api/") and API_KEY:
        supplied_key = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(supplied_key, API_KEY):
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid API key"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


class ApprovalRequest(BaseModel):
    approved: bool


class RenderRequest(BaseModel):
    start: float
    end: float
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"


@app.get("/health")
def health():
    return {"status": "ok", "service": "clipforge-api"}


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
        "path": str(destination),
        "status": "uploaded",
        "transcript": None,
    }
    return {"id": video_id, "filename": jobs[video_id]["filename"], "status": "uploaded"}


@app.get("/api/videos")
def list_videos():
    return [{k: v for k, v in job.items() if k != "path"} for job in jobs.values()]


@app.post("/api/videos/{video_id}/transcribe")
def transcribe(video_id: str, model_name: str = "base"):
    global whisper_model
    job = jobs.get(video_id)
    if not job:
        raise HTTPException(404, "Video not found")
    if shutil.which("ffmpeg") is None:
        raise HTTPException(503, "FFmpeg is not installed on the server")
    try:
        if whisper_model is None:
            whisper_model = whisper.load_model(model_name)
        result = whisper_model.transcribe(job["path"], word_timestamps=True)
    except Exception as exc:
        raise HTTPException(500, f"Transcription failed: {exc}") from exc
    segments = [
        {"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
        for s in result.get("segments", [])
    ]
    job["transcript"] = segments
    job["status"] = "transcribed"
    return {"video_id": video_id, "language": result.get("language"), "segments": segments}


@app.post("/api/videos/{video_id}/suggest-clips")
def suggest_clips(video_id: str, min_seconds: float = 15, max_seconds: float = 60):
    job = jobs.get(video_id)
    if not job:
        raise HTTPException(404, "Video not found")
    if not job.get("transcript"):
        raise HTTPException(409, "Transcribe the video before suggesting clips")
    if min_seconds <= 0 or max_seconds < min_seconds or max_seconds > 180:
        raise HTTPException(422, "Invalid clip duration limits")

    ranked, detection_method = detect_highlights(job["transcript"], min_seconds, max_seconds)
    candidates = []
    for item in ranked:
        candidate_id = str(uuid.uuid4())
        candidate = {
            "id": candidate_id,
            "video_id": video_id,
            **item,
            "status": "approved",
            "approval_method": "automatic",
        }
        clip_candidates[candidate_id] = candidate
        candidates.append(candidate)
    job["status"] = "clips_approved"
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
    return candidate


@app.get("/api/clips/{clip_id}/download")
def download_clip(clip_id: str):
    candidate = clip_candidates.get(clip_id)
    if not candidate:
        raise HTTPException(404, "Clip candidate not found")
    output = OUTPUT_DIR / f"{clip_id}.mp4"
    if not output.is_file():
        raise HTTPException(404, "Rendered clip not found")
    return FileResponse(output, media_type="video/mp4", filename=f"clipforge-{clip_id}.mp4")


@app.post("/api/clips/{clip_id}/render")
def render_clip(clip_id: str, request: RenderRequest):
    candidate = clip_candidates.get(clip_id)
    if not candidate:
        raise HTTPException(404, "Clip candidate not found")
    if candidate["status"] != "approved":
        raise HTTPException(409, "A clip must be approved before rendering")
    if request.start < 0 or request.end <= request.start or request.end - request.start > 180:
        raise HTTPException(422, "Invalid clip time range")
    job = jobs[candidate["video_id"]]
    output = OUTPUT_DIR / f"{clip_id}.mp4"
    dimensions = {"9:16": "1080:1920", "16:9": "1920:1080", "1:1": "1080:1080"}
    command = [
        "ffmpeg", "-y", "-ss", str(request.start), "-i", job["path"],
        "-t", str(request.end - request.start),
        "-vf", f"scale={dimensions[request.aspect]}:force_original_aspect_ratio=increase,crop={dimensions[request.aspect]}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-movflags", "+faststart", str(output),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Rendering timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(500, f"FFmpeg render failed: {exc.stderr[-1200:]}") from exc
    return {"clip_id": clip_id, "status": "rendered", "file": str(output)}
