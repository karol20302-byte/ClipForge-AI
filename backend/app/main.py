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
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, HttpUrl

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
    allow_origins=os.getenv(
        "CLIPFORGE_ALLOWED_ORIGINS",
        "https://clipforge-ai-db2.pages.dev",
    ).split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)

jobs: dict[str, dict] = {}
clip_candidates: dict[str, dict] = {}


@app.on_event("startup")
def restore_persistent_state():
    init_storage()
    restored_jobs, restored_clips = load_state()
    jobs.update(restored_jobs)
    clip_candidates.update(restored_clips)


def local_video_path(job: dict) -> Path:
    location = job["path"]
    if location.startswith("b2://"):
        suffix = Path(job.get("filename", "source.mp4")).suffix or ".mp4"
        cached = UPLOAD_DIR / (job["id"] + "-cached" + suffix)
        return get_media(location, cached)
    return Path(location)
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


@app.middleware("http")
async def protect_api_and_add_headers(request: Request, call_next):
    if request.method != "OPTIONS" and request.url.path.startswith("/api/") and API_KEY:
        supplied_key = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(supplied_key, API_KEY):
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid API key"})
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


class ImportUrlRequest(BaseModel):
    url: str


class TranscriptionRequest(BaseModel):
    language: Literal["en", "ml", "hi", "ta", "kn", "te", "bn", "mr", "ur", "es", "fr", "de", "ar", "pt", "ru", "ja", "ko", "zh", "auto"] = "auto"
    task: Literal["transcribe", "translate"] = "transcribe"


@app.post("/api/admin/reset")
def reset_all_data(request: ResetRequest):
    if not API_KEY:
        raise HTTPException(503, "Set CLIPFORGE_API_KEY before using the reset endpoint")
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
        "path": put_media(destination, f"uploads/{video_id}{suffix}"),
        "status": "uploaded",
        "transcript": None,
    }
    save_job(jobs[video_id])
    return {"id": video_id, "filename": jobs[video_id]["filename"], "status": "uploaded"}


