const DEFAULT_API = "https://clipforge-ai-04gj.onrender.com";
const $ = (selector) => document.querySelector(selector);
const savedApi = localStorage.getItem("clipforge-api");
const state = { api: (!savedApi || savedApi === "https://clipforge-backend-i21j.onrender.com") ? DEFAULT_API : savedApi, apiKey: localStorage.getItem("clipforge-key") || "", sessionToken: localStorage.getItem("clipforge-session") || "", authenticated: false, videoId: null, clips: [], youtubeImportArmed: false, autoGenerateAfterUpload: false, workflowRunning: false, renderQueueRunning: false };

$("#api-url").value = state.api;
$("#api-key").value = state.apiKey;
$("#save-api").addEventListener("click", () => {
  state.api = $("#api-url").value.trim().replace(/\/$/, "");
  localStorage.setItem("clipforge-api", state.api);
  toast("Backend address saved");
});
$("#save-key").addEventListener("click", () => {
  state.apiKey = $("#api-key").value.trim();
  localStorage.setItem("clipforge-key", state.apiKey);
  toast("API key saved in this browser");
});
$("#open-import").addEventListener("click", () => $("#video-file").click());
const unifiedDropzone = $("#unified-dropzone");
["dragenter", "dragover"].forEach(type => unifiedDropzone.addEventListener(type, event => {
  event.preventDefault();
  unifiedDropzone.classList.add("is-dragover");
}));
["dragleave", "drop"].forEach(type => unifiedDropzone.addEventListener(type, event => {
  event.preventDefault();
  unifiedDropzone.classList.remove("is-dragover");
}));
unifiedDropzone.addEventListener("drop", event => {
  const file = [...(event.dataTransfer?.files || [])].find(item => item.type.startsWith("video/") || /\\.(mp4|mov|mkv|webm|m4v)$/i.test(item.name));
  if (!file) { setStatus("#upload-status", "Drop a supported video file (MP4, MOV, MKV, WEBM or M4V)."); return; }
  const transfer = new DataTransfer();
  transfer.items.add(file);
  $("#video-file").files = transfer.files;
  $("#video-file").dispatchEvent(new Event("change", { bubbles: true }));
});
$("#video-file").addEventListener("change", () => {
  const file = $("#video-file").files[0];
  $("#file-label").textContent = file ? file.name : "Drop a video or tap + to add";

  $("#upload-button").disabled = !file;
  if (file && state.youtubeImportArmed) {
    state.youtubeImportArmed = false;
    state.autoGenerateAfterUpload = true;
    uploadVideo();
  }
});
$("#youtube-use-file").addEventListener("click", () => {
  if (!$("#youtube-authorized").checked) {
    setStatus("#youtube-status", "Please confirm that you own this video or have permission to download and edit it.");
    return;
  }
  if (!$("#youtube-url").value.trim()) {
    setStatus("#youtube-status", "Paste and preview the YouTube link first.");
    return;
  }
  state.youtubeImportArmed = true;
  $("#video-file").click();
});
$("#hero-auto-edit").addEventListener("click", () => {
  $("#workflow-panel").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("#upload-button").addEventListener("click", () => {
  state.youtubeImportArmed = false;
  state.autoGenerateAfterUpload = false;
  uploadVideo();
});
$("#media-import").addEventListener("click", importMediaUrl);
$("#youtube-preview-button").addEventListener("click", previewYouTubeLink);
$("#process-button").addEventListener("click", runFullWorkflow);
function randomCaptionSettings() {
  const styles = ["bold-pop", "creator-yellow", "clean"];
  const positions = ["bottom", "top"];
  return {
    caption_style: styles[Math.floor(Math.random() * styles.length)],
    caption_position: positions[Math.floor(Math.random() * positions.length)]
  };
}

$("#reset-data").addEventListener("click", async () => {
  if (!state.apiKey) {
    toast("Save your private API key before resetting");
    return;
  }
  const confirmation = window.prompt('This permanently deletes ALL stored videos, rendered clips, transcripts and project records. Type RESET ALL CLIPFORGE DATA to continue:');
  if (confirmation !== "RESET ALL CLIPFORGE DATA") {
    if (confirmation !== null) toast("Reset cancelled: confirmation did not match");
    return;
  }
  const button = $("#reset-data");
  button.disabled = true;
  button.textContent = "Resetting…";
  try {
    const result = await api("/api/admin/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation })
    });
    state.videoId = null;
    state.clips = [];
    $("#clips-list").innerHTML = '<div class="empty-state"><span>✳</span><strong>Your workspace is clear</strong><p>Upload a new video to start again.</p></div>';
    $("#clip-count").textContent = "0 clips";
    $("#video-summary").innerHTML = '<span class="summary-dot"></span><span>No video uploaded yet</span>';
    $("#process-button").disabled = true;
    $("#video-file").value = "";
    $("#file-label").textContent = "Choose a video";
    setStatus("#upload-status", "All project data has been cleared.");
    toast("Reset complete. Deleted " + result.deleted_cloud_objects.uploads + " upload objects and " + result.deleted_cloud_objects.renders + " render objects.");
  } catch (error) {
    toast("Reset failed: " + error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Reset all project data";
  }
});


async function initPrivateGoogleLogin() {
  const status = $("#login-status");
  try {
    status.textContent = "Connecting to secure sign-in…";
    const response = await fetch(state.api + "/auth/config", { cache: "no-store" });
    const config = await response.json();
    if (!response.ok || !config.configured || !config.client_id) {
      throw new Error("Google sign-in is not configured on the backend yet. Add GOOGLE_CLIENT_ID and CLIPFORGE_ALLOWED_EMAIL in Render environment variables.");
    }
    if (state.sessionToken && config.session_configured) {
      try {
        const saved = await fetch(state.api + "/auth/session", {
          headers: { "Authorization": "Bearer " + state.sessionToken },
          cache: "no-store"
        });
        if (saved.ok) {
          state.authenticated = true;
          $("#login-gate").hidden = true;
          $("#app-shell").hidden = false;
          status.textContent = "Signed in · saved session";
          await prepareFreshWorkspace();
          return;
        }
      } catch (_) {}
      state.sessionToken = "";
      localStorage.removeItem("clipforge-session");
    }
    if (!window.google?.accounts?.id) {
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "https://accounts.google.com/gsi/client";
        script.async = true;
        script.defer = true;
        script.onload = resolve;
        script.onerror = () => reject(new Error("Google sign-in could not load. Check your connection."));
        document.head.appendChild(script);
      });
    }
    window.google.accounts.id.initialize({
      client_id: config.client_id,
      callback: handleGoogleCredential,
      auto_select: false,
      cancel_on_tap_outside: true
    });
    window.google.accounts.id.renderButton($("#google-signin-button"), {
      type: "standard", theme: "filled_black", size: "large",
      text: "continue_with", shape: "pill", width: 280
    });
    status.textContent = "Only the approved Google account can enter this workspace.";
  } catch (error) {
    status.textContent = error.message;
  }
}

