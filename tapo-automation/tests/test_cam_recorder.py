"""
cam_recorder.py: the per-camera ffmpeg supervisor, driven with a fake clock and
tests/fake_ffmpeg.py standing in for the real binary.
"""
import json
import logging
import os
import shutil
import stat
import time
from collections import namedtuple

import pytest

import cam_recorder as cr
from conftest import FAKE_FFMPEG, wait_until, write_config


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_recorder(base, cameras, recording=None, always=False, clock=None, disk_usage=shutil.disk_usage):
    write_config(base, cameras=cameras, recording=recording)
    cfg = cr.load_config(str(base))
    return cr.Recorder(cfg, clock=clock or FakeClock(), always=always, disk_usage=disk_usage)


def job(rec, name):
    return next(j for j in rec.jobs if j.spec.name == name)


def segments(base, cam):
    d = base / "recordings" / cam
    return sorted(p for p in d.iterdir() if cr.SEGMENT_RE.match(p.name)) if d.exists() else []


def proc_gone(j):
    return j.proc is None


def set_control(base, value):
    (base / ".tapo_motion_state").write_text(f"{value}\n")


@pytest.fixture
def two_cams(tmp_path):
    cams = {
        "cam_a": {"ip": "10.0.0.1", "rtsp": "fake://record"},
        "cam_b": {"ip": "10.0.0.2", "rtsp": "fake://record"},
    }
    return tmp_path, cams


@pytest.fixture(autouse=True)
def _stop_everything():
    """Make sure no fake ffmpeg outlives a test."""
    recorders = []
    yield recorders
    for rec in recorders:
        rec.shutdown()


@pytest.fixture
def start(_stop_everything):
    def _start(base, cameras, **kw):
        rec = make_recorder(base, cameras, **kw)
        _stop_everything.append(rec)
        return rec
    return _start


# --------------------------------------------------------------------------- command line

def test_default_rtsp_command(tmp_path):
    write_config(tmp_path, cameras={"c": {"ip": "1.1.1.1", "rtsp": "rtsp://u:p@1.1.1.1:554/stream2"}},
                 recording={"audio": "aac", "segment_seconds": 600})
    cfg = cr.load_config(str(tmp_path))
    cmd = cr.build_command(cfg, cfg.cameras[0], "/rec/c")
    joined = " ".join(cmd)
    assert cmd[0] == str(FAKE_FFMPEG)
    assert "-nostdin" not in cmd, "ffmpeg 7.0 ignores 'q' and SIGTERM with -nostdin"
    assert "-segment_format_options movflags=+frag_keyframe+empty_moov+default_base_moof:flush_packets=1" in joined
    assert "-rtsp_transport tcp" in joined
    assert "-timeout 5000000" in joined
    assert "-use_wallclock_as_timestamps 1" in joined
    assert "-i rtsp://u:p@1.1.1.1:554/stream2" in joined
    assert "-c:v copy -c:a aac -b:a 32k" in joined
    assert ("-f segment -segment_time 600 -segment_atclocktime 1 -segment_format mp4 "
            "-segment_format_options movflags=+frag_keyframe+empty_moov+default_base_moof:flush_packets=1 "
            "-reset_timestamps 1 -strftime 1") in joined
    assert cmd[-1] == "/rec/c/%Y-%m-%d_%H-%M-%S.mp4"
    assert cmd.index("-i") < cmd.index("-c:v"), "input options must come before -i, output options after"


def test_plain_mp4_can_be_chosen(tmp_path):
    write_config(tmp_path, cameras={"c": {"ip": "1.1.1.1", "rtsp": "rtsp://x/1"}}, recording={"fragmented_mp4": False})
    cfg = cr.load_config(str(tmp_path))
    joined = " ".join(cr.build_command(cfg, cfg.cameras[0], "/rec/c"))
    assert "-segment_format_options flush_packets=1 " in joined
    assert "frag_keyframe" not in joined


@pytest.mark.parametrize("audio,expected", [("none", ["-an"]), ("copy", ["-c:a", "copy"])])
def test_audio_modes(tmp_path, audio, expected):
    write_config(tmp_path, cameras={"c": {"ip": "1.1.1.1", "rtsp": "rtsp://x/1"}}, recording={"audio": audio})
    cfg = cr.load_config(str(tmp_path))
    cmd = cr.build_command(cfg, cfg.cameras[0], "/rec/c")
    assert " ".join(expected) in " ".join(cmd)
    assert "aac" not in cmd


def test_non_rtsp_input_gets_no_rtsp_options(tmp_path):
    write_config(tmp_path, cameras={"c": {"ip": "1.1.1.1", "rtsp": "fake://record"}})
    cfg = cr.load_config(str(tmp_path))
    cmd = cr.build_command(cfg, cfg.cameras[0], "/rec/c")
    assert "-rtsp_transport" not in cmd and "-timeout" not in cmd


