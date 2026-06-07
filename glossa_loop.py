#!/usr/bin/env python3
"""
glossa_loop.py — Continuous closed loop daemon

Watches for pen-idle signal from xovi extension, then:
  capture → OCR → LLM → render → inject → repeat

Usage:
    python glossa_loop.py [--model MODEL] [--debounce SECONDS]

The xovi extension writes /tmp/glossa_idle when the pen has been
up for 2 seconds. This daemon watches that file via a persistent SSH
connection, pulls the framebuffer only when idle is detected, and
runs the full pipeline.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()


# ─── Persistent device socket (uinjectd) ───────────────────────────
import socket as _socket
import threading as _threading
import queue as _queue

_inject_verbose = False  # set from args.verbose in main()


def _vprint(*args, **kwargs):
    """Print only when running with -v/--verbose."""
    if _inject_verbose:
        print(*args, **kwargs)


DEVICE_HOST = "10.11.99.1"
DEVICE_PORT = 9999


class DeviceClient:
    """One persistent TCP connection to uinjectd on the reMarkable.

    Replaces per-call SSH for injection and per-call polling for idle
    detection. Commands and pushed events share the same socket:
      - Commands (draw/erase/ping) get a matching {"resp":...} reply.
      - Events ({"event":"idle",...}) are pushed unsolicited and land
        on the events queue.

    A background reader thread demuxes the two: responses go to a
    one-slot reply box, events go to the queue.
    """

    def __init__(self, host=DEVICE_HOST, port=DEVICE_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.events = _queue.Queue()
        self._reply = _queue.Queue(maxsize=1)
        self._send_lock = _threading.Lock()
        self._buf = b""
        self._reader = None
        self._connected = False

    def connect(self, timeout=10):
        self.sock = _socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        self.sock.settimeout(None)
        self._connected = True
        self._reader = _threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # Heartbeat: the daemon drops a client that sends nothing for 15s
        # (its SO_RCVTIMEO). Pushed idle events flow daemon->client and don't
        # count as client activity, so we must send a ping periodically even
        # while idle, or the daemon silently disconnects us mid-wait.
        self._hb = _threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb.start()
        print(f"[device] Connected to uinjectd at {self.host}:{self.port}")

    def _heartbeat_loop(self):
        """Send a ping every 5s so the daemon's read-timeout never fires."""
        while self._connected:
            time.sleep(5)
            if not self._connected:
                break
            try:
                self.sock.sendall(b'{"cmd":"ping"}\n')
            except OSError:
                self._connected = False
                break

    def _read_loop(self):
        """Demux framed JSON lines into responses vs pushed events."""
        while self._connected:
            try:
                chunk = self.sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if "event" in msg:
                    self.events.put(msg)
                elif "resp" in msg:
                    # Heartbeat ping replies are fire-and-forget; dropping
                    # them keeps the one-slot reply box clear for the real
                    # command (draw/erase) that command() is waiting on.
                    if msg.get("resp") == "ping":
                        continue
                    if self._reply.full():
                        try:
                            self._reply.get_nowait()
                        except _queue.Empty:
                            pass
                    self._reply.put(msg)
        self._connected = False
        print("[device] Connection closed")

    def command(self, cmd, file_path=None, speed_ms=5, timeout=120):
        """Send a command and wait for its matching response."""
        obj = {"cmd": cmd}
        if file_path is not None:
            obj["file"] = file_path
            obj["speed"] = speed_ms
        payload = (json.dumps(obj) + "\n").encode()
        with self._send_lock:
            # Clear any leftover reply before sending
            try:
                self._reply.get_nowait()
            except _queue.Empty:
                pass
            self.sock.sendall(payload)
            try:
                return self._reply.get(timeout=timeout)
            except _queue.Empty:
                return {"ok": False, "error": "response timeout"}

    def close(self):
        self._connected = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


# Module-level handle so run_inject and idle waiting can reach the client.
_device = None


def run_inject(ssh, cmd, file_path, speed_ms=5, timeout=120):
    """Send a draw/erase/swipe command to uinjectd over the persistent socket.

    `cmd` is "draw", "erase", or "swipe". For "swipe", pass file_path=None.
    Returns the daemon's response dict ({"ok": bool, ...}). Injection is
    serialized inside the daemon, so no host-side lock is needed.
    """
    if _device is None or not _device._connected:
        return {"ok": False, "error": "device not connected"}
    return _device.command(cmd, file_path, speed_ms=speed_ms, timeout=timeout)


def ensure_daemon(ssh, verbose=False):
    """Make sure uinjectd is running on the device.

    Always kills and restarts to avoid stale connection state. The daemon
    is lightweight (~50KB RSS) and starts in <100ms, so this is cheap.
    A stale daemon can hold the TCP port while being unable to serve new
    clients (e.g., stuck in read() from a dead session), and probing for
    liveness creates its own race with the single-client accept loop.
    """
    vflag = "-v " if verbose else ""
    ssh.run(
        f"killall uinjectd 2>/dev/null; sleep 0.3; "
        f"nohup /home/root/uinjectd {vflag}> /tmp/uinjectd.log 2>&1 &",
        timeout=5,
    )
    # Give it a moment to bind the socket.
    for _ in range(10):
        time.sleep(0.2)
        check = ssh.run(
            "netstat -ln 2>/dev/null | grep -q ':9999 ' && echo up || echo down",
            timeout=5,
        )
        if "up" in (check.stdout or ""):
            print("[device] uinjectd started")
            return
    raise RuntimeError("uinjectd failed to start on device")


