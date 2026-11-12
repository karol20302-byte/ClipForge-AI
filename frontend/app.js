const DEFAULT_API = "https://clipforge-backend-i21j.onrender.com";
const $ = (selector) => document.querySelector(selector);
const state = { api: localStorage.getItem("clipforge-api") || DEFAULT_API, videoId: null, clips: [] };

$("#api-url").value = state.api;
$("#save-api").addEventListener("click", () => {
  state.api = $("#api-url").value.trim().replace(/\/$/, "");
  localStorage.setItem("clipforge-api", state.api);
  toast("Backend address saved");
});
$("#video-file").addEventListener("change", () => {
  const file = $("#video-file").files[0];
  $("#file-label").textContent = file ? file.name : "Choose a video";
  $("#upload-button").disabled = !file;
});
$("#upload-button").addEventListener("click", uploadVideo);
$("#transcribe-button").addEventListener("click", transcribeVideo);
$("#detect-button").addEventListener("click", detectClips);

async function api(path, options = {}) {
  const response = await fetch(state.api + path, options);
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

async function uploadVideo() {
  const file = $("#video-file").files[0];
  if (!file) return;
  const button = $("#upload-button");
  button.disabled = true;
  setStatus("#upload-status", "Uploading video…");
  try {
    const form = new FormData();
    form.append("file", file);
    const result = await api("/api/videos", { method: "POST", body: form });
    state.videoId = result.id;
    $("#video-summary").innerHTML = '<span class="summary-dot"></span><span></span>';
    $("#video-summary span:last-child").textContent = result.filename;
    $("#transcribe-button").disabled = false;
    setStatus("#upload-status", "Upload complete. Ready to transcribe.");
    toast("Video uploaded");
  } catch (error) {
    setStatus("#upload-status", error.message);
    button.disabled = false;
  }
}
async function transcribeVideo() {
  if (!state.videoId) return;
  $("#transcribe-button").disabled = true;
  setStatus("#process-status", "Transcribing audio with Whisper… This can take a while.");
  try {
    await api("/api/videos/" + encodeURIComponent(state.videoId) + "/transcribe", { method: "POST" });
    $("#detect-button").disabled = false;
    setStatus("#process-status", "Transcript ready. Run highlight detection next.");
    toast("Transcription complete");
  } catch (error) {
    setStatus("#process-status", error.message);
    $("#transcribe-button").disabled = false;
  }
}
async function detectClips() {
  if (!state.videoId) return;
  $("#detect-button").disabled = true;
  setStatus("#process-status", "Finding and ranking promising moments…");
  try {
    const result = await api("/api/videos/" + encodeURIComponent(state.videoId) + "/suggest-clips", { method: "POST" });
    state.clips = result.candidates || [];
    renderClips();
    setStatus("#process-status", "Detection method: " + (result.detection_method || "automatic") + ". Candidates are automatically approved.");
    toast(state.clips.length + " clip candidates found");
  } catch (error) {
    setStatus("#process-status", error.message);
    $("#detect-button").disabled = false;
  }
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
    const render = document.createElement("button"); render.className = "button button-primary"; render.textContent = "Render clip";
    render.addEventListener("click", () => renderClip(clip, aspect.value, render));
    actions.append(aspect, render);
    card.append(top, excerpt, meta, reason, actions);
    list.append(card);
  });
}
async function renderClip(clip, aspect, button) {
  button.disabled = true; button.textContent = "Rendering…";
  try {
    await api("/api/clips/" + encodeURIComponent(clip.id) + "/render", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start: clip.start, end: clip.end, aspect })
    });
    const link = document.createElement("a");
    link.href = state.api + "/api/clips/" + encodeURIComponent(clip.id) + "/download";
    link.className = "button button-secondary";
    link.textContent = "Download MP4";
    link.style.textDecoration = "none";
    link.style.display = "grid";
    link.style.placeItems = "center";
    button.replaceWith(link);
    toast("Clip rendered");
  } catch (error) {
    button.disabled = false; button.textContent = "Retry render";
    toast(error.message);
  }
}
function formatTime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return Math.floor(value / 60) + ":" + String(value % 60).padStart(2, "0");
}
