# pimotion_webcam
# PiCam Motion Webcam

A headless, high-performance motion detection security camera built specifically for the **Raspberry Pi Zero 2W** (and other modern Pi models running Bookworm). It provides a responsive live web stream, OpenCV-based motion detection, and hardware-accelerated H.264 video recording, all wrapped in a portable `.deb` package.

## Features

- 📸 **Live MJPEG Stream:** View your camera in real-time from any web browser.
- 🏃 **Smart Motion Detection:** Uses OpenCV frame differencing (reusing the live stream buffer to save CPU/RAM).
- 🎥 **Hardware H.264 Encoding:** Records motion clips natively to MP4 format without bogging down the CPU.
- ⚙️ **Web Config UI:** Adjust motion sensitivity, clip length, and bitrate on the fly.
- 🛡️ **Bulletproof Streaming:** Uses `gevent` and `picamera2.StreamOutput` to ensure the camera never locks up when clients disconnect or navigate away.
- ☁️ **Cloud Sync Ready:** Includes documentation for syncing clips to Google Drive.
- ⚙️ **SSL:** Letsencrypt certificate generation script via the DYNU API and acme.sh .

## Requirements

- Raspberry Pi running **Raspberry Pi OS Bookworm** (or newer).
- A compatible Raspberry Pi Camera Module (e.g., Camera Module 3).
- Network connection (Wi-Fi/Ethernet).

---

## Installation via `.deb` Package (Recommended)

The easiest way to install is by building and using the Debian package, which automatically handles all dependencies and sets up the `systemd` service to run on boot.

### 1. Build the Package

Clone the repository and build the `.deb` file:

bash
git clone https://github.com/hackinjack/picam-webcam.git
cd picam-webcam
dpkg-deb --build picam-webcam_1.0-1_all

### 2. Install the Package
Install the newly created .deb file. This will automatically pull down dependencies like OpenCV, Flask, Gevent, and Picamera2.

bash
sudo apt install ./picam-webcam_1.0-1_all.deb
The postinst script will automatically start the background service.

### 3. Usage
Find your Raspberry Pi's IP address (hostname -I) and open it in a web browser on port 5000:

👉 http://<your-pi-ip>:5000

From the web UI, you can view the live stream, toggle motion detection, download recorded clips, and tune the camera's sensitivity.

Manual Installation (Without .deb)
If you prefer to run it manually without the package manager:

### 1. Install dependencies:

bash
sudo apt update
sudo apt install -y python3-flask python3-picamera2 python3-opencv python3-gevent python3-numpy ffmpeg

### 2. Run the script:

bash
python3 webcam.py

Cloud Backup (Google Drive)
You can easily sync your motion clips to Google Drive using rclone.

1. Configure rclone
Install rclone:

bash
sudo apt install -y rclone
Follow the headless setup guide to authorize your Google account:

bash
rclone config
(Name the remote PiCam1 and select Google Drive).

### 2. Set up the Sync Script
Create a script at /home/jfk/sync_videos.sh:

bash
#!/bin/bash
rclone sync /home/jfk/videos PiCam1:MotionVideos/ --include "*.mp4" --config /home/jfk/.config/rclone/rclone.conf --log-file /home/jfk/rclone.log

# Keep only the latest 50 clips locally
find /home/jfk/videos -name "*.mp4" | sort | head -n -50 | xargs -r rm
Make it executable: chmod +x /home/jfk/sync_videos.sh

3. Automate with Systemd Timer
Run the sync script automatically every 5 minutes.

Create /etc/systemd/system/picam-sync.service:

text
[Unit]
Description=Sync PiCam clips to Cloud
After=network-online.target

[Service]
Type=oneshot
ExecStart=/home/jfk/sync_videos.sh
User=jfk
Create /etc/systemd/system/picam-sync.timer:

text
[Unit]
Description=Sync PiCam clips every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
Enable it:

bash
sudo systemctl daemon-reload
sudo systemctl enable --now picam-sync.timer
Troubleshooting
Service won't start? Check the logs:

bash
journalctl -u webcam.service -f
"Device or resource busy" / "Pipeline handler in use" error? Another libcamera process is running. The service handles graceful shutdowns via SIGTERM, but if it crashes hard, kill rogue processes:

bash
sudo pkill -9 -f "python3.*webcam"
Motion not triggering? Open the Config page in the Web UI and lower the "Sensitivity" threshold.

License
MIT License. See LICENSE for details.
