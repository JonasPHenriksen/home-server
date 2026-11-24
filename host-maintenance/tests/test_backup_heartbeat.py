"""
check-backup-heartbeat: the laptop's alarm for a backup server that stopped
running, against a temporary heartbeat file and a fake ntfy-notify.
"""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "check-backup-heartbeat"


def run(tmp_path, status="ok", age_days=1):
    hb = tmp_path / "hb"
    hb.write_text(f"last_cycle=2026-09-20T02:08:05Z\nstatus={status}\nfailures={0 if status == 'ok' else 2}\n"
                  "free_gb=309\nhost=DietPi-Backup\n")
    t = time.time() - age_days * 86400
    os.utime(hb, (t, t))
    calls = tmp_path / "calls"
    notify = tmp_path / "notify"
    notify.write_text(f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> {calls}\n")
    notify.chmod(0o755)
    env = dict(os.environ, HEARTBEAT=str(hb), STATE=str(tmp_path / "state"), LOG=str(tmp_path / "log"),
               NOTIFY=str(notify))
    subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    return (calls.read_text() if calls.exists() else ""), (tmp_path / "log").read_text()


def test_a_backup_server_that_stopped_running_is_pushed(tmp_path):
    calls, log = run(tmp_path, age_days=10)
    assert calls.startswith("-p high -t backup,rotating_light Backup server has not run for 10 days")
    assert "STALE" in log


def test_a_failed_cycle_is_only_logged_since_machine_reports_pushes_it_with_the_reasons(tmp_path):
    calls, log = run(tmp_path, status="failed")
    assert calls == ""
    assert "cycle reported failures: status=failed failures=2 (machine-reports pushes the details)" in log


def test_a_good_recent_cycle_is_quiet(tmp_path):
    calls, log = run(tmp_path)
    assert calls == "" and "ok: last cycle 2026-09-20T02:08:05Z" in log
