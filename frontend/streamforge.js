(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const input = $("#sf-url");
  if (!input) return;
  const queue = [];
  const fields = { format: $("#sf-format"), quality: $("#sf-quality"), start: $("#sf-start"), end: $("#sf-end"), subs: $("#sf-subs"), meta: $("#sf-meta"), keyframes: $("#sf-keyframes"), authorized: $("#sf-authorized") };
  const out = { ytdlp: $("#sf-ytdlp"), formats: $("#sf-formats"), ffmpeg: $("#sf-ffmpeg") };
  let current = null;
  function shellQuote(value) { return "'" + String(value).replace(/'/g, "'\\''") + "'"; }
  function windowsQuote(value) { return '"' + String(value).replace(/"/g, '""') + '"'; }
  function parseUrl(value) {
    const raw = value.trim();
    if (!raw) return { empty: true, valid: false };
    let u; try { u = new URL(raw); } catch (_) { return { valid: false, error: "Malformed URL" }; }
    if (!["http:", "https:"].includes(u.protocol)) return { valid: false, error: "HTTP/HTTPS only" };
    const host = u.hostname.toLowerCase().replace(/^www\./, ""), path = u.pathname;
    const direct = path.match(/\.(mp4|webm|ogg|mov|m4v)$/i);
    if (direct) return { valid: true, url: raw, platform: "Direct media", id: path.split("/").pop() || "video", type: direct[1].toUpperCase() + " file", direct: true, manifest: false };
    if (/\.(m3u8|mpd)$/i.test(path)) return { valid: true, url: raw, platform: "Adaptive stream", id: path.split("/").pop(), type: /\.mpd$/i.test(path) ? "MPEG-DASH manifest" : "HLS playlist", direct: false, manifest: true };
    if (host === "youtu.be" || host.endsWith("youtube.com")) {
      const parts = path.split("/").filter(Boolean);
      const id = host === "youtu.be" ? parts[0] : (u.searchParams.get("v") || (["shorts", "live", "embed"].includes(parts[0]) ? parts[1] : "") || "Unknown");
      return { valid: true, url: raw, platform: "YouTube", id, type: "DASH / adaptive", direct: false, manifest: false };
    }
    if (host.endsWith("vimeo.com")) return { valid: true, url: raw, platform: "Vimeo", id: path.match(/\/(\d+)/)?.[1] || "Vimeo video", type: "HLS / progressive", direct: false, manifest: false };
    if (host.endsWith("twitch.tv")) return { valid: true, url: raw, platform: "Twitch", id: path.split("/").filter(Boolean).pop() || "Twitch video", type: "HLS", direct: false, manifest: false };
    return { valid: true, url: raw, platform: host, id: path.split("/").filter(Boolean).pop() || "Page media", type: "yt-dlp auto-detect", direct: false, manifest: false };
  }
  function options() { return { format: fields.format.value, quality: fields.quality.value, start: fields.start.value.trim(), end: fields.end.value.trim(), subs: fields.subs.checked, meta: fields.meta.checked, keyframes: fields.keyframes.checked }; }
  function validTime(value) { return !value || /^(?:\d{1,3}:)?[0-5]?\d:[0-5]\d$/.test(value); }
  function ytdlpCommand(url, opt, windows) {
    const q = windows ? windowsQuote : shellQuote, parts = ["yt-dlp"];
    const audio = ["mp3", "m4a", "wav"].includes(opt.format) || opt.quality === "audio";
    if (audio) {
      const fmt = ["mp3", "m4a", "wav"].includes(opt.format) ? opt.format : "mp3";
      parts.push("-x", "--audio-format", fmt, "--audio-quality", "0");
    } else {
      const height = opt.quality === "best" ? "" : "[height<=" + opt.quality + "]";
      if (opt.format === "mp4") parts.push("-f", q("bestvideo" + height + "[ext=mp4]+bestaudio[ext=m4a]/best" + height + "[ext=mp4]/best"), "--merge-output-format", "mp4");
      else if (opt.format === "webm") parts.push("-f", q("bestvideo" + height + "[ext=webm]+bestaudio[ext=webm]/best" + height), "--merge-output-format", "webm");
      else parts.push("-f", q("bestvideo" + height + "+bestaudio/best" + height), "--merge-output-format", opt.format);
    }
    if (opt.start || opt.end) { parts.push("--download-sections", q("*" + (opt.start || "00:00:00") + "-" + (opt.end || "inf"))); if (opt.keyframes) parts.push("--force-keyframes-at-cuts"); }
    if (opt.subs && !audio) parts.push("--write-auto-subs", "--sub-lang", "en", "--embed-subs");
    if (opt.meta) parts.push("--embed-metadata", "--embed-thumbnail");
    parts.push("-o", q("%(title)s [%(id)s].%(ext)s"), q(url));
    return parts.join(" ");
  }
  function update() {
    const info = parseUrl(input.value); current = info;
    const status = (id, value) => { $(id).textContent = value; };
    if (info.empty) { status("#sf-platform", "—"); status("#sf-id", "—"); status("#sf-type", "—"); status("#sf-playable", "—"); }
    else if (!info.valid) { status("#sf-platform", info.error); status("#sf-id", "—"); status("#sf-type", "Invalid URL"); status("#sf-playable", "No"); }
    else { status("#sf-platform", info.platform); status("#sf-id", info.id); status("#sf-type", info.type); status("#sf-playable", info.direct ? "Yes · HTML5" : "Local command"); }
    const opt = options(), target = info.valid ? info.url : "https://www.youtube.com/watch?v=VIDEO_ID";
    out.ytdlp.textContent = ytdlpCommand(target, opt, false);
    out.formats.textContent = "yt-dlp -F " + shellQuote(target);
    const ff = ["ffmpeg"];
    if (opt.start) ff.push("-ss", shellQuote(opt.start));
    if (opt.end) ff.push("-to", shellQuote(opt.end));
    const ffInput = info.valid && (info.direct || info.manifest) ? target : "$(yt-dlp -g -f best " + shellQuote(target) + ")";
    ff.push("-i", shellQuote(ffInput));
    const audio = ["mp3", "m4a", "wav"].includes(opt.format) || opt.quality === "audio";
    if (audio) ff.push("-vn", "-q:a", "0", shellQuote("output_clip." + (["mp3", "m4a", "wav"].includes(opt.format) ? opt.format : "mp3")));
    else if (opt.start || opt.end || opt.keyframes) ff.push("-c:v", "libx264", "-crf", "20", "-c:a", "aac", shellQuote("output_clip." + opt.format));
    else ff.push("-c", "copy", shellQuote("output_clip." + opt.format));
    out.ffmpeg.textContent = ff.join(" ");
    const wrap = $("#sf-preview-wrap"), player = $("#sf-player");
    if (info.valid && info.direct) { wrap.hidden = false; if (player.src !== info.url) { player.src = info.url; player.load(); } $("#sf-preview-status").textContent = "Direct media URL detected. Playback depends on browser codec and source CORS settings."; }
    else { wrap.hidden = true; if (player.hasAttribute("src")) { player.pause(); player.removeAttribute("src"); player.load(); } }
  }
  function notify(message) { if (typeof window.toast === "function") window.toast(message); else if ($("#sf-preview-status")) $("#sf-preview-status").textContent = message; }
  function renderQueue() {
    $("#sf-count").textContent = String(queue.length);
    const root = $("#sf-queue"); root.replaceChildren();
    if (!queue.length) { const p = document.createElement("p"); p.textContent = "No batch items yet. Add a URL above to prepare a local script."; root.append(p); return; }
    queue.forEach((item, index) => {
      const row = document.createElement("div"); row.className = "streamforge-queue-item";
      const info = document.createElement("div"), name = document.createElement("strong"), detail = document.createElement("small");
      name.textContent = (index + 1) + ". " + item.platform + " · " + item.id;
      detail.textContent = item.format.toUpperCase() + " · " + item.quality + " · " + item.url;
      info.append(name, detail);
      const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "Remove";
      remove.addEventListener("click", () => { queue.splice(index, 1); renderQueue(); });
      row.append(info, remove); root.append(row);
    });
  }
  function downloadFile(name, content) {
    const href = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
    const a = document.createElement("a"); a.href = href; a.download = name; document.body.append(a); a.click(); a.remove();
    window.setTimeout(() => URL.revokeObjectURL(href), 1000);
  }
  function downloadBatch(ext) {
    if (!fields.authorized.checked) { notify("Confirm that you have permission to edit these videos first."); return; }
    const items = queue.length ? queue : (current?.valid ? [{ ...current, ...options() }] : []);
    if (!items.length) { notify("Add a valid video URL first."); return; }
    const win = ext === "bat";
    const lines = win ? ["@echo off", "REM Generated by ClipForge StreamForge", "REM Requires yt-dlp and FFmpeg installed and available in PATH", ""] : ["#!/usr/bin/env bash", "# Generated by ClipForge StreamForge", "# Requires yt-dlp and FFmpeg installed and available in PATH", "set -e", ""];
    items.forEach((item, i) => { lines.push(win ? "echo [" + (i + 1) + "/" + items.length + "] " + item.id : "echo " + shellQuote("[" + (i + 1) + "/" + items.length + "] " + item.id)); lines.push(ytdlpCommand(item.url, item, win)); lines.push(""); });
    lines.push(win ? "echo Batch complete." : "echo 'Batch complete.'"); if (win) lines.push("pause");
    downloadFile("clipforge_streamforge_batch." + ext, lines.join(win ? "\r\n" : "\n"));
    notify("Batch script created · " + items.length + " job(s)");
  }
  input.addEventListener("input", update);
  Object.values(fields).forEach(el => el.addEventListener("change", update));
  [fields.start, fields.end].forEach(el => el.addEventListener("input", update));
  $("#sf-add").addEventListener("click", () => {
    const info = parseUrl(input.value);
    if (!info.valid) { notify(info.error || "Enter a valid HTTP/HTTPS URL."); input.focus(); return; }
    if (!fields.authorized.checked) { notify("Confirm that you own this video or have permission to edit it."); return; }
    if (!validTime(fields.start.value.trim()) || !validTime(fields.end.value.trim())) { notify("Use HH:MM:SS for clip timestamps."); return; }
    if (fields.start.value && fields.end.value && fields.end.value <= fields.start.value) { notify("Clip end time must be after clip start time."); return; }
    queue.push({ ...info, ...options() }); renderQueue(); notify("Added to StreamForge batch queue.");
  });
  document.querySelectorAll("[data-sf-copy]").forEach(button => button.addEventListener("click", async () => {
    const text = $("#" + button.dataset.sfCopy).textContent;
    try { await navigator.clipboard.writeText(text); notify("Command copied."); }
    catch (_) { const area = document.createElement("textarea"); area.value = text; document.body.append(area); area.select(); document.execCommand("copy"); area.remove(); notify("Command copied."); }
  }));
  $("#sf-sh").addEventListener("click", () => downloadBatch("sh"));
  $("#sf-bat").addEventListener("click", () => downloadBatch("bat"));
  update(); renderQueue();
})();