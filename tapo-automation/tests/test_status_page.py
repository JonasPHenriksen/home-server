"""
status_page.py: the camera web page, run against a real server on a random
port with fake state files and (where ffmpeg exists) a real tiny clip.
"""
import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

import status_page as sp
from conftest import write_config

FFMPEG = shutil.which("ffmpeg")


def make_clip(base, cam, name, size=1000, age=0, real=False):
    d = base / "recordings" / cam
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    if real and FFMPEG:
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                        "-t", "3", "-pix_fmt", "yuv420p", "-y", str(p)], check=True, timeout=60)
    else:
        with open(p, "wb") as f:
            f.write(b"\0" * size)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


@pytest.fixture
def site(tmp_path):
    """A running server; returns (base, url, stop)."""
    write_config(tmp_path, recording={"segment_seconds": 600})
    cfg = json.loads((tmp_path / "config.json").read_text())
    # the recorder config points at tests/fake_ffmpeg.py, which never exits; thumbnails need the real one
    cfg["status_page"] = {"port": 8090, "bind": "127.0.0.1", "auth": "", "thumbnails": True, "ffmpeg": FFMPEG or "/bin/false"}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    (tmp_path / "night_mode.sh").write_text("#!/bin/bash\necho 'night mode: ON (22:00-06:00)'\n")
    (tmp_path / "night_mode.sh").chmod(0o755)
    servers = []

    def start(auth=""):
        c = json.loads((tmp_path / "config.json").read_text())
        c["status_page"]["auth"] = auth
        (tmp_path / "config.json").write_text(json.dumps(c))
        srv = sp.make_server(str(tmp_path), port=0)   # any free port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield tmp_path, start
    for s in servers:
        s.shutdown()
        s.server_close()


def get(url, headers=None, method="GET"):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_index_and_status_reflect_the_state_files(site):
    base, start = site
    url = start()
    (base / ".tapo_motion_state").write_text("enable\n")
    (base / ".tapo_ping_fail_count").write_text("7\n")
    (base / ".tapo_away_threshold").write_text("18\n")
    (base / ".tapo_pending").touch()
    (base / "recordings").mkdir(exist_ok=True)
    (base / "recordings" / "status.json").write_text(json.dumps({
        "recording_wanted": True, "updated": "x",
        "cameras": {"cam_1": {"state": "recording", "failures": 0, "pid": 5, "running_for_seconds": 754,
                                 "retry_in_seconds": None, "last_error": None}}}))
    code, hdr, body = get(url + "/")
    assert code == 200 and "text/html" in hdr["Content-Type"] and b"Cameras" in body and b"timeline" in body
    code, _, body = get(url + "/api/status")
    s = json.loads(body)
    assert code == 200
    assert s["presence"] == {"state": "enable", "away": True, "phones_missing_checks": 7, "away_threshold": 18,
                             "switch_pending": True, "probe_error": False}
    assert s["night"] == "night mode: ON (22:00-06:00)"
    assert s["recorder"]["cameras"]["cam_1"]["state"] == "recording"
    assert s["recording_wanted"] is True
    assert s["cameras"] == ["cam_1", "cam_2", "cam_3"]
    assert s["retain_days"] == 14 and s["segment_seconds"] == 600


def test_days_and_day_listing_with_duration_estimates(site):
    base, start = site
    url = start()
    now = time.time()
    make_clip(base, "cam_1", "2026-09-12_15-10-00.mp4", size=5000, age=now - (time.mktime(time.strptime("2026-09-12 15:20:00", "%Y-%m-%d %H:%M:%S"))))
    make_clip(base, "cam_1", "2026-09-12_15-20-00.mp4", size=3000, age=now - (time.mktime(time.strptime("2026-09-12 15:22:30", "%Y-%m-%d %H:%M:%S"))))
    make_clip(base, "cam_3", "2026-09-11_02-00-00.mp4", size=100, age=now - (time.mktime(time.strptime("2026-09-11 02:10:00", "%Y-%m-%d %H:%M:%S"))))
    (base / "recordings" / "cam_1" / "notes.txt").write_text("not a clip")
    code, _, body = get(url + "/api/days")
    days = json.loads(body)
    assert [d["date"] for d in days] == ["2026-09-12", "2026-09-11"], "newest first"
    assert days[0] == {"date": "2026-09-12", "segments": 2, "bytes": 8000, "cameras": ["cam_1"]}
    code, _, body = get(url + "/api/day/2026-09-12")
    d = json.loads(body)
    room = d["cameras"]["cam_1"]
    assert [c["name"] for c in room] == ["2026-09-12_15-10-00.mp4", "2026-09-12_15-20-00.mp4"]
    assert room[0]["start"] == "15:10:00" and room[0]["start_s"] == 15 * 3600 + 600
    assert room[0]["duration"] == 600, "a full segment"
    assert room[1]["duration"] == 150, "cut short: ended at its last write"
    assert room[0]["live"] is False
    assert d["cameras"]["cam_2"] == []
    assert get(url + "/api/day/not-a-date")[0] == 404


