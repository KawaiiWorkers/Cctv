import os
import cv2
import time
import json
import re
import threading
import base64
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, Response, request, redirect, url_for, session, flash, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash
import numpy as np

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')
ADMIN_PASSWORD_HASH = generate_password_hash(os.environ.get('ADMIN_PASSWORD', 'admin123'))

# Global state
connected_cameras = {}  # {camera_id: {last_frame, last_seen, video_writer, segment_start, current_file, frame_count, fps, last_fps_time}}
footage_folder = 'footage'
os.makedirs(footage_folder, exist_ok=True)

# Recording settings
SEGMENT_DURATION = 600  # 10 minutes in seconds

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# ==================== HTML TEMPLATES ====================

CAMERA_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>CCTV Camera</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            background: #000; 
            display: flex; 
            flex-direction: column;
            align-items: center; 
            justify-content: center; 
            min-height: 100vh;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            color: white;
            overflow: hidden;
        }
        #video { 
            width: 100vw; 
            height: 100vh;
            object-fit: cover;
            transform: scaleX(-1);
        }
        #status {
            position: fixed;
            top: 15px;
            left: 15px;
            background: rgba(0,0,0,0.75);
            padding: 10px 18px;
            border-radius: 25px;
            font-size: 13px;
            z-index: 100;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
        }
        .status-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 6px;
        }
        .recording { background: #ff3b30; animation: pulse 1.2s infinite; }
        .connecting { background: #ff9500; }
        .connected { background: #34c759; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.2; } }
        #info {
            position: fixed;
            bottom: 15px;
            left: 15px;
            font-size: 11px;
            color: rgba(255,255,255,0.5);
            background: rgba(0,0,0,0.6);
            padding: 8px 14px;
            border-radius: 15px;
            backdrop-filter: blur(10px);
        }
        #cameraSelect {
            position: fixed;
            top: 15px;
            right: 15px;
            z-index: 100;
            background: rgba(0,0,0,0.75);
            border: 1px solid rgba(255,255,255,0.2);
            color: white;
            padding: 10px 15px;
            border-radius: 25px;
            font-size: 13px;
            backdrop-filter: blur(10px);
            outline: none;
        }
        #cameraSelect option { background: #1c1c1e; color: white; }
        #fullscreenBtn {
            position: fixed;
            bottom: 15px;
            right: 15px;
            z-index: 100;
            background: rgba(0,0,0,0.75);
            border: 1px solid rgba(255,255,255,0.2);
            color: white;
            padding: 10px 15px;
            border-radius: 25px;
            font-size: 13px;
            backdrop-filter: blur(10px);
            cursor: pointer;
        }
    </style>
