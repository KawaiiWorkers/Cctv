"""
Real-Time CCTV & Surveillance Web App
Flask + WebRTC + Supabase
"""
import os
import io
import json
import time
import uuid
import threading
import hashlib
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify, session,
    redirect, url_for, Response, abort, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# ──────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB max chunk upload

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "footages")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = generate_password_hash(os.getenv("ADMIN_PASSWORD", "admin"))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

# ──────────────────────────────────────────────────────────────────
# Thread-Safe In-Memory State
# ──────────────────────────────────────────────────────────────────
_state_lock = threading.RLock()

# camera_id -> {info}
CAMERAS = {}
# camera_id -> [ {candidate} ]  (incoming ICE candidates for camera)
CAMERA_ICE = {}
# session_id -> {offer, camera_id, answer, created_at}
WEBRTC_SESSIONS = {}
# admin_id -> [ {candidate, session_id} ] (incoming ICE for admin)
ADMIN_ICE = {}

CAMERA_TTL = 45  # seconds before camera considered offline
SESSION_TTL = 60  # seconds for webrtc session cleanup


def cleanup_loop():
    """Background thread that removes stale cameras and expired sessions."""
    while True:
        time.sleep(15)
        now = time.time()
        with _state_lock:
            stale_cams = [
                cid for cid, info in CAMERAS.items()
                if now - info.get("last_ping", 0) > CAMERA_TTL
            ]
            for cid in stale_cams:
                CAMERAS.pop(cid, None)
                CAMERA_ICE.pop(cid, None)
                print(f"[cleanup] Removed stale camera {cid}")

            stale_sessions = [
                sid for sid, sess in WEBRTC_SESSIONS.items()
                if now - sess.get("created_at", 0) > SESSION_TTL and sess.get("answer")
            ]
            for sid in stale_sessions:
                WEBRTC_SESSIONS.pop(sid, None)
                print(f"[cleanup] Removed expired webrtc session {sid}")


threading.Thread(target=cleanup_loop, daemon=True).start()


# ──────────────────────────────────────────────────────────────────
# Auth Decorators
# ──────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ──────────────────────────────────────────────────────────────────
# Page Routes
# ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if session.get("is_admin"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session["is_admin"] = True
            session["admin_id"] = hashlib.sha256(
                (username + str(time.time())).encode()
            ).hexdigest()[:16]
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials"), 401
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/camera")
def camera():
    # The camera page generates its own ID client-side and registers via API.
    return render_template("camera.html")


@app.route("/footage")
@login_required
def footage():
    return render_template("footage.html")


# ──────────────────────────────────────────────────────────────────
# Camera Registration & Heartbeat API
# ──────────────────────────────────────────────────────────────────
@app.route("/api/cameras/register", methods=["POST"])
def register_camera():
    data = request.get_json(force=True, silent=True) or {}
    camera_id = data.get("camera_id") or f"cam-{uuid.uuid4().hex[:8]}"
    name = data.get("name", "Untitled Camera")
    with _state_lock:
        CAMERAS[camera_id] = {
            "camera_id": camera_id,
            "name": name,
            "last_ping": time.time(),
            "registered_at": time.time(),
            "ip": request.remote_addr,
            "user_agent": request.headers.get("User-Agent", "")[:120],
        }
        CAMERA_ICE.setdefault(camera_id, [])
    return jsonify({"camera_id": camera_id, "status": "registered"})


@app.route("/api/cameras/ping", methods=["POST"])
def camera_ping():
    data = request.get_json(force=True, silent=True) or {}
    camera_id = data.get("camera_id")
    if not camera_id:
        return jsonify({"error": "missing camera_id"}), 400
    with _state_lock:
        if camera_id in CAMERAS:
            CAMERAS[camera_id]["last_ping"] = time.time()
            if "name" in data:
                CAMERAS[camera_id]["name"] = data["name"]
            return jsonify({"status": "ok"})
        # auto re-register if missing
        CAMERAS[camera_id] = {
            "camera_id": camera_id,
            "name": data.get("name", "Untitled Camera"),
            "last_ping": time.time(),
            "registered_at": time.time(),
            "ip": request.remote_addr,
            "user_agent": request.headers.get("User-Agent", "")[:120],
        }
        CAMERA_ICE.setdefault(camera_id, [])
    return jsonify({"status": "re-registered"})


@app.route("/api/cameras")
@api_login_required
def list_cameras():
    now = time.time()
    with _state_lock:
        out = []
        for cid, info in CAMERAS.items():
            out.append({
                "camera_id": cid,
                "name": info.get("name"),
                "online": (now - info.get("last_ping", 0)) < CAMERA_TTL,
                "last_ping_ago": round(now - info.get("last_ping", 0), 1),
            })
    return jsonify(out)


