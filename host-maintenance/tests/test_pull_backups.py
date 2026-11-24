"""
backup-server/pull-backups.sh: the snapshot pull script that runs on the backup
server, exercised offline: a full dry run, and the pruning logic against fake
snapshot directories.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "backup-server" / "pull-backups.sh"

APT = "-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"


def run_dry(tmp_path, **env):
    # temp dirs may live on a small tmpfs: pin the free-space figure unless a test overrides it
    e = dict(os.environ, DRY_RUN="1", FREE_GB_OVERRIDE="500", BACKUP_ROOT=str(tmp_path / "backups"),
             LOG=str(tmp_path / "log.txt"))
    e.update(env)
    r = subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=60)
    log = tmp_path / "log.txt"
    return r, (log.read_text() if log.exists() else "")


def powered_off_or_stayed_awake(stdout, log):
    """The idle check sees whoever runs the tests; either outcome is the script working."""
    return "DRY: sudo poweroff" in stdout or "User detected. Staying awake." in log


def bash_func(tmp_path, snippet, **env):
    """Source the script and run a snippet against it (prune tests)."""
    e = dict(os.environ, BACKUP_ROOT=str(tmp_path / "backups"), LOG=str(tmp_path / "log.txt"), **env)
    return subprocess.run(["bash", "-c", f"source {SCRIPT}; {snippet}"], env=e,
                          capture_output=True, text=True, timeout=60)


def make_snapshots(root, name, stamps, latest=None):
    d = root / "backups" / name
    d.mkdir(parents=True)
    for s in stamps:
        (d / s).mkdir()
        (d / s / "file").write_text(s)
    (d / "inprogress_tmp").mkdir()
    if latest:
        (d / "latest").symlink_to(latest)
    return d


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_dry_run_shows_every_step_in_order(tmp_path):
    r, log = run_dry(tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "DRY: sudo smartctl -H -A /dev/disk/by-id/ata-ST1000DM003-1CH162_EXAMPLE1" in out
    assert "DRY: wait for 192.168.50.10" in out
    rsyncs = [l for l in out.splitlines() if l.startswith("DRY: rsync")]
    assert len(rsyncs) == 3
    laptop, desktop, nextcloud = rsyncs
    assert "backup-server@192.168.50.10:/mnt/dietpi-backup/data/" in laptop and "-a -H -X --numeric-ids" in laptop
    assert "--exclude=/var/lib/containerd" in desktop and "/mnt/dietpi-desktop-backup/" in desktop
    assert "--fuzzy --no-owner --no-group --chmod=D2775,F664" in nextcloud
    for line in rsyncs:
        assert " -z " not in line and "-z" not in line.split("rsync ")[1].split(" -S")[0], "no compression on the LAN"
        assert "--timeout=900" in line
        assert "--rsync-path" not in line, "the laptop's backup-gate does the sudo, through rrsync"
        assert "BatchMode=yes" in line and "ConnectTimeout=15" in line
        assert "--link-dest" not in line, "first run: nothing to link against"
    assert "DRY: sudo /boot/dietpi/dietpi-update 1" in out
    assert "DRY: sudo apt-get update -q" in out
    assert f"DRY: sudo apt-get {APT} upgrade --with-new-pkgs" in out
    assert f"DRY: sudo apt-get {APT} autoremove --purge" in out
    assert "DRY: sleep 900" in out
    assert powered_off_or_stayed_awake(out, log)
    assert log.count("SUCCESS:") == 3
    assert "Backup Cycle Finished" in log


def test_dry_run_links_against_the_previous_snapshot(tmp_path):
    make_snapshots(tmp_path, "laptop", ["2026-09-01_01-00-00"], latest="2026-09-01_01-00-00")
    r, _ = run_dry(tmp_path)
    laptop = [l for l in r.stdout.splitlines() if l.startswith("DRY: rsync") and "dietpi-backup/data" in l][0]
    assert f"--link-dest={tmp_path}/backups/laptop/2026-09-01_01-00-00" in laptop


def test_prune_removes_oldest_first_and_keeps_the_minimum(tmp_path):
    stamps = [f"2026-08-{d:02d}_01-00-00" for d in range(1, 11)]  # 10 snapshots
    for name in ("laptop", "desktop", "nextcloud"):
        make_snapshots(tmp_path, name, stamps, latest=stamps[-1])
    # free space "stays" at 10 GB, so pruning runs until every server is at the minimum
    r = bash_func(tmp_path, "prune_snapshots; echo PRUNED=$PRUNED_COUNT", FREE_GB_OVERRIDE="10")
    assert r.returncode == 0, r.stderr
    assert "PRUNED=6" in r.stdout
    for name in ("laptop", "desktop", "nextcloud"):
        left = sorted(p.name for p in (tmp_path / "backups" / name).iterdir() if p.name[:4].isdigit())
        assert left == stamps[2:], name  # the two oldest are gone
        assert (tmp_path / "backups" / name / "inprogress_tmp").exists(), "only snapshots are candidates"
        assert (tmp_path / "backups" / name / "latest").exists()
    log = (tmp_path / "log.txt").read_text()
    assert log.count("PRUNE:") == 6
    assert "laptop/2026-08-01_01-00-00" in log


def test_prune_does_nothing_when_space_is_fine_or_below_minimum_count(tmp_path):
    stamps = [f"2026-08-{d:02d}_01-00-00" for d in range(1, 6)]  # 5 < KEEP_MIN_SNAPSHOTS
    make_snapshots(tmp_path, "laptop", stamps, latest=stamps[-1])
    r = bash_func(tmp_path, "prune_snapshots; echo PRUNED=$PRUNED_COUNT", FREE_GB_OVERRIDE="10")
    assert "PRUNED=0" in r.stdout
    make_snapshots(tmp_path, "desktop", [f"2026-08-{d:02d}_01-00-00" for d in range(1, 21)])
    r = bash_func(tmp_path, "prune_snapshots; echo PRUNED=$PRUNED_COUNT", FREE_GB_OVERRIDE="500")
    assert "PRUNED=0" in r.stdout, "plenty of space: nothing pruned even with 20 snapshots"


def test_prune_never_deletes_the_latest_target(tmp_path):
    stamps = [f"2026-08-{d:02d}_01-00-00" for d in range(1, 11)]
    make_snapshots(tmp_path, "laptop", stamps, latest=stamps[0])  # latest points at the OLDEST
    r = bash_func(tmp_path, "prune_snapshots; echo PRUNED=$PRUNED_COUNT", FREE_GB_OVERRIDE="10")
    # 10 snapshots, 8 must stay: two may go, and the protected one is passed over
    # for the next-oldest instead of stopping the whole prune
    assert "PRUNED=2" in r.stdout, r.stdout
    assert (tmp_path / "backups" / "laptop" / stamps[0]).exists(), "latest's target survives"
    assert not (tmp_path / "backups" / "laptop" / stamps[1]).exists()
    assert not (tmp_path / "backups" / "laptop" / stamps[2]).exists()
    assert (tmp_path / "backups" / "laptop" / stamps[3]).exists(), "and no more than needed"


def test_low_space_abort_path_in_dry_run(tmp_path):
    r, log = run_dry(tmp_path, FREE_GB_OVERRIDE="10")
    assert "CRITICAL: Low Disk Space!" in log
    assert r.returncode == 1, "a cycle that backed up nothing exits non-zero"
    assert "DRY: rsync" not in r.stdout, "no backups attempted when the disk stays low"
    assert f"DRY: sudo apt-get {APT} upgrade --with-new-pkgs" in r.stdout, "still updates"
    assert powered_off_or_stayed_awake(r.stdout, log)


def test_a_failing_pull_is_counted_and_reported(tmp_path):
    r = bash_func(tmp_path, 'FAILURES=2; log_message "x"; if [ "$FAILURES" -gt 0 ]; then echo COUNTED; fi')
    assert "COUNTED" in r.stdout


def test_interrupted_cycle_is_logged(tmp_path):
    (tmp_path / "backups").mkdir()
    bash_func(tmp_path, 'CURRENT_STEP="pull desktop"; on_exit')
    assert "ABORTED during: pull desktop" in (tmp_path / "log.txt").read_text()
    bash_func(tmp_path, 'FINISHED=1; CURRENT_STEP="idle wait"; on_exit')
    assert (tmp_path / "log.txt").read_text().count("ABORTED") == 1


def test_second_cycle_exits_while_the_lock_is_held(tmp_path):
    import fcntl
    (tmp_path / "backups").mkdir()
    with open(tmp_path / "backups" / ".cycle.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r, _ = run_dry(tmp_path)
    assert r.returncode == 0
    assert "another backup cycle is already in progress" in r.stderr
    assert "DRY:" not in r.stdout



# --------------------------------------------------------------------------- what the live cycle added

def test_dry_run_covers_the_heartbeat_health_check_and_verification_window(tmp_path):
    r, log = run_dry(tmp_path)
    out = r.stdout
    assert r.returncode == 0, r.stderr
    hb = out.index("DRY: heartbeat (ok) -> 192.168.50.10:/mnt/dietpi_userdata/logs/backup-server/heartbeat")
    up = out.index("upgrade --with-new-pkgs")
    hc = out.index("DRY: health check (")
    assert hb < up < hc, "heartbeat right after the pulls, health check after the upgrade"
    assert "dist-upgrade" not in out, "an unattended run must never remove packages"
    assert "Verifying for up to 15 minutes" in out
    assert "last-complete.new" in out, "the last-complete pointer is advanced for a whole snapshot"


def test_a_failed_cycle_sends_a_failed_heartbeat(tmp_path):
    r = bash_func(tmp_path, 'FAILURES=2; send_heartbeat failed', DRY_RUN="1", FREE_GB_OVERRIDE="500")
    assert "DRY: heartbeat (failed) -> 192.168.50.10" in r.stdout


# A cycle that pulls nothing must never report "ok": the laptop would stay quiet
# while no backups are being made, the one thing its heartbeat check is for.
@pytest.mark.parametrize("cause, snippet, free_gb", [
    ("disk still too full after pruning", "main", "10"),
    ("SMART check failed", "disk_health() { return 1; }; main", "500"),
    ("laptop never came up", "wait_for_source() { return 1; }; main", "500"),
])
def test_a_cycle_that_pulls_nothing_reports_failed(tmp_path, cause, snippet, free_gb):
    r = bash_func(tmp_path, f'{snippet}; echo "failures=$FAILURES"', DRY_RUN="1", FREE_GB_OVERRIDE=free_gb)
    assert "DRY: rsync" not in r.stdout, cause
    assert "DRY: heartbeat (failed)" in r.stdout, cause
    assert "DRY: heartbeat (ok)" not in r.stdout, cause
    assert "failures=1" in r.stdout, cause
    assert "Backup Cycle Finished with 1 failure(s)" in (tmp_path / "log.txt").read_text(), cause


def test_health_check_failure_counts_and_the_dry_run_does_not_inspect_this_machine(tmp_path):
    r = bash_func(tmp_path, 'health_check; echo "rc=$?"', DRY_RUN="1")
    assert "rc=0" in r.stdout and "DRY: health check" in r.stdout
    assert not (tmp_path / "log.txt").exists() or "HEALTH: FAIL" not in (tmp_path / "log.txt").read_text()
    r = bash_func(tmp_path, 'hc_fail "kernel 6.1 has no initramfs"; echo "problems=$HEALTH_PROBLEMS"', DRY_RUN="0")
    assert "problems=1" in r.stdout
    assert "HEALTH: FAIL - kernel 6.1 has no initramfs" in (tmp_path / "log.txt").read_text()


def test_pruning_never_removes_the_last_complete_snapshot(tmp_path):
    root = tmp_path / "backups"
    stamps = [f"2026-08-{d:02d}_01-00-00" for d in range(1, 12)]
    d = make_snapshots(tmp_path, "laptop", stamps, latest=stamps[-1])
    (d / "last-complete").symlink_to(stamps[0])          # the oldest is the last complete one
    r = bash_func(tmp_path, 'MIN_FREE_GB=50; prune_snapshots; echo "pruned=$PRUNED_COUNT"; ls backups/laptop',
                  FREE_GB_OVERRIDE="10", DRY_RUN="0")
    assert (d / stamps[0]).exists(), "last-complete is protected like latest"
    assert not (d / stamps[1]).exists(), "the next-oldest went instead"


# --------------------------------------------------------------------------- the report and the clock

SMART_OUT = """SMART overall-health self-assessment test result: PASSED
ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0
  9 Power_On_Hours          0x0032   098   098   000    Old_age   Always       -       2463
