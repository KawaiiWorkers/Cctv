"""
Real-time CCTV & Surveillance Web Application
Flask + WebRTC signaling + Supabase private storage
"""
import os
import json
import time
import uuid
import threading
from functools import wraps
from datetime import datetime
from io import BytesIO

from flask import (
    Flask, request, jsonify, render_template,
    redirect, url_for, session, Response, abort, stream_with_context
)
from supabase import create_client, Client

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY      = os.environ.get("SUPABASE_SERVICE_KEY", "")   # service role key
SUPABASE_BUCKET   = os.environ.get("SUPABASE_BUCKET", "footages")
ADMIN_USERNAME    = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD    = os.environ.get("ADMIN_PASSWORD", "admin123")
SECRET_KEY        = os.environ.get("FLASK_SECRET", "change-me-in-production-" + uuid.uuid4().hex)
PING_TIMEOUT      = 15          # seconds until camera considered OFFLINE
SIGNED_URL_TTL    = 60 * 30     # 30-minute signed URL for playback

# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------
supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"[WARN] Supabase init failed: {e}")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024   # 50 MB per chunk upload

# ---------------------------------------------------------------------------
# Thread-safe in-memory state
# ---------------------------------------------------------------------------
_state_lock = threading.RLock()

# camera_id -> {"name": str, "last_ping": float, "registered": float}
# NOTE: this is only a fast local cache. The source of truth is a JSON file
# in the Supabase bucket (REGISTRY_PATH), so the registry survives process
# restarts and is shared across every gunicorn worker / dyno instead of
# living in one process's private memory.
cameras: dict[str, dict] = {}
REGISTRY_PATH      = "_registry/cameras.json"
_registry_synced   = 0.0
REGISTRY_CACHE_TTL = 3   # seconds before we re-read the registry from Supabase


def _load_registry(force: bool = False):
    """Refresh the in-memory `cameras` cache from the Supabase-backed registry."""
    global _registry_synced
    if supabase is None:
        return cameras
    if not force and (_now() - _registry_synced) < REGISTRY_CACHE_TTL:
        return cameras
    try:
        blob = supabase.storage.from_(SUPABASE_BUCKET).download(REGISTRY_PATH)
        data = json.loads(blob.decode("utf-8"))
        with _state_lock:
            cameras.clear()
            cameras.update(data)
            _registry_synced = _now()
    except Exception:
        # registry file doesn't exist yet (first run) or a transient error;
        # don't wipe out whatever we already have cached locally.
        _registry_synced = _now()
    return cameras


def _save_registry():
    """Persist the current `cameras` cache back to the Supabase-backed registry."""
    global _registry_synced
    if supabase is None:
        return
    try:
        with _state_lock:
            payload = json.dumps(cameras).encode("utf-8")
        supabase.storage.from_(SUPABASE_BUCKET).upload(
            path=REGISTRY_PATH,
            file=payload,
            file_options={"content-type": "application/json", "upsert": "true"},
        )
        _registry_synced = _now()
    except Exception as e:
        print(f"[WARN] camera registry save failed: {e}")

# camera_id -> list of pending offers from admin  (admin -> camera)
# each item: {"sdp": str, "type": "offer", "session": str, "ts": float}
pending_offers: dict[str, list] = {}

# session_id -> answer from camera (camera -> admin)
pending_answers: dict[str, dict] = {}

# session_id -> list of ICE candidates queued FOR that session (both directions)
# keyed further by "to": "camera" | "admin"
ice_queue: dict[str, dict[str, list]] = {}


def _now() -> float:
    return time.time()


