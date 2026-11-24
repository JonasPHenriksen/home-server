#!/home/dietpi/tapo-automation/tapo-env/bin/python3
"""
cam_recorder.py - lightweight continuous recorder for the Tapo cameras.

Replaces Frigate. One ffmpeg process per camera copies the RTSP stream as-is
(no transcoding, so almost no CPU) into MP4 segments under
recordings/<camera>/YYYY-MM-DD_HH-MM-SS.mp4.

  * recording follows the away/home state file written by
    check_phones_and_toggle.sh: it records while the file says "enable",
  * every camera is supervised on its own: a camera that is offline is retried
    with its own backoff and never delays or stops the others,
  * an ffmpeg that is running but no longer writing data is restarted,
  * segments older than retain_days are deleted, and the oldest segments go
    early when the disk drops below min_free_gb,
  * a camera that keeps failing, and a disk that stays low after pruning, are
    pushed to the phone through ntfy-notify (recording.notify_command), once
    per problem and once more when it is over.

Usage:
    cam_recorder.py [--base-dir DIR] [--always] [--check]

Configuration lives in config.json next to this file (see config.example.json).
"""
import argparse
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DEFAULT_BASE_DIR = "/home/dietpi/tapo-automation"

RECORDING_DEFAULTS = {
    "root": "recordings",              # relative paths are resolved against base_dir
    "control_file": ".tapo_motion_state",
    "record_when": "enable",           # record while the control file holds this value
    "retain_days": 14,
    "min_free_gb": 20,                 # delete oldest segments below this much free space
    "segment_seconds": 600,
    "stall_seconds": 60,               # restart ffmpeg if no data is written for this long
    "audio": "aac",                    # aac | copy | none
    "fragmented_mp4": True,            # segments stay playable after a crash or power cut
    "ffmpeg": None,                    # path to ffmpeg, default: PATH then <base_dir>/bin/ffmpeg
    "input_args": None,                # override the default RTSP input args (list)
    "output_args": None,               # override the default codec args (list)
    "tick_seconds": 1.0,
    "progress_check_seconds": 5.0,
    "prune_interval_seconds": 600,
    "stop_grace_seconds": 10,          # SIGTERM -> SIGKILL after this long
    "backoff_base_seconds": 5,
    "backoff_max_seconds": 300,
    "healthy_after_seconds": 60,       # a run this long resets the backoff
    "ffmpeg_log_max_bytes": 1_000_000,
    "notify_command": None,            # push helper, e.g. /usr/local/bin/ntfy-notify; null = no pushes
    "notify_after_failures": 5,        # consecutive failures of one camera before a push (0 = never)
}

# Same choices Frigate uses for its record role: TCP transport (no packet loss
# on wifi), a 5s socket timeout so a dead camera makes ffmpeg exit instead of
# waiting forever, and wallclock timestamps so segments line up with real time.
DEFAULT_RTSP_INPUT_ARGS = [
    "-rtsp_transport", "tcp",
    "-timeout", "5000000",
    "-avoid_negative_ts", "make_zero",
    "-fflags", "+genpts+discardcorrupt",
    "-use_wallclock_as_timestamps", "1",
]

SEGMENT_PATTERN = "%Y-%m-%d_%H-%M-%S.mp4"
SEGMENT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.mp4$")
CREDENTIALS_RE = re.compile(r"://[^/@\s]+@")

log = logging.getLogger("rec")


class ConfigError(Exception):
    pass


def redact(text):
    """Hide user:password@ in URLs before they reach a log."""
    return CREDENTIALS_RE.sub("://***@", text)


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class CameraSpec:
    name: str
    url: str
    input_args: Optional[List[str]] = None
    output_args: Optional[List[str]] = None


@dataclass
class RecorderConfig:
    root: str
    control_file: Optional[str]
    record_when: str
    retain_days: float
    min_free_gb: float
    segment_seconds: int
    stall_seconds: float
    audio: str
    fragmented_mp4: bool
    ffmpeg: str
    input_args: Optional[List[str]]
    output_args: Optional[List[str]]
    tick_seconds: float
    progress_check_seconds: float
    prune_interval_seconds: float
    stop_grace_seconds: float
    backoff_base_seconds: float
    backoff_max_seconds: float
    healthy_after_seconds: float
    ffmpeg_log_max_bytes: int
    notify_command: Optional[str]
    notify_after_failures: int
    cameras: List[CameraSpec] = field(default_factory=list)