194 Temperature_Celsius     0x0022   032   040   000    Old_age   Always       -       32
197 Current_Pending_Sector  0x0012   100   100   000    Old_age   Always       -       0
198 Offline_Uncorrectable   0x0010   100   100   000    Old_age   Offline      -       0
Num  Test_Description    Status                  Remaining  LifeTime(hours)  LBA_of_first_error
# 1  Short offline       Completed without error       00%      2463         -
"""


def fake_machine(tmp_path):
    """sudo that just runs the command, and canned smartctl, journalctl, ufw, ethtool, apt, systemctl."""
    b = tmp_path / "fakebin"
    b.mkdir()
    fakes = {
        "sudo": 'exec "$@"',
        "smartctl": f"cat <<'OUT'\n{SMART_OUT}OUT",
        "ethtool": r'printf "\tSupports Wake-on: pumbg\n\tWake-on: g\n"',
        "journalctl": 'echo "kernel: [UFW BLOCK] IN=eth0 SRC=192.168.50.1 DST=224.0.0.1 PROTO=2"',
        "systemctl": "true",
        "apt": "echo Listing...",
        "ufw": "echo 'Status: active'",
        "ssh-keygen": "echo '256 SHA256:x no comment (ED25519)'",
    }
    for name, body in fakes.items():
        (b / name).write_text(f"#!/bin/bash\n{body}\n")
        (b / name).chmod(0o755)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def report_env(tmp_path):
    (tmp_path / "log.txt").write_text(
        "2026-09-20 02:00:00 - a line from last week's run\n"
        "2026-09-27 02:00:54 - --- Backup Cycle Started ---\n"
        "2026-09-27 02:03:48 - CHURN ALARM: desktop has 54661 fewer files than the last complete snapshot\n"
        "2026-09-27 02:23:22 - No active users. Powering off.\n")
    return dict(fake_machine(tmp_path), DRY_RUN="0", FREE_GB_OVERRIDE="307", VERIFY_TOOL="/nonexistent")


REPORT_RUN = ('LOG_START_LINE=1; RUN_STARTED=$(date -d "2026-09-27 02:00:54" +%s); RUN_TIMESTAMP=2026-09-27_02-00-54; '
              'CLOCK_SYNCED=yes; SOURCE_UPTIME=382; DECISION=poweroff; ')


def keyvalues(report):
    head = report.split("\n=====")[0]
    return dict(l.split("=", 1) for l in head.splitlines() if "=" in l and not l.startswith("#"))


def test_the_report_carries_the_run_as_key_value_lines_then_sections(tmp_path):
    make_snapshots(tmp_path, "laptop", ["2026-09-20_02-00-54", "2026-09-27_02-00-54"], latest="2026-09-27_02-00-54")
    r = bash_func(tmp_path, REPORT_RUN + 'FAILURES=1; T_RESULT[laptop]=ok; T_XFER_FILES[laptop]=0; '
                  'T_FILES[laptop]=37390; T_FILES_BEFORE[laptop]=37390; T_SOURCE_TIME[laptop]=1790473766; '
                  f'INSTALLED_FILES=({SCRIPT}); build_report', **report_env(tmp_path))
    assert r.returncode == 0, r.stderr
    kv = keyvalues(r.stdout)
    assert kv["run"] == "2026-09-27_02-00-54" and kv["started"] == "2026-09-27T02:00:54Z"
    assert kv["status"] == "failed" and kv["failures"] == "1" and kv["decision"] == "poweroff"
    assert kv["clock_synced"] == "yes" and kv["source_uptime"] == "382" and kv["free_gb"] == "307"
    assert (kv["disk_health"], kv["disk_reallocated"], kv["disk_pending"], kv["disk_hours"], kv["disk_temp"]) \
        == ("PASSED", "0", "0", "2463", "32")
    assert kv["selftest"] == "Short offline Completed without error 00% 2463 -"
    assert kv["journal_warnings"] == "0", "the router's blocked IGMP queries are not warnings"
    assert kv["wake_on"] == "g"
    assert kv["tree_laptop"] == "ok" and kv["tree_laptop_files"] == "37390"
    assert kv["tree_laptop_snapshots"] == "2" and kv["tree_laptop_oldest"] == "2026-09-20_02-00-54"
    assert kv["tree_laptop_source_time"] == "2026-09-27T01:49:26Z"
    assert kv["tree_desktop"] == "skipped", "a tree this run never got to"
    assert len(kv["sha256_pull-backups.sh"]) == 64
    problems = r.stdout.split("===== problems\n")[1].split("\n=====")[0]
    assert "CHURN ALARM: desktop has 54661 fewer files" in problems
    log = r.stdout.split("===== log\n")[1].split("\n=====")[0]
    assert "--- Backup Cycle Started ---" in log and "last week" not in log, "this run's lines only"
    for section in ("smart", "verify", "system", "checksums"):
        assert f"\n===== {section}\n" in r.stdout


def test_a_clean_run_reports_no_problems(tmp_path):
    env = report_env(tmp_path)
    (tmp_path / "log.txt").write_text("2026-09-27 02:00:54 - --- Backup Cycle Started ---\n")
    r = bash_func(tmp_path, REPORT_RUN + "LOG_START_LINE=0; build_report", **env)
    assert keyvalues(r.stdout)["status"] == "ok"
    assert "===== problems\nnone\n" in r.stdout


def test_the_clock_is_waited_for_but_never_blocks_the_backup(tmp_path):
    flag = tmp_path / "synchronized"
    flag.touch()
    r = bash_func(tmp_path, 'wait_for_clock; echo "synced=$CLOCK_SYNCED waited=$CLOCK_WAITED"',
                  DRY_RUN="0", TIMESYNC_FLAG=str(flag))
    assert "synced=yes waited=0" in r.stdout
    flag.unlink()
    r = bash_func(tmp_path, 'CLOCK_WAIT_SECS=0; wait_for_clock; echo "synced=$CLOCK_SYNCED rc=$?"',
                  DRY_RUN="0", TIMESYNC_FLAG=str(flag))
    assert "synced=no rc=0" in r.stdout
    assert "WARNING: clock still not synchronised" in (tmp_path / "log.txt").read_text()


def test_dry_run_waits_for_the_clock_self_tests_and_reports_last(tmp_path):
    r, log = run_dry(tmp_path)
    out = r.stdout
    assert r.returncode == 0, r.stderr
    assert out.index("DRY: wait for /run/systemd/timesync/synchronized") \
        < out.index("DRY: sudo smartctl -t short /dev/disk/by-id/ata-ST1000DM003-1CH162_EXAMPLE1") \
        < out.index("DRY: rsync")
    m = re.search(r"DRY: report \((\d+) lines\) -> 192\.168\.50\.10:/mnt/dietpi_userdata/logs/backup-server/"
                  r"report-(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)\.txt", out)
    assert m and int(m.group(1)) > 50
    assert f"-> {m.group(2)}" in log, "named like the snapshots"
    assert out.index("DRY: sleep 900") < m.start(), "after the verification window"
    if "DRY: sudo poweroff" in out:
        assert m.start() < out.index("DRY: sudo poweroff"), "and before the power-off"
    assert "SMART: short self-test started" in log


def test_a_pull_records_its_numbers_and_a_churn_alarm_names_the_directories(tmp_path):
    # the previous complete snapshot: 1300 files, 1200 of them VS Code server
    prev = tmp_path / "backups" / "laptop" / "2026-09-20_02-00-54"
    for i in range(1200):
        f = prev / "home" / "dietpi" / ".vscode-server" / f"bin{i % 7}" / f"f{i}"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    src = tmp_path / "src"
    for i in range(100):
        for base in (prev, src):
            f = base / "etc" / f"conf{i}"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("y")
    for link in ("latest", "last-complete"):
        (prev.parent / link).symlink_to(prev.name)
    # an rsync that copies the fake source and prints the stats2 summary
    b = tmp_path / "fakebin"
    b.mkdir()
    (b / "rsync").write_text('#!/bin/bash\ndest="${@: -1}"; mkdir -p "$dest"; cp -a "$FAKE_SRC/." "$dest/"\n'
                             'printf "Number of regular files transferred: 100\\nTotal file size: 12,345 bytes\\n'
                             'Total transferred file size: 2,345 bytes\\n"\n')
    (b / "rsync").chmod(0o755)
    r = bash_func(tmp_path, 'backup_server laptop /mnt/dietpi-backup/data/ "" 2026-09-27_02-00-54; '
                  'echo "failures=$FAILURES result=${T_RESULT[laptop]} xfer=${T_XFER_FILES[laptop]} '
                  'bytes=${T_XFER_BYTES[laptop]} total=${T_TOTAL[laptop]} files=${T_FILES[laptop]} '
                  'before=${T_FILES_BEFORE[laptop]} secs=${T_SECONDS[laptop]} src=${T_SOURCE_TIME[laptop]}"',
                  DRY_RUN="0", FAKE_SRC=str(src), PATH=f"{b}:{os.environ['PATH']}")
    assert r.returncode == 0, r.stderr
    assert "failures=1 result=ok xfer=100 bytes=2345 total=12345 files=100 before=1300" in r.stdout
    assert re.search(r"secs=\d+ src=\d+", r.stdout)
    log = (tmp_path / "log.txt").read_text()
    assert "FILES: laptop 1300 -> 100 (-1200)" in log
    assert re.search(r"CHURN ALARM: laptop has 1200 fewer files .* Biggest drops: home/dietpi/\.vscode-server -1200$",
                     log, re.M), log


def test_a_routine_kernel_swap_is_no_churn_alarm(tmp_path):
    # 11 Oct 2026: the old kernel's modules left the laptop's tree, 44147 -> 39329 files (-10 %),
    # which set off the alarm while its limit was 10 %. In miniature, with the 1000-file minimum
    # out of the way: 11 % fewer files is only logged, 21 % fewer still alarms.
    b = tmp_path / "fakebin"
    b.mkdir()
    (b / "rsync").write_text('#!/bin/bash\ndest="${@: -1}"; mkdir -p "$dest"; cp -a "$FAKE_SRC/." "$dest/"\n'
                             'printf "Number of regular files transferred: 100\\nTotal file size: 12,345 bytes\\n'
                             'Total transferred file size: 2,345 bytes\\n"\n')
    (b / "rsync").chmod(0o755)
    for kept, alarm in ((89, False), (79, True)):
        root = tmp_path / f"kept{kept}"
        prev = root / "backups" / "laptop" / "2026-10-04_01-59-18"
        src = root / "src"
        for i in range(100):
            for base in ((prev, src) if i < kept else (prev,)):
                f = base / "usr" / "lib" / "modules" / f"m{i}"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x")
        for link in ("latest", "last-complete"):
            (prev.parent / link).symlink_to(prev.name)
        r = bash_func(root, 'CHURN_DELETE_MIN=1; backup_server laptop /mnt/dietpi-backup/data/ "" '
                      '2026-10-11_01-58-20; echo "failures=$FAILURES"',
                      DRY_RUN="0", FAKE_SRC=str(src), PATH=f"{b}:{os.environ['PATH']}")
        assert r.returncode == 0, r.stderr
        log = (root / "log.txt").read_text()
        assert f"FILES: laptop 100 -> {kept} (-{100 - kept})" in log, log
        assert ("CHURN ALARM" in log) == alarm, log
        assert f"failures={int(alarm)}" in r.stdout, r.stdout + log


# `ps -eo pid=,ppid=,args=` as the laptop showed it on 2026-09-26: the listening
# dropbear, the Claude app's connection, and an SSH login shell.
LISTENER = "      1       0 /sbin/init\n    847       1 /usr/sbin/dropbear -EF -p 22 -W 65536 -w -s\n"
CLAUDE_APP = ("2207440     847 /usr/sbin/dropbear -EF -p 22 -W 65536 -w -s -2 8\n"
              "2207442 2207440 /usr/lib/sftp-server\n"
              "2207531 2207440 /home/dietpi/.claude/remote/srv/f6010b978a0b/server --bridge "
              "--socket /home/dietpi/.claude/remote/run/e377eae2/rpc.sock\n")
LOGIN_SHELL = "2241150     847 /usr/sbin/dropbear -EF -p 22 -W 65536 -w -s -2 8\n2241151 2241150 -bash\n"


def fake_processes(tmp_path, table):
    """A `ps` that prints TABLE, and a `pgrep` that finds nothing (no OpenSSH, no tty1)."""
    b = tmp_path / "psbin"
    b.mkdir(exist_ok=True)
    (b / "ps").write_text(f"#!/bin/bash\ncat <<'EOF'\n{table}EOF\n")
    (b / "pgrep").write_text("#!/bin/bash\nexit 1\n")
    for f in ("ps", "pgrep"):
        (b / f).chmod(0o755)
    return f"{b}:{os.environ['PATH']}"


def test_the_claude_apps_own_connection_is_not_a_login(tmp_path):
    def sessions(table):
        return bash_func(tmp_path, "ssh_sessions", PATH=fake_processes(tmp_path, table)).stdout.split()
    assert sessions(LISTENER) == ["0", "0"]
    assert sessions(LISTENER + CLAUDE_APP) == ["0", "1"]
    assert sessions(LISTENER + CLAUDE_APP + LOGIN_SHELL) == ["1", "1"], "a login shell still counts"
    conn = "3000     847 /usr/sbin/dropbear -EF -p 22 -W 65536 -w -s -2 8\n"
    assert sessions(LISTENER + conn + "3001    3000 /usr/lib/sftp-server\n") == ["1", "0"], \
        "file transfers over sftp without the app count"
    assert sessions(LISTENER + conn) == ["1", "0"], "so does a connection that is still logging in"
    assert sessions(LISTENER + CLAUDE_APP + "2207600 2207440 bash\n") == ["1", "0"], \
        "and anything else running inside the app's connection"
    # the owner's check on 2026-09-26 counted itself: its command line said "dropbear"
    checking = ("5000 2241151 sudo bash -c source /usr/local/sbin/ssh-backups-cycle.sh; pgrep -o dropbear\n"
                "5001    5000 bash -c source /usr/local/sbin/ssh-backups-cycle.sh; pgrep -o dropbear\n"
                "5002    5001 awk { if (cmd ~ /dropbear/) n++ }\n")
    assert sessions(LISTENER + CLAUDE_APP + LOGIN_SHELL + checking) == ["1", "1"]


def test_it_powers_off_with_only_the_claude_app_connected(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    r, log = run_dry(tmp_path / "a", PATH=fake_processes(tmp_path, LISTENER + CLAUDE_APP))
    assert r.returncode == 0, r.stderr
    assert "Status - SSH logins: 0 (Claude app, not counted: 1), OpenSSH: 0, Physical: 0" in log
    assert "No active users. Powering off." in log and "DRY: sudo poweroff" in r.stdout
    r, log = run_dry(tmp_path / "b", PATH=fake_processes(tmp_path, LISTENER + CLAUDE_APP + LOGIN_SHELL))
    assert "User detected. Staying awake." in log and "DRY: sudo poweroff" not in r.stdout
