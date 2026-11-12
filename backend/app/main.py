from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Literal

import whisper

from .highlight_detector import detect_highlights
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DATA_DIR = Path(os.getenv("CLIPFORGE_DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "renders"
for folder in (UPLOAD_DIR, OUTPUT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ClipForge AI API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CLIPFORGE_ALLOWED_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# Demo-only in-memory state. Replace with PostgreSQL before production.
jobs: dict[str, dict] = {}
clip_candidates: dict[str, dict] = {}
whisper_model = None
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


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
    video_id = str(uuid.uuid4())
    destination = UPLOAD_DIR / f"{video_id}{suffix}"
    try:
        with destination.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        await file.close()
    jobs[video_id] = {
        "id": video_id,
        "filename": file.filename,
        "path": str(destination),
        "status": "uploaded",
        "transcript": None,
    }
    return {"id": video_id, "filename": file.filename, "status": "uploaded"}


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

    segments = job["transcript"]
    ranked, detection_method = detect_highlights(segments, min_seconds, max_seconds)
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