</head>
<body>
    <div id="status">
        <span class="status-dot connecting" id="statusDot"></span>
        <span id="statusText">Initializing...</span>
    </div>

    <select id="cameraSelect">
        <option value="environment">📷 Back Camera</option>
        <option value="user">🤳 Front Camera</option>
    </select>

    <video id="video" autoplay playsinline muted></video>
    <canvas id="canvas" style="display:none;"></canvas>

    <div id="info">
        🎥 <span id="camId">-</span> | 
        📐 <span id="res">-</span> | 
        🎞 <span id="fps">0</span> FPS | 
        💾 <span id="buffered">0</span>s buffered
    </div>

    <button id="fullscreenBtn" onclick="toggleFullscreen()">⛶ Fullscreen</button>

    <script>
        const video = document.getElementById('video');
        const canvas = document.getElementById('canvas');
        const ctx = canvas.getContext('2d');
        const statusDot = document.getElementById('statusDot');
        const statusText = document.getElementById('statusText');
        const camIdEl = document.getElementById('camId');
        const fpsEl = document.getElementById('fps');
        const resEl = document.getElementById('res');
        const bufferedEl = document.getElementById('buffered');
        const cameraSelect = document.getElementById('cameraSelect');

        let cameraId = localStorage.getItem('cctv_camera_id');
        if (!cameraId) {
            cameraId = 'cam_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
            localStorage.setItem('cctv_camera_id', cameraId);
        }
        camIdEl.textContent = cameraId;

        let stream = null;
        let frameCount = 0;
        let lastFpsTime = Date.now();
        let isOnline = navigator.onLine;
        let frameBuffer = [];
        let isUploading = false;
        let currentFacing = 'environment';

        // ~10 minutes of frames at 10fps = 6000 frames
        const MAX_OFFLINE_FRAMES = 6000;
        const SERVER_URL = window.location.origin;

        async function startCamera(facing = 'environment') {
            currentFacing = facing;
            if (stream) {
                stream.getTracks().forEach(t => t.stop());
            }

            try {
                stream = await navigator.mediaDevices.getUserMedia({
                    video: { 
                        facingMode: { ideal: facing },
                        width: { ideal: 1280 },
                        height: { ideal: 720 }
                    },
                    audio: false
                });
                video.srcObject = stream;

                video.onloadedmetadata = () => {
                    resEl.textContent = video.videoWidth + 'x' + video.videoHeight;
                    canvas.width = video.videoWidth;
                    canvas.height = video.videoHeight;
                };

                updateStatus('connected', 'Live - Recording');
                startStreaming();
            } catch (err) {
                updateStatus('connecting', 'Camera Error: ' + err.message);
                setTimeout(() => startCamera(facing), 3000);
            }
        }

        function updateStatus(type, text) {
            statusDot.className = 'status-dot ' + type;
            statusText.textContent = text;
        }

        function startStreaming() {
            setInterval(() => {
                if (video.readyState === video.HAVE_ENOUGH_DATA) {
                    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                    const frameData = canvas.toDataURL('image/jpeg', 0.65);

                    const now = Date.now();
                    frameCount++;
                    if (now - lastFpsTime >= 1000) {
                        fpsEl.textContent = frameCount;
                        frameCount = 0;
                        lastFpsTime = now;
                    }

                    if (navigator.onLine) {
                        sendFrame(frameData, now);
                    } else {
                        frameBuffer.push({ data: frameData, timestamp: now });
                        if (frameBuffer.length > MAX_OFFLINE_FRAMES) {
                            frameBuffer.shift();
                        }
                        bufferedEl.textContent = Math.round(frameBuffer.length / 10);
                        updateStatus('connecting', 'Offline - Buffering...');
                    }
                }
            }, 100); // 10 FPS
        }

        async function sendFrame(frameData, timestamp) {
            try {
                const response = await fetch(SERVER_URL + '/camera/feed', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        camera_id: cameraId,
                        frame: frameData,
                        timestamp: timestamp
                    }),
                    keepalive: true
                });

                if (response.ok) {
                    updateStatus('recording', 'Live - Recording');
                    bufferedEl.textContent = '0';

                    if (frameBuffer.length > 0 && !isUploading) {
                        uploadBufferedFrames();
                    }
                }
            } catch (e) {
                // Connection failed - buffer the frame
                frameBuffer.push({ data: frameData, timestamp: timestamp });
                if (frameBuffer.length > MAX_OFFLINE_FRAMES) frameBuffer.shift();
                bufferedEl.textContent = Math.round(frameBuffer.length / 10);
                updateStatus('connecting', 'Connection Lost - Buffering...');
            }
        }

        async function uploadBufferedFrames() {
            if (isUploading || frameBuffer.length === 0) return;
            isUploading = true;
            updateStatus('connecting', 'Uploading backlog (' + frameBuffer.length + ' frames)...');

            const batch = frameBuffer.splice(0, 50);

            try {
                await fetch(SERVER_URL + '/camera/bulk_upload', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        camera_id: cameraId,
                        frames: batch
                    }),
                    keepalive: true
                });

                bufferedEl.textContent = Math.round(frameBuffer.length / 10);
                isUploading = false;

                if (frameBuffer.length > 0) {
                    setTimeout(uploadBufferedFrames, 200);
                } else {
                    updateStatus('recording', 'Live - Recording');
                }
            } catch (e) {
                frameBuffer.unshift(...batch);
                isUploading = false;
                updateStatus('connecting', 'Upload Failed - Retrying...');
            }
        }

        window.addEventListener('online', () => {
            isOnline = true;
            updateStatus('connected', 'Reconnected - Uploading...');
            uploadBufferedFrames();
        });

        window.addEventListener('offline', () => {
            isOnline = false;
            updateStatus('connecting', 'Offline - Buffering...');
        });

        cameraSelect.addEventListener('change', (e) => {
            startCamera(e.target.value);
        });

        function toggleFullscreen() {
            if (!document.fullscreenElement) {
                document.documentElement.requestFullscreen();
            } else {
                document.exitFullscreen();
            }
        }

        // Prevent screen sleep
        if ('wakeLock' in navigator) {
            navigator.wakeLock.request('screen').catch(() => {});
        }

        // Start
        startCamera();
    </script>
