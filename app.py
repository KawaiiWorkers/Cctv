import os
import json
import uuid
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response, send_file
from supabase import create_client, Client
import requests

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-in-production')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False  # Set True in production with HTTPS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# ─── Supabase Configuration ───
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')  # Service role for private bucket access
BUCKET_NAME = 'footages'

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print(f'[Supabase] Connected to {SUPABASE_URL}')
    except Exception as e:
        print(f'[Supabase] Connection failed: {e}')
else:
    print('[Supabase] WARNING: Credentials not set. Footage features disabled.')

# ─── Thread-Safe In-Memory State ───
_state_lock = threading.RLock()
_cameras = {}          # camera_id -> {id, name, last_ping, ip, webrtc_state, ...}
_webrtc_offers = {}   # camera_id -> offer_sdp (from admin)
_webrtc_answers = {}  # camera_id -> answer_sdp (from camera)
_ice_candidates = {}  # camera_id -> { 'admin': [], 'camera': [] }

# ─── Auth Helpers ───
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated

# ─── State Management ───
def register_camera(camera_id: str, name: str, ip: str):
    with _state_lock:
        _cameras[camera_id] = {
            'id': camera_id,
            'name': name or f'Camera-{camera_id[:8]}',
            'ip': ip,
            'last_ping': time.time(),
            'status': 'ONLINE',
            'webrtc_connected': False,
            'created_at': datetime.utcnow().isoformat()
        }
        _webrtc_offers[camera_id] = None
        _webrtc_answers[camera_id] = None
        _ice_candidates[camera_id] = {'admin': [], 'camera': []}
        print(f'[Camera] Registered: {camera_id} ({name}) from {ip}')

def update_camera_ping(camera_id: str):
    with _state_lock:
        if camera_id in _cameras:
            _cameras[camera_id]['last_ping'] = time.time()
            _cameras[camera_id]['status'] = 'ONLINE'

def get_camera(camera_id: str):
    with _state_lock:
        return _cameras.get(camera_id)\n
def get_all_cameras():
    with _state_lock:
        # Mark stale cameras as OFFLINE
        now = time.time()
        for cam in _cameras.values():
            if now - cam['last_ping'] > 30:  # 30s timeout
                cam['status'] = 'OFFLINE'
                cam['webrtc_connected'] = False
        return list(_cameras.values())

def set_webrtc_offer(camera_id: str, offer: dict):
    with _state_lock:
        _webrtc_offers[camera_id] = offer

def get_webrtc_offer(camera_id: str):
    with _state_lock:
        offer = _webrtc_offers.get(camera_id)
        _webrtc_offers[camera_id] = None  # Consume once
        return offer

def set_webrtc_answer(camera_id: str, answer: dict):
    with _state_lock:
        _webrtc_answers[camera_id] = answer

def get_webrtc_answer(camera_id: str):
    with _state_lock:
        answer = _webrtc_answers.get(camera_id)
        _webrtc_answers[camera_id] = None
        return answer

def add_ice_candidate(camera_id: str, source: str, candidate: dict):
    with _state_lock:
        if camera_id not in _ice_candidates:
            _ice_candidates[camera_id] = {'admin': [], 'camera': []}
        _ice_candidates[camera_id][source].append(candidate)

def get_ice_candidates(camera_id: str, source: str):
    with _state_lock:
        candidates = _ice_candidates.get(camera_id, {}).get(source, [])
        _ice_candidates[camera_id][source] = []
        return candidates

def mark_webrtc_connected(camera_id: str, connected: bool):
    with _state_lock:
        if camera_id in _cameras:
            _cameras[camera_id]['webrtc_connected'] = connected

