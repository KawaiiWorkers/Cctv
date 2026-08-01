import os
import json
import uuid
import threading
import time
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response, send_file
from supabase import create_client, Client
import bcrypt
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-change-in-production')

# ─── Supabase ───
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')
SUPABASE_BUCKET = os.getenv('SUPABASE_BUCKET', 'footages')

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError('Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in environment')

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ─── Auth ───
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD_HASH = os.getenv('ADMIN_PASSWORD_HASH')
if not ADMIN_PASSWORD_HASH:
    raise RuntimeError('ADMIN_PASSWORD_HASH not set — generate with bcrypt')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated

# ─── In-memory camera state (thread-safe) ───
_cameras = {}
_cameras_lock = threading.RLock()
_webrtc_signaling = {}  # camera_id -> {offer, answer, ice_candidates[]}
_signaling_lock = threading.RLock()

CAMERA_TIMEOUT = 30  # seconds

def prune_stale_cameras():
    while True:
        time.sleep(10)
        now = time.time()
        with _cameras_lock:
            stale = [cid for cid, c in _cameras.items() if now - c['last_ping'] > CAMERA_TIMEOUT]
            for cid in stale:
                _cameras.pop(cid, None)
                with _signaling_lock:
                    _webrtc_signaling.pop(cid, None)

threading.Thread(target=prune_stale_cameras, daemon=True).start()

# ─── Helpers ───
def get_camera(camera_id: str):
    with _cameras_lock:
        return _cameras.get(camera_id)

def set_camera(camera_id: str, data: dict):
    with _cameras_lock:
        _cameras[camera_id] = data

def update_camera_ping(camera_id: str):
    with _cameras_lock:
        if camera_id in _cameras:
            _cameras[camera_id]['last_ping'] = time.time()
            _cameras[camera_id]['status'] = 'ONLINE'

def get_signaling(camera_id: str):
    with _signaling_lock:
        return _webrtc_signaling.get(camera_id, {}).copy()

def set_signaling_offer(camera_id: str, offer: dict):
    with _signaling_lock:
        if camera_id not in _webrtc_signaling:
            _webrtc_signaling[camera_id] = {'ice_candidates': []}
        _webrtc_signaling[camera_id]['offer'] = offer
        _webrtc_signaling[camera_id]['answer'] = None
        _webrtc_signaling[camera_id]['ice_candidates'] = []

def set_signaling_answer(camera_id: str, answer: dict):
    with _signaling_lock:
        if camera_id in _webrtc_signaling:
            _webrtc_signaling[camera_id]['answer'] = answer

def add_ice_candidate(camera_id: str, candidate: dict, target: str):
    """target: 'camera' or 'admin'"""
    with _signaling_lock:
        if camera_id not in _webrtc_signaling:
            _webrtc_signaling[camera_id] = {'ice_candidates': []}
        _webrtc_signaling[camera_id].setdefault(f'{target}_ice_candidates', []).append(candidate)

def pop_ice_candidates(camera_id: str, target: str):
    with _signaling_lock:
        cands = _webrtc_signaling.get(camera_id, {}).pop(f'{target}_ice_candidates', [])
        return cands

