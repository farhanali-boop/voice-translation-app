"""
VoiceBridge — server.py (v3 Offline)
faster-whisper (STT) + Helsinki-NLP (Translation) + pyttsx3 (TTS)
Fully Offline — English, Urdu, Arabic, Chinese
Real-time — One button press to start, one to stop
"""

import os
import base64
import sqlite3
import hashlib
import secrets
import tempfile
import io
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, disconnect
import jwt
from faster_whisper import WhisperModel
from transformers import MarianMTModel, MarianTokenizer
import pyttsx3
import torch

# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
CORS(app, supports_credentials=True, origins="*")
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    max_http_buffer_size=20_000_000,
    ping_timeout=120,
    ping_interval=30,
)

JWT_SECRET = secrets.token_hex(32)
DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

# ── Load faster-whisper ───────────────────────────────────────────────────────
print("⏳  Loading faster-whisper ...")
whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
print("✅  faster-whisper ready!")

# ── Helsinki-NLP Translation Models ──────────────────────────────────────────
print("⏳  Loading translation models ...")

TRANSLATION_MODELS = {}

MODEL_PAIRS = [
    ("en", "ur", "Helsinki-NLP/opus-mt-en-ur"),
    ("ur", "en", "Helsinki-NLP/opus-mt-ur-en"),
    ("en", "ar", "Helsinki-NLP/opus-mt-en-ar"),
    ("ar", "en", "Helsinki-NLP/opus-mt-ar-en"),
    ("en", "zh", "Helsinki-NLP/opus-mt-en-zh"),
    ("zh", "en", "Helsinki-NLP/opus-mt-zh-en"),
]

for src, tgt, model_name in MODEL_PAIRS:
    try:
        tokenizer = MarianTokenizer.from_pretrained(model_name)
        model     = MarianMTModel.from_pretrained(model_name)
        TRANSLATION_MODELS[f"{src}-{tgt}"] = (model, tokenizer)
        print(f"  ✅ {src} → {tgt}")
    except Exception as e:
        print(f"  ❌ {src} → {tgt}: {e}")

print("✅  All translation models ready!")

# ── Supported Languages ───────────────────────────────────────────────────────
LANGUAGES = {
    "en": "English",
    "ur": "Urdu",
    "ar": "Arabic",
    "zh": "Chinese",
}

# ─────────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email    TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS translations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                source_lang TEXT,
                target_lang TEXT,
                source_text TEXT,
                translated  TEXT,
                created     TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        conn.commit()

init_db()

# ── Auth ──────────────────────────────────────────────────────────────────────
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def make_token(user_id, username):
    return jwt.encode(
        {"user_id": user_id, "username": username, "exp": datetime.utcnow() + timedelta(hours=24)},
        JWT_SECRET, algorithm="HS256"
    )

def verify_token(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except:
        return None

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        data  = verify_token(token)
        if not data:
            return jsonify({"error": "Invalid or expired token"}), 401
        return f(data, *args, **kwargs)
    return decorated

# ── REST Auth ─────────────────────────────────────────────────────────────────
@app.route("/api/register", methods=["POST"])
def register():
    body = request.get_json() or {}
    username = body.get("username", "").strip()
    email    = body.get("email", "").strip()
    password = body.get("password", "")
    if not username or not email or not password:
        return jsonify({"error": "All fields required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password min 6 characters"}), 400
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO users (username,email,password) VALUES (?,?,?)",
                         (username, email, hash_pw(password)))
            conn.commit()
        return jsonify({"message": "Account created!"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username or email taken"}), 409

@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json() or {}
    username = body.get("username", "").strip()
    password = body.get("password", "")
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username=? AND password=?",
                            (username, hash_pw(password))).fetchone()
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401
    return jsonify({"token": make_token(user["id"], user["username"]),
                    "username": user["username"], "message": "Login successful"})

@app.route("/api/languages", methods=["GET"])
def languages():
    return jsonify(LANGUAGES)

@app.route("/api/history", methods=["GET"])
@token_required
def history(user_data):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM translations WHERE user_id=? ORDER BY created DESC LIMIT 50",
            (user_data["user_id"],)).fetchall()
    return jsonify([dict(r) for r in rows])