# ─── Supabase Storage Helpers ───
def upload_footage(camera_id: str, chunk_blob: bytes, chunk_index: int, timestamp: str) -> str:
    """Upload video chunk to private Supabase bucket. Returns signed URL."""
    if not supabase:
        raise RuntimeError('Supabase not configured')
    
    # Path: footages/{camera_id}/{YYYY-MM-DD}/{timestamp}_{chunk_index}.webm
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    filename = f'{timestamp}_{chunk_index:04d}.webm'
    storage_path = f'{camera_id}/{date_str}/{filename}'
    
    try:
        res = supabase.storage.from_(BUCKET_NAME).upload(
            path=storage_path,
            file=chunk_blob,
            file_options={'content-type': 'video/webm', 'upsert': 'false'}
        )
        if hasattr(res, 'error') and res.error:
            raise Exception(res.error.message)
        
        # Generate signed URL (valid 1 hour)
        signed = supabase.storage.from_(BUCKET_NAME).create_signed_url(storage_path, 3600)
        if signed.get('error'):
            raise Exception(signed['error'].message)
        return signed['signedURL']
    except Exception as e:
        print(f'[Supabase Upload] Failed for {storage_path}: {e}')
        raise

def list_footage(camera_id: str = None, date: str = None, limit: int = 100):
    """List footage files in private bucket, optionally filtered."""
    if not supabase:
        return []
    try:
        prefix = ''
        if camera_id:
            prefix = f'{camera_id}/'
            if date:
                prefix += f'{date}/'
        res = supabase.storage.from_(BUCKET_NAME).list(prefix, {'limit': limit, 'sortBy': {'column': 'name', 'order': 'desc'}})
        if hasattr(res, 'error') and res.error:
            return []
        files = []
        for item in res:
            if item['name'].endswith('.webm'):
                full_path = f'{prefix}{item["name"]}'
                signed = supabase.storage.from_(BUCKET_NAME).create_signed_url(full_path, 3600)
                if not signed.get('error'):
                    files.append({
                        'path': full_path,
                        'name': item['name'],
                        'size': item.get('metadata', {}).get('size', 0),
                        'signed_url': signed['signedURL'],
                        'camera_id': camera_id or full_path.split('/')[0],
                        'date': date or full_path.split('/')[1] if '/' in full_path else ''
                    })
        return files
    except Exception as e:
        print(f'[Supabase List] Error: {e}')
        return []

def get_signed_url(storage_path: str) -> str:
    if not supabase:
        return None
    signed = supabase.storage.from_(BUCKET_NAME).create_signed_url(storage_path, 3600)
    if signed.get('error'):
        return None
    return signed['signedURL']

# ─── Routes ───
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['authenticated'] = True
            session.permanent = True
            next_url = request.args.get('next') or url_for('dashboard')
            return redirect(next_url)
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── Camera Client Routes ───
@app.route('/camera')
def camera_page():
    # Camera client accesses via /camera?camera_id=...&name=...
    camera_id = request.args.get('camera_id') or str(uuid.uuid4())
    name = request.args.get('name', f'Camera-{camera_id[:8]}')
    return render_template('camera.html', camera_id=camera_id, camera_name=name)

@app.route('/api/camera/register', methods=['POST'])
def api_camera_register():
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    name = data.get('name', f'Camera-{camera_id[:8]}')
    ip = request.remote_addr
    if not camera_id:
        return jsonify({'error': 'camera_id required'}), 400
    register_camera(camera_id, name, ip)
    return jsonify({'status': 'registered', 'camera_id': camera_id})

@app.route('/api/camera/ping', methods=['POST'])
def api_camera_ping():
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    if not camera_id:
        return jsonify({'error': 'camera_id required'}), 400
    update_camera_ping(camera_id)
    # Check for pending WebRTC offer from admin
    offer = get_webrtc_offer(camera_id)
    return jsonify({'status': 'ok', 'offer': offer})

@app.route('/api/camera/webrtc/answer', methods=['POST'])
def api_camera_webrtc_answer():
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    answer = data.get('answer')
    if not camera_id or not answer:
        return jsonify({'error': 'camera_id and answer required'}), 400
    set_webrtc_answer(camera_id, answer)
    mark_webrtc_connected(camera_id, True)
    return jsonify({'status': 'ok'})