def test_input_and_output_args_can_be_overridden(tmp_path):
    write_config(tmp_path, cameras={
        "c": {"ip": "1.1.1.1", "rtsp": "rtsp://x/1", "input_args": ["-re"], "output_args": ["-c:v", "libx264"]},
        "d": {"ip": "1.1.1.2", "rtsp": "rtsp://x/2"},
    }, recording={"input_args": ["-rtsp_transport", "udp"], "output_args": ["-c", "copy"]})
    cfg = cr.load_config(str(tmp_path))
    c = " ".join(cr.build_command(cfg, cfg.cameras[0], "/r/c"))
    d = " ".join(cr.build_command(cfg, cfg.cameras[1], "/r/d"))
    assert "-re -i rtsp://x/1 -c:v libx264 -f segment" in c
    assert "-rtsp_transport udp -i rtsp://x/2 -c copy -f segment" in d


# --------------------------------------------------------------------------- config

def test_cameras_without_rtsp_or_with_record_false_are_skipped(tmp_path):
    write_config(tmp_path, cameras={
        "toggle_only": {"ip": "1.1.1.1"},
        "off": {"ip": "1.1.1.2", "rtsp": "rtsp://x", "record": False},
        "on": {"ip": "1.1.1.3", "rtsp": "rtsp://y"},
    })
    cfg = cr.load_config(str(tmp_path))
    assert [c.name for c in cfg.cameras] == ["on"]


def test_relative_paths_resolve_against_base_dir(tmp_path):
    write_config(tmp_path, cameras={"c": {"ip": "1", "rtsp": "rtsp://x"}},
                 recording={"root": "rec", "control_file": "ctl", "ffmpeg": "bin/ff"})
    cfg = cr.load_config(str(tmp_path))
    assert cfg.root == str(tmp_path / "rec")
    assert cfg.control_file == str(tmp_path / "ctl")
    assert cfg.ffmpeg == str(tmp_path / "bin" / "ff")


def test_absolute_paths_are_kept(tmp_path):
    write_config(tmp_path, cameras={"c": {"ip": "1", "rtsp": "rtsp://x"}},
                 recording={"root": "/mnt/rec", "control_file": None})
    cfg = cr.load_config(str(tmp_path))
    assert cfg.root == "/mnt/rec"
    assert cfg.control_file is None


@pytest.mark.parametrize("recording,match", [
    ({"audio": "mp3"}, "audio must be"),
    ({"nonsense": 1}, "unknown recording option"),
    ({"retain_days": -1}, "non-negative"),
    ({"segment_seconds": 0}, "at least 1"),
    ({"input_args": "-re"}, "list of strings"),
    ({"fragmented_mp4": "yes"}, "true or false"),
])
def test_invalid_recording_options(tmp_path, recording, match):
    write_config(tmp_path, cameras={"c": {"ip": "1", "rtsp": "rtsp://x"}}, recording=recording)
    with pytest.raises(cr.ConfigError, match=match):
        cr.load_config(str(tmp_path))


def test_invalid_camera_entries(tmp_path):
    write_config(tmp_path, cameras={"bad name/": {"ip": "1", "rtsp": "rtsp://x"}})
    with pytest.raises(cr.ConfigError, match="directory name"):
        cr.load_config(str(tmp_path))
    write_config(tmp_path, cameras={"c": {"ip": "1", "rtsp": 5}})
    with pytest.raises(cr.ConfigError, match="must be a string"):
        cr.load_config(str(tmp_path))
    write_config(tmp_path, cameras={})
    with pytest.raises(cr.ConfigError, match="non-empty"):
        cr.load_config(str(tmp_path))


def test_missing_and_broken_config_file(tmp_path):
    with pytest.raises(cr.ConfigError, match="not found"):
        cr.load_config(str(tmp_path))
    (tmp_path / "config.json").write_text("[]")
    with pytest.raises(cr.ConfigError, match="top level"):
        cr.load_config(str(tmp_path))


