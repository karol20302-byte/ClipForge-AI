# Upstream integrations

ClipForge-AI uses these upstream projects as separate components rather than copying their source into this repository. The exact upstream revisions are recorded in `upstreams.json` so updates can be reviewed and pinned deliberately.

| Project | ClipForge role | Current connection |
|---|---|---|
| [HyperFrames](https://github.com/heygen-com/hyperframes) | Optional motion-graphics composition | Pinned upstream reference; renderer adapter remains a follow-up |
| [FFmpeg](https://github.com/FFmpeg/FFmpeg) | Media processing and export | Installed in the backend container as a system executable |
| [Whisper](https://github.com/openai/whisper) | Transcription | Python dependency used by the backend |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Optional source import | Python dependency added; import endpoint is not enabled by default |

## Runtime notes

- FFmpeg is an executable, not a Python package. The backend Docker image installs it with the operating-system package manager.
- Whisper is installed from its Python package. Model weights are downloaded when a model is first loaded, so inference needs disk space and CPU/GPU resources.
- HyperFrames requires Node.js 22+ and FFmpeg. It is registered as the future composition/rendering adapter; it is not yet invoked by the FastAPI API.
- yt-dlp is available as a dependency for a future opt-in importer. Do not use it to download media without the rights or permission to do so. It is not exposed as a public URL-download endpoint.

## Local setup

From the repository root:

```bash
cd backend
python -m pip install -r requirements.txt
ffmpeg -version
```

To work on the HyperFrames renderer separately, install Node.js 22+ and follow the upstream project's own setup instructions. Review its license and changes before upgrading the pinned revision.

## Current integration boundary

The existing backend can transcribe uploaded files with Whisper and render approved clips with FFmpeg. Candidate suggestions are currently a baseline transcript/time grouping, not semantic highlight detection. HyperFrames rendering and yt-dlp importing are not yet wired into API routes. The human approval gate remains in place.