def _resolve_path(base_dir, path):
    if path is None:
        return None
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


def resolve_ffmpeg(base_dir, configured=None):
    """Configured path, else ffmpeg on PATH, else the bundled <base_dir>/bin/ffmpeg."""
    if configured:
        return _resolve_path(base_dir, configured)
    found = shutil.which("ffmpeg")
    if found:
        return found
    bundled = os.path.join(base_dir, "bin", "ffmpeg")
    if os.access(bundled, os.X_OK):
        return bundled
    return "ffmpeg"


def _check_list_of_str(name, value):
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{name}' must be a list of strings")
    return value


def load_config(base_dir):
    path = os.path.join(base_dir, "config.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found (copy config.example.json and fill it in)")
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be an object")

    rec = dict(RECORDING_DEFAULTS)
    user_rec = data.get("recording", {})
    if not isinstance(user_rec, dict):
        raise ConfigError(f"{path}: 'recording' must be an object")
    unknown = set(user_rec) - set(RECORDING_DEFAULTS)
    if unknown:
        raise ConfigError(f"{path}: unknown recording option(s): {', '.join(sorted(unknown))}")
    rec.update(user_rec)

    if rec["audio"] not in ("aac", "copy", "none"):
        raise ConfigError(f"{path}: recording.audio must be aac, copy or none")
    if not isinstance(rec["fragmented_mp4"], bool):
        raise ConfigError(f"{path}: recording.fragmented_mp4 must be true or false")
    for key in ("retain_days", "min_free_gb", "segment_seconds", "stall_seconds",
                "tick_seconds", "progress_check_seconds", "prune_interval_seconds",
                "stop_grace_seconds", "backoff_base_seconds", "backoff_max_seconds",
                "healthy_after_seconds", "ffmpeg_log_max_bytes", "notify_after_failures"):
        if not isinstance(rec[key], (int, float)) or isinstance(rec[key], bool) or rec[key] < 0:
            raise ConfigError(f"{path}: recording.{key} must be a non-negative number")
    if rec["notify_command"] is not None and not isinstance(rec["notify_command"], str):
        raise ConfigError(f"{path}: recording.notify_command must be a string or null")
    if rec["segment_seconds"] < 1:
        raise ConfigError(f"{path}: recording.segment_seconds must be at least 1")

    cams_in = data.get("cameras")
    if not isinstance(cams_in, dict) or not cams_in:
        raise ConfigError(f"{path}: 'cameras' must be a non-empty object")

    cameras = []
    for name, cam in cams_in.items():
        if not isinstance(cam, dict):
            raise ConfigError(f"{path}: camera {name!r} must be an object")
        if not re.match(r"^[A-Za-z0-9_.-]+$", name):
            raise ConfigError(f"{path}: camera name {name!r} may only contain letters, "
                              "digits, '_', '.' and '-' (it becomes a directory name)")
        if cam.get("record") is False:
            continue
        url = cam.get("rtsp")
        if not url:
            # a camera that is only toggled, never recorded
            continue
        if not isinstance(url, str):
            raise ConfigError(f"{path}: camera {name!r}: 'rtsp' must be a string")
        cameras.append(CameraSpec(
            name=name,
            url=url,
            input_args=_check_list_of_str(f"cameras.{name}.input_args", cam.get("input_args")),
            output_args=_check_list_of_str(f"cameras.{name}.output_args", cam.get("output_args")),
        ))

    return RecorderConfig(
        root=_resolve_path(base_dir, rec["root"]),
        control_file=_resolve_path(base_dir, rec["control_file"]),
        record_when=str(rec["record_when"]),
        retain_days=rec["retain_days"],
        min_free_gb=rec["min_free_gb"],
        segment_seconds=int(rec["segment_seconds"]),
        stall_seconds=rec["stall_seconds"],
        audio=rec["audio"],
        fragmented_mp4=rec["fragmented_mp4"],
        ffmpeg=resolve_ffmpeg(base_dir, rec["ffmpeg"]),
        input_args=_check_list_of_str("recording.input_args", rec["input_args"]),
        output_args=_check_list_of_str("recording.output_args", rec["output_args"]),
        tick_seconds=rec["tick_seconds"],
        progress_check_seconds=rec["progress_check_seconds"],
        prune_interval_seconds=rec["prune_interval_seconds"],
        stop_grace_seconds=rec["stop_grace_seconds"],
        backoff_base_seconds=rec["backoff_base_seconds"],
        backoff_max_seconds=rec["backoff_max_seconds"],
        healthy_after_seconds=rec["healthy_after_seconds"],
        ffmpeg_log_max_bytes=int(rec["ffmpeg_log_max_bytes"]),
        notify_command=_resolve_path(base_dir, rec["notify_command"] or None),
        notify_after_failures=int(rec["notify_after_failures"]),
        cameras=cameras,
    )


