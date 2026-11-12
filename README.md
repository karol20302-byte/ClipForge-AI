# ClipForge-AI

AI-assisted short-form video clipping studio with a human approval gate.

## Current starter scope

- FastAPI backend for uploading source videos and listing clip candidates.
- Local Whisper transcription and timestamped segments.
- FFmpeg-based vertical MP4 rendering.
- Human review before rendering/export.
- HyperFrames integration is an adapter boundary for branded motion graphics.

## Architecture

```text
Browser UI -> FastAPI -> job storage
                       -> Whisper transcription
                       -> candidate clip suggestions
                       -> human approval
                       -> FFmpeg render -> optional HyperFrames composition
```

## Run locally

Requirements: Python 3.11+, FFmpeg installed and available on PATH.

```bash
cd backend
python -m venv .venv
# activate the environment
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs.

Set `CLIPFORGE_DATA_DIR` to choose where uploads and rendered files are stored.

## Important

- Only upload or process media you own or are authorized to use.
- The app does not publish automatically. Every suggested clip must be approved by a human.
- Whisper model downloads and inference require compute and may be slow on CPU.
- Do not expose this development server directly to the public internet. Add authentication, upload limits, malware scanning, private object storage, and a durable job queue before production deployment.

## Repository integrations

- [HyperFrames](https://github.com/heygen-com/hyperframes): HTML/CSS-driven video compositions.
- [FFmpeg](https://github.com/FFmpeg/FFmpeg): media probing, trimming, scaling, subtitles, and encoding.
- [Whisper](https://github.com/openai/whisper): speech transcription and timestamps.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp): optional import adapter for content the user is authorized to download. It is intentionally not enabled in the initial upload-only API.

## Status

Initial backend scaffold. Cloud deployment, authentication, persistent database, background workers, and the web review interface remain to be implemented.