@app.route('/api/camera/webrtc/ice', methods=['POST'])
def api_camera_webrtc_ice():
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    candidate = data.get('candidate')
    if not camera_id or not candidate:
        return jsonify({'error': 'camera_id and candidate required'}), 400
    add_ice_candidate(camera_id, 'camera', candidate)
    return jsonify({'status': 'ok'})

@app.route('/api/camera/upload_chunk', methods=['POST'])
def api_camera_upload_chunk():
    """Receive 15-second video chunk from camera client, upload to Supabase."""
    camera_id = request.form.get('camera_id')
    chunk_index = int(request.form.get('chunk_index', '0'))
    timestamp = request.form.get('timestamp', datetime.utcnow().strftime('%H-%M-%S'))
    
    if not camera_id:
        return jsonify({'error': 'camera_id required'}), 400
    
    if 'video' not in request.files:
        return jsonify({'error': 'video file required'}), 400
    
    video_file = request.files['video']
    chunk_blob = video_file.read()
    
    if not chunk_blob:
        return jsonify({'error': 'empty video data'}), 400
    
    try:
        signed_url = upload_footage(camera_id, chunk_blob, chunk_index, timestamp)
        return jsonify({'status': 'uploaded', 'signed_url': signed_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Admin Dashboard API ───
@app.route('/dashboard')
@login_required
def dashboard():
    cameras = get_all_cameras()
    return render_template('dashboard.html', cameras=cameras)

@app.route('/footage')
@login_required
def footage_page():
    camera_id = request.args.get('camera_id')
    date = request.args.get('date', datetime.utcnow().strftime('%Y-%m-%d'))
    files = list_footage(camera_id, date) if camera_id else []
    cameras = get_all_cameras()
    return render_template('footage.html', files=files, cameras=cameras, selected_camera=camera_id, selected_date=date)

@app.route('/api/admin/cameras')
@login_required
def api_admin_cameras():
    return jsonify(get_all_cameras())

@app.route('/api/admin/webrtc/offer', methods=['POST'])
@login_required
def api_admin_webrtc_offer():
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    offer = data.get('offer')
    if not camera_id or not offer:
        return jsonify({'error': 'camera_id and offer required'}), 400
    set_webrtc_offer(camera_id, offer)
    return jsonify({'status': 'offer_sent'})

@app.route('/api/admin/webrtc/answer/<camera_id>')
@login_required
def api_admin_webrtc_answer(camera_id):
    answer = get_webrtc_answer(camera_id)
    return jsonify({'answer': answer})

@app.route('/api/admin/webrtc/ice/<camera_id>')
@login_required
def api_admin_webrtc_ice(camera_id):
    candidates = get_ice_candidates(camera_id, 'camera')
    return jsonify({'candidates': candidates})

@app.route('/api/admin/webrtc/ice', methods=['POST'])
@login_required
def api_admin_webrtc_ice_post():
    data = request.get_json() or {}
    camera_id = data.get('camera_id')
    candidate = data.get('candidate')
    if not camera_id or not candidate:
        return jsonify({'error': 'camera_id and candidate required'}), 400
    add_ice_candidate(camera_id, 'admin', candidate)
    return jsonify({'status': 'ok'})

@app.route('/api/admin/footage/list')
@login_required
def api_admin_footage_list():
    camera_id = request.args.get('camera_id')
    date = request.args.get('date')
    files = list_footage(camera_id, date)
    return jsonify(files)

@app.route('/api/admin/footage/url')
@login_required
def api_admin_footage_url():
    path = request.args.get('path')
    if not path:
        return jsonify({'error': 'path required'}), 400
    url = get_signed_url(path)
    if not url:
        return jsonify({'error': 'failed to generate signed url'}), 500
    return jsonify({'signed_url': url})

# ─── Health Check ───
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'cameras_online': len([c for c in get_all_cameras() if c['status'] == 'ONLINE'])})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
