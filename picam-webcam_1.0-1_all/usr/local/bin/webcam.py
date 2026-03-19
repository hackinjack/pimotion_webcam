#!/usr/bin/env python3
import gevent.monkey
gevent.monkey.patch_all()

import os
import time
import subprocess

import shutil
import json
from picamera2.outputs import FfmpegOutput
from picamera2.encoders import H264Encoder

from flask import Flask, Response, render_template_string, send_from_directory, request
import picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput
import io
from threading import Condition, Lock
import threading
import cv2
import numpy as np
import logging

# SSL & Auth
from functools import wraps
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash

# --- PRE-INIT CLEANUP ---
# Forcibly clear any ghost libcamera processes holding /dev/video0 BEFORE importing picamera2
try:
    subprocess.run(["fuser", "-k", "/dev/video0"], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    time.sleep(1) # Let the V4L2 kernel driver unmap the memory
except Exception:
    pass


logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Auth
# Default Config (Including Auth)
RECORDING_DIR = '/home/jfk/videos'
CONFIG_FILE = '/home/jfk/webcam_config.json'
MOTION_ENABLED = True

config_data = {
    "MOTION_THRESHOLD": 1500000,
    "CLIP_SECONDS": 10,
    "VIDEO_BITRATE": 10000000,
    "MOTION_ENABLED": True,
    "WEB_PORT": 8773,
    "AUTH_USERNAME": "admin",
    "AUTH_PASSWORD_HASH": "pbkdf2:sha256:150000$8vt9qhg9NVcrgXpW$fc93fd73bd1a1b2be69525c3b0c514a6e2e18105b61b2af903b3e2edd11b7652"
}

def load_config():
    global config_data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                saved_data = json.load(f)
                config_data.update(saved_data)
                logger.info("Loaded persistent configuration.")
        except Exception as e:
            logger.error(f"Failed to load config: {e}")

def save_config():
    with config_lock:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=4)


# Web config state (thread-safe)
config_lock = Lock()
os.makedirs(RECORDING_DIR, exist_ok=True)