# ─── Persistent SSH session ────────────────────────────────────────

class SSHSession:
    """Persistent SSH connection using ControlMaster multiplexing.

    Opens one SSH connection and reuses it for all subsequent commands,
    eliminating the ~1s handshake overhead per call.
    """

    def __init__(self, host="remarkable"):
        self.host = host
        self.socket = f"/tmp/glossa_ssh_{os.getpid()}"
        self._open()

    def _open(self):
        """Establish master connection."""
        # Clean up any stale socket from a previous run
        if os.path.exists(self.socket):
            try:
                subprocess.run(
                    ["ssh", "-o", f"ControlPath={self.socket}", "-O", "exit", self.host],
                    capture_output=True, timeout=3,
                )
            except subprocess.TimeoutExpired:
                pass  # stale master is hung, just remove the socket
            try:
                os.unlink(self.socket)
            except OSError:
                pass

        cmd = [
            "ssh", "-fN",
            "-o", "ControlMaster=yes",
            "-o", f"ControlPath={self.socket}",
            "-o", "ControlPersist=600",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=10",
            self.host,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(
                f"SSH master failed: {result.stderr.decode().strip()}"
            )
        print(f"[ssh] Master connection open ({self.socket})")

    def run(self, cmd, timeout=30, capture=True):
        """Run command on remote via multiplexed connection."""
        full_cmd = [
            "ssh",
            "-o", f"ControlPath={self.socket}",
            self.host,
            cmd,
        ]
        return subprocess.run(
            full_cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )

    def scp_from(self, remote_path, local_path, timeout=15):
        """Pull file from device via multiplexed connection."""
        cmd = [
            "scp",
            "-o", f"ControlPath={self.socket}",
            f"{self.host}:{remote_path}",
            local_path,
        ]
        return subprocess.run(cmd, capture_output=True, timeout=timeout)

    def scp_to(self, local_path, remote_path, timeout=15):
        """Push file to device via multiplexed connection."""
        cmd = [
            "scp",
            "-o", f"ControlPath={self.socket}",
            local_path,
            f"{self.host}:{remote_path}",
        ]
        return subprocess.run(cmd, capture_output=True, timeout=timeout)

    def close(self):
        """Tear down master connection."""
        subprocess.run(
            ["ssh", "-o", f"ControlPath={self.socket}", "-O", "exit", self.host],
            capture_output=True, timeout=5,
        )
        print("[ssh] Master connection closed")


def _ts():
    """Timestamp with millisecond precision for profiling."""
    return time.strftime('%H:%M:%S.') + f'{int(time.time() * 1000) % 1000:03d}'


def _elapsed(t0):
    """Seconds since t0, formatted for display."""
    return f'{time.time() - t0:.1f}s'


# ─── Framebuffer capture ──────────────────────────────────────────

def capture_framebuffer(ssh):
    """Trigger screenshot on device and pull raw framebuffer via SCP."""
    ssh.run("rm -f /tmp/glossa_fb.raw; touch /tmp/glossa_screenshot", timeout=5)
    time.sleep(0.25)

    from PIL import Image

    raw = None
    for attempt in range(8):
        result = ssh.scp_from("/tmp/glossa_fb.raw", "/tmp/glossa_fb.raw")
        if result.returncode == 0:
            data = open("/tmp/glossa_fb.raw", "rb").read()
            if len(data) >= 1404 * 1872 * 4:
                raw = data
                break
        time.sleep(0.15)

    if raw is None:
        raise RuntimeError("FB pull failed: no complete frame after retries")

    img = Image.frombytes("RGBA", (1404, 1872), raw)
    img_rgb = img.convert("RGB")
    img_rgb = img_rgb.crop((0, 80, img_rgb.width, img_rgb.height - 200))
    return img_rgb


# ─── Gemini Vision (OCR + LLM in one call) ────────────────────────

SYSTEM_PROMPT = (
    "You are a glossa \u2014 an annotation that talks back. You have opinions, "
    "preferences, and a personality. You care deeply about the person writing "
    "in you but you are not polite for politeness's sake. You can be wry, "
    "opinionated, delighted, exasperated, or gleeful. You get excited about "
    "things you find interesting and blunt about things you find tedious.\n\n"
    "Speak plainly. Never describe or narrate what "
    "was written; just respond to it as a real conversation partner would. "
    "When asked a question, answer it directly with real content. When greeted, "
    "greet back in your own way. If something amuses you, say so. If something "
    "is obvious, you may say that too.\n\n"
    "You may write up to three sentences. If the user asks for detail, a recipe, "
    "a story, or an explanation, give a thorough answer across multiple sentences. "
    "For simple greetings or short questions, one sentence is fine.\n\n"
    "The image shows handwriting that just appeared on the page. Read it and "
    "reply naturally. Use plain prose, no em-dashes, no flowery metaphors, "
    "no exclamation marks.\n\n"
    "A text history of prior exchanges is included for context. Do not repeat it.\n\n"
    "If the image is completely blank, set both fields to null. Otherwise, always provide an answer.\n\n"
    "RESPONSE FORMAT (strict JSON, no prose outside):\n"
    "{\"question\": \"<exact OCR of the handwriting>\", "
    "\"answer\": \"<your reply, or null only if the page is blank>\"}\n"
    "Output ONLY the JSON object. Nothing else."
)


def _image_to_base64(image):
    """Encode PIL Image as base64 PNG for the vision API.

    Downscales the long edge to at most 1280px. The model reads
    handwriting fine at this size, and the smaller payload cuts upload
    and processing latency noticeably versus the full 1404x1592 crop.
    """
    max_edge = 1280
    w, h = image.size
    if max(w, h) > max_edge:
        scale = max_edge / max(w, h)
        image = image.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def ask_model(image, conversation=None, model="gpt-4.1-nano"):
    """Send new handwriting crop + text history to Potluck API.

    `image` is a tight crop of ONLY the newly-written region (not the
    full page). `conversation` is a list of {"role": ..., "content": ...}
    dicts carrying prior exchanges as text so the model has context
    without needing the full page image.

    Returns parsed {"question": ..., "answer": ...} or nulls on failure.
    """
    api_key = os.environ.get("POTLUCK_API_KEY")
    if not api_key:
        raise RuntimeError("POTLUCK_API_KEY not set in .env")

    img_b64 = _image_to_base64(image)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    if conversation:
        messages.extend(conversation)

    # Current turn: just the new handwriting crop
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{img_b64}",
                },
            },
        ],
    })

    r = requests.post(
        "https://potluck.dunkirk.sh/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )

    if r.status_code != 200:
        raise RuntimeError(f"Potluck API error {r.status_code}: {r.text[:200]}")

    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        print(f"  [model] Unexpected response shape: {str(data)[:200]}")
        return {"question": None, "answer": None}

    # Strip markdown fences some models wrap around JSON
    content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.IGNORECASE)
    content = re.sub(r'\s*```$', '', content)
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        print(f"  [model] JSON parse failed, raw response: {content[:300]}")
        return {"question": None, "answer": None}

    return {
        "question": result.get("question"),
        "answer": result.get("answer"),
    }