</body>
</html>
"""

ADMIN_LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Admin Login - CCTV Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: linear-gradient(135deg, #0f0f1a 0%, #1a1a2e 50%, #16213e 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
        }
        .login-box {
            background: rgba(255,255,255,0.03);
            backdrop-filter: blur(20px);
            border: 1px solid rgba(255,255,255,0.08);
            padding: 50px 40px;
            border-radius: 24px;
            width: 100%;
            max-width: 420px;
            box-shadow: 0 25px 80px rgba(0,0,0,0.4);
        }
        .icon { text-align: center; font-size: 56px; margin-bottom: 15px; }
        h1 { color: #fff; text-align: center; margin-bottom: 8px; font-size: 26px; font-weight: 700; }
        .subtitle { color: #666; text-align: center; margin-bottom: 35px; font-size: 14px; }
        .input-group { margin-bottom: 22px; }
        label { display: block; color: #888; margin-bottom: 10px; font-size: 13px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }
        input {
            width: 100%;
            padding: 16px;
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            background: rgba(0,0,0,0.25);
            color: #fff;
            font-size: 16px;
            outline: none;
            transition: all 0.3s;
        }
        input:focus { border-color: #4a9eff; box-shadow: 0 0 0 3px rgba(74,158,255,0.1); }
        input::placeholder { color: #444; }
        button {
            width: 100%;
            padding: 16px;
            border: none;
            border-radius: 12px;
            background: linear-gradient(135deg, #4a9eff, #357abd);
            color: white;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            margin-top: 10px;
        }
        button:hover { transform: translateY(-2px); box-shadow: 0 12px 35px rgba(74,158,255,0.3); }
        .error { 
            color: #ff6b6b; 
            text-align: center; 
            margin-top: 18px; 
            font-size: 14px; 
            padding: 10px;
            background: rgba(255,107,107,0.1);
            border-radius: 8px;
        }
    </style>
</head>
<body>
    <div class="login-box">
        <div class="icon">🎥</div>
        <h1>CCTV Monitor</h1>
        <p class="subtitle">Admin Dashboard Access</p>
        <form method="POST" action="/admin">
            <div class="input-group">
                <label>Password</label>
                <input type="password" name="password" placeholder="Enter admin password" required autofocus autocomplete="off">
            </div>
            <button type="submit">🔐 Login to Dashboard</button>
            {% if error %}
            <p class="error">⚠️ {{ error }}</p>
            {% endif %}
        </form>
    </div>
</body>
</html>
"""

