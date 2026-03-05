#!/usr/bin/env python3
import gevent.monkey
gevent.monkey.patch_all()

from flask import Flask, Response, render_template_string, send_from_directory, request
import picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput
import io
from threading import Condition, Lock
import threading
import time
import os
import cv2
import numpy as np
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Config - tune these
MOTION_THRESHOLD = 1500000    # pixels changed (500k-5M)
CLIP_SECONDS = 10             # recording duration (5-60s)
VIDEO_BITRATE = 10000000      # MP4 quality (5M-20M)
MOTION_ENABLED = True
RECORDING_DIR = '/home/jfk/videos'

# Web config state (thread-safe)
config_lock = Lock()
os.makedirs(RECORDING_DIR, exist_ok=True)

# ----------------------------------------------------------------------
# 1. Thread-safe Streaming Output Class
# Parses raw chunked file bytes into valid JPEG frames
# ----------------------------------------------------------------------
class StreamingBuffer(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()
        self.buffer = io.BytesIO()

    def write(self, buf):
        # Check if this chunk is the start of a new JPEG image
        if buf.startswith(b'\xff\xd8'):
            # It's a new frame! Save the old buffer as a complete frame
            self.buffer.seek(0)
            completed_frame = self.buffer.read()
            
            # Reset buffer for the new frame
            self.buffer.seek(0)
            self.buffer.truncate()
            
            # Notify clients that a complete frame is ready
            if len(completed_frame) > 0:
                with self.condition:
                    self.frame = completed_frame
                    self.condition.notify_all()
                    
        # Append the new chunk to the current buffer
        self.buffer.write(buf)
        return len(buf)

# Initialize the global streaming buffer
stream_buffer = StreamingBuffer()

# ----------------------------------------------------------------------
# 2. Camera Setup (Background Encoding)
# ----------------------------------------------------------------------
camera = picamera2.Picamera2()
config = camera.create_video_configuration(main={"size": (1280, 720)})
camera.configure(config)

camera.start_recording(JpegEncoder(), FileOutput(stream_buffer))
camera.start()

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

    with config_lock:
        clip_length = CLIP_SECONDS

    timestamp = time.strftime('%Y%m%d-%H%M%S')
    filename = f'{RECORDING_DIR}/motion_{timestamp}.mp4'
    from picamera2.outputs import FfmpegOutput
    from picamera2.encoders import H264Encoder

    output = FfmpegOutput(filename)
    encoder = H264Encoder(bitrate=VIDEO_BITRATE) 
    
    camera.start_encoder(encoder, output)
    logger.info(f"[MOTION] Recording MP4 started: {filename}")
    time.sleep(clip_length)
    camera.stop_encoder(encoder)
    logger.info(f"[MOTION] MP4 Recording finished: {filename}")
    is_recording = False

def motion_worker():
    """Background thread that constantly checks for motion."""
    global prev_gray
    while True:
        with stream_buffer.condition:
            # Wait for a fully completed JPEG frame
            stream_buffer.condition.wait(timeout=1.0)
            jpeg_bytes = stream_buffer.frame
            
        if jpeg_bytes is None:
            time.sleep(0.1)
            continue
            
        try:
            # Decode the JPEG into an OpenCV array
            np_arr = np.frombuffer(jpeg_bytes, np.uint8)
            frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            
            if frame is None:
                continue

            with config_lock:
                threshold = MOTION_THRESHOLD
                  
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            
            if prev_gray is not None:
                frame_delta = cv2.absdiff(prev_gray, gray)
                thresh = cv2.threshold(frame_delta, 25, 255, cv2.THRESH_BINARY)[1]
                motion_score = int(thresh.sum())
                
                if MOTION_ENABLED and motion_score > threshold:
                    threading.Thread(target=start_recording, daemon=True).start()
                    logger.info(f"[MOTION] Detected: {motion_score} (threshold: {threshold})")
            
            prev_gray = gray
        except Exception as e:
            logger.error(f"Motion processing error: {e}")
            
        time.sleep(0.2)  # Throttle to ~5fps for motion analysis to save CPU

threading.Thread(target=motion_worker, daemon=True).start()

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

@app.route('/')
def index():
    status = "ON" if MOTION_ENABLED else "OFF"
    return render_template_string(f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>RPi Zero 2W PiCam Motion Webcam</title>
        <style>body{{font-family:Arial}} button{{padding:10px;font-size:18px}}</style>
    </head>
    <body>
        <h1>RPi Zero 2W PiCam Motion Webcam</h1>
        <img src="/video_feed" width="640" height="480">
        <br><br>
        <button onclick="toggleMotion()">Motion Detection: <span id="status">{status}</span></button>
        <a href="/config" style="color:blue;font-size:18px;margin-left:20px">⚙️ Config</a>

        <p>Threshold: {MOTION_THRESHOLD} | Videos: <a href="/videos">[List Recordings]</a></p>
        <script>
        async function toggleMotion() {{
            const res = await fetch('/toggle');
            if (res.ok) location.reload();
        }}
        </script>
    </body>
    </html>
    ''')

@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/toggle')
def toggle():
    global MOTION_ENABLED
    MOTION_ENABLED = not MOTION_ENABLED
    logger.info(f"[WEB] Motion toggled {'ON' if MOTION_ENABLED else 'OFF'}")
    return 'OK'

@app.route('/videos')
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
def download(filename):
    return send_from_directory(RECORDING_DIR, filename, as_attachment=True, download_name=filename)

@app.route('/config', methods=['GET', 'POST'])
def config():
    global MOTION_THRESHOLD, CLIP_SECONDS, VIDEO_BITRATE
    
    if request.method == 'POST':
        with config_lock:
            MOTION_THRESHOLD = int(request.form.get('sensitivity', MOTION_THRESHOLD))
            CLIP_SECONDS = int(request.form.get('clip_length', CLIP_SECONDS))
            VIDEO_BITRATE = int(request.form.get('bitrate', VIDEO_BITRATE))
        logger.info(f"CONFIG: sensitivity={MOTION_THRESHOLD}, clip={CLIP_SECONDS}s, bitrate={VIDEO_BITRATE}")
    
    html = f'''
    <!DOCTYPE html>
    <html>
    <head><title>Config</title>
    <style>body{{font-family:Arial;margin:40px}} input[type=range]{{width:300px}}</style>
    </head>
    <body>
        <h1>📱 Webcam Config</h1>
        <a href="/" style="color:blue;font-size:18px">🏠 Live View</a>
        <form method="POST">
            <p><label>Sensitivity: <span id="sens_val">{MOTION_THRESHOLD}</span></label><br>
            <input type="range" name="sensitivity" min="500000" max="5000000" step="100000" 
                   value="{MOTION_THRESHOLD}" oninput="document.getElementById('sens_val').innerText=this.value">
            <br><small>Lower = more sensitive (hand wave triggers)</small></p>
            
            <p><label>Clip Length: <span id="clip_val">{CLIP_SECONDS}</span>s</label><br>
            <input type="range" name="clip_length" min="5" max="60" step="5" 
                   value="{CLIP_SECONDS}" oninput="document.getElementById('clip_val').innerText=this.value">s</p>
            
            <p><label>Video Bitrate: <span id="bit_val">{VIDEO_BITRATE//1000000}</span>Mbps</label><br>
            <input type="range" name="bitrate" min="5000000" max="20000000" step="1000000" 
                   value="{VIDEO_BITRATE}" oninput="document.getElementById('bit_val').innerText=Math.round(this.value/1000000)+'M'">
            <br><small>Higher = better quality (slower on Zero 2W)</small></p>
            
            <button type="submit" style="padding:12px 24px;font-size:18px">💾 Apply Settings</button>
        </form>
    </body>
    </html>
    '''
    return html

if __name__ == '__main__':
    logger.info("PiCam Motion Webcam (Gevent mode) starting on http://0.0.0.0:5000")
    from gevent.pywsgi import WSGIServer
    import gevent
    import signal

    server = WSGIServer(('0.0.0.0', 5000), app)

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

