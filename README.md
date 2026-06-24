# Arducam 64MP · Raspberry Pi 5 Camera Controller

A full-featured web camera controller for the **Arducam 64MP IMX686** (and Camera Module 3) on Raspberry Pi 5.  
The Flask web UI (`camweb.py`) is accessible from any browser on the LAN and runs as a systemd service.

---

## Features

| Feature | Detail |
|---|---|
| Live MJPEG preview | Any browser on LAN — multi-viewer |
| Resolution / FPS / Bitrate | 720p · 1080p · 2160p (4K), 5–30 fps |
| Digital zoom | 1×–16×, live ROI crop |
| Autofocus | Continuous / Auto / Manual with lens position slider |
| Image transform | Flip vertical · Mirror horizontal |
| Info overlay | FPS, TX kbps, JPEG size, zoom, AF mode, timestamp |
| Local RTSP stream | MediaMTX on port 8554 — VLC, NVR, any RTSP client |
| RTSP relay | Push to any remote RTSP server (NVR, cloud, another Pi) |
| Relay resolution | Per-relay: 720p · 1080p · 2160p (4K), FFmpeg scale filter |
| Relay auto-reconnect | Exponential backoff, per-relay |
| Snapshot | 720p · 12MP · 64MP with overlay burn-in |
| Web tunnel | Expose web UI publicly — localhost.run, cloudflared, bore, ngrok |
| Hardware monitor | CPU%, RAM%, temperature, task count — live panel + threshold alerts |
| Event log | In-app log panel (last 300 entries) + `cam_events.log` file |
| Camera profiles | Arducam 64MP · Camera Module 3 (auto-matched resolution maps) |
| Config persistence | `camtest_config.json` — auto-saved, restored on restart |
| **Authentication** | Session-based login (PIN/Password) — `admin` and `user` roles |
| **Role-based UI** | `admin`: full control · `user`: view-only (controls greyed out) |
| **User Management** | Create / edit / delete users and change passwords from the Settings panel |
| Systemd service | Auto-start on boot, restart on crash |

---

## Hardware Requirements