# ─── Routes ───
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username == ADMIN_USERNAME and verify_password(password, ADMIN_PASSWORD_HASH):
            session['admin_logged_in'] = True
            return redirect(request.args.get('next') or url_for('dashboard'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── Camera registration & heartbeat ───
@app.route('/api/camera/register', methods=['POST'])
def camera_register():
    data = request.get_json() or {}
    camera_id = data.get('camera_id') or str(uuid.uuid4())[:8]
    name = data.get('name', f'Camera {camera_id}')
    
    set_camera(camera_id, {
        'id': camera_id,
        'name': name,
        'last_ping': time.time(),
        'status': 'ONLINE',
        'registered_at': datetime.now(timezone.utc).isoformat(),
    })
    return jsonify({'camera_id': camera_id, 'status': 'registered'})

@app.route('/api/camera/ping', methods=['POST'])
def camera_ping():
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    if not camera_id or camera_id not in _cameras:
        return jsonify({'error': 'Unknown camera'}), 404
    update_camera_ping(camera_id)
    return jsonify({'status': 'ok'})

# ─── WebRTC Signaling (polling-based, no WebSockets) ───
@app.route('/api/webrtc/offer', methods=['POST'])
def webrtc_offer():
    """Admin posts offer for a camera"""
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    offer = data.get('offer')
    if not camera_id or not offer:
        return jsonify({'error': 'Missing camera_id or offer'}), 400
    if camera_id not in _cameras:
        return jsonify({'error': 'Camera offline'}), 404
    set_signaling_offer(camera_id, offer)
    return jsonify({'status': 'offer stored'})

@app.route('/api/webrtc/answer', methods=['POST'])
def webrtc_answer():
    """Camera posts answer"""
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    answer = data.get('answer')
    if not camera_id or not answer:
        return jsonify({'error': 'Missing camera_id or answer'}), 400
    set_signaling_answer(camera_id, answer)
    return jsonify({'status': 'answer stored'})

@app.route('/api/webrtc/ice', methods=['POST'])
def webrtc_ice():
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    candidate = data.get('candidate')
    target = data.get('target', 'camera')  # 'camera' or 'admin'
    if not camera_id or not candidate:
        return jsonify({'error': 'Missing camera_id or candidate'}), 400
    add_ice_candidate(camera_id, candidate, target)
    return jsonify({'status': 'ice stored'})

@app.route('/api/webrtc/poll/<camera_id>')
def webrtc_poll(camera_id):
    """Camera polls for offer + admin ICE candidates"""
    if camera_id not in _cameras:
        return jsonify({'error': 'Camera offline'}), 404
    sig = get_signaling(camera_id)
    offer = sig.get('offer')
    admin_ice = pop_ice_candidates(camera_id, 'admin')
    return jsonify({'offer': offer, 'ice_candidates': admin_ice})

@app.route('/api/webrtc/poll_admin/<camera_id>')
def webrtc_poll_admin(camera_id):
    """Admin polls for answer + camera ICE candidates"""
    if camera_id not in _cameras:
        return jsonify({'error': 'Camera offline'}), 404
    sig = get_signaling(camera_id)
    answer = sig.get('answer')
    camera_ice = pop_ice_candidates(camera_id, 'camera')
    return jsonify({'answer': answer, 'ice_candidates': camera_ice})

# ─── Camera list for dashboard ───
@app.route('/api/cameras')
@login_required
def list_cameras():
    with _cameras_lock:
        cams = []
        for c in _cameras.values():
            cams.append({
                'id': c['id'],
                'name': c['name'],
                'status': c['status'],
                'last_ping': c['last_ping'],
            })
        return jsonify(cams)

# ─── Footage upload (from camera client) ───
@app.route('/api/footage/upload', methods=['POST'])
def upload_footage():
    """Receive 15s video chunk, upload to private Supabase bucket"""
    camera_id = request.form.get('camera_id')
    chunk_index = request.form.get('chunk_index', '0')
    timestamp = request.form.get('timestamp', datetime.now(timezone.utc).isoformat())
    video_file = request.files.get('video')
    
    if not camera_id or not video_file:
        return jsonify({'error': 'Missing camera_id or video file'}), 400
    
    # Generate unique path: footages/{camera_id}/{YYYY-MM-DD}/{timestamp}_{chunk}.webm
    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    date_path = dt.strftime('%Y-%m-%d')
    filename = f"{dt.strftime('%H-%M-%S')}_{chunk_index}.webm"
    storage_path = f"{camera_id}/{date_path}/{filename}"
    
    try:
        video_file.seek(0)
        supabase.storage.from_(SUPABASE_BUCKET).upload(
            storage_path,
            video_file.read(),
            file_options={'content-type': 'video/webm', 'upsert': 'false'}
        )
        return jsonify({'status': 'uploaded', 'path': storage_path})
    except Exception as e:
        app.logger.error(f'Upload failed: {e}')
        return jsonify({'error': 'Upload failed'}), 500

# ─── Footage listing (signed URLs) ───
@app.route('/api/footage/list')
@login_required
def list_footage():
    camera_id = request.args.get('camera_id')
    date = request.args.get('date')  # YYYY-MM-DD
    
    try:
        prefix = ''
        if camera_id:
            prefix = f"{camera_id}/"
            if date:
                prefix += f"{date}/"
        
        res = supabase.storage.from_(SUPABASE_BUCKET).list(prefix)
        files = []
        for item in res:
            if item['name'].endswith('.webm'):
                full_path = f"{prefix}{item['name']}" if prefix else item['name']
                # Generate signed URL (1 hour expiry)
                signed = supabase.storage.from_(SUPABASE_BUCKET).create_signed_url(full_path, 3600)
                files.append({
                    'path': full_path,
                    'name': item['name'],
                    'size': item.get('metadata', {}).get('size', 0),
                    'signed_url': signed.get('signedURL'),
                    'camera_id': camera_id,
                    'date': date,
                })
        # Sort newest first
        files.sort(key=lambda x: x['name'], reverse=True)
        return jsonify(files)
    except Exception as e:
        app.logger.error(f'List footage error: {e}')
        return jsonify({'error': 'Failed to list footage'}), 500

# ─── Secure footage streaming (proxy through backend) ───
@app.route('/api/footage/stream/<path:storage_path>')
@login_required
def stream_footage(storage_path):
    """Stream private footage via signed URL — avoids exposing bucket"""
    try:
        signed = supabase.storage.from_(SUPABASE_BUCKET).create_signed_url(storage_path, 300)
        signed_url = signed.get('signedURL')
        if not signed_url:
            return 'Not found', 404
        # Redirect to signed URL (browser handles video streaming)
        return redirect(signed_url)
    except Exception as e:
        app.logger.error(f'Stream error: {e}')
        return 'Not found', 404

# ─── Pages ───
@app.route('/camera.html')
def camera_page():
    camera_id = request.args.get('camera_id')
    return render_template('camera.html', camera_id=camera_id)

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/footage')
@login_required
def footage_page():
    return render_template('footage.html')

# ─── Health check ───
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'cameras': len(_cameras)})

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
