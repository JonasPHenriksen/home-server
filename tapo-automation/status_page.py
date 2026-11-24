#!/home/dietpi/tapo-automation/tapo-env/bin/python3
"""
status_page.py - a small web page for the camera system.

What Frigate's recordings view gave, without Frigate: the state of the house
and of every recorder, a per-day timeline of what was recorded, thumbnails,
playback in the browser and a download button for the clip on screen. It
reads the same files the other scripts write and serves the MP4 segments
cam_recorder.py produces. Standard library only; ffmpeg makes the thumbnails.

Usage:
    status_page.py [--base-dir DIR] [--check]

Configuration, in config.json:
    "status_page": {
        "port": 8090,
        "bind": "0.0.0.0",
        "auth": "user:password",      # HTTP basic auth; empty or absent = open
        "thumbnails": true,           # needs ffmpeg
        "ffmpeg": null,               # binary for thumbnails; null = the recorder's
        "tls_cert": null,             # certificate and key for HTTPS; null = plain HTTP
        "tls_key": null
    }

Recording happens only while the house is empty (the cameras are in privacy
mode otherwise), so a day with people home shows an empty timeline; that is
the system working, not a gap.
"""
import argparse
import base64
import email.utils
import hmac
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cam_recorder import DEFAULT_BASE_DIR, SEGMENT_RE, ConfigError, load_config, read_control  # noqa: E402

log = logging.getLogger("status")

PAGE_DEFAULTS = {"port": 8090, "bind": "0.0.0.0", "auth": "", "thumbnails": True, "ffmpeg": None,
                 "tls_cert": None, "tls_key": None}
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CAM_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
THUMB_DIR = ".thumbs"


# --------------------------------------------------------------------------- config

