#!/home/dietpi/tapo-automation/tapo-env/bin/python3
"""
Put the Tapo cameras into the "away" (enable) or "home" (disable) configuration.

    tapo_toggle.py enable|disable all|night [--force] [--deadline SECS]

Design goals (exercised by tests/test_tapo_toggle.py):

  * cameras are configured in parallel, so one unreachable camera cannot delay
    the others,
  * the whole run has a hard deadline, so a hanging camera cannot stall the
    presence loop that calls this script every ~10s,
  * per-camera sync state (.tapo_cam_state.json) means a retry only touches the
    cameras that are still out of sync.

Cameras are read from config.json (see config.example.json), credentials from
.tapo_credentials (username on line 1, password on line 2).

Exit codes: 0 all cameras in sync, 1 at least one camera still out of sync,
2 bad usage or configuration.
"""
import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

DEFAULT_BASE_DIR = "/home/dietpi/tapo-automation"

LOG_MAX_LINES = 2000
LOG_KEEP_LINES = 1000

# In-process retries stay short: the shell callers retry with their own backoff.
MAX_RETRIES = 3
BASE_DELAY = 2.0          # seconds, doubles per attempt: 2, 4
SETTLE_DELAY = 1.0        # pause around privacy-mode changes so the camera keeps up
DEFAULT_DEADLINE = 60.0   # seconds for the whole run, all cameras included
SOCKET_TIMEOUT = 10.0     # guards any request pytapo makes without its own timeout

# Retrying these makes the camera's login lockout worse, so abort instead.
AUTH_ERROR_MARKERS = ("authentication", "suspension", "credential")

NIGHT_MODE_FILE = ".tapo_night_mode"   # written by night_mode.sh, see night_common.sh
NIGHT_DEFAULT_WINDOW = ("21:00", "02:00")

log = logging.getLogger("tapo")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Camera:
    name: str
    ip: str
    night: bool  # "night" group: watches quietly at night when night mode is on (night_mode.sh)


# --------------------------------------------------------------------------- config

def load_config(base_dir):
    path = os.path.join(base_dir, "config.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found (copy config.example.json and fill it in)")
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}")

    cams = data.get("cameras") if isinstance(data, dict) else None
    if not isinstance(cams, dict) or not cams:
        raise ConfigError(f"{path}: 'cameras' must be a non-empty object")

    cameras = []
    for name, cam in cams.items():
        if not isinstance(cam, dict) or not cam.get("ip"):
            raise ConfigError(f"{path}: camera {name!r} needs an 'ip'")
        group = cam.get("group", "always")
        if group not in ("always", "night"):
            raise ConfigError(f"{path}: camera {name!r} has unknown group {group!r} "
                              "(use 'always' or 'night')")
        cameras.append(Camera(name=name, ip=cam["ip"], night=(group == "night")))
    return cameras


def load_credentials(base_dir):
    path = os.path.join(base_dir, ".tapo_credentials")
    try:
        with open(path) as f:
            user = f.readline().strip()
            password = f.readline().strip()
    except FileNotFoundError:
        raise ConfigError(f"{path} not found")
    if not user or not password:
        raise ConfigError(f"{path} must hold the username on line 1 and the password on line 2")
    return user, password


# --------------------------------------------------------------------------- state