# --------------------------------------------------------------------------- ffmpeg command

def build_command(cfg, cam, cam_dir):
    # No -nostdin: ffmpeg 7.0 only checks for a stop request (the 'q' key or a
    # signal) inside its stdin interaction loop, so the pipe is what lets us
    # stop it gracefully and get a finalised MP4.
    cmd = [cfg.ffmpeg, "-hide_banner", "-loglevel", "warning"]

    input_args = cam.input_args if cam.input_args is not None else cfg.input_args
    if input_args is None and cam.url.startswith("rtsp://"):
        input_args = DEFAULT_RTSP_INPUT_ARGS
    cmd += input_args or []
    cmd += ["-i", cam.url]

    output_args = cam.output_args if cam.output_args is not None else cfg.output_args
    if output_args is None:
        output_args = ["-c:v", "copy"]
        if cfg.audio == "aac":
            output_args += ["-c:a", "aac", "-b:a", "32k"]   # Tapo sends PCM, which MP4 cannot hold
        elif cfg.audio == "copy":
            output_args += ["-c:a", "copy"]
        else:
            output_args += ["-an"]
    cmd += output_args

    # flush_packets: write through instead of buffering 32 KB, so the file grows
    # continuously. Fragmented MP4 additionally closes a self-contained fragment
    # at every keyframe, so a segment cut short by a crash or power loss plays up
    # to the last fragment instead of being unreadable without its index.
    format_options = "flush_packets=1"
    if cfg.fragmented_mp4:
        format_options = "movflags=+frag_keyframe+empty_moov+default_base_moof:" + format_options

    cmd += [
        "-f", "segment",
        "-segment_time", str(cfg.segment_seconds),
        "-segment_atclocktime", "1",        # cut on the wall clock: :00, :10, :20 ...
        "-segment_format", "mp4",
        "-segment_format_options", format_options,
        "-reset_timestamps", "1",
        "-strftime", "1",
        os.path.join(cam_dir, SEGMENT_PATTERN),
    ]
    return cmd


# --------------------------------------------------------------------------- helpers

def newest_segment(cam_dir):
    """(name, size) of the newest segment in cam_dir, or None."""
    try:
        names = [e for e in os.listdir(cam_dir) if SEGMENT_RE.match(e)]
    except FileNotFoundError:
        return None
    if not names:
        return None
    name = max(names)  # names sort chronologically
    try:
        return name, os.path.getsize(os.path.join(cam_dir, name))
    except OSError:
        return None


def private_dir(path):
    """The logs folder: ffmpeg's own messages carry the RTSP URL, password
    included (only what this script writes is redacted), so only the owner may
    read it."""
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)


