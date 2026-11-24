"""
login-summary: the screen shown at SSH login, run in plain mode against the
real host plus fake state directories, so the tests never need root and do not
depend on what the weekly job or the cameras are doing right now.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "login-summary"

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def flat(text):
    """Line breaks and indents collapsed, for phrases a narrow terminal may wrap."""
    return " ".join(text.split())


def run(columns=80, colour=False, **env):
    e = dict(os.environ, COLUMNS=str(columns), **env)
    args = ["bash", str(SCRIPT), "--color" if colour else "--plain"]
    t0 = time.time()
    r = subprocess.run(args, env=e, capture_output=True, text=True, timeout=30)
    r.seconds = time.time() - t0
    return r


def fake_state(tmp_path, *, weekly="finished, 0 failure(s)", disk_warning=False, apt=0, presence="disable",
               cam_state="idle", failures=0, pending=False, backup_age_days=2, security="nothing to do"):
    log = tmp_path / "logs"; log.mkdir()
    (log / "maintenance-status.log").write_text(f"2026-09-06 01:48 finished, 0 failure(s)\n2026-09-13 01:52 {weekly}\n")
    (log / "security-upgrades.log").write_text(f"2026-09-12 01:26 nothing to do\n2026-09-14 01:26 {security}\n")
    (log / "disk-health.log").write_text("2026-09-13 01:48 health=PASSED reallocated=0 pending=0 uncorrectable=0 power_on_hours=9000 temp=33C last_selftest=[none]\n")
    if disk_warning:
        (log / "DISK_WARNING").touch()
        (log / "disk-health.log").write_text("2026-09-13 01:48 health=FAILED! reallocated=12 pending=3 uncorrectable=0\n")
    run_dir = tmp_path / "run"; run_dir.mkdir()
    if apt:
        (run_dir / ".apt_updates").write_text(f"{apt}\n")
    tapo = tmp_path / "tapo"; (tapo / "recordings" / "cam_1").mkdir(parents=True)
    (tapo / ".tapo_motion_state").write_text(f"{presence}\n")
    (tapo / ".tapo_ping_fail_count").write_text("0\n" if presence == "disable" else "7\n")
    (tapo / ".tapo_away_threshold").write_text("18\n")
    if pending:
        (tapo / ".tapo_pending").touch()
    (tapo / "night_mode.sh").write_text("#!/bin/bash\necho 'night mode: ON (22:00-06:00)'\n")
    (tapo / "night_mode.sh").chmod(0o755)
    (tapo / "config.json").write_text(json.dumps({"recording": {"retain_days": 14}}))
    (tapo / "recordings" / "status.json").write_text(json.dumps({
        "cameras": {"cam_1": {"state": cam_state, "failures": failures, "pid": 1,
                                 "running_for_seconds": 754 if cam_state == "recording" else None,
                                 "retry_in_seconds": None, "last_error": None}},
        "recording_wanted": presence == "enable", "updated": "x"}, indent=2))
    (tapo / "recordings" / "cam_1" / "2026-09-13_10-20-00.mp4").write_bytes(b"\0" * 100)
    (tapo / "tapo.log").write_text("2026-09-13 10:19:58 [phones] state change: 'disable' -> 'enable' (fail_count=18)\n")
    backup = tmp_path / "backup"; backup.mkdir()
    for d in ("data", "data_2", "data_3"):
        (backup / d).mkdir()
    when = time.strftime("%Y-%m-%d_%H:%M:%S", time.gmtime(time.time() - backup_age_days * 86400))
    (backup / ".dietpi-backup_stats").write_text(f"{when}  Backup starting\n{when}  Backup completed\n")
    return dict(TAPO_DIR=str(tapo), LOG_DIR=str(log), RUN_DIR=str(run_dir), BACKUP_DIR=str(backup),
                DESKTOP_COPY=str(tmp_path / "no-desktop-copy"))


def test_the_camera_page_address_says_https_when_it_is(tmp_path):
    env = fake_state(tmp_path)
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    (fake_bin / "systemctl").write_text('#!/bin/bash\n[ "$*" = "is-active status_page" ] && exit 0\n'
                                        'exec /usr/bin/systemctl "$@"\n')
    (fake_bin / "systemctl").chmod(0o755)
    env["PATH"] = f"{fake_bin}:{os.environ['PATH']}"
    config = Path(env["TAPO_DIR"]) / "config.json"
    config.write_text(json.dumps({"status_page": {"port": 8090}}))
    assert re.search(r"\bhttp://\S+:8090", run(**env).stdout)
    config.write_text(json.dumps({"status_page": {"port": 8090, "tls_cert": "/x/laptop.crt",
                                                  "tls_key": "/x/laptop.key"}}))
    assert re.search(r"\bhttps://\S+:8090", run(**env).stdout)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_runs_against_the_real_host_quickly_and_covers_every_section():
    r = run()
    assert r.returncode == 0, r.stderr
    assert r.stderr == "", "no command inside may complain"
    for label in ("System", "Disk", "Network", "Services", "Updates", "Weekly", "Backups"):
        assert re.search(rf"^  {label}\s", r.stdout, re.M), f"missing row {label}\n{r.stdout}"
    assert "load " in r.stdout and "RAM " in r.stdout and " free" in r.stdout
    assert r.seconds < 3, f"took {r.seconds:.1f}s"


@pytest.mark.parametrize("columns", [60, 80, 100])
def test_every_line_fits_the_terminal_width(tmp_path, columns):
    # Both runs use the fake logs. The colour run used to read the real ones, so
    # the width depended on what the daily job last installed, and the test went
    # red overnight when "installed: jq libjq1" made a line 61 columns wide.
    env = fake_state(tmp_path, weekly="finished, 2 failure(s)", disk_warning=True, apt=7,
                     presence="enable", cam_state="recording", failures=2, pending=True,
                     security="installed: libssl3t64 openssl openssl-provider-legacy libcrypt1 libgnutls30t64")
    r = run(columns, **env)
    assert r.returncode == 0, r.stderr
    for line in r.stdout.splitlines():
        assert len(line) <= columns, f"{len(line)} > {columns}: {line!r}"
    plain = ANSI.sub("", run(columns, colour=True, **env).stdout)
    for line in plain.splitlines():
        assert len(line) <= columns, f"colour codes must not count: {line!r}"


def test_an_item_wider_than_the_line_breaks_at_its_spaces(tmp_path):
    env = fake_state(tmp_path, security="installed: jq libjq1")
    narrow, wide = run(60, **env).stdout, run(110, **env).stdout
    item = "✓ security fixes Mon 14 Sep: installed: jq libjq1"
    assert item not in narrow and item in flat(narrow), "split at a space, nothing cut off"
    assert item in wide, "kept whole when it fits"


def test_plain_mode_has_no_escape_codes_and_colour_mode_does(tmp_path):
    env = fake_state(tmp_path)
    assert not ANSI.search(run(**env).stdout)
    assert ANSI.search(run(colour=True, **env).stdout)


def test_all_good_state_is_quiet(tmp_path):
    # the kernel line reads the host's packages: here they hold only the running kernel, as
    # after a reboot (the real host may well be waiting for one)
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    (fake_bin / "dpkg-query").write_text("#!/bin/bash\n[ \"${@: -1}\" = 'linux-image-[0-9]*' ] "
                                         "|| exec /usr/bin/dpkg-query \"$@\"\necho \"linux-image-$(uname -r)\"\n")
    (fake_bin / "dpkg-query").chmod(0o755)
    r = run(PATH=f"{fake_bin}:{os.environ['PATH']}", **fake_state(tmp_path))
    out = r.stdout
    assert "kernel is the newest installed" in out
    assert "HOME privacy mode, not recording" in out
    assert "night mode: ON (22:00-06:00)" in out
    assert "recorder: cam_1 idle" in out
    assert "recordings: " in out and "in 1 segments" in out and "newest 2026-09-13 10:20 (cam_1)" in out
    assert "kept 14 days" in out
    assert "✓ SMART PASSED, checked 2026-09-13" in out
    assert "✓ Sun 13 Sep 01:52: finished, no failures" in out
    assert "APT up to date" in out
    assert "✓ security fixes Mon 14 Sep: nothing to do" in out
    assert "dietpi-backup" in out and "(2 d ago)" in out and "3 rotating copies" in out
    # the rows driven by the fake state carry no marks (continuation lines belong to
    # the row above them); the real host's rows are left alone
    faked, include = [], False
    for line in out.splitlines():
        m = re.match(r"  (\S+)\s", line)
        if m:
            include = m.group(1) in ("Disk", "Cameras", "Updates", "Weekly", "Backups")
        elif not line.startswith("            "):
            include = False
        if include:
            faked.append(line)
    faked = "\n".join(faked)
    assert "!" not in faked.replace("FAILED!", "") and "✗" not in faked, faked


def test_problems_are_marked(tmp_path):
    r = run(colour=True, **fake_state(tmp_path, weekly="aborted during: dist-upgrade", disk_warning=True, apt=5,
                                      presence="enable", cam_state="backoff", failures=4, pending=True, backup_age_days=20,
                                      security="WARNING: unattended-upgrade failed (rc=1), wanted: openssl"))
    out = r.stdout
    plain = ANSI.sub("", out)
    assert "✗ DISK WARNING, plan a replacement" in plain
    assert "health=FAILED! reallocated=12" in plain and "pending=3 uncorrectable=0" in plain
    assert "✗ Sun 13 Sep 01:52: aborted during: dist-upgrade" in plain
    assert "! 5 APT package(s) waiting" in plain
    assert "✗ security fixes Mon 14 Sep: unattended-upgrade failed (rc=1), wanted: openssl" in flat(plain)
    assert "AWAY cameras armed, recording" in plain
    assert "! no phone for 7 checks (arms at 18, ~3 min)" in plain
    assert "! camera switch incomplete, retrying" in plain
    assert "✗ cam_1 backoff  ! 4 failures" in plain
    assert "\x1b[91m" in out, "red is used for the bad news"
    # a backup older than 15 days is red
    red_lines = [l for l in out.splitlines() if "dietpi-backup" in l]
    assert red_lines and "\x1b[91m" in red_lines[0]


def test_recording_state_and_next_weekly_run(tmp_path):
    out = run(**fake_state(tmp_path, presence="enable", cam_state="recording")).stdout
    assert "✓ cam_1 recording 12 min" in out
    m = re.search(r"next (\w{3}) \d\d \w{3} 03:30 \(in .*\)", out)
    assert m and m.group(1) == "Sun"


def test_held_notifications_are_shown_only_when_there_are_some(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    env = dict(fake_state(tmp_path), SPOOL_DIR=str(spool))
    assert "Notices" not in run(**env).stdout, "silent when the queue is empty"

    created = int(time.time()) - 3 * 3600
    (spool / f"{created}000000000-1.msg").write_text(
        f"priority=high\ntags=x\ntitle=Disk trouble\nclick=\ntopic=alerts\ncreated={created}\n\nbody")
    (spool / f"{created + 60}000000000-1.msg").write_text(
        f"priority=low\ntags=x\ntitle=Later\nclick=\ntopic=alerts\ncreated={created + 60}\n\nbody")
    out = run(**env).stdout
    assert "! 2 notification(s) waiting for the phone to come home" in out
    assert "oldest 3 h ago" in out


def test_a_skipped_security_run_is_shown_briefly(tmp_path):
    out = run(**fake_state(tmp_path, security="skipped: Sunday, the weekly job upgrades everything")).stdout
    assert "security fixes Mon 14 Sep: skipped (Sunday)" in out
    assert "upgrades everything" not in out, "the long reason stays in the log"
    other = tmp_path / "other"; other.mkdir()
    out = run(**fake_state(other, security="skipped: the weekly maintenance is running")).stdout
    assert "skipped (the weekly maintenance is running)" in flat(out)


def test_missing_sources_are_reported_not_fatal(tmp_path):
    r = run(TAPO_DIR=str(tmp_path / "none"), LOG_DIR=str(tmp_path / "none"),
            BACKUP_DIR=str(tmp_path / "none"), RUN_DIR=str(tmp_path / "none"))
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    assert "SMART: no weekly check logged yet" in r.stdout
    assert "no weekly run logged yet" in r.stdout
    assert "no dietpi-backup found under" in r.stdout
    assert "security fixes: no daily run logged yet" in r.stdout
    assert "Cameras" not in r.stdout, "no tapo-automation directory, no camera rows"


def test_the_other_machines_last_reports_are_on_the_backups_row(tmp_path):
    env = fake_state(tmp_path)
    logs = Path(env["LOG_DIR"])
    fin = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    for machine, status in (("backup-server", "ok"), ("desktop", "failed")):
        (logs / machine).mkdir()
        (logs / machine / "report-2026-09-27_02-00-54.txt").write_text(f"status={status}\nfinished={fin}\n")
    out = flat(run(110, **env).stdout)
    when = time.strftime("%a %d %b %H:%M", time.localtime(time.time() - 3600))
    assert f"backup server {when} ok" in out
    assert f"✗ desktop {when} (1 h ago): problems, see machine-reports --status" in out


def test_the_desktop_is_on_demand_so_its_last_backup_shows_and_only_a_month_is_late(tmp_path):
    env = fake_state(tmp_path)
    copy = tmp_path / "desktop-copy"; copy.mkdir()
    env["DESKTOP_COPY"] = str(copy)
    for days, late in ((20, False), (40, True)):
        t = time.time() - days * 86400
        os.utime(copy, (t, t))
        out = run(110, colour=True, **env).stdout
        line = [l for l in out.splitlines() if "desktop backup" in l][0]
        assert f"desktop backup {time.strftime('%a %d %b %H:%M', time.localtime(t))} ({days} d ago)" in ANSI.sub("", line)
        assert ("\x1b[93m" in line) == late, "yellow only after a month"


def test_containers_come_from_service_watch_when_docker_refuses(tmp_path):
    """dietpi is not in the docker group; service-watch leaves the list behind."""
    listing = tmp_path / "containers.tsv"
    listing.write_text("metube\tUp 3 hours (healthy)\t8081/tcp\n"
                       "metube-login\tUp 3 hours\t0.0.0.0:8081->8081/tcp\n"
                       "ntfy\tExited (1) 2 minutes ago\t\n")
    r = run(DOCKER="false", CONTAINERS_FILE=str(listing))
    assert r.returncode == 0, r.stderr
    out = flat(r.stdout)
    if "Docker" not in out:
        pytest.skip("docker is not running on this host")
    assert "metube-login Up 3 hours :8081" in out and "ntfy Exited (1) 2 minutes ago" in out
