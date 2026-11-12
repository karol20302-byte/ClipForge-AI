# ClipForge-AI

AI-assisted short-form video clipping studio with automatic clip-candidate approval.

## Current starter scope

- FastAPI backend for uploading source videos, listing clip candidates, rendering clips, and downloading MP4 exports.
- Mobile-first static dashboard in `frontend/` for the upload-to-export workflow.
- Local Whisper transcription and timestamped segments.
- Transcript-window highlight scoring, ranked by an optional OpenAI model when `OPENAI_API_KEY` is configured; otherwise a local heuristic ranks candidates.
- FFmpeg-based vertical MP4 rendering.
- Automatically approve generated clip candidates so they can be rendered without a manual approval step.
- Keep the approval endpoint available for manual rejection or status overrides.
- HyperFrames integration is an adapter boundary for branded motion graphics.

## Architecture

```text
Browser UI -> FastAPI -> job storage
                       -> Whisper transcription
                       -> candidate clip suggestions
                       -> automatic candidate approval
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

Open http://localhost:8000/docs for the API. Serve the `frontend/` directory as a static site and enter the API address in the dashboard.

Set `CLIPFORGE_DATA_DIR` to choose where uploads and rendered files are stored. To enable LLM-based highlight ranking, set `OPENAI_API_KEY` on the backend (never commit it); optionally set `OPENAI_MODEL`, which defaults to `gpt-4o-mini`. Without a key, local heuristic scoring is used.

## Important

- Only upload or process media you own or are authorized to use.
- Generated clip candidates are automatically marked approved and can be rendered immediately. This does not publish or upload them to social platforms.
- Whisper model downloads and inference require compute and may be slow on CPU.
- Do not expose this development server directly to the public internet. Add authentication, upload limits, malware scanning, private object storage, and a durable job queue before production deployment.

## Repository integrations

- [HyperFrames](https://github.com/heygen-com/hyperframes): HTML/CSS-driven video compositions.
- [FFmpeg](https://github.com/FFmpeg/FFmpeg): media probing, trimming, scaling, subtitles, and encoding.
- [Whisper](https://github.com/openai/whisper): speech transcription and timestamps.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp): optional import adapter for content the user is authorized to download. It is intentionally not enabled in the initial upload-only API.

## Status

Initial backend scaffold with automatic candidate approval and optional LLM-based transcript highlight ranking. The detector ranks transcript windows; it does not yet analyze visual scenes, audio energy, or audience-retention analytics. Cloud deployment and production hardening remain ongoing. Optional durable metadata and media storage are implemented through Cloudflare D1 and R2; they must be provisioned and configured on the backend before they are active. The initial mobile dashboard is available in `frontend/`.


## Durable cloud storage (optional)

For durable deployments, configure Cloudflare D1 for metadata and Backblaze B2 for media objects. The backend restores job and clip records from D1 at startup and stores source videos and rendered MP4 files in Backblaze B2. D1 stores JSON records only; video bytes remain in object storage.

Backblaze B2 offers 10 GB free storage. For US West, use the exact endpoint shown in your bucket details (commonly `https://s3.us-west-004.backblazeb2.com`) and region `us-west-004`; use path-style addressing. Review current usage limits before uploading large media.

Set these Render environment variables (never commit their values):

- `CLOUDFLARE_ACCOUNT_ID`: Cloudflare account ID (also used by the existing Workers AI transcription integration).
- `CLOUDFLARE_D1_DATABASE_ID`: D1 database UUID.
- `CLOUDFLARE_D1_API_TOKEN`: API token with D1 Read and D1 Write permissions.
- `B2_ENDPOINT`: the exact S3 endpoint shown in your Backblaze bucket details.
- `B2_KEY_ID` and `B2_APPLICATION_KEY`: Backblaze application key credentials.
- `STORED_BUCKET_NAME`: exact bucket name created in Backblaze B2.

Create the D1 tables automatically on backend startup. The backend must have both D1 and Backblaze B2 configured; it intentionally refuses a partial cloud-storage configuration to avoid persisting metadata while leaving media on ephemeral disk. Existing in-memory/local-disk behavior remains available when no cloud-storage variables are configured. Existing jobs created before cloud storage is enabled are not migrated automatically; upload those source videos again after setup.