def open_private_log(path):
    """Append to an ffmpeg log that only the owner can read, whatever its old mode."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab")


def tail_lines(path, n=5):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [l.strip() for l in data.splitlines() if l.strip()]
    return [redact(l) for l in lines[-n:]]


def write_json_atomic(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def read_control(path, record_when, previous):
    """Desired recording state from the control file.

    Missing file: no state is known, do not record. Empty file: the writer is
    half way through `echo x > file`, keep the previous decision. Anything
    else: record only if it equals record_when.
    """
    if path is None:
        return True
    try:
        with open(path) as f:
            value = f.read().strip()
    except FileNotFoundError:
        return False
    except OSError:
        return previous
    if value == "":
        return previous
    return value == record_when


# --------------------------------------------------------------------------- notifications

class Notifier:
    """Push notifications through an external helper (ntfy-notify), fire and forget.

    The helper runs in the background so a slow or dead ntfy server can never
    hold up the supervisor loop; finished helpers are reaped on the next tick
    and a failing one is logged. Without a command every call is a no-op.
    """

    def __init__(self, command):
        self.command = command
        self._running = []

    @property
    def enabled(self):
        return bool(self.command)

    def send(self, title, message, priority="default", tags="video_camera"):
        """Start the helper. Returns True if it was started."""
        if not self.command:
            return False
        try:
            proc = subprocess.Popen(
                [self.command, "-p", priority, "-t", tags, title, redact(message)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                start_new_session=True)
        except OSError as e:
            log.warning("cannot run %s: %s", self.command, e)
            return False
        self._running.append((proc, title))
        log.info("notification: %s", title)
        return True

    def reap(self):
        still = []
        for proc, title in self._running:
            if proc.poll() is None:
                still.append((proc, title))
                continue
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            proc.stderr.close()
            if proc.returncode != 0:
                log.warning("notification %r failed (rc=%s): %s", title, proc.returncode, err or "no output")
        self._running = still

    def wait(self, timeout=10.0):
        """Give pending helpers a moment to finish (shutdown)."""
        deadline = time.time() + timeout
        for proc, _ in self._running:
            try:
                proc.wait(timeout=max(0.0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()
        self.reap()


# --------------------------------------------------------------------------- per camera supervisor

class CameraJob:
    """Owns the ffmpeg process of one camera. All timing uses the injected clock."""

    def __init__(self, spec, cfg, clock, notifier=None):
        self.spec = spec
        self.cfg = cfg
        self.clock = clock
        self.notifier = notifier or Notifier(None)
        self.dir = os.path.join(cfg.root, spec.name)
        self.log_dir = os.path.join(os.path.dirname(cfg.root), "logs")
        self.ffmpeg_log = os.path.join(self.log_dir, f"ffmpeg_{spec.name}.log")

        self.proc = None
        self._log_fh = None
        self.started_at = None
        self.stopping_since = None
        self.stop_is_failure = False
        self.failures = 0
        self.next_start_at = 0.0
        self.last_progress_at = None
        self.last_marker = None
        self.last_progress_check_at = None
        self.last_error = None
        self.state = "idle"
        self.notified = False   # a push about the current failure streak went out

    # -- public

    @property
    def running(self):
        return self.proc is not None

    def tick(self, want_record):
        now = self.clock()

        if self.proc is not None:
            rc = self.proc.poll()
            if rc is not None:
                self._on_exit(now, rc)
            elif self.stopping_since is not None:
                if now - self.stopping_since >= self.cfg.stop_grace_seconds:
                    log.warning("[%s] ffmpeg ignored SIGTERM for %.0fs, killing it",
                                self.spec.name, now - self.stopping_since)
                    self._kill()
            elif not want_record:
                self._begin_stop(now, "recording disabled", failure=False)
            else:
                self._check_progress(now)

        if self.proc is None and want_record and now >= self.next_start_at:
            self._start(now)

    def request_stop(self):
        """Begin a graceful stop (used at shutdown). Non-blocking."""
        if self.proc is not None and self.stopping_since is None:
            self._begin_stop(self.clock(), "shutdown", failure=False)

    def wait_stopped(self, deadline_wall):
        """Block until ffmpeg is gone, killing it once deadline_wall (time.time) passes."""
        while self.proc is not None:
            rc = self.proc.poll()
            if rc is not None:
                self._on_exit(self.clock(), rc)
                break
            if time.time() >= deadline_wall:
                self._kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                self._on_exit(self.clock(), self.proc.returncode)
                break
            time.sleep(0.05)

    def status(self):
        now = self.clock()
        return {
            "state": self.state,
            "pid": self.proc.pid if self.proc is not None else None,
            "failures": self.failures,
            "running_for_seconds": round(now - self.started_at) if self.proc is not None else None,
            "retry_in_seconds": max(0, round(self.next_start_at - now)) if self.state == "backoff" else None,
            "last_error": self.last_error,
        }

    # -- internals

    def _start(self, now):
        try:
            os.makedirs(self.dir, exist_ok=True)
            private_dir(self.log_dir)
            self._rotate_ffmpeg_log()
            self._log_fh = open_private_log(self.ffmpeg_log)
            cmd = build_command(self.cfg, self.spec, self.dir)
            self._log_fh.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')}: "
                               f"{redact(' '.join(cmd))}\n".encode())
            self._log_fh.flush()
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._log_fh,
                start_new_session=True,
            )
        except OSError as e:
            self._close_log()
            self.proc = None
            self._record_failure(now, f"cannot start ffmpeg ({self.cfg.ffmpeg}): {e}")
            return

        self.started_at = now
        self.stopping_since = None
        self.stop_is_failure = False
        self.last_marker = newest_segment(self.dir)
        self.last_progress_at = now
        self.last_progress_check_at = now
        self.state = "recording"
        log.info("[%s] ffmpeg started (pid %d, failures so far %d)",
                 self.spec.name, self.proc.pid, self.failures)

    def _check_progress(self, now):
        if now - self.last_progress_check_at < self.cfg.progress_check_seconds:
            return
        self.last_progress_check_at = now
        marker = newest_segment(self.dir)
        if marker != self.last_marker:
            self.last_marker = marker
            self.last_progress_at = now
            if self.notified and now - self.started_at >= self.cfg.healthy_after_seconds:
                self._notify_recovered(now - self.started_at)
        elif now - self.last_progress_at >= self.cfg.stall_seconds:
            log.warning("[%s] no data written for %.0fs, restarting ffmpeg",
                        self.spec.name, now - self.last_progress_at)
            self._begin_stop(now, "stalled", failure=True)

    def _begin_stop(self, now, reason, failure):
        log.info("[%s] stopping ffmpeg (%s)", self.spec.name, reason)
        self.stopping_since = now
        self.stop_is_failure = failure
        self.last_error = reason if failure else self.last_error
        self.state = "stopping"
        # 'q' is ffmpeg's documented quit key and works on every version; SIGTERM
        # is the fallback for builds that honour it. Either way ffmpeg writes the
        # MP4 trailer, so the open segment stays playable.
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.write(b"q\n")
                self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            self.proc.terminate()
        except OSError:
            pass

    def _kill(self):
        try:
            self.proc.kill()
        except OSError:
            pass

    def _on_exit(self, now, rc):
        ran_for = now - (self.started_at if self.started_at is not None else now)
        was_stopping = self.stopping_since is not None
        failure = (not was_stopping) or self.stop_is_failure
        self._close_log()
        self.proc = None
        self.stopping_since = None

        if not failure:
            log.info("[%s] ffmpeg stopped (rc=%s) after %.0fs", self.spec.name, rc, ran_for)
            self.state = "idle"
            if ran_for >= self.cfg.healthy_after_seconds:
                # A long, healthy run that we asked to stop clears an old streak,
                # so status.json does not keep reporting a problem that is over
                # and the next real failure starts from the shortest backoff.
                self.failures = 0
                self.last_error = None
                if self.notified:
                    self._notify_recovered(ran_for)
            return

        if ran_for >= self.cfg.healthy_after_seconds:
            self.failures = 0  # it worked for a while: this is a fresh problem
        detail = self.last_error if was_stopping else "; ".join(tail_lines(self.ffmpeg_log)) or f"rc={rc}"
        self._record_failure(now, f"ffmpeg exited rc={rc} after {ran_for:.0f}s: {detail}")

    def _record_failure(self, now, message):
        self.failures += 1
        delay = min(self.cfg.backoff_base_seconds * 2 ** (self.failures - 1),
                    self.cfg.backoff_max_seconds)
        self.next_start_at = now + delay
        self.last_error = message
        self.state = "backoff"
        log.warning("[%s] %s (failure %d), retrying in %.0fs",
                    self.spec.name, redact(message), self.failures, delay)
        # One push per outage: `notified` stays set until the camera has really
        # recorded again for healthy_after_seconds, however often the streak
        # counter is reset in between.
        limit = self.cfg.notify_after_failures
        if limit and self.failures >= limit and not self.notified:
            self.notified = self.notifier.send(
                f"{self.spec.name}: recording keeps failing",
                f"{self.failures} failures in a row. Last: {message}. Next retry in {delay:.0f}s.",
                priority="high", tags="warning,video_camera")

    def _notify_recovered(self, ran_for):
        self.notified = False
        self.notifier.send(f"{self.spec.name}: recording again",
                           f"ffmpeg has been writing for {ran_for:.0f}s.",
                           priority="low", tags="white_check_mark,video_camera")

    def _rotate_ffmpeg_log(self):
        try:
            if os.path.getsize(self.ffmpeg_log) > self.cfg.ffmpeg_log_max_bytes:
                os.replace(self.ffmpeg_log, self.ffmpeg_log + ".1")
        except OSError:
            pass

    def _close_log(self):
        if self.proc is not None and self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None


# --------------------------------------------------------------------------- retention

def prune_recordings(cfg, now_wall=None, disk_usage=shutil.disk_usage):
    """Delete segments older than retain_days, then oldest-first while the disk is low.

    Returns the number of deleted files. Only touches the directories of the
    configured cameras and never a file written within the last two segments.
    """
    now_wall = time.time() if now_wall is None else now_wall
    deleted = 0
    cutoff = now_wall - cfg.retain_days * 86400
    live_margin = now_wall - 2 * cfg.segment_seconds

    all_files = []
    for cam in cfg.cameras:
        cam_dir = os.path.join(cfg.root, cam.name)
        try:
            entries = list(os.scandir(cam_dir))
        except FileNotFoundError:
            continue
        for entry in entries:
            if not SEGMENT_RE.match(entry.name):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if cfg.retain_days > 0 and st.st_mtime < cutoff:
                if _remove(entry.path):
                    deleted += 1
                continue
            all_files.append((st.st_mtime, st.st_size, entry.path))

    if cfg.min_free_gb > 0 and os.path.isdir(cfg.root):
        need = cfg.min_free_gb * 1024 ** 3
        free = disk_usage(cfg.root).free
        if free < need:
            all_files.sort()
            for mtime, size, path in all_files:
                if free >= need:
                    break
                if mtime > live_margin:
                    continue  # probably being written right now
                if _remove(path):
                    deleted += 1
                    free += size
            if free < need:
                log.warning("disk still low after pruning: %.1f GiB free, want %.1f GiB",
                            free / 1024 ** 3, cfg.min_free_gb)
            else:
                log.info("low disk: freed space down to %.1f GiB free", free / 1024 ** 3)
    return deleted


def _remove(path):
    try:
        os.remove(path)
        return True
    except OSError as e:
        log.warning("could not delete %s: %s", path, e)
        return False


# --------------------------------------------------------------------------- recorder

class Recorder:
    def __init__(self, cfg, clock=time.monotonic, always=False, disk_usage=shutil.disk_usage):
        self.cfg = cfg
        self.clock = clock
        self.always = always
        self.disk_usage = disk_usage
        self.notifier = Notifier(cfg.notify_command)
        self.jobs = [CameraJob(spec, cfg, clock, self.notifier) for spec in cfg.cameras]
        self.want_record = False
        self.last_prune_at = None
        self.disk_notified = False
        self.last_status = None
        self.status_path = os.path.join(cfg.root, "status.json")
        self._stop_requested = False

    def desired_state(self):
        if self.always:
            return True
        return read_control(self.cfg.control_file, self.cfg.record_when, self.want_record)

    def tick(self):
        now = self.clock()
        want = self.desired_state()
        if want != self.want_record:
            log.info("recording %s (control file %s)",
                     "ON" if want else "OFF", self.cfg.control_file or "n/a")
            self.want_record = want

        for job in self.jobs:
            try:
                job.tick(want)
            except Exception:  # one camera's trouble must never stop the others
                log.exception("[%s] unexpected error in supervisor", job.spec.name)

        if self.last_prune_at is None or now - self.last_prune_at >= self.cfg.prune_interval_seconds:
            self.last_prune_at = now
            try:
                n = prune_recordings(self.cfg, disk_usage=self.disk_usage)
                if n:
                    log.info("pruned %d old segment(s)", n)
            except Exception:
                log.exception("pruning failed")
            self.check_disk()

        self.notifier.reap()
        self.write_status()

    def check_disk(self):
        """Push once when the disk stays below min_free_gb after pruning, and once when it is back."""
        if self.cfg.min_free_gb <= 0 or not self.notifier.enabled:
            return
        try:
            free = self.disk_usage(self.cfg.root).free
        except OSError:
            return
        need = self.cfg.min_free_gb * 1024 ** 3
        if free < need and not self.disk_notified:
            self.disk_notified = self.notifier.send(
                "Recording disk is low",
                f"{free / 1024 ** 3:.1f} GiB free under {self.cfg.root}, want {self.cfg.min_free_gb:g} GiB. "
                "Old segments are already being deleted early; something else is filling the disk.",
                priority="high", tags="warning,floppy_disk")
        elif free >= need and self.disk_notified:
            self.disk_notified = False
            self.notifier.send("Recording disk OK again",
                               f"{free / 1024 ** 3:.1f} GiB free under {self.cfg.root}.",
                               priority="low", tags="white_check_mark,floppy_disk")

    def write_status(self):
        status = {
            "recording_wanted": self.want_record,
            "cameras": {job.spec.name: job.status() for job in self.jobs},
        }
        if status != self.last_status:
            self.last_status = status
            try:
                os.makedirs(self.cfg.root, exist_ok=True)
                write_json_atomic(self.status_path,
                                  dict(status, updated=time.strftime("%Y-%m-%d %H:%M:%S")))
            except OSError as e:
                log.warning("could not write %s: %s", self.status_path, e)

    def shutdown(self):
        for job in self.jobs:
            job.request_stop()
        deadline = time.time() + self.cfg.stop_grace_seconds
        for job in self.jobs:
            job.wait_stopped(deadline)
        self.notifier.wait()
        self.write_status()

    def request_stop(self, *_):
        self._stop_requested = True

    def run(self):
        log.info("cam_recorder starting: %d camera(s), ffmpeg=%s, root=%s, retain=%sd, "
                 "segments=%ss, control=%s",
                 len(self.jobs), self.cfg.ffmpeg, self.cfg.root, self.cfg.retain_days,
                 self.cfg.segment_seconds, "always" if self.always else self.cfg.control_file)
        if not shutil.which(self.cfg.ffmpeg) and not os.access(self.cfg.ffmpeg, os.X_OK):
            log.error("ffmpeg not found at %r; install it (apt install ffmpeg) or set "
                      "recording.ffmpeg in config.json. Will keep retrying.", self.cfg.ffmpeg)
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        try:
            while not self._stop_requested:
                self.tick()
                time.sleep(self.cfg.tick_seconds)
        finally:
            log.info("cam_recorder stopping, waiting for ffmpeg to finalise segments")
            self.shutdown()
            log.info("cam_recorder stopped")


# --------------------------------------------------------------------------- cli

def setup_logging(base_dir):
    log_dir = os.path.join(base_dir, "logs")
    private_dir(log_dir)
    fmt = logging.Formatter("%(asctime)s [rec] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "cam_recorder.log"), maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler], force=True)


def check(cfg):
    """Print the resolved configuration and verify ffmpeg runs. Returns exit code."""
    print(f"ffmpeg:        {cfg.ffmpeg}")
    try:
        out = subprocess.run([cfg.ffmpeg, "-version"], capture_output=True, text=True, timeout=15)
        print(f"               {out.stdout.splitlines()[0] if out.stdout else out.stderr.strip()}")
        ok = out.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"               NOT RUNNABLE: {e}")
        ok = False
    print(f"recordings:    {cfg.root}")
    print(f"control file:  {cfg.control_file} (record while it says {cfg.record_when!r})")
    print(f"retention:     {cfg.retain_days} days, keep {cfg.min_free_gb} GiB free")
    print(f"segments:      {cfg.segment_seconds}s, audio={cfg.audio}")
    if cfg.notify_command:
        runnable = os.access(cfg.notify_command, os.X_OK)
        print(f"notifications: {cfg.notify_command} after {cfg.notify_after_failures} failures"
              f"{'' if runnable else '  (NOT EXECUTABLE)'}")
    else:
        print("notifications: off (set recording.notify_command)")
    print(f"cameras:       {len(cfg.cameras)}")
    for cam in cfg.cameras:
        cmd = build_command(cfg, cam, os.path.join(cfg.root, cam.name))
        print(f"  {cam.name}: {redact(' '.join(cmd))}")
    return 0 if ok else 1


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base-dir", default=os.environ.get("TAPO_BASE_DIR", DEFAULT_BASE_DIR),
                   help="project directory holding config.json")
    p.add_argument("--always", action="store_true",
                   help="record regardless of the control file")
    p.add_argument("--check", action="store_true",
                   help="print the resolved config and ffmpeg commands, then exit")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        cfg = load_config(args.base_dir)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.check:
        return check(cfg)
    setup_logging(args.base_dir)
    Recorder(cfg, always=args.always).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