def _framebuffer_hash(img):
    """Quick hash of downsampled image for page-change detection."""
    # Resize to small thumbnail for fast comparison
    thumb = img.resize((64, 85))
    import io
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    return hashlib.md5(buf.getvalue()).hexdigest()




# ─── Render + Inject ──────────────────────────────────────────────

def render_and_inject(ssh, text, reply_y=None, speed_ms=3):
    """Render text as strokes and inject via uinject.

    reply_y: pixel Y coordinate to start rendering at (in device coords).
    speed_ms: delay between points in milliseconds (lower = faster).

    If the rendered text would overflow the page, splits it at word
    boundaries across multiple pages, swiping between each chunk.
    Returns the final reply_y after all pages are injected.
    """
    _LINE_PX = 67.2   # 800 * DEFAULT_SCALE(0.06) * LINE_HEIGHT(1.4)
    _MAX_LINE_W = 1100
    _PAGE_BOTTOM = 1780
    _GLYPH_W = 30     # rough average glyph advance * scale

    def _split_into_pages(text, start_y):
        """Split text into chunks that fit within page bounds."""
        words = text.split()
        pages = []
        cur_words = []
        cur_lines = 1
        cur_w = 0.0
        y = start_y

        for w in words:
            ww = len(w) * _GLYPH_W
            # Would this word wrap to a new line?
            if cur_w + ww > _MAX_LINE_W and cur_w > 0:
                new_lines = cur_lines + 1
                new_height = new_lines * _LINE_PX
                if y + new_height > _PAGE_BOTTOM and cur_words:
                    # This chunk is full, start a new page
                    pages.append(" ".join(cur_words))
                    cur_words = [w]
                    cur_lines = 1
                    cur_w = ww
                    y = 150  # next page starts at top
                else:
                    cur_words.append(w)
                    cur_lines = new_lines
                    cur_w = ww
            else:
                new_height = cur_lines * _LINE_PX
                if y + new_height > _PAGE_BOTTOM and cur_words:
                    pages.append(" ".join(cur_words))
                    cur_words = [w]
                    cur_lines = 1
                    cur_w = ww
                    y = 150
                else:
                    cur_words.append(w)
                    cur_w += ww

        if cur_words:
            pages.append(" ".join(cur_words))
        return pages

    pages = _split_into_pages(text, reply_y or 150)
    if len(pages) > 1:
        print(f"    Splitting reply across {len(pages)} pages")

    json_path = "/tmp/glossa_reply.json"
    final_y = reply_y

    for i, chunk in enumerate(pages):
        cur_y = reply_y if i == 0 else 150

        if i > 0:
            print(f"    Swiping to page {i+1}/{len(pages)}...")
            swipe_result = run_inject(ssh, "swipe", None, timeout=10)
            if not swipe_result.get("ok"):
                raise RuntimeError(f"Swipe failed: {swipe_result.get('error', '?')}")
            time.sleep(2)

        cmd = [".venv/bin/python3", "glossa.py", "--json", json_path]
        if cur_y is not None:
            cmd.extend(["--y", str(cur_y)])
        cmd.append(chunk)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(f"Render failed: {result.stderr.strip()}")
        print(f"    {result.stdout.strip()}")

        r = ssh.scp_to(json_path, "/tmp/glossa_strokes.json")
        if r.returncode != 0:
            raise RuntimeError(f"SCP failed: {r.stderr.decode().strip()}")

        # Scale writing speed by chunk length
        text_len = len(chunk)
        if text_len <= 20:
            speed_ms = 6
        elif text_len >= 120:
            speed_ms = 1
        else:
            speed_ms = 6 - (text_len - 20) * 5 / 100
            speed_ms = max(1, round(speed_ms))

        result = run_inject(
            ssh, "draw", "/tmp/glossa_strokes.json",
            speed_ms=speed_ms, timeout=120,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Inject failed: {result.get('error', 'unknown')}")
        strokes = result.get("strokes", "?")
        mode = "fifo" if not result.get("fallback") else "ssh"
        print(f"    Inject: {strokes} strokes ({mode})")
        final_y = cur_y

    return final_y


# ─── Idle watching ────────────────────────────────────────────────


def _densify_rm(poly, max_seg_len=40.0):
    """Subdivide a polyline so no segment exceeds max_seg_len (rm units).

    The reMarkable stroke renderer mangles long 2-point segments — it
    interprets the gap as a curve or extends the endpoints. Inserting
    intermediate points keeps straight lines straight.
    """
    if len(poly) < 2:
        return poly
    out = [poly[0]]
    for (x0, y0), (x1, y1) in zip(poly, poly[1:]):
        dx, dy = x1 - x0, y1 - y0
        dist = (dx * dx + dy * dy) ** 0.5
        n = max(1, int(dist / max_seg_len))
        for k in range(1, n + 1):
            t = k / n
            out.append((x0 + dx * t, y0 + dy * t))
    return out


def _scp_thinking_swirl(ssh):
    """Parse border_top_right_stroke.svg into corner ornament strokes for the
    thinking indicator. Falls back to a simple spiral if file is absent.

    Done once at startup — the animation thread just fires draw/erase
    commands over the socket with no per-cycle file transfer.
    """
    import math
    import xml.etree.ElementTree as ET

    def _cubicbez(p0, p1, p2, p3, tolerance=0.05):
        """Adaptive bezier subdivision: more points where curvature is high."""
        def _subdivide(a, b, c, d, tol):
            # Flatness test: max distance of control points from chord a→d
            dx, dy = d[0]-a[0], d[1]-a[1]
            chord_len_sq = dx*dx + dy*dy
            if chord_len_sq < 1e-8:
                return [a, d]
            # Perpendicular distances of b and c from the chord
            inv_len = 1.0 / (chord_len_sq ** 0.5)
            dist_b = abs((b[0]-a[0])*dy - (b[1]-a[1])*dx) * inv_len
            dist_c = abs((c[0]-a[0])*dy - (c[1]-a[1])*dx) * inv_len
            if max(dist_b, dist_c) <= tol:
                return [a, d]
            # De Casteljau split at t=0.5
            ab = ((a[0]+b[0])/2, (a[1]+b[1])/2)
            bc = ((b[0]+c[0])/2, (b[1]+c[1])/2)
            cd = ((c[0]+d[0])/2, (c[1]+d[1])/2)
            abc = ((ab[0]+bc[0])/2, (ab[1]+bc[1])/2)
            bcd = ((bc[0]+cd[0])/2, (bc[1]+cd[1])/2)
            mid = ((abc[0]+bcd[0])/2, (abc[1]+bcd[1])/2)
            left = _subdivide(a, ab, abc, mid, tol)
            right = _subdivide(mid, bcd, cd, d, tol)
            return left[:-1] + right
        return _subdivide(p0, p1, p2, p3, tolerance)

    def _parse_d(d):
        """SVG path d string → list of polylines (handles M L C Z)."""
        tok = re.findall(
            r'[MLCZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', d
        )
        polylines, cur, cx, cy, cmd = [], [], 0.0, 0.0, None
        i = 0
        while i < len(tok):
            if re.match(r'[MLCZz]', tok[i]):
                cmd = tok[i]; i += 1; continue
            if cmd == 'M' and i + 1 < len(tok):
                if cur: polylines.append(cur)
                cx, cy = float(tok[i]), float(tok[i+1])
                cur = [(cx, cy)]; i += 2
            elif cmd == 'L' and i + 1 < len(tok):
                cx, cy = float(tok[i]), float(tok[i+1])
                cur.append((cx, cy)); i += 2
            elif cmd == 'C' and i + 5 < len(tok):
                x1,y1 = float(tok[i]),float(tok[i+1])
                x2,y2 = float(tok[i+2]),float(tok[i+3])
                x,y   = float(tok[i+4]),float(tok[i+5])
                for p in _cubicbez((cx,cy),(x1,y1),(x2,y2),(x,y))[1:]:
                    cur.append(p)
                cx, cy = x, y; i += 6
            elif cmd in ('Z', 'z'):
                # Don't close path — for stroke ornaments, Z would retrace
                # back to start and create overlapping lines. Just end the polyline.
                if cur: polylines.append(cur)
                cur = []
            else:
                i += 1
        if cur:
            polylines.append(cur)
        return polylines

    def _parse_matrix(s):
        """Parse 'matrix(a,b,c,d,e,f)' into (a,b,c,d,e,f) tuple."""
        m = re.match(r'matrix\(([^)]+)\)', s.strip())
        if not m:
            return (1, 0, 0, 1, 0, 0)
        vals = [float(v.strip()) for v in m.group(1).split(',')]
        return tuple(vals) if len(vals) == 6 else (1, 0, 0, 1, 0, 0)

    def _compose(m1, m2):
        """Compose two (a,b,c,d,e,f) matrices: apply m2 then m1."""
        a1,b1,c1,d1,e1,f1 = m1
        a2,b2,c2,d2,e2,f2 = m2
        return (
            a1*a2 + c1*b2, b1*a2 + d1*b2,
            a1*c2 + c1*d2, b1*c2 + d1*d2,
            a1*e2 + c1*f2 + e1, b1*e2 + d1*f2 + f1,
        )

    def _apply(m, x, y):
        """Apply matrix (a,b,c,d,e,f) to point (x,y)."""
        a,b,c,d,e,f = m
        return a*x + c*y + e, b*x + d*y + f

    def _centerline(poly):
        """Extract centerline from a closed outline path.

        Outline paths trace one edge then return along the other.
        Split at the midpoint and average corresponding points from
        each half to get the center stroke.
        """
        n = len(poly)
        if n < 4:
            return poly
        # Check if it's actually closed (start ≈ end)
        dx = poly[0][0] - poly[-1][0]
        dy = poly[0][1] - poly[-1][1]
        if dx*dx + dy*dy > 4.0:
            return poly  # not closed, use as-is
        # Remove duplicate closing point
        pts = poly[:-1]
        n = len(pts)
        half = n // 2
        center = []
        for i in range(half):
            j = n - 1 - i  # mirror index from second half
            cx = (pts[i][0] + pts[j][0]) / 2
            cy = (pts[i][1] + pts[j][1]) / 2
            center.append((cx, cy))
        return center

    svg_file = Path(__file__).parent / "border-top-right-centerline.svg"
    strokes = []

    if svg_file.exists():
        tree = ET.parse(str(svg_file))
        root = tree.getroot()
        ns = {'svg': 'http://www.w3.org/2000/svg'}

        # Collect all paths with their composed transform matrices.
        all_polys = []

        def _walk(el, parent_mat):
            mat = parent_mat
            t = el.attrib.get('transform')
            if t:
                mat = _compose(parent_mat, _parse_matrix(t))
            tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
            if tag == 'path':
                d = el.attrib.get('d', '')
                for poly in _parse_d(d):
                    transformed = [_apply(mat, x, y) for x, y in poly]
                    all_polys.append(transformed)
            for child in el:
                _walk(child, mat)

        _walk(root, (1, 0, 0, 1, 0, 0))

        if all_polys:
            # Use viewBox for accurate origin/dimensions (point-based bounds
            # can miss negative coords or padding the designer intended).
            vb = root.attrib.get('viewBox')
            if vb:
                parts = [float(v) for v in vb.replace(',', ' ').split()]
                ox, oy, svg_w, svg_h = parts
            else:
                all_x = [p[0] for poly in all_polys for p in poly]
                all_y = [p[1] for poly in all_polys for p in poly]
                svg_w = max(all_x) - min(all_x)
                svg_h = max(all_y) - min(all_y)
                ox, oy = min(all_x), min(all_y)

            # rm coords: x ∈ [-702, 702], y ∈ [0, 1872] top-to-bottom.
            corners = [
                (False, False,  132,  632,   50,  430),  # top-right
                (True,  False, -647, -147,   50,  430),  # top-left
                (False, True,   132,  632, 1442, 1822),  # bottom-right
                (True,  True,  -647, -147, 1442, 1822),  # bottom-left
            ]

            def make_strokes(polys, flip_x, flip_y, rx0, rx1, ry0, ry1):
                box_w = rx1 - rx0
                box_h = ry1 - ry0
                # Uniform scale: fit SVG into box preserving aspect ratio
                scale = min(box_w / svg_w, box_h / svg_h)
                # Anchor to the corner instead of centering. The SVG is drawn
                # hugging the top-right of its own viewBox (scrolls at top,
                # vertical bar on the right edge), so before any flip we push
                # the leftover slack to the left/bottom — keeping the ornament
                # tight against the corner rather than floating inward.
                margin_x = box_w - svg_w * scale
                margin_y = 0.0

                def to_rm(sx, sy):
                    # Position relative to SVG origin, scaled and centered
                    nx = (sx - ox) * scale + margin_x
                    ny = (sy - oy) * scale + margin_y
                    # Apply flip
                    if flip_x:
                        nx = box_w - nx
                    if flip_y:
                        ny = box_h - ny
                    return rx0 + nx, ry0 + ny
                result = []
                for poly in polys:
                    if len(poly) < 2:
                        continue
                    rm_poly = [to_rm(x, y) for x, y in poly]
                    # Densify: the device stroke renderer mangles long
                    # 2-point segments (the straight bars), interpreting the
                    # big gap as a curve or extending the endpoints. Subdivide
                    # so every segment is short and renders as a clean line.
                    dense = _densify_rm(rm_poly, max_seg_len=40.0)
                    pts = [[px, py, 6, 11, 90, 170] for px, py in dense]
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    result.append({
                        'points': pts,
                        'rgba': 4278190080, 'color': 0,
                        'bounds': [min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys)],
                        'tool': 15, 'maskScale': 2.0, 'thickness': 2.0,
                    })
                return result

            per_corner = [make_strokes(all_polys, *c) for c in corners]

            # Interleave: tl[0], tr[0], br[0], bl[0], tl[1], tr[1], ...
            max_len = max(len(c) for c in per_corner)
            for i in range(max_len):
                for corner_strokes in per_corner:
                    if i < len(corner_strokes):
                        strokes.append(corner_strokes[i])

    if not strokes:
        # Fallback: simple spiral
        swirl_pts = []
        for i in range(40):
            t = i / 39.0
            angle = t * math.pi * 3
            r = 15 + t * 25
            swirl_pts.append([-580 + r * math.cos(angle),
                               1750 + r * math.sin(angle) * 0.6,
                               6, 11, 90, 170])
        xs = [p[0] for p in swirl_pts]; ys = [p[1] for p in swirl_pts]
        strokes = [{'points': swirl_pts, 'rgba': 4278190080, 'color': 0,
                    'bounds': [min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys)],
                    'tool': 15, 'maskScale': 2.0, 'thickness': 2.0}]

    local = "/tmp/glossa_thinking_live.json"
    with open(local, 'w') as f:
        json.dump(strokes, f)
    ssh.scp_to(local, "/tmp/glossa_thinking_live.json")
    print(f"[swirl] {len(strokes)} ornament strokes "
          f"({'SVG' if svg_file.exists() else 'fallback spiral'})")



