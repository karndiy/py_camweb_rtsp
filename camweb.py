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
from PIL import Image, ImageDraw, ImageFont

try:
    from flask import Flask, Response, request, jsonify, send_from_directory
except ImportError:
    print("Run first:  pip3 install flask pillow")
    raise SystemExit(1)

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
_BASE       = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BASE, "camtest_config.json")   # shared with camtest5.py
LOG_PATH    = os.path.join(_BASE, "cam_events.log")
SNAP_DIR    = _BASE

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
#  RTSP RELAY  (push to a remote RTSP server)
#
#  No local MediaMTX needed — FFmpeg connects directly to the target URL.
#  Works with any RTSP ingest endpoint: NVR, cloud relay, another Pi, etc.
# ─────────────────────────────────────────────────────────────────────────────
class RtspRelay:
    def __init__(self, relay_id: str, target_url: str,
                 fps=30, bitrate_kbps=2000, auto_reconnect=True):
        self.relay_id        = relay_id
        self.target_url      = target_url
        self._q              = queue.Queue(maxsize=2)
        self._stop_evt       = threading.Event()
        self._ff_proc        = None
        self._auto_reconnect = auto_reconnect
        self.tx_kbps         = 0.0
        self.state           = "connecting"   # connecting | live | error | stopped
        self._ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-f", "mjpeg", "-i", "pipe:0",
            "-vf", f"fps={fps}",
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
        defaults = {
            "autostart":      False,
            "resolution":     "720p",
            "fps":            "30",
            "bitrate":        "2000",
            "show_preview":   True,
            "auto_reconnect": True,
            "show_overlay":   True,
            "zoom_level":     1.0,
            "af_mode":        "continuous",
            "lens_position":  0.0,
            "flip":           False,
            "mirror":         False,
            "camera_index":   0,
            "saved_relays":   [],  # [{target_url, fps, bitrate, auto_reconnect}]
        }
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
                                  auto_reconnect=bool(rc.get("auto_reconnect", True)))
                with self._lock:
                    self._relays[rid] = relay
                cam_log.info(f"Relay [{rid}] restored → {url}")
            except Exception as ex:
                cam_log.error(f"Relay restore failed ({url}): {ex}")
        if self._relays and self._preview_th:
            self._preview_th.feeders = self._build_feeders()

    def add_relay(self, target_url: str, fps=30,
                  bitrate_kbps=2000, auto_reconnect=True):
        rid = str(uuid.uuid4())[:8]
        relay = RtspRelay(rid, target_url, fps=fps,
                          bitrate_kbps=bitrate_kbps,
                          auto_reconnect=auto_reconnect)
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
            })
            self.save_config()
            cam_log.info(f"Relay config auto-saved: {target_url}")
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
app  = Flask(__name__)
ctrl: CameraController = None    # set in main()

# silence Flask's request logging
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)


@app.route("/")
def index():
    return Response(HTML_TEMPLATE, mimetype="text/html")


@app.route("/stream")
def stream():
    """MJPEG HTTP stream — works directly in <img src="/stream">."""
    cid, q = ctrl.mjpeg_bc.subscribe()

    def generate():
        try:
            while True:
                try:
                    jpeg = q.get(timeout=5)
                except queue.Empty:
                    # keep-alive: send current frame again or wait
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
def api_status():
    return jsonify(ctrl.get_status())


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        ctrl.save_config(data)
        return jsonify({"ok": True})
    return jsonify(ctrl._config)


@app.route("/api/rtsp/start", methods=["POST"])
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
def api_rtsp_stop():
    ok, msg = ctrl.stop_local_rtsp()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/relay/add", methods=["POST"])
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
    rid, info, newly_saved = ctrl.add_relay(url, fps=fps, bitrate_kbps=bitrate,
                                             auto_reconnect=d.get("auto_reconnect", True))
    return jsonify({"ok": True, "relay": info, "saved": newly_saved})


@app.route("/api/relay/remove/<relay_id>", methods=["DELETE"])
def api_relay_remove(relay_id):
    ok, msg = ctrl.remove_relay(relay_id)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/snapshot", methods=["POST"])
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
def api_zoom():
    d = request.get_json(silent=True) or {}
    try:
        level = float(d.get("level", 1.0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "level must be a number"}), 400
    return jsonify(ctrl.set_zoom(level))


