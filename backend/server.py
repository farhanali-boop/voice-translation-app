#import eventlet
#eventlet.monkey_patch()

"""
VoiceBridge — server.py (v7 Turbo-Optimized) — patched with structured logging
 - DEBUG toggles console logging; logs always go to rotating log file.
 - Non-blocking inference and TTS preserved from previous patch.
"""

import os
import io
import time
import base64
import sqlite3
import hashlib
import secrets
import asyncio
import threading
from datetime import datetime, timedelta
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
import logging
from logging.handlers import RotatingFileHandler

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, disconnect
import jwt
import torch
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator
import edge_tts

# ---------- Config / Logging ----------
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")
LOG_FILE = os.getenv("LOG_FILE", "voicebridge.log")
LOG_LEVEL = logging.DEBUG if DEBUG else logging.INFO

logger = logging.getLogger("voicebridge")
logger.setLevel(logging.DEBUG)  # capture everything and let handlers filter

formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
file_handler.setFormatter(formatter)
file_handler.setLevel(LOG_LEVEL)
logger.addHandler(file_handler)

if DEBUG:
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(LOG_LEVEL)
    logger.addHandler(stream_handler)

logger.info("Logger initialized (DEBUG=%s, log_file=%s)", DEBUG, LOG_FILE)

# ---------- App & Socket ----------
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
CORS(app, supports_credentials=True, origins="*")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    max_http_buffer_size=20_000_000,
    ping_timeout=60,
    ping_interval=25,
)

JWT_SECRET = secrets.token_hex(32)
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "users.db"))

# Thread pools
db_executor = ThreadPoolExecutor(max_workers=4)
inference_executor = ThreadPoolExecutor(max_workers=1)  # serialize model access (safe for GPU)

# ---------- Model & Constants ----------
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
COMPUTE_TYPE = "float16" if DEVICE == "cuda" else "int8"

logger.info("Loading faster-whisper ('tiny') on %s (%s) ...", DEVICE.upper(), COMPUTE_TYPE)
whisper_model = WhisperModel(
    "tiny",
    device=DEVICE,
    compute_type=COMPUTE_TYPE,
    cpu_threads=4,
    num_workers=2,
)
logger.info("faster-whisper ready (device=%s).", DEVICE)

LANGUAGES = {
    "en": "English", "ur": "Urdu", "ar": "Arabic", "zh": "Chinese",
    "es": "Spanish", "fr": "French", "de": "German", "hi": "Hindi"
}

TTS_VOICES = {
    "en": "en-US-AriaNeural",
    "ur": "ur-PK-UzmaNeural",
    "ar": "ar-SA-ZariyahNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "hi": "hi-IN-SwaraNeural"
}