def _cleanup_stale():
    """Remove stale sessions / offline camera housekeeping (called opportunistically)."""
    cutoff = _now() - 300  # 5 min
    with _state_lock:
        stale_sessions = [
            sid for sid, q in ice_queue.items()
            if not q.get("_ts") or q["_ts"] < cutoff
        ]
        for sid in stale_sessions:
            ice_queue.pop(sid, None)
            pending_answers.pop(sid, None)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def _wrap(*a, **kw):
        if not session.get("logged_in"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "auth required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return _wrap


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session["logged_in"] = True
            session["user"] = u
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Invalid credentials"
    return f"""
    <!doctype html><html><head><meta charset="utf-8"><title>Login • CCTV</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
      *{{box-sizing:border-box}}
      body{{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0b1220;color:#e6edf7;
           display:flex;align-items:center;justify-content:center;min-height:100vh}}
      .card{{background:#131b2e;border:1px solid #22304d;padding:32px;border-radius:16px;
             width:100%;max-width:360px;box-shadow:0 20px 60px rgba(0,0,0,.45)}}
      h1{{margin:0 0 20px;font-size:20px;letter-spacing:.3px}}
      input{{width:100%;padding:12px 14px;margin:6px 0 14px;border-radius:10px;border:1px solid #2a3a5c;
             background:#0e1626;color:#e6edf7;font-size:15px;outline:none}}
      input:focus{{border-color:#4c8dff}}
      button{{width:100%;padding:12px;border:0;border-radius:10px;background:#4c8dff;color:#fff;
              font-weight:600;font-size:15px;cursor:pointer}}
      .err{{color:#ff7676;font-size:13px;margin:-6px 0 10px}}
      .logo{{font-size:22px;font-weight:700;margin-bottom:6px}}
      .sub{{opacity:.6;font-size:13px;margin-bottom:20px}}
    </style></head><body>
    <form class="card" method="post">
      <div class="logo">🎥 CCTV Console</div>
      <div class="sub">Sign in to manage cameras & footage</div>
      { f'<div class="err">{error}</div>' if error else '' }
      <label>Username</label>
      <input name="username" autofocus required>
      <label>Password</label>
      <input name="password" type="password" required>
      <button type="submit">Sign in</button>
    </form></body></html>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("dashboard") if session.get("logged_in") else url_for("login"))


@app.route("/camera")
def camera_page():
    # Camera page can be opened directly on a phone via a share link.
    # No admin session required (the phone is the sensor).
    return render_template("camera.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/footage")
@login_required
def footage_page():
    return render_template("footage.html")


# ---------------------------------------------------------------------------
# Camera registration / heartbeat
# ---------------------------------------------------------------------------
@app.route("/api/camera/register", methods=["POST"])
def camera_register():
    data = request.get_json(silent=True) or {}
    _load_registry()
    cam_id = (data.get("camera_id") or "").strip() or f"cam-{uuid.uuid4().hex[:8]}"
    name   = (data.get("name") or f"Camera-{cam_id[-4:]}").strip()
    with _state_lock:
        cameras[cam_id] = {
            "name": name,
            "last_ping": _now(),
            "registered": _now(),
        }
        pending_offers.setdefault(cam_id, [])
    _save_registry()
    return jsonify({"camera_id": cam_id, "name": name})


@app.route("/api/camera/ping", methods=["POST"])
def camera_ping():
    data = request.get_json(silent=True) or {}
    cam_id = data.get("camera_id")
    if not cam_id:
        return jsonify({"error": "camera_id required"}), 400
    _load_registry()
    with _state_lock:
        if cam_id not in cameras:
            return jsonify({"error": "unknown camera"}), 404
        cameras[cam_id]["last_ping"] = _now()
        offers = pending_offers.get(cam_id, [])
        pending_offers[cam_id] = []
    _save_registry()
    return jsonify({"ok": True, "pending_offers": offers})


@app.route("/api/cameras")
@login_required
def list_cameras():
    _load_registry(force=True)
    now = _now()
    out = []
    with _state_lock:
        for cid, meta in cameras.items():
            online = (now - meta["last_ping"]) < PING_TIMEOUT
            out.append({
                "id": cid,
                "name": meta["name"],
                "online": online,
                "last_ping": meta["last_ping"],
                "registered": meta["registered"],
            })
    out.sort(key=lambda c: (not c["online"], c["name"]))
    return jsonify({"cameras": out})


# ---------------------------------------------------------------------------
# WebRTC signaling
#
# Flow:
#   1. Admin creates offer -> POST /api/webrtc/offer  (session_id generated)
#   2. Camera long-polls its offers via /api/camera/ping response
#   3. Camera answers -> POST /api/webrtc/answer
#   4. Admin polls /api/webrtc/answer?session=...
#   5. Both sides trickle ICE via /api/webrtc/ice (POST) and GET
# ---------------------------------------------------------------------------
@app.route("/api/webrtc/offer", methods=["POST"])
@login_required
def webrtc_offer():
    data = request.get_json(silent=True) or {}
    cam_id = data.get("camera_id")
    sdp    = data.get("sdp")
    if not cam_id or not sdp:
        return jsonify({"error": "camera_id and sdp required"}), 400
    session_id = uuid.uuid4().hex
    with _state_lock:
        if cam_id not in cameras:
            return jsonify({"error": "unknown camera"}), 404
        pending_offers.setdefault(cam_id, []).append({
            "session": session_id,
            "type": "offer",
            "sdp": sdp,
            "ts": _now(),
        })
        ice_queue[session_id] = {"to_camera": [], "to_admin": [], "_ts": _now()}
    return jsonify({"session": session_id})


@app.route("/api/webrtc/answer", methods=["POST"])
def webrtc_answer():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session")
    sdp        = data.get("sdp")
    if not session_id or not sdp:
        return jsonify({"error": "session and sdp required"}), 400
    with _state_lock:
        pending_answers[session_id] = {"type": "answer", "sdp": sdp, "ts": _now()}
    return jsonify({"ok": True})


@app.route("/api/webrtc/answer", methods=["GET"])
@login_required
def webrtc_get_answer():
    session_id = request.args.get("session", "")
    with _state_lock:
        ans = pending_answers.pop(session_id, None)
    return jsonify({"answer": ans})


@app.route("/api/webrtc/ice", methods=["POST"])
def webrtc_post_ice():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session")
    candidate  = data.get("candidate")
    to_side    = data.get("to")   # "camera" | "admin"
    if not session_id or candidate is None or to_side not in ("camera", "admin"):
        return jsonify({"error": "bad payload"}), 400
    with _state_lock:
        q = ice_queue.setdefault(session_id, {"to_camera": [], "to_admin": [], "_ts": _now()})
        q[f"to_{to_side}"].append(candidate)
        q["_ts"] = _now()
    return jsonify({"ok": True})


@app.route("/api/webrtc/ice", methods=["GET"])
def webrtc_get_ice():
    session_id = request.args.get("session", "")
    to_side    = request.args.get("to", "")
    if to_side not in ("camera", "admin"):
        return jsonify({"error": "to required"}), 400
    with _state_lock:
        q = ice_queue.get(session_id)
        if not q:
            return jsonify({"candidates": []})
        key = f"to_{to_side}"
        cands = q[key]
        q[key] = []
    return jsonify({"candidates": cands})


# ---------------------------------------------------------------------------
# Footage upload / listing / streaming (private bucket)
# ---------------------------------------------------------------------------
def _ensure_supabase():
    if supabase is None:
        abort(503, "Supabase not configured on server (set SUPABASE_URL / SUPABASE_SERVICE_KEY).")


@app.route("/api/footage/upload", methods=["POST"])
def footage_upload():
    _ensure_supabase()
    cam_id = request.form.get("camera_id") or request.args.get("camera_id")
    if not cam_id:
        return jsonify({"error": "camera_id required"}), 400
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "file required"}), 400

    ts   = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    ext  = os.path.splitext(file.filename or "clip.webm")[1] or ".webm"
    key  = f"{cam_id}/{ts}-{uuid.uuid4().hex[:6]}{ext}"
    data = file.read()

    try:
        supabase.storage.from_(SUPABASE_BUCKET).upload(
            path=key,
            file=data,
            file_options={
                "content-type": file.mimetype or "video/webm",
                "upsert": "false",
            },
        )
    except Exception as e:
        return jsonify({"error": f"upload failed: {e}"}), 500

    # touch heartbeat while we're at it
    _load_registry()
    touched = False
    with _state_lock:
        if cam_id in cameras:
            cameras[cam_id]["last_ping"] = _now()
            touched = True
    if touched:
        _save_registry()

    return jsonify({"ok": True, "path": key, "bytes": len(data)})


@app.route("/api/footage/list")
@login_required
def footage_list():
    _ensure_supabase()
    cam = request.args.get("camera_id")
    prefix = cam or ""
    try:
        # list top-level (camera folders) or a specific camera folder
        if not cam:
            # aggregate: list every camera folder that actually exists in the
            # bucket (folders come back with id == None), rather than relying
            # on the camera registry — this way clips always show up even if
            # a camera was never (re-)registered or the registry lagged.
            root = supabase.storage.from_(SUPABASE_BUCKET).list(
                path="", options={"limit": 1000}
            )
            cam_ids = [
                it["name"] for it in (root or [])
                if it.get("id") is None and not it.get("name", "").startswith(("_", "."))
            ]
            items = []
            for c in cam_ids:
                try:
                    files = supabase.storage.from_(SUPABASE_BUCKET).list(
                        path=c,
                        options={"limit": 200, "sortBy": {"column": "created_at", "order": "desc"}},
                    )
                    for f in files or []:
                        if f.get("name", "").startswith("."):
                            continue
                        items.append({
                            "camera_id": c,
                            "name": f["name"],
                            "path": f"{c}/{f['name']}",
                            "size": f.get("metadata", {}).get("size", 0),
                            "created_at": f.get("created_at"),
                            "updated_at": f.get("updated_at"),
                        })
                except Exception:
                    continue
            items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
            return jsonify({"items": items[:500]})
        else:
            files = supabase.storage.from_(SUPABASE_BUCKET).list(
                path=prefix,
                options={"limit": 500, "sortBy": {"column": "created_at", "order": "desc"}},
            )
            items = [{
                "camera_id": cam,
                "name": f["name"],
                "path": f"{cam}/{f['name']}",
                "size": f.get("metadata", {}).get("size", 0),
                "created_at": f.get("created_at"),
                "updated_at": f.get("updated_at"),
            } for f in files or [] if not f.get("name", "").startswith(".")]
            return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/footage/signed")
@login_required
def footage_signed():
    """Return a short-lived signed URL for a private object."""
    _ensure_supabase()
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "path required"}), 400
    try:
        res = supabase.storage.from_(SUPABASE_BUCKET).create_signed_url(path, SIGNED_URL_TTL)
        url = res.get("signedURL") or res.get("signed_url") or res.get("url")
        return jsonify({"url": url, "ttl": SIGNED_URL_TTL})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/footage/stream")
@login_required
def footage_stream():
    """
    Secure backend proxy stream — pulls the private object from Supabase and
    pipes it to the authenticated admin. Keeps the bucket private end-to-end.
    """
    _ensure_supabase()
    path = request.args.get("path", "")
    if not path:
        abort(400)
    try:
        blob = supabase.storage.from_(SUPABASE_BUCKET).download(path)
    except Exception as e:
        abort(404, f"not found: {e}")

    def _gen(data: bytes, chunk: int = 64 * 1024):
        buf = BytesIO(data)
        while True:
            b = buf.read(chunk)
            if not b:
                break
            yield b

    ext = os.path.splitext(path)[1].lower()
    mime = {
        ".webm": "video/webm",
        ".mp4":  "video/mp4",
        ".mkv":  "video/x-matroska",
    }.get(ext, "application/octet-stream")

    return Response(stream_with_context(_gen(blob)), mimetype=mime,
                    headers={"Cache-Control": "private, max-age=60"})


@app.route("/api/footage/delete", methods=["POST"])
@login_required
def footage_delete():
    _ensure_supabase()
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "path required"}), 400
    try:
        supabase.storage.from_(SUPABASE_BUCKET).remove([path])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    _cleanup_stale()
    with _state_lock:
        return jsonify({
            "ok": True,
            "cameras": len(cameras),
            "supabase": supabase is not None,
        })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
