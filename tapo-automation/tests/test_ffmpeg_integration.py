"""
End-to-end check of the real ffmpeg pipeline, without a camera: a synthetic
test pattern is fed through the exact segmenting output arguments the recorder
uses, and the resulting MP4 segments are verified with ffprobe.

Skipped when no ffmpeg is available (PATH or <project>/bin/ffmpeg).
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import cam_recorder as cr
from conftest import ROOT, wait_until, write_config

FFMPEG = shutil.which("ffmpeg") or (str(ROOT / "bin" / "ffmpeg") if os.access(ROOT / "bin" / "ffmpeg", os.X_OK) else None)
FFPROBE = shutil.which("ffprobe") or (str(ROOT / "bin" / "ffprobe") if os.access(ROOT / "bin" / "ffprobe", os.X_OK) else None)

pytestmark = pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe not available")


def probe_duration(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"ffprobe failed on {path}: {out.stderr}"
    return float(out.stdout.strip())


def probe_frames(path):
    """Frames that actually decode out of the file, whatever the container header says."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"ffprobe failed on {path}: {out.stderr}"
    value = out.stdout.strip()
    return int(value) if value.isdigit() else 0   # N/A means nothing decoded


def probe_codec(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


@pytest.fixture
def synthetic_camera(tmp_path):
    """A camera whose 'stream' is ffmpeg's built-in test pattern, encoded live."""
    cams = {
        "synthetic": {
            "ip": "127.0.0.1",
            "rtsp": "testsrc=size=160x120:rate=10",
            "input_args": ["-re", "-f", "lavfi"],
            "output_args": ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                            "-pix_fmt", "yuv420p", "-g", "10"],
        }
    }
    write_config(tmp_path, cameras=cams, recording={
        "ffmpeg": FFMPEG, "segment_seconds": 2, "audio": "none", "stall_seconds": 10,
        "progress_check_seconds": 0.5, "stop_grace_seconds": 10, "min_free_gb": 0,
    })
    return tmp_path


def test_real_ffmpeg_writes_playable_segments_and_finalises_on_stop(synthetic_camera):
    base = synthetic_camera
    cfg = cr.load_config(str(base))
    rec = cr.Recorder(cfg, always=True)
    seg_dir = Path(cfg.root) / "synthetic"
    try:
        deadline = time.time() + 12
        while time.time() < deadline:
            rec.tick()
            done = [p for p in seg_dir.glob("*.mp4")] if seg_dir.exists() else []
            if len(done) >= 3:
                break
            time.sleep(0.2)
        job = rec.jobs[0]
        assert job.running and job.state == "recording", job.status()
        assert job.failures == 0, job.last_error
    finally:
        rec.shutdown()

    files = sorted(seg_dir.glob("*.mp4"))
    assert len(files) >= 2, f"expected several 2s segments, got {files}"
    for f in files:
        assert cr.SEGMENT_RE.match(f.name), f.name
        assert probe_codec(f) == "h264"
        assert probe_duration(f) > 0.5, f"{f.name} should hold real video"
    # the last segment was open when SIGTERM arrived and must still be a valid file
    assert probe_duration(files[-1]) > 0


def test_real_ffmpeg_failure_is_reported_with_redacted_detail(tmp_path):
    cams = {"dead": {"ip": "127.0.0.1", "rtsp": "rtsp://user:topsecret@127.0.0.1:1/stream2"}}
    write_config(tmp_path, cameras=cams, recording={"ffmpeg": FFMPEG, "min_free_gb": 0})
    cfg = cr.load_config(str(tmp_path))
    rec = cr.Recorder(cfg, always=True)
    job = rec.jobs[0]
    try:
        rec.tick()
        wait_until(lambda: job.proc is None or job.proc.poll() is not None, timeout=20,
                   message="ffmpeg to give up on the closed port")
        rec.tick()
    finally:
        rec.shutdown()
    assert job.state == "backoff" and job.failures == 1
    assert "topsecret" not in job.last_error
    assert "ffmpeg exited" in job.last_error
    log_text = (tmp_path / "logs" / "ffmpeg_dead.log").read_text()
    assert "--- start" in log_text
    assert "topsecret" not in log_text.split("\n")[1], "command line in the log is redacted"


def test_check_with_real_ffmpeg(tmp_path, capsys):
    write_config(tmp_path, cameras={"c": {"ip": "1", "rtsp": "rtsp://u:p@h/s"}}, recording={"ffmpeg": FFMPEG})
    assert cr.main(["--check", "--base-dir", str(tmp_path)]) == 0
    assert "ffmpeg version" in capsys.readouterr().out


def test_segment_survives_a_hard_kill(tmp_path):
    """Power loss: the segment being written must still play up to the last fragment.

    One long segment (as in production, where they are 10 minutes) so the kill
    always lands inside an open segment that already holds several fragments,
    instead of possibly hitting a freshly rolled, still empty one.
    """
    cams = {
        "synthetic": {
            "ip": "127.0.0.1",
            "rtsp": "testsrc=size=160x120:rate=10",
            "input_args": ["-re", "-f", "lavfi"],
            "output_args": ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                            "-pix_fmt", "yuv420p", "-g", "10"],
        }
    }
    write_config(tmp_path, cameras=cams, recording={
        "ffmpeg": FFMPEG, "segment_seconds": 600, "audio": "none", "stall_seconds": 30,
        "progress_check_seconds": 0.5, "stop_grace_seconds": 10, "min_free_gb": 0,
    })
    cfg = cr.load_config(str(tmp_path))
    rec = cr.Recorder(cfg, always=True)
    seg_dir = Path(cfg.root) / "synthetic"
    try:
        rec.tick()
        job = rec.jobs[0]
        wait_until(lambda: seg_dir.exists() and any(p.stat().st_size > 10_000 for p in seg_dir.glob("*.mp4")),
                   timeout=25, message="an open segment with a few fragments")
        victim = next(iter(seg_dir.glob("*.mp4")))
        job.proc.kill()  # SIGKILL: no trailer, no chance to clean up
        job.proc.wait(timeout=5)
    finally:
        rec.shutdown()

    # No trailer was written, so the container's own duration may read N/A, but
    # every fragment that reached the disk is still decodable.
    assert probe_codec(victim) == "h264"
    frames = probe_frames(victim)
    assert frames >= 10, f"fragmented MP4 must still decode after SIGKILL, got {frames} frames"