@app.post("/api/videos/import-url")
def import_video_url(request: ImportUrlRequest):
    from urllib.parse import urlparse, unquote

    parsed = urlparse(request.url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, "Use a public HTTPS direct video-file URL.")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"localhost", "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        raise HTTPException(422, "This field accepts direct media-file URLs, not YouTube watch links.")
    suffix = Path(unquote(parsed.path)).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, "The URL must point directly to MP4, MOV, MKV, WEBM, or M4V.")
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError("No address found")
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("Non-public address")
    except Exception as exc:
        raise HTTPException(422, "The media host must resolve to a public internet address.") from exc

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    video_id = str(uuid.uuid4())
    destination = UPLOAD_DIR / f"{video_id}{suffix}"
    total = 0
    try:
        req = urllib.request.Request(request.url.strip(), headers={"User-Agent": "ClipForgeAI/1.0"})
        with opener.open(req, timeout=30) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"video/mp4", "video/quicktime", "video/x-matroska", "video/webm", "application/octet-stream"}:
                raise HTTPException(415, "The URL did not return a supported video file.")
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB import limit")
            with destination.open("wb") as out:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"Video exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB import limit")
                    out.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(502, f"Media host returned HTTP {exc.code}; redirects are not followed.") from exc
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(502, f"Could not import the media URL: {exc}") from exc
    if total == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, "The media URL returned an empty file")

    filename = Path(unquote(parsed.path)).name or f"imported-video{suffix}"
    jobs[video_id] = {
        "id": video_id,
        "filename": filename,
        "path": put_media(destination, f"uploads/{video_id}{suffix}"),
        "status": "uploaded",
        "transcript": None,
    }
    save_job(jobs[video_id])
    return {"id": video_id, "filename": filename, "status": "uploaded"}


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
            "-ar", "16000", "-b:a", "64k", "-f", "mp3", str(audio_path),
        ]
        subprocess.run(extract, check=True, capture_output=True, text=True, timeout=600)
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
    candidates = []
    for item in ranked:
        candidate_id = str(uuid.uuid4())
        candidate = {
            "id": candidate_id,
            "video_id": video_id,
            **item,
            "status": "approved",
            "approval_method": "automatic",
            "aspect": aspect,
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
    dimensions = {"9:16": "480:854", "16:9": "854:480", "1:1": "480:480"}
    preview_path = OUTPUT_DIR / f"preview-{uuid.uuid4().hex}.mp4"
    preview_filters = f"scale={dimensions.get(aspect, '480:854')}:force_original_aspect_ratio=increase,crop={dimensions.get(aspect, '480:854')}"
    caption_file = OUTPUT_DIR / f"preview-{uuid.uuid4().hex}.ass"
    try:
        transcript_segments = job.get("transcript") or []
        if transcript_segments:
            _build_caption_ass(
                transcript_segments, float(candidate["start"]), float(candidate["end"]),
                aspect, "bold-pop", "bottom", caption_file
            )
            escaped_ass_path = str(caption_file).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            preview_filters += f",subtitles='{escaped_ass_path}'"
        command = [
            "ffmpeg", "-y", "-ss", str(candidate["start"]), "-i", str(source_video),
            "-t", str(duration), "-vf", preview_filters,
            "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(preview_path),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=240)
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
    return value.replace("\\\\", "").replace("{", "(").replace("}", ")").replace("\\n", " ").replace("\\r", " ")


def _build_caption_ass(segments: list[dict], clip_start: float, clip_end: float,
                       aspect: str, style: str, position: str, destination: Path) -> None:
    themes = {
        "bold-pop": ("&H006AF3C8", 1, 5, 2),
        "creator-yellow": ("&H005CE4FF", 1, 5, 2),
        "clean": ("&H00FFFFFF", 0, 2, 1),
    }
    highlight, bold, outline, shadow = themes[style]
    font_size = 58 if aspect == "9:16" else (46 if aspect == "1:1" else 40)
    alignment = 8 if position == "top" else 2
    margin_v = 110 if position == "top" else (190 if aspect == "9:16" else 90)
    play_width, play_height = (1920, 1080) if aspect == "16:9" else (1080, 1920)
    header = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {play_width}
PlayResY: {play_height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},{highlight},&H00FFFFFF,&HCC000000,&H80000000,{bold},0,1,{outline},{shadow},{alignment},70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues = []
    for segment in segments:
        try:
            absolute_start = float(segment.get("start", 0))
            absolute_end = float(segment.get("end", 0))
        except (TypeError, ValueError):
            continue
        start = max(absolute_start, clip_start)
        end = min(absolute_end, clip_end)
        words = str(segment.get("text", "")).split()
        if end <= start or not words:
            continue
        duration_cs = max(len(words), int((end - start) * 100))
        base, remainder = divmod(duration_cs, len(words))
        karaoke = []
        for index, word in enumerate(words):
            safe_word = _ass_escape_word(word)
            if safe_word:
                word_cs = base + (1 if index < remainder else 0)
                karaoke.append(f"{{\\k{word_cs}}}{safe_word}")
        if karaoke:
            dialogues.append(
                f"Dialogue: 0,{_ass_time(start - clip_start)},{_ass_time(end - clip_start)},Default,,0,0,0,,{' '.join(karaoke)}"
            )
    destination.write_text(header + "\\n".join(dialogues) + "\\n", encoding="utf-8")


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
    dimensions = {"9:16": "1080:1920", "16:9": "1920:1080", "1:1": "1080:1080"}
    filters = f"scale={dimensions[request.aspect]}:force_original_aspect_ratio=increase,crop={dimensions[request.aspect]}"
    caption_file = None
    if request.captions:
        transcript_segments = job.get("transcript") or []
        if not transcript_segments:
            raise HTTPException(409, "Transcribe this source video before rendering captions.")
        caption_file = OUTPUT_DIR / f"{clip_id}-captions.ass"
        try:
            _build_caption_ass(
                transcript_segments, request.start, request.end, request.aspect,
                request.caption_style, request.caption_position, caption_file
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
        "-threads", "1", "-c:v", "libx264", "-preset", "veryfast", "-crf", {"standard": "29", "high": "25", "maximum": "21"}[request.quality],
        "-c:a", "aac", "-threads", "1", "-movflags", "+faststart", str(output),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
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
    return {"clip_id": clip_id, "status": "rendered", "file": str(output)}