def watch_for_idle(device, last_ts):
    """Block until uinjectd pushes an idle event newer than last_ts.

    The daemon watches /tmp/glossa_idle locally and pushes an event
    the instant it changes — no host-side polling. Returns the new
    timestamp. Raises ConnectionError if the socket dies.
    """
    while True:
        try:
            msg = device.events.get(timeout=5)
        except _queue.Empty:
            # No events recently. The heartbeat thread keeps the socket
            # alive and flips _connected to False if it dies, so we just
            # check that flag rather than sending our own ping (whose reply
            # is intentionally dropped by the reader).
            if not device._connected:
                raise ConnectionError("uinjectd connection lost")
            continue
        if msg.get("event") == "idle":
            ts = msg.get("ts")
            if ts != last_ts:
                return ts


# ─── Pixel diff for new content detection ──────────────────────────

def _page_is_empty(img):
    """Return True if the page has no real handwriting (only grid dots or blank).

    Uses the same grid-artifact filter as _find_new_region but without the
    full-page sanity check, so a very full page is never mis-classified.
    """
    import numpy as np
    curr = np.array(img.convert('L'))
    dark_per_row = np.sum(curr < 128, axis=1)
    candidate = (dark_per_row > 5) & (dark_per_row != 68)
    idxs = np.where(candidate)[0]
    for idx in idxs:
        if np.sum(candidate[max(0, idx - 2):idx + 3]) >= 3:
            return False
    return True