- Raspberry Pi 5
- [Arducam 64MP IMX686](https://www.arducam.com/64mp-ultra-high-res-camera-raspberry-pi/) **or** Camera Module 3 — connected via MIPI CSI-2
- Raspberry Pi OS 64-bit (Bookworm recommended)

---

## Software Requirements

```bash
# Python packages
pip3 install flask pillow

# System packages
sudo apt install -y ffmpeg
```

### MediaMTX (local RTSP only)

Required only for the **local RTSP stream** feature. Not needed for relay-only or web UI use.

```bash
cd /home/pi
wget -q https://github.com/bluenviron/mediamtx/releases/download/v1.9.3/mediamtx_v1.9.3_linux_arm64v8.tar.gz
tar xzf mediamtx_v1.9.3_linux_arm64v8.tar.gz
sudo mv mediamtx /usr/local/bin/mediamtx
sudo chmod +x /usr/local/bin/mediamtx
rm mediamtx_v1.9.3_linux_arm64v8.tar.gz mediamtx.yml
```

---

## Authentication

All routes require login. Two roles are available:

| Role | Access |
|---|---|
| `admin` | Full control — all panels and controls |
| `user` | View-only — stream, status, hardware info; all control panels greyed out |

**Default credentials** (created automatically on first run):

| Username | Password | Role |
|---|---|---|
| `admin` | `123456` | admin |
| `user` | `123456` | user |

> **Change default passwords immediately** via Settings → User Management after first login.

### How it works

- Sessions use Flask signed cookies (`CAMWEB_SECRET` env var or built-in fallback key)
- Passwords stored as PBKDF2 hashes in `users.json` (via `werkzeug.security`)
- Unauthenticated page requests → redirect to `/login`
- Unauthenticated API requests → `401 {"ok": false, "error": "Unauthorized"}`
- Non-admin API requests to admin endpoints → `403 {"ok": false, "error": "Forbidden — admin only"}`

### Custom secret key

```bash
export CAMWEB_SECRET="your-random-secret-here"
python3 camweb.py
```

Or set `Environment=CAMWEB_SECRET=...` in `camweb.service`.

---

## Quick Start

```bash
python3 camweb.py
```

Open in any browser on the same network:

```
http://<PI_IP>:5000
```

You will be redirected to the login page. Enter `admin` / `123456` to log in.

---

## Auto-Start on Boot (systemd)

```bash
sudo cp /home/pi/py/camweb.service /etc/systemd/system/camweb.service
sudo systemctl daemon-reload
sudo systemctl enable camweb.service
sudo systemctl start camweb.service
```

### Common commands

```bash
sudo systemctl status camweb          # check status
sudo systemctl restart camweb         # restart after editing camweb.py
sudo journalctl -u camweb -f          # live logs
sudo journalctl -u camweb -n 50       # last 50 lines
sudo systemctl stop camweb
sudo systemctl disable camweb
```

---

## File Overview

```
camweb.py              Web backend (Flask) — all controller logic
camweb.service         systemd service unit for auto-start on boot
camtest_config.json    Persistent settings (auto-created on first run)
cam_events.log         Event log (auto-created on first run)
hardware_stats.json    Latest hardware snapshot written every poll cycle
users.json             User accounts with hashed passwords (auto-created on first run)
printscreens.py        Desktop screenshot helper (pyautogui)
static/css/app.css     Web UI stylesheet
static/js/app.js       Web UI JavaScript
templates/index.html   Web UI HTML template
templates/login.html   Login page
snap_*.jpg             Snapshot output files (gitignored)
```

---

## REST API

| Method | Endpoint | Description |
|---|---|---|
**Authentication** (no login required)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/login` | Login page |
| `POST` | `/api/auth/login` | Submit credentials → sets session cookie |
| `POST` | `/api/auth/logout` | Clear session |

**General** (login required)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/stream` | MJPEG browser stream |
| `GET` | `/api/auth/me` | Current user info `{username, role}` |
| `GET` | `/api/status` | Full JSON status (camera, relays, hw, log) |
| `GET` | `/api/config` | Read config |
| `GET` | `/api/config/defaults` | Factory default values |
| `GET` | `/api/hardware` | Latest hardware stats JSON |
| `GET` | `/api/log/file` | Last 100 lines of `cam_events.log` |
| `GET` | `/api/tunnel/providers` | List installed tunnel providers |
| `GET` | `/snaps/<filename>` | Download snapshot file |

**Admin only** (login + admin role required)

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/config` | Patch config |
| `POST` | `/api/config/reset` | Reset to defaults (keeps saved relays) |
| `POST` | `/api/rtsp/start` | Start local RTSP via MediaMTX |
| `POST` | `/api/rtsp/stop` | Stop local RTSP |
| `POST` | `/api/relay/add` | Add RTSP relay |
| `DELETE` | `/api/relay/remove/<id>` | Remove relay by ID |
| `POST` | `/api/snapshot` | Trigger snapshot |
| `POST` | `/api/zoom` | Set digital zoom level |
| `POST` | `/api/focus` | Set autofocus mode / lens position |
| `POST` | `/api/transform` | Set flip / mirror |
| `POST` | `/api/camera` | Switch camera index |
| `POST` | `/api/log/clear` | Clear event log |
| `POST` | `/api/tunnel/start` | Start web tunnel |
| `POST` | `/api/tunnel/stop` | Stop web tunnel |
| `GET` | `/api/auth/users` | List all users (admin only) |
| `POST` | `/api/auth/users` | Create / update / delete user (admin only) |

### Example: Login (get session cookie)

```bash
curl -c cookies.txt -X POST http://PI_IP:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"123456"}'
```

All subsequent admin API calls must include `-b cookies.txt`.

### Example: Add a user

```bash
curl -b cookies.txt -X POST http://PI_IP:5000/api/auth/users \
  -H "Content-Type: application/json" \
  -d '{"username":"operator","password":"secret","role":"user"}'
```

### Example: Change password / role

```bash
curl -b cookies.txt -X POST http://PI_IP:5000/api/auth/users \
  -H "Content-Type: application/json" \
  -d '{"username":"operator","password":"newpass","role":"admin"}'
```

### Example: Delete a user

```bash
curl -b cookies.txt -X POST http://PI_IP:5000/api/auth/users \
  -H "Content-Type: application/json" \
  -d '{"username":"operator","delete":true}'
```

### Example: Start local RTSP

```bash
curl -X POST http://PI_IP:5000/api/rtsp/start \
  -H "Content-Type: application/json" \
  -d '{"resolution":"1080p","fps":"30","bitrate":"4000","auto_reconnect":true}'
```

### Example: Add RTSP relay

```bash
curl -X POST http://PI_IP:5000/api/relay/add \
  -H "Content-Type: application/json" \
  -d '{"target_url":"rtsp://192.168.1.100:554/live","resolution":"1080p","fps":30,"bitrate":4000,"auto_reconnect":true}'
```

`resolution` must be `720p`, `1080p`, or `2160p` (default `720p`).

### Example: Trigger snapshot

```bash
curl -X POST http://PI_IP:5000/api/snapshot \
  -H "Content-Type: application/json" \
  -d '{"level":"high"}'
```

`level`: `low` (720p) · `mid` (12MP) · `high` (64MP)

### Example: Digital zoom

```bash
curl -X POST http://PI_IP:5000/api/zoom \
  -H "Content-Type: application/json" \
  -d '{"level":4.0}'
```

### Example: Set focus

```bash
curl -X POST http://PI_IP:5000/api/focus \
  -H "Content-Type: application/json" \
  -d '{"mode":"manual","lens_position":10.0}'
```

`mode`: `continuous` · `auto` · `manual`

---

## RTSP Relay

Push the camera stream to any remote RTSP server (NVR, cloud relay, another Pi). No local MediaMTX needed.

```
Pi Camera
   └─→ rpicam-vid (MJPEG)
           ├─→ MJPEG HTTP  → /stream (browser, multi-client)
           ├─→ Local RTSP  → MediaMTX → rtsp://PI:8554/cam
           ├─→ Relay 1     → FFmpeg scale(1080p) → rtsp://nvr.local:554/cam
           └─→ Relay 2     → FFmpeg scale(720p)  → rtsp://cloud:8554/live
```

- Each relay runs an independent FFmpeg process with its own resolution scale filter
- Resolution choices: `720p` (1280×720) · `1080p` (1920×1080) · `2160p` (3840×2160)
- Auto-reconnect with exponential backoff up to 60 s
- Multiple relays can run simultaneously
- Configs are auto-saved to `saved_relays` and restored on restart

---

## Web Tunnel

Expose the web UI to the public internet without port-forwarding. Supports four providers:

| Provider | Install |
|---|---|
| **localhost.run** | Only needs `ssh` (pre-installed on Pi OS) |
| **cloudflared** | Download the Cloudflare binary |
| **bore** | `cargo install bore-cli` |
| **ngrok** | Download from ngrok.com |

Start via the **WEB TUNNEL** panel in the UI, or:

```bash
curl -X POST http://PI_IP:5000/api/tunnel/start \
  -H "Content-Type: application/json" \
  -d '{"provider":"localhost.run"}'
```

The public URL appears in the UI and in `cam_events.log` once the tunnel is live.

---

## Hardware Monitor

Polls `/proc/stat`, `/proc/meminfo`, `/proc/loadavg`, and `/sys/class/thermal` every `hw_poll_interval` seconds (default 10 s).

- Live stats in the **HARDWARE** panel: CPU%, RAM%, temperature °C, task count
- Writes `hardware_stats.json` after every poll
- Logs a summary line to `cam_events.log` every minute
- Threshold warnings logged when CPU/RAM/temp exceed configured limits

---

## Camera Profiles

| Index | Name | Snapshot resolutions |
|---|---|---|
| `0` | Arducam 64MP | low: 1280×720 · mid: 4608×2592 · high: 9152×6944 |
| `1` | Camera Module 3 | low: 1280×720 · mid: 2304×1296 · high: 4608×2592 |

Switch via the UI camera selector or `POST /api/camera` with `{"index": 1}`.

---

## Configuration (`camtest_config.json`)

Auto-created on first run. All fields can be patched via `POST /api/config` or the Settings panel.

```json
{
  "autostart":        false,
  "resolution":       "720p",
  "fps":              "30",
  "bitrate":          "2000",
  "show_preview":     true,
  "auto_reconnect":   true,
  "show_overlay":     true,
  "zoom_level":       1.0,
  "af_mode":          "continuous",
  "lens_position":    0.0,
  "flip":             false,
  "mirror":           false,
  "camera_index":     0,
  "saved_relays":     [],
  "last_relay_url":   "",
  "hw_poll_interval": 10,
  "hw_cpu_warn":      80,
  "hw_ram_warn":      85,
  "hw_temp_warn":     75
}
```

| Key | Type | Description |
|---|---|---|
| `autostart` | bool | Start local RTSP automatically on launch |
| `resolution` | string | RTSP stream resolution: `720p` · `1080p` · `2160p` |
| `fps` | string | Frames per second: `5` · `10` · `15` · `30` |
| `bitrate` | string | Encoding bitrate in kbps |
| `show_preview` | bool | Show MJPEG preview in browser |
| `auto_reconnect` | bool | Restart FFmpeg on disconnect |
| `show_overlay` | bool | Burn info HUD onto preview frames |
| `zoom_level` | float | Digital zoom multiplier (1.0–16.0) |
| `af_mode` | string | `continuous` · `auto` · `manual` |
| `lens_position` | float | Manual lens position (0.0 = infinity) |
| `flip` | bool | Flip image vertically |
| `mirror` | bool | Mirror image horizontally |
| `camera_index` | int | `0` = Arducam 64MP · `1` = Camera Module 3 |
| `saved_relays` | array | Persisted relay configs (url, fps, bitrate, resolution, auto_reconnect) |
| `last_relay_url` | string | Last used relay URL (pre-filled in UI) |
| `hw_poll_interval` | int | Hardware poll interval in seconds |
| `hw_cpu_warn` | int | CPU % threshold for log warning |
| `hw_ram_warn` | int | RAM % threshold for log warning |
| `hw_temp_warn` | int | Temperature °C threshold for log warning |

---

## Architecture

### Single-camera tee

Only one process can open the camera at a time. `StreamPreview` is the single reader; all outputs are tee'd:

```
rpicam-vid (one process)
    └─→ StreamPreview thread
            ├─→ OverlayFeeder  (burns HUD when show_overlay=true)
            │       └─→ MjpegBroadcaster → /stream (HTTP, unlimited clients)
            ├─→ RtspBroadcaster → FFmpeg → MediaMTX → rtsp://PI:8554/cam
            └─→ RtspRelay(s)   → FFmpeg (per relay, with scale filter) → remote RTSP
```

### RTSP encoding

- Encoder: `libx264 -preset ultrafast -tune zerolatency`
- `h264_v4l2m2m` is **not** available on Pi 5 — do not use it
- Frame rate + scale: `-vf fps=N,scale=W:H` (relay only; local RTSP uses capture resolution)

### Relay resolution scaling

Each `RtspRelay` applies `-vf fps=N,scale=W:H` in its FFmpeg command, so relay output resolution is independent of the camera capture resolution.

---

## Troubleshooting

### VLC cannot connect to RTSP

```bash
ps aux | grep ffmpeg       # FFmpeg running?
ps aux | grep mediamtx     # MediaMTX running?
ss -tlnp | grep 8554       # port open?
```

### Error 255 on high-resolution snapshot

Increase CMA in `/boot/firmware/config.txt`:

```
dtoverlay=vc4-kms-v3d
gpu_mem=256
```

Then reboot.

### `rpicam-vid` not found

```bash
which libcamera-vid   # older Pi OS uses libcamera- prefix
```

`camweb.py` auto-detects `rpicam` vs `libcamera` prefix at startup.

### MJPEG stream freezes in browser

The browser reconnects automatically after 2 s via the `onerror` handler on the `<img>` tag.  
Check CPU load — 2160p encoding is heavy without hardware acceleration.

### Relay stuck in `reconnecting`

Check the target URL is reachable from the Pi:

```bash
ffprobe rtsp://TARGET_URL
```

Auto-reconnect backs off up to 60 s between retries. Remove and re-add the relay to reset immediately.

---

## Development Notes

- Python 3.11 · Raspberry Pi OS 64-bit (Bookworm) · Pi 5
- FFmpeg 5.1.8 (system package)
- MediaMTX v1.9.3
- Flask 2.x (session-based auth via signed cookies)
- Pillow (PIL)
- werkzeug.security — PBKDF2 password hashing (bundled with Flask, no extra install)

### Auth internals

- `users.json` — flat JSON array; loaded fresh on every request (file-based, no DB needed)
- `_load_users()` auto-creates the file with default `admin`/`user` accounts on first run
- `@login_required` / `@admin_required` decorators on all routes
- `applyRoleUI(role)` in `app.js` toggles `.ctrl-disabled` CSS class on `[data-admin-only]` accordion sections
