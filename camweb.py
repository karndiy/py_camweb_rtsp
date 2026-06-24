#!/usr/bin/env python3
"""
camweb.py — Arducam 64MP  web controller  (Flask edition)

  python3 camweb.py
  → http://PI_IP:5000

Requires:
    pip3 install flask pillow
    mediamtx installed at /usr/local/bin/mediamtx
"""
import subprocess
import threading
import time
import io
import queue
import socket
import json
import os
import logging
import uuid
from collections import deque
from datetime import datetime
from functools import wraps
from PIL import Image, ImageDraw, ImageFont

try:
    from flask import (Flask, Response, request, jsonify,
                       send_from_directory, render_template,
                       session, redirect)
    from werkzeug.security import generate_password_hash, check_password_hash
except ImportError:
    print("Run first:  pip3 install flask pillow")
    raise SystemExit(1)

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
_BASE       = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BASE, "camtest_config.json")   # shared with camtest5.py
LOG_PATH      = os.path.join(_BASE, "cam_events.log")
HW_STATS_PATH = os.path.join(_BASE, "hardware_stats.json")
USERS_PATH    = os.path.join(_BASE, "users.json")
SNAP_DIR      = _BASE

RTSP_RES_MAP = {
    "720p":  (1280,  720),
    "1080p": (1920, 1080),
    "2160p": (3840, 2160),
}

CAMERA_PROFILES = {
    0: {
        "name":     "Arducam 64MP",
        "snap_res": {"low": (1280, 720), "mid": (4608, 2592), "high": (9152, 6944)},
    },
    1: {
        "name":     "Camera Module 3",
        "snap_res": {"low": (1280, 720), "mid": (2304, 1296), "high": (4608, 2592)},
    },
}

SOI = b'\xff\xd8'
EOI = b'\xff\xd9'

WEB_PORT = 5000

CONFIG_DEFAULTS = {
    "autostart":        False,
    "resolution":       "720p",
    "fps":              "30",
    "bitrate":          "2000",
    "show_preview":     True,
    "auto_reconnect":   True,
    "show_overlay":     True,
    "zoom_level":       1.0,
    "af_mode":          "continuous",
    "lens_position":    0.0,
    "flip":             False,
    "mirror":           False,
    "camera_index":     0,
    "saved_relays":     [],
    "last_relay_url":   "",
    "hw_poll_interval": 10,
    "hw_cpu_warn":      80,
    "hw_ram_warn":      85,
    "hw_temp_warn":     75,
}