def test_resolve_ffmpeg_prefers_path_then_bundled(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert cr.resolve_ffmpeg(str(tmp_path)) == "/usr/bin/ffmpeg"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert cr.resolve_ffmpeg(str(tmp_path)) == "ffmpeg"
    bundled = tmp_path / "bin" / "ffmpeg"
    bundled.parent.mkdir()
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    assert cr.resolve_ffmpeg(str(tmp_path)) == str(bundled)
    assert cr.resolve_ffmpeg(str(tmp_path), "/opt/ffmpeg") == "/opt/ffmpeg"


def test_read_control(tmp_path):
    path = tmp_path / "state"
    assert cr.read_control(str(path), "enable", previous=True) is False, "missing file: do not record"
    path.write_text("enable\n")
    assert cr.read_control(str(path), "enable", previous=False) is True
    path.write_text("disable\n")
    assert cr.read_control(str(path), "enable", previous=True) is False
    path.write_text("")
    assert cr.read_control(str(path), "enable", previous=True) is True, "half written file keeps the last decision"
    assert cr.read_control(None, "enable", previous=False) is True


def test_redact_hides_credentials():
    assert cr.redact("rtsp://bob:s3cr3t@10.0.0.1:554/stream2 failed") == "rtsp://***@10.0.0.1:554/stream2 failed"
    assert cr.redact("nothing here") == "nothing here"


# --------------------------------------------------------------------------- supervision

def test_records_only_while_control_file_says_enable(two_cams, start):
    base, cams = two_cams
    rec = start(base, cams)
    a = job(rec, "cam_a")

    rec.tick()
    assert not a.running, "no control file: stay idle"

    set_control(base, "disable")
    rec.tick()
    assert not a.running

    set_control(base, "enable")
    rec.tick()
    assert a.running and job(rec, "cam_b").running
    wait_until(lambda: segments(base, "cam_a") and segments(base, "cam_a")[0].stat().st_size > 0,
               message="fake ffmpeg to write data")
    assert a.state == "recording"

    set_control(base, "disable")
    rec.tick()
    assert a.state == "stopping"
    wait_until(lambda: a.proc.poll() is not None, message="ffmpeg to exit on SIGTERM")
    rec.tick()
    assert a.state == "idle" and not a.running
    assert a.failures == 0, "a requested stop is not a failure"
    assert segments(base, "cam_a")[0].read_bytes().endswith(b"moov"), "ffmpeg got the chance to finalise"


def test_always_flag_ignores_control_file(two_cams, start):
    base, cams = two_cams
    rec = start(base, cams, always=True)
    rec.tick()
    assert all(j.running for j in rec.jobs)


def test_one_failing_camera_never_affects_the_other(two_cams, start, caplog):
    base, cams = two_cams
    cams["cam_b"]["rtsp"] = "fake://exit/1"
    clock = FakeClock()
    rec = start(base, cams, clock=clock)
    set_control(base, "enable")
    a, b = job(rec, "cam_a"), job(rec, "cam_b")

    with caplog.at_level(logging.INFO):
        rec.tick()
        assert a.running and b.running
        wait_until(lambda: b.proc.poll() is not None, message="cam_b to fail")
        rec.tick()

    assert b.state == "backoff" and b.failures == 1
    assert b.next_start_at == clock() + 5
    assert "simulated failure" in b.last_error
    assert "[cam_b] ffmpeg exited rc=1" in caplog.text
    assert a.running and a.state == "recording", "cam_a keeps recording"

    # backoff grows 5, 10, 20, 40, 40 (capped)
    expected_delays = [10, 20, 40, 40]
    for delay in expected_delays:
        clock.advance(b.next_start_at - clock())
        rec.tick()
        assert b.running, "restarted once the backoff passed"
        wait_until(lambda: b.proc.poll() is not None)
        rec.tick()
        assert b.next_start_at - clock() == delay
    assert a.running and a.state == "recording"
    assert len(segments(base, "cam_a")) == 1


def test_ffmpeg_logs_are_private_since_they_carry_the_rtsp_password(two_cams, start):
    """ffmpeg's own messages name the input URL, password included; only what the
    recorder writes itself is redacted. So the folder and the files are the
    owner's alone, including ones left from before with wider modes."""
    base, cams = two_cams
    cams["cam_b"]["rtsp"] = "fake://exit/1"
    logs = base / "logs"
    logs.mkdir()
    os.chmod(logs, 0o755)
    old = logs / "ffmpeg_cam_b.log"
    old.write_text("old\n")
    os.chmod(old, 0o644)
    rec = start(base, cams)
    set_control(base, "enable")
    rec.tick()
    b = job(rec, "cam_b")
    wait_until(lambda: b.proc.poll() is not None, message="cam_b to fail")
    rec.tick()
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    files = sorted(logs.glob("ffmpeg_*.log"))
    assert [f.name for f in files] == ["ffmpeg_cam_a.log", "ffmpeg_cam_b.log"]
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    assert "simulated failure" in old.read_text(), "the old file is appended to, not replaced"


def test_a_healthy_run_clears_an_old_failure_streak_on_a_clean_stop(two_cams, start):
    """status.json must not keep reporting failures a camera has recovered from."""
    base, cams = two_cams
    clock = FakeClock()
    rec = start(base, cams, clock=clock)
    set_control(base, "enable")
    a = job(rec, "cam_a")
    a.failures = 2                     # e.g. the camera was still leaving privacy mode
    a.last_error = "ffmpeg exited rc=145"
    rec.tick()
    assert a.running
    clock.advance(rec.cfg.healthy_after_seconds + 1)
    set_control(base, "disable")
    rec.tick()
    wait_until(lambda: a.proc.poll() is not None, message="ffmpeg to stop")
    rec.tick()
    assert a.state == "idle"
    assert a.failures == 0 and a.last_error is None
    status = json.loads((base / "recordings" / "status.json").read_text())
    assert status["cameras"]["cam_a"]["failures"] == 0
    assert status["cameras"]["cam_a"]["last_error"] is None


def test_a_short_clean_run_keeps_the_streak(two_cams, start):
    """Stopping again within seconds proves nothing, so the backoff must stay."""
    base, cams = two_cams
    clock = FakeClock()
    rec = start(base, cams, clock=clock)
    set_control(base, "enable")
    a = job(rec, "cam_a")
    a.failures = 2
    rec.tick()
    clock.advance(1)
    set_control(base, "disable")
    rec.tick()
    wait_until(lambda: a.proc.poll() is not None)
    rec.tick()
    assert a.failures == 2


def test_backoff_resets_after_a_healthy_run(two_cams, start):
    base, cams = two_cams
    cams["cam_a"]["rtsp"] = "fake://die-after/0.5"
    clock = FakeClock()
    rec = start(base, cams, clock=clock)
    set_control(base, "enable")
    a = job(rec, "cam_a")
    a.failures = 4
    rec.tick()
    assert a.running
    clock.advance(rec.cfg.healthy_after_seconds + 1)  # it "ran" long enough
    wait_until(lambda: a.proc.poll() is not None, message="cam_a to die")
    rec.tick()
    assert a.failures == 1, "an old failure streak is forgotten after a healthy run"
    assert a.next_start_at == clock() + rec.cfg.backoff_base_seconds


def test_stalled_ffmpeg_is_restarted(two_cams, start, caplog):
    base, cams = two_cams
    cams["cam_a"]["rtsp"] = "fake://hang"
    clock = FakeClock()
    rec = start(base, cams, clock=clock)
    set_control(base, "enable")
    a = job(rec, "cam_a")
    rec.tick()
    assert a.running
    wait_until(lambda: segments(base, "cam_b"), message="cam_b to write its first bytes")

    with caplog.at_level(logging.WARNING):
        clock.advance(rec.cfg.stall_seconds - 0.1)
        rec.tick()
        assert a.state == "recording", "not stalled yet"
        clock.advance(1)
        rec.tick()
    assert a.state == "stopping"
    assert "no data written for" in caplog.text
    wait_until(lambda: a.proc.poll() is not None)
    rec.tick()
    assert a.state == "backoff" and a.failures == 1
    assert "stalled" in a.last_error
    assert job(rec, "cam_b").running, "cam_b unaffected"


def test_writing_ffmpeg_is_not_considered_stalled(two_cams, start):
    base, cams = two_cams
    clock = FakeClock()
    rec = start(base, cams, clock=clock)
    set_control(base, "enable")
    a = job(rec, "cam_a")
    rec.tick()
    for _ in range(4):
        time.sleep(0.1)  # let the fake write more bytes
        clock.advance(rec.cfg.stall_seconds)  # far more than the stall limit per check
        rec.tick()
        assert a.state == "recording"


def test_ffmpeg_ignoring_sigterm_is_killed(two_cams, start, caplog):
    base, cams = two_cams
    cams["cam_a"]["rtsp"] = "fake://stubborn"
    clock = FakeClock()
    rec = start(base, cams, clock=clock)
    set_control(base, "enable")
    a = job(rec, "cam_a")
    rec.tick()
    wait_until(lambda: segments(base, "cam_a"))

    set_control(base, "disable")
    rec.tick()
    assert a.state == "stopping"
    time.sleep(0.2)
    assert a.proc.poll() is None, "the stubborn fake ignores SIGTERM"
    with caplog.at_level(logging.WARNING):
        clock.advance(rec.cfg.stop_grace_seconds)
        rec.tick()
    assert "ignored SIGTERM" in caplog.text
    wait_until(lambda: a.proc is None or a.proc.poll() is not None, message="SIGKILL to land")
    rec.tick()
    assert a.state == "idle" and not a.running


def test_missing_ffmpeg_binary_is_a_retried_failure(two_cams, start, caplog):
    base, cams = two_cams
    rec = start(base, cams, recording={"ffmpeg": "/nonexistent/ffmpeg"})
    set_control(base, "enable")
    with caplog.at_level(logging.WARNING):
        rec.tick()
    for j in rec.jobs:
        assert j.state == "backoff" and j.failures == 1
        assert "cannot start ffmpeg" in j.last_error
    assert "/nonexistent/ffmpeg" in caplog.text


def test_supervisor_error_in_one_camera_does_not_stop_the_tick(two_cams, start, monkeypatch, caplog):
    base, cams = two_cams
    rec = start(base, cams)
    set_control(base, "enable")
    a, b = job(rec, "cam_a"), job(rec, "cam_b")

    def boom(_want):
        raise RuntimeError("supervisor bug")

    monkeypatch.setattr(a, "tick", boom)
    with caplog.at_level(logging.ERROR):
        rec.tick()
    assert "unexpected error in supervisor" in caplog.text
    assert b.running


def test_status_file_reflects_camera_states(two_cams, start):
    base, cams = two_cams
    cams["cam_b"]["rtsp"] = "fake://exit/3"
    rec = start(base, cams)
    set_control(base, "enable")
    rec.tick()
    b = job(rec, "cam_b")
    wait_until(lambda: b.proc.poll() is not None)
    rec.tick()
    status = json.loads((base / "recordings" / "status.json").read_text())
    assert status["recording_wanted"] is True
    assert status["cameras"]["cam_a"]["state"] == "recording"
    assert status["cameras"]["cam_a"]["pid"] == job(rec, "cam_a").proc.pid
    assert status["cameras"]["cam_b"]["state"] == "backoff"
    assert status["cameras"]["cam_b"]["failures"] == 1
    assert status["cameras"]["cam_b"]["retry_in_seconds"] == 5
    assert "updated" in status


def test_shutdown_stops_every_camera_gracefully(two_cams, start):
    base, cams = two_cams
    rec = start(base, cams)
    set_control(base, "enable")
    rec.tick()
    wait_until(lambda: segments(base, "cam_a") and segments(base, "cam_b"))
    started = time.monotonic()
    rec.shutdown()
    assert time.monotonic() - started < rec.cfg.stop_grace_seconds
    assert all(not j.running for j in rec.jobs)
    for cam in ("cam_a", "cam_b"):
        assert segments(base, cam)[0].read_bytes().endswith(b"moov")
    status = json.loads((base / "recordings" / "status.json").read_text())
    assert all(c["state"] == "idle" for c in status["cameras"].values())


def test_shutdown_kills_a_stubborn_ffmpeg_within_the_grace_period(two_cams, start):
    base, cams = two_cams
    cams["cam_a"]["rtsp"] = "fake://stubborn"
    rec = start(base, cams, recording={"stop_grace_seconds": 0.5})
    set_control(base, "enable")
    rec.tick()
    a = job(rec, "cam_a")
    pid = a.proc.pid
    started = time.monotonic()
    rec.shutdown()
    assert time.monotonic() - started < 3
    assert not a.running
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_ffmpeg_stderr_goes_to_a_per_camera_log_and_is_redacted(two_cams, start):
    base, cams = two_cams
    cams["cam_b"]["rtsp"] = "fake://exit/1"
    rec = start(base, cams)
    set_control(base, "enable")
    rec.tick()
    b = job(rec, "cam_b")
    wait_until(lambda: b.proc.poll() is not None)
    rec.tick()
    log_file = base / "logs" / "ffmpeg_cam_b.log"
    assert log_file.exists()
    text = log_file.read_text()
    assert "simulated failure" in text
    assert "--- start" in text


def test_ffmpeg_log_is_rotated_when_large(two_cams, start):
    base, cams = two_cams
    rec = start(base, cams, recording={"ffmpeg_log_max_bytes": 100})
    log_file = base / "logs" / "ffmpeg_cam_a.log"
    log_file.parent.mkdir()
    log_file.write_text("x" * 200)
    set_control(base, "enable")
    rec.tick()
    assert (base / "logs" / "ffmpeg_cam_a.log.1").read_text() == "x" * 200
    assert "xxx" not in log_file.read_text()
    assert "--- start" in log_file.read_text()


def test_control_file_flip_logs_once(two_cams, start, caplog):
    base, cams = two_cams
    rec = start(base, cams)
    set_control(base, "enable")
    with caplog.at_level(logging.INFO):
        rec.tick()
        rec.tick()
        rec.tick()
    assert caplog.text.count("recording ON") == 1


# --------------------------------------------------------------------------- retention

Usage = namedtuple("Usage", "total used free")


def make_segment(base, cam, name, size=1000, age_seconds=0, now=None):
    d = base / "recordings" / cam
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    with open(p, "wb") as f:
        f.truncate(size)  # sparse: any size without using disk
    now = time.time() if now is None else now
    os.utime(p, (now - age_seconds, now - age_seconds))
    return p


def test_prune_deletes_segments_older_than_retain_days(two_cams):
    base, cams = two_cams
    write_config(base, cameras=cams, recording={"retain_days": 14, "min_free_gb": 0})
    cfg = cr.load_config(str(base))
    now = time.time()
    old = make_segment(base, "cam_a", "2026-08-01_10-00-00.mp4", age_seconds=15 * 86400, now=now)
    edge = make_segment(base, "cam_a", "2026-08-23_10-00-00.mp4", age_seconds=14 * 86400 - 60, now=now)
    fresh = make_segment(base, "cam_b", "2026-09-06_10-00-00.mp4", age_seconds=60, now=now)
    other = base / "recordings" / "cam_a" / "notes.txt"
    other.write_text("keep me")
    (base / "recordings" / "not_a_camera").mkdir()
    stray = make_segment(base, "not_a_camera", "2026-01-01_00-00-00.mp4", age_seconds=300 * 86400, now=now)

    assert cr.prune_recordings(cfg, now_wall=now) == 1
    assert not old.exists()
    assert edge.exists() and fresh.exists()
    assert other.exists(), "only segment files are touched"
    assert stray.exists(), "directories of unknown cameras are left alone"


def test_prune_frees_disk_oldest_first_but_keeps_live_files(two_cams):
    base, cams = two_cams
    write_config(base, cameras=cams, recording={"retain_days": 14, "min_free_gb": 1, "segment_seconds": 600})
    cfg = cr.load_config(str(base))
    now = time.time()
    gib = 1024 ** 3
    seg = int(0.3 * gib)  # sparse files, so this costs no real disk
    oldest = make_segment(base, "cam_a", "2026-09-01_10-00-00.mp4", size=seg, age_seconds=5 * 86400, now=now)
    middle = make_segment(base, "cam_b", "2026-09-03_10-00-00.mp4", size=seg, age_seconds=3 * 86400, now=now)
    newer = make_segment(base, "cam_a", "2026-09-05_10-00-00.mp4", size=seg, age_seconds=1 * 86400, now=now)
    live = make_segment(base, "cam_b", "2026-09-06_10-00-00.mp4", size=seg, age_seconds=30, now=now)

    # 0.5 GiB free, want 1 GiB: deleting the two oldest 0.3 GiB segments is enough
    deleted = cr.prune_recordings(cfg, now_wall=now,
                                  disk_usage=lambda _p: Usage(10 * gib, int(9.5 * gib), int(0.5 * gib)))
    assert deleted == 2
    assert not oldest.exists() and not middle.exists()
    assert newer.exists() and live.exists()


def test_prune_never_deletes_live_files_even_when_disk_stays_low(two_cams, caplog):
    base, cams = two_cams
    write_config(base, cameras=cams, recording={"min_free_gb": 1, "segment_seconds": 600})
    cfg = cr.load_config(str(base))
    now = time.time()
    live = make_segment(base, "cam_a", "2026-09-06_10-00-00.mp4", age_seconds=30, now=now)
    with caplog.at_level(logging.WARNING):
        deleted = cr.prune_recordings(cfg, now_wall=now,
                                      disk_usage=lambda _p: Usage(10, 10, 0))
    assert deleted == 0 and live.exists()
    assert "disk still low" in caplog.text


def test_prune_runs_on_schedule_from_tick(two_cams, start):
    base, cams = two_cams
    clock = FakeClock()
    rec = start(base, cams, recording={"prune_interval_seconds": 600, "retain_days": 1}, clock=clock)
    old = make_segment(base, "cam_a", "2026-08-01_10-00-00.mp4", age_seconds=3 * 86400)
    rec.tick()
    assert not old.exists(), "first tick prunes"
    old2 = make_segment(base, "cam_a", "2026-08-02_10-00-00.mp4", age_seconds=3 * 86400)
    clock.advance(100)
    rec.tick()
    assert old2.exists(), "not yet due"
    clock.advance(500)
    rec.tick()
    assert not old2.exists()


def test_retain_days_zero_disables_age_pruning(two_cams):
    base, cams = two_cams
    write_config(base, cameras=cams, recording={"retain_days": 0, "min_free_gb": 0})
    cfg = cr.load_config(str(base))
    old = make_segment(base, "cam_a", "2020-01-01_00-00-00.mp4", age_seconds=2000 * 86400)
    assert cr.prune_recordings(cfg) == 0
    assert old.exists()


# --------------------------------------------------------------------------- cli

def test_check_command_prints_redacted_commands(tmp_path, capsys):
    write_config(tmp_path, cameras={"c": {"ip": "1.1.1.1", "rtsp": "rtsp://bob:hunter2@1.1.1.1:554/stream2"}})
    assert cr.main(["--check", "--base-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "rtsp://***@1.1.1.1:554/stream2" in out
    assert "ffmpeg version fake" in out
    assert "cameras:       1" in out


def test_check_fails_when_ffmpeg_cannot_run(tmp_path, capsys):
    write_config(tmp_path, cameras={"c": {"ip": "1", "rtsp": "rtsp://x"}}, recording={"ffmpeg": "/nonexistent"})
    assert cr.main(["--check", "--base-dir", str(tmp_path)]) == 1
    assert "NOT RUNNABLE" in capsys.readouterr().out


def test_main_reports_config_errors(tmp_path, capsys):
    assert cr.main(["--base-dir", str(tmp_path)]) == 2
    assert "config error" in capsys.readouterr().err


def test_newest_segment_helper(tmp_path):
    d = tmp_path / "cam"
    assert cr.newest_segment(str(d)) is None
    d.mkdir()
    (d / "readme.txt").write_text("x")
    assert cr.newest_segment(str(d)) is None
    (d / "2026-09-06_10-00-00.mp4").write_bytes(b"a")
    (d / "2026-09-06_10-10-00.mp4").write_bytes(b"abc")
    assert cr.newest_segment(str(d)) == ("2026-09-06_10-10-00.mp4", 3)


def test_tail_lines_redacts_and_limits(tmp_path):
    p = tmp_path / "log"
    p.write_text("\n".join(f"line {i} rtsp://u:p@h/x" for i in range(20)) + "\n")
    tail = cr.tail_lines(str(p), n=3)
    assert tail == [f"line {i} rtsp://***@h/x" for i in (17, 18, 19)]
    assert cr.tail_lines(str(tmp_path / "missing")) == []


def test_stop_uses_the_q_key_on_stdin(two_cams, start):
    """ffmpeg 7.0 ignores SIGTERM without stdin interaction; 'q' must reach it."""
    base, cams = two_cams
    cams["cam_a"]["rtsp"] = "fake://qonly"
    rec = start(base, cams)
    set_control(base, "enable")
    rec.tick()
    a = job(rec, "cam_a")
    wait_until(lambda: segments(base, "cam_a"))
    set_control(base, "disable")
    rec.tick()
    wait_until(lambda: a.proc.poll() is not None, timeout=3, message="fake to quit on 'q'")
    assert a.proc.returncode == 0
    rec.tick()
    assert a.state == "idle" and a.failures == 0
    assert segments(base, "cam_a")[0].read_bytes().endswith(b"moov")


# --------------------------------------------------------------------------- notifications

def fake_notify(base):
    """A stand-in for ntfy-notify that records its arguments, one call per line."""
    script = base / "fake_notify.sh"
    calls = base / "notify_calls.log"
    script.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "$*" >> "{calls}"\n')
    script.chmod(0o755)
    return script, calls


def notify_calls(calls):
    return calls.read_text().splitlines() if calls.exists() else []


def fail_once(rec, j, clock):
    """Let a failing camera be (re)started and fail once more."""
    clock.advance(max(0.0, j.next_start_at - clock()))
    rec.tick()
    assert j.running
    wait_until(lambda: j.proc.poll() is not None, message=f"{j.spec.name} to fail")
    rec.tick()


def wait_for_progress(j):
    """Block until the fake ffmpeg has written more since the supervisor last looked."""
    wait_until(lambda: cr.newest_segment(j.dir) not in (None, j.last_marker), message="ffmpeg to write data")


NOTIFY_FAST = {"notify_after_failures": 1, "progress_check_seconds": 0, "stall_seconds": 1000}


def test_a_failure_streak_is_pushed_once_per_outage(two_cams, start):
    base, cams = two_cams
    cams["cam_b"]["rtsp"] = "fake://exit/1"
    script, calls = fake_notify(base)
    clock = FakeClock()
    rec = start(base, cams, recording={"notify_command": str(script), "notify_after_failures": 3}, clock=clock)
    set_control(base, "enable")
    b = job(rec, "cam_b")

    fail_once(rec, b, clock)
    fail_once(rec, b, clock)
    assert b.failures == 2 and not b.notified
    time.sleep(0.1)
    assert notify_calls(calls) == [], "not yet"

    fail_once(rec, b, clock)
    assert b.failures == 3 and b.notified
    wait_until(lambda: notify_calls(calls), message="the push")
    [call] = notify_calls(calls)
    assert call.startswith("-p high -t warning,video_camera cam_b: recording keeps failing "
                           "3 failures in a row. Last: ffmpeg exited rc=1")
    assert "simulated failure" in call
    assert call.endswith("Next retry in 20s.")

    for _ in range(3):
        fail_once(rec, b, clock)
    assert b.failures == 6
    time.sleep(0.1)
    assert len(notify_calls(calls)) == 1, "one push per outage"
    assert job(rec, "cam_a").running and job(rec, "cam_a").state == "recording"


def test_recovery_is_pushed_after_a_healthy_minute_of_data(two_cams, start):
    base, cams = two_cams
    cams["cam_b"]["rtsp"] = "fake://exit/1"
    script, calls = fake_notify(base)
    clock = FakeClock()
    rec = start(base, cams, recording=dict(NOTIFY_FAST, notify_command=str(script)), clock=clock)
    set_control(base, "enable")
    b = job(rec, "cam_b")
    fail_once(rec, b, clock)
    wait_until(lambda: len(notify_calls(calls)) == 1)

    b.spec = cr.CameraSpec(name="cam_b", url="fake://record")  # the camera is back
    clock.advance(b.next_start_at - clock())
    rec.tick()
    assert b.running and b.state == "recording"
    wait_for_progress(b)
    clock.advance(30)
    rec.tick()
    assert b.notified, "30 s of data is not a recovery yet"
    wait_for_progress(b)
    clock.advance(30)
    rec.tick()
    assert not b.notified
    wait_until(lambda: len(notify_calls(calls)) == 2)
    assert notify_calls(calls)[1].startswith(
        "-p low -t white_check_mark,video_camera cam_b: recording again ffmpeg has been writing for 60s.")


def test_recovery_is_also_noticed_on_a_clean_stop_after_a_healthy_run(two_cams, start):
    base, cams = two_cams
    cams["cam_b"]["rtsp"] = "fake://exit/1"
    script, calls = fake_notify(base)
    clock = FakeClock()
    rec = start(base, cams, recording=dict(NOTIFY_FAST, notify_command=str(script)), clock=clock)
    set_control(base, "enable")
    b = job(rec, "cam_b")
    fail_once(rec, b, clock)
    wait_until(lambda: len(notify_calls(calls)) == 1)

    b.spec = cr.CameraSpec(name="cam_b", url="fake://record")
    clock.advance(b.next_start_at - clock())
    rec.tick()
    wait_for_progress(b)
    clock.advance(60)
    set_control(base, "disable")   # everyone is home again: a clean stop after a healthy minute
    rec.tick()
    wait_until(lambda: proc_gone(b) or b.proc.poll() is not None)
    rec.tick()
    assert b.failures == 0 and not b.notified and b.state == "idle"
    wait_until(lambda: len(notify_calls(calls)) == 2)
    assert "cam_b: recording again" in notify_calls(calls)[1]


def test_a_reset_streak_does_not_push_twice_before_a_real_recovery(tmp_path, start):
    """A camera that connects but never delivers data: every stall restart counts as a
    fresh problem (failures reset), yet the outage is pushed once until data flows."""
    script, calls = fake_notify(tmp_path)
    clock = FakeClock()
    rec = start(tmp_path, {"cam_b": {"ip": "10.0.0.2", "rtsp": "fake://hang"}},
                recording={"notify_command": str(script), "notify_after_failures": 1,
                           "progress_check_seconds": 0, "stall_seconds": 60,
                           "healthy_after_seconds": 60}, clock=clock)
    set_control(tmp_path, "enable")
    b = job(rec, "cam_b")
    for _ in range(3):
        clock.advance(max(0.0, b.next_start_at - clock()))
        rec.tick()
        assert b.running
        clock.advance(60)
        rec.tick()                      # stalled: asked to stop, counted as a failure
        wait_until(lambda: b.proc is None or b.proc.poll() is not None)
        rec.tick()
        assert b.failures == 1 and b.notified
    time.sleep(0.1)
    assert len(notify_calls(calls)) == 1


def test_disk_staying_low_after_pruning_is_pushed_once_and_its_recovery_too(two_cams, start):
    base, cams = two_cams
    script, calls = fake_notify(base)
    gib = 1024 ** 3
    free = {"bytes": 10 * gib}
    usage = lambda _p: Usage(100 * gib, 100 * gib - free["bytes"], free["bytes"])
    clock = FakeClock()
    rec = start(base, cams, recording={"notify_command": str(script), "min_free_gb": 20,
                                       "prune_interval_seconds": 600}, clock=clock, disk_usage=usage)
    (base / "recordings").mkdir(exist_ok=True)
    rec.tick()   # the first tick prunes and checks the disk
    wait_until(lambda: notify_calls(calls))
    [call] = notify_calls(calls)
    assert call.startswith("-p high -t warning,floppy_disk Recording disk is low 10.0 GiB free under")
    assert "want 20 GiB" in call
    clock.advance(600)
    rec.tick()
    time.sleep(0.1)
    assert len(notify_calls(calls)) == 1, "still low: no repeat"

    free["bytes"] = 30 * gib
    clock.advance(600)
    rec.tick()
    wait_until(lambda: len(notify_calls(calls)) == 2)
    assert notify_calls(calls)[1].startswith("-p low -t white_check_mark,floppy_disk Recording disk OK again 30.0 GiB free")
    assert not rec.disk_notified


def test_notifications_are_off_by_default(two_cams, start):
    base, cams = two_cams
    rec = start(base, cams)
    assert rec.cfg.notify_command is None and not rec.notifier.enabled
    assert rec.notifier.send("x", "y") is False


def test_a_failing_helper_is_logged_and_never_hurts_recording(two_cams, start, caplog):
    base, cams = two_cams
    cams["cam_b"]["rtsp"] = "fake://exit/1"
    bad = base / "bad_notify.sh"
    bad.write_text("#!/bin/bash\necho 'ntfy-notify: http://127.0.0.1:8082/alerts answered HTTP 000' >&2\nexit 2\n")
    bad.chmod(0o755)
    clock = FakeClock()
    rec = start(base, cams, recording={"notify_command": str(bad), "notify_after_failures": 1}, clock=clock)
    set_control(base, "enable")
    b = job(rec, "cam_b")
    with caplog.at_level(logging.INFO):
        fail_once(rec, b, clock)
        assert b.notified
        wait_until(lambda: all(p.poll() is not None for p, _ in rec.notifier._running))
        rec.tick()
    assert "notification: cam_b: recording keeps failing" in caplog.text
    assert "failed (rc=2): ntfy-notify: http://127.0.0.1:8082/alerts answered HTTP 000" in caplog.text
    assert rec.notifier._running == []
    assert job(rec, "cam_a").running


def test_a_missing_helper_is_logged_and_the_push_is_retried_next_failure(two_cams, start, caplog):
    base, cams = two_cams
    cams["cam_b"]["rtsp"] = "fake://exit/1"
    clock = FakeClock()
    rec = start(base, cams, recording={"notify_command": str(base / "absent"), "notify_after_failures": 1}, clock=clock)
    set_control(base, "enable")
    b = job(rec, "cam_b")
    with caplog.at_level(logging.WARNING):
        fail_once(rec, b, clock)
    assert not b.notified
    assert "cannot run" in caplog.text and "absent" in caplog.text


def test_notify_config_validation(tmp_path):
    write_config(tmp_path, recording={"notify_command": 5})
    with pytest.raises(cr.ConfigError, match="notify_command must be a string or null"):
        cr.load_config(str(tmp_path))
    write_config(tmp_path, recording={"notify_after_failures": -1})
    with pytest.raises(cr.ConfigError, match="notify_after_failures must be a non-negative number"):
        cr.load_config(str(tmp_path))
    write_config(tmp_path, recording={"notify_command": "bin/notify"})
    assert cr.load_config(str(tmp_path)).notify_command == str(tmp_path / "bin" / "notify")
    write_config(tmp_path, recording={"notify_command": ""})
    assert cr.load_config(str(tmp_path)).notify_command is None
    write_config(tmp_path, recording={"notify_command": "/usr/local/bin/ntfy-notify", "notify_after_failures": 2.0})
    cfg = cr.load_config(str(tmp_path))
    assert cfg.notify_command == "/usr/local/bin/ntfy-notify" and cfg.notify_after_failures == 2


def test_check_command_reports_the_notification_setting(tmp_path, capsys):
    write_config(tmp_path, recording={"notify_command": str(tmp_path / "nope")})
    cr.main(["--check", "--base-dir", str(tmp_path)])
    assert f"notifications: {tmp_path / 'nope'} after 5 failures  (NOT EXECUTABLE)" in capsys.readouterr().out
    write_config(tmp_path)
    cr.main(["--check", "--base-dir", str(tmp_path)])
    assert "notifications: off (set recording.notify_command)" in capsys.readouterr().out