class CamState:
    """What each camera is currently believed to be set to (thread-safe)."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f)
        os.replace(tmp, self.path)

    def get(self, ip):
        with self._lock:
            return self._data.get(ip)

    def set(self, ip, sig):
        with self._lock:
            self._data[ip] = sig
            self._save()

    def forget(self, ip):
        with self._lock:
            if self._data.pop(ip, None) is not None:
                self._save()


# --------------------------------------------------------------------------- camera logic

@dataclass(frozen=True)
class NightMode:
    start: int                  # minutes since midnight
    end: int                    # may be smaller than start: the window crosses midnight
    expires: Optional[int]      # epoch, only set by `night_mode.sh tonight`

    @property
    def window(self):
        return f"{self.start // 60:02d}:{self.start % 60:02d}-{self.end // 60:02d}:{self.end % 60:02d}"


def parse_hhmm(text):
    """'21', '21:30', '7:05' -> minutes since midnight. Raises ValueError."""
    hours, sep, minutes = text.strip().partition(":")
    if sep and not minutes:
        raise ValueError(text)
    h = int(hours)
    m = int(minutes) if minutes else 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(text)
    return h * 60 + m


def read_night_mode(base_dir):
    """The night mode setting written by night_mode.sh, or None when it is off.

    Same rules as night_common.sh: key=value lines start/end/expires; an empty
    file or a bare epoch means the default window.
    """
    try:
        with open(os.path.join(base_dir, NIGHT_MODE_FILE)) as f:
            content = f.read()
    except FileNotFoundError:
        return None
    start, end = NIGHT_DEFAULT_WINDOW
    expires = None
    for line in content.splitlines():
        line = "".join(line.split())
        if not line:
            continue
        key, sep, val = line.partition("=")
        if sep:
            if key == "start":
                start = val
            elif key == "end":
                end = val
            elif key == "expires" and val.isdigit():
                expires = int(val)
        elif line.isdigit():
            expires = int(line)
    try:
        s, e = parse_hhmm(start), parse_hhmm(end)
    except ValueError:
        s, e = parse_hhmm(NIGHT_DEFAULT_WINDOW[0]), parse_hhmm(NIGHT_DEFAULT_WINDOW[1])
    if s == e:
        e = parse_hhmm(NIGHT_DEFAULT_WINDOW[1])
    return NightMode(start=s, end=e, expires=expires)


def in_window(now_min, start, end):
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def night_rules_apply(base_dir, now=None):
    """The night group watches quietly right now: night mode is on, not expired, window open."""
    mode = read_night_mode(base_dir)
    if mode is None:
        return False
    now = now or datetime.now()
    if mode.expires is not None and mode.expires <= now.timestamp():
        return False
    return in_window(now.hour * 60 + now.minute, mode.start, mode.end)


def target_settings(enable, night_cam, is_night):
    if night_cam and is_night:
        return {"privacy": False, "alarm": False, "features": True}
    return {"privacy": not enable, "alarm": enable, "features": enable}


def signature(target):
    """Short id of a settings combination, used as the per-camera state value."""
    return "p%da%df%d" % (target["privacy"], target["alarm"], target["features"])


def is_auth_error(exc):
    msg = str(exc).lower()
    return any(marker in msg for marker in AUTH_ERROR_MARKERS)


def default_camera_factory(ip, user, password):
    from pytapo import Tapo  # imported lazily so the tests do not need pytapo
    return Tapo(ip, user, password)


def apply_settings(cam, target, sleep=time.sleep):
    cam.setPrivacyMode(False)
    sleep(SETTLE_DELAY)

    cam.setAlarm(target["alarm"])
    cam.setMotionDetection(target["features"])
    cam.setNotificationsEnabled(target["features"])
    cam.setPersonDetection(target["features"])
    cam.setTamperDetection(target["features"])

    sleep(SETTLE_DELAY)
    cam.setPrivacyMode(target["privacy"])


def configure_camera(camera, target, user, password, camera_factory,
                     deadline, clock=time.monotonic, sleep=time.sleep):
    """Apply `target` to one camera with short retries. Returns True on success."""
    sig = signature(target)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            cam = camera_factory(camera.ip, user, password)
            apply_settings(cam, target, sleep)
            log.info("[%s] %s set to %s (attempt %d/%d)",
                     camera.ip, camera.name, sig, attempt, MAX_RETRIES)
            return True
        except Exception as e:  # pytapo raises plain Exception for most errors
            if is_auth_error(e):
                log.error("[%s] %s auth error, NOT retrying to avoid lockout: %s",
                          camera.ip, camera.name, e)
                return False
            delay = BASE_DELAY * 2 ** (attempt - 1)
            if attempt < MAX_RETRIES and clock() + delay < deadline:
                log.warning("[%s] %s attempt %d/%d failed (%s: %s), retrying in %.0fs",
                            camera.ip, camera.name, attempt, MAX_RETRIES,
                            type(e).__name__, e, delay)
                sleep(delay)
            else:
                log.error("[%s] %s unreachable after %d attempt(s) (%s: %s), "
                          "leaving it out of sync for the next run",
                          camera.ip, camera.name, attempt, type(e).__name__, e)
                return False
    return False


# --------------------------------------------------------------------------- run

def select_cameras(cameras, group):
    if group == "all":
        return list(cameras)
    return [c for c in cameras if c.night]


def run(action, group, base_dir, force=False, deadline_secs=DEFAULT_DEADLINE,
        camera_factory=None, clock=time.monotonic, sleep=time.sleep, is_night=None, now=None):
    """Configure the selected cameras. Returns the process exit code.

    `is_night` forces the night rules on or off; by default they follow
    night_mode.sh and the wall clock (`now`, a datetime, defaults to now).
    """
    cameras = load_config(base_dir)
    user, password = load_credentials(base_dir)
    if camera_factory is None:
        camera_factory = default_camera_factory
    if is_night is None:
        is_night = night_rules_apply(base_dir, now)

    enable = action == "enable"
    log.info("Mode=%s, NightRules=%s, group=%s%s",
             "Away" if enable else "Home", "on" if is_night else "off", group,
             ", forced" if force else "")

    state = CamState(os.path.join(base_dir, ".tapo_cam_state.json"))
    deadline = clock() + deadline_secs

    todo = []
    for cam in select_cameras(cameras, group):
        target = target_settings(enable, cam.night, is_night)
        sig = signature(target)
        if not force and state.get(cam.ip) == sig:
            log.info("[%s] %s already at %s, skipping", cam.ip, cam.name, sig)
            continue
        # Forget the cached value before touching the camera: if we die halfway
        # through apply_settings the next run has to try again.
        state.forget(cam.ip)
        todo.append((cam, target, sig))

    results = {}
    results_lock = threading.Lock()
    closed = threading.Event()  # set once the deadline has passed; late results are ignored

    def worker(cam, target, sig):
        ok = configure_camera(cam, target, user, password, camera_factory,
                              deadline, clock=clock, sleep=sleep)
        with results_lock:
            if closed.is_set():
                log.warning("[%s] %s finished after the deadline, result ignored "
                            "(will be re-checked next run)", cam.ip, cam.name)
                return
            results[cam.ip] = ok
            if ok:
                state.set(cam.ip, sig)

    # Daemon threads: a camera that hangs inside pytapo must not keep the
    # interpreter alive after the deadline.
    threads = [threading.Thread(target=worker, args=item, daemon=True, name=item[0].name)
               for item in todo]
    for t in threads:
        t.start()
    for t in threads:
        remaining = deadline - clock()
        if remaining > 0:
            t.join(remaining)

    with results_lock:
        closed.set()
        timed_out = [cam.name for cam, _, _ in todo if cam.ip not in results]
        out_of_sync = [cam.name for cam, _, _ in todo if not results.get(cam.ip)]

    if timed_out:
        log.error("deadline of %.0fs reached, still busy: %s", deadline_secs, ", ".join(timed_out))
    if out_of_sync:
        log.error("still out of sync: %s", ", ".join(out_of_sync))
        return 1

    log.info("all targets in sync")
    return 0


# --------------------------------------------------------------------------- cli

def trim_log(path):
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    if len(lines) > LOG_MAX_LINES:
        with open(path, "w") as f:
            f.writelines(lines[-LOG_KEEP_LINES:])


def setup_logging(base_dir):
    log_file = os.path.join(base_dir, "tapo.log")
    trim_log(log_file)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [tapo] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
        force=True,
    )


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("action", choices=["enable", "disable"],
                   help="enable = away (armed), disable = home (privacy mode)")
    p.add_argument("group", choices=["all", "night"],
                   help="all cameras, or only the 'night' group")
    p.add_argument("--force", action="store_true",
                   help="reconfigure cameras even if they are believed to be in sync")
    p.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE, metavar="SECS",
                   help=f"give up on cameras still busy after this long (default {DEFAULT_DEADLINE:.0f})")
    p.add_argument("--base-dir", default=os.environ.get("TAPO_BASE_DIR", DEFAULT_BASE_DIR),
                   help="project directory holding config.json and the state files")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    setup_logging(args.base_dir)
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    try:
        return run(args.action, args.group, args.base_dir,
                   force=args.force, deadline_secs=args.deadline)
    except ConfigError as e:
        log.error("config error: %s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