# ─────────────────────────────────────────────────────────────────────────────
#  LOGGER
# ─────────────────────────────────────────────────────────────────────────────
class CamLog:
    def __init__(self):
        self._entries = deque(maxlen=300)
        self._lock    = threading.Lock()
        h = logging.FileHandler(LOG_PATH, encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        self._log = logging.getLogger("cam")
        self._log.setLevel(logging.DEBUG)
        if not self._log.handlers:
            self._log.addHandler(h)
        self.info("=== camweb started ===")

    def _add(self, tag, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._entries.appendleft({"tag": tag, "ts": ts, "msg": msg})

    def info(self,  msg): self._log.info(msg);    self._add("INF", msg)
    def warn(self,  msg): self._log.warning(msg); self._add("WRN", msg)
    def error(self, msg): self._log.error(msg);   self._add("ERR", msg)

    def recent(self, n=20):
        with self._lock:
            return list(self._entries)[:n]


cam_log = CamLog()


# ─────────────────────────────────────────────────────────────────────────────
#  HARDWARE MONITOR  (CPU, RAM, tasks, temperature)
# ─────────────────────────────────────────────────────────────────────────────
class HardwareMonitor:
    def __init__(self, get_config=None):
        self._get_config = get_config   # callable → config dict, or None
        self._lock       = threading.Lock()
        self._stats      = {}
        self._stop_evt   = threading.Event()
        self._prev_cpu   = None
        self._poll_count = 0
        self._thread     = threading.Thread(target=self._run, daemon=True, name="hw-monitor")
        self._thread.start()
        cam_log.info("Hardware monitor started")

    def _cfg(self, key):
        if self._get_config:
            return self._get_config().get(key, CONFIG_DEFAULTS.get(key))
        return CONFIG_DEFAULTS.get(key)

    def _read_cpu_fields(self):
        with open("/proc/stat") as f:
            line = f.readline()
        return list(map(int, line.split()[1:8]))  # user nice sys idle iowait irq softirq

    def _cpu_percent(self):
        curr = self._read_cpu_fields()
        if self._prev_cpu is None:
            time.sleep(0.5)
            curr = self._read_cpu_fields()
            self._prev_cpu = curr
            return 0.0
        prev = self._prev_cpu
        self._prev_cpu = curr
        idle  = curr[3] - prev[3]
        total = sum(curr) - sum(prev)
        return round(100.0 * (1.0 - idle / total), 1) if total else 0.0

    def _ram_stats(self):
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k.strip()] = int(v.split()[0])  # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        used  = total - avail
        pct   = round(100.0 * used / total, 1) if total else 0.0
        return {"total_mb": round(total / 1024, 1),
                "used_mb":  round(used  / 1024, 1),
                "percent":  pct}

    def _task_count(self):
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        running, total = parts[3].split("/")
        return {"running": int(running), "total": int(total)}

    def _temperature(self):
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return round(int(f.read().strip()) / 1000.0, 1)
        except Exception:
            return None

    def _run(self):
        while not self._stop_evt.is_set():
            try:
                cpu   = self._cpu_percent()
                ram   = self._ram_stats()
                tasks = self._task_count()
                temp  = self._temperature()

                stats = {
                    "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "cpu_percent":   cpu,
                    "ram":           ram,
                    "tasks":         tasks,
                    "temperature_c": temp,
                }
                with self._lock:
                    self._stats = stats

                try:
                    with open(HW_STATS_PATH, "w") as f:
                        json.dump(stats, f, indent=2)
                except Exception as ex:
                    cam_log.error(f"HW stats save failed: {ex}")

                self._poll_count += 1
                # Write to log file every minute (every 6 × 10s polls)
                if self._poll_count % 6 == 0:
                    cam_log._log.info(
                        f"HW cpu={cpu}% ram={ram['percent']}%"
                        f" ({ram['used_mb']:.0f}/{ram['total_mb']:.0f}MB)"
                        f" temp={temp}°C tasks={tasks['total']}"
                    )

                cpu_warn  = self._cfg("hw_cpu_warn")
                ram_warn  = self._cfg("hw_ram_warn")
                temp_warn = self._cfg("hw_temp_warn")
                if cpu > cpu_warn:
                    cam_log.warn(f"High CPU: {cpu}%  (warn>{cpu_warn}%)")
                if ram["percent"] > ram_warn:
                    cam_log.warn(f"High RAM: {ram['percent']}%  ({ram['used_mb']:.0f}/{ram['total_mb']:.0f} MB)")
                if temp is not None and temp > temp_warn:
                    cam_log.warn(f"High temp: {temp}°C  (warn>{temp_warn}°C)")

            except Exception as ex:
                cam_log.error(f"HW monitor error: {ex}")

            self._stop_evt.wait(self._cfg("hw_poll_interval") or 10)

    def get_stats(self):
        with self._lock:
            return dict(self._stats)

    def stop(self):
        self._stop_evt.set()
        cam_log.info("Hardware monitor stopped")


hw_monitor: "HardwareMonitor" = None   # set in main()


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_cmd_prefix():
    if subprocess.run(["which", "rpicam-vid"], capture_output=True).returncode == 0:
        return "rpicam"
    return "libcamera"


def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


LAN_IP = _get_lan_ip()


# ─────────────────────────────────────────────────────────────────────────────
#  MJPEG HTTP BROADCASTER
#  Distributes raw JPEG bytes to all connected /stream browser clients.
# ─────────────────────────────────────────────────────────────────────────────
class MjpegBroadcaster:
    def __init__(self):
        self._clients  = {}     # cid → queue.Queue
        self._lock     = threading.Lock()
        self._last     = None   # last frame (sent immediately to new subscribers)

    def feed(self, jpeg: bytes):
        self._last = jpeg
        with self._lock:
            for q in self._clients.values():
                try:
                    q.put_nowait(jpeg)
                except queue.Full:
                    pass   # slow client — drop frame

    def subscribe(self):
        cid = str(uuid.uuid4())[:8]
        q   = queue.Queue(maxsize=4)
        if self._last:
            q.put_nowait(self._last)
        with self._lock:
            self._clients[cid] = q
        return cid, q

    def unsubscribe(self, cid):
        with self._lock:
            self._clients.pop(cid, None)

    @property
    def client_count(self):
        with self._lock:
            return len(self._clients)


# ─────────────────────────────────────────────────────────────────────────────
#  OVERLAY FEEDER  (burns info HUD onto MJPEG frames before broadcasting)
# ─────────────────────────────────────────────────────────────────────────────
class OverlayFeeder:
    """Burns a two-line info HUD onto each JPEG when show_overlay is on."""

    def __init__(self, broadcaster, get_stats, get_config):
        self._bc         = broadcaster
        self._get_stats  = get_stats
        self._get_config = get_config
        self._font_cache = {}

    def _font(self, size):
        if size not in self._font_cache:
            try:
                self._font_cache[size] = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", size)
            except Exception:
                self._font_cache[size] = ImageFont.load_default()
        return self._font_cache[size]

    def feed(self, jpeg: bytes):
        cfg = self._get_config()
        if not cfg.get("show_overlay"):
            self._bc.feed(jpeg)
            return
        try:
            img  = Image.open(io.BytesIO(jpeg))
            w, h = img.size
            st   = self._get_stats()
            ts   = datetime.now().strftime("%H:%M:%S")
            fs   = max(11, w // 90)
            font = self._font(fs)
            zoom     = cfg.get("zoom_level", 1.0)
            af       = cfg.get("af_mode", "auto")
            cam_idx  = int(cfg.get("camera_index", 0))
            cam_name = CAMERA_PROFILES.get(cam_idx, {}).get("name", f"CAM{cam_idx}")
            line1 = f"{cam_name}  {w}×{h}  {st['fps']:.1f} fps  {st['jpeg_kb']:.1f} kB/frame"
            line2 = f"ZOOM {zoom:.1f}×  AF:{af}  {ts}"
            lh    = fs + 4
            bh    = lh * 2 + 12
            banner = Image.new("RGBA", (w, bh), (0, 0, 0, 160))
            base   = img.convert("RGBA")
            base.paste(banner, (0, 0), mask=banner.split()[3])
            draw   = ImageDraw.Draw(base)
            draw.text((8, 6),      line1, font=font, fill="#00e5b0")
            draw.text((8, 6 + lh), line2, font=font, fill="#a0c0ff")
            buf = io.BytesIO()
            base.convert("RGB").save(buf, format="JPEG", quality=82)
            self._bc.feed(buf.getvalue())
        except Exception:
            self._bc.feed(jpeg)


# ─────────────────────────────────────────────────────────────────────────────
#  STREAM PREVIEW  (camera reader thread)
#  Feeds raw JPEG bytes to any object with a .feed(jpeg) method.
# ─────────────────────────────────────────────────────────────────────────────
class StreamPreview(threading.Thread):
    """
    Reads MJPEG from rpicam-vid and distributes raw JPEG bytes to feeders.
    feeders: list of objects implementing .feed(jpeg_bytes).
    on_stats(fps, avg_jpeg_kb) fires once per second.
    """

    def __init__(self, cmd_prefix, width, height, fps,
                 feeders=None, on_status=None, on_stats=None,
                 zoom_level=1.0, af_mode="continuous", lens_position=0.0,
                 flip=False, mirror=False, camera_index=0):
        super().__init__(daemon=True)
        self.cmd_prefix    = cmd_prefix
        self.width         = width
        self.height        = height
        self.fps           = fps
        self.feeders       = feeders or []
        self.on_status     = on_status or (lambda m: None)
        self.on_stats      = on_stats
        self.zoom_level    = zoom_level
        self.af_mode       = af_mode
        self.lens_position = lens_position
        self.flip          = flip
        self.mirror        = mirror
        self.camera_index  = camera_index
        self._stop_evt     = threading.Event()
        self._proc         = None

    def stop(self):
        self._stop_evt.set()
        if self._proc:
            try: self._proc.terminate(); self._proc.wait(timeout=2)
            except Exception: pass

    def run(self):
        cmd = [
            f"{self.cmd_prefix}-vid",
            "--camera",    str(self.camera_index),
            "--codec",     "mjpeg",
            "--width",     str(self.width),
            "--height",    str(self.height),
            "--framerate", str(self.fps),
            "--nopreview",
            "-t", "0", "-o", "-",
        ]
        # center-crop digital zoom via ROI
        if self.zoom_level > 1.0:
            z  = float(self.zoom_level)
            rw = 1.0 / z
            rh = 1.0 / z
            rx = (1.0 - rw) / 2.0
            ry = (1.0 - rh) / 2.0
            cmd += ["--roi", f"{rx:.4f},{ry:.4f},{rw:.4f},{rh:.4f}"]
        # flip / mirror
        if self.flip:
            cmd += ["--vflip"]
        if self.mirror:
            cmd += ["--hflip"]
        # autofocus
        cmd += ["--autofocus-mode", self.af_mode]
        if self.af_mode == "manual":
            cmd += ["--lens-position", str(self.lens_position)]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
        except FileNotFoundError:
            self.on_status(f"{self.cmd_prefix}-vid not found"); return
        except Exception as ex:
            self.on_status(f"Launch failed: {ex}"); return

        cam_log.info(f"Camera started  {self.width}×{self.height} @ {self.fps}fps")
        self.on_status("Streaming")
        buf    = b""
        s_cnt  = 0; s_byt = 0; s_ts = time.time()

        while not self._stop_evt.is_set():
            try:
                chunk = self._proc.stdout.read(65536)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk

            while True:
                s = buf.find(SOI)
                if s == -1: buf = b""; break
                e = buf.find(EOI, s + 2)
                if e == -1: buf = buf[s:]; break

                jpeg = buf[s : e + 2]
                buf  = buf[e + 2:]

                for f in self.feeders:
                    try: f.feed(jpeg)
                    except Exception: pass

                s_cnt += 1; s_byt += len(jpeg)
                now = time.time(); dt = now - s_ts
                if dt >= 1.0 and self.on_stats:
                    try:
                        self.on_stats(s_cnt / dt, (s_byt / s_cnt) / 1024)
                    except Exception:
                        pass
                    s_cnt = 0; s_byt = 0; s_ts = now

        cam_log.info("Camera stopped")
        if self._proc:
            try: self._proc.stdout.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
#  LOCAL RTSP BROADCASTER  (FFmpeg → local MediaMTX)
# ─────────────────────────────────────────────────────────────────────────────
class RtspBroadcaster:
    def __init__(self, fps=30, bitrate_kbps=2000,
                 auto_reconnect=True, on_disconnect=None, on_reconnect=None):
        self._q              = queue.Queue(maxsize=2)
        self._stop_evt       = threading.Event()
        self._mtx_proc       = None
        self._ff_proc        = None
        self._auto_reconnect = auto_reconnect
        self._on_disconnect  = on_disconnect
        self._on_reconnect   = on_reconnect
        self.tx_kbps         = 0.0
        self._ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-f", "mjpeg", "-i", "pipe:0",
            "-vf", f"fps={fps}",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", f"{bitrate_kbps}k",
            "-g", str(fps),
            "-rtsp_transport", "tcp",
            "-f", "rtsp", "rtsp://localhost:8554/cam",
        ]
        self._start()

    @property
    def rtsp_url(self): return f"rtsp://{LAN_IP}:8554/cam"

    def _start(self):
        cfg = "/tmp/mediamtx_cam.yml"
        with open(cfg, "w") as f:
            f.write("paths:\n  cam: {}\n")
        self._mtx_proc = subprocess.Popen(
            ["mediamtx", cfg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        self._ff_proc = self._spawn_ff()
        threading.Thread(target=self._run,      daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        cam_log.info(f"RTSP local started  {self.rtsp_url}")

    def _spawn_ff(self):
        return subprocess.Popen(
            self._ffmpeg_cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)

    def feed(self, jpeg: bytes):
        try: self._q.put_nowait(jpeg)
        except queue.Full: pass

    def _run(self):
        tx_b = 0; tx_t = time.time()
        while not self._stop_evt.is_set():
            try: frame = self._q.get(timeout=0.5)
            except queue.Empty: continue
            proc = self._ff_proc
            if proc is None or proc.poll() is not None: continue
            try:
                proc.stdin.write(frame)
                tx_b += len(frame)
                now = time.time()
                if now - tx_t >= 1.0:
                    self.tx_kbps = tx_b * 8 / (now - tx_t) / 1000
                    tx_b = 0; tx_t = now
            except (BrokenPipeError, OSError): pass

    def _watchdog(self):
        while not self._stop_evt.is_set():
            time.sleep(3)
            if self._stop_evt.is_set(): break
            if self._mtx_proc and self._mtx_proc.poll() is not None:
                cam_log.warn("RTSP: MediaMTX crashed")
                if self._on_disconnect: self._on_disconnect("MediaMTX crashed")
                if self._auto_reconnect and not self._stop_evt.is_set():
                    self._restart_all()
                continue
            if self._ff_proc and self._ff_proc.poll() is not None:
                cam_log.warn("RTSP: FFmpeg exited")
                if self._on_disconnect: self._on_disconnect("FFmpeg exited")
                if self._auto_reconnect and not self._stop_evt.is_set():
                    self._restart_ff()

    def _restart_ff(self):
        backoff = 3
        while not self._stop_evt.is_set():
            try:
                if self._ff_proc:
                    try: self._ff_proc.stdin.close()
                    except: pass
                self._ff_proc = self._spawn_ff()
                cam_log.info("RTSP: FFmpeg restarted")
                if self._on_reconnect: self._on_reconnect()
                return
            except Exception as ex:
                cam_log.error(f"RTSP restart failed: {ex}")
                for _ in range(backoff):
                    if self._stop_evt.is_set(): return
                    time.sleep(1)
                backoff = min(backoff * 2, 60)

    def _restart_all(self):
        for p in (self._ff_proc, self._mtx_proc):
            if p:
                try: p.terminate()
                except: pass
        time.sleep(1)
        try:
            cfg = "/tmp/mediamtx_cam.yml"
            self._mtx_proc = subprocess.Popen(
                ["mediamtx", cfg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.8)
            self._ff_proc = self._spawn_ff()
            cam_log.info("RTSP: full restart ok")
            if self._on_reconnect: self._on_reconnect()
        except Exception as ex:
            cam_log.error(f"RTSP full restart failed: {ex}")

    def stop(self):
        cam_log.info("RTSP local stopping")
        self._stop_evt.set()
        if self._ff_proc:
            try: self._ff_proc.stdin.close()
            except: pass
            try: self._ff_proc.terminate(); self._ff_proc.wait(timeout=3)
            except: pass
        if self._mtx_proc:
            try: self._mtx_proc.terminate(); self._mtx_proc.wait(timeout=3)
            except: pass


# ─────────────────────────────────────────────────────────────────────────────
#  WEB TUNNEL  (expose the Flask web UI to the public internet, MVP)
# ─────────────────────────────────────────────────────────────────────────────
class TunnelManager:
    """Reverse-tunnel the web UI for external access. Zero-install default: localhost.run."""

    _PROVIDERS = {
        "localhost.run": {
            "cmd":    lambda p: ["ssh", "-o", "StrictHostKeyChecking=no",
                                 "-o", "ServerAliveInterval=30",
                                 "-o", "ExitOnForwardFailure=yes",
                                 "-R", f"80:localhost:{p}", "nokey@localhost.run"],
            "url_re": r"https://[a-z0-9]+\.lhrtunnel\.link",
            "needs":  "ssh",
        },
        "cloudflared": {
            "cmd":    lambda p: ["cloudflared", "tunnel", "--url", f"http://localhost:{p}"],
            "url_re": r"https://[a-z0-9\-]+\.trycloudflare\.com",
            "needs":  "cloudflared",
        },
        "bore": {
            "cmd":    lambda p: ["bore", "local", str(p), "--to", "bore.pub"],
            "url_re": r"bore\.pub:(\d+)",
            "url_grp": lambda m: f"http://bore.pub:{m.group(1)}",
            "needs":  "bore",
        },
        "ngrok": {
            "cmd":    lambda p: ["ngrok", "http", str(p), "--log", "stdout", "--log-level", "info"],
            "url_re": r"url=(https://[^\s]+\.ngrok[^\s]*)",
            "url_grp": lambda m: m.group(1),
            "needs":  "ngrok",
        },
    }

    def __init__(self, port: int = WEB_PORT):
        self._port     = port
        self._proc     = None
        self._lock     = threading.Lock()
        self._url      = None
        self._provider = None
        self._state    = "stopped"   # stopped | starting | running | error
        self._error    = None

    @classmethod
    def available_providers(cls) -> list:
        return [name for name, cfg in cls._PROVIDERS.items()
                if subprocess.run(["which", cfg["needs"]], capture_output=True).returncode == 0]

    def start(self, provider: str = "localhost.run") -> dict:
        with self._lock:
            if self._state not in ("stopped", "error"):
                return {"ok": False, "error": "Tunnel already running"}
            cfg = self._PROVIDERS.get(provider)
            if not cfg:
                return {"ok": False, "error": f"Unknown provider: {provider}"}
            if subprocess.run(["which", cfg["needs"]], capture_output=True).returncode != 0:
                return {"ok": False, "error": f"'{cfg['needs']}' not installed"}
            self._state    = "starting"
            self._url      = None
            self._error    = None
            self._provider = provider

        try:
            proc = subprocess.Popen(
                cfg["cmd"](self._port),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except Exception as ex:
            with self._lock:
                self._state = "error"
                self._error = str(ex)
            return {"ok": False, "error": str(ex)}

        with self._lock:
            self._proc = proc

        threading.Thread(
            target=self._reader, args=(proc, cfg["url_re"], cfg.get("url_grp")),
            daemon=True, name="tunnel-reader"
        ).start()
        cam_log.info(f"Tunnel starting via {provider}")
        return {"ok": True}

    def stop(self) -> dict:
        with self._lock:
            proc = self._proc
            self._proc  = None
            self._url   = None
            self._state = "stopped"
            self._error = None
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try: proc.kill()
                except Exception: pass
        cam_log.info("Tunnel stopped")
        return {"ok": True}

    def _reader(self, proc, url_re: str, url_grp):
        import re
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    cam_log.info(f"[tunnel] {line[:140]}")
                m = re.search(url_re, line)
                if m:
                    url = url_grp(m) if url_grp else m.group(0)
                    with self._lock:
                        if self._proc is proc:
                            self._url   = url
                            self._state = "running"
                    cam_log.info(f"Tunnel live: {url}")
        except Exception:
            pass
        with self._lock:
            if self._proc is proc:
                self._state = "error"
                self._error = "Tunnel process exited"
                self._proc  = None
        cam_log.warn(f"Tunnel [{self._provider}] exited")

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "state":    self._state,
                "url":      self._url,
                "provider": self._provider,
                "error":    self._error,
            }


tunnel_mgr: TunnelManager = None   # set in main()


# ─────────────────────────────────────────────────────────────────────────────
#  RTSP RELAY  (push to a remote RTSP server)
#
#  No local MediaMTX needed — FFmpeg connects directly to the target URL.
#  Works with any RTSP ingest endpoint: NVR, cloud relay, another Pi, etc.
# ─────────────────────────────────────────────────────────────────────────────
class RtspRelay:
    def __init__(self, relay_id: str, target_url: str,
                 fps=30, bitrate_kbps=2000, auto_reconnect=True, resolution="720p"):
        self.relay_id        = relay_id
        self.target_url      = target_url
        self.resolution      = resolution
        self._q              = queue.Queue(maxsize=2)
        self._stop_evt       = threading.Event()
        self._ff_proc        = None
        self._auto_reconnect = auto_reconnect
        self.tx_kbps         = 0.0
        self.state           = "connecting"   # connecting | live | error | stopped
        w, h = RTSP_RES_MAP.get(resolution, (1280, 720))
        self._ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-f", "mjpeg", "-i", "pipe:0",
            "-vf", f"fps={fps},scale={w}:{h}",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", f"{bitrate_kbps}k",
            "-g", str(fps),
            "-rtsp_transport", "tcp",
            "-f", "rtsp", target_url,
        ]
        self._spawn()

    def _spawn(self):
        try:
            self._ff_proc = subprocess.Popen(
                self._ffmpeg_cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
            self.state = "live"
            cam_log.info(f"Relay [{self.relay_id}] started → {self.target_url}")
        except Exception as ex:
            self.state = "error"
            cam_log.error(f"Relay [{self.relay_id}] spawn failed: {ex}")
            return
        threading.Thread(target=self._run,      daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def feed(self, jpeg: bytes):
        try: self._q.put_nowait(jpeg)
        except queue.Full: pass

    def _run(self):
        tx_b = 0; tx_t = time.time()
        while not self._stop_evt.is_set():
            try: frame = self._q.get(timeout=0.5)
            except queue.Empty: continue
            proc = self._ff_proc
            if proc is None or proc.poll() is not None: continue
            try:
                proc.stdin.write(frame)
                tx_b += len(frame)
                now = time.time()
                if now - tx_t >= 1.0:
                    self.tx_kbps = tx_b * 8 / (now - tx_t) / 1000
                    tx_b = 0; tx_t = now
            except (BrokenPipeError, OSError): pass

    def _watchdog(self):
        while not self._stop_evt.is_set():
            time.sleep(3)
            if self._stop_evt.is_set(): break
            if self._ff_proc and self._ff_proc.poll() is not None:
                self.state = "reconnecting"
                cam_log.warn(f"Relay [{self.relay_id}] lost connection")
                if self._auto_reconnect and not self._stop_evt.is_set():
                    self._reconnect()
                else:
                    self.state = "error"

    def _reconnect(self):
        backoff = 3
        while not self._stop_evt.is_set():
            try:
                if self._ff_proc:
                    try: self._ff_proc.stdin.close()
                    except: pass
                self._ff_proc = subprocess.Popen(
                    self._ffmpeg_cmd, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
                self.state = "live"
                cam_log.info(f"Relay [{self.relay_id}] reconnected")
                return
            except Exception as ex:
                cam_log.error(f"Relay [{self.relay_id}] reconnect failed: {ex}")
                for _ in range(backoff):
                    if self._stop_evt.is_set(): return
                    time.sleep(1)
                backoff = min(backoff * 2, 60)

    def stop(self):
        self._stop_evt.set()
        self.state = "stopped"
        if self._ff_proc:
            try: self._ff_proc.stdin.close()
            except: pass
            try: self._ff_proc.terminate(); self._ff_proc.wait(timeout=3)
            except: pass
        cam_log.info(f"Relay [{self.relay_id}] stopped")

    def to_dict(self):
        return {
            "id":         self.relay_id,
            "target_url": self.target_url,
            "resolution": self.resolution,
            "state":      self.state,
            "tx_kbps":    round(self.tx_kbps, 1),
        }


# ─────────────────────────────────────────────────────────────────────────────
#  CAMERA CONTROLLER  (coordinates camera, RTSP, relays, snapshot)
# ─────────────────────────────────────────────────────────────────────────────
class CameraController:
    def __init__(self):
        self._lock        = threading.Lock()
        self._preview_th  = None
        self._local_rtsp  = None
        self._relays      = {}           # relay_id → RtspRelay
        self.mjpeg_bc     = MjpegBroadcaster()
        self._config      = self._load_config()
        self._overlay_feeder = OverlayFeeder(
            self.mjpeg_bc,
            lambda: self._stats,
            lambda: self._config,
        )
        self._stats       = {"fps": 0.0, "jpeg_kb": 0.0}
        self._status      = "Initializing"
        self._snap_busy   = False
        self._cmd_prefix  = get_cmd_prefix()
        self._start_preview()
        self._restore_relays()

    # ── config ────────────────────────────────────────────────────────────────
    def _load_config(self) -> dict:
        import copy
        defaults = copy.deepcopy(CONFIG_DEFAULTS)
        try:
            with open(CONFIG_PATH) as f:
                return {**defaults, **json.load(f)}
        except Exception:
            return defaults

    def save_config(self, patch: dict = None):
        if patch:
            self._config.update(patch)
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(self._config, f, indent=2)
        except Exception as ex:
            cam_log.error(f"Config save failed: {ex}")

    # ── preview ───────────────────────────────────────────────────────────────
    def _stream_wh(self):
        if self._local_rtsp:
            return RTSP_RES_MAP.get(self._config["resolution"], (1280, 720))
        return (1280, 720)

    def _stream_fps(self):
        if self._local_rtsp:
            return max(1, int(self._config["fps"]))
        return 30

    def _build_feeders(self):
        f = [self._overlay_feeder]
        if self._local_rtsp:
            f.append(self._local_rtsp)
        with self._lock:
            f.extend(self._relays.values())
        return f

    def _start_preview(self):
        self._stop_preview()
        w, h = self._stream_wh()
        fps  = self._stream_fps()
        self._preview_th = StreamPreview(
            self._cmd_prefix, w, h, fps,
            feeders=self._build_feeders(),
            on_status=lambda m: setattr(self, "_status", m),
            on_stats=self._on_stats,
            zoom_level=float(self._config.get("zoom_level", 1.0)),
            af_mode=self._config.get("af_mode", "continuous"),
            lens_position=float(self._config.get("lens_position", 0.0)),
            flip=bool(self._config.get("flip", False)),
            mirror=bool(self._config.get("mirror", False)),
            camera_index=int(self._config.get("camera_index", 0)),
        )
        self._preview_th.start()

    def _stop_preview(self):
        if self._preview_th and self._preview_th.is_alive():
            self._preview_th.stop()
            self._preview_th.join(timeout=3)
        self._preview_th = None

    def restart_preview(self):
        """Restart stream (e.g., after resolution change or snapshot)."""
        self._start_preview()

    def _on_stats(self, fps, jpeg_kb):
        self._stats = {"fps": round(fps, 1), "jpeg_kb": round(jpeg_kb, 1)}

    # ── local RTSP ────────────────────────────────────────────────────────────
    def start_local_rtsp(self, resolution=None, fps=None,
                         bitrate=None, auto_reconnect=True):
        if self._local_rtsp:
            return False, "Already running"
        res = resolution or self._config["resolution"]
        f   = int(fps or self._config["fps"])
        b   = int(bitrate or self._config["bitrate"])
        try:
            self._local_rtsp = RtspBroadcaster(
                fps=f, bitrate_kbps=b, auto_reconnect=auto_reconnect,
                on_disconnect=lambda r: cam_log.warn(f"RTSP lost: {r}"),
                on_reconnect=lambda: cam_log.info("RTSP reconnected"),
            )
        except Exception as ex:
            return False, str(ex)
        self._config.update({"resolution": res, "fps": str(f), "bitrate": str(b),
                              "auto_reconnect": auto_reconnect})
        self.save_config()
        self._start_preview()   # restart at RTSP resolution
        return True, self._local_rtsp.rtsp_url

    def stop_local_rtsp(self):
        if not self._local_rtsp:
            return False, "Not running"
        bc = self._local_rtsp
        self._local_rtsp = None
        if self._preview_th:
            self._preview_th.feeders = self._build_feeders()
        bc.stop()
        self._start_preview()   # restart at 720p
        return True, "Stopped"

    # ── relays ────────────────────────────────────────────────────────────────
    def _restore_relays(self):
        """Auto-restore saved relay configs on startup."""
        for rc in self._config.get("saved_relays", []):
            url = (rc.get("target_url") or "").strip()
            if not url:
                continue
            try:
                rid = str(uuid.uuid4())[:8]
                relay = RtspRelay(rid, url,
                                  fps=int(rc.get("fps", 30)),
                                  bitrate_kbps=int(rc.get("bitrate", 2000)),
                                  auto_reconnect=bool(rc.get("auto_reconnect", True)),
                                  resolution=rc.get("resolution", "720p"))
                with self._lock:
                    self._relays[rid] = relay
                cam_log.info(f"Relay [{rid}] restored → {url}")
            except Exception as ex:
                cam_log.error(f"Relay restore failed ({url}): {ex}")
        if self._relays and self._preview_th:
            self._preview_th.feeders = self._build_feeders()

    def add_relay(self, target_url: str, fps=30,
                  bitrate_kbps=2000, auto_reconnect=True, resolution="720p"):
        if resolution not in RTSP_RES_MAP:
            resolution = "720p"
        rid = str(uuid.uuid4())[:8]
        relay = RtspRelay(rid, target_url, fps=fps,
                          bitrate_kbps=bitrate_kbps,
                          auto_reconnect=auto_reconnect,
                          resolution=resolution)
        with self._lock:
            self._relays[rid] = relay
        if self._preview_th:
            self._preview_th.feeders = self._build_feeders()
        # auto-save: add to saved_relays if URL not already stored
        saved = self._config.setdefault("saved_relays", [])
        newly_saved = not any(r.get("target_url") == target_url for r in saved)
        if newly_saved:
            saved.append({
                "target_url":     target_url,
                "fps":            fps,
                "bitrate":        bitrate_kbps,
                "auto_reconnect": auto_reconnect,
                "resolution":     resolution,
            })
            cam_log.info(f"Relay config auto-saved: {target_url} @ {resolution}")
        self._config["last_relay_url"] = target_url
        self.save_config()
        return rid, relay.to_dict(), newly_saved

    def remove_relay(self, relay_id: str):
        with self._lock:
            relay = self._relays.pop(relay_id, None)
        if relay is None:
            return False, "Not found"
        # auto-remove from saved config
        saved = self._config.get("saved_relays", [])
        new_saved = [r for r in saved if r.get("target_url") != relay.target_url]
        if len(new_saved) != len(saved):
            self._config["saved_relays"] = new_saved
            self.save_config()
        relay.stop()
        if self._preview_th:
            self._preview_th.feeders = self._build_feeders()
        return True, "Removed"

    # ── snapshot ──────────────────────────────────────────────────────────────
    def take_snapshot(self, level="mid") -> dict:
        if self._snap_busy:
            return {"ok": False, "error": "Snapshot already in progress"}
        cam_idx = int(self._config.get("camera_index", 0))
        res_map = CAMERA_PROFILES.get(cam_idx, CAMERA_PROFILES[0])["snap_res"]
        w, h   = res_map.get(level, (4608, 2592))
        zoom   = float(self._config.get("zoom_level", 1.0))
        af     = self._config.get("af_mode", "continuous")
        lens   = float(self._config.get("lens_position", 0.0))
        self._snap_busy = True
        self._stop_preview()
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(SNAP_DIR, f"snap_{level}_{ts}.jpg")
        try:
            cam_log.info(f"Snapshot {level}  {w}×{h}  zoom={zoom:.1f}×  af={af}")
            snap_cmd = [
                f"{self._cmd_prefix}-still",
                "--camera", str(cam_idx), "-n",
                "--width", str(w), "--height", str(h),
                "--quality", "95",
                "-o", out,
            ]
            if self._config.get("flip"):   snap_cmd += ["--vflip"]
            if self._config.get("mirror"): snap_cmd += ["--hflip"]
            # apply digital zoom via ROI (same centre-crop as the live preview)
            if zoom > 1.0:
                rw = 1.0 / zoom
                rh = 1.0 / zoom
                rx = (1.0 - rw) / 2.0
                ry = (1.0 - rh) / 2.0
                snap_cmd += ["--roi", f"{rx:.4f},{ry:.4f},{rw:.4f},{rh:.4f}"]
            # autofocus: trigger AF before shutter when not in manual mode
            snap_cmd += ["--autofocus-mode", af]
            if af == "manual":
                snap_cmd += ["--lens-position", str(lens)]
            else:
                snap_cmd += ["--autofocus-on-capture"]
            subprocess.run(snap_cmd, check=True, timeout=40)
            self._burn_overlay(out, level, w, h, zoom, af)
            cam_log.info(f"Snapshot saved: {os.path.basename(out)}")
            result = {"ok": True, "file": os.path.basename(out),
                      "zoom": zoom, "af": af}
        except subprocess.CalledProcessError as ex:
            cam_log.error(f"Snapshot failed code={ex.returncode}")
            result = {"ok": False, "error": f"rpicam-still error {ex.returncode}"}
        except subprocess.TimeoutExpired:
            cam_log.error("Snapshot timed out")
            result = {"ok": False, "error": "Timeout"}
        except Exception as ex:
            cam_log.error(f"Snapshot exception: {ex}")
            result = {"ok": False, "error": str(ex)}
        finally:
            self._snap_busy = False
            self._start_preview()
        return result

    def _burn_overlay(self, path, level, w, h, zoom=1.0, af="auto"):
        try:
            img = Image.open(path).convert("RGBA")
            fs  = max(22, img.width // 52)
            try:
                font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", fs)
            except Exception:
                font = ImageFont.load_default()
            cam_idx  = int(self._config.get("camera_index", 0))
            cam_name = CAMERA_PROFILES.get(cam_idx, {}).get("name", f"CAM{cam_idx}")
            ts       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            zoom_s   = f"  ZOOM {zoom:.1f}×" if zoom > 1.0 else ""
            line1    = f"{cam_name}  {level.upper()}  {w}×{h}{zoom_s}  {ts}"
            line2   = f"AF:{af}" + (f"  ROI {100/zoom:.0f}% of sensor" if zoom > 1.0 else "")
            lh      = fs + 6
            bh      = lh * 2 + 16
            banner  = Image.new("RGBA", (img.width, bh), (0, 0, 0, 170))
            img.paste(banner, (0, img.height - bh), mask=banner.split()[3])
            draw    = ImageDraw.Draw(img)
            draw.text((18, img.height - bh + 8),      line1, font=font, fill="#00e5b0")
            draw.text((18, img.height - bh + 8 + lh), line2, font=font, fill="#a0c0ff")
            img.convert("RGB").save(path, quality=95)
        except Exception as ex:
            cam_log.error(f"Overlay error: {ex}")

    # ── zoom / focus ──────────────────────────────────────────────────────────
    def set_zoom(self, level: float) -> dict:
        level = max(1.0, min(16.0, float(level)))
        self._config["zoom_level"] = level
        self.save_config()
        self._start_preview()
        cam_log.info(f"Zoom → {level:.1f}×")
        return {"ok": True, "zoom_level": level}

    def set_focus(self, mode: str, lens_position: float = None) -> dict:
        if mode not in ("continuous", "auto", "manual"):
            return {"ok": False, "error": "mode must be continuous|auto|manual"}
        self._config["af_mode"] = mode
        if lens_position is not None:
            self._config["lens_position"] = max(0.0, min(32.0, float(lens_position)))
        self.save_config()
        self._start_preview()
        cam_log.info(f"Focus → mode={mode} lens={self._config['lens_position']:.1f}")
        return {"ok": True, "af_mode": mode,
                "lens_position": self._config["lens_position"]}

    def set_transform(self, flip: bool = None, mirror: bool = None) -> dict:
        if flip   is not None: self._config["flip"]   = bool(flip)
        if mirror is not None: self._config["mirror"] = bool(mirror)
        self.save_config()
        self._start_preview()
        cam_log.info(f"Transform → flip={self._config['flip']} mirror={self._config['mirror']}")
        return {"ok": True, "flip": self._config["flip"], "mirror": self._config["mirror"]}

    def set_camera(self, index: int) -> dict:
        index = int(index)
        if index not in CAMERA_PROFILES:
            return {"ok": False, "error": f"camera must be one of {list(CAMERA_PROFILES.keys())}"}
        self._config["camera_index"] = index
        self.save_config()
        self._start_preview()
        name = CAMERA_PROFILES[index]["name"]
        cam_log.info(f"Camera source → {index} ({name})")
        return {"ok": True, "camera_index": index, "camera_name": name}

    # ── status dict ───────────────────────────────────────────────────────────
    def get_status(self) -> dict:
        rtsp_info = None
        if self._local_rtsp:
            rtsp_info = {
                "url":     self._local_rtsp.rtsp_url,
                "tx_kbps": round(self._local_rtsp.tx_kbps, 1),
            }
        with self._lock:
            relays = [r.to_dict() for r in self._relays.values()]
        w, h = self._stream_wh()
        return {
            "status":       self._status,
            "fps":          self._stats["fps"],
            "jpeg_kb":      self._stats["jpeg_kb"],
            "resolution":   f"{w}×{h}",
            "viewers":      self.mjpeg_bc.client_count,
            "local_rtsp":   rtsp_info,
            "relays":       relays,
            "snap_busy":    self._snap_busy,
            "config":        self._config,
            "log":           cam_log.recent(12),
            "lan_ip":        LAN_IP,
            "zoom_level":    self._config.get("zoom_level", 1.0),
            "af_mode":       self._config.get("af_mode", "continuous"),
            "lens_position": self._config.get("lens_position", 0.0),
            "flip":          self._config.get("flip", False),
            "mirror":        self._config.get("mirror", False),
            "camera_index":  self._config.get("camera_index", 0),
            "camera_name":   CAMERA_PROFILES.get(
                                 int(self._config.get("camera_index", 0)), {}
                             ).get("name", "Unknown"),
            "tunnel":        tunnel_mgr.to_dict() if tunnel_mgr else None,
        }

    def shutdown(self):
        cam_log.info("Shutting down")
        for r in list(self._relays.values()):
            r.stop()
        if self._local_rtsp:
            self._local_rtsp.stop()
        self._stop_preview()


# ─────────────────────────────────────────────────────────────────────────────
#  FLASK APPLICATION
# ─────────────────────────────────────────────────────────────────────────────
app        = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("CAMWEB_SECRET", "camweb-rtsp-2025-xk9q")

ctrl:       CameraController = None    # set in main()
tunnel_mgr: TunnelManager    = None    # set in main()
# hw_monitor declared at module level above; populated in main()

# silence Flask's request logging
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH — user store & decorators
# ─────────────────────────────────────────────────────────────────────────────
def _load_users():
    if not os.path.exists(USERS_PATH):
        users = [
            {"username": "admin", "password_hash": generate_password_hash("123456"), "role": "admin"},
            {"username": "user",  "password_hash": generate_password_hash("123456"), "role": "user"},
        ]
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2)
        return users
    with open(USERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_users(users):
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)

def _find_user(username):
    for u in _load_users():
        if u["username"] == username:
            return u
    return None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
            return redirect("/login")
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Forbidden — admin only"}), 403
            return redirect("/")
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/login")
def login_page():
    if "username" in session:
        return redirect("/")
    return render_template("login.html")


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    d        = request.get_json(silent=True) or {}
    username = d.get("username", "").strip()
    password = d.get("password", "")
    user = _find_user(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "error": "Invalid credentials"}), 401
    session["username"] = user["username"]
    session["role"]     = user["role"]
    cam_log.info(f"Login: {username} ({user['role']})")
    return jsonify({"ok": True, "username": user["username"], "role": user["role"]})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    username = session.get("username", "?")
    session.clear()
    cam_log.info(f"Logout: {username}")
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def api_auth_me():
    if "username" not in session:
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    return jsonify({"ok": True, "username": session["username"], "role": session["role"]})


@app.route("/api/auth/users", methods=["GET", "POST"])
@admin_required
def api_auth_users():
    if request.method == "GET":
        users = _load_users()
        return jsonify({"ok": True, "users": [
            {"username": u["username"], "role": u["role"]} for u in users
        ]})
    d            = request.get_json(silent=True) or {}
    username     = d.get("username", "").strip()
    new_password = d.get("password", "").strip()
    new_role     = d.get("role", "").strip()
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    users = _load_users()
    found = False
    for u in users:
        if u["username"] == username:
            if new_password:
                u["password_hash"] = generate_password_hash(new_password)
            if new_role in ("admin", "user"):
                u["role"] = new_role
            found = True
            break
    if not found:
        if not new_password:
            return jsonify({"ok": False, "error": "password required for new user"}), 400
        role = new_role if new_role in ("admin", "user") else "user"
        users.append({"username": username,
                      "password_hash": generate_password_hash(new_password),
                      "role": role})
    _save_users(users)
    cam_log.info(f"User updated: {username} by {session.get('username')}")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
#  CAMERA ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/stream")
@login_required
def stream():
    """MJPEG HTTP stream — works directly in <img src="/stream">."""
    cid, q = ctrl.mjpeg_bc.subscribe()

    def generate():
        try:
            while True:
                try:
                    jpeg = q.get(timeout=5)
                except queue.Empty:
                    continue
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
        except GeneratorExit:
            pass
        finally:
            ctrl.mjpeg_bc.unsubscribe(cid)

    return Response(generate(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
@login_required
def api_status():
    return jsonify(ctrl.get_status())


@app.route("/api/config", methods=["GET", "POST"])
@login_required
def api_config():
    if request.method == "POST":
        if session.get("role") != "admin":
            return jsonify({"ok": False, "error": "Forbidden — admin only"}), 403
        data = request.get_json(silent=True) or {}
        ctrl.save_config(data)
        return jsonify({"ok": True})
    return jsonify(ctrl._config)


@app.route("/api/rtsp/start", methods=["POST"])
@admin_required
def api_rtsp_start():
    d   = request.get_json(silent=True) or {}
    ok, info = ctrl.start_local_rtsp(
        resolution=d.get("resolution"),
        fps=d.get("fps"),
        bitrate=d.get("bitrate"),
        auto_reconnect=d.get("auto_reconnect", True),
    )
    return jsonify({"ok": ok, "url": info if ok else None, "error": None if ok else info})


@app.route("/api/rtsp/stop", methods=["POST"])
@admin_required
def api_rtsp_stop():
    ok, msg = ctrl.stop_local_rtsp()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/relay/add", methods=["POST"])
@admin_required
def api_relay_add():
    d = request.get_json(silent=True) or {}
    url = (d.get("target_url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "target_url required"}), 400
    try:
        fps      = max(1, int(d.get("fps", 30)))
        bitrate  = max(100, int(d.get("bitrate", 2000)))
    except (TypeError, ValueError):
        fps, bitrate = 30, 2000
    resolution = d.get("resolution", "720p")
    if resolution not in RTSP_RES_MAP:
        return jsonify({"ok": False, "error": f"resolution must be one of {list(RTSP_RES_MAP.keys())}"}), 400
    rid, info, newly_saved = ctrl.add_relay(url, fps=fps, bitrate_kbps=bitrate,
                                             auto_reconnect=d.get("auto_reconnect", True),
                                             resolution=resolution)
    return jsonify({"ok": True, "relay": info, "saved": newly_saved})


@app.route("/api/relay/remove/<relay_id>", methods=["DELETE"])
@admin_required
def api_relay_remove(relay_id):
    ok, msg = ctrl.remove_relay(relay_id)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/snapshot", methods=["POST"])
@admin_required
def api_snapshot():
    d     = request.get_json(silent=True) or {}
    level = d.get("level", "mid")
    if level not in ("low", "mid", "high"):
        return jsonify({"ok": False, "error": "level must be low|mid|high"}), 400

    snap_result = {}

    def _run():
        nonlocal snap_result
        snap_result = ctrl.take_snapshot(level)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=45)
    status = ctrl.get_status()
    status["snap_result"] = snap_result
    return jsonify(status)


@app.route("/api/zoom", methods=["POST"])
@admin_required
def api_zoom():
    d = request.get_json(silent=True) or {}
    try:
        level = float(d.get("level", 1.0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "level must be a number"}), 400
    return jsonify(ctrl.set_zoom(level))


@app.route("/api/focus", methods=["POST"])
@admin_required
def api_focus():
    d = request.get_json(silent=True) or {}
    mode = d.get("mode", "continuous")
    lens_pos = d.get("lens_position")
    if lens_pos is not None:
        try:
            lens_pos = float(lens_pos)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "lens_position must be a number"}), 400
    return jsonify(ctrl.set_focus(mode, lens_pos))


@app.route("/api/transform", methods=["POST"])
@admin_required
def api_transform():
    d = request.get_json(silent=True) or {}
    flip   = d.get("flip")
    mirror = d.get("mirror")
    if flip   is not None: flip   = bool(flip)
    if mirror is not None: mirror = bool(mirror)
    return jsonify(ctrl.set_transform(flip=flip, mirror=mirror))


@app.route("/api/camera", methods=["POST"])
@admin_required
def api_camera():
    d = request.get_json(silent=True) or {}
    try:
        index = int(d.get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "index must be an integer"}), 400
    return jsonify(ctrl.set_camera(index))


@app.route("/api/log/file")
@login_required
def api_log_file():
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return jsonify({"ok": True, "lines": lines[-100:]})
    except FileNotFoundError:
        return jsonify({"ok": True, "lines": []})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


@app.route("/api/log/clear", methods=["POST"])
@admin_required
def api_log_clear():
    try:
        open(LOG_PATH, "w").close()
        with cam_log._lock:
            cam_log._entries.clear()
        cam_log.info("Log cleared")
        return jsonify({"ok": True})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


@app.route("/api/hardware")
@login_required
def api_hardware():
    return jsonify(hw_monitor.get_stats() if hw_monitor else {})


@app.route("/api/config/defaults")
@login_required
def api_config_defaults():
    import copy
    d = copy.deepcopy(CONFIG_DEFAULTS)
    d.pop("saved_relays", None)
    return jsonify(d)


@app.route("/api/config/reset", methods=["POST"])
@admin_required
def api_config_reset():
    import copy
    d = copy.deepcopy(CONFIG_DEFAULTS)
    d["saved_relays"] = ctrl._config.get("saved_relays", [])
    ctrl.save_config(d)
    ctrl._start_preview()
    cam_log.info("Config reset to defaults")
    return jsonify({"ok": True, "config": ctrl._config})


@app.route("/api/tunnel/start", methods=["POST"])
@admin_required
def api_tunnel_start():
    d        = request.get_json(silent=True) or {}
    provider = d.get("provider", "localhost.run")
    return jsonify(tunnel_mgr.start(provider))


@app.route("/api/tunnel/stop", methods=["POST"])
@admin_required
def api_tunnel_stop():
    return jsonify(tunnel_mgr.stop())


@app.route("/api/tunnel/providers")
@login_required
def api_tunnel_providers():
    return jsonify({"providers": TunnelManager.available_providers()})


@app.route("/snaps/<path:filename>")
@login_required
def serve_snap(filename):
    return send_from_directory(SNAP_DIR, filename)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT  (HTML served from templates/index.html)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ctrl       = CameraController()
    hw_monitor = HardwareMonitor(get_config=lambda: ctrl._config)
    tunnel_mgr = TunnelManager(port=WEB_PORT)

    if ctrl._config.get("autostart"):
        threading.Timer(2.0, lambda: ctrl.start_local_rtsp()).start()

    print(f"\n  Arducam 64MP · Web Controller")
    print(f"  ─────────────────────────────────────────")
    print(f"  Local:   http://localhost:{WEB_PORT}")
    print(f"  Network: http://{LAN_IP}:{WEB_PORT}")
    print(f"  MJPEG:   http://{LAN_IP}:{WEB_PORT}/stream")
    print(f"  ─────────────────────────────────────────\n")
    cam_log.info(f"Web UI at http://{LAN_IP}:{WEB_PORT}")

    try:
        app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel_mgr:
            tunnel_mgr.stop()
        ctrl.shutdown()
        if hw_monitor:
            hw_monitor.stop()