# ---------- Database ----------
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    logger.info("Database location: %s", DB_PATH)
    try:
        with get_db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS translations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source_lang TEXT, target_lang TEXT,
                source_text TEXT, translated TEXT,
                created TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id))""")
            conn.commit()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.exception("Database initialization failed: %s", e)

init_db()

# ---------- Helpers ----------
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def make_token(uid, uname):
    payload = {"user_id": uid, "username": uname, "exp": datetime.utcnow() + timedelta(hours=24)}
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    logger.debug("Token created for user_id=%s username=%s", uid, uname)
    return token

def verify_token(token):
    if not token:
        logger.debug("verify_token called with empty token")
        return None
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        logger.debug("Token verified for user_id=%s", data.get("user_id"))
        return data
    except jwt.ExpiredSignatureError:
        logger.warning("Token expired")
        return None
    except Exception as e:
        logger.warning("Token verify error: %s", e)
        return None

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        data = verify_token(token)
        if not data:
            logger.warning("Unauthorized access attempt to %s", request.path)
            return jsonify({"error": "Invalid or expired token"}), 401
        return f(data, *args, **kwargs)
    return decorated

# ---------- Persistent asyncio loop for TTS ----------
_tts_loop = asyncio.new_event_loop()
def _tts_loop_runner(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()
threading.Thread(target=_tts_loop_runner, args=(_tts_loop,), daemon=True).start()
logger.debug("Persistent TTS asyncio loop started")

# Async TTS using edge-tts
async def tts_to_base64_async(text, lang):
    if not text or not isinstance(text, str):
        return ""
    voice = TTS_VOICES.get(lang, "en-US-AriaNeural")
    communicate = edge_tts.Communicate(text, voice)
    out_stream = io.BytesIO()
    try:
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                out_stream.write(chunk.get("data") or b"")
    except Exception as e:
        logger.exception("TTS streaming error: %s", e)
        return ""
    return base64.b64encode(out_stream.getvalue()).decode("utf-8")

def generate_tts_sync(text, lang, timeout=30):
    if not text or not isinstance(text, str):
        return ""
    coro = tts_to_base64_async(text, lang)
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, _tts_loop)
        audio_b64 = fut.result(timeout=timeout)
        logger.debug("TTS generated (len=%d) for lang=%s", len(audio_b64) if audio_b64 else 0, lang)
        return audio_b64
    except Exception as e:
        logger.exception("TTS generation error/timeout: %s", e)
        try:
            fut.cancel()
        except Exception:
            pass
        return ""

def fast_translate(text, tgt_lang):
    if not text or not isinstance(text, str) or not text.strip():
        return ""
    try:
        translated = GoogleTranslator(source="auto", target=tgt_lang).translate(text)
        logger.debug("Translation completed (target=%s): %s...", tgt_lang, (translated[:100] + "...") if translated and len(translated) > 100 else translated)
        return str(translated) if translated else text
    except Exception as e:
        logger.exception("Translation error: %s", e)
        return str(text)

def async_save_history(user_id, source_lang, target_lang, source_text, translated):
    if not user_id:
        return
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO translations(user_id, source_lang, target_lang, source_text, translated) VALUES(?,?,?,?,?)",
                (user_id, str(source_lang), str(target_lang), str(source_text), str(translated))
            )
            conn.commit()
        logger.debug("Saved history for user_id=%s (source_len=%d translated_len=%d)", user_id, len(source_text or ""), len(translated or ""))
    except Exception as e:
        logger.exception("DB Save Error: %s", e)

# ---------- Core Pipeline ----------
def _transcribe_sync(audio_bytes, wlang):
    audio_stream = io.BytesIO(audio_bytes)
    segments, info = whisper_model.transcribe(
        audio_stream,
        language=wlang,
        beam_size=1,
        best_of=1,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=150),
    )
    return segments, info

def run_pipeline(audio_bytes, src_lang, tgt_lang, user_id):
    t0 = time.time()
    logger.info("Pipeline start for user_id=%s src=%s tgt=%s audio_bytes=%d", user_id, src_lang, tgt_lang, len(audio_bytes) if audio_bytes else 0)

    wlang = src_lang if src_lang not in ("auto", None, "") else None
    try:
        future = inference_executor.submit(_transcribe_sync, audio_bytes, wlang)
        segments, info = future.result()
    except Exception as e:
        logger.exception("Transcription error: %s", e)
        return {"error": f"transcription_failed: {e}"}

    source_text = " ".join(str(getattr(s, "text", "")).strip() for s in segments if s).strip()
    detected = str(info.language) if info and getattr(info, "language", None) else (src_lang or "en")
    t_stt = time.time()

    logger.debug("STT done (detected=%s source_len=%d) time=%.2fs", detected, len(source_text or ""), t_stt - t0)

    if not source_text:
        logger.info("No speech detected (silence)")
        return {"error": "silence"}

    try:
        translated = fast_translate(source_text, tgt_lang)
        if not translated or not isinstance(translated, str):
            translated = source_text
    except Exception as e:
        logger.exception("Translation pipeline error: %s", e)
        translated = source_text
    t_trans = time.time()
    logger.debug("Translation done time=%.2fs", t_trans - t_stt)

    try:
        audio_b64 = generate_tts_sync(translated, tgt_lang, timeout=30)
    except Exception as e:
        logger.exception("TTS generation error: %s", e)
        audio_b64 = ""
    t_tts = time.time()

    logger.info("Pipeline finished for user_id=%s [STT=%.2fs Trans=%.2fs TTS=%.2fs Total=%.2fs]",
                user_id, t_stt - t0, t_trans - t_stt, t_tts - t_trans, t_tts - t0)

    try:
        db_executor.submit(async_save_history, user_id, detected, tgt_lang, source_text, translated)
    except Exception as e:
        logger.exception("DB submit error: %s", e)

    return {
        "source_text": source_text,
        "translated": translated,
        "detected_lang": detected,
        "target_lang": tgt_lang,
        "audio_b64": audio_b64,
    }

# ---------- REST Endpoints ----------
@app.route("/api/register", methods=["POST"])
def register():
    b = request.get_json() or {}
    u, e, p = b.get("username", "").strip(), b.get("email", "").strip(), b.get("password", "")
    logger.debug("/api/register attempt username=%s email=%s", u, e)
    if not u or not e or not p:
        logger.warning("Register failed: missing fields")
        return jsonify({"error": "All fields required"}), 400
    if len(p) < 6:
        logger.warning("Register failed: weak password for username=%s", u)
        return jsonify({"error": "Password min 6 chars"}), 400
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO users(username,email,password)VALUES(?,?,?)", (u, e, hash_pw(p)))
            conn.commit()
        logger.info("Account created username=%s", u)
        return jsonify({"message": "Account created!"}), 201
    except sqlite3.IntegrityError:
        logger.warning("Register conflict: username/email taken username=%s email=%s", u, e)
        return jsonify({"error": "Username or email taken"}), 409
    except Exception as ex:
        logger.exception("Register error: %s", ex)
        return jsonify({"error": "Server error"}), 500

@app.route("/api/login", methods=["POST"])
def login():
    b = request.get_json() or {}
    u, p = b.get("username", "").strip(), b.get("password", "")
    logger.debug("/api/login attempt username=%s", u)
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username=? AND password=?", (u, hash_pw(p))).fetchone()
    if not user:
        logger.warning("Invalid login for username=%s", u)
        return jsonify({"error": "Invalid credentials"}), 401
    token = make_token(user["id"], user["username"])
    logger.info("User logged in username=%s user_id=%s", user["username"], user["id"])
    return jsonify({"token": token, "username": user["username"], "message": "Login successful"})

@app.route("/api/languages", methods=["GET"])
def languages():
    logger.debug("/api/languages requested")
    return jsonify(LANGUAGES)

@app.route("/api/history", methods=["GET"])
@token_required
def history(user_data):
    logger.debug("/api/history request for user_id=%s", user_data["user_id"])
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM translations WHERE user_id=? ORDER BY created DESC LIMIT 50",
            (user_data["user_id"],)
        ).fetchall()
    logger.debug("/api/history returned %d rows for user_id=%s", len(rows), user_data["user_id"])
    return jsonify([dict(r) for r in rows])

@app.route("/api/translate", methods=["POST"])
@token_required
def translate_rest(user_data):
    b = request.get_json() or {}
    ab = b.get("audio")
    sl = b.get("source_lang", "auto")
    tl = b.get("target_lang", "ur")
    logger.debug("/api/translate invoked by user_id=%s src=%s tgt=%s audio_present=%s", user_data["user_id"], sl, tl, bool(ab))
    if not ab:
        logger.warning("/api/translate missing audio for user_id=%s", user_data["user_id"])
        return jsonify({"error": "No audio"}), 400
    try:
        r = run_pipeline(base64.b64decode(ab), sl, tl, user_data["user_id"])
        if "error" in r:
            logger.warning("/api/translate pipeline error for user_id=%s: %s", user_data["user_id"], r.get("error"))
            return jsonify(r), 422
        logger.debug("/api/translate success for user_id=%s result_source_len=%d", user_data["user_id"], len(r.get("source_text", "")))
        return jsonify(r)
    except Exception as e:
        logger.exception("/api/translate exception: %s", e)
        return jsonify({"error": str(e)}), 500

# ---------- WebSocket Gateway ----------
@socketio.on("connect")
def on_connect():
    token = request.args.get("token", "")
    data = verify_token(token)
    sid = request.sid
    if not data:
        logger.warning("Socket connect unauthorized sid=%s", sid)
        disconnect()
        return False
    logger.info("Socket connected sid=%s username=%s", sid, data.get("username"))
    emit("connected", {"message": f"Welcome {data['username']}! Ready."})

def _handle_translate_bg(sid, token, audio_b64, sl, tl):
    user = verify_token(token)
    if not user:
        logger.warning("WebSocket translate unauthorized sid=%s", sid)
        socketio.emit("error", {"message": "Unauthorized"}, room=sid)
        return

    logger.debug("Background translate started sid=%s user_id=%s src=%s tgt=%s audio_present=%s", sid, user.get("user_id"), sl, tl, bool(audio_b64))
    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as e:
        logger.warning("Invalid audio from sid=%s: %s", sid, e)
        socketio.emit("error", {"message": f"Invalid audio: {e}"}, room=sid)
        return

    socketio.emit("processing", {"message": "⚡ Translating..."}, room=sid)
    try:
        r = run_pipeline(audio_bytes, sl, tl, user["user_id"])
        if "error" in r:
            if r["error"] == "silence":
                logger.info("Silence detected sid=%s user_id=%s", sid, user["user_id"])
                socketio.emit("silence", {}, room=sid)
            else:
                logger.warning("Pipeline error sid=%s user_id=%s error=%s", sid, user["user_id"], r.get("error"))
                socketio.emit("error", r, room=sid)
        else:
            logger.info("Translation_result emitted sid=%s user_id=%s", sid, user["user_id"])
            logger.info("RESULT DATA: %s", r)
            socketio.emit("translation_result", r, room=sid)
    except Exception as e:
        logger.exception("WebSocket pipeline error sid=%s: %s", sid, e)
        socketio.emit("error", {"message": str(e)}, room=sid)

@socketio.on("translate_audio")
def on_translate_audio(payload):
    token = request.args.get("token", "")
    sid = request.sid
    if not payload:
        logger.warning("translate_audio received empty payload sid=%s", sid)
        emit("error", {"message": "No payload"})
        return
    ab = payload.get("audio")
    sl = payload.get("source_lang", "auto")
    tl = payload.get("target_lang", "ur")
    if not ab:
        logger.warning("translate_audio missing audio sid=%s", sid)
        emit("error", {"message": "No audio"})
        return

    logger.debug("translate_audio received sid=%s src=%s tgt=%s audio_len=%d", sid, sl, tl, len(ab))
    try:
        socketio.start_background_task(_handle_translate_bg, sid, token, ab, sl, tl)
    except Exception as e:
        logger.exception("start_background_task error: %s", e)
        emit("error", {"message": "Server error starting background task"})

# ---------- Run ----------
if __name__ == "__main__":
    logger.info("Starting VoiceBridge v7 — TURBO MODE RUNNING")
    logger.info("URL: http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000)