# ──────────────────────────────────────────────────────────────────
# WebRTC Signaling
# ──────────────────────────────────────────────────────────────────
# Flow:
#   1. Admin → POST /api/webrtc/offer {camera_id, sdp}  → creates session
#   2. Camera → GET /api/webrtc/poll?camera_id=...   → grabs pending offer
#   3. Camera → POST /api/webrtc/answer {session_id, sdp}
#   4. Admin → GET /api/webrtc/answer?session_id=...
#   5. Both exchange ICE candidates via /api/webrtc/ice
# ──────────────────────────────────────────────────────────────────

@app.route("/api/webrtc/offer", methods=["POST"])
@api_login_required
def webrtc_offer():
    """Admin sends an SDP offer to a specific camera."""
    data = request.get_json(force=True, silent=True) or {}
    camera_id = data.get("camera_id")
    sdp = data.get("sdp")
    if not camera_id or not sdp:
        return jsonify({"error": "missing camera_id or sdp"}), 400
    with _state_lock:
        if camera_id not in CAMERAS:
            return jsonify({"error": "camera not found"}), 404
        session_id = f"sess-{uuid.uuid4().hex[:12]}"
        WEBRTC_SESSIONS[session_id] = {
            "session_id": session_id,
            "camera_id": camera_id,
            "admin_id": session.get("admin_id"),
            "offer": sdp,
            "answer": None,
            "created_at": time.time(),
            "consumed_by_camera": False,
        }
        # Mark pending offer for camera to pick up
        CAMERAS[camera_id]["pending_offer"] = {
            "session_id": session_id,
            "sdp": sdp,
        }
    return jsonify({"session_id": session_id})


@app.route("/api/webrtc/poll", methods=["GET"])
def webrtc_poll():
    """Camera polls for pending WebRTC offers and incoming ICE candidates."""
    camera_id = request.args.get("camera_id")
    if not camera_id:
        return jsonify({"error": "missing camera_id"}), 400
    with _state_lock:
        if camera_id not in CAMERAS:
            return jsonify({"error": "camera not registered"}), 404
        offer = CAMERAS[camera_id].pop("pending_offer", None)
        ice_candidates = CAMERA_ICE.get(camera_id, [])
        CAMERA_ICE[camera_id] = []
    return jsonify({
        "offer": offer,  # {session_id, sdp} or None
        "ice_candidates": ice_candidates,
    })


@app.route("/api/webrtc/answer", methods=["POST", "GET"])
def webrtc_answer():
    """Camera POSTs its SDP answer; Admin GETs it via session_id."""
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        session_id = data.get("session_id")
        sdp = data.get("sdp")
        if not session_id or not sdp:
            return jsonify({"error": "missing session_id or sdp"}), 400
        with _state_lock:
            if session_id not in WEBRTC_SESSIONS:
                return jsonify({"error": "session not found"}), 404
            WEBRTC_SESSIONS[session_id]["answer"] = sdp
            WEBRTC_SESSIONS[session_id]["answered_at"] = time.time()
        return jsonify({"status": "ok"})

    # GET — admin retrieves the answer
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "missing session_id"}), 400
    with _state_lock:
        sess = WEBRTC_SESSIONS.get(session_id)
        if not sess:
            return jsonify({"error": "session not found"}), 404
        answer = sess.get("answer")
        # Collect ICE candidates queued for this admin session
        admin_id = sess.get("admin_id")
        ice = ADMIN_ICE.get(admin_id, [])
        ADMIN_ICE[admin_id] = [c for c in ice if c.get("session_id") != session_id]
        admin_ice_for_session = [c for c in ice if c.get("session_id") == session_id]
    return jsonify({
        "answer": answer,
        "ice_candidates": admin_ice_for_session,
    })


