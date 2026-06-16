# Arducam 64MP · Raspberry Pi 5 Camera Controller

A full-featured web camera controller for the **Arducam 64MP IMX686** on Raspberry Pi 5.  
The Flask web UI (`camweb.py`) is accessible from any browser on the LAN and runs as a systemd service.

---

## Features

| Feature | `camweb.py` (Web UI) |
|---|---|
| Live MJPEG preview | ✅ Any browser on LAN |
| Camera settings (resolution, FPS, bitrate, zoom, AF) | ✅ |
| RTSP stream (local via MediaMTX) | ✅ |
| RTSP relay to remote server | ✅ |
| Snapshot (64MP / 12MP / 720p) | ✅ |
| Info overlay (FPS, TX, JPEG size) | ✅ |
| Event log panel | ✅ |
| RTSP auto-reconnect | ✅ |
| Multi-viewer support | ✅ |

---

## Hardware Requirements

- Raspberry Pi 5
- [Arducam 64MP IMX686](https://www.arducam.com/64mp-ultra-high-res-camera-raspberry-pi/) connected via MIPI CSI-2
- Raspberry Pi OS (64-bit)

---

## Software Requirements

```bash
# Python packages
pip3 install pillow flask

# System packages (usually pre-installed)
sudo apt install -y ffmpeg python3-tk
```

### MediaMTX (RTSP server)

Required for the **local RTSP stream** feature. Not needed for relay-only use.

```bash
cd /home/pi
wget -q https://github.com/bluenviron/mediamtx/releases/download/v1.9.3/mediamtx_v1.9.3_linux_arm64v8.tar.gz
tar xzf mediamtx_v1.9.3_linux_arm64v8.tar.gz
sudo mv mediamtx /usr/local/bin/mediamtx
sudo chmod +x /usr/local/bin/mediamtx
rm mediamtx_v1.9.3_linux_arm64v8.tar.gz mediamtx.yml
```

---

## Quick Start

### Web UI (manual)

```bash
python3 camweb.py
```

Then open in any browser on the same network:

```
http://<PI_IP>:5000
```

Example: `http://172.22.1.163:5000`

---

## Auto-Start on Boot (systemd)

`camweb.py` runs as a systemd service — starts automatically after every boot, restarts itself on crash.

### Install

```bash
# Copy the service file
sudo cp /home/pi/py/camweb.service /etc/systemd/system/camweb.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable camweb.service
sudo systemctl start camweb.service
```

### Common commands

```bash
# Check status
sudo systemctl status camweb

# View live logs
sudo journalctl -u camweb -f

# View last 50 log lines
sudo journalctl -u camweb -n 50

# Restart (e.g. after editing camweb.py)
sudo systemctl restart camweb

# Stop
sudo systemctl stop camweb

# Disable auto-start
sudo systemctl disable camweb
```

### How it works

- Starts after `network-online.target` — waits for LAN before binding port 5000
- Runs as user `pi` (required for camera access)
- `Restart=on-failure` — systemd restarts the process automatically if it crashes
- Logs go to the system journal (`journalctl`) and also to `cam_events.log`

---

## File Overview

```
camweb.py            Web backend (Flask) — browser-based controller
camweb.service       systemd service unit for auto-start on boot
camtest_config.json  Shared settings file (auto-created on first run)
cam_events.log       Event log (auto-created on first run)
printscreens.py      Desktop screenshot helper (pyautogui)
snap_*.jpg           Snapshot output files
```

---

## Web UI (`camweb.py`)

Access at `http://<PI_IP>:5000` from any device on the LAN.

### REST API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/stream` | MJPEG browser stream |
| `GET` | `/api/status` | Full JSON status |
| `POST` | `/api/rtsp/start` | Start local RTSP |
| `POST` | `/api/rtsp/stop` | Stop local RTSP |
| `POST` | `/api/relay/add` | Add RTSP relay |
| `DELETE` | `/api/relay/remove/<id>` | Remove relay |
| `GET/POST` | `/api/config` | Read / update config |
| `POST` | `/api/snapshot` | Trigger snapshot |
| `GET` | `/snaps/<filename>` | Download snapshot |

#### Start RTSP

```bash
curl -X POST http://PI_IP:5000/api/rtsp/start \
  -H "Content-Type: application/json" \
  -d '{"resolution":"1080p","fps":"30","bitrate":"4000","auto_reconnect":true}'
```

#### Add RTSP Relay

```bash
curl -X POST http://PI_IP:5000/api/relay/add \
  -H "Content-Type: application/json" \
  -d '{"target_url":"rtsp://192.168.1.100:554/live","fps":30,"bitrate":3000}'
```

#### Trigger Snapshot

```bash
curl -X POST http://PI_IP:5000/api/snapshot \
  -H "Content-Type: application/json" \
  -d '{"level":"mid"}'
```

---

## RTSP Relay Feature

The relay feature lets you **push the camera stream to any remote RTSP server** — NVR systems (Blue Iris, Milestone, Frigate), other Raspberry Pis, or cloud relay endpoints.

```
Pi Camera
   └─→ rpicam-vid (MJPEG)
           ├─→ Local preview / MJPEG HTTP stream
           ├─→ Local RTSP  (MediaMTX on port 8554)
           ├─→ Relay 1 → rtsp://nvr.local:554/cam
           └─→ Relay 2 → rtsp://192.168.1.50:8554/live
```

- Each relay runs an independent FFmpeg process
- Auto-reconnect with exponential backoff (up to 60 s)
- No local MediaMTX needed — FFmpeg connects directly to the remote
- Multiple relays can run simultaneously

---

## Architecture

### Single-camera constraint

Only **one process** can access the camera at a time. The solution is a **tee architecture**:

```
rpicam-vid (single process)
    └─→ StreamPreview thread
            ├─→ MjpegBroadcaster  → /stream (HTTP, multiple clients)
            ├─→ RtspBroadcaster   → FFmpeg → MediaMTX → rtsp://PI:8554/cam
            └─→ RtspRelay(s)      → FFmpeg → remote RTSP server(s)
```

### Tearing/stuttering fix

Worker threads write to `_pending_frame` (last-write-wins).  
A `_gui_tick()` timer fires every 33 ms and renders the latest frame once — preventing Tk event queue flooding.

### RTSP encoding

- Encoder: `libx264 -preset ultrafast -tune zerolatency`
- `h264_v4l2m2m` is **not** available on Pi 5 — do not use it
- FPS downsampling: `-vf fps=N` filter (e.g., 30 fps capture → 5 fps RTSP output)

---

## Configuration (`camtest_config.json`)

Auto-created in the same directory as the script on first run.

```json
{
  "autostart":      true,
  "resolution":     "2160p",
  "fps":            "30",
  "bitrate":        "4000",
  "show_preview":   true,
  "auto_reconnect": true,
  "show_overlay":   false,
  "zoom_level":     4.0,
  "af_mode":        "auto",
  "lens_position":  0.0,
  "flip":           false,
  "mirror":         false
}
```

| Key | Type | Description |
|---|---|---|
| `autostart` | bool | Start RTSP stream automatically on launch |
| `resolution` | string | Stream resolution: `720p` · `1080p` · `2160p` |
| `fps` | string | Frames per second: `5` · `10` · `15` · `30` |
| `bitrate` | string | Encoding bitrate in kbps |
| `show_preview` | bool | Show MJPEG preview |
| `auto_reconnect` | bool | Restart FFmpeg on network loss |
| `show_overlay` | bool | Show FPS/TX/size stats on preview |
| `zoom_level` | float | Digital zoom multiplier (1.0 = no zoom) |
| `af_mode` | string | Autofocus mode: `auto` · `manual` · `continuous` |
| `lens_position` | float | Manual lens position (0.0 = infinity) |
| `flip` | bool | Flip image vertically |
| `mirror` | bool | Mirror image horizontally |

---

## Troubleshooting

### VLC cannot connect to RTSP

1. Check FFmpeg is running: `ps aux | grep ffmpeg`
2. Check MediaMTX is running: `ps aux | grep mediamtx`
3. Check port 8554 is open: `ss -tlnp | grep 8554`
4. Verify MediaMTX config has `cam: {}` (not `cam:` null)

### Error 255 on high-resolution snapshot

Increase CMA (Contiguous Memory Allocator) in `/boot/firmware/config.txt`:

```
dtoverlay=vc4-kms-v3d
gpu_mem=256
```

### `rpicam-vid` not found

```bash
which libcamera-vid   # older Pi OS uses libcamera prefix
```

The scripts auto-detect `rpicam` vs `libcamera` prefix.

### MJPEG stream freezes in browser

The browser reconnects automatically after 2 s via the `onerror` handler on the `<img>` tag. Check the Pi's CPU load — 2160p RTSP encoding is heavy.

---

## Screenshot Helper (`printscreens.py`)

Utility for capturing the full desktop screen using `pyautogui`. Saves output as `full_screen_<timestamp>.png`.

```bash
pip3 install pyautogui
```

---

## Development Notes

- Python 3.11 · Raspberry Pi OS 64-bit · Pi 5
- FFmpeg 5.1.8 (system package)
- MediaMTX v1.9.3
- Flask 2.2.2
- Pillow (PIL)