def test_clips_are_served_with_range_support_and_download_header(site):
    base, start = site
    url = start()
    p = make_clip(base, "cam_1", "2026-09-12_15-10-00.mp4", size=100000)
    p.write_bytes(bytes(range(256)) * 400)
    code, hdr, body = get(url + "/clip/cam_1/2026-09-12_15-10-00.mp4")
    assert code == 200 and hdr["Content-Type"] == "video/mp4" and hdr["Accept-Ranges"] == "bytes"
    assert len(body) == 102400 and "Content-Disposition" not in hdr
    code, hdr, body = get(url + "/clip/cam_1/2026-09-12_15-10-00.mp4", {"Range": "bytes=10-19"})
    assert code == 206 and hdr["Content-Range"] == "bytes 10-19/102400" and body == bytes(range(10, 20))
    code, hdr, body = get(url + "/clip/cam_1/2026-09-12_15-10-00.mp4", {"Range": "bytes=102300-"})
    assert code == 206 and len(body) == 100
    code, hdr, body = get(url + "/clip/cam_1/2026-09-12_15-10-00.mp4", {"Range": "bytes=-5"})
    assert code == 206 and len(body) == 5 and hdr["Content-Range"] == "bytes 102395-102399/102400"
    code, hdr, _ = get(url + "/clip/cam_1/2026-09-12_15-10-00.mp4", {"Range": "bytes=999999-"})
    assert code == 416
    code, hdr, body = get(url + "/clip/cam_1/2026-09-12_15-10-00.mp4?download=1")
    assert code == 200 and hdr["Content-Disposition"] == 'attachment; filename="cam_1_2026-09-12_15-10-00.mp4"'
    code, hdr, body = get(url + "/clip/cam_1/2026-09-12_15-10-00.mp4", method="HEAD")
    assert code == 200 and hdr["Content-Length"] == "102400" and body == b""


@pytest.mark.parametrize("path", [
    "/clip/cam_1/../../config.json",
    "/clip/cam_1/%2e%2e/%2e%2e/config.json",
    "/clip/other_cam/2026-09-12_15-10-00.mp4",
    "/clip/cam_1/notes.txt",
    "/clip/cam_1/2026-09-12_15-10-00.mp4.bak",
    "/thumb/cam_1/../x.jpg",
    "/thumb/cam_1/notes.jpg",
    "/config.json",
    "/api/nothing",
])
def test_only_real_clips_are_reachable(site, path):
    base, start = site
    url = start()
    make_clip(base, "cam_1", "2026-09-12_15-10-00.mp4")
    (base / "recordings" / "cam_1" / "notes.txt").write_text("x")
    code, _, body = get(url + path)
    assert code == 404, (path, code)
    assert b"secret" not in body and b"rtsp" not in body