ADMIN_DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>CCTV Admin Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #050505;
            color: #fff;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            min-height: 100vh;
        }
        .header {
            background: linear-gradient(135deg, #0f0f1a, #1a1a2e);
            padding: 20px 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .header h1 { font-size: 22px; display: flex; align-items: center; gap: 12px; font-weight: 700; }
        .live-badge {
            background: linear-gradient(135deg, #ff3b30, #ff6b6b);
            color: white;
            padding: 5px 14px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 700;
            animation: pulse 2s infinite;
            letter-spacing: 0.5px;
        }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
        .nav-tabs {
            display: flex;
            gap: 0;
            padding: 0 30px;
            background: #0a0a0a;
            border-bottom: 1px solid #1a1a1a;
            position: sticky;
            top: 68px;
            z-index: 99;
        }
        .nav-tab {
            padding: 16px 28px;
            cursor: pointer;
            border-bottom: 3px solid transparent;
            transition: all 0.3s;
            color: #555;
            font-size: 14px;
            font-weight: 500;
        }
        .nav-tab:hover { color: #888; }
        .nav-tab.active { color: #4a9eff; border-bottom-color: #4a9eff; }
        .content { padding: 25px 30px; max-width: 1600px; margin: 0 auto; }
        .section { display: none; animation: fadeIn 0.3s ease; }
        .section.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

        /* Live Cameras Grid */
        .cameras-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
            gap: 20px;
        }
        .camera-card {
            background: #0f0f0f;
            border-radius: 16px;
            overflow: hidden;
            border: 1px solid #1a1a1a;
            transition: all 0.3s;
        }
        .camera-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            border-color: #222;
        }
        .camera-header {
            padding: 14px 18px;
            background: #141414;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .camera-name { font-weight: 600; font-size: 14px; color: #ddd; }
        .camera-status {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            color: #666;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }
        .status-online { background: #34c759; box-shadow: 0 0 8px rgba(52,199,89,0.4); }
        .status-offline { background: #ff3b30; }
        .camera-feed {
            width: 100%;
            aspect-ratio: 16/9;
            background: #000;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
            overflow: hidden;
        }
        .camera-feed img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .no-signal {
            color: #333;
            font-size: 14px;
            text-align: center;
            padding: 20px;
        }
        .no-signal-icon { font-size: 32px; margin-bottom: 8px; }
        .camera-info {
            padding: 12px 18px;
            background: #141414;
            font-size: 12px;
            color: #555;
            display: flex;
            justify-content: space-between;
        }

        /* Footage Section */
        .footage-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 18px;
        }
        .footage-card {
            background: #0f0f0f;
            border-radius: 14px;
            overflow: hidden;
            border: 1px solid #1a1a1a;
            cursor: pointer;
            transition: all 0.3s;
        }
        .footage-card:hover { border-color: #4a9eff; transform: translateY(-2px); }
        .footage-thumb {
            width: 100%;
            aspect-ratio: 16/9;
            background: linear-gradient(135deg, #0a0a0a, #111);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 44px;
            position: relative;
        }
        .footage-play {
            position: absolute;
            width: 50px;
            height: 50px;
            background: rgba(74,158,255,0.9);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            opacity: 0;
            transition: opacity 0.3s;
        }
        .footage-card:hover .footage-play { opacity: 1; }
        .footage-info {
            padding: 18px;
        }
        .footage-date { font-size: 15px; font-weight: 600; margin-bottom: 6px; color: #ddd; }
        .footage-meta { font-size: 12px; color: #555; line-height: 1.6; }
        .footage-duration { color: #4a9eff; font-weight: 600; }

        /* Video Player Modal */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.95);
            z-index: 1000;
            align-items: center;
            justify-content: center;
            backdrop-filter: blur(10px);
        }
        .modal-overlay.active { display: flex; }
        .modal-content {
            background: #111;
            border-radius: 16px;
            overflow: hidden;
            max-width: 95vw;
            max-height: 95vh;
            width: 1000px;
            border: 1px solid #222;
        }
        .modal-header {
            padding: 18px 24px;
            background: #1a1a1a;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #222;
        }
        .modal-title { font-weight: 600; font-size: 15px; color: #ddd; }
        .modal-close {
            background: none;
            border: none;
            color: #666;
            font-size: 24px;
            cursor: pointer;
            width: auto;
            padding: 0;
            transition: color 0.3s;
        }
        .modal-close:hover { color: #fff; }
        .modal-video {
            width: 100%;
            max-height: 75vh;
            display: block;
        }

        .empty-state {
            text-align: center;
            padding: 80px 20px;
            color: #333;
        }
        .empty-state .icon { font-size: 52px; margin-bottom: 20px; }
        .empty-state p { font-size: 16px; margin-bottom: 8px; }
        .empty-state .hint { font-size: 13px; color: #222; }

        .logout-btn {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            color: #888;
            padding: 10px 22px;
            border-radius: 10px;
            text-decoration: none;
            font-size: 13px;
            transition: all 0.3s;
            font-weight: 500;
        }
        .logout-btn:hover { background: rgba(255,255,255,0.1); color: #fff; }

        .refresh-btn {
            background: linear-gradient(135deg, #4a9eff, #357abd);
            border: none;
            color: white;
            padding: 10px 22px;
            border-radius: 10px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            transition: all 0.3s;
        }
        .refresh-btn:hover { transform: translateY(-1px); box-shadow: 0 8px 25px rgba(74,158,255,0.3); }

        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 25px;
        }
        .section-title { font-size: 18px; color: #888; font-weight: 500; }
        .section-count { color: #4a9eff; font-weight: 700; }

        @media (max-width: 768px) {
            .cameras-grid { grid-template-columns: 1fr; }
            .footage-grid { grid-template-columns: 1fr; }
            .content { padding: 15px; }
            .nav-tabs { padding: 0 15px; }
            .header { padding: 15px; }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🎥 CCTV Monitor <span class="live-badge">LIVE</span></h1>
        <div style="display:flex;align-items:center;gap:15px;">
            <span style="color:#555; font-size:13px;">👤 Admin</span>
            <a href="/logout" class="logout-btn">Logout</a>
        </div>
    </div>

    <div class="nav-tabs">
        <div class="nav-tab active" onclick="showTab('live')">📡 Live Cameras</div>
        <div class="nav-tab" onclick="showTab('footage')">📁 Footage Archive</div>
    </div>

    <div class="content">
        <!-- Live Cameras Tab -->
        <div id="live-section" class="section active">
            <div class="section-header">
                <div class="section-title">Connected Cameras (<span class="section-count" id="cam-count">0</span>)</div>
                <button class="refresh-btn" onclick="loadCameras()">🔄 Refresh</button>
            </div>
            <div id="cameras-container" class="cameras-grid">
                <div class="empty-state" style="grid-column:1/-1;">
                    <div class="icon">📡</div>
                    <p>No cameras connected</p>
                    <p class="hint">Open <code>/camera</code> on your mobile device to connect</p>
                </div>
            </div>
        </div>

        <!-- Footage Tab -->
        <div id="footage-section" class="section">
            <div class="section-header">
                <div class="section-title">Recorded Footage</div>
                <button class="refresh-btn" onclick="loadFootage()">🔄 Refresh</button>
            </div>
            <div id="footage-container" class="footage-grid">
                <div class="empty-state" style="grid-column:1/-1;">
                    <div class="icon">📁</div>
                    <p>No footage recorded yet</p>
                    <p class="hint">Footage is saved automatically every 10 minutes</p>
                </div>
            </div>
        </div>
    </div>

    <!-- Video Player Modal -->
    <div id="videoModal" class="modal-overlay" onclick="closeModal(event)">
        <div class="modal-content" onclick="event.stopPropagation()">
            <div class="modal-header">
                <span class="modal-title" id="modalTitle">Footage</span>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <video id="modalVideo" class="modal-video" controls autoplay></video>
        </div>
    </div>

    <script>
        let currentTab = 'live';

        function showTab(tab) {
            currentTab = tab;
            document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById(tab + '-section').classList.add('active');

            if (tab === 'live') loadCameras();
            if (tab === 'footage') loadFootage();
        }

        async function loadCameras() {
            try {
                const res = await fetch('/api/cameras');
                const cameras = await res.json();
                document.getElementById('cam-count').textContent = cameras.length;

                const container = document.getElementById('cameras-container');
                if (cameras.length === 0) {
                    container.innerHTML = `
                        <div class="empty-state" style="grid-column:1/-1;">
                            <div class="icon">📡</div>
                            <p>No cameras connected</p>
                            <p class="hint">Open <code>/camera</code> on your mobile device to connect</p>
                        </div>`;
                    return;
                }

                container.innerHTML = cameras.map(cam => `
                    <div class="camera-card">
                        <div class="camera-header">
                            <span class="camera-name">📱 ${cam.id.substring(0, 20)}${cam.id.length > 20 ? '...' : ''}</span>
                            <div class="camera-status">
                                <span class="status-dot ${cam.online ? 'status-online' : 'status-offline'}"></span>
                                ${cam.online ? 'Online' : 'Offline'}
                            </div>
                        </div>
                        <div class="camera-feed">
                            <img src="/camera/stream/${cam.id}?t=${Date.now()}" alt="Camera Feed" 
                                 onerror="this.style.display='none'; this.parentElement.innerHTML='<div class=\'no-signal\'><div class=\'no-signal-icon\'>📡</div>No Signal</div>'">
                        </div>
                        <div class="camera-info">
                            <span>Last seen: ${cam.last_seen}</span>
                            <span>${cam.fps} FPS</span>
                        </div>
                    </div>
                `).join('');
            } catch (e) {
                console.error('Failed to load cameras:', e);
            }
        }

        async function loadFootage() {
            try {
                const res = await fetch('/api/footage');
                const footage = await res.json();

                const container = document.getElementById('footage-container');
                if (footage.length === 0) {
                    container.innerHTML = `
                        <div class="empty-state" style="grid-column:1/-1;">
                            <div class="icon">📁</div>
                            <p>No footage recorded yet</p>
                            <p class="hint">Footage is saved automatically every 10 minutes</p>
                        </div>`;
                    return;
                }

                container.innerHTML = footage.map(f => `
                    <div class="footage-card" onclick="playFootage('${f.path}', '${f.name.replace(/'/g, "\'")}')">
                        <div class="footage-thumb">
                            🎬
                            <div class="footage-play">▶</div>
                        </div>
                        <div class="footage-info">
                            <div class="footage-date">${f.date}</div>
                            <div class="footage-meta">
                                📱 ${f.camera}<br>
                                <span class="footage-duration">⏱ ${f.duration}</span> · 
                                <span>${f.size}</span>
                            </div>
                        </div>
                    </div>
                `).join('');
            } catch (e) {
                console.error('Failed to load footage:', e);
            }
        }

        function playFootage(path, name) {
            document.getElementById('modalTitle').textContent = name;
            document.getElementById('modalVideo').src = '/footage/' + path;
            document.getElementById('videoModal').classList.add('active');
        }

        function closeModal(e) {
            if (!e || e.target.id === 'videoModal' || e.target.classList.contains('modal-close')) {
                document.getElementById('videoModal').classList.remove('active');
                document.getElementById('modalVideo').pause();
                document.getElementById('modalVideo').src = '';
            }
        }

        // Auto-refresh live cameras
        setInterval(() => {
            if (currentTab === 'live') loadCameras();
        }, 2000);

        // Initial load
        loadCameras();
    </script>
</body>
</html>
"""

# ==================== VIDEO STREAMING HELPERS ====================

def get_video_chunk(filename, byte1=None, byte2=None):
    """Read video file in chunks for HTTP range support"""
    filesize = os.path.getsize(filename)
    yielded = 0
    yield_size = 1024 * 1024  # 1MB chunks

    if byte1 is not None:
        if not byte2:
            byte2 = filesize
        yielded = byte1
        filesize = byte2

    with open(filename, 'rb') as f:
        if byte1 is not None:
            f.seek(byte1)

        while True:
            remaining = filesize - yielded
            if yielded >= filesize:
                break
            chunk_size = min(yield_size, remaining)
            data = f.read(chunk_size)
            if not data:
                break
            yield data
            yielded += len(data)

def parse_range_header(range_header, file_size):
    """Parse HTTP Range header"""
    match = re.search(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        return 0, file_size - 1

    byte_start = int(match.group(1))
    byte_end_str = match.group(2).strip()
    if byte_end_str:
        byte_end = int(byte_end_str)
    else:
        byte_end = file_size - 1

    byte_end = min(byte_end, file_size - 1)
    return byte_start, byte_end

# ==================== ROUTES ====================

@app.route('/camera')
def camera_page():
    return CAMERA_HTML

@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if check_password_hash(ADMIN_PASSWORD_HASH, password):
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        return ADMIN_LOGIN_HTML.replace('{% if error %}', '').replace('{% endif %}', '').replace('{{ error }}', 'Invalid password')

    return ADMIN_LOGIN_HTML.replace('{% if error %}', '').replace('{% endif %}', '').replace('{{ error }}', '')

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    return ADMIN_DASHBOARD_HTML

@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

# ==================== CAMERA FEED HANDLING ====================

def save_frame_to_video(camera_id, frame_data, timestamp):
    """Save a frame to the current video segment for this camera"""
    try:
        # Decode base64 image
        img_data = base64.b64decode(frame_data.split(',')[1])
        nparr = np.frombuffer(img_data, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            return

        cam_data = connected_cameras.get(camera_id)
        if not cam_data:
            return

        current_time = datetime.fromtimestamp(timestamp / 1000)

        # Check if we need to start a new segment (every 10 minutes)
        if cam_data.get('video_writer') is None or            (current_time - cam_data.get('segment_start', current_time)).total_seconds() >= SEGMENT_DURATION:

            # Close previous writer
            if cam_data.get('video_writer'):
                cam_data['video_writer'].release()
                print(f"[INFO] Saved segment: {cam_data.get('current_file', 'unknown')}")

            # Create new segment
            date_folder = os.path.join(footage_folder, camera_id, current_time.strftime('%Y-%m-%d'))
            os.makedirs(date_folder, exist_ok=True)

            filename = f"{current_time.strftime('%H-%M-%S')}.mp4"
            filepath = os.path.join(date_folder, filename)

            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            h, w = frame.shape[:2]
            cam_data['video_writer'] = cv2.VideoWriter(filepath, fourcc, 10.0, (w, h))
            cam_data['segment_start'] = current_time
            cam_data['current_file'] = filepath
            print(f"[INFO] Started new segment: {filepath}")

        # Write frame
        if cam_data.get('video_writer'):
            cam_data['video_writer'].write(frame)

    except Exception as e:
        print(f"[ERROR] Saving frame: {e}")

@app.route('/camera/feed', methods=['POST'])
def camera_feed():
    data = request.get_json()
    camera_id = data.get('camera_id')
    frame_data = data.get('frame')
    timestamp = data.get('timestamp', time.time() * 1000)

    if not camera_id or not frame_data:
        return jsonify({'error': 'Missing data'}), 400

    # Update or create camera entry
    if camera_id not in connected_cameras:
        connected_cameras[camera_id] = {
            'last_frame': None,
            'last_seen': datetime.now(),
            'video_writer': None,
            'segment_start': None,
            'current_file': None,
            'frame_count': 0,
            'fps': 0,
            'last_fps_time': time.time()
        }
        print(f"[INFO] New camera connected: {camera_id}")

    cam = connected_cameras[camera_id]
    cam['last_frame'] = frame_data
    cam['last_seen'] = datetime.now()
    cam['frame_count'] += 1

    # Calculate FPS
    now = time.time()
    if now - cam['last_fps_time'] >= 1:
        cam['fps'] = cam['frame_count']
        cam['frame_count'] = 0
        cam['last_fps_time'] = now

    # Save to video file
    save_frame_to_video(camera_id, frame_data, timestamp)

    return jsonify({'status': 'ok'})

@app.route('/camera/bulk_upload', methods=['POST'])
def bulk_upload():
    """Handle bulk upload of buffered frames from offline cameras"""
    data = request.get_json()
    camera_id = data.get('camera_id')
    frames = data.get('frames', [])

    if camera_id not in connected_cameras:
        connected_cameras[camera_id] = {
            'last_frame': None,
            'last_seen': datetime.now(),
            'video_writer': None,
            'segment_start': None,
            'current_file': None,
            'frame_count': 0,
            'fps': 0,
            'last_fps_time': time.time()
        }

    for frame_info in frames:
        save_frame_to_video(camera_id, frame_info['data'], frame_info['timestamp'])

    print(f"[INFO] Bulk uploaded {len(frames)} frames from {camera_id}")
    return jsonify({'status': 'ok', 'uploaded': len(frames)})

@app.route('/camera/stream/<camera_id>')
def camera_stream(camera_id):
    """MJPEG stream for admin dashboard"""
    def generate():
        while True:
            cam = connected_cameras.get(camera_id)
            if cam and cam.get('last_frame'):
                try:
                    frame_data = cam['last_frame']
                    img_data = base64.b64decode(frame_data.split(',')[1])

                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + img_data + b'\r\n')
                except:
                    pass
            time.sleep(0.1)

    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

# ==================== API ENDPOINTS ====================

@app.route('/api/cameras')
@login_required
def api_cameras():
    now = datetime.now()
    cameras = []
    for cam_id, cam_data in connected_cameras.items():
        is_online = (now - cam_data['last_seen']).total_seconds() < 15
        cameras.append({
            'id': cam_id,
            'online': is_online,
            'last_seen': cam_data['last_seen'].strftime('%H:%M:%S'),
            'fps': cam_data.get('fps', 0)
        })
    return jsonify(cameras)

@app.route('/api/footage')
@login_required
def api_footage():
    footage_list = []

    if not os.path.exists(footage_folder):
        return jsonify([])

    for camera_id in os.listdir(footage_folder):
        camera_path = os.path.join(footage_folder, camera_id)
        if not os.path.isdir(camera_path):
            continue

        for date_folder in os.listdir(camera_path):
            date_path = os.path.join(camera_path, date_folder)
            if not os.path.isdir(date_path):
                continue

            for filename in os.listdir(date_path):
                if filename.endswith('.mp4'):
                    filepath = os.path.join(date_path, filename)
                    size = os.path.getsize(filepath)
                    size_str = f"{size / 1024 / 1024:.1f} MB" if size > 1024*1024 else f"{size / 1024:.1f} KB"

                    # Get video duration
                    duration = "10:00"
                    try:
                        cap = cv2.VideoCapture(filepath)
                        fps = cap.get(cv2.CAP_PROP_FPS)
                        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                        if fps > 0 and frame_count > 0:
                            dur_sec = int(frame_count / fps)
                            duration = f"{dur_sec//60:02d}:{dur_sec%60:02d}"
                        cap.release()
                    except:
                        pass

                    footage_list.append({
                        'name': f"{camera_id} - {date_folder} {filename.replace('.mp4', '')}",
                        'path': f"{camera_id}/{date_folder}/{filename}",
                        'camera': camera_id,
                        'date': f"{date_folder} {filename.replace('.mp4', '')}",
                        'duration': duration,
                        'size': size_str
                    })

    # Sort by date (newest first)
    footage_list.sort(key=lambda x: x['path'], reverse=True)
    return jsonify(footage_list)

# ==================== VIDEO SERVING WITH RANGE SUPPORT ====================

@app.route('/footage/<path:filename>')
@login_required
def serve_footage(filename):
    """Serve video files with HTTP Range support for seeking/scrubbing"""
    video_path = os.path.join(footage_folder, filename)

    if not os.path.exists(video_path):
        return abort(404, "Video not found.")

    range_header = request.headers.get('Range', None)
    file_size = os.path.getsize(video_path)

    if not range_header:
        # No Range header - serve complete file
        with open(video_path, 'rb') as f:
            data = f.read()
        response = Response(data, 200, mimetype="video/mp4")
        response.headers.add("Content-Length", str(file_size))
        response.headers.add("Accept-Ranges", "bytes")
        return response

    # Handle partial content (Range header)
    byte_start, byte_end = parse_range_header(range_header, file_size)
    chunk_size = (byte_end - byte_start) + 1

    response = Response(
        get_video_chunk(video_path, byte_start, byte_end + 1),
        206,
        mimetype="video/mp4"
    )
    response.headers.add("Content-Range", f"bytes {byte_start}-{byte_end}/{file_size}")
    response.headers.add("Accept-Ranges", "bytes")
    response.headers.add("Content-Length", str(chunk_size))
    return response

# ==================== CLEANUP THREAD ====================

def cleanup_old_cameras():
    """Remove cameras that haven't been seen for 30 seconds and close their writers"""
    while True:
        time.sleep(10)
        now = datetime.now()
        to_remove = []
        for cam_id, cam_data in connected_cameras.items():
            if (now - cam_data['last_seen']).total_seconds() > 30:
                # Close video writer before removing
                if cam_data.get('video_writer'):
                    cam_data['video_writer'].release()
                    print(f"[INFO] Closed writer for disconnected camera: {cam_id}")
                to_remove.append(cam_id)

        for cam_id in to_remove:
            del connected_cameras[cam_id]
            print(f"[INFO] Camera disconnected: {cam_id}")

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_cameras, daemon=True)
cleanup_thread.start()

@app.after_request
def after_request(response):
    response.headers.add('Accept-Ranges', 'bytes')
    return response

if __name__ == '__main__':
    print("=" * 65)
    print("  🎥 CCTV Monitoring Server")
    print("=" * 65)
    print(f"  📱 Camera endpoint: http://localhost:5000/camera")
    print(f"  🔐 Admin endpoint:  http://localhost:5000/admin")
    print(f"  🔑 Admin password:  {os.environ.get('ADMIN_PASSWORD', 'admin123')}")
    print(f"  💾 Footage folder:  {os.path.abspath(footage_folder)}")
    print(f"  ⏱  Segment duration: {SEGMENT_DURATION//60} minutes")
    print("=" * 65)
    print("  📋 Setup:")
    print("     1. pip install -r requirements.txt")
    print("     2. Set ADMIN_PASSWORD env variable (optional, default: admin123)")
    print("     3. python cctv_server.py")
    print("     4. Open /camera on your old phone")
    print("     5. Open /admin on your admin device")
    print("=" * 65)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)