@app.route("/api/webrtc/ice", methods=["POST", "GET"])
def webrtc_ice():
    """
    POST: Either camera or admin pushes an ICE candidate.
      Body: {session_id, candidate, from: 'camera'|'admin'}
    GET:  Admin pulls ICE candidates for a session (also handled by answer GET).
    """
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        session_id = data.get("session_id")
        candidate = data.get("candidate")
        src = data.get("from", "camera")
        if not session_id or candidate is None:
            return jsonify({"error": "missing session_id or candidate"}), 400
        with _state_lock:
            sess = WEBRTC_SESSIONS.get(session_id)
            if not sess:
                return jsonify({"error": "session not found"}), 404
            if src == "camera":
                # send to admin
                admin_id = sess.get("admin_id")
                ADMIN_ICE.setdefault(admin_id, []).append({
                    "session_id": session_id,
                    "candidate": candidate,
                })
            else:
                # send to camera
                camera_id = sess.get("camera_id")
                CAMERA_ICE.setdefault(camera_id, []).append(candidate)
        return jsonify({"status": "ok"})

    # GET: pull ICE for camera by session_id
    session_id = request.args.get("session_id")
    camera_id = request.args.get("camera_id")
    with _state_lock:
        if session_id:
            sess = WEBRTC_SESSIONS.get(session_id)
            if not sess:
                return jsonify({"error": "session not found"}), 404
            cid = sess.get("camera_id")
            ice = CAMERA_ICE.get(cid, [])
            CAMERA_ICE[cid] = []
            return jsonify({"ice_candidates": ice})
        if camera_id:
            ice = CAMERA_ICE.get(camera_id, [])
            CAMERA_ICE[camera_id] = []
            return jsonify({"ice_candidates": ice})
    return jsonify({"error": "missing session_id or camera_id"}), 400


# ──────────────────────────────────────────────────────────────────
# Footage Upload (from camera)
# ──────────────────────────────────────────────────────────────────
@app.route("/api/footage/upload", methods=["POST"])
def upload_footage():
    if "video" not in request.files:
        return jsonify({"error": "no video file"}), 400
    file = request.files["video"]
    camera_id = request.form.get("camera_id", "unknown")
    camera_name = request.form.get("camera_name", "Untitled")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_cam = "".join(c if c.isalnum() else "_" for c in camera_id)[:24]
    object_path = f"{safe_cam}/{ts}_{uuid.uuid4().hex[:6]}.webm"

    file_bytes = file.read()
    if not file_bytes:
        return jsonify({"error": "empty file"}), 400

    if not supabase:
        return jsonify({"error": "storage not configured"}), 500
    try:
        supabase.storage.from_(SUPABASE_BUCKET).upload(
            path=object_path,
            file=file_bytes,
            file_options={
                "content-type": "video/webm",
                "upsert": "false",
                "metadata": {
                    "camera_id": camera_id,
                    "camera_name": camera_name,
                    "uploaded_at": ts,
                }
            },
        )
        # Insert metadata row for fast listing without scanning storage
        try:
            supabase.table("footages").insert({
                "object_path": object_path,
                "camera_id": camera_id,
                "camera_name": camera_name,
                "size_bytes": len(file_bytes),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as db_err:
            print(f"[db] metadata insert failed (non-fatal): {db_err}")
        return jsonify({"status": "uploaded", "path": object_path})
    except Exception as e:
        print(f"[upload] error: {e}")
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────────
# Footage Listing & Streaming (signed URLs / backend proxy)
# ──────────────────────────────────────────────────────────────────
@app.route("/api/footage/list")
@api_login_required
def footage_list():
    """Return list of footage metadata with signed URLs."""
    try:
        # Try metadata DB first
        items = []
        try:
            res = supabase.table("footages") \
                .select("object_path,camera_id,camera_name,size_bytes,created_at") \
                .order("created_at", desc=True) \
                .limit(200) \
                .execute()
            items = res.data or []
        except Exception:
            # Fallback: list objects in storage
            res = supabase.storage.from_(SUPABASE_BUCKET).list()
            for f in res:
                if f.get("name", "").endswith(".webm"):
                    items.append({
                        "object_path": f["name"],
                        "camera_id": "unknown",
                        "camera_name": "Unknown",
                        "size_bytes": f.get("metadata", {}).get("size", 0),
                        "created_at": f.get("created_at"),
                    })

        # Generate signed URLs (10 minutes)
        out = []
        for it in items:
            path = it["object_path"]
            try:
                url = supabase.storage.from_(SUPABASE_BUCKET).create_signed_url(
                    path, expires_in=600
                )["signedURL"]
            except Exception as e:
                print(f"[signed-url] failed for {path}: {e}")
                url = None
            out.append({
                "object_path": path,
                "camera_id": it.get("camera_id"),
                "camera_name": it.get("camera_name", "Unknown"),
                "size_bytes": it.get("size_bytes", 0),
                "created_at": it.get("created_at"),
                "signed_url": url,
            })
        return jsonify(out)
    except Exception as e:
        print(f"[footage-list] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/footage/delete", methods=["POST"])
@api_login_required
def footage_delete():
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("object_path")
    if not path:
        return jsonify({"error": "missing object_path"}), 400
    try:
        supabase.storage.from_(SUPABASE_BUCKET).remove([path])
        try:
            supabase.table("footages").delete().eq("object_path", path).execute()
        except Exception:
            pass
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok", "cameras": len(CAMERAS)})


# ──────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