# ----------------------------------------------------------------------
# 1. Thread-safe Streaming Output Class
# Robustly parses raw MJPEG byte streams into valid JPEG frames
# ----------------------------------------------------------------------
class StreamingBuffer(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()
        self.buffer = b''
        self.last_frame_time = time.time()  # watchdog for V2L2 lock

    def write(self, buf):
        self.buffer += buf
        
        # Search for JPEG End of Image marker (\xff\xd9)
        a = self.buffer.find(b'\xff\xd8') # Start of Image
        b = self.buffer.find(b'\xff\xd9') # End of Image
        
        if a != -1 and b != -1 and b > a:
            # We found a complete frame!
            completed_frame = self.buffer[a:b+2]
            
            # Keep whatever bytes came after this frame for the next loop
            self.buffer = self.buffer[b+2:]

            with self.condition:
                self.frame = completed_frame
                self.last_frame_time = time.time()  # watchdog for V2L2 lock
                self.condition.notify_all()

        return len(buf)

# Initialize the global streaming buffer
stream_buffer = StreamingBuffer()

# ----------------------------------------------------------------------
# 2. Camera Setup (Background Encoding)
# ----------------------------------------------------------------------
logger.info("Acquiring camera...")
camera = picamera2.Picamera2()

# Create config and explicitly disable buffering to prevent memory leaks
config = camera.create_video_configuration(main={"size": (1280, 720)}, queue=False)
camera.configure(config)

camera.start_recording(JpegEncoder(), FileOutput(stream_buffer))
camera.start()
logger.info("Camera initialized and streaming.")

# ----------------------------------------------------------------------
# 3. Motion Detection (Runs in a separate background thread)
# ----------------------------------------------------------------------
prev_gray = None
is_recording = False
recording_lock = Lock()

def start_recording():
    global is_recording
    with recording_lock:
        if is_recording:
            return
        is_recording = True

# READ FROM config_data INSTEAD OF GLOBAL VARIABLES
    try:
        with config_lock:
            clip_length = config_data.get("CLIP_SECONDS", 10)
            current_bitrate = config_data.get("VIDEO_BITRATE", 10000000)

        timestamp = time.strftime('%Y%m%d-%H%M%S')
        filename = f'{RECORDING_DIR}/motion_{timestamp}.mp4'

        output = FfmpegOutput(filename)
        encoder = H264Encoder(current_bitrate) 
    
        camera.start_encoder(encoder, output)
        logger.info(f"[MOTION] Recording MP4 started: {filename}")
        time.sleep(clip_length)
        camera.stop_encoder(encoder)
    # immediate cloud sync after each clip
    # Trigger rclone asynchronously (non-blocking) so it doesn't freeze the recording thread
        subprocess.Popen(
            f'rclone copy "{filename}" PiCam1:PiCam1/ --progress',
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    except Exception as e:
        logger.error(f"[MOTION] Critical error during recording: {e}")
        os.system(f'rclone copy "{filename}" PiCam1:PiCam1/ --progress')
        logger.info(f"[MOTION] MP4 Recording finished: {filename}")
    is_recording = False

def motion_worker():
    """Background thread that constantly checks for motion."""
    global prev_gray
    while True:
        with stream_buffer.condition:
            stream_buffer.condition.wait(timeout=1.0)
            jpeg_bytes = stream_buffer.frame
            
        if jpeg_bytes is None or len(jpeg_bytes) == 0:
            time.sleep(0.1)
            continue
            
        try:
            # Decode the JPEG into an OpenCV array
            np_arr = np.frombuffer(jpeg_bytes, np.uint8)
            frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            
            if frame is None:
                continue

            with config_lock:
                base_threshold = config_data.get("MOTION_THRESHOLD", 1500000)
                  
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            
                        # --- DYNAMIC THRESHOLD LOGIC ---
            global current_brightness
            current_brightness = np.mean(gray)
            active_threshold = base_threshold

            # If image is dark (heavy sensor noise), double the required threshold
            if current_brightness < 50:
                active_threshold = base_threshold * 2

            if prev_gray is not None:
                frame_delta = cv2.absdiff(prev_gray, gray)
                thresh = cv2.threshold(frame_delta, 25, 255, cv2.THRESH_BINARY)[1]
                motion_score = int(thresh.sum())

                if MOTION_ENABLED and motion_score > active_threshold:
                    threading.Thread(target=start_recording, daemon=True).start()
                    logger.info(f"[MOTION] Score: {motion_score} (Thresh: {active_threshold}, Light: {int(current_brightness)})")
#
            prev_gray = gray
        except Exception as e:
            logger.error(f"Motion processing error: {e}")
            
        time.sleep(0.2)  # Throttle to ~5fps for motion analysis to save CPU

threading.Thread(target=motion_worker, daemon=True).start()

# ----------------------------------------------------------------------
# Watchdog Monitor (Auto-Reboot on Camera Freeze)
# ----------------------------------------------------------------------
def watchdog_worker():
    """Monitors the camera stream. If it freezes, reboots the Pi."""
    logger.info("[WATCHDOG] Camera freeze monitor started.")
    time.sleep(45) # Give the camera and OS plenty of time to start up initially

    while True:
        idle_time = time.time() - stream_buffer.last_frame_time

        # If no new frames have arrived in 15 seconds, the V4L2 driver has crashed
        if idle_time > 15.0:
            logger.error(f"🚨 CAMERA FREEZE DETECTED: No frames for {idle_time:.1f}s. REBOOTING PI! 🚨")

            # Issue the OS reboot command
            os.system("sudo reboot")

            # Sleep so we don't spam the reboot command while the OS shuts down
            time.sleep(120)

        time.sleep(5)

threading.Thread(target=watchdog_worker, daemon=True).start()


# ======================================================================
# DASHBOARD LOGIC
# ======================================================================
current_brightness = 100 # Placeholder for Stage 3

def get_sys_status():
    """Gets CPU Temp and Disk Space for dashboard."""
    try:
        temp = os.popen("vcgencmd measure_temp").readline().strip().replace("temp=","")
    except:
        temp = "N/A"
    total, used, free = shutil.disk_usage("/")
    free_gb = free // (2**30)
    return temp, free_gb

# ----------------------------------------------------------------------
# 4. Flask Web Routes
# ----------------------------------------------------------------------

def gen_frames():
    """Yields frames to the browser safely using the condition variable."""
    while True:
        with stream_buffer.condition:
            stream_buffer.condition.wait()
            frame = stream_buffer.frame
            
        if frame is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

# ======================================================================
# BASIC AUTHENTICATION
# ======================================================================
def check_auth(username, password):
    try:
        saved_username = config_data.get("AUTH_USERNAME", "admin")
        saved_hash = config_data.get("AUTH_PASSWORD_HASH", "")

        return username == saved_username and check_password_hash(saved_hash, password)
    except ValueError as e:
        logger.error(f"Hash validation error: {e}")
        return False

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    global current_brightness
    status = "ON" if MOTION_ENABLED else "OFF"

    # Grab the latest system metrics
    cpu_temp, disk_free = get_sys_status()

    return render_template_string(f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>PiCam Security</title>
        <style>
            body {{ font-family: Arial; background: #222; color: white; text-align: center; margin: 0; padding: 20px; }}
            .video-container {{ position: relative; display: inline-block; border: 3px solid #444; border-radius: 8px; overflow: hidden; }}
            .timestamp {{ position: absolute; bottom: 10px; right: 15px; color: yellow; font-size: 20px; font-weight: bold; background: rgba(0,0,0,0.5); padding: 2px 8px; border-radius: 4px; font-family: monospace; }}
            button, .btn {{ padding: 10px 15px; font-size: 16px; margin: 5px; cursor: pointer; text-decoration: none; color: black; background: #ddd; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <h2>🛡️ PiCam Zero 2W</h2>
            <div class="status-bar">
                🌡️ Temp: <b>{cpu_temp}</b> &nbsp;|&nbsp;
                💾 SD Free: <b>{disk_free} GB</b> &nbsp;|&nbsp;
                ☀️ Light: <b>{int(current_brightness)}/255</b>
        </div>

        <br><br>
        <div class="video-container">
            <img src="/video_feed" width="640" height="480">
            <div class="timestamp" id="clock"></div>
        </div>
        <br><br>
        <button onclick="fetch('/toggle').then(()=>location.reload())">Motion: {status}</button>
        <a href="/config" class="btn">⚙️ Settings</a>
        <a href="/videos" class="btn">📹 Recordings</a>
        
        <script>
            // Live JS Clock for Overlay
            setInterval(() => {{
                let d = new Date();
                document.getElementById('clock').innerText = d.toISOString().replace('T', ' ').substring(0, 19);
            }}, 1000);
        </script>
    </body>
    </html>
    ''')

@app.route('/config', methods=['GET', 'POST'])
@requires_auth
def config():
    global config_data
    update_msg = ""
    
    if request.method == 'POST':
        with config_lock:
            # Update camera settings
            config_data["MOTION_THRESHOLD"] = int(request.form.get('sensitivity', config_data["MOTION_THRESHOLD"]))
            config_data["CLIP_SECONDS"] = int(request.form.get('clip_length', config_data["CLIP_SECONDS"]))
            config_data["VIDEO_BITRATE"] = int(request.form.get('bitrate', config_data["VIDEO_BITRATE"]))
            config_data["WEB_PORT"] = int(request.form.get('web_port', config_data.get("WEB_PORT", 8773)))
            
            # Handle credential updates if provided
            new_user = request.form.get('new_username', '').strip()
            new_pass = request.form.get('new_password', '').strip()
            
            if new_user and new_pass:
                config_data["AUTH_USERNAME"] = new_user
                # Safely generate a new hash using pbkdf2 to avoid OpenSSL errors
                config_data["AUTH_PASSWORD_HASH"] = generate_password_hash(new_pass, method='pbkdf2:sha256:150000')
                update_msg = f"<p style='color:green; font-weight:bold;'>Credentials updated for {new_user}! Please refresh to login.</p>"
                
        save_config()
        logger.info("Config and/or credentials updated and saved.")
        if not update_msg:
            update_msg = "<p style='color:green; font-weight:bold;'>Settings Saved Successfully!</p>"
    
    cpu_temp, disk_free = get_sys_status()
    current_port = config_data.get("WEB_PORT", 5000)
    
    html = f'''
    <!DOCTYPE html><html><head><title>Config</title><style>body{{font-family:Arial; margin:40px}}</style></head>
    <body>
        <h1>⚙️ Dashboard & Config</h1>
        <a href="/">🏠 Live View</a><hr>
        {update_msg}
        
        <div style="background:#eee; color:black; padding:15px; border-radius:8px; margin-bottom:20px; width:400px;">
            <h3>📊 System Status</h3>
            <b>CPU Temp:</b> {cpu_temp}<br>
            <b>SD Card Free:</b> {disk_free} GB<br>
            <b>Light Level:</b> {int(current_brightness)}/255<br>
        </div>

        <form method="POST">
            <h3>Camera Settings</h3>
            <p><b>Sensitivity (Threshold):</b> {config_data["MOTION_THRESHOLD"]}<br>
            <input type="range" name="sensitivity" min="500000" max="5000000" step="100000" value="{config_data["MOTION_THRESHOLD"]}" style="width:300px"></p>
            <p><b>Clip Length (sec):</b> {config_data["CLIP_SECONDS"]}<br>
            <input type="range" name="clip_length" min="5" max="60" step="5" value="{config_data["CLIP_SECONDS"]}" style="width:300px"></p>
            <p><b>Bitrate (bps):</b> {config_data["VIDEO_BITRATE"]}<br>
            <hr>
            <h3>Network Settings</h3>
            <p><b>Web UI Port:</b><br>
            <input type="number" name="web_port" min="1024" max="65535" value="{current_port}" style="width:150px"></p>
            <p style="font-size:12px; color:#555;"><i>Note: Changing the port requires restarting the camera service in the terminal for the change to take effect.</i></p>
            <input type="range" name="bitrate" min="5000000" max="20000000" step="1000000" value="{config_data["VIDEO_BITRATE"]}" style="width:300px"></p>
            
            <hr>
            <h3>Security Settings</h3>
            <p><i>Leave blank to keep current credentials</i></p>
            <p><b>New Username:</b><br><input type="text" name="new_username" placeholder="admin"></p>
            <p><b>New Password:</b><br><input type="password" name="new_password" placeholder="New secret password"></p>
            
            <button type="submit" style="padding:10px 20px; background:#4CAF50; color:white; border:none; margin-top:15px;">💾 Save All Settings</button>
        </form>
    </body></html>
    '''
    return html

@app.route('/video_feed')
@requires_auth
def video_feed():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/toggle')
@requires_auth
def toggle():
    global MOTION_ENABLED
    MOTION_ENABLED = not MOTION_ENABLED
    logger.info(f"[WEB] Motion toggled {'ON' if MOTION_ENABLED else 'OFF'}")
    save_config()
    return 'OK'

@app.route('/videos')
@requires_auth
def list_videos():
    videos = []
    for filename in sorted(os.listdir(RECORDING_DIR), reverse=True)[:50]:
        if filename.endswith('.mp4'):
            path = os.path.join(RECORDING_DIR, filename)
            stat = os.stat(path)
            mtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))
            size_mb = round(stat.st_size / (1024*1024), 1)
            videos.append({
                'filename': filename,
                'mtime': mtime,
                'size_mb': size_mb,
                'url': f'/download/{filename}'
            })

    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Motion Clips</title>
        <style>
        body { font-family: Arial; margin: 20px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; border: 1px solid #ddd; text-align: left; }
        th { background-color: #f2f2f2; }
        img.thumb { width: 120px; height: 90px; object-fit: cover; }
        .btn { padding: 10px 20px; margin: 5px; font-size: 16px; text-decoration: none; }
        .btn-home { background: #4CAF50; color: white; }
        .btn-refresh { background: #2196F3; color: white; }
        .btn-download { background: #FF9800; color: white; padding: 8px 12px; text-decoration: none; border-radius: 4px; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        </style>
    </head>
    <body>
        <div style="margin-bottom: 20px;">
            <a href="/" class="btn btn-home">🏠 Home</a>
            <button onclick="location.reload()" class="btn btn-refresh">🔄 Refresh</button>
        </div>
    '''

    if videos:
        html += f'''
        <h2>Recent Motion Clips ({len(videos)})</h2>
        <table>
            <tr>
                <th>Thumbnail</th>
                <th>Filename</th>
                <th>Date/Time</th>
                <th>Size</th>
                <th>Download</th>
            </tr>
        '''
        for video in videos:
            html += f'''
            <tr>
                <td><canvas class="thumb" data-file="{video['filename']}"></canvas></td>
                <td>{video['filename']}</td>
                <td>{video['mtime']}</td>
                <td>{video['size_mb']} MB</td>
                <td><a href="{video['url']}" class="btn btn-download">⬇️ Download</a></td>
            </tr>
            '''
        html += '</table>'
    else:
        html += '<p>No motion clips yet. Wave at the camera! 😄</p>'

    html += '''
        <script>
        document.querySelectorAll('canvas.thumb').forEach(canvas => {{
            canvas.addEventListener('load', function() {{
                const ctx = this.getContext('2d');
                ctx.fillStyle = '#333';
                ctx.fillRect(0, 0, this.width, this.height);
                ctx.fillStyle = 'white';
                ctx.font = '16px Arial';
                ctx.textAlign = 'center';
                ctx.fillText('No Preview', 60, 45);
            }});
        }});
        </script>
    </body>
    </html>
    '''
    return html

@app.route('/download/<filename>')
@requires_auth
def download(filename):
    return send_from_directory(RECORDING_DIR, filename, as_attachment=True, download_name=filename)

if __name__ == '__main__':
    # Important: Load config FIRST to ensure we get the saved WEB_PORT
    load_config()
    current_port = config_data.get("WEB_PORT", 8773)

    logger.info(f"PiCam Motion Webcam (Gevent mode) starting on https://0.0.0.0:{current_port}")
    from gevent.pywsgi import WSGIServer
    import gevent
    import signal
    import ssl
    import sys
    import logging

    # Create a custom logger that ignores the SSLEOFError tracebacks
    # class QuietGeventLogger(logging.Logger):
#         def write(self, msg):
#             if "EOF occurred in violation of protocol" not in msg and "SSLEOFError" not in msg:
#                 logger.error(msg.strip())

#     quiet_logger = QuietGeventLogger("QuietGevent")


    # --- HTTPS MAIN SERVER & ERROR SUPPRESSION ---
    
    # Intercept sys.stderr to filter out the Gevent greenlet SSLEOFError crashes
    class FilteredStderr(object):
        def __init__(self, original_stderr):
            self.original_stderr = original_stderr
            self.ignore_next_traceback = False
            
        def write(self, msg):
            # Check if this line is the start of an SSLEOFError traceback
            if "ssl.SSLEOFError: EOF occurred in violation of protocol" in msg:
                self.ignore_next_traceback = True
                return
            elif "failed with SSLEOFError" in msg:
                self.ignore_next_traceback = False
                return
                
            # If we are currently ignoring a block, suppress standard traceback lines
            if self.ignore_next_traceback and ("Traceback" in msg or "File" in msg or msg.startswith(" ")):
                return
                
            # Otherwise, write the error normally
            self.original_stderr.write(msg)
            
        def flush(self):
            self.original_stderr.flush()

    # Apply the interceptor
    sys.stderr = FilteredStderr(sys.stderr)

# Explicitly create the SSL context
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    logger.info("SSL context created")
    
    try:
        # Load your Let's Encrypt certificates
        context.load_cert_chain(
            keyfile='/etc/letsencrypt/live/picam1.thirteenb.mywire.org/privkey.pem',
            certfile='/etc/letsencrypt/live/picam1.thirteenb.mywire.org/fullchain.pem'
            )
    except PermissionError:
        logger.error("CRITICAL: Python does not have permission to read the Let's Encrypt certificates!")
        exit(1)
    except Exception as e:
        logger.error(f"CRITICAL: SSL Context failed to load: {e}")
        exit(1)    
# Pass the context directly to the WSGIServer
    logger.info("Starting up")
    server = WSGIServer(('::', 8773), 
                        app,
                        ssl_context=context
    )
    logger.info("Flask webserver up, listening on https://0.0.0.0:{current_port}")

    def shutdown():
        logger.info("Shutting down... releasing camera.")
        server.stop()
        try:
            camera.stop()
            camera.close()
        except Exception as e:
            logger.error(f"Error closing camera: {e}")

    gevent.signal_handler(signal.SIGTERM, shutdown)
    gevent.signal_handler(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()