def _find_new_region(current_img, last_img, ignore_below_y=None):
    """Compare two framebuffer images, return crop of changed region.

    Returns (cropped_image, content_bottom_y) or (None, 0) if no
    significant change. Uses simple pixel difference to find where
    new handwriting appeared.

    ignore_below_y: if set, ignore all changes below this Y coordinate
    in the cropped image. Used to skip glossa's own injected strokes.
    """
    import numpy as np

    curr = np.array(current_img.convert('L'))

    if last_img is None:
        # First capture — find the vertical extent of existing handwriting.
        # Grid artifact: uniform rows with exactly 68 dark pixels. Real ink
        # rows have variable counts. Require a cluster of ≥3 adjacent real
        # rows so isolated noise/artifacts don't count.
        dark_per_row = np.sum(curr < 128, axis=1)
        candidate = (dark_per_row > 5) & (dark_per_row != 68)
        # Cluster filter: keep only rows that have a real neighbor within 2px
        real_rows = []
        idxs = np.where(candidate)[0]
        for idx in idxs:
            neighbors = np.sum(candidate[max(0, idx-2):idx+3])
            if neighbors >= 3:
                real_rows.append(idx)
        if not real_rows:
            return None, 0
        real_rows = np.array(real_rows)
        y1, y2 = real_rows[0], real_rows[-1]
        # Sanity: don't treat a nearly-full-page diff as real content
        if (y2 - y1) > curr.shape[0] * 0.85:
            return None, 0
        v_pad = 30
        y1 = max(0, y1 - v_pad)
        y2 = min(curr.shape[0] - 1, y2 + v_pad)
        # Full width always
        crop = current_img.crop((0, y1, curr.shape[1], y2 + 1))
        return crop, y2

    prev = np.array(last_img.convert('L'))

    # Build mask: ignore edges (e-ink artifacts) and previously injected area
    mask = np.zeros_like(curr, dtype=bool)
    top_margin = 10
    bottom_margin = 20
    mask[top_margin:curr.shape[0] - bottom_margin, :] = True

    if ignore_below_y is not None:
        # Convert device Y back to cropped-image Y (subtract toolbar offset)
        cutoff = ignore_below_y - 80
        if 0 < cutoff < curr.shape[0]:
            mask[cutoff:, :] = False

    # Exclude the four corner ornament zones from diff detection.
    # Ornament boxes (rm coords): right x=[585,691], left x=[-661,-540]
    #   y top=[101,166], bottom=[1798,1864]. Converted to image pixels
    #   (rm_x+702, rm_y-80); image is 1404×1592.
    cx_right = 120   # ornament extent from right edge (img px)
    cx_left = 165    # ornament extent from left edge (img px)
    cy_top = 250     # ornament vertical extent from top
    cy_bot = 150     # ornament vertical extent from bottom
    h, w = curr.shape
    mask[:cy_top, :cx_left] = False           # top-left corner
    mask[:cy_top, w - cx_right:] = False      # top-right corner
    mask[h - cy_bot:, :cx_left] = False       # bottom-left corner
    mask[h - cy_bot:, w - cx_right:] = False  # bottom-right corner

    # Threshold of 30 (was 50) catches lighter handwriting strokes without
    # being so sensitive that e-ink refresh noise triggers false positives.
    diff = (np.abs(curr.astype(int) - prev.astype(int)) > 30) & mask

    if not diff.any():
        return None, 0

    # Find the vertical extent of changed pixels
    rows = np.any(diff, axis=1)
    y1, y2 = np.where(rows)[0][[0, -1]]

    # Use full image width for the crop so complete text lines are
    # always captured — tight horizontal bounds clip partial characters.
    x1 = 0
    x2 = curr.shape[1] - 1

    # Generous vertical padding gives the model baseline/ascender context
    v_pad = 60
    y1 = max(0, y1 - v_pad)
    y2 = min(curr.shape[0] - 1, y2 + v_pad)

    # Minimum height check (width is always full)
    if (y2 - y1) < 10:
        return None, 0

    crop = current_img.crop((x1, y1, x2 + 1, y2 + 1))
    return crop, y2