def test_basic_auth_when_configured(site):
    base, start = site
    url = start(auth="viewer:hunter2")
    code, hdr, _ = get(url + "/api/status")
    assert code == 401 and hdr["WWW-Authenticate"].startswith("Basic ")
    ok = "Basic " + base64.b64encode(b"viewer:hunter2").decode()
    assert get(url + "/api/status", {"Authorization": ok})[0] == 200
    bad = "Basic " + base64.b64encode(b"viewer:hunter3").decode()
    assert get(url + "/api/status", {"Authorization": bad})[0] == 401
    assert get(url + "/clip/cam_1/2026-09-12_15-10-00.mp4")[0] == 401, "clips too"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_thumbnails_are_generated_once_and_pruned_with_their_clip(site):
    base, start = site
    url = start()
    make_clip(base, "cam_1", "2026-09-12_15-10-00.mp4", real=True)
    code, hdr, body = get(url + "/thumb/cam_1/2026-09-12_15-10-00.jpg")
    assert code == 200 and hdr["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8", "a JPEG"
    thumb = base / "recordings" / ".thumbs" / "cam_1" / "2026-09-12_15-10-00.jpg"
    assert thumb.exists()
    first = thumb.stat().st_mtime_ns
    assert get(url + "/thumb/cam_1/2026-09-12_15-10-00.jpg")[0] == 200
    assert thumb.stat().st_mtime_ns == first, "served from the cache the second time"
    # the recorder deletes the clip; housekeeping drops the orphaned thumbnail
    (base / "recordings" / "cam_1" / "2026-09-12_15-10-00.mp4").unlink()
    srv_site = sp.Site(str(base), sp.load_config(str(base)), sp.load_page_config(str(base)))
    assert srv_site.prune_thumbs() == 1 and not thumb.exists()


def test_a_junk_clip_yields_no_thumbnail_but_no_crash(site):
    base, start = site
    url = start()
    make_clip(base, "cam_1", "2026-09-12_15-10-00.mp4", size=100)   # zeros, not a video
    code, _, _ = get(url + "/thumb/cam_1/2026-09-12_15-10-00.jpg")
    assert code == 404
    assert get(url + "/api/status")[0] == 200, "the server is still fine"


def test_log_tail(site):
    base, start = site
    url = start()
    (base / "tapo.log").write_text("".join(f"line {i}\n" for i in range(100)))
    (base / "logs").mkdir()
    (base / "logs" / "cam_recorder.log").write_text("rec a\nrec b\n")
    code, _, body = get(url + "/api/log")
    l = json.loads(body)
    assert l["presence"][-1] == "line 99" and len(l["presence"]) == 40
    assert l["recorder"] == ["rec a", "rec b"]


def test_config_validation_and_check(tmp_path, capsys):
    write_config(tmp_path)
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["status_page"] = {"port": 70000}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(sp.ConfigError, match="port"):
        sp.load_page_config(str(tmp_path))
    cfg["status_page"] = {"port": 8090, "auth": "nocolon"}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(sp.ConfigError, match="user:password"):
        sp.load_page_config(str(tmp_path))
    cfg["status_page"] = {"port": 8090, "colour": "red"}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(sp.ConfigError, match="unknown status_page option"):
        sp.load_page_config(str(tmp_path))
    cfg["status_page"] = {"port": 8090, "auth": "viewer:pw", "ffmpeg": 5}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(sp.ConfigError, match="ffmpeg must be a path"):
        sp.load_page_config(str(tmp_path))
    cfg["status_page"] = {"port": 8090, "auth": "viewer:pw", "thumbnails": False}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    assert sp.main(["--check", "--base-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "http://0.0.0.0:8090" in out and "basic, user viewer" in out and "thumbnails:    off" in out
    del cfg["status_page"]
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    assert sp.load_page_config(str(tmp_path))["port"] == 8090, "defaults apply without the section"


def test_https_when_configured(tmp_path, capsys):
    """With a certificate, the page (and its login) is only served over HTTPS."""
    import socket
    import ssl
    write_config(tmp_path)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-noenc",
                    "-days", "2", "-subj", "/CN=test", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["status_page"] = {"bind": "127.0.0.1", "auth": "viewer:pw", "thumbnails": False,
                          "tls_cert": str(cert), "tls_key": str(key)}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    (tmp_path / "night_mode.sh").write_text("#!/bin/bash\necho 'night mode: OFF'\n")
    (tmp_path / "night_mode.sh").chmod(0o755)
    srv = sp.make_server(str(tmp_path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        tls = ssl.create_default_context(cafile=str(cert))
        auth = {"Authorization": "Basic " + base64.b64encode(b"viewer:pw").decode()}
        req = urllib.request.Request(f"https://127.0.0.1:{port}/api/status", headers=auth)
        with urllib.request.urlopen(req, timeout=10, context=tls) as r:
            assert r.status == 200
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET /api/status HTTP/1.0\r\n\r\n")
            try:
                answer = s.recv(100)
            except ConnectionResetError:
                answer = b""
            assert not answer.startswith(b"HTTP"), "nothing over plain HTTP"
        with urllib.request.urlopen(req, timeout=10, context=tls) as r:
            assert r.status == 200, "and it carries on after one"
    finally:
        srv.shutdown()
        srv.server_close()
    assert sp.main(["--check", "--base-dir", str(tmp_path)]) == 0
    assert "https://127.0.0.1:8090" in capsys.readouterr().out
    cfg["status_page"] = {"tls_cert": str(cert)}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(sp.ConfigError, match="both be paths"):
        sp.load_page_config(str(tmp_path))


def test_a_client_that_never_finishes_its_request_is_dropped(site, monkeypatch):
    """A half request (no newline) used to park a server thread forever."""
    import socket
    base, start = site
    monkeypatch.setattr(sp.Handler, "timeout", 1)
    url = start()
    host, port = url[7:].split(":")
    s = socket.create_connection((host, int(port)))
    s.sendall(b"GET / HTTP/1.1")          # no line ending, ever
    s.settimeout(5)
    t0 = time.time()
    try:
        data = s.recv(100)
    except (ConnectionResetError, socket.timeout):
        data = b""
    s.close()
    assert time.time() - t0 < 4, "the server closed it after its own timeout, not ours"
    assert data == b"" or data.startswith(b"HTTP/1.0 4"), data
    assert get(url + "/api/status")[0] == 200, "and keeps serving others"