@app.route("/api/focus", methods=["POST"])
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
def api_transform():
    d = request.get_json(silent=True) or {}
    flip   = d.get("flip")
    mirror = d.get("mirror")
    if flip   is not None: flip   = bool(flip)
    if mirror is not None: mirror = bool(mirror)
    return jsonify(ctrl.set_transform(flip=flip, mirror=mirror))


@app.route("/api/camera", methods=["POST"])
def api_camera():
    d = request.get_json(silent=True) or {}
    try:
        index = int(d.get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "index must be an integer"}), 400
    return jsonify(ctrl.set_camera(index))


@app.route("/snaps/<path:filename>")
def serve_snap(filename):
    return send_from_directory(SNAP_DIR, filename)


# ─────────────────────────────────────────────────────────────────────────────
#  WEB UI  (inline HTML — no separate templates/ directory needed)
# ─────────────────────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arducam 64MP · Web Controller</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f14;color:#e8eaf0;font-family:'Courier New',monospace;font-size:13px;height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{background:#13161d;padding:0 20px;height:48px;display:flex;align-items:center;gap:16px;flex-shrink:0;border-bottom:1px solid #1e2230}
header h1{color:#00e5b0;font-size:16px;letter-spacing:2px}
header span{color:#5a6070;font-size:11px}
#clock{margin-left:auto;color:#5a6070;font-size:11px}
#main{display:flex;flex:1;overflow:hidden}
#preview-wrap{flex:1;background:#000;position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center}
#stream-img{max-width:100%;max-height:100%;object-fit:contain;display:block}
.badge{position:absolute;background:#000;padding:3px 8px;font-size:11px;border-radius:3px}
#badge-tl{top:10px;left:10px;color:#00e5b0}
#badge-tr{top:10px;right:10px;color:#5a6070}
#badge-br{bottom:10px;right:10px;color:#5a6070}
#sidebar{width:300px;background:#13161d;overflow-y:auto;flex-shrink:0;border-left:1px solid #1e2230;padding-bottom:16px}
.section{padding:14px 16px 0}
.sec-title{font-size:10px;font-weight:bold;color:#5a6070;letter-spacing:1.5px;margin-bottom:8px}
.sep{height:1px;background:#1e2230;margin:12px 16px}
.btn{display:block;width:100%;padding:10px;border:none;border-radius:4px;font-family:inherit;font-size:12px;font-weight:bold;cursor:pointer;letter-spacing:.5px;transition:filter .15s}
.btn:hover{filter:brightness(1.15)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-rtsp-off{background:#1e2230;color:#5a6070}
.btn-rtsp-on{background:#007a3d;color:#fff}
.btn-blue{background:#0077ff;color:#fff}
.btn-danger{background:#7a1a1a;color:#ffa0a0}
.url-lbl{color:#00e5b0;font-size:11px;margin-top:6px;word-break:break-all;min-height:16px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
label{color:#e8eaf0;font-size:12px}
.lbl-sm{color:#5a6070;font-size:11px;margin-bottom:4px}
input[type=text],input[type=number]{background:#19212e;border:1px solid #2a3248;color:#e8eaf0;padding:5px 8px;border-radius:3px;font-family:inherit;font-size:12px;width:100%}
input[type=number]{width:80px}
select{background:#19212e;border:1px solid #2a3248;color:#e8eaf0;padding:5px 8px;border-radius:3px;font-family:inherit;font-size:12px}
.radio-group{display:flex;gap:6px;flex-wrap:wrap}
.radio-group label{display:flex;align-items:center;gap:4px;cursor:pointer;padding:3px 7px;border-radius:3px;border:1px solid #2a3248;transition:border-color .15s}
.radio-group label:hover{border-color:#00e5b0}
.radio-group input[type=radio]{accent-color:#00e5b0}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 8px}
.stat-item{background:#0d0f14;border-radius:3px;padding:6px 8px}
.stat-val{font-size:15px;color:#00e5b0;font-weight:bold}
.stat-key{font-size:10px;color:#5a6070;margin-top:1px}
#log-box{background:#080a0e;padding:8px;border-radius:4px;margin-top:4px;height:150px;overflow-y:auto;font-size:10px;line-height:1.6}
.log-INF{color:#5a6070}
.log-WRN{color:#ff9800}
.log-ERR{color:#ff3333}
.relay-item{background:#0d0f14;border-radius:4px;padding:8px 10px;margin-bottom:6px;position:relative}
.relay-url{color:#e8eaf0;font-size:11px;word-break:break-all}
.relay-state{font-size:10px;margin-top:3px}
.relay-state.live{color:#00e5b0}
.relay-state.reconnecting{color:#ff9800}
.relay-state.error{color:#ff3333}
.relay-state.connecting{color:#5a6070}
.relay-del{position:absolute;top:6px;right:8px;background:none;border:none;color:#5a6070;cursor:pointer;font-size:14px;line-height:1}
.relay-del:hover{color:#ff3333}
.snap-row{display:flex;gap:6px;align-items:center}
.chip{padding:3px 8px;border-radius:3px;background:#19212e;border:1px solid #2a3248;color:#e8eaf0;font-family:inherit;font-size:11px;cursor:pointer}
.chip.active,.chip:hover{background:#00e5b0;color:#000;border-color:#00e5b0}
.viewers-badge{display:inline-block;background:#19212e;padding:2px 8px;border-radius:10px;font-size:10px;color:#5a6070;margin-left:6px}
.zoom-btn{padding:4px 10px;background:#19212e;border:1px solid #2a3248;color:#e8eaf0;border-radius:3px;font-family:inherit;font-size:11px;cursor:pointer;transition:all .15s}
.zoom-btn.active,.zoom-btn:hover{background:#00e5b0;color:#000;border-color:#00e5b0}
.focus-btn{padding:4px 10px;background:#19212e;border:1px solid #2a3248;color:#e8eaf0;border-radius:3px;font-family:inherit;font-size:11px;cursor:pointer;transition:all .15s}
.focus-btn.active{background:#0077ff;color:#fff;border-color:#0077ff}
.focus-btn:hover{border-color:#0077ff}
input[type=range]{width:100%;accent-color:#00e5b0;cursor:pointer;margin-top:4px}
.slider-val{color:#00e5b0;font-size:12px;font-weight:bold}
.toggle-btn{padding:6px 14px;background:#19212e;border:1px solid #2a3248;color:#e8eaf0;border-radius:3px;font-family:inherit;font-size:12px;font-weight:bold;cursor:pointer;letter-spacing:.5px;transition:all .15s;flex:1}
.toggle-btn.active{background:#00e5b0;color:#000;border-color:#00e5b0}
.toggle-btn:hover{border-color:#00e5b0}
</style>
</head>
<body>
<header>
  <h1>ARDUCAM 64MP</h1>
  <span>WEB CONTROLLER</span>
  <span id="viewers-info" class="viewers-badge">0 viewers</span>
  <span id="clock">--:--:--</span>
</header>

<div id="main">
  <!-- ── MJPEG preview ── -->
  <div id="preview-wrap">
    <img id="stream-img" src="/stream" alt="stream"
         onerror="setTimeout(()=>{this.src='/stream?t='+Date.now()},2000)">
    <div class="badge" id="badge-tl">MJPEG  720×480</div>
    <div class="badge" id="badge-tr"></div>
    <div class="badge" id="badge-br"></div>
  </div>

  <!-- ── Sidebar ── -->
  <aside id="sidebar">

    <!-- Camera Source -->
    <div class="section" style="padding-top:16px">
      <div class="sec-title">CAMERA SOURCE</div>
      <div style="display:flex;gap:6px">
        <button class="toggle-btn active" id="cam-src-0" onclick="setCamera(0)">CAM 0  Arducam 64MP</button>
        <button class="toggle-btn" id="cam-src-1" onclick="setCamera(1)">CAM 1  Module 3</button>
      </div>
      <div id="cam-name-lbl" style="color:#5a6070;font-size:10px;margin-top:4px"></div>
    </div>

    <div class="sep"></div>

    <!-- Status stats -->
    <div class="section">
      <div class="sec-title">LIVE STATS</div>
      <div class="stat-grid">
        <div class="stat-item"><div class="stat-val" id="st-fps">—</div><div class="stat-key">FPS</div></div>
        <div class="stat-item"><div class="stat-val" id="st-jpeg">—</div><div class="stat-key">JPEG kB</div></div>
        <div class="stat-item"><div class="stat-val" id="st-res">—</div><div class="stat-key">Resolution</div></div>
        <div class="stat-item"><div class="stat-val" id="st-tx">—</div><div class="stat-key">RTSP TX kbps</div></div>
      </div>
    </div>

    <div class="sep"></div>

    <!-- Local RTSP -->
    <div class="section">
      <div class="sec-title">LOCAL RTSP STREAM</div>
      <button id="btn-rtsp" class="btn btn-rtsp-off" onclick="toggleRtsp()">◉  START RTSP</button>
      <div class="url-lbl" id="rtsp-url"></div>

      <div style="margin-top:12px">
        <div class="lbl-sm">RESOLUTION</div>
        <div class="radio-group" id="rg-res">
          <label><input type="radio" name="res" value="720p" checked> 720p</label>
          <label><input type="radio" name="res" value="1080p"> 1080p</label>
          <label><input type="radio" name="res" value="2160p"> 2160p</label>
        </div>
      </div>
      <div style="margin-top:8px">
        <div class="lbl-sm">FPS</div>
        <div class="radio-group" id="rg-fps">
          <label><input type="radio" name="fps" value="5"> 5</label>
          <label><input type="radio" name="fps" value="10"> 10</label>
          <label><input type="radio" name="fps" value="15"> 15</label>
          <label><input type="radio" name="fps" value="30" checked> 30</label>
        </div>
      </div>
      <div class="row" style="margin-top:8px">
        <div>
          <div class="lbl-sm">BITRATE (kbps)</div>
          <input type="number" id="inp-bitrate" value="2000" min="100" max="20000" style="width:90px">
        </div>
        <div>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;margin-top:16px">
            <input type="checkbox" id="chk-reconnect" checked style="accent-color:#00e5b0">
            Auto-reconnect
          </label>
        </div>
      </div>
    </div>

    <div class="sep"></div>

    <!-- RTSP Relay -->
    <div class="section">
      <div class="sec-title">RTSP RELAY  <span style="font-size:9px;font-weight:normal">(push to remote server)</span></div>

      <div style="margin-bottom:6px">
        <div class="lbl-sm">TARGET URL</div>
        <input type="text" id="relay-url" placeholder="rtsp://192.168.1.100:554/live">
      </div>
      <div style="margin-bottom:6px">
        <div class="lbl-sm">SAVED CONFIGS</div>
        <select id="saved-relay-select" style="width:100%" onchange="loadSavedRelay(this.value)">
          <option value="">— load saved —</option>
        </select>
      </div>
      <div class="row">
        <div>
          <div class="lbl-sm">FPS</div>
          <select id="relay-fps">
            <option value="5">5</option>
            <option value="10">10</option>
            <option value="15">15</option>
            <option value="30" selected>30</option>
          </select>
        </div>
        <div>
          <div class="lbl-sm">BITRATE (kbps)</div>
          <input type="number" id="relay-bitrate" value="2000" min="100" max="20000" style="width:90px">
        </div>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;align-self:flex-end;padding-bottom:2px">
          <input type="checkbox" id="relay-reconnect" checked style="accent-color:#00e5b0">
          Auto
        </label>
      </div>
      <button class="btn btn-blue" style="margin-top:4px" onclick="addRelay()">+ ADD RELAY</button>
      <div id="relay-save-status" style="color:#00e5b0;font-size:10px;min-height:14px;margin-top:4px"></div>

      <div id="relay-list" style="margin-top:6px"></div>
    </div>

    <div class="sep"></div>

    <!-- Snapshot -->
    <div class="section">
      <div class="sec-title">SNAPSHOT</div>
      <div id="snap-status" style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">
        <span id="snap-zoom-pill" style="display:none;padding:2px 8px;border-radius:10px;background:#00e5b020;border:1px solid #00e5b0;color:#00e5b0;font-size:10px"></span>
        <span id="snap-af-pill"   style="padding:2px 8px;border-radius:10px;background:#0077ff20;border:1px solid #0077ff;color:#a0c0ff;font-size:10px">AF:—</span>
      </div>
      <div class="snap-row">
        <span class="chip active" id="snap-low"  onclick="setSnapLevel('low')">LOW 720p</span>
        <span class="chip"        id="snap-mid"  onclick="setSnapLevel('mid')">MED 12MP</span>
        <span class="chip"        id="snap-high" onclick="setSnapLevel('high')">HIGH 64MP</span>
      </div>
      <button class="btn btn-blue" style="margin-top:8px" id="btn-snap" onclick="takeSnapshot()">● CAPTURE</button>
      <div id="snap-result" style="color:#00e5b0;font-size:11px;margin-top:4px;min-height:14px"></div>
    </div>

    <div class="sep"></div>

    <!-- Digital Zoom -->
    <div class="section">
      <div class="sec-title">DIGITAL ZOOM  <span class="slider-val" id="zoom-badge">1.0×</span></div>
      <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px" id="zoom-presets">
        <button class="zoom-btn active" onclick="setZoom(1)">1×</button>
        <button class="zoom-btn" onclick="setZoom(1.5)">1.5×</button>
        <button class="zoom-btn" onclick="setZoom(2)">2×</button>
        <button class="zoom-btn" onclick="setZoom(3)">3×</button>
        <button class="zoom-btn" onclick="setZoom(4)">4×</button>
        <button class="zoom-btn" onclick="setZoom(6)">6×</button>
        <button class="zoom-btn" onclick="setZoom(8)">8×</button>
      </div>
      <div class="lbl-sm">FINE ADJUST  <span class="slider-val" id="zoom-slider-val">1.0×</span></div>
      <input type="range" id="zoom-slider" min="10" max="160" value="10" step="1"
             oninput="previewZoomSlider(this.value)" onchange="commitZoomSlider(this.value)">
    </div>

    <div class="sep"></div>

    <!-- Autofocus -->
    <div class="section">
      <div class="sec-title">AUTOFOCUS</div>
      <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px">
        <button class="focus-btn active" id="af-continuous" onclick="setAfMode('continuous')">CONTINUOUS</button>
        <button class="focus-btn" id="af-auto" onclick="setAfMode('auto')">AUTO</button>
        <button class="focus-btn" id="af-manual" onclick="setAfMode('manual')">MANUAL</button>
      </div>
      <button class="btn btn-blue" id="btn-trigger-af" style="display:none;margin-bottom:8px" onclick="triggerAF()">⊙ TRIGGER AF</button>
      <div id="lens-pos-wrap" style="display:none">
        <div class="lbl-sm">LENS POSITION  <span class="slider-val" id="lens-val">0.0</span></div>
        <input type="range" id="lens-slider" min="0" max="100" value="0" step="1"
               oninput="previewLensSlider(this.value)" onchange="commitLensSlider(this.value)">
        <div style="display:flex;justify-content:space-between;font-size:10px;color:#5a6070;margin-top:2px"><span>∞ Far</span><span>Near ◎</span></div>
      </div>
    </div>

    <div class="sep"></div>

    <!-- Flip / Mirror / Overlay -->
    <div class="section">
      <div class="sec-title">IMAGE TRANSFORM</div>
      <div style="display:flex;gap:8px">
        <button class="toggle-btn" id="btn-flip"   onclick="toggleFlip()">&#8597; FLIP</button>
        <button class="toggle-btn" id="btn-mirror" onclick="toggleMirror()">&#8596; MIRROR</button>
      </div>
      <div style="margin-top:8px">
        <button class="toggle-btn" id="btn-overlay" onclick="toggleOverlay()" style="width:100%">&#9432; INFO OVERLAY</button>
      </div>
    </div>

    <div class="sep"></div>

    <!-- Event log -->
    <div class="section" style="padding-bottom:8px">
      <div class="sec-title">EVENT LOG</div>
      <div id="log-box"></div>
    </div>

  </aside>
</div>

<script>
// ── state ────────────────────────────────────────────────────────────────────
let rtspActive   = false;
let snapLevel    = 'low';
let snapBusy     = false;
let _savedRelays = [];

// ── clock ────────────────────────────────────────────────────────────────────
function tickClock() {
  const n = new Date();
  document.getElementById('clock').textContent =
    n.toISOString().replace('T',' ').slice(0,19);
  setTimeout(tickClock, 1000);
}
tickClock();

// ── status poll ──────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    updateUI(d);
  } catch(e) {}
  setTimeout(pollStatus, 2000);
}

function updateUI(d) {
  // stats
  document.getElementById('st-fps').textContent  = d.fps;
  document.getElementById('st-jpeg').textContent = d.jpeg_kb;
  document.getElementById('st-res').textContent  = d.resolution || '—';
  document.getElementById('st-tx').textContent   =
    d.local_rtsp ? d.local_rtsp.tx_kbps : '—';
  document.getElementById('viewers-info').textContent =
    d.viewers + ' viewer' + (d.viewers !== 1 ? 's' : '');

  // RTSP button
  rtspActive = !!d.local_rtsp;
  const btn = document.getElementById('btn-rtsp');
  if (rtspActive) {
    btn.textContent = '◉  STOP RTSP';
    btn.className   = 'btn btn-rtsp-on';
    document.getElementById('rtsp-url').textContent = d.local_rtsp.url;
  } else {
    btn.textContent = '◉  START RTSP';
    btn.className   = 'btn btn-rtsp-off';
    document.getElementById('rtsp-url').textContent = '';
  }

  // overlay badges
  document.getElementById('badge-tl').textContent =
    (d.local_rtsp ? 'RTSP' : 'MJPEG') + '  ' + (d.resolution || '');
  document.getElementById('badge-tr').textContent =
    d.fps + ' fps';
  document.getElementById('badge-br').textContent =
    d.jpeg_kb + ' kB / frame';

  // snapshot zoom / AF status pills
  const zl = parseFloat(d.zoom_level) || 1.0;
  const zoomPill = document.getElementById('snap-zoom-pill');
  if (zl > 1.0) {
    zoomPill.textContent = `ZOOM ${zl.toFixed(1)}×`;
    zoomPill.style.display = 'inline';
  } else {
    zoomPill.style.display = 'none';
  }
  document.getElementById('snap-af-pill').textContent = 'AF:' + (d.af_mode || '—');

  // camera source
  const camIdx = d.camera_index ?? 0;
  [0, 1].forEach(i =>
    document.getElementById('cam-src-' + i)?.classList.toggle('active', i === camIdx));
  const lbl = document.getElementById('cam-name-lbl');
  if (lbl) lbl.textContent = d.camera_name || '';

  // relays
  renderRelays(d.relays || []);
  updateSavedRelays(d.config && d.config.saved_relays);

  // snapshot busy
  snapBusy = d.snap_busy;
  document.getElementById('btn-snap').disabled = snapBusy;

  // log
  renderLog(d.log || []);

  // apply saved config to form (once on first load if stored)
  if (d.config && !window._cfgApplied) {
    window._cfgApplied = true;
    applyConfig(d.config);
  }
}

function applyConfig(c) {
  const rRes = document.querySelectorAll('input[name=res]');
  rRes.forEach(r => r.checked = (r.value === c.resolution));
  const rFps = document.querySelectorAll('input[name=fps]');
  rFps.forEach(r => r.checked = (r.value === c.fps));
  document.getElementById('inp-bitrate').value = c.bitrate || '2000';
  document.getElementById('chk-reconnect').checked = !!c.auto_reconnect;
  // zoom
  const zl = parseFloat(c.zoom_level) || 1.0;
  currentZoom = zl;
  document.getElementById('zoom-slider').value = Math.round(zl * 10);
  previewZoomSlider(Math.round(zl * 10));
  // focus
  if (c.af_mode) {
    currentAfMode = c.af_mode;
    ['continuous','auto','manual'].forEach(m =>
      document.getElementById('af-'+m)?.classList.toggle('active', m===c.af_mode));
    document.getElementById('btn-trigger-af').style.display = c.af_mode==='auto' ? 'block':'none';
    document.getElementById('lens-pos-wrap').style.display  = c.af_mode==='manual' ? 'block':'none';
    if (c.af_mode === 'manual' && c.lens_position !== undefined) {
      const sv = Math.round(parseFloat(c.lens_position) * 10);
      document.getElementById('lens-slider').value = sv;
      document.getElementById('lens-val').textContent = parseFloat(c.lens_position).toFixed(1);
    }
  }
  // camera source
  if (c.camera_index !== undefined) {
    [0, 1].forEach(i =>
      document.getElementById('cam-src-' + i)?.classList.toggle('active', i === c.camera_index));
  }
  // flip / mirror / overlay
  flipActive    = !!c.flip;
  mirrorActive  = !!c.mirror;
  overlayActive = !!c.show_overlay;
  document.getElementById('btn-flip').classList.toggle('active', flipActive);
  document.getElementById('btn-mirror').classList.toggle('active', mirrorActive);
  document.getElementById('btn-overlay').classList.toggle('active', overlayActive);
}

// ── RTSP toggle ──────────────────────────────────────────────────────────────
async function toggleRtsp() {
  const btn = document.getElementById('btn-rtsp');
  btn.disabled = true;
  if (!rtspActive) {
    const res = document.querySelector('input[name=res]:checked')?.value || '720p';
    const fps = document.querySelector('input[name=fps]:checked')?.value || '30';
    const bit = document.getElementById('inp-bitrate').value || '2000';
    const arc = document.getElementById('chk-reconnect').checked;
    btn.textContent = '◉  STARTING…';
    const r = await fetch('/api/rtsp/start', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({resolution:res, fps:fps, bitrate:bit, auto_reconnect:arc})
    });
    const d = await r.json();
    if (!d.ok) alert('RTSP start failed: ' + d.error);
  } else {
    btn.textContent = '◉  STOPPING…';
    await fetch('/api/rtsp/stop', {method:'POST'});
  }
  btn.disabled = false;
}

// ── saved relay helpers ───────────────────────────────────────────────────────
function updateSavedRelays(saved) {
  _savedRelays = saved || [];
  const sel = document.getElementById('saved-relay-select');
  const prev = sel.value;
  sel.innerHTML = '<option value="">— load saved —</option>';
  _savedRelays.forEach((r, i) => {
    const opt = document.createElement('option');
    opt.value = String(i);
    const fps = r.fps || 30;
    const bit = r.bitrate || 2000;
    opt.textContent = `${r.target_url}  [${fps}fps  ${bit}kbps]`;
    sel.appendChild(opt);
  });
  if (prev && sel.querySelector(`option[value="${prev}"]`)) sel.value = prev;
}

function loadSavedRelay(idx) {
  if (idx === '') return;
  const r = _savedRelays[parseInt(idx)];
  if (!r) return;
  document.getElementById('relay-url').value         = r.target_url || '';
  document.getElementById('relay-fps').value         = String(r.fps || 30);
  document.getElementById('relay-bitrate').value     = r.bitrate || 2000;
  document.getElementById('relay-reconnect').checked = !!r.auto_reconnect;
  document.getElementById('saved-relay-select').value = '';
}

// ── relay ────────────────────────────────────────────────────────────────────
async function addRelay() {
  const url = document.getElementById('relay-url').value.trim();
  if (!url) { alert('Enter target RTSP URL'); return; }
  const fps = parseInt(document.getElementById('relay-fps').value);
  const bit = parseInt(document.getElementById('relay-bitrate').value) || 2000;
  const arc = document.getElementById('relay-reconnect').checked;
  const r = await fetch('/api/relay/add', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({target_url:url, fps:fps, bitrate:bit, auto_reconnect:arc})
  });
  const d = await r.json();
  if (d.ok) {
    document.getElementById('relay-url').value = '';
    if (d.saved) {
      const ss = document.getElementById('relay-save-status');
      ss.textContent = '✓ Config auto-saved to JSON';
      setTimeout(() => { ss.textContent = ''; }, 3000);
    }
  } else {
    alert('Relay failed: ' + d.error);
  }
}

async function removeRelay(id) {
  await fetch('/api/relay/remove/' + id, {method:'DELETE'});
}

function renderRelays(relays) {
  const box = document.getElementById('relay-list');
  box.innerHTML = '';
  if (!relays.length) {
    box.innerHTML = '<div style="color:#5a6070;font-size:11px">No active relays</div>';
    return;
  }
  relays.forEach(r => {
    const d = document.createElement('div');
    d.className = 'relay-item';
    d.innerHTML = `
      <div class="relay-url">${r.target_url}</div>
      <div class="relay-state ${r.state}">${r.state}  ${r.state==='live'?r.tx_kbps+' kbps':''}</div>
      <button class="relay-del" onclick="removeRelay('${r.id}')" title="Remove">✕</button>`;
    box.appendChild(d);
  });
}

// ── snapshot ─────────────────────────────────────────────────────────────────
function setSnapLevel(l) {
  snapLevel = l;
  ['low','mid','high'].forEach(k => {
    document.getElementById('snap-'+k).classList.toggle('active', k===l);
  });
}

async function takeSnapshot() {
  if (snapBusy) return;
  document.getElementById('btn-snap').disabled = true;
  document.getElementById('snap-result').textContent = 'Capturing…';
  const r = await fetch('/api/snapshot', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({level:snapLevel})
  });
  const d = await r.json();
  const sr = d.snap_result || {};
  const res = document.getElementById('snap-result');
  if (sr.ok && sr.file) {
    const zoomStr = sr.zoom > 1.001
      ? ` <span style="color:#a0c0ff">ZOOM ${parseFloat(sr.zoom).toFixed(1)}× · AF:${sr.af}</span>`
      : ` <span style="color:#5a6070">AF:${sr.af}</span>`;
    res.innerHTML = `✓ <a href="/snaps/${sr.file}" style="color:#00e5b0" target="_blank">${sr.file}</a>${zoomStr}`;
  } else if (sr.ok === false && sr.error) {
    res.textContent = '✗ ' + sr.error;
  } else {
    const log0 = (d.log || []).find(e => e.msg.includes('snap_'));
    const fname = log0 ? (log0.msg.match(/snap_\S+\.jpg/)?.[0] || '') : '';
    res.innerHTML = fname
      ? `✓ <a href="/snaps/${fname}" style="color:#00e5b0" target="_blank">${fname}</a>`
      : '✓ Done';
  }
}

// ── zoom ─────────────────────────────────────────────────────────────────────
let currentZoom = 1.0;
const ZOOM_PRESETS = [1, 1.5, 2, 3, 4, 6, 8];

function previewZoomSlider(val) {
  const z = (parseInt(val) / 10).toFixed(1);
  document.getElementById('zoom-slider-val').textContent = z + '×';
  document.getElementById('zoom-badge').textContent = z + '×';
  ZOOM_PRESETS.forEach((p, i) => {
    document.querySelectorAll('#zoom-presets .zoom-btn')[i]
      ?.classList.toggle('active', Math.abs(p - parseFloat(z)) < 0.05);
  });
}

function commitZoomSlider(val) { applyZoom(parseInt(val) / 10); }

async function setZoom(level) {
  document.getElementById('zoom-slider').value = Math.round(level * 10);
  previewZoomSlider(Math.round(level * 10));
  await applyZoom(level);
}

async function applyZoom(level) {
  currentZoom = level;
  try {
    await fetch('/api/zoom', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({level})});
  } catch(e) {}
}

// ── autofocus ─────────────────────────────────────────────────────────────────
let currentAfMode = 'continuous';

async function setAfMode(mode) {
  currentAfMode = mode;
  ['continuous','auto','manual'].forEach(m =>
    document.getElementById('af-'+m).classList.toggle('active', m===mode));
  document.getElementById('btn-trigger-af').style.display = mode==='auto'   ? 'block':'none';
  document.getElementById('lens-pos-wrap').style.display  = mode==='manual' ? 'block':'none';
  if (mode !== 'manual') {
    try {
      await fetch('/api/focus', {method:'POST',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode})});
    } catch(e) {}
  }
}

function previewLensSlider(val) {
  document.getElementById('lens-val').textContent = (parseInt(val)/10).toFixed(1);
}

async function commitLensSlider(val) {
  const pos = parseInt(val) / 10;
  document.getElementById('lens-val').textContent = pos.toFixed(1);
  try {
    await fetch('/api/focus', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode:'manual', lens_position:pos})});
  } catch(e) {}
}

async function triggerAF() {
  const btn = document.getElementById('btn-trigger-af');
  btn.disabled = true; btn.textContent = '⊙ FOCUSING…';
  try {
    await fetch('/api/focus', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:'auto'})});
  } catch(e) {}
  setTimeout(() => { btn.disabled=false; btn.textContent='⊙ TRIGGER AF'; }, 3000);
}

// ── camera source ────────────────────────────────────────────────────────────
async function setCamera(index) {
  [0, 1].forEach(i =>
    document.getElementById('cam-src-' + i)?.classList.toggle('active', i === index));
  try {
    await fetch('/api/camera', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({index})
    });
  } catch(e) {}
}

// ── flip / mirror / overlay ───────────────────────────────────────────────────
let flipActive    = false;
let mirrorActive  = false;
let overlayActive = true;

async function toggleFlip() {
  flipActive = !flipActive;
  document.getElementById('btn-flip').classList.toggle('active', flipActive);
  try {
    await fetch('/api/transform', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({flip: flipActive})});
  } catch(e) {}
}

async function toggleMirror() {
  mirrorActive = !mirrorActive;
  document.getElementById('btn-mirror').classList.toggle('active', mirrorActive);
  try {
    await fetch('/api/transform', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({mirror: mirrorActive})});
  } catch(e) {}
}

async function toggleOverlay() {
  overlayActive = !overlayActive;
  document.getElementById('btn-overlay').classList.toggle('active', overlayActive);
  try {
    await fetch('/api/config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({show_overlay: overlayActive})});
  } catch(e) {}
}

// ── log render ───────────────────────────────────────────────────────────────
function renderLog(entries) {
  const box = document.getElementById('log-box');
  box.innerHTML = entries.map(e =>
    `<div class="log-${e.tag}">[${e.ts}] ${e.msg}</div>`
  ).join('');
}

// ── init ─────────────────────────────────────────────────────────────────────
pollStatus();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ctrl = CameraController()

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
        ctrl.shutdown()