async function handleGoogleCredential(response) {
  const status = $("#login-status");
  status.textContent = "Verifying your Google account…";
  try {
    const verification = await fetch(state.api + "/auth/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credential: response.credential })
    });
    const result = await verification.json().catch(() => ({}));
    if (!verification.ok || !result.ok) throw new Error(result.detail || "This account is not allowed to use ClipForge.");
    state.sessionToken = result.session_token;
    localStorage.setItem("clipforge-session", result.session_token);
    state.authenticated = true;
    $("#login-gate").hidden = true;
    $("#app-shell").hidden = false;
    status.textContent = "Signed in.";
    await prepareFreshWorkspace();
  } catch (error) {
    state.sessionToken = "";
    status.textContent = error.message;
    if (window.google?.accounts?.id) window.google.accounts.id.disableAutoSelect();
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.apiKey) headers.set("X-API-Key", state.apiKey);
  if (state.sessionToken) headers.set("Authorization", "Bearer " + state.sessionToken);
  const maxBusyRetries = 12;
  for (let attempt = 0; ; attempt++) {
    const response = await fetch(state.api + path, { ...options, headers });
    const body = await response.json().catch(() => ({}));
    const message = body.detail || body.message || "";
    const renderBusy = response.status === 503 && /another video task|render.*busy|task.*using.*render|ffmpeg.*busy/i.test(message);
    if (renderBusy && attempt < maxBusyRetries) {
      setStatus("#process-status", "Another video is finishing its render. Waiting automatically… (" + (attempt + 1) + "/" + maxBusyRetries + ")");
      await new Promise(resolve => window.setTimeout(resolve, 3000));
      continue;
    }
    if (!response.ok) throw new Error(message || "Request failed");
    return body;
  }
}

