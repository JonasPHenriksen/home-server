"""
machine-reports: reads the backup server's and the desktop's reports and sends
the weekly push, against a temporary logs directory and a fake ntfy-notify that
records every call. The repository itself is the reference for the checksums.
"""
import calendar
import hashlib
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "machine-reports"
NOW = calendar.timegm((2026, 9, 27, 2, 26, 40, 0, 0, 0))   # a Sunday morning, half an hour after the weekly job

NOTIFY = '''#!/bin/bash
printf '%s\\x1f' "$@" >> "$CALLS"; printf '\\x1e' >> "$CALLS"
[ ! -e "$CALLS.fail" ]
'''


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def sha(rel):
    return hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()


def bs_report(problems="none", **kv):
    fields = {
        "host": "DietPi-Backup", "run": "2026-09-27_02-00-54", "finished": iso(NOW - 120),
        "status": "ok", "failures": "0", "decision": "poweroff", "free_gb": "307",
        "disk_health": "PASSED", "disk_reallocated": "0", "disk_pending": "0", "disk_uncorrectable": "0",
        "selftest": "Short offline Completed without error 00% 2463 -",
        "verify_source": "ok", "verify_scrub": "ok", "failed_units": "0",
        "sha256_ssh-backups-cycle.sh": sha("backup-server/pull-backups.sh"),
        "sha256_verify-backups.sh": sha("backup-server/verify-backups.sh"),
        "sha256_ssh-backups.service": sha("backup-server/ssh-backups.service"),
        "sha256_ssh-backups.timer": sha("backup-server/ssh-backups.timer"),
        "sha256_wol.service": sha("backup-server/wol.service"),
    }
    for t, moved in (("laptop", 0), ("desktop", 1_930_766_069), ("nextcloud", 15_099_440)):
        fields.update({f"tree_{t}": "ok", f"tree_{t}_bytes_transferred": str(moved),
                       f"tree_{t}_source_time": iso(NOW - 3 * 3600)})
    fields.update(kv)
    head = "\n".join(f"{k}={v}" for k, v in fields.items())
    return f"# backup server report, format 1\n{head}\n\n===== problems\n{problems}\n\n===== log\n...\n"


def desktop_report(problems="none", finished=NOW - 3 * 3600, **kv):
    fields = {"host": "DietPi-Desktop", "run": "2026-09-26_23-00-05", "finished": iso(finished),
              "status": "ok", "failures": "0",
              "sha256_dietpi-backup-sync.sh": sha("desktop/dietpi-backup-sync.sh"),
              "sha256_dietpi-boot-backup.timer": sha("desktop/dietpi-boot-backup.timer"),
              "sha256_dietpi-boot-backup.service": sha("desktop/dietpi-boot-backup.service")}
    fields.update(kv)
    head = "\n".join(f"{k}={v}" for k, v in fields.items())
    return f"# desktop report, format 1\n{head}\n\n===== problems\n{problems}\n\n===== log\n...\n"


