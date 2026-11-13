const DEFAULT_API = "https://clipforge-ai-04gj.onrender.com";
const $ = (selector) => document.querySelector(selector);
const savedApi = localStorage.getItem("clipforge-api");
const state = { api: (!savedApi || savedApi === "https://clipforge-backend-i21j.onrender.com") ? DEFAULT_API : savedApi, apiKey: localStorage.getItem("clipforge-key") || "", videoId: null, clips: [] };

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
$("#video-file").addEventListener("change", () => {
  const file = $("#video-file").files[0];
  $("#file-label").textContent = file ? file.name : "Choose a video";
  $("#upload-button").disabled = !file;
});
$("#upload-button").addEventListener("click", uploadVideo);
$("#media-import").addEventListener("click", importMediaUrl);
$("#process-button").addEventListener("click", runFullWorkflow);

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.apiKey) headers.set("X-API-Key", state.apiKey);
  const response = await fetch(state.api + path, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.message || "Request failed");
  return body;
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
  const input = $("#media-url");
  const button = $("#media-import");
  const url = input.value.trim();
  if (!url) { setStatus("#media-status", "Paste a video URL first."); return; }
  button.disabled = true;
  button.textContent = "Starting…";
  setStatus("#media-status", "Checking the authorized direct video-file link…");
  try {
    const result = await api("/api/videos/import-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url })
    });
    state.videoId = result.id;
    $("#video-summary").innerHTML = '<span class="summary-dot"></span><span></span>';
    $("#video-summary span:last-child").textContent = result.filename;
    $("#process-button").disabled = false;
    
    setStatus("#upload-status", "Video imported. Ready to transcribe.");
    setStatus("#media-status", "Source imported into ClipForge. Next, transcribe audio and detect highlights.");
    toast("Video imported successfully");
  } catch (error) {
    setStatus("#media-status", error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Import & prepare";
  }
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
  if (!state.videoId) return;
  const button = $("#process-button");
  button.disabled = true;
  button.textContent = "Generating clips…";
  ["transcribe", "detect", "approve"].forEach(name => updateGenerationStep(name, ""));
  showGenerationProgress("Clip generation started", "Connecting to the transcription service…");
  updateGenerationStep("transcribe", "active");
  try {
    const language = $("#transcription-language").value;
    const task = $("#transcription-task").value;
    const transcript = await api("/api/videos/" + encodeURIComponent(state.videoId) + "/transcribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language, task })
    });
    const languageNames = { en: "English", ml: "Malayalam", hi: "Hindi", ta: "Tamil", kn: "Kannada", te: "Telugu", bn: "Bengali", mr: "Marathi", ur: "Urdu", es: "Spanish", fr: "French", de: "German", ar: "Arabic", pt: "Portuguese", ru: "Russian", ja: "Japanese", ko: "Korean", zh: "Chinese" };
    const detected = languageNames[transcript.language] || (language === "auto" ? "automatically detected" : languageNames[language]);
    updateGenerationStep("transcribe", "done");
    updateGenerationStep("detect", "active");
    $("#generation-title").textContent = "Audio transcribed";
    $("#generation-detail").textContent = "Now analysing the transcript to find strong moments…";
    setStatus("#process-status", "Transcription complete (" + detected + "). Finding highlights…");

    const minDuration = Number($("#min-duration").value);
    const maxDuration = Number($("#max-duration").value);
    const aspect = $("#clip-ratio").value;
    if (!Number.isFinite(minDuration) || !Number.isFinite(maxDuration) || minDuration < 10 || maxDuration < minDuration || maxDuration > 180) {
      throw new Error("Choose durations from 10 to 180 seconds, with maximum at least the minimum.");
    }
    const settings = new URLSearchParams({ min_seconds: String(minDuration), max_seconds: String(maxDuration), aspect });
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
    button.disabled = false;
    button.textContent = "Try again";
    return;
  }
  button.disabled = false;
  button.textContent = "Generate clips again";
}
function renderClips() {
  $("#clip-count").textContent = state.clips.length + (state.clips.length === 1 ? " clip" : " clips");
  const list = $("#clips-list");
  list.replaceChildren();
  if (!state.clips.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = "<span>✳</span><strong>No clips found in this video</strong><p>Try a longer source video or adjust the backend clip duration settings.</p>";
    list.append(empty);
    return;
  }
  state.clips.forEach((clip, index) => {
    const card = document.createElement("article");
    card.className = "clip-card";
    const top = document.createElement("div"); top.className = "clip-top";
    const title = document.createElement("div"); title.className = "clip-title"; title.textContent = "Highlight " + String(index + 1).padStart(2, "0");
    const score = document.createElement("div"); score.className = "clip-score"; score.textContent = (clip.score ?? "—") + " / 100";
    top.append(title, score);
    const excerpt = document.createElement("p"); excerpt.className = "clip-excerpt"; excerpt.textContent = clip.transcript_excerpt || "No transcript excerpt available.";
    const meta = document.createElement("div"); meta.className = "clip-meta";
    [formatTime(clip.start) + " – " + formatTime(clip.end), clip.status || "approved", clip.detection_method || "ranked"].forEach(value => {
      const chip = document.createElement("span"); chip.className = "meta-chip"; chip.textContent = value; meta.append(chip);
    });
    const reason = document.createElement("p"); reason.className = "hint"; reason.textContent = clip.reason || "Automatically approved for rendering.";
    const actions = document.createElement("div"); actions.className = "clip-actions";
    const aspect = document.createElement("select"); aspect.setAttribute("aria-label", "Clip aspect ratio");
    [["9:16","Vertical 9:16"],["1:1","Square 1:1"],["16:9","Landscape 16:9"]].forEach(([value,label]) => { const option=document.createElement("option"); option.value=value; option.textContent=label; aspect.append(option); });
    aspect.value = clip.aspect || $("#clip-ratio").value;
    const render = document.createElement("button"); render.className = "button button-primary"; render.textContent = "Render clip";
    render.addEventListener("click", () => renderClip(clip, aspect.value, render));
    actions.append(aspect, render);
    card.append(top, excerpt, meta, reason, actions);
    list.append(card);
  });
}
async function renderClip(clip, aspect, button) {
  button.disabled = true; button.textContent = "Rendering…";
  let stage = "Sending render request";
  try {
    await api("/api/clips/" + encodeURIComponent(clip.id) + "/render", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start: clip.start, end: clip.end, aspect })
    });
    stage = "Fetching rendered MP4";
    const downloadResponse = await fetch(state.api + "/api/clips/" + encodeURIComponent(clip.id) + "/download", {
      headers: state.apiKey ? { "X-API-Key": state.apiKey } : {}
    });
    if (!downloadResponse.ok) {
      const body = await downloadResponse.json().catch(() => ({}));
      throw new Error(body.detail || "Download request failed (HTTP " + downloadResponse.status + ").");
    }
    const downloadUrl = URL.createObjectURL(await downloadResponse.blob());
    const link = document.createElement("a");
    link.href = downloadUrl;
    link.className = "button button-secondary";
    link.textContent = "Download MP4";
    link.style.textDecoration = "none";
    link.style.display = "grid";
    link.style.placeItems = "center";
    button.replaceWith(link);
    link.addEventListener("click", () => setTimeout(() => URL.revokeObjectURL(downloadUrl), 60000), { once: true });
    toast("Clip rendered");
  } catch (error) {
    button.disabled = false; button.textContent = "Retry render";
    toast(stage + ": " + error.message);
  }
}
function formatTime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return Math.floor(value / 60) + ":" + String(value % 60).padStart(2, "0");
}
