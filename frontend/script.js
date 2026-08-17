/*
 * VoiceBridge — script.js
 * Handles:  Auth (login / register)
 *           Translation page (WebSocket + MediaRecorder + REST fallback)
 *           Audio level visualiser, TTS playback, History drawer
 * ─────────────────────────────────────────────────────────────────────
 */

const API = "http://localhost:5000";

/* ── Storage helpers ─────────────────────────────────────────────── */
const getToken    = () => localStorage.getItem("vb_token");
const getUsername = () => localStorage.getItem("vb_user");
const saveAuth    = (token, user) => {
  localStorage.setItem("vb_token", token);
  localStorage.setItem("vb_user",  user);
};
const clearAuth   = () => {
  localStorage.removeItem("vb_token");
  localStorage.removeItem("vb_user");
};

/* ── DOM shortcut ─────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);

/* ══════════════════════════════════════════════════════════════════
   AUTH PAGE  (index.html)
   ══════════════════════════════════════════════════════════════════ */
if (document.querySelector(".auth-page") || document.querySelector(".auth-card")) {

  /* Redirect already-logged-in users */
  if (getToken()) { location.href = "translate.html"; }

  /* ── Tab switching ── */
  document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      const pane = btn.dataset.p || btn.dataset.pane;
      document.querySelectorAll(".tab, .tp").forEach(el => el.classList.remove("active"));
      btn.classList.add("active");
      const paneEl = document.getElementById("p-" + pane) || document.getElementById("pane-" + pane);
      if (paneEl) paneEl.classList.add("active");
    });
  });

  /* ── Message helper ── */
  function showMsg(elId, text, type = "err") {
    const el = $(elId);
    if (!el) return;
    el.textContent = text;
    el.className   = "msgbox show " + type;
  }

  /* ── Button loading state ── */
  function setBtnLoad(btn, loading) {
    btn.classList.toggle("loading", loading);
    btn.disabled = loading;
  }

  /* ── LOGIN ── */
  const loginBtn = $("l-btn");
  if (loginBtn) {
    loginBtn.addEventListener("click", async () => {
      const body = {
        username: ($("l-u") || $("l-user"))?.value.trim(),
        password: ($("l-p") || $("l-pass"))?.value,
      };
      const msgId = "l-m";

      if (!body.username || !body.password) {
        showMsg(msgId, "Please fill in all fields."); return;
      }
      setBtnLoad(loginBtn, true);
      try {
        const res  = await fetch(`${API}/api/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) {
          showMsg(msgId, data.error || "Login failed.");
        } else {
          saveAuth(data.token, data.username);
          showMsg(msgId, "Welcome back! Loading…", "ok");
          setTimeout(() => location.href = "translate.html", 700);
        }
      } catch {
        showMsg(msgId, "Cannot reach server. Make sure the backend is running.");
      }
      setBtnLoad(loginBtn, false);
    });
  }

  /* ── REGISTER ── */
  const regBtn = $("r-btn");
  if (regBtn) {
    regBtn.addEventListener("click", async () => {
      const body = {
        username: ($("r-u") || $("r-user"))?.value.trim(),
        email:    ($("r-e") || $("r-email"))?.value.trim(),
        password: ($("r-p") || $("r-pass"))?.value,
      };
      const msgId = "r-m";

      if (!body.username || !body.email || !body.password) {
        showMsg(msgId, "Please fill in all fields."); return;
      }
      setBtnLoad(regBtn, true);
      try {
        const res  = await fetch(`${API}/api/register`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) {
          showMsg(msgId, data.error || "Registration failed.");
        } else {
          showMsg(msgId, "Account created! Please sign in.", "ok");
          setTimeout(() => {
            /* Switch to login tab */
            const loginTab = document.querySelector('[data-p="login"], [data-pane="login"]');
            if (loginTab) loginTab.click();
            const luEl = $("l-u") || $("l-user");
            if (luEl) luEl.value = body.username;
          }, 900);
        }
      } catch {
        showMsg(msgId, "Cannot reach server. Make sure the backend is running.");
      }
      setBtnLoad(regBtn, false);
    });
  }

  /* ── Enter-key shortcuts ── */
  ["l-u", "l-p", "l-user", "l-pass"].forEach(id => {
    $(id)?.addEventListener("keydown", e => { if (e.key === "Enter") loginBtn?.click(); });
  });
  ["r-u", "r-e", "r-p", "r-user", "r-email", "r-pass"].forEach(id => {
    $(id)?.addEventListener("keydown", e => { if (e.key === "Enter") regBtn?.click(); });
  });
}

/* ══════════════════════════════════════════════════════════════════
   TRANSLATE PAGE  (translate.html)
   ══════════════════════════════════════════════════════════════════ */
if (document.querySelector(".app-page") || document.querySelector("main.app-main")) {

  /* Guard — must be logged in */
  if (!getToken()) { location.href = "index.html"; }

  /* Show username */
  const navUser = $("nav-user") || $("nav-username");
  if (navUser) navUser.textContent = getUsername() || "—";

  /* ── State ─────────────────────────────────────────────────────── */
  let socket      = null;
  let wsOk        = false;
  let mediaRec    = null;
  let audioChunks = [];
  let isRecording = false;
  let audioCtx    = null;
  let analyser    = null;
  let lvlRaf      = null;
  let ttsB64      = null;

  /* ── Status bar helper ── */
  function setSt(text, spinning = false) {
    const stEl  = $("st-txt")  || $("status-text");
    const spEl  = $("ssp")     || $("status-spinner");
    if (stEl) stEl.textContent = text;
    if (spEl) spEl.classList.toggle("on", spinning),
              spEl.classList.toggle("active", spinning);
  }

  /* ── WebSocket status pill ── */
  function setWsPill(online) {
    wsOk = online;
    const pill  = $("ws-pill");
    const label = $("ws-label");
    const dot   = $("ws-status");      /* fallback id used in older version */
    if (pill)  pill.classList.toggle("on", online);
    if (label) label.textContent = online ? "Live" : "Offline";
    if (dot) {
      dot.className = "status-dot " + (online ? "connected" : "error");
      dot.title     = online ? "Connected" : "Disconnected";
    }
  }

  /* ── Init WebSocket ── */
  function initSocket() {
    if (typeof io === "undefined") { setSt("Socket.IO not loaded — REST mode"); return; }

    socket = io(API, {
      query:      { token: getToken() },
      transports: ["websocket"],
    });

    socket.on("connect",       () => { setWsPill(true);  setSt("Connected — ready to translate"); });
    socket.on("disconnect",    () => { setWsPill(false); setSt("Disconnected — REST fallback active"); });
    socket.on("connect_error", () => { setWsPill(false); setSt("WebSocket error — REST fallback active"); });
    socket.on("processing",    d  => setSt(d.message || "Processing…", true));
    socket.on("translation_result", onResult);
    socket.on("error",         d  => { setSt("Error: " + (d.message || "Unknown"), false); console.error(d); });
    socket.on("connected",     d  => setSt(d.message || "Connected", false));
  }
  initSocket();

  /* ── Load languages from API ── */
  (async () => {
    try {
      const res  = await fetch(`${API}/api/languages`);
      const data = await res.json();
      const srcEl = $("src-lang") || $("source-lang");
      const tgtEl = $("tgt-lang") || $("target-lang");
      if (!srcEl || !tgtEl) return;

      Object.entries(data).forEach(([code, name]) => {
        srcEl.insertAdjacentHTML("beforeend", `<option value="${code}">${name}</option>`);
        tgtEl.insertAdjacentHTML("beforeend", `<option value="${code}">${name}</option>`);
      });
      tgtEl.value = "es"; /* default: Spanish */
    } catch (e) {
      console.warn("Could not load languages:", e);
    }
  })();

  /* ── Swap languages ── */
  const swapBtn = $("swap-btn");
  if (swapBtn) {
    swapBtn.addEventListener("click", () => {
      const srcEl = $("src-lang") || $("source-lang");
      const tgtEl = $("tgt-lang") || $("target-lang");
      if (!srcEl || !tgtEl || srcEl.value === "auto") return;
      const tmp = srcEl.value;
      srcEl.value = tgtEl.value;
      tgtEl.value = tmp;
    });
  }

  /* ── Audio level visualiser ── */
  const BARS = ["ab1","ab2","ab3","ab4","ab5","ab6","ab7","ab8"];

  function startLevel(stream) {
    try {
      audioCtx  = new (window.AudioContext || window.webkitAudioContext)();
      analyser  = audioCtx.createAnalyser();
      analyser.fftSize = 256;
      audioCtx.createMediaStreamSource(stream).connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);

      const tick = () => {
        analyser.getByteFrequencyData(data);
        const bins = Math.floor(data.length / BARS.length);
        BARS.forEach((id, i) => {
          const el = $(id); if (!el) return;
          const avg = data.slice(i * bins, (i + 1) * bins).reduce((a, b) => a + b, 0) / bins;
          const h   = Math.max(4, Math.min(18, avg * 0.22));
          el.style.height     = h + "px";
          el.style.background = avg > 60
            ? "rgba(244,63,94,0.85)"
            : "rgba(0,212,255,0.45)";
        });
        lvlRaf = requestAnimationFrame(tick);
      };
      tick();
    } catch (e) { console.warn("Audio analyser failed:", e); }
  }

  function stopLevel() {
    cancelAnimationFrame(lvlRaf);
    if (audioCtx) { audioCtx.close(); audioCtx = null; }
    BARS.forEach(id => {
      const el = $(id); if (!el) return;
      el.style.height     = "4px";
      el.style.background = "rgba(244,63,94,0.3)";
    });
  }

  /* ── Start / stop recording ── */
  async function startRecording() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      startLevel(stream);

      mediaRec    = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
      audioChunks = [];

      mediaRec.addEventListener("dataavailable", e => {
        if (e.data.size > 0) audioChunks.push(e.data);
      });
      mediaRec.addEventListener("stop", () => {
        stream.getTracks().forEach(t => t.stop());
        processAudio();
      });
      mediaRec.start();

      isRecording = true;
      const micBtn = $("mic-btn");
      const inPanel = $("in-panel");
      const micHint = $("mic-hint");
      if (micBtn)  micBtn.classList.add("rec");
      if (inPanel) inPanel.classList.add("active-rec");
      if (micHint) micHint.textContent = "Recording… release to translate";
      setSt("🔴 Recording…", false);

    } catch (err) {
      console.error("Mic error:", err);
      setSt("Microphone access denied. Please allow mic in browser settings.", false);
    }
  }

  function stopRecording() {
    if (mediaRec && mediaRec.state !== "inactive") mediaRec.stop();
    isRecording = false;

    const micBtn  = $("mic-btn");
    const inPanel = $("in-panel");
    const micHint = $("mic-hint");
    if (micBtn)  micBtn.classList.remove("rec");
    if (inPanel) inPanel.classList.remove("active-rec");
    if (micHint) micHint.textContent = "Hold to record";
    stopLevel();
    setSt("Processing audio…", true);
  }

  /* ── Convert chunks → base64, send for translation ── */
  async function processAudio() {
    if (!audioChunks.length) { setSt("No audio captured.", false); return; }

    const blob   = new Blob(audioChunks, { type: "audio/webm" });
    const buffer = await blob.arrayBuffer();
    const b64    = btoa(String.fromCharCode(...new Uint8Array(buffer)));

    const srcEl = $("src-lang") || $("source-lang");
    const tgtEl = $("tgt-lang") || $("target-lang");
    const sl = srcEl?.value || "auto";
    const tl = tgtEl?.value || "es";

    /* Prefer WebSocket; fall back to REST */
    if (wsOk && socket?.connected) {
      socket.emit("translate_audio", { audio: b64, source_lang: sl, target_lang: tl });
    } else {
      setSt("Sending via REST API…", true);
      try {
        const res  = await fetch(`${API}/api/translate`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + getToken(),
          },
          body: JSON.stringify({ audio: b64, source_lang: sl, target_lang: tl }),
        });
        const data = await res.json();
        if (!res.ok) setSt("Error: " + (data.error || "Unknown"), false);
        else         onResult(data);
      } catch (e) {
        setSt("Network error: " + e.message, false);
      }
    }
  }

  /* ── Handle translation result ── */
  function onResult(data) {
    /* Source text */
    const srcBox = $("src-txt") || $("source-text");
    if (srcBox) { srcBox.innerHTML = ""; srcBox.textContent = data.source_text || ""; }

    /* Translated text */
    const tgtBox = $("tgt-txt") || $("target-text");
    if (tgtBox) { tgtBox.innerHTML = ""; tgtBox.textContent = data.translated || ""; }

    /* Detected language tag */
    const dtag = $("dtag") || $("detected-tag");
    if (dtag && data.detected_lang) {
      dtag.textContent = "Detected: " + data.detected_lang.toUpperCase();
      dtag.classList.add("show", "visible");
    }

    /* TTS audio */
    if (data.audio_b64) {
      ttsB64 = data.audio_b64;
      const pb = $("play-btn");
      if (pb) pb.disabled = false;
    }

    setSt("✅ Translation complete!", false);
    loadHistory();
  }

  /* ── Mic button — mouse & touch ── */
  const micBtn = $("mic-btn");
  if (micBtn) {
    micBtn.addEventListener("mousedown",  e => { e.preventDefault(); if (!isRecording) startRecording(); });
    micBtn.addEventListener("mouseup",    ()  => { if (isRecording) stopRecording(); });
    micBtn.addEventListener("mouseleave", ()  => { if (isRecording) stopRecording(); });
    micBtn.addEventListener("touchstart", e  => { e.preventDefault(); if (!isRecording) startRecording(); }, { passive: false });
    micBtn.addEventListener("touchend",   e  => { e.preventDefault(); if (isRecording)  stopRecording();  }, { passive: false });
  }

  /* ── TTS Playback ── */
  const playBtn  = $("play-btn");
  const ttsAudio = $("tts-audio");
  if (playBtn) {
    playBtn.addEventListener("click", () => {
      if (!ttsB64 || !ttsAudio) return;
      ttsAudio.src = "data:audio/mp3;base64," + ttsB64;
      ttsAudio.play().catch(console.error);
    });
  }

  /* ── Copy translation ── */
  const copyBtn = $("copy-btn");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const tgtBox = $("tgt-txt") || $("target-text");
      const text   = tgtBox?.textContent;
      if (!text || text.includes("Translation will appear")) return;
      try {
        await navigator.clipboard.writeText(text);
        const orig = copyBtn.textContent;
        copyBtn.textContent = "✅";
        setTimeout(() => copyBtn.textContent = orig, 1500);
      } catch (e) { console.warn("Clipboard write failed:", e); }
    });
  }

  /* ── History drawer ── */
  const drawer  = $("drawer");
  const overlay = $("overlay");

  function openDrawer() {
    drawer?.classList.add("open");
    overlay?.classList.add("show");
    loadHistory();
  }
  function closeDrawer() {
    drawer?.classList.remove("open");
    overlay?.classList.remove("show");
  }

  $("hist-btn")    ?.addEventListener("click", openDrawer);
  $("history-toggle")?.addEventListener("click", openDrawer);
  $("close-d")     ?.addEventListener("click", closeDrawer);
  $("close-drawer")?.addEventListener("click", closeDrawer);
  overlay          ?.addEventListener("click", closeDrawer);

  async function loadHistory() {
    const list = $("hist-list") || $("history-list");
    if (!list) return;
    try {
      const res  = await fetch(`${API}/api/history`, {
        headers: { "Authorization": "Bearer " + getToken() },
      });
      const data = await res.json();
      list.innerHTML = "";

      if (!data.length) {
        list.innerHTML = '<p class="empty-s empty-state">No translations yet.</p>';
        return;
      }
      data.forEach(item => {
        const div = document.createElement("div");
        div.className = "hi history-item";
        div.innerHTML = `
          <div class="hi-langs history-langs">${esc(item.source_lang || "?").toUpperCase()} → ${esc(item.target_lang || "?").toUpperCase()}</div>
          <div class="hi-src history-source">${esc(item.source_text || "")}</div>
          <div class="hi-trl history-translated">${esc(item.translated || "")}</div>
          <div class="hi-time history-time">${new Date(item.created).toLocaleString()}</div>
        `;
        div.addEventListener("click", () => {
          const s = $("src-txt") || $("source-text");
          const t = $("tgt-txt") || $("target-text");
          if (s) s.textContent = item.source_text || "";
          if (t) t.textContent = item.translated  || "";
          closeDrawer();
        });
        list.appendChild(div);
      });
    } catch (e) { console.warn("History load failed:", e); }
  }

  /* ── Logout ── */
  const logoutBtn = $("logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
      if (socket) socket.disconnect();
      clearAuth();
      location.href = "index.html";
    });
  }

  /* ── HTML escape helper ── */
  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
}