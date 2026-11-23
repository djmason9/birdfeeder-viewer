#!/usr/bin/env python3
"""Bird feeder viewer.

Polls the (unofficial) VicoHome cloud API for motion events from your feeder
camera, archives snapshots and clips locally before their signed URLs expire,
and serves a small web UI to browse them.

Standard library only (Python 3.8+). ffmpeg is optional but needed to save clips.

    python3 server.py            # run the poller + web UI
    python3 server.py --probe    # log in once, print what the API returns, exit

API calls are based on https://github.com/TomTje/vicohome-ha (MIT) and on
traffic captured from the VicoHome iOS app (v4.7.0). The live-view signalling
flow follows the protocol used by https://github.com/bbielsa/vicostream.
"""
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if val[:1] in ('"', "'") and val[-1:] == val[:1] and len(val) >= 2:
            val = val[1:-1]  # quoted: keep as-is (may contain '#')
        else:
            val = re.split(r"\s+#", val, 1)[0].strip()  # drop inline comment
        os.environ.setdefault(key.strip(), val)


load_env(ROOT / ".env")

EMAIL = os.environ.get("VICO_EMAIL", "")
PASSWORD = os.environ.get("VICO_PASSWORD", "")
REGION = os.environ.get("VICO_REGION", "us").lower()
COUNTRY = os.environ.get("VICO_COUNTRY", "US" if REGION == "us" else "DE")
POLL_SECONDS = max(30, int(os.environ.get("POLL_SECONDS", "120")))
BACKFILL_HOURS = int(os.environ.get("BACKFILL_HOURS", "72"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
MEDIA_DIR = DATA_DIR / "media"
STATIC_DIR = ROOT / "static"
# The current app talks to api-<region>.vicoo.tech; api-<region>.vicohome.io is
# the older host and also answered. Override with VICO_API_BASE if needed.
BASE_URL = os.environ.get("VICO_API_BASE", "https://api-%s.vicoo.tech" % REGION).rstrip("/")
SERIAL_OVERRIDE = os.environ.get("VICO_SERIAL", "").strip()
FFMPEG = shutil.which("ffmpeg")
MAX_VIDEO_ATTEMPTS = 4
EVENTS_PAGE = 50
# Browser-side recording of live view (no cloud plan needed): length of auto-recorded clips.
RECORD_SECONDS = max(5, min(120, int(os.environ.get("RECORD_SECONDS", "20"))))
RECORDING_MAX_BYTES = 300 * 1024 * 1024
WATCH_POLL_SECONDS = 30      # poll interval while a page is auto-recording (project minimum)
WATCH_GRACE_SECONDS = 90     # how long a page counts as watching after its last status request
# Follow deletions made in the VicoHome app: visits from the last PRUNE_HOURS that the cloud
# no longer lists are removed here too (re-checked every PRUNE_EVERY seconds). Older visits
# are left alone, so ones that simply age out of the cloud stay in the archive.
PRUNE_HOURS = 24
PRUNE_EVERY = 600
# Sent with every call, as the app does. Some endpoints may require it.
APP_META = {
    "bundle": "addx.ai.vicoo",
    "appName": "VicoHome",
    "tenantId": "vicoo",
    "appType": "iOS",
    "env": "product",
    "version": 34319,
    "versionName": "4.7.0",
    "timeZone": "UTC",
}


def _local_tz_name():
    # IANA name from the /etc/localtime symlink (macOS and Raspberry Pi OS).
    real = os.path.realpath("/etc/localtime")
    return real.split("zoneinfo/", 1)[1] if "zoneinfo/" in real else "UTC"


APP_META["timeZone"] = _local_tz_name()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("feeder")


# --------------------------------------------------------------------------- API


class VicoError(RuntimeError):
    pass


class VicoClient:
    def __init__(self):
        self.auth = None  # full Authorization header value, e.g. "Bearer eyJ..."
        self.lock = threading.Lock()

    def _post(self, path, body, auth=True):
        body = dict(body)
        body.setdefault("countryNo", COUNTRY)
        body.setdefault("language", "en")
        body.setdefault("requestId", uuid.uuid4().hex)
        body.setdefault("app", APP_META)
        req = urllib.request.Request(
            BASE_URL + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if auth and self.auth:
            req.add_header("Authorization", self.auth)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def login(self):
        if not EMAIL or not PASSWORD or PASSWORD == "changeme" or EMAIL == "you@example.com":
            raise VicoError("VICO_EMAIL / VICO_PASSWORD not set in .env - add them and restart")
        data = self._post(
            "/account/login",
            {"email": EMAIL, "password": PASSWORD, "loginType": 0},
            auth=False,
        )
        if data.get("result") != 0:
            raise VicoError("login failed: %s (result=%s)" % (data.get("msg"), data.get("result")))
        tok = data["data"]["token"]
        self.auth = "%s %s" % (tok.get("tokenType") or "Bearer", tok["token"])
        log.info("logged in to %s", BASE_URL)

    def call(self, path, body, relogin=True):
        with self.lock:
            if not self.auth:
                self.login()
            data = self._post(path, body)
            if data.get("result") != 0 and relogin:
                # Token expired or we were kicked by another login: retry once.
                log.info("%s -> %s (%s); logging in again", path, data.get("result"), data.get("msg"))
                self.login()
                data = self._post(path, body)
            if data.get("result") != 0:
                raise VicoError("%s failed: %s (result=%s)" % (path, data.get("msg"), data.get("result")))
            return data.get("data")

    def events(self, start, end):
        return self.events_checked(start, end)[0]

    def events_checked(self, start, end):
        """(events, complete) for [start, end], following the from/to paging the app uses.
        complete is True only when we got as many events as the server's `total`, so
        callers can trust that a missing event really is gone from the cloud."""
        out, seen, frm = [], set(), 0
        while True:
            data = self.call("/library/newselectlibrary", {
                "startTimestamp": int(start),
                "endTimestamp": int(end),
                "from": frm,
                "to": frm + EVENTS_PAGE,
                "marked": -1,
                "missing": -1,
                "tags": [],
                "doorbellTags": [],
                "deviceCallEventTag": "",
            }) or {}
            new = [e for e in (data.get("list") or []) if e.get("traceId") not in seen]
            for e in new:
                seen.add(e.get("traceId"))
            out.extend(new)
            total = data.get("total")
            frm += EVENTS_PAGE
            # Stop on an empty/duplicate page, when total is reached, or at a sanity cap.
            if not new or (isinstance(total, int) and len(seen) >= total) or frm >= 5000:
                return out, isinstance(total, int) and len(seen) >= total

    def events_window(self, start, end, chunk_hours=24):
        return self.events_window_checked(start, end, chunk_hours)[0]

    def events_window_checked(self, start, end, chunk_hours=24):
        out, complete = [], True
        t = start
        while t < end:
            e = min(t + chunk_hours * 3600, end)
            got, ok = self.events_checked(t, e)
            out.extend(got)
            complete = complete and ok
            t = e
        return out, complete

    def devices(self):
        data = self.call("/device/listuserdevices/v4", {"list": [], "supportBindFlow": 0}) or {}
        return data.get("deviceList") or []

    def device(self, serial):
        return self.call("/device/selectsingledevice",
                         {"serialNumber": serial, "list": [], "supportBindFlow": 0}) or {}

    def live_ticket(self, serial):
        """Wake the camera and get WebRTC signalling credentials (getWebrtcTicket)."""
        try:
            self.call("/device/wakeupDevice", {"serialNumber": serial, "voiceReminder": False},
                      relogin=False)
        except (VicoError, urllib.error.URLError, OSError) as err:
            log.info("live: wakeupDevice failed (continuing): %s", err)
        return self.call("/device/getWebrtcTicket", {
            "serialNumber": serial,
            "list": [],
            "supportUnlimitedWebsocket": True,
            "needWakeupDevice": False,
            "verify4GVIPStatus": True,
            "verifyDormancyStatus": True,
        }) or {}


# ----------------------------------------------------------------------- Archive


def safe_id(trace_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(trace_id))[:120]


def event_ts(ev):
    try:
        ts = float(ev.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0.0
    return ts / 1000.0 if ts > 1e12 else ts


class Archive:
    def __init__(self):
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = DATA_DIR / "events.json"
        self.lock = threading.Lock()
        self.events = {}
        if self.path.exists():
            try:
                self.events = json.loads(self.path.read_text())
            except ValueError:
                log.warning("events.json unreadable; starting fresh")
        self.device = {}
        self.serial = None
        self.status = {"last_poll": None, "last_error": None, "polling": False}
        self.rev = int(time.time())   # bumped on every save so pages know when to re-fetch events

    def save(self):
        self.rev += 1
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.events))
        tmp.replace(self.path)

    def merge(self, events):
        added = 0
        with self.lock:
            for ev in events:
                tid = ev.get("traceId")
                if not tid:
                    continue
                cur = self.events.get(tid)
                if cur is None:
                    ev["_first_seen"] = time.time()
                    ev["_local"] = {}
                    self.events[tid] = ev
                    added += 1
                else:
                    # Refresh fields (signed URLs rotate) but keep our local bookkeeping.
                    keep = {k: v for k, v in cur.items() if k.startswith("_")}
                    cur.clear()
                    cur.update(ev)
                    cur.update(keep)
                if ev.get("serialNumber") and not self.serial:
                    self.serial = ev["serialNumber"]
            self.save()
        return added

    def sorted_events(self):
        with self.lock:
            evs = list(self.events.values())
        return sorted(evs, key=event_ts, reverse=True)

    def newest(self):
        """The newest camera event (not our own recordings), for auto-record triggers."""
        with self.lock:
            best = None
            for ev in self.events.values():
                if ev.get("_synthetic"):
                    continue
                if best is None or event_ts(ev) > event_ts(best):
                    best = ev
        return {"traceId": best["traceId"], "ts": event_ts(best)} if best else None

    def attach_recording(self, trace, kind, name, ts):
        """Record a browser-made clip/still on an event. Manual recordings (trace 'rec-…')
        get an event of their own; anything else must be an event we already have."""
        with self.lock:
            ev = self.events.get(trace)
            if ev is None:
                if not trace.startswith("rec-"):
                    return False
                ev = self.events[trace] = {
                    "traceId": trace, "timestamp": int(ts), "mediaType": "video",
                    "serialNumber": self.serial, "deviceName": self.device.get("deviceName"),
                    "_synthetic": True, "_first_seen": time.time(), "_local": {},
                }
            local = ev.setdefault("_local", {})
            if kind == "video":
                old = local.get("video")
                if old and old != name and local.get("recorded"):
                    try:
                        (MEDIA_DIR / old).unlink()
                    except OSError:
                        pass
                local["video"] = name
                local["recorded"] = True
                local.pop("video_deleted", None)
            elif not local.get("image"):
                local["image"] = name
            self.save()
        return True

    def prune_deleted(self, start, end, cloud_ids):
        """Remove camera visits timed within [start, end] that the cloud no longer lists, i.e.
        ones deleted in the VicoHome app. Visits with a clip we recorded are kept (that clip
        exists only here); the Delete button removes those."""
        with self.lock:
            gone = [tid for tid, ev in self.events.items()
                    if tid not in cloud_ids and not ev.get("_synthetic")
                    and not (ev.get("_local") or {}).get("recorded")
                    and start <= event_ts(ev) <= end]
            files = []
            for tid in gone:
                local = self.events.pop(tid).get("_local") or {}
                files.extend(local[k] for k in ("image", "video") if local.get(k))
            if gone:
                self.save()
        for name in files:
            try:
                (MEDIA_DIR / name).unlink()
            except OSError:
                pass
        return len(gone)

    def delete_video(self, trace):
        """Delete an event's clip. A manual recording's whole entry goes (it has nothing else);
        a camera visit keeps its snapshot and is marked so the clip isn't downloaded again.
        Returns None if there was no clip, else whether the whole entry was removed."""
        with self.lock:
            ev = self.events.get(trace)
            local = (ev or {}).get("_local") or {}
            if not local.get("video"):
                return None
            files = [local.pop("video")]
            local.pop("recorded", None)
            removed = bool(ev.get("_synthetic"))
            if removed:
                if local.get("image"):
                    files.append(local["image"])
                del self.events[trace]
            else:
                local["video_deleted"] = True
            self.save()
        for name in files:
            try:
                (MEDIA_DIR / name).unlink()
            except OSError:
                pass
        log.info("deleted clip of %s%s", safe_id(trace), " and its entry" if removed else "")
        return removed

    def fetch_media(self):
        for ev in self.sorted_events():
            local = ev.setdefault("_local", {})
            sid = safe_id(ev["traceId"])
            changed = False

            if not local.get("image") and ev.get("imageUrl"):
                dest = MEDIA_DIR / (sid + ".jpg")
                try:
                    download(ev["imageUrl"], dest)
                    local["image"] = dest.name
                    changed = True
                except Exception as err:  # noqa: BLE001
                    log.info("image %s not saved yet: %s", sid, err)

            attempts = local.get("video_attempts", 0)
            if (FFMPEG and not local.get("video") and ev.get("videoUrl") and not local.get("video_deleted")
                    and attempts < MAX_VIDEO_ATTEMPTS):
                dest = MEDIA_DIR / (sid + ".mp4")
                local["video_attempts"] = attempts + 1
                changed = True
                if save_hls(ev["videoUrl"], dest):
                    local["video"] = dest.name

            if changed:
                with self.lock:
                    self.save()


def download(url, dest):
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    if tmp.stat().st_size == 0:
        tmp.unlink()
        raise IOError("empty download")
    tmp.replace(dest)


def save_hls(url, dest):
    tmp = dest.with_name(dest.stem + ".part.mp4")
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-fflags", "+discardcorrupt",
           "-i", url, "-c", "copy", "-movflags", "+faststart", str(tmp)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        log.info("ffmpeg timed out for %s", dest.name)
        if tmp.exists():
            tmp.unlink()
        return False
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        log.info("clip %s not saved yet: %s", dest.name,
                 proc.stderr.decode("utf-8", "replace").strip()[-200:])
        if tmp.exists():
            tmp.unlink()
        return False
    tmp.replace(dest)
    log.info("saved clip %s", dest.name)
    return True


def finish_recording(src, dest):
    """Turn a browser recording into a seekable MP4 that Safari can play.

    Safari records MP4 (remux, audio to AAC); Chrome/Firefox record WebM (transcode).
    Without ffmpeg, or if it fails, the upload is kept as is. Returns the final path."""
    if FFMPEG:
        tmp = dest.with_name(dest.stem + ".part.mp4")
        if src.suffix == ".mp4":
            codec = ["-c:v", "copy", "-c:a", "aac"]
        else:
            codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                     "-pix_fmt", "yuv420p", "-c:a", "aac"]
        cmd = ([FFMPEG, "-y", "-loglevel", "error", "-i", str(src)] + codec
               + ["-movflags", "+faststart", str(tmp)])
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(dest)
                src.unlink()
                return dest
            log.info("recording %s kept as uploaded: %s", src.name,
                     proc.stderr.decode("utf-8", "replace").strip()[-200:])
        except subprocess.TimeoutExpired:
            log.info("ffmpeg timed out converting %s; kept as uploaded", src.name)
        if tmp.exists():
            tmp.unlink()
    final = dest.with_suffix(src.suffix)
    src.replace(final)
    return final


# ------------------------------------------------------------------------ Poller


client = VicoClient()
archive = Archive()
wake = threading.Event()
watch_until = [0.0]   # a page is auto-recording until this time (it renews on each status request)
last_prune = [0.0]    # when the PRUNE_HOURS window was last checked against the cloud


def mark_watched():
    now = time.time()
    was_watched = now < watch_until[0]
    watch_until[0] = now + WATCH_GRACE_SECONDS
    if not was_watched and POLL_SECONDS > WATCH_POLL_SECONDS:
        wake.set()   # switch to the faster interval now rather than after the current wait


def find_serial():
    if SERIAL_OVERRIDE:
        return SERIAL_OVERRIDE
    devs = client.devices()
    if devs:
        if len(devs) > 1:
            log.info("%d devices on the account; using the first (set VICO_SERIAL to choose)", len(devs))
        return devs[0].get("serialNumber")
    return None


def poll_once(first):
    now = time.time()
    if not archive.serial:
        try:
            archive.serial = find_serial()
        except (VicoError, urllib.error.URLError, OSError, ValueError) as err:
            log.info("device list failed: %s", err)
    prune_due = now - last_prune[0] >= PRUNE_EVERY
    if first:
        window = BACKFILL_HOURS * 3600
    elif prune_due:
        window = PRUNE_HOURS * 3600
    else:
        window = max(POLL_SECONDS * 3, 3600)
    start = now - window
    events, complete = client.events_window_checked(start, now)
    added = archive.merge(events)
    if added:
        log.info("%d new event(s), %d total", added, len(archive.events))
    if complete:
        # Small margin at the window's old edge so a boundary rounding can't drop a visit.
        pruned = archive.prune_deleted(max(start, now - PRUNE_HOURS * 3600) + 60, now,
                                       set(e.get("traceId") for e in events))
        if pruned:
            log.info("%d visit(s) deleted in the VicoHome app removed here, %d left", pruned, len(archive.events))
    if complete and window >= PRUNE_HOURS * 3600:
        last_prune[0] = now   # an incomplete fetch leaves the 24 h check due for the next poll
    if archive.serial:
        try:
            archive.device = client.device(archive.serial)
        except Exception as err:  # noqa: BLE001
            log.info("device details failed: %s", err)
    archive.fetch_media()


def poller():
    first = True
    while True:
        archive.status["polling"] = True
        try:
            poll_once(first)
            first = False
            archive.status["last_error"] = None
        except (VicoError, urllib.error.URLError, OSError, ValueError) as err:
            log.warning("poll failed: %s", err)
            archive.status["last_error"] = str(err)
        finally:
            archive.status["polling"] = False
            archive.status["last_poll"] = time.time()
        watched = time.time() < watch_until[0]
        wake.wait(min(POLL_SECONDS, WATCH_POLL_SECONDS) if watched else POLL_SECONDS)
        wake.clear()


# ------------------------------------------------------------- Live (WebSocket)
#
# Live view is WebRTC between the browser and the camera, through VicoHome's
# TURN relays. The browser can't talk to the signalling server directly (it
# needs WebSocket ping frames and the ticket's access token), so this server
# relays: browser <-ws-> /api/live/ws <-wss-> signalServer/iot-channel/connect.
# The server sends AUTH and pings; the browser drives JOIN_LIVE / SDP / ICE.

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10
live_lock = threading.Lock()


def _read_exact(rf, n):
    buf = b""
    while len(buf) < n:
        chunk = rf.read(n - len(buf))
        if not chunk:
            raise EOFError("websocket closed")
        buf += chunk
    return buf


def ws_send(write, lock, opcode, payload=b"", mask=False):
    if isinstance(payload, str):
        payload = payload.encode()
    n = len(payload)
    head = bytes([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        head += key
    with lock:
        write(head + payload)


def ws_recv(rf, on_ping):
    """Return (opcode, payload) for the next data or close message."""
    parts, first_op = [], None
    while True:
        b1, b2 = _read_exact(rf, 2)
        fin, op, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _read_exact(rf, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _read_exact(rf, 8))[0]
        key = _read_exact(rf, 4) if b2 & 0x80 else None
        data = _read_exact(rf, n)
        if key:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        if op == OP_PING:
            on_ping(data)
            continue
        if op == OP_PONG:
            continue
        if op == OP_CLOSE:
            return OP_CLOSE, data
        if op != OP_CONT:
            first_op = op
        parts.append(data)
        if fin:
            return first_op, b"".join(parts)


class Upstream:
    """Minimal WebSocket client (TLS) for the VicoHome signalling server."""

    def __init__(self, url):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("wss", "ws"):
            raise VicoError("unexpected signal server scheme: %s" % u.scheme)
        host, port = u.hostname, u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=15)
        self.sock = (ssl.create_default_context().wrap_socket(raw, server_hostname=host)
                     if u.scheme == "wss" else raw)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n") % (u.path or "/", host, key)
        self.sock.sendall(req.encode())
        self.rf = self.sock.makefile("rb")
        status = self.rf.readline().decode("latin-1").strip()
        while self.rf.readline() not in (b"\r\n", b"\n", b""):
            pass
        if " 101 " not in status + " ":
            self.close()
            raise VicoError("signal server refused WebSocket: %s" % status)
        self.sock.settimeout(None)
        self.lock = threading.Lock()

    def send(self, opcode, payload=b""):
        ws_send(self.sock.sendall, self.lock, opcode, payload, mask=True)

    def recv(self):
        return ws_recv(self.rf, lambda d: self.send(OP_PONG, d))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def ice_servers_for_browser(ticket):
    out = []
    for s in ticket.get("iceServer") or []:
        if s.get("url"):
            entry = {"urls": s["url"]}
            if s.get("username"):
                entry["username"] = s["username"]
                entry["credential"] = s.get("credential", "")
            out.append(entry)
    return out


# -------------------------------------------------------------------------- HTTP


MEDIA_RE = re.compile(r"^/media/([A-Za-z0-9_-]+\.(?:jpg|mp4|webm))$")
MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".jpg": "image/jpeg"}
RECORDING_EXTS = {"video": ("mp4", "webm"), "image": ("jpg",)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def same_origin(self):
        # WebSocket and simple POSTs aren't covered by same-origin rules; refuse cross-site pages.
        origin = self.headers.get("Origin")
        return not origin or urllib.parse.urlsplit(origin).netloc == self.headers.get("Host")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/events":
            return self.send_json(archive.sorted_events())
        if path == "/api/status":
            watching = "watch=1" in query.split("&")
            if watching:
                mark_watched()
            return self.send_json({
                "device": archive.device,
                "serial": archive.serial,
                "status": archive.status,
                "event_count": len(archive.events),
                "rev": archive.rev,
                "newest": archive.newest(),
                "poll_seconds": min(POLL_SECONDS, WATCH_POLL_SECONDS) if watching else POLL_SECONDS,
                "record_seconds": RECORD_SECONDS,
                "ffmpeg": bool(FFMPEG),
                "live": bool(archive.serial),
            })
        if path == "/api/live/ws":
            return self.live_ws()
        m = MEDIA_RE.match(path)
        if m:
            return self.send_media(MEDIA_DIR / m.group(1))
        return super().do_GET()

    # ---- live view relay

    def live_ws(self):
        if self.headers.get("Upgrade", "").lower() != "websocket" or not self.headers.get("Sec-WebSocket-Key"):
            return self.send_error(400, "WebSocket upgrade required")
        if not self.same_origin():
            return self.send_error(403, "cross-origin WebSocket refused")
        accept = base64.b64encode(hashlib.sha1(
            (self.headers["Sec-WebSocket-Key"].strip() + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True

        block = threading.Lock()

        def to_browser(obj):
            ws_send(self.wfile.write, block, OP_TEXT, json.dumps(obj))

        if not live_lock.acquire(blocking=False):
            to_browser({"method": "ERROR", "message": "A live view is already open in another tab."})
            return ws_send(self.wfile.write, block, OP_CLOSE)
        up = None
        stop = threading.Event()
        try:
            serial = archive.serial
            if not serial:
                to_browser({"method": "ERROR", "message": "Camera serial not known yet - wait for the first poll."})
                return
            to_browser({"method": "STATUS", "message": "Waking the camera…"})
            ticket = client.live_ticket(serial)
            if not ticket.get("signalServer") or not ticket.get("accessToken"):
                to_browser({"method": "ERROR", "message": "No live ticket returned (camera asleep or offline?)"})
                return
            to_browser({
                "method": "TICKET",
                "serial": serial,
                "id": ticket.get("id"),
                "groupId": ticket.get("groupId"),
                "traceId": ticket.get("traceId"),
                "iceServers": ice_servers_for_browser(ticket),
                "appStopLiveTimeout": ticket.get("appStopLiveTimeout"),
            })
            to_browser({"method": "STATUS", "message": "Connecting to VicoHome signalling…"})
            sig = urllib.parse.urlsplit(ticket["signalServer"])
            url = urllib.parse.urlunsplit((sig.scheme, sig.netloc, ticket.get("websocketPath") or sig.path or "/", "", ""))
            try:
                up = Upstream(url)
            except OSError as err:
                raise VicoError("could not reach signalling server: %s" % err)
            up.send(OP_TEXT, json.dumps({
                "method": "AUTH", "clientType": "app", "status": "normal",
                "accessToken": ticket["accessToken"], "id": ticket.get("id"),
            }))
            log.info("live: signalling connected for %s", serial)

            interval = ticket.get("signalPingInterval")
            interval = min(30, max(1, int(interval))) if isinstance(interval, (int, float)) else 10

            def pinger():
                while not stop.wait(interval):
                    try:
                        up.send(OP_PING)
                    except OSError:
                        return

            def upstream_to_browser():
                try:
                    while not stop.is_set():
                        op, data = up.recv()
                        if op == OP_CLOSE:
                            to_browser({"method": "ERROR", "message": "Signalling server closed the connection."})
                            break
                        if op == OP_TEXT:
                            try:
                                log.info("live: <- %s", json.loads(data).get("method"))
                            except ValueError:
                                pass
                            ws_send(self.wfile.write, block, OP_TEXT, data)
                except (EOFError, OSError):
                    pass
                finally:
                    stop.set()
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            threading.Thread(target=pinger, daemon=True).start()
            threading.Thread(target=upstream_to_browser, daemon=True).start()

            # Browser -> signalling server.
            while not stop.is_set():
                op, data = ws_recv(self.rfile, lambda d: ws_send(self.wfile.write, block, OP_PONG, d))
                if op == OP_CLOSE:
                    break
                if op == OP_TEXT:
                    try:
                        log.info("live: -> %s", json.loads(data).get("method"))
                    except ValueError:
                        continue
                    up.send(OP_TEXT, data)
        except (VicoError, urllib.error.URLError, ValueError) as err:
            log.info("live: failed: %s", err)
            try:
                to_browser({"method": "ERROR", "message": str(err)})
            except OSError:
                pass
        except (EOFError, OSError):
            pass
        finally:
            stop.set()
            if up:
                up.close()
            live_lock.release()
            log.info("live: session ended")
            try:
                ws_send(self.wfile.write, block, OP_CLOSE)
            except OSError:
                pass

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path == "/api/refresh":
            wake.set()
            return self.send_json({"ok": True})
        if path == "/api/recording":
            return self.save_recording(urllib.parse.parse_qs(query))
        self.send_error(404)

    def do_DELETE(self):
        path, _, query = self.path.partition("?")
        if path != "/api/video":
            return self.send_error(404)
        if not self.same_origin():
            return self.send_error(403, "cross-origin delete refused")
        trace = (urllib.parse.parse_qs(query).get("trace") or [""])[0]
        removed = archive.delete_video(trace) if trace else None
        if removed is None:
            return self.send_error(404, "no clip for that visit")
        return self.send_json({"ok": True, "entry_removed": removed})

    def save_recording(self, q):
        """POST /api/recording?trace=&kind=video|image&ext=mp4|webm|jpg&ts= with the raw file
        as the body: a clip (or still) the page recorded from live view."""
        if not self.same_origin():
            return self.send_error(403, "cross-origin upload refused")
        arg = lambda k: (q.get(k) or [""])[0]
        trace, kind, ext = arg("trace"), arg("kind"), arg("ext")
        if not trace or ext not in RECORDING_EXTS.get(kind, ()):
            return self.send_error(400, "bad recording parameters")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            ts = float(arg("ts") or time.time())
        except ValueError:
            return self.send_error(400, "bad recording parameters")
        if length <= 0 or length > RECORDING_MAX_BYTES:
            return self.send_error(413, "recording empty or too large")
        if trace not in archive.events and not trace.startswith("rec-"):
            return self.send_error(404, "unknown event")
        if kind == "image" and not trace.startswith("rec-"):
            return self.send_error(400, "camera events keep their own snapshot")
        sid = safe_id(trace)
        upload = MEDIA_DIR / ("%s.upload.%s" % (sid, ext))
        remaining = length
        with open(upload, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining:
            upload.unlink()
            return self.send_error(400, "upload cut short")
        if kind == "video":
            final = finish_recording(upload, MEDIA_DIR / (sid + "-rec.mp4"))
        else:
            final = MEDIA_DIR / (sid + ".jpg")
            upload.replace(final)
        if not archive.attach_recording(trace, kind, final.name, ts):
            final.unlink()
            return self.send_error(404, "unknown event")
        log.info("saved recording %s (%d KB)", final.name, length // 1024)
        return self.send_json({"ok": True, "file": final.name})

    def send_media(self, file):
        if not file.is_file():
            return self.send_error(404)
        size = file.stat().st_size
        ctype = MEDIA_TYPES.get(file.suffix, "application/octet-stream")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                return self.end_headers()
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        with open(file, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


# ------------------------------------------------------------------------- Probe


PROBE_HIDE = re.compile(r"email|phone|token|password|secret|credential|sign", re.I)


def probe_view(obj, key=""):
    """Shorten long values and hide personal/secret ones so probe output is safe to share."""
    if isinstance(obj, dict):
        return {k: probe_view(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [probe_view(v, key) for v in obj[:5]]
    if isinstance(obj, str) and obj and PROBE_HIDE.search(key):
        return "<hidden>"
    if isinstance(obj, str) and obj.startswith("http") and len(obj) > 80:
        return obj[:80] + "..."
    return obj


def probe():
    client.login()
    print("API: %s" % BASE_URL)
    devs = client.devices()
    print("\nDevices on the account: %d" % len(devs))
    for d in devs:
        print("  %s  %s  online=%s battery=%s signal=%s firmware=%s stream=%s" % (
            d.get("serialNumber"), d.get("deviceName"), d.get("online"), d.get("batteryLevel"),
            d.get("signalStrength"), d.get("firmwareId"),
            (d.get("deviceModel") or {}).get("streamProtocol")))
    now = time.time()
    events = client.events_window(now - 24 * 3600, now)
    print("\nEvents in the last 24h: %d" % len(events))
    if events:
        print("Event fields:", ", ".join(sorted(events[0].keys())))
        print("\nNewest event:\n" + json.dumps(probe_view(events[0]), indent=2))
    else:
        print("No events yet - wave at the camera to trigger one, then run --probe again.")


def main():
    if "--probe" in sys.argv:
        try:
            probe()
        except (VicoError, urllib.error.URLError) as err:
            sys.exit("probe failed: %s" % err)
        return
    if not FFMPEG:
        log.warning("ffmpeg not found: snapshots will be saved, clips will not")
    threading.Thread(target=poller, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("viewer at http://%s:%d", "localhost" if HOST in ("127.0.0.1", "0.0.0.0") else HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