class Box:
    def __init__(self, tmp_path):
        self.logs = tmp_path / "logs"
        (self.logs / "backup-server").mkdir(parents=True)
        (self.logs / "desktop").mkdir()
        self.calls = tmp_path / "calls"
        notify = tmp_path / "notify"
        notify.write_text(NOTIFY)
        notify.chmod(0o755)
        # the desktop's system tree on the laptop: its mtime is the desktop's last backup
        self.copy = tmp_path / "desktop-copy"
        self.copy.mkdir()
        self.desktop_copy(4 * 3600)
        self.env = dict(os.environ, LOG_DIR=str(self.logs), STATE_DIR=str(tmp_path / "state"),
                        NOTIFY=str(notify), REPO=str(ROOT), NOW=str(NOW), CALLS=str(self.calls),
                        DESKTOP_COPY=str(self.copy))

    def desktop_copy(self, age):
        os.utime(self.copy, (NOW - age, NOW - age))

    def report(self, machine, name, text, mtime=NOW - 60):
        f = self.logs / machine / f"report-{name}.txt"
        f.write_text(text)
        os.utime(f, (mtime, mtime))
        return f

    def pending(self, age=1900, failures=0, warnings="", containers="metube"):
        f = self.logs / ".weekly-summary"
        f.write_text(f"created={NOW - age}\nfailures={failures}\ncontainers={containers}\n"
                     "disk=health=PASSED reallocated=0 pending=0 uncorrectable=0 power_on_hours=10515 temp=35C\n"
                     f"warnings={warnings}\n")
        os.utime(f, (NOW - age, NOW - age))

    def run(self, *args):
        r = subprocess.run(["bash", str(SCRIPT), *args], env=self.env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stderr == "", r.stderr
        return r

    def pushes(self):
        if not self.calls.exists():
            return []
        return [c.split("\x1f")[:-1] for c in self.calls.read_text().split("\x1e") if c]


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_the_weekly_push_waits_for_the_backup_server_and_covers_all_three_machines(tmp_path):
    box = Box(tmp_path)
    box.pending()
    box.report("desktop", "2026-09-26_23-00-05", desktop_report(), mtime=NOW - 3 * 3600)
    box.run()
    assert box.pushes() == [], "the backup server has not reported yet"
    assert (box.logs / ".weekly-summary").exists()
    box.report("backup-server", "2026-09-27_02-00-54", bs_report())
    box.run()
    (push,) = box.pushes()
    assert push[:5] == ["-p", "low", "-t", "white_check_mark", "Weekly maintenance OK"]
    lines = push[5].splitlines()
    assert lines[0] == "Laptop: backup, updates and Pi-hole done, containers updated: metube, disk PASSED."
    assert lines[1] == "Backup server: 3 backups OK, 1.9 GB new, verify OK, disk OK, 307 GB free."
    assert lines[2] == "Desktop: last on Sat 26 Sep 23:26 UTC (3 h ago): backed up and updated, checks OK."
    assert not (box.logs / ".weekly-summary").exists()
    box.run()
    assert len(box.pushes()) == 1, "once"


def test_problems_make_the_weekly_push_loud_and_say_why(tmp_path):
    box = Box(tmp_path)
    box.pending()
    box.report("backup-server", "2026-09-27_02-00-54", bs_report(
        status="failed", failures="1",
        problems="2026-09-27 02:03:48 - CHURN ALARM: desktop has 54661 fewer files than the last complete "
                 "snapshot (158266 -> 103605, -34%). Biggest drops: usr/lib/modules -21255"))
    box.run()
    (push,) = box.pushes()
    assert push[:5] == ["-p", "high", "-t", "warning", "Weekly maintenance: 1 problem(s)"]
    assert "Problems:\n- backup server: CHURN ALARM: desktop has 54661 fewer files" in push[5]
    assert "Biggest drops: usr/lib/modules -21255" in push[5]
    assert "02:03:48" not in push[5], "the log timestamp is dropped"


def test_without_a_report_the_weekly_push_goes_out_at_the_deadline(tmp_path):
    box = Box(tmp_path)
    box.pending(age=1900)
    box.run()
    assert box.pushes() == []
    box.pending(age=8200)
    hb = box.logs / "backup-server" / "heartbeat"
    hb.parent.mkdir(exist_ok=True)
    hb.write_text("last_cycle=2026-09-27T02:08:05Z\nstatus=failed\nfailures=2\nfree_gb=309\nhost=DietPi-Backup\n")
    os.utime(hb, (NOW - 7000, NOW - 7000))
    box.run()
    (push,) = box.pushes()
    assert push[4] == "Weekly maintenance: 1 problem(s)"
    assert "Backup server: no report." in push[5]
    assert "its early status said failed with 2 failure(s), 309 GB free" in push[5]


def test_the_laptops_failed_steps_are_repeated_in_the_weekly_push(tmp_path):
    box = Box(tmp_path)
    box.pending(failures=2, warnings="WARNING: dist-upgrade failed (rc=100), continuing | WARNING: pihole -g failed")
    box.report("backup-server", "2026-09-27_02-00-54", bs_report())
    box.run()
    (push,) = box.pushes()
    assert push[4] == "Weekly maintenance: 1 problem(s)"
    assert "Laptop: 2 failed step(s), pushed at 01:55: WARNING: dist-upgrade failed (rc=100)" in push[5]


def test_a_report_at_any_other_time_pushes_only_problems(tmp_path):
    box = Box(tmp_path)
    box.report("backup-server", "2026-09-26_05-13-58", bs_report())
    box.run()
    assert box.pushes() == [], "a good manual run is not news"
    box.report("desktop", "2026-09-28_19-00-00", desktop_report(
        status="failed", failures="1", problems="[2026-09-28 19:04:10] FAILED: smoke test GPU (nvidia module not loaded)"))
    box.run()
    (push,) = box.pushes()
    assert push[:5] == ["-p", "high", "-t", "warning", "Desktop: 1 problem(s)"]
    assert "FAILED: smoke test GPU" in push[5] and "[2026-09-28" not in push[5]
    box.run()
    assert len(box.pushes()) == 1, "each report once"


def test_checks_beyond_the_log(tmp_path):
    box = Box(tmp_path)
    box.report("backup-server", "2026-09-27_02-00-54", bs_report(**{
        "disk_reallocated": "3", "free_gb": "42",
        "selftest": "Short offline Completed: read failure 90% 2470 1234567",
        "tree_laptop_source_time": iso(NOW - 10 * 86400),
        "tree_desktop_source_time": iso(NOW - 20 * 86400),
        "failed_units": "1",
        "sha256_ssh-backups-cycle.sh": "0" * 64}))
    out = box.run("--status").stdout
    for expected in ("disk: 3 reallocated sector(s)", "disk self-test: Short offline Completed: read failure",
                     "only 42 GB free on the backup disk", "the laptop copy it pulled is 10 days old",
                     "1 failed unit(s) on the backup server",
                     "runs a different ssh-backups-cycle.sh than the repository"):
        assert expected in out, f"{expected}\n{out}"
    assert "desktop copy" not in out, "the desktop is judged by itself, with a month's leeway"
    assert box.pushes() == [], "--status never pushes"


def test_a_self_test_still_running_is_not_a_problem(tmp_path):
    box = Box(tmp_path)
    box.report("backup-server", "2026-09-27_02-00-54",
               bs_report(selftest="Short offline Self-test routine in progress 10% 2463 -"))
    assert "problem" not in box.run("--status").stdout


def at(t):
    return time.strftime("%a %d %b %H:%M UTC", time.gmtime(t))


def test_a_desktop_left_off_for_weeks_is_normal_and_the_push_says_when_it_was_last_on(tmp_path):
    box = Box(tmp_path)
    box.pending()
    last = NOW - 10 * 86400
    box.report("desktop", "2026-09-17_02-00", desktop_report(finished=last), mtime=last)
    box.desktop_copy(10 * 86400 + 300)
    box.report("backup-server", "2026-09-27_02-00-54", bs_report())
    box.run()
    (push,) = box.pushes()
    assert push[4] == "Weekly maintenance OK", "the desktop is switched on when needed"
    assert f"Desktop: last on {at(last)} (10 days ago): backed up and updated, checks OK." in push[5]


def test_a_month_without_the_desktops_cycle_turns_the_weekly_push_into_a_warning(tmp_path):
    box = Box(tmp_path)
    box.pending()
    last = NOW - 32 * 86400
    box.report("desktop", "2026-08-26_02-00", desktop_report(finished=last), mtime=last)
    box.desktop_copy(32 * 86400 + 300)
    box.report("backup-server", "2026-09-27_02-00-54", bs_report())
    box.run()
    (push,) = box.pushes()
    assert push[:5] == ["-p", "high", "-t", "warning", "Weekly maintenance: 1 problem(s)"]
    assert f"Desktop: last on {at(last)} (32 days ago)" in push[5]
    assert "- desktop: not switched on for its cycle in 32 days; switch it on so it backs up and updates" in push[5]


def test_without_a_report_the_desktops_backup_copy_says_when(tmp_path):
    box = Box(tmp_path)
    box.desktop_copy(5 * 86400)
    out = box.run("--status").stdout
    assert f"Desktop: last backup {at(NOW - 5 * 86400)} (5 days ago)." in out and "problem" not in out
    # a report older than the newest copy (its report never arrived): the copy is the latest word
    box.report("desktop", "2026-09-10_12-00", desktop_report(finished=NOW - 16 * 86400), mtime=NOW - 16 * 86400)
    assert "(5 days ago)" in box.run("--status").stdout
    box.desktop_copy(40 * 86400)
    box.report("desktop", "2026-09-10_12-00", desktop_report(finished=NOW - 45 * 86400), mtime=NOW - 45 * 86400)
    out = box.run("--status").stdout
    assert "problem: not switched on for its cycle in 40 days" in out


def test_a_backup_server_left_on_is_pushed_at_once_with_the_reason(tmp_path):
    box = Box(tmp_path)
    box.pending(age=300)
    with open(box.logs / ".weekly-summary", "a") as f:
        f.write("backup_server_was_on=yes\n")
    os.utime(box.logs / ".weekly-summary", (NOW - 300, NOW - 300))
    box.run()
    (push,) = box.pushes()
    assert push[4] == "Weekly maintenance: 1 problem(s)", "no waiting until the deadline: no report is coming"
    assert "Backup server: was already on, so no run this week." in push[5]
    assert "- backup server was already on when the laptop went to wake it, so its weekly run didn't start" in push[5]


def test_a_backup_server_that_stays_on_or_was_on_long_before_its_run_is_flagged(tmp_path):
    box = Box(tmp_path)
    box.report("backup-server", "2026-09-27_02-00-54", bs_report(
        decision="stay-awake-user", clock_synced="yes",
        booted=iso(NOW - 6 * 3600), started=iso(NOW - 26 * 60)))
    out = box.run("--status").stdout
    assert "stayed on after its run because someone was logged in (the Claude app counts)" in out
    assert "had been on for 5 h 34 min before this run" in out
    box2 = Box(tmp_path / "normal")
    box2.report("backup-server", "2026-09-27_02-00-54", bs_report(
        decision="poweroff", clock_synced="yes", booted=iso(NOW - 31 * 60), started=iso(NOW - 26 * 60)))
    assert "problem" not in box2.run("--status").stdout, "booted five minutes before the run, as the wake-up does"


def test_a_push_that_fails_is_retried(tmp_path):
    box = Box(tmp_path)
    box.pending()
    box.report("backup-server", "2026-09-27_02-00-54", bs_report())
    Path(str(box.calls) + ".fail").touch()
    box.run()
    assert len(box.pushes()) == 1 and (box.logs / ".weekly-summary").exists(), "attempted, still pending"
    Path(str(box.calls) + ".fail").unlink()
    box.run()
    assert len(box.pushes()) == 2 and not (box.logs / ".weekly-summary").exists()


def report_keys(text):
    return [l.split("=", 1)[0] for l in text.split("\n=====")[0].splitlines() if "=" in l and not l.startswith("#")]


def test_no_report_value_is_ever_run_as_code(tmp_path):
    """The reports come from other machines and this runs as root: bash evaluates
    text inside $(( )), so a value like PATH[$(cmd)] must never get there. Every
    field of both reports gets the payload in turn, through the weekly push."""
    marker = tmp_path / "ran"
    # PATH is always set: under set -u an unset name stops bash before the subscript runs
    payload = f"PATH[$(touch {marker})]"
    extra = {"clock_synced": "yes", "started": iso(NOW - 900), "booted": iso(NOW - 1200)}
    bs, desk = bs_report(**extra), desktop_report()
    cases = [("bs", k) for k in report_keys(bs)] + [("desktop", k) for k in report_keys(desk)]
    assert len(cases) > 30

    def poisoned(text, key):
        return "\n".join(f"{key}={payload}" if l.startswith(key + "=") else l for l in text.split("\n"))

    for i, (machine, key) in enumerate(cases):
        box = Box(tmp_path / f"case{i}")
        box.pending()
        if machine == "bs":
            box.report("backup-server", "2026-09-27_02-00-54", poisoned(bs, key))
        else:
            box.report("desktop", "2026-09-26_23-00-05", poisoned(desk, key), mtime=NOW - 3 * 3600)
            box.report("backup-server", "2026-09-27_02-00-54", bs)
        subprocess.run(["bash", str(SCRIPT)], env=box.env, capture_output=True, text=True, timeout=60)
        assert not marker.exists(), f"{machine} report field {key} was run as code"
    assert box.pushes(), "the weekly push still went out"


def test_a_garbage_byte_count_counts_as_zero(tmp_path):
    box = Box(tmp_path)
    box.pending()
    box.report("backup-server", "2026-09-27_02-00-54", bs_report(tree_desktop_bytes_transferred="lots"))
    box.run()
    (push,) = box.pushes()
    assert "Backup server: 3 backups OK, 15 MB new," in push[5]


def test_old_reports_are_pruned(tmp_path):
    box = Box(tmp_path)
    for i in range(55):
        box.report("backup-server", f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}_02-00-00", bs_report())
    box.run()
    kept = sorted(p.name for p in (box.logs / "backup-server").iterdir())
    assert len(kept) == 52 and kept[0] == "report-2026-01-04_02-00-00.txt"
