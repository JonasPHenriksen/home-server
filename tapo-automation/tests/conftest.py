import fcntl
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The scripts print and compare host-local times. Pin the zone so the expected
# strings are the same on every machine (the production host runs on UTC too).
os.environ["TZ"] = "UTC"
time.tzset()

FAKE_FFMPEG = ROOT / "tests" / "fake_ffmpeg.py"

CAM_1 = "192.168.50.16"
CAM_2 = "192.168.50.12"
CAM_3 = "192.168.50.14"

DEFAULT_CAMERAS = {
    "cam_1": {"ip": CAM_1, "group": "always", "rtsp": "fake://record"},
    "cam_2": {"ip": CAM_2, "group": "always", "rtsp": "fake://record"},
    "cam_3": {"ip": CAM_3, "group": "night", "rtsp": "fake://record"},
}

DEFAULT_PHONES = {
    "phone_1": {"ip": "10.0.0.1"},
    "phone_2": {"ip": "10.0.0.2"},
}

# Fast timings so the supervisor tests finish quickly. Everything the recorder
# times is driven by an injected clock, so these mostly matter for real waits.
FAST_RECORDING = {
    "ffmpeg": str(FAKE_FFMPEG),
    "tick_seconds": 0.05,
    "progress_check_seconds": 0.5,
    "prune_interval_seconds": 600,
    "stop_grace_seconds": 2,
    "backoff_base_seconds": 5,
    "backoff_max_seconds": 40,
    "healthy_after_seconds": 60,
    "stall_seconds": 3,
    "segment_seconds": 2,
    "retain_days": 14,
    "min_free_gb": 0,
    "audio": "none",
}


def write_config(base, cameras=None, recording=None, phones=None, presence=None):
    cfg = {
        "phones": DEFAULT_PHONES if phones is None else phones,
        "presence": presence or {},
        "cameras": DEFAULT_CAMERAS if cameras is None else cameras,
        "recording": dict(FAST_RECORDING, **(recording or {})),
    }
    (Path(base) / "config.json").write_text(json.dumps(cfg, indent=2))
    return cfg


@pytest.fixture
def base(tmp_path):
    """A project directory with credentials and a default config.json."""
    (tmp_path / ".tapo_credentials").write_text("admin\nsecret\n")
    write_config(tmp_path)
    return tmp_path


def wait_until(predicate, timeout=5.0, interval=0.02, message="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


# --------------------------------------------------------------------------- shell script harness

class ShellHarness:
    """Runs one of the bash scripts against fakes in a temp directory.

    The fake toggle appends its arguments to toggle_calls.log, exits with the
    code in `toggle_rc` (default 0) and sleeps `toggle_sleep` seconds first if
    that file exists. The fake presence probe reports "home" when phones_home
    lists any IP, exits with `probe_rc` if that file exists and sleeps
    `probe_sleep` seconds first if that one does. The fake push helper appends
    its arguments to notify_calls.log (see notify_calls()).
    """

    def __init__(self, base, script):
        self.base = Path(base)
        self.script = ROOT / script
        self.calls_file = self.base / "toggle_calls.log"

        self.fake_toggle = self.base / "fake_toggle.sh"
        self.fake_toggle.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "$*" >> "{self.calls_file}"
            if [ -f "{self.base}/toggle_sleep" ]; then sleep "$(cat "{self.base}/toggle_sleep")"; fi
            rc=0
            [ -f "{self.base}/toggle_rc" ] && rc=$(cat "{self.base}/toggle_rc")
            exit "$rc"
            """))
        self.fake_toggle.chmod(0o755)

        self.fake_probe = self.base / "fake_probe.sh"
        self.fake_probe.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            if [ -f "{self.base}/probe_sleep" ]; then sleep "$(cat "{self.base}/probe_sleep")"; fi
            if [ -f "{self.base}/probe_rc" ]; then echo "probe exploded"; exit "$(cat "{self.base}/probe_rc")"; fi
            if [ -s "{self.base}/phones_home" ]; then
                echo "home: $(tr '\\n' ' ' < "{self.base}/phones_home" | sed 's/ $//') via fake"
                exit 0
            fi
            echo "away: nobody answered"
            exit 1
            """))
        self.fake_probe.chmod(0o755)

        self.fake_notify = self.base / "fake_notify.sh"
        self.notify_calls_file = self.base / "notify_calls.log"
        self.fake_notify.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> "{self.notify_calls_file}"
            """))
        self.fake_notify.chmod(0o755)

        self.toggle_timeout = 10
        self.probe_timeout = 10

    def env(self, now=None, hour=None, time=None, extra=None):
        env = dict(os.environ)
        env.update({
            "TAPO_BASE_DIR": str(self.base),
            "TAPO_TOGGLE_CMD": str(self.fake_toggle),
            "TAPO_TOGGLE_TIMEOUT": str(self.toggle_timeout),
            "TAPO_PROBE_CMD": str(self.fake_probe),
            "TAPO_PROBE_TIMEOUT": str(self.probe_timeout),
            "TAPO_NOTIFY_CMD": str(self.fake_notify),
        })
        if now is not None:
            env["TAPO_NOW"] = str(int(now))
        if time is not None:
            env["TAPO_TIME"] = time
        elif hour is not None:
            env["TAPO_TIME"] = f"{hour:02d}:00"
        if extra:
            env.update(extra)
        return env

    def run(self, now=None, hour=None, timeout=30, args=(), time=None, extra=None):
        return subprocess.run(["bash", str(self.script), *args], env=self.env(now, hour, time, extra),
                              capture_output=True, text=True, timeout=timeout)

    # -- fakes

    def phones_home(self, *ips):
        (self.base / "phones_home").write_text("\n".join(ips) + "\n")

    def phones_away(self):
        p = self.base / "phones_home"
        if p.exists():
            p.unlink()

    def toggle_rc(self, rc):
        (self.base / "toggle_rc").write_text(str(rc))

    def toggle_sleep(self, secs):
        (self.base / "toggle_sleep").write_text(str(secs))

    def probe_rc(self, rc):
        (self.base / "probe_rc").write_text(str(rc))

    def probe_sleep(self, secs):
        (self.base / "probe_sleep").write_text(str(secs))

    def toggle_calls(self):
        if not self.calls_file.exists():
            return []
        return self.calls_file.read_text().splitlines()

    def notify_calls(self):
        """Arguments of every push so far. The helper runs in the background, so poll."""
        p = self.notify_calls_file
        return p.read_text().splitlines() if p.exists() else []

    def wait_for_notify_calls(self, n, timeout=5.0):
        wait_until(lambda: len(self.notify_calls()) >= n, timeout=timeout,
                   message=f"{n} push(es), have {self.notify_calls()}")
        return self.notify_calls()

    # -- state files

    def read(self, name, default=None):
        p = self.base / name
        if not p.exists():
            return default
        text = p.read_text().strip()
        return text if text else default

    def write(self, name, value):
        (self.base / name).write_text(f"{value}\n")

    def exists(self, name):
        return (self.base / name).exists()

    def log(self):
        p = self.base / "tapo.log"
        return p.read_text() if p.exists() else ""

    def hold_lock(self, name):
        fh = open(self.base / name, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh


@pytest.fixture
def phones(tmp_path):
    return ShellHarness(tmp_path, "check_phones_and_toggle.sh")


@pytest.fixture
def night_mode(tmp_path):
    return ShellHarness(tmp_path, "night_mode.sh")


@pytest.fixture
def loop(tmp_path):
    return ShellHarness(tmp_path, "check_phones_loop.sh")
