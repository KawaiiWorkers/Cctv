"""
CCTV Monitoring System - WebRTC & Supabase Edition
==================================================
"""

import os
import time
import threading
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, render_template, redirect, url_for,
    session, jsonify, Response, abort
)
from supabase import create_client, Client

# ---------- Config ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

if os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
SECRET_KEY     = os.environ.get("SECRET_KEY", "change-me")

# Supabase Config
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    print("[WARNING] Supabase URL or KEY is missing. Uploads will fail.")

# ---------- App ----------
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024   # 500 MB max chunk

# State Management
cameras = {}
webrtc_signals = {}
cameras_lock = threading.Lock()
CAMERA_TIMEOUT = 15 

# ---------- Helpers ----------
def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapper

def touch_camera(cam_id, name=None, frame=None):
    with cameras_lock:
        entry = cameras.setdefault(cam_id, {"last_seen": 0, "last_frame": None, "name": cam_id})
        entry["last_seen"] = time.time()
        if name:
            entry["name"] = name
        if frame is not None:
            entry["last_frame"] = frame

def get_live_cameras():
    now = time.time()
    out = []
    with cameras_lock:
        for cid, info in cameras.items():
            online = (now - info["last_seen"]) < CAMERA_TIMEOUT
            out.append({
                "id": cid,
                "name": info["name"],
                "online": online,
                "last_seen": datetime.fromtimestamp(info["last_seen"]).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return out

# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return """
    <html><head><title>CCTV System</title>
    <style>body{font-family:sans-serif;background:#111;color:#eee;text-align:center;padding:60px}
    a{color:#4af;font-size:22px;display:block;margin:20px}</style></head>
    <body>
    <h1>🎥 CCTV Monitoring System</h1>
    <a href="/camera">📱 Turn this device into a camera</a>
    <a href="/admin">🔒 Admin login</a>
    </body></html>
    """

@app.route("/camera")
def camera_page():
    return render_template("camera.html")

@app.route("/frame/<cam_id>", methods=["POST"])
def upload_frame(cam_id):
    name  = request.args.get("name", cam_id)
    data  = request.get_data()
    if data:
        touch_camera(cam_id, name=name, frame=data)
    return "ok"

@app.route("/preview/<cam_id>")
@login_required
def get_preview(cam_id):
    with cameras_lock:
        info = cameras.get(cam_id)
        frame = info["last_frame"] if info else None
    if not frame:
        abort(404)
    return Response(frame, mimetype="image/jpeg")

@app.route("/upload", methods=["POST"])
def upload_chunk():
    cam_id     = request.form.get("camera_id", "unknown")
    started_at = request.form.get("started_at")   
    file       = request.files.get("video")
    
    if not file:
        return jsonify(ok=False, error="no file"), 400

    try:
        ts = datetime.fromisoformat(started_at.replace("Z", ""))
    except Exception:
        ts = datetime.utcnow()

    safe_cam = "".join(c for c in cam_id if c.isalnum() or c in "-_")[:40] or "cam"
    fname = f"{safe_cam}_{ts.strftime('%Y-%m-%d_%H-%M-%S')}.webm"
    
    if supabase:
        try:
            file_bytes = file.read()
            supabase.storage.from_("footages").upload(
                path=fname, 
                file=file_bytes, 
                file_options={"content-type": "video/webm"}
            )
        except Exception as e:
            print(f"[Supabase Upload Error] {e}")
            return jsonify(ok=False, error=str(e)), 500

    touch_camera(cam_id, name=request.form.get("name", cam_id))
    return jsonify(ok=True, file=fname)

# ---------- WebRTC Signaling ----------
@app.route("/webrtc/<cam_id>/<action>", methods=["GET", "POST"])
def webrtc_signaling(cam_id, action):
    with cameras_lock:
        if cam_id not in webrtc_signals:
            webrtc_signals[cam_id] = {'offer': None, 'answer': None, 'admin_ice': [], 'cam_ice': [], 'offer_id': 0}
        sig = webrtc_signals[cam_id]

    if request.method == "POST":
        data = request.get_json(force=True)
        if action == "offer":
            sig['offer'] = data
            sig['answer'] = None
            sig['admin_ice'].clear()
            sig['cam_ice'].clear()
            sig['offer_id'] += 1
        elif action == "answer":
            sig['answer'] = data
        elif action == "admin_ice":
            sig['admin_ice'].append(data)
        elif action == "cam_ice":
            sig['cam_ice'].append(data)
        return jsonify(ok=True)
    else:
        if action == "offer":
            return jsonify({'offer': sig['offer'], 'id': sig['offer_id']})
        elif action == "answer":
            return jsonify(sig['answer'])
        elif action == "admin_ice":
            res = list(sig['admin_ice'])
            sig['admin_ice'].clear()
            return jsonify(res)
        elif action == "cam_ice":
            res = list(sig['cam_ice'])
            sig['cam_ice'].clear()
            return jsonify(res)
    return jsonify(None)

# ---------- Admin side ----------
@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("dashboard"))
        error = "Wrong password"
    return f"""
    <html><head><title>Admin Login</title>
    <style>body{{font-family:sans-serif;background:#111;color:#eee;
    display:flex;align-items:center;justify-content:center;height:100vh}}
    form{{background:#222;padding:30px;border-radius:12px;min-width:300px}}
    input{{width:100%;padding:10px;margin:8px 0;border-radius:6px;border:0;background:#333;color:#fff}}
    button{{width:100%;padding:10px;background:#4af;color:#000;border:0;border-radius:6px;font-weight:bold;cursor:pointer}}
    .err{{color:#f66}}</style></head>
    <body><form method="post">
    <h2>🔒 Admin Login</h2>
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button>Login</button>
    {"<p class='err'>"+error+"</p>" if error else ""}
    </form></body></html>
    """

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")

@app.route("/api/cameras")
@login_required
def api_cameras():
    return jsonify(get_live_cameras())

# ---------- Footage ----------
@app.route("/footage")
@login_required
def footage_list():
    files = []
    if supabase:
        try:
            res = supabase.storage.from_("footages").list()
            for f in res:
                if f.get("name") == ".emptyFolderPlaceholder": 
                    continue
                meta = f.get("metadata", {})
                size = meta.get("size", 0)
                files.append({
                    "name": f.get("name"),
                    "size_mb": round(size / 1024 / 1024, 2),
                    "modified": f.get("created_at", "Unknown")[:19].replace("T", " ")
                })
            files.sort(key=lambda x: x["name"], reverse=True)
        except Exception as e:
            print(f"[Supabase List Error] {e}")
    return render_template("footage.html", files=files)

@app.route("/footage/<path:filename>")
@login_required
def footage_file(filename):
    if supabase:
        try:
            # Generate a secure signed URL valid for 1 hour
            res = supabase.storage.from_("footages").create_signed_url(filename, 3600)
            return redirect(res.get("signedURL"))
        except Exception as e:
            print(f"[Supabase Signed URL Error] {e}")
            abort(404)
    abort(404)

if __name__ == "__main__":
    print(f"[CCTV] Connected to Supabase: {'Yes' if supabase else 'No'}")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