def load_page_config(base_dir):
    with open(os.path.join(base_dir, "config.json")) as f:
        data = json.load(f)
    raw = data.get("status_page", {})
    if not isinstance(raw, dict):
        raise ConfigError("status_page must be an object")
    unknown = set(raw) - set(PAGE_DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown status_page option(s): {', '.join(sorted(unknown))}")
    cfg = dict(PAGE_DEFAULTS, **raw)
    if not isinstance(cfg["port"], int) or not 1 <= cfg["port"] <= 65535:
        raise ConfigError("status_page.port must be a port number")
    if cfg["auth"] and ":" not in cfg["auth"]:
        raise ConfigError("status_page.auth must be user:password")
    if cfg["ffmpeg"] is not None and not isinstance(cfg["ffmpeg"], str):
        raise ConfigError("status_page.ffmpeg must be a path or null")
    tls = (cfg["tls_cert"], cfg["tls_key"])
    if any(v is not None and not isinstance(v, str) for v in tls) or (tls[0] is None) != (tls[1] is None):
        raise ConfigError("status_page.tls_cert and tls_key must both be paths, or both null")
    return cfg


# --------------------------------------------------------------------------- the model

class Site:
    """Everything the handlers need, built once from the base directory."""

    def __init__(self, base_dir, cfg, page):
        self.base_dir = base_dir
        self.cfg = cfg
        self.page = page
        self.cameras = [c.name for c in cfg.cameras]
        self.root = cfg.root
        self.thumb_root = os.path.join(cfg.root, THUMB_DIR)
        self.ffmpeg = (page["ffmpeg"] or cfg.ffmpeg) if page["thumbnails"] else None
        self._thumb_lock = threading.Semaphore(1)
        self._night_cache = (0.0, "")
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_page.html")) as f:
            self.index_html = f.read()

    # -- state files

    def _read(self, name, default=""):
        try:
            with open(os.path.join(self.base_dir, name)) as f:
                return f.read().strip()
        except OSError:
            return default

    def _exists(self, name):
        return os.path.exists(os.path.join(self.base_dir, name))

    def night_status(self):
        now = time.time()
        at, text = self._night_cache
        if now - at < 30:
            return text
        script = os.path.join(self.base_dir, "night_mode.sh")
        text = "night mode: unknown"
        if os.access(script, os.X_OK):
            try:
                out = subprocess.run([script, "status"], capture_output=True, text=True, timeout=5)
                text = (out.stdout.strip().splitlines() or [text])[0]
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._night_cache = (now, text)
        return text

    def status(self):
        state = self._read(self.cfg.control_file and os.path.basename(self.cfg.control_file) or ".tapo_motion_state")
        try:
            recorder = json.load(open(os.path.join(self.root, "status.json")))
        except (OSError, ValueError):
            recorder = {"cameras": {}, "recording_wanted": None, "updated": None}
        try:
            usage = shutil.disk_usage(self.root)
            free = usage.free
        except OSError:
            free = None
        fails = self._read(".tapo_ping_fail_count", "0")
        threshold = self._read(".tapo_away_threshold", "3")
        return {
            "presence": {
                "state": state,                      # enable = away, disable = home
                "away": state == self.cfg.record_when,
                "phones_missing_checks": int(fails) if fails.isdigit() else 0,
                "away_threshold": int(threshold) if threshold.isdigit() else 3,
                "switch_pending": self._exists(".tapo_pending"),
                "probe_error": self._exists(".tapo_probe_error"),
            },
            "night": self.night_status(),
            "recorder": recorder,
            "recording_wanted": read_control(self.cfg.control_file, self.cfg.record_when, False),
            "cameras": self.cameras,
            "retain_days": self.cfg.retain_days,
            "segment_seconds": self.cfg.segment_seconds,
            "disk": {"recordings_bytes": self._tree_size(), "free_bytes": free},
            "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }

    def _tree_size(self):
        total = 0
        for cam in self.cameras:
            try:
                with os.scandir(os.path.join(self.root, cam)) as it:
                    for e in it:
                        if SEGMENT_RE.match(e.name):
                            try:
                                total += e.stat().st_size
                            except OSError:
                                pass
            except FileNotFoundError:
                pass
        return total

    # -- recordings

    def segments(self, cam):
        """[(name, size, mtime)] for one camera, sorted by name (chronological)."""
        out = []
        try:
            with os.scandir(os.path.join(self.root, cam)) as it:
                for e in it:
                    if SEGMENT_RE.match(e.name):
                        try:
                            st = e.stat()
                        except OSError:
                            continue
                        out.append((e.name, st.st_size, st.st_mtime))
        except FileNotFoundError:
            pass
        out.sort()
        return out

    def days(self):
        per_day = {}
        for cam in self.cameras:
            for name, size, _ in self.segments(cam):
                d = per_day.setdefault(name[:10], {"date": name[:10], "segments": 0, "bytes": 0, "cameras": set()})
                d["segments"] += 1
                d["bytes"] += size
                d["cameras"].add(cam)
        out = []
        for d in sorted(per_day.values(), key=lambda x: x["date"], reverse=True):
            d["cameras"] = sorted(d["cameras"])
            out.append(d)
        return out

    def day(self, date):
        now = time.time()
        result = {"date": date, "cameras": {}}
        for cam in self.cameras:
            clips = []
            for name, size, mtime in self.segments(cam):
                if not name.startswith(date):
                    continue
                start = time.mktime(time.strptime(name[:-4], "%Y-%m-%d_%H-%M-%S"))
                # a segment lasts segment_seconds unless it was cut short: the
                # last write is where it ended
                duration = max(1, min(self.cfg.segment_seconds, int(round(mtime - start))))
                live = now - mtime < 30 and name == self.segments(cam)[-1][0]
                clips.append({
                    "name": name,
                    "start": name[11:19].replace("-", ":"),
                    "start_s": int(start - time.mktime(time.strptime(date, "%Y-%m-%d"))),
                    "duration": duration,
                    "bytes": size,
                    "live": live,
                })
            result["cameras"][cam] = clips
        return result

    def clip_path(self, cam, name):
        """Absolute path of a clip, or None if the request is not a clip we own."""
        if cam not in self.cameras or not SEGMENT_RE.match(name):
            return None
        path = os.path.join(self.root, cam, name)
        return path if os.path.isfile(path) else None

    def thumb_path(self, cam, name):
        """Cached JPEG for a clip, generated on first request. None if impossible."""
        clip = self.clip_path(cam, name)
        if clip is None or not self.ffmpeg:
            return None
        out = os.path.join(self.thumb_root, cam, name[:-4] + ".jpg")
        if os.path.isfile(out):
            return out
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp.jpg"
        with self._thumb_lock:                       # one ffmpeg at a time
            if os.path.isfile(out):
                return out
            for seek in ("2", None):                 # a very short clip may not reach 2 s
                cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
                if seek:
                    cmd += ["-ss", seek]
                cmd += ["-i", clip, "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6", "-y", tmp]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=20, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    break
                if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                    os.replace(tmp, out)
                    return out
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None

    def prune_thumbs(self):
        """Drop thumbnails whose clip is gone (the recorder prunes clips, not these)."""
        removed = 0
        for cam in self.cameras:
            d = os.path.join(self.thumb_root, cam)
            try:
                names = os.listdir(d)
            except FileNotFoundError:
                continue
            for n in names:
                if n.endswith(".jpg") and not os.path.isfile(os.path.join(self.root, cam, n[:-4] + ".mp4")):
                    try:
                        os.remove(os.path.join(d, n))
                        removed += 1
                    except OSError:
                        pass
        return removed

    def tail_logs(self, n=40):
        out = {}
        for label, rel in (("presence", "tapo.log"), ("recorder", os.path.join("logs", "cam_recorder.log"))):
            try:
                with open(os.path.join(self.base_dir, rel), "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - 16384))
                    lines = f.read().decode("utf-8", "replace").splitlines()
                out[label] = lines[-n:]
            except OSError:
                out[label] = []
        return out


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "tapo-status/1"
    site = None      # set on the server class
    timeout = 30     # a client that connects and then sends nothing is dropped, not waited for forever

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    # -- helpers

    def _auth_ok(self):
        want = self.site.page["auth"]
        if not want:
            return True
        got = self.headers.get("Authorization", "")
        if not got.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(got[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(decoded.encode(), want.encode())

    def _send(self, status, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=HTTPStatus.OK):
        self._send(status, json.dumps(obj, default=list), "application/json")

    def _file(self, path, ctype, download_name=None, cache=False):
        """Serve a file with Range support (Safari will not play video without it)."""
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            self._send(HTTPStatus.NOT_FOUND, "not found")
            return
        with f:
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            status = HTTPStatus.OK
            if rng and rng.startswith("bytes="):
                m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
                if not m:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                a, b = m.groups()
                if a == "" and b == "":
                    pass
                elif a == "":
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    if b:
                        end = min(size - 1, int(b))
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", email.utils.formatdate(os.path.getmtime(path), usegmt=True))
            self.send_header("Cache-Control", "private, max-age=86400" if cache else "no-store")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download_name:
                self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # -- routes

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._auth_ok():
            self._send(HTTPStatus.UNAUTHORIZED, "login required",
                       extra={"WWW-Authenticate": 'Basic realm="cameras", charset="UTF-8"'})
            return
        url = urllib.parse.urlsplit(self.path)
        parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
        query = urllib.parse.parse_qs(url.query)
        site = self.site
        try:
            if not parts:
                self._send(HTTPStatus.OK, site.index_html, "text/html; charset=utf-8")
            elif parts == ["api", "status"]:
                self._json(site.status())
            elif parts == ["api", "days"]:
                self._json(site.days())
            elif len(parts) == 3 and parts[:2] == ["api", "day"] and DAY_RE.match(parts[2]):
                self._json(site.day(parts[2]))
            elif parts == ["api", "log"]:
                self._json(site.tail_logs())
            elif len(parts) == 3 and parts[0] == "clip":
                path = site.clip_path(parts[1], parts[2])
                if path is None:
                    self._send(HTTPStatus.NOT_FOUND, "no such clip")
                    return
                name = f"{parts[1]}_{parts[2]}" if "download" in query else None
                self._file(path, "video/mp4", download_name=name)
            elif len(parts) == 3 and parts[0] == "thumb":
                if not CAM_RE.match(parts[1]) or not parts[2].endswith(".jpg"):
                    self._send(HTTPStatus.NOT_FOUND, "no such thumbnail")
                    return
                path = site.thumb_path(parts[1], parts[2][:-4] + ".mp4")
                if path is None:
                    self._send(HTTPStatus.NOT_FOUND, "no thumbnail")
                    return
                self._file(path, "image/jpeg", cache=True)
            else:
                self._send(HTTPStatus.NOT_FOUND, "not found")
        except Exception:  # one bad request must not take the page down
            log.exception("request %s failed", self.path)
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "error")


class TLSServer(ThreadingHTTPServer):
    """HTTPS. The handshake runs in the connection's own thread, so a slow or
    refusing client cannot hold up the others."""

    def __init__(self, address, handler, tls):
        super().__init__(address, handler)
        self.tls = tls

    def finish_request(self, request, client_address):
        request.settimeout(Handler.timeout)
        try:
            conn = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return      # plain http://, or a browser that does not trust the certificate yet
        try:
            self.RequestHandlerClass(conn, client_address, self)
        finally:
            conn.close()


def make_server(base_dir, port=None, bind=None):
    cfg = load_config(base_dir)
    page = load_page_config(base_dir)
    site = Site(base_dir, cfg, page)
    handler = type("BoundHandler", (Handler,), {"site": site})
    address = (bind if bind is not None else page["bind"], port if port is not None else page["port"])
    if page["tls_cert"]:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(page["tls_cert"], page["tls_key"])
        srv = TLSServer(address, handler, tls)
    else:
        srv = ThreadingHTTPServer(address, handler)
    srv.daemon_threads = True
    srv.site = site
    return srv


def housekeeping(site, stop):
    """Hourly: drop thumbnails of clips the recorder has deleted."""
    while not stop.wait(3600):
        try:
            n = site.prune_thumbs()
            if n:
                log.info("removed %d orphaned thumbnail(s)", n)
        except Exception:
            log.exception("thumbnail housekeeping failed")


def check(base_dir):
    cfg = load_config(base_dir)
    page = load_page_config(base_dir)
    print(f"listen:        {'https' if page['tls_cert'] else 'http'}://{page['bind']}:{page['port']}")
    print(f"auth:          {'basic, user ' + page['auth'].split(':')[0] if page['auth'] else 'OFF (anyone on the network)'}")
    print(f"recordings:    {cfg.root}")
    print(f"cameras:       {', '.join(c.name for c in cfg.cameras)}")
    ff = (page["ffmpeg"] or cfg.ffmpeg) if page["thumbnails"] else None
    print(f"thumbnails:    {ff if ff and (shutil.which(ff) or os.access(ff, os.X_OK)) else 'off'}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base-dir", default=os.environ.get("TAPO_BASE_DIR", DEFAULT_BASE_DIR))
    p.add_argument("--check", action="store_true", help="print the resolved configuration and exit")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [status] %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    try:
        if args.check:
            return check(args.base_dir)
        srv = make_server(args.base_dir)
    except (ConfigError, OSError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    site = srv.site
    n = site.prune_thumbs()
    log.info("status page on %s://%s:%d, %d camera(s), auth %s, thumbnails %s%s",
             "https" if site.page["tls_cert"] else "http", srv.server_address[0], srv.server_address[1],
             len(site.cameras),
             "on" if site.page["auth"] else "OFF", "on" if site.ffmpeg else "off",
             f", pruned {n} thumbnail(s)" if n else "")
    stop = threading.Event()
    threading.Thread(target=housekeeping, args=(site, stop), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