async function prepareFreshWorkspace() {
  // Preserve only connection settings. Every page load starts with an empty
  // temporary workspace, including a normal browser refresh or reopening.
  state.videoId = null;
  state.clips = [];
  state.workflowRunning = false;
  state.renderQueueRunning = false;
  $("#video-file").value = "";
  $("#file-label").textContent = "MP4 · MOV · MKV · WEBM";
  $("#upload-button").disabled = true;
  $("#process-button").disabled = true;
  $("#clips-list").innerHTML = '<div class="empty-state"><span>✳</span><strong>Your workspace is fresh</strong><p>Upload a new video to start again.</p></div>';
  $("#clip-count").textContent = "0 clips";
  $("#video-summary").innerHTML = '<span class="summary-dot"></span><span>No video uploaded yet</span>';
  $("#generation-progress").hidden = true;
  $("#upload-progress").hidden = true;

  // Clear this app's browser session/cache data without removing the saved
  // backend URL or private API key from localStorage.
  try { sessionStorage.clear(); } catch (_) {}
  if ("caches" in window) {
    try {
      const cacheNames = await caches.keys();
      await Promise.all(cacheNames.map(name => caches.delete(name)));
    } catch (_) {}
  }

  if (!state.apiKey) {
    setStatus("#upload-status", "Fresh workspace is ready. Save your private API key to enable automatic server-side cleanup on refresh.");
    return;
  }

  setStatus("#upload-status", "Refreshing workspace and clearing temporary files…");
  try {
    const result = await api("/api/admin/prepare-session", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    toast("Fresh workspace ready · temporary files cleared");
    setStatus("#upload-status", "Fresh temporary workspace ready. Previous uploads, clips, transcripts, cached files and project records were cleared.");
  } catch (error) {
    setStatus("#upload-status", "Workspace cleanup failed: " + error.message + ". Check your API connection and key before uploading.");
  }
}
function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2600);
}
function setStatus(selector, message) { $(selector).textContent = message; }

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 MB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}
function uploadVideo() {
  const file = $("#video-file").files[0];
  if (!file) return;
  const button = $("#upload-button");
  const progress = $("#upload-progress");
  const fill = $("#upload-fill");
  const startedAt = Date.now();
  let lastLoaded = 0;
  let lastTime = startedAt;
  let smoothedSpeed = 0;
  button.disabled = true;
  progress.hidden = false;
  fill.style.width = "0%";
  $("#upload-phase").textContent = "INITIALIZING TRANSFER";
  $("#upload-percent").textContent = "0%";
  $("#upload-bytes").textContent = "0 MB / " + formatBytes(file.size);
  $("#upload-speed").textContent = "Connecting…";
  $("#upload-eta").textContent = "Calculating time…";
  setStatus("#upload-status", "Preparing secure upload…");

  const xhr = new XMLHttpRequest();
  xhr.open("POST", state.api + "/api/videos");
  if (state.apiKey) xhr.setRequestHeader("X-API-Key", state.apiKey);
  if (state.sessionToken) xhr.setRequestHeader("Authorization", "Bearer " + state.sessionToken);
  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
    const now = Date.now();
    const elapsed = (now - lastTime) / 1000;
    if (elapsed >= 0.35) {
      const instantSpeed = (event.loaded - lastLoaded) / elapsed;
      smoothedSpeed = smoothedSpeed ? smoothedSpeed * 0.65 + instantSpeed * 0.35 : instantSpeed;
      lastLoaded = event.loaded;
      lastTime = now;
    }
    fill.style.width = percent + "%";
    $("#upload-percent").textContent = percent + "%";
    $("#upload-bytes").textContent = formatBytes(event.loaded) + " / " + formatBytes(event.total);
    $("#upload-phase").textContent = percent >= 100 ? "FINALIZING VIDEO" : "TRANSFERRING VIDEO";
    $("#upload-speed").textContent = smoothedSpeed > 0 ? formatBytes(smoothedSpeed) + "/s" : "Measuring speed…";
    const remaining = smoothedSpeed > 0 ? Math.ceil((event.total - event.loaded) / smoothedSpeed) : null;
    $("#upload-eta").textContent = remaining !== null ? (remaining <= 1 ? "Almost there" : remaining + " sec remaining") : "Calculating time…";
    setStatus("#upload-status", percent >= 100 ? "Transfer sent. Waiting for server confirmation…" : "Uploading your video…");
  });
  xhr.addEventListener("load", () => {
    let result = {};
    try { result = JSON.parse(xhr.responseText || "{}"); } catch (_) {}
    if (xhr.status >= 200 && xhr.status < 300) {
      state.videoId = result.id;
      $("#video-summary").innerHTML = '<span class="summary-dot"></span><span></span>';
      $("#video-summary span:last-child").textContent = result.filename;
      $("#process-button").disabled = false;
      fill.style.width = "100%";
      $("#upload-percent").textContent = "100%";
      $("#upload-phase").textContent = "TRANSFER COMPLETE";
      $("#upload-speed").textContent = "Verified by server";
      $("#upload-eta").textContent = "Ready for AI processing";
      progress.classList.add("is-complete");
      setStatus("#upload-status", "Upload complete. Your video is ready to transcribe.");
      toast("Video uploaded successfully");
      if (state.autoGenerateAfterUpload) {
        state.autoGenerateAfterUpload = false;
        setStatus("#youtube-status", "Upload complete. Starting transcription and highlight detection…");
        runFullWorkflow();
      }
    } else {
      const message = result.detail || result.message || "Upload failed (" + xhr.status + ")";
      $("#upload-phase").textContent = "TRANSFER INTERRUPTED";
      progress.classList.add("is-error");
      setStatus("#upload-status", message);
      button.disabled = false;
    }
  });
  xhr.addEventListener("error", () => {
    $("#upload-phase").textContent = "CONNECTION ERROR";
    progress.classList.add("is-error");
    setStatus("#upload-status", "Network error. Check your connection and try again.");
    button.disabled = false;
  });
  xhr.addEventListener("abort", () => {
    $("#upload-phase").textContent = "TRANSFER CANCELLED";
    setStatus("#upload-status", "Upload was cancelled.");
    button.disabled = false;
  });
  const form = new FormData();
  form.append("file", file);
  xhr.send(form);
}