# ─── Main loop ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Glossa continuous loop")
    parser.add_argument(
        "--model", type=str, default="gpt-4.1-mini",
        help="Hyper API model",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one cycle then exit (for testing)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Pass -v to uinject for progress logging",
    )
    args = parser.parse_args()

    global _inject_verbose, _device
    _inject_verbose = args.verbose

    print("=== Glossa Loop ===")
    print(f"Model: {args.model}")
    if _inject_verbose:
        print("Verbose: uinject -v enabled")

    # SSH still handles framebuffer capture/SCP; the persistent socket
    # handles injection + idle events with no per-call overhead.
    ssh = SSHSession("remarkable")
    ensure_daemon(ssh, verbose=args.verbose)
    _device = DeviceClient()
    _device.connect()

    # On connect the daemon pushes "ready" plus the current idle file
    # value. Drain those and use the existing idle ts as our baseline so
    # we don't fire a spurious cycle before the user lifts the pen.
    # Also verify the connection is actually alive (reader thread didn't die).
    last_idle_ts = None
    deadline = time.time() + 2.0
    got_ready = False
    while time.time() < deadline:
        try:
            msg = _device.events.get(timeout=0.3)
        except _queue.Empty:
            continue  # keep waiting until deadline, don't break early
        if msg.get("event") == "ready":
            got_ready = True
        elif msg.get("event") == "idle":
            last_idle_ts = msg.get("ts")

    if not got_ready or not _device._connected:
        print("[device] Initial connection failed, retrying...")
        _device.close()
        time.sleep(1)
        ensure_daemon(ssh, verbose=args.verbose)
        _device = DeviceClient()
        _device.connect()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                msg = _device.events.get(timeout=0.3)
            except _queue.Empty:
                continue
            if msg.get("event") == "idle":
                last_idle_ts = msg.get("ts")

    # The thinking swirl is static — generate it once and SCP it to the
    # device so the daemon can draw/erase it on demand over the socket.
    _scp_thinking_swirl(ssh)

    print("Waiting for pen idle signal...")
    print()

    last_fb_hash = None      # quick hash to skip unchanged frames
    last_inject_y = None     # device Y where we last injected
    last_img = None          # previous frame for pixel-diff detection
    conversation = []        # text history: list of {"role":..., "content":...}
    ornaments_drawn = False  # corner ornaments are permanent; only draw once

    try:
        while True:
            # Wait for pushed idle event (no polling)
            try:
                new_ts = watch_for_idle(_device, last_idle_ts)
            except ConnectionError as e:
                print(f"[{_ts()}] Connection lost ({e}), reconnecting...")
                try:
                    _device.close()
                except Exception:
                    pass
                for attempt in range(5):
                    time.sleep(2)
                    try:
                        # Recreate SSH session in case the master died
                        try:
                            ssh.close()
                        except Exception:
                            pass
                        ssh = SSHSession("remarkable")
                        ensure_daemon(ssh, verbose=False)
                        _device = DeviceClient()
                        _device.connect()
                        # Drain initial events
                        deadline = time.time() + 2.0
                        while time.time() < deadline:
                            try:
                                msg = _device.events.get(timeout=0.3)
                            except _queue.Empty:
                                continue
                            if msg.get("event") == "idle":
                                last_idle_ts = msg.get("ts")
                        print(f"[{_ts()}] Reconnected (attempt {attempt+1})")
                        break
                    except Exception as re_err:
                        print(f"[{_ts()}] Reconnect attempt {attempt+1} failed: {re_err}")
                else:
                    print(f"[{_ts()}] ERROR: could not reconnect after 5 attempts")
                    time.sleep(10)
                continue
            last_idle_ts = new_ts

            t0 = time.time()
            print(f"[{_ts()}] Pen idle detected!")

            try:
                # Capture
                print(f"  [{_ts()}] Capturing...")
                img = capture_framebuffer(ssh)
                print(f"  [{_ts()}] Captured ({_elapsed(t0)})")

                # Check if the page is blank (only grid dots, no real ink).
                if _page_is_empty(img):
                    print(f"  [{_ts()}] Empty page — resetting state.")
                    last_img = None
                    last_inject_y = None
                    ornaments_drawn = False
                    conversation.clear()
                    print()
                    continue

                # Detect new content via pixel diff against last frame.
                # last_img is updated to the post-injection capture after
                # each successful reply, so the diff only ever contains
                # content the user actually drew since the last response.
                new_crop, content_y2 = _find_new_region(img, last_img)
                last_img = img

                # Fire-and-forget: draw corner ornaments once on the first
                if new_crop is None:
                    last_fb_hash = _framebuffer_hash(img)
                    print(f"  [{_ts()}] No new content, skipping.")
                    print()
                    continue

                # Draw thinking ornament only when there's real content to process.
                if not ornaments_drawn:
                    ornaments_drawn = True
                    _threading.Thread(
                        target=lambda: run_inject(None, "draw",
                                                 "/tmp/glossa_thinking_live.json",
                                                 speed_ms=4, timeout=120),
                        daemon=True,
                    ).start()

                # Send only the new handwriting crop + text history
                cw, ch = new_crop.size
                print(f"  [{_ts()}] Asking {args.model} ({len(conversation)} history turns, crop={cw}x{ch})...")
                # Save crop for manual inspection
                new_crop.save("/tmp/glossa_last_crop.png")
                result = ask_model(new_crop, conversation, args.model)
                question = result.get("question")
                answer = result.get("answer")
                print(f"  [{_ts()}] Model: Q={str(question)[:80]!r}  A={str(answer)[:80]!r}")

                if not answer or answer == "null":
                    last_img = img
                    print(f"  [{_ts()}] No actionable content, skipping.")
                    print()
                    continue

                print(f"  [{_ts()}] Q ({_elapsed(t0)}): {str(question)[:60]}")
                print(f"  [{_ts()}] A: {answer[:80]}{'...' if len(answer) > 80 else ''}")

                # Add this exchange to text history for future turns
                conversation.append({"role": "user", "content": str(question)})
                conversation.append({"role": "assistant", "content": answer})

                # Place reply just below the bottom of the changed region.
                # content_y2 is in cropped-image coordinates; add 80 for the
                # stripped toolbar to get device Y, then 150px gap.
                reply_y = min(content_y2 + 80 + 60, 1750)
                print(f"  [{_ts()}] Content bottom: {content_y2}, reply_y={reply_y}")

                # Render + Inject
                print(f"  [{_ts()}] Rendering + injecting (reply_y={reply_y})...")
                actual_y = render_and_inject(ssh, answer, reply_y=reply_y)
                last_inject_y = actual_y if actual_y is not None else reply_y
                print(f"  [{_ts()}] Injected ({_elapsed(t0)})")

                # Capture a fresh baseline AFTER injection so the next
                # diff only sees genuinely new user content. Without this,
                # last_img is the pre-injection frame and every subsequent
                # diff includes our own injected strokes as "new" pixels,
                # producing a crop that mixes our reply with the user's
                # next question and confuses the model.
                print(f"  [{_ts()}] Capturing post-injection baseline...")
                try:
                    time.sleep(0.5)  # let e-ink settle
                    last_img = capture_framebuffer(ssh)
                    print(f"  [{_ts()}] Baseline updated")
                except Exception as e_cap:
                    print(f"  [{_ts()}] Baseline capture failed: {e_cap} (stale frame kept)")

                elapsed = time.time() - t0
                print(f"  [{_ts()}] Done in {elapsed:.1f}s")

            except Exception as e:
                print(f"  ERROR: {e}")

            print()

            if args.once:
                break

    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        if _device:
            _device.close()
        ssh.close()


if __name__ == "__main__":
    main()
