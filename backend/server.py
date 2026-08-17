import eventlet
eventlet.monkey_patch()

"""
VoiceBridge — server.py (v5 Fast)
faster-whisper + deep-translator (Google) + pyttsx3
Fast: 1-2 second translation
WebSocket fixed with eventlet
"""

import os
import base64
import sqlite3
import hashlib
import secrets
import tempfile
import io
import threading
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, disconnect
import jwt
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator
import pyttsx3
import torch

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
CORS(app, supports_credentials=True, origins="*")
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    max_http_buffer_size=20_000_000,
    ping_timeout=60,
    ping_interval=25,
)

JWT_SECRET = secrets.token_hex(32)
DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

print("⏳  Loading faster-whisper ...")
whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
print("✅  faster-whisper ready!")

LANGUAGES = {"en":"English","ur":"Urdu","ar":"Arabic","zh":"Chinese","es":"Spanish","fr":"French","de":"German","hi":"Hindi"}

_tts_lock = threading.Lock()

def tts_to_wav(text, lang):
    with _tts_lock:
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 150)
            engine.setProperty("volume", 1.0)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tmp = tf.name
            engine.save_to_file(text, tmp)
            engine.runAndWait()
            engine.stop()
            with open(tmp, "rb") as f:
                data = f.read()
            os.unlink(tmp)
            return base64.b64encode(data).decode()
        except Exception as e:
            print(f"TTS error: {e}")
            return ""

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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

init_db()

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()
def make_token(uid, uname):
    return jwt.encode({"user_id":uid,"username":uname,"exp":datetime.utcnow()+timedelta(hours=24)},JWT_SECRET,algorithm="HS256")
def verify_token(token):
    try: return jwt.decode(token,JWT_SECRET,algorithms=["HS256"])
    except: return None
def token_required(f):
    @wraps(f)
    def decorated(*args,**kwargs):
        token=request.headers.get("Authorization","").replace("Bearer ","").strip()
        data=verify_token(token)
        if not data: return jsonify({"error":"Invalid or expired token"}),401
        return f(data,*args,**kwargs)
    return decorated

@app.route("/api/register",methods=["POST"])
def register():
    b=request.get_json() or {}
    u,e,p=b.get("username","").strip(),b.get("email","").strip(),b.get("password","")
    if not u or not e or not p: return jsonify({"error":"All fields required"}),400
    if len(p)<6: return jsonify({"error":"Password min 6 chars"}),400
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO users(username,email,password)VALUES(?,?,?)",(u,e,hash_pw(p)))
            conn.commit()
        return jsonify({"message":"Account created!"}),201
    except sqlite3.IntegrityError: return jsonify({"error":"Username or email taken"}),409

@app.route("/api/login",methods=["POST"])
def login():
    b=request.get_json() or {}
    u,p=b.get("username","").strip(),b.get("password","")
    with get_db() as conn:
        user=conn.execute("SELECT * FROM users WHERE username=? AND password=?",(u,hash_pw(p))).fetchone()
    if not user: return jsonify({"error":"Invalid credentials"}),401
    return jsonify({"token":make_token(user["id"],user["username"]),"username":user["username"],"message":"Login successful"})

@app.route("/api/languages",methods=["GET"])
def languages(): return jsonify(LANGUAGES)

@app.route("/api/history",methods=["GET"])
@token_required
def history(user_data):
    with get_db() as conn:
        rows=conn.execute("SELECT * FROM translations WHERE user_id=? ORDER BY created DESC LIMIT 50",(user_data["user_id"],)).fetchall()
    return jsonify([dict(r) for r in rows])

def fast_translate(text, tgt_lang):
    """Fast Google translation with timeout"""
    try:
        return GoogleTranslator(source="auto", target=tgt_lang).translate(text)
    except Exception as e:
        print(f"Translation error: {e}")
        return text

def run_pipeline(audio_bytes, src_lang, tgt_lang, user_id):
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tf:
        tf.write(audio_bytes)
        tmp_path = tf.name
    try:
        # STT — faster-whisper
        wlang = src_lang if src_lang not in ("auto", None, "") else None
        segments, info = whisper_model.transcribe(
            tmp_path,
            language=wlang,
            beam_size=2,         # faster — was 3
            best_of=1,           # faster
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=200),
        )
        source_text = " ".join(s.text.strip() for s in segments).strip()
        detected = info.language or (src_lang or "en")

        if not source_text:
            return {"error": "silence"}

        # Fast Google Translation
        translated = fast_translate(source_text, tgt_lang)

        # TTS — offline pyttsx3
        audio_b64 = tts_to_wav(translated, tgt_lang)

        # Save to DB
        with get_db() as conn:
            conn.execute(
                "INSERT INTO translations(user_id,source_lang,target_lang,source_text,translated)VALUES(?,?,?,?,?)",
                (user_id, detected, tgt_lang, source_text, translated)
            )
            conn.commit()

        return {
            "source_text": source_text,
            "translated": translated,
            "detected_lang": detected,
            "target_lang": tgt_lang,
            "audio_b64": audio_b64,
        }
    finally:
        os.unlink(tmp_path)

@app.route("/api/translate",methods=["POST"])
@token_required
def translate_rest(user_data):
    b=request.get_json() or {}
    ab=b.get("audio"); sl=b.get("source_lang","auto"); tl=b.get("target_lang","ur")
    if not ab: return jsonify({"error":"No audio"}),400
    try:
        r=run_pipeline(base64.b64decode(ab),sl,tl,user_data["user_id"])
        if "error" in r: return jsonify(r),422
        return jsonify(r)
    except Exception as e: return jsonify({"error":str(e)}),500

@socketio.on("connect")
def on_connect():
    token=request.args.get("token","")
    data=verify_token(token)
    if not data: disconnect(); return False
    emit("connected",{"message":f"Welcome {data['username']}! Ready."})

@socketio.on("translate_audio")
def on_translate_audio(payload):
    token=request.args.get("token","")
    user=verify_token(token)
    if not user: emit("error",{"message":"Unauthorized"}); return
    ab=payload.get("audio"); sl=payload.get("source_lang","auto"); tl=payload.get("target_lang","ur")
    if not ab: emit("error",{"message":"No audio"}); return
    emit("processing",{"message":"⚡ Translating..."})
    try:
        r=run_pipeline(base64.b64decode(ab),sl,tl,user["user_id"])
        if "error" in r:
            if r["error"]=="silence": emit("silence",{})
            else: emit("error",r)
        else:
            emit("translation_result",r)
    except Exception as e:
        emit("error",{"message":str(e)})

if __name__=="__main__":
    print("🚀  VoiceBridge v5 — FAST MODE")
    print("    http://localhost:5000")
    print("    Translation: Google (fast) + pyttsx3 TTS (offline)")
    socketio.run(app, host="0.0.0.0", port=5000)