# ── Offline Translation ───────────────────────────────────────────────────────
def translate_offline(text, src_lang, tgt_lang):
    """Translate using Helsinki-NLP models — fully offline"""
    if src_lang == tgt_lang:
        return text

    key = f"{src_lang}-{tgt_lang}"

    # Direct translation
    if key in TRANSLATION_MODELS:
        model, tokenizer = TRANSLATION_MODELS[key]
        inputs  = tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=512)
        with torch.no_grad():
            outputs = model.generate(**inputs, max_length=512)
        return tokenizer.decode(outputs[0], skip_special_tokens=True)

    # Pivot through English
    en_key1 = f"{src_lang}-en"
    en_key2 = f"en-{tgt_lang}"
    if en_key1 in TRANSLATION_MODELS and en_key2 in TRANSLATION_MODELS:
        # Step 1: src -> English
        model1, tok1 = TRANSLATION_MODELS[en_key1]
        inputs1 = tok1(text, return_tensors="pt", padding=True, truncation=True, max_length=512)
        with torch.no_grad():
            out1 = model1.generate(**inputs1, max_length=512)
        english_text = tok1.decode(out1[0], skip_special_tokens=True)

        # Step 2: English -> tgt
        model2, tok2 = TRANSLATION_MODELS[en_key2]
        inputs2 = tok2(english_text, return_tensors="pt", padding=True, truncation=True, max_length=512)
        with torch.no_grad():
            out2 = model2.generate(**inputs2, max_length=512)
        return tok2.decode(out2[0], skip_special_tokens=True)

    return f"[Translation not available for {src_lang}→{tgt_lang}]"

# ── Offline TTS ───────────────────────────────────────────────────────────────
def tts_offline(text, lang):
    """Generate speech using pyttsx3 — fully offline"""
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", 150)
        engine.setProperty("volume", 1.0)

        # Set voice based on language
        voices = engine.getProperty("voices")
        for voice in voices:
            if lang == "ur" and "urdu" in voice.name.lower():
                engine.setProperty("voice", voice.id)
                break
            elif lang == "ar" and "arabic" in voice.name.lower():
                engine.setProperty("voice", voice.id)
                break
            elif lang == "zh" and ("chinese" in voice.name.lower() or "mandarin" in voice.name.lower()):
                engine.setProperty("voice", voice.id)
                break

        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp_path = tf.name

        engine.save_to_file(text, tmp_path)
        engine.runAndWait()
        engine.stop()

        with open(tmp_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()
        os.unlink(tmp_path)
        return audio_b64
    except Exception as e:
        print(f"TTS error: {e}")
        return ""

# ── Main Pipeline ─────────────────────────────────────────────────────────────
def run_pipeline(audio_bytes, src_lang, tgt_lang, user_id):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tf.write(audio_bytes)
        tmp_path = tf.name

    try:
        # 1. Speech to Text
        whisper_lang = src_lang if src_lang not in ("auto", None, "") else None
        segments, info = whisper_model.transcribe(
            tmp_path,
            language=whisper_lang,
            beam_size=3,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=300),
        )
        source_text = " ".join(seg.text.strip() for seg in segments).strip()
        detected    = info.language if info.language else (src_lang or "en")

        if not source_text:
            return {"error": "No speech detected. Please speak clearly."}

        # 2. Offline Translation
        translated = translate_offline(source_text, detected, tgt_lang)

        # 3. Offline TTS
        audio_b64 = tts_offline(translated, tgt_lang)

        # 4. Save to DB
        with get_db() as conn:
            conn.execute(
                "INSERT INTO translations (user_id,source_lang,target_lang,source_text,translated) VALUES (?,?,?,?,?)",
                (user_id, detected, tgt_lang, source_text, translated),
            )
            conn.commit()

        return {
            "source_text":   source_text,
            "translated":    translated,
            "detected_lang": detected,
            "target_lang":   tgt_lang,
            "audio_b64":     audio_b64,
        }
    finally:
        os.unlink(tmp_path)

# ── REST Translate ────────────────────────────────────────────────────────────
@app.route("/api/translate", methods=["POST"])
@token_required
def translate_rest(user_data):
    body      = request.get_json() or {}
    audio_b64 = body.get("audio")
    src_lang  = body.get("source_lang", "auto")
    tgt_lang  = body.get("target_lang", "ur")
    if not audio_b64:
        return jsonify({"error": "No audio data"}), 400
    try:
        result = run_pipeline(base64.b64decode(audio_b64), src_lang, tgt_lang, user_data["user_id"])
        if "error" in result:
            return jsonify(result), 422
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── WebSocket ─────────────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    token = request.args.get("token", "")
    data  = verify_token(token)
    if not data:
        disconnect(); return False
    emit("connected", {"message": f"Welcome {data['username']}! Offline mode ready."})

@socketio.on("translate_audio")
def on_translate_audio(payload):
    token = request.args.get("token", "")
    user  = verify_token(token)
    if not user:
        emit("error", {"message": "Unauthorized"}); return

    audio_b64 = payload.get("audio")
    src_lang  = payload.get("source_lang", "auto")
    tgt_lang  = payload.get("target_lang", "ur")

    if not audio_b64:
        emit("error", {"message": "No audio"}); return

    emit("processing", {"message": "⚡ Translating offline..."})

    try:
        result = run_pipeline(base64.b64decode(audio_b64), src_lang, tgt_lang, user["user_id"])
        if "error" in result:
            emit("error", result)
        else:
            emit("translation_result", result)
    except Exception as e:
        emit("error", {"message": f"Error: {str(e)}"})

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀  VoiceBridge v3 — OFFLINE MODE")
    print("    http://localhost:5000")
    print("    Languages: English | Urdu | Arabic | Chinese")
    print("    Press Ctrl+C to stop.")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)