async function importMediaUrl() {
  const input = $("#youtube-url");
  const button = $("#media-import");
  const progress = $("#external-download-progress");
  const title = $("#external-download-title");
  const detail = $("#external-download-detail");
  const stage = $("#external-download-stage");
  const elapsed = $("#external-download-elapsed");
  const url = input.value.trim();
  if (!url) { setStatus("#youtube-status", "Paste a video URL first."); return; }
  if (!$("#youtube-authorized").checked) { setStatus("#youtube-status", "Please confirm that you own this video or have permission to download and edit it."); return; }
  const startedAt = Date.now();
  let elapsedTimer;
  const showExternalProgress = (heading, message, currentStage) => {
    progress.hidden = false;
    title.textContent = heading;
    detail.textContent = message;
    stage.textContent = currentStage;
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    elapsed.textContent = Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
  };
  button.disabled = true;
  button.textContent = "Connecting…";
  showExternalProgress("CONNECTING TO SOURCE", "Sending the external video link to ClipForge…", "SOURCE CHECK");
  elapsedTimer = window.setInterval(() => showExternalProgress(title.textContent, detail.textContent, stage.textContent), 1000);
  setStatus("#youtube-status", "Connecting to the external video source…");
  try {
    const queued = await api("/api/videos/import-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url })
    });
    state.videoId = queued.id;
    $("#process-button").disabled = true;
    $("#video-summary").innerHTML = '<span class="summary-dot"></span><span></span>';
    $("#video-summary span:last-child").textContent = "External import queued…";
    showExternalProgress("SOURCE ACCEPTED", "ClipForge is checking the video and preparing the download…", "SOURCE CHECK");
    setStatus("#youtube-status", "External link accepted. Download progress will appear here.");

    let completed = false;
    for (let attempt = 0; attempt < 900; attempt++) {
      await new Promise(resolve => window.setTimeout(resolve, 2000));
      const status = await api("/api/videos/" + encodeURIComponent(queued.id) + "/import-status", { cache: "no-store" });
      if (status.status === "failed") throw new Error(status.error || "Video import failed.");
      if (status.status === "uploaded") {
        completed = true;
        $("#video-summary span:last-child").textContent = status.filename || "Video imported";
        $("#process-button").disabled = false;
        progress.classList.add("is-complete");
        showExternalProgress("DOWNLOAD COMPLETE", "Your external video is imported and ready for AI processing.", "READY TO EDIT");
        setStatus("#upload-status", "Video imported. Starting the automatic Shorts workflow.");
        setStatus("#youtube-status", "Download complete. Starting transcription, highlight detection and automatic Shorts rendering…");
        toast("Video imported — generating Shorts");
        runFullWorkflow();
        break;
      }
      const downloading = status.status === "downloading";
      const heading = downloading ? "DOWNLOADING EXTERNAL VIDEO" : status.status === "checking" ? "CHECKING VIDEO SOURCE" : "WAITING FOR IMPORT WORKER";
      const percent = Number.isFinite(Number(status.progress)) ? " " + Math.round(Number(status.progress)) + "%" : "";
      const message = status.detail || (downloading ? "Fetching video data from the external platform. This can take a while for long videos…" : status.status === "checking" ? "Validating the source and reading video details…" : "Your import is queued. ClipForge is preparing the downloader…");
      const currentStage = downloading ? "DOWNLOADING" + percent : status.status === "checking" ? "SOURCE CHECK" : "QUEUED";
      showExternalProgress(heading, message, currentStage);
      setStatus("#youtube-status", message);
      if (status.filename && status.filename !== "Checking video link…") {
        $("#video-summary span:last-child").textContent = status.filename;
      }
    }
    if (!completed) throw new Error("Import is taking longer than expected. It may still be running; check again shortly.");
  } catch (error) {
    progress.classList.add("is-error");
    const rawMessage = String(error?.message || "");
    const extractionFailure = /player response|sign in to confirm|no downloadable formats|unable to download|requested format/i.test(rawMessage);
    const friendlyMessage = extractionFailure
      ? "YouTube could not provide a downloadable video to the server. Please use Choose video file instead, or try again later."
      : "The video link could not be imported. Please check the URL or choose the video file instead.";
    showExternalProgress("IMPORT NEEDS ATTENTION", friendlyMessage, "UPLOAD FILE INSTEAD");
    setStatus("#youtube-status", friendlyMessage);
    if (!progress.querySelector(".import-fallback-button")) {
      const fallback = document.createElement("button");
      fallback.type = "button";
      fallback.className = "button button-secondary import-fallback-button";
      fallback.textContent = "Choose video file instead";
      fallback.addEventListener("click", () => $("#youtube-use-file").click());
      progress.append(fallback);
    }
    if (!state.videoId) $("#process-button").disabled = true;
  } finally {
    window.clearInterval(elapsedTimer);
    button.disabled = false;
    button.textContent = "Import link";
  }
}

function previewYouTubeLink() {
  const input = $("#youtube-url");
  const status = $("#youtube-status");
  const container = $("#youtube-preview");
  let parsed;
  try { parsed = new URL(input.value.trim()); } catch (_) {
    status.textContent = "Enter a valid YouTube video link.";
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
  let videoId = "";
  if (host === "youtu.be") {
    videoId = parsed.pathname.split("/").filter(Boolean)[0] || "";
  } else if (host === "youtube.com" || host === "m.youtube.com" || host === "music.youtube.com") {
    if (parsed.pathname === "/watch") videoId = parsed.searchParams.get("v") || "";
    else {
      const parts = parsed.pathname.split("/").filter(Boolean);
      if (["shorts", "embed", "live"].includes(parts[0])) videoId = parts[1] || "";
    }
  }
  if (!/^[A-Za-z0-9_-]{11}$/.test(videoId)) {
    status.textContent = "That link format was not recognized. Use a standard YouTube video, Shorts, or youtu.be link.";
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const frame = document.createElement("iframe");
  frame.src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(videoId);
  frame.title = "YouTube video preview";
  frame.loading = "lazy";
  frame.referrerPolicy = "strict-origin-when-cross-origin";
  frame.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
  frame.allowFullscreen = true;
  frame.style.width = "100%";
  frame.style.aspectRatio = "16 / 9";
  frame.style.border = "0";
  frame.style.borderRadius = "12px";
  container.replaceChildren(frame);
  container.hidden = false;
  status.textContent = "Preview loaded. If you own this video or have permission to edit it, use Import link to fetch it directly into ClipForge.";
}

function updateGenerationStep(name, status) {
  const step = $("#generation-step-" + name);
  if (!step) return;
  step.classList.remove("is-active", "is-done", "is-error");
  if (status) step.classList.add("is-" + status);
  const stateLabel = step.querySelector(".step-state");
  if (stateLabel) stateLabel.textContent = status === "active" ? "IN PROGRESS" : status === "done" ? "COMPLETE" : status === "error" ? "FAILED" : "WAITING";
}
function showGenerationProgress(title, detail) {
  const panel = $("#generation-progress");
  panel.hidden = false;
  panel.classList.remove("is-error", "is-complete");
  $("#generation-title").textContent = title;
  $("#generation-detail").textContent = detail;
}
async function runFullWorkflow() {
  if (!state.videoId || state.workflowRunning) return;
  state.workflowRunning = true;
  const button = $("#process-button");
  button.disabled = true;
  button.textContent = "Generating clips…";
  ["transcribe", "detect", "approve"].forEach(name => updateGenerationStep(name, ""));
  showGenerationProgress("Clip generation started", "Connecting to the transcription service…");
  updateGenerationStep("transcribe", "active");
  try {
    const language = "auto";
    const task = "transcribe";
    state.renderQuality = $("#render-quality").value;
    const transcript = await api("/api/videos/" + encodeURIComponent(state.videoId) + "/transcribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language, task })
    });
    const languageNames = { en: "English", ml: "Malayalam", hi: "Hindi", ta: "Tamil", kn: "Kannada", te: "Telugu", bn: "Bengali", mr: "Marathi", ur: "Urdu", es: "Spanish", fr: "French", de: "German", ar: "Arabic", pt: "Portuguese", ru: "Russian", ja: "Japanese", ko: "Korean", zh: "Chinese" };
    const detected = languageNames[transcript.language] || "automatically detected";
    updateGenerationStep("transcribe", "done");
    updateGenerationStep("detect", "active");
    $("#generation-title").textContent = "Audio transcribed";
    $("#generation-detail").textContent = "Now analysing the transcript to find strong moments…";
    setStatus("#process-status", "Transcription complete (" + detected + "). Finding highlights…");

    const maxDuration = Number($("#clip-length").value);
    const minDuration = Math.min(20, Math.max(10, Math.floor(maxDuration / 3)));
    const settings = new URLSearchParams({ min_seconds: String(minDuration), max_seconds: String(maxDuration), aspect: "9:16" });
    const result = await api("/api/videos/" + encodeURIComponent(state.videoId) + "/suggest-clips?" + settings.toString(), { method: "POST" });
    updateGenerationStep("detect", "done");
    updateGenerationStep("approve", "active");
    $("#generation-title").textContent = "Highlights found";
    $("#generation-detail").textContent = "Preparing " + (result.candidates || []).length + " clip candidates…";
    state.clips = result.candidates || [];
    renderClips();
    updateGenerationStep("approve", "done");
    $("#generation-progress").classList.add("is-complete");
    $("#generation-title").textContent = "Your clips are ready";
    $("#generation-detail").textContent = state.clips.length + " clip candidates found and automatically approved.";
    setStatus("#process-status", "Complete · " + (languageNames[transcript.language] || detected) + (task === "translate" ? " → English translation" : "") + " · " + state.clips.length + " clips found and automatically approved. Detection: " + (result.detection_method || "automatic") + ".");
    toast(state.clips.length + " approved clip candidates found");
  } catch (error) {
    $("#generation-progress").classList.add("is-error");
    $("#generation-title").textContent = "Generation needs attention";
    $("#generation-detail").textContent = error.message;
    const activeStep = document.querySelector(".generation-step.is-active");
    if (activeStep) { activeStep.classList.remove("is-active"); activeStep.classList.add("is-error"); const label = activeStep.querySelector(".step-state"); if (label) label.textContent = "FAILED"; }
    setStatus("#process-status", error.message);
    state.workflowRunning = false;
    button.disabled = false;
    button.textContent = "Try again";
    return;
  }
  state.workflowRunning = false;
  button.disabled = false;
  button.textContent = "Generate clips again";
}
function renderClips() {
  $("#clip-count").textContent = state.clips.length + (state.clips.length === 1 ? " clip" : " clips");
  const list = $("#clips-list");
  list.replaceChildren();
  list.classList.add("clip-reel-gallery");
  if (!state.clips.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = "<span>✳</span><strong>No clips found in this video</strong><p>Try a longer source video or adjust the backend clip duration settings.</p>";
    list.append(empty);
    return;
  }

  const jobs = [];
  state.clips.forEach((clip, index) => {
    const card = document.createElement("article");
    const isTopPick = index < 4;
    card.className = "clip-card reel-output-card " + (isTopPick ? "is-top-pick" : "is-alternate");
    const heading = document.createElement("div");
    heading.className = "reel-card-heading";
    const name = document.createElement("strong");
    name.textContent = "AI CLIPPER " + (index + 1);
    const score = document.createElement("span");
    score.className = "reel-card-score";
    score.textContent = Number.isFinite(Number(clip.score)) ? Math.round(Number(clip.score)) + " PTS" : "AI PICK";
    heading.append(name, score);

    const frame = document.createElement("div");
    frame.className = "reel-video-frame";
    const player = document.createElement("video");
    player.className = "highlight-player auto-ready-player reel-output-player";
    player.controls = true;
    player.playsInline = true;
    player.preload = "metadata";
    player.setAttribute("aria-label", "AI clipper " + (index + 1));
    const placeholder = document.createElement("div");
    placeholder.className = "clip-preparing reel-clip-preparing";
    placeholder.textContent = "✦ PREPARING SHORT " + String(index + 1).padStart(2, "0");
    frame.append(player, placeholder);

    const actions = document.createElement("div");
    actions.className = "reel-card-actions";
    const download = document.createElement("button");
    download.type = "button";
    download.className = "button button-secondary reel-download-button";
    download.textContent = "Download Short";
    download.disabled = true;
    download.addEventListener("click", () => {
      const objectUrl = player.dataset.objectUrl;
      if (!objectUrl) return;
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "clipforge-short-" + (index + 1) + ".mp4";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    });
    actions.append(download);
    card.append(heading, frame, actions);
    list.append(card);
    jobs.push({ clip, player, card, index, placeholder, download });
  });

  const status = document.createElement("div");
  status.className = "auto-render-status reel-gallery-status";
  status.textContent = "Preparing your Shorts…";
  list.prepend(status);

  $("#clips-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
  if (state.renderQueueRunning) return;
  state.renderQueueRunning = true;
  (async () => {
    let ready = 0;
    let failed = 0;
    try {
      for (let i = 0; i < jobs.length; i++) {
        status.textContent = "Rendering SHORT " + (i + 1) + " of " + jobs.length + "…";
        const item = jobs[i];
        const ok = await prepareClipForPlayback(item.clip, item.player, status, item.placeholder);
        if (ok) {
          ready++;
          item.download.disabled = false;
        } else {
          failed++;
        }
      }
      status.textContent = failed
        ? ready + " Shorts ready · " + failed + " need retry"
        : "All " + ready + " Shorts are ready to preview and download";
      status.classList.toggle("is-error", failed > 0);
      status.classList.toggle("is-complete", failed === 0);
      if (failed === 0) window.setTimeout(() => status.remove(), 3500);
    } finally {
      state.renderQueueRunning = false;
    }
  })();
}
async function prepareClipForPlayback(clip, player, status, placeholder) {
  try {
    await api("/api/clips/" + encodeURIComponent(clip.id) + "/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start: clip.start,
        end: clip.end,
        aspect: "9:16",
        captions: true,
        focus_x: 0.5,
        focus_y: 0.5,
        quality: state.renderQuality || "high",
        edit_style: "creator",
        ...randomCaptionSettings()
      })
    });

    const response = await fetch(state.api + "/api/clips/" + encodeURIComponent(clip.id) + "/download", {
      headers: { ...(state.apiKey ? { "X-API-Key": state.apiKey } : {}), ...(state.sessionToken ? { "Authorization": "Bearer " + state.sessionToken } : {}) }
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "Could not load rendered MP4 (" + response.status + ")");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    player.src = url;
    player.load();
    await new Promise((resolve, reject) => {
      if (player.readyState >= 3) return resolve();
      const ready = () => { cleanup(); resolve(); };
      const failed = () => { cleanup(); reject(new Error("The MP4 could not be decoded by this browser.")); };
      const cleanup = () => {
        player.removeEventListener("canplay", ready);
        player.removeEventListener("error", failed);
      };
      player.addEventListener("canplay", ready, { once: true });
      player.addEventListener("error", failed, { once: true });
      window.setTimeout(() => { cleanup(); reject(new Error("Timed out while preparing the MP4 player.")); }, 45000);
    });
    player.classList.add("is-ready");
    player.dataset.objectUrl = url;
    const downloadButton = player.closest(".reel-output-card")?.querySelector(".reel-download-button");
    if (downloadButton) downloadButton.disabled = false;
    if (placeholder) placeholder.remove();
    return true;
  } catch (error) {
    status.classList.add("is-error");
    status.textContent = "Clip preparation failed: " + error.message;
    if (placeholder) placeholder.textContent = "CLIP RENDER FAILED · " + error.message;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "button button-secondary";
    retry.textContent = "Retry";
    retry.addEventListener("click", () => {
      retry.remove();
      status.classList.remove("is-error");
      status.textContent = "Preparing clip again…";
      prepareClipForPlayback(clip, player, status, placeholder);
    }, { once: true });
    status.append(retry);
    return false;
  }
}
function formatTime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return Math.floor(value / 60) + ":" + String(value % 60).padStart(2, "0");
}



$("#sign-out").addEventListener("click", () => {
  state.sessionToken = "";
  state.apiKey = "";
  $("#api-key").value = "";
  localStorage.removeItem("clipforge-key");
  state.authenticated = false;
  state.videoId = null;
  state.clips = [];
  localStorage.removeItem("clipforge-session");
  if (window.google?.accounts?.id) window.google.accounts.id.disableAutoSelect();
  $("#app-shell").hidden = true;
  $("#login-gate").hidden = false;
  $("#login-status").textContent = "Signed out. Sign in with your approved Google account to continue.";
  toast("You have signed out of ClipForge");
});

// Restore the saved 30-day session or request Google sign-in.
initPrivateGoogleLogin();
