"""
system/system-and-pihole-updater: the weekly host maintenance script, exercised
in dry-run mode so nothing on the machine changes.
"""
import os
import subprocess

import pytest

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = ROOT / "system-and-pihole-updater"

# apt flags that keep an unattended run from ever waiting for an answer
APT = "-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"


def dry_run(tmp_path, **env):
    # machine-reports may or may not be installed on the machine running the
    # tests; unless a test says otherwise, behave as if it is not
    e = dict(os.environ, DRY_RUN="1", LOG_DIR=str(tmp_path / "logs"), REPORTS=str(tmp_path / "absent"))
    e.update(env)
    return subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=60)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_dry_run_touches_nothing_and_covers_every_step(tmp_path):
    r = dry_run(tmp_path, WAKE_AFTER_REBOOT="0")  # the immediate wake-up path; the deferred one has its own test
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for expected in ("DRY: /boot/dietpi/dietpi-backup 1",
                     "DRY: /usr/bin/wakeonlan 02:00:00:00:00:30",
                     "--- Disk health (/dev/sda)",
                     "smartctl -H -A -l selftest /dev/sda",
                     "smartctl -t short /dev/sda",
                     "DRY: /boot/dietpi/dietpi-update 1",
                     "DRY: /usr/bin/apt-get update -q",
                     f"DRY: /usr/bin/apt-get {APT} dist-upgrade",
                     "Removing old kernels",
                     f"DRY: /usr/bin/apt-get {APT} autoremove --purge",
                     "DRY: /usr/local/bin/pihole -g",
                     "DRY: /sbin/reboot"):
        assert expected in out, f"missing step: {expected}\n{out}"
    assert "STATUS: finished, 0 failure(s)" in out
    written = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert written == [".maintenance.lock"], f"dry run must not write logs, wrote {written}"


def test_kernel_purge_never_takes_the_newest_installed_kernel(tmp_path):
    """dpkg also lists kernels it merely knows, like not-installed -unsigned builds. One
    of those sorting last made the newest installed kernel look old (1 October 2026)."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "dpkg-query").write_text("""#!/bin/bash
[ "${@: -1}" = 'linux-image-[0-9]*' ] || exec /usr/bin/dpkg-query "$@"
cat <<'LIST'
linux-image-6.12.99+deb13-amd64 installed
linux-image-6.12.107+deb13-amd64 installed
linux-image-6.12.107+deb13-amd64-unsigned not-installed
linux-image-6.12.111+deb13-amd64 installed
linux-image-6.12.111+deb13-amd64-unsigned not-installed
LIST
""")
    (fake / "uname").write_text('#!/bin/bash\n[ "$1" = -r ] && { echo 6.12.107+deb13-amd64; exit 0; }\nexec /usr/bin/uname "$@"\n')
    for f in fake.iterdir():
        f.chmod(0o755)
    r = dry_run(tmp_path, PATH=f"{fake}:{os.environ['PATH']}")
    purging = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith("purging:")]
    assert purging == ["purging: linux-image-6.12.99+deb13-amd64"], r.stdout


def test_kernel_purge_keeps_the_running_and_the_newest_kernel(tmp_path):
    r = dry_run(tmp_path)
    running = "linux-image-" + os.uname().release
    rows = subprocess.run(["dpkg-query", "-W", "-f=${Package} ${db:Status-Status}\\n", "linux-image-[0-9]*"],
                          capture_output=True, text=True).stdout.splitlines()
    installed = [r.split()[0] for r in rows if r.split()[-1] == "installed"]
    purge_lines = [l for l in r.stdout.splitlines() if l.strip().startswith("purging:")]
    if len(installed) <= 2:
        assert "nothing to purge" in r.stdout
        return
    assert purge_lines, r.stdout
    purged = purge_lines[0].split(":", 1)[1].split()
    newest = sorted(installed, key=lambda p: [int(x) if x.isdigit() else x for x in p.replace("+", ".").replace("-", ".").split(".")])[-1]
    assert running not in purged, "must never purge the running kernel"
    assert newest not in purged, "must keep the newest installed kernel"
    assert set(purged) <= set(installed)
    assert f"DRY: /usr/bin/apt-get {APT} purge {' '.join(purged)}" in r.stdout


def test_wake_up_is_deferred_to_after_the_reboot_when_the_unit_exists(tmp_path):
    r = dry_run(tmp_path, WAKE_AFTER_REBOOT="1")
    assert "will be woken after the reboot" in r.stdout
    assert f"DRY: touch {tmp_path / 'logs'}/.wake-backup-server" in r.stdout
    assert "DRY: /usr/bin/wakeonlan" not in r.stdout
    r = dry_run(tmp_path, WAKE_AFTER_REBOOT="0")
    assert "DRY: /usr/bin/wakeonlan 02:00:00:00:00:30" in r.stdout
    assert "touch" not in r.stdout


def test_boot_time_wake_script(tmp_path):
    """wake-backup-server: does nothing without the flag, sends WoL and clears it with the flag."""
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    (fake_bin / "wakeonlan").write_text("#!/bin/bash\necho \"$@\" > %s/wol_called\n" % tmp_path)
    (fake_bin / "wakeonlan").chmod(0o755)
    (fake_bin / "ping").write_text("#!/bin/bash\n[ -e %s/server_on ]\n" % tmp_path)   # answers only when "on"
    (fake_bin / "ping").chmod(0o755)
    script = (ROOT / "wake-backup-server").read_text().replace("/usr/bin/wakeonlan", str(fake_bin / "wakeonlan"))
    patched = tmp_path / "wake-backup-server"; patched.write_text(script); patched.chmod(0o755)
    env = dict(os.environ, FLAG=str(tmp_path / "flag"), LOG=str(tmp_path / "log"), SETTLE_SECS="0", MAC="aa:bb:cc:dd:ee:ff",
               SUMMARY=str(tmp_path / "summary"), PATH=f"{fake_bin}:{os.environ['PATH']}")
    assert subprocess.run(["bash", str(patched)], env=env).returncode == 0
    assert not (tmp_path / "wol_called").exists(), "no flag, no wake-up"
    (tmp_path / "flag").touch()
    (tmp_path / "summary").write_text("created=1\nfailures=0\n")
    assert subprocess.run(["bash", str(patched)], env=env).returncode == 0
    assert (tmp_path / "wol_called").read_text().strip() == "aa:bb:cc:dd:ee:ff"
    assert not (tmp_path / "flag").exists(), "flag cleared after a successful wake-up"
    log = (tmp_path / "log").read_text()
    assert "woke the backup server" in log and "already on" not in log
    assert "backup_server_was_on" not in (tmp_path / "summary").read_text()


def test_a_backup_server_left_on_is_noted_for_the_weekly_push(tmp_path):
    """It only runs at boot, so if it already answers, this week's run won't start."""
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    (fake_bin / "wakeonlan").write_text("#!/bin/bash\ntrue\n"); (fake_bin / "wakeonlan").chmod(0o755)
    (fake_bin / "ping").write_text("#!/bin/bash\ntrue\n"); (fake_bin / "ping").chmod(0o755)
    script = (ROOT / "wake-backup-server").read_text().replace("/usr/bin/wakeonlan", str(fake_bin / "wakeonlan"))
    patched = tmp_path / "wake-backup-server"; patched.write_text(script); patched.chmod(0o755)
    (tmp_path / "flag").touch()
    (tmp_path / "summary").write_text("created=1\nfailures=0\n")
    env = dict(os.environ, FLAG=str(tmp_path / "flag"), LOG=str(tmp_path / "log"), SETTLE_SECS="0",
               SUMMARY=str(tmp_path / "summary"), PATH=f"{fake_bin}:{os.environ['PATH']}")
    assert subprocess.run(["bash", str(patched)], env=env).returncode == 0
    assert "WARNING: the backup server (192.168.50.30) was already on" in (tmp_path / "log").read_text()
    assert (tmp_path / "summary").read_text().endswith("backup_server_was_on=yes\n")


def test_disk_can_be_overridden(tmp_path):
    r = dry_run(tmp_path, DISK="/dev/sdz")
    assert "--- Disk health (/dev/sdz)" in r.stdout
    assert "smartctl -t short /dev/sdz" in r.stdout


# --------------------------------------------------------------------------- unattended-run safety

def source_and_run(tmp_path, snippet, **env):
    """Source the script (which defines the helpers without running main) and poke at it."""
    e = dict(os.environ, LOG_DIR=str(tmp_path / "logs"), REPORTS=str(tmp_path / "absent"))
    e.update(env)
    (tmp_path / "logs").mkdir(exist_ok=True)
    return subprocess.run(["bash", "-c", f"source {SCRIPT}; {snippet}"], env=e,
                          capture_output=True, text=True, timeout=60)


def test_apt_is_fully_non_interactive(tmp_path):
    r = dry_run(tmp_path, WAKE_AFTER_REBOOT="0")
    for line in [l for l in r.stdout.splitlines() if "apt-get" in l and "clean" not in l and "update" not in l]:
        assert "-y" in line, line
        assert "--force-confold" in line and "--force-confdef" in line, line
    env = source_and_run(tmp_path, "echo FRONTEND=$DEBIAN_FRONTEND")
    assert "FRONTEND=noninteractive" in env.stdout


def test_a_failing_step_warns_and_continues(tmp_path):
    r = source_and_run(tmp_path, 'run_step "pretend" false; echo "rc=$?"; run_step "ok" true; echo "failures=$FAILURES"')
    assert "WARNING: pretend failed (rc=1), continuing" in r.stdout
    assert "rc=1" in r.stdout
    assert "failures=1" in r.stdout, "a successful step must not add to the count"


def test_apt_update_retries_then_gives_up(tmp_path):
    # replace `run` after sourcing so every apt-get update attempt fails instantly
    r = source_and_run(tmp_path, 'APT_UPDATE_WAIT=0; tries=0; run() { tries=$((tries+1)); return 1; }; '
                                 'apt_update; echo "rc=$? tries=$tries failures=$FAILURES"',
                       DRY_RUN="0")
    assert "tries=3 failures=1" in r.stdout, r.stdout
    assert "rc=1" in r.stdout
    assert "retrying in 0s" in r.stdout


def test_apt_update_succeeds_on_the_second_attempt(tmp_path):
    r = source_and_run(tmp_path, 'APT_UPDATE_WAIT=0; tries=0; run() { tries=$((tries+1)); [ $tries -ge 2 ]; }; '
                                 'apt_update; echo "rc=$? tries=$tries failures=$FAILURES"',
                       DRY_RUN="0")
    assert "rc=0 tries=2 failures=0" in r.stdout, r.stdout


def test_an_interrupted_run_leaves_a_status_line(tmp_path):
    r = source_and_run(tmp_path, 'CURRENT_STEP="dist-upgrade"; on_exit', DRY_RUN="1")
    assert "STATUS: aborted during: dist-upgrade" in r.stdout
    r = source_and_run(tmp_path, 'FINISHED=1; CURRENT_STEP="reboot"; on_exit', DRY_RUN="1")
    assert "aborted" not in r.stdout, "a finished run writes no abort line"


def test_no_reboot_flag(tmp_path):
    r = dry_run(tmp_path, WAKE_AFTER_REBOOT="0", NO_REBOOT="1")
    assert "NO_REBOOT set, not rebooting." in r.stdout
    assert "DRY: /sbin/reboot" not in r.stdout


def test_second_run_exits_while_the_lock_is_held(tmp_path):
    import fcntl
    (tmp_path / "logs").mkdir(exist_ok=True)
    with open(tmp_path / "logs" / ".maintenance.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = dry_run(tmp_path, WAKE_AFTER_REBOOT="0")
    assert r.returncode == 0
    assert "another maintenance run is already in progress" in r.stderr
    assert "DRY:" not in r.stdout, "nothing ran"


# --------------------------------------------------------------------------- notifications

def fake_notify(tmp_path):
    """A stand-in for ntfy-notify that records its arguments, one call per line."""
    script = tmp_path / "fake-notify"
    calls = tmp_path / "notify_calls"
    script.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "$*" >> "{calls}"\n')
    script.chmod(0o755)
    return script, calls


def test_dry_run_ends_with_an_ok_notification_before_the_reboot(tmp_path):
    out = dry_run(tmp_path, WAKE_AFTER_REBOOT="0").stdout
    ok = out.index("DRY: /usr/local/bin/ntfy-notify -p low -t white_check_mark Weekly maintenance OK")
    assert out.index("STATUS: finished, 0 failure(s)") < ok < out.index("DRY: /sbin/reboot")


def test_failures_are_pushed_with_high_priority_and_the_warning_lines(tmp_path):
    script, calls = fake_notify(tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "weekly-updater.log").write_text(
        "Starting DietPi backup...\n"
        "WARNING: dist-upgrade failed (rc=100), continuing\n"
        "some apt output\n"
        "WARNING: pihole -g failed (rc=1), continuing\n")
    r = source_and_run(tmp_path, 'FAILURES=2; notify_summary', DRY_RUN="0", NOTIFY=str(script))
    assert r.returncode == 0, r.stderr
    call = calls.read_text()
    assert call.startswith("-p high -t warning Weekly maintenance: 2 failed step(s) WARNING: dist-upgrade failed")
    assert "WARNING: pihole -g failed (rc=1)" in call
    assert "some apt output" not in call


def test_ok_notification_carries_the_disk_line(tmp_path):
    script, calls = fake_notify(tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "disk-health.log").write_text(
        "2026-08-30 01:48 health=PASSED reallocated=0 pending=0 uncorrectable=0\n"
        "2026-09-06 01:48 health=PASSED reallocated=0 pending=0 uncorrectable=0 power_on_hours=9000\n")
    source_and_run(tmp_path, 'notify_summary', DRY_RUN="0", NOTIFY=str(script))
    call = calls.read_text()
    assert call.startswith("-p low -t white_check_mark Weekly maintenance OK Backup, updates and Pi-hole done")
    assert "Disk: health=PASSED reallocated=0 pending=0 uncorrectable=0 power_on_hours=9000" in call
    assert "2026-09-06" not in call, "the timestamp is dropped"



def fake_reports(tmp_path):
    reports = tmp_path / "machine-reports"
    reports.write_text("#!/bin/bash\n")
    reports.chmod(0o755)
    return str(reports)


def test_the_ok_push_is_left_to_machine_reports_when_the_backup_server_was_woken(tmp_path):
    script, calls = fake_notify(tmp_path)
    (tmp_path / "logs" / "disk-health.log").parent.mkdir(exist_ok=True)
    (tmp_path / "logs" / "disk-health.log").write_text("2026-09-27 01:49 health=PASSED reallocated=0 pending=0\n")
    r = source_and_run(tmp_path, 'BACKUP_SERVER_WOKEN=1; UPDATED_CONTAINERS=metube; notify_summary',
                       DRY_RUN="0", NOTIFY=str(script), REPORTS=fake_reports(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not calls.exists(), "no push from here: machine-reports sends it with the backup server's result"
    summary = (tmp_path / "logs" / ".weekly-summary").read_text()
    assert "failures=0" in summary and "containers=metube" in summary
    assert "disk=health=PASSED reallocated=0 pending=0" in summary
    assert "created=" in summary


def test_failed_steps_are_pushed_at_once_and_left_for_the_weekly_push_too(tmp_path):
    script, calls = fake_notify(tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "weekly-updater.log").write_text(
        "WARNING: dist-upgrade failed (rc=100), continuing\nWARNING: pihole -g failed (rc=1), continuing\n")
    source_and_run(tmp_path, 'BACKUP_SERVER_WOKEN=1; FAILURES=2; notify_summary',
                   DRY_RUN="0", NOTIFY=str(script), REPORTS=fake_reports(tmp_path))
    assert calls.read_text().startswith("-p high -t warning Weekly maintenance: 2 failed step(s)")
    summary = (tmp_path / "logs" / ".weekly-summary").read_text()
    assert "failures=2" in summary
    assert "warnings=WARNING: dist-upgrade failed (rc=100), continuing | WARNING: pihole -g failed" in summary


def test_without_machine_reports_or_a_woken_backup_server_the_ok_push_goes_out_as_before(tmp_path):
    script, calls = fake_notify(tmp_path)
    source_and_run(tmp_path, 'BACKUP_SERVER_WOKEN=1; notify_summary', DRY_RUN="0", NOTIFY=str(script))
    assert "Weekly maintenance OK" in calls.read_text(), "machine-reports is not installed"
    calls.unlink()
    source_and_run(tmp_path, 'notify_summary', DRY_RUN="0", NOTIFY=str(script), REPORTS=fake_reports(tmp_path))
    assert "Weekly maintenance OK" in calls.read_text(), "the backup server was not woken: nothing to wait for"
    assert not (tmp_path / "logs" / ".weekly-summary").exists()


def test_dry_run_says_the_push_is_left_to_machine_reports(tmp_path):
    out = dry_run(tmp_path, WAKE_AFTER_REBOOT="0", REPORTS=fake_reports(tmp_path)).stdout
    assert "Weekly maintenance OK" not in out
    assert out.index("DRY: weekly push left to") < out.index("DRY: /sbin/reboot")

def test_missing_helper_is_only_noted(tmp_path):
    r = source_and_run(tmp_path, 'notify_summary; echo "rc=$?"', DRY_RUN="0", NOTIFY=str(tmp_path / "absent"))
    assert "not installed, no notification sent (Weekly maintenance OK)" in r.stdout
    assert "rc=0" in r.stdout


def test_a_failing_helper_does_not_fail_the_job(tmp_path):
    bad = tmp_path / "bad-notify"
    bad.write_text("#!/bin/bash\necho 'ntfy-notify: answered HTTP 500' >&2\nexit 2\n")
    bad.chmod(0o755)
    r = source_and_run(tmp_path, 'notify_summary; echo "rc=$?"', DRY_RUN="0", NOTIFY=str(bad))
    assert "note: notification failed (Weekly maintenance OK)" in r.stdout
    assert "rc=0" in r.stdout


SMART_BAD = """\
SMART overall-health self-assessment test result: FAILED!
  5 Reallocated_Sector_Ct   0x0033   100   100   140    Pre-fail  Always       -       12
  9 Power_On_Hours          0x0032   090   090   000    Old_age   Always       -       7000
194 Temperature_Celsius     0x0022   110   095   000    Old_age   Always       -       33
197 Current_Pending_Sector  0x0032   200   200   000    Old_age   Always       -       3
198 Offline_Uncorrectable   0x0030   100   253   000    Old_age   Offline      -       0
"""
SMART_GOOD = SMART_BAD.replace("FAILED!", "PASSED").replace("-       12", "-       0").replace("-       3\n", "-       0\n")


def run_disk_health(tmp_path, smart_output, notify):
    (tmp_path / "smart.txt").write_text(smart_output)
    return source_and_run(tmp_path, f'smartctl() {{ cat "{tmp_path}/smart.txt"; }}; disk_health; echo "failures=$FAILURES"',
                          DRY_RUN="0", NOTIFY=str(notify))


def test_disk_problems_are_pushed_as_urgent(tmp_path):
    script, calls = fake_notify(tmp_path)
    r = run_disk_health(tmp_path, SMART_BAD, script)
    assert "failures=1" in r.stdout, r.stdout
    assert (tmp_path / "logs" / "DISK_WARNING").exists()
    call = calls.read_text()
    assert call.startswith("-p urgent -t rotating_light Disk /dev/sda reports problems ")
    assert "health=FAILED! reallocated=12 pending=3 uncorrectable=0" in call


def test_a_healthy_disk_pushes_nothing(tmp_path):
    script, calls = fake_notify(tmp_path)
    r = run_disk_health(tmp_path, SMART_GOOD, script)
    assert "failures=0" in r.stdout, r.stdout
    assert not calls.exists()
    assert not (tmp_path / "logs" / "DISK_WARNING").exists()
    assert "health=PASSED reallocated=0 pending=0 uncorrectable=0 power_on_hours=7000 temp=33C" in r.stdout


def test_an_aborted_run_is_pushed(tmp_path):
    script, calls = fake_notify(tmp_path)
    r = source_and_run(tmp_path, 'CURRENT_STEP="dist-upgrade"; on_exit', DRY_RUN="0", NOTIFY=str(script))
    assert r.returncode == 0, r.stderr
    assert calls.read_text().startswith("-p high -t warning Weekly maintenance aborted Aborted during: dist-upgrade")


# --------------------------------------------------------------------------- backup output

def fake_dietpi_backup(tmp_path, rc=0):
    """Prints what dietpi-backup prints: a flood of rsync noise around a few summary lines."""
    f = tmp_path / "dietpi-backup"
    f.write_text(
        "#!/bin/bash\n"
        "echo 'Backup log from 2026-09-13_03:30:35'\n"
        "echo 'sending incremental file list'\n"
        "for i in $(seq 1 500); do echo \"deleting var/cache/apt/archives/pkg$i.deb\"; done\n"
        "echo 'home/dietpi/host-maintenance/README.md'\n"
        "echo 'etc/pihole/'\n"
        "echo 'sent 1,234,567 bytes  received 89 bytes  12,345.67 bytes/sec'\n"
        "echo 'total size is 9,876,543,210  speedup is 7,999.00'\n"
        "echo '[  OK  ] DietPi-Backup | Backup completed'\n"
        f"exit {rc}\n")
    f.chmod(0o755)
    return f


def test_backup_noise_is_filtered_but_the_summary_and_exit_code_survive(tmp_path):
    fake = fake_dietpi_backup(tmp_path)
    r = source_and_run(tmp_path, 'dietpi_backup; echo "rc=$?"', DRY_RUN="0", DIETPI_BACKUP=str(fake))
    lines = r.stdout.splitlines()
    assert "rc=0" in lines
    assert not any(l.startswith("deleting ") for l in lines), "rsync deletions are dropped"
    assert "home/dietpi/host-maintenance/README.md" not in lines and "etc/pihole/" not in lines, "file listings too"
    assert "sending incremental file list" not in lines
    assert "Backup log from 2026-09-13_03:30:35" in lines
    assert "sent 1,234,567 bytes  received 89 bytes  12,345.67 bytes/sec" in lines
    assert "total size is 9,876,543,210  speedup is 7,999.00" in lines
    assert "[  OK  ] DietPi-Backup | Backup completed" in lines
    assert len(lines) < 10, lines


def test_a_failed_backup_is_still_counted_after_filtering(tmp_path):
    """The pipe through grep must not hide dietpi-backup's exit code (PIPESTATUS)."""
    fake = fake_dietpi_backup(tmp_path, rc=3)
    r = source_and_run(tmp_path, 'dietpi_backup; echo "rc=$?"', DRY_RUN="0", DIETPI_BACKUP=str(fake))
    assert "rc=3" in r.stdout.splitlines()


def test_dry_run_still_names_the_real_backup_command(tmp_path):
    out = dry_run(tmp_path, WAKE_AFTER_REBOOT="0").stdout
    assert "DRY: /boot/dietpi/dietpi-backup 1" in out
    assert "deleting" not in out


# --------------------------------------------------------------------------- container images

def test_dry_run_updates_containers_after_pihole_and_before_the_reboot(tmp_path):
    out = dry_run(tmp_path, WAKE_AFTER_REBOOT="0").stdout
    a = out.index("DRY: /usr/local/bin/pihole -g")
    b = out.index("DRY: /usr/bin/docker compose ls -a --format json")
    c = out.index("DRY: /usr/bin/docker image prune -f")
    d = out.index("DRY: /sbin/reboot")
    assert a < b < c < d


def fake_docker(tmp_path, *, metube_changes=True, health="healthy", pull_fails=False):
    """Two Compose projects; metube's image id changes after the pull, ntfy's does not."""
    docker = tmp_path / "docker"
    calls = tmp_path / "docker_calls"
    docker.write_text(f"""#!/bin/bash
echo "$*" >> "{calls}"
case "$*" in
  "compose ls -a --format json")
    echo '[{{"Name":"metube","Status":"running(1)","ConfigFiles":"/srv/metube/compose.yaml"}},{{"Name":"ntfy","Status":"running(1)","ConfigFiles":"/srv/ntfy/compose.yaml"}}]' ;;
  "compose -f /srv/metube/compose.yaml images -q")
    n=$(cat "{tmp_path}/metube_images" 2>/dev/null || echo 0); echo $((n+1)) > "{tmp_path}/metube_images"
    if [ "$n" -ge 1 ] && [ "{str(metube_changes).lower()}" = true ]; then echo sha256:new; else echo sha256:old; fi ;;
  "compose -f /srv/ntfy/compose.yaml images -q") echo sha256:same ;;
  "compose -f "*" pull --quiet") [ "{str(pull_fails).lower()}" = true ] && {{ echo "pull access denied"; exit 1; }} ; exit 0 ;;
  "compose -f "*" up -d") exit 0 ;;
  "compose -f "*" ps -q") echo cid1 ;;
  "inspect -f "*) echo "{health}" ;;
  "image prune -f") exit 0 ;;
esac
""")
    docker.chmod(0o755)
    return docker, calls


def test_only_projects_whose_image_changed_count_as_updated(tmp_path):
    docker, calls = fake_docker(tmp_path)
    r = source_and_run(tmp_path, 'update_containers; echo "updated=[$UPDATED_CONTAINERS] failures=$FAILURES"',
                       DRY_RUN="0", DOCKER=str(docker), CONTAINER_HEALTH_WAIT="0")
    assert "metube: image updated" in r.stdout and "metube: healthy" in r.stdout
    assert "ntfy: already current" in r.stdout
    assert "updated=[metube] failures=0" in r.stdout, r.stdout
    log = calls.read_text()
    assert "compose -f /srv/metube/compose.yaml pull --quiet" in log
    assert "compose -f /srv/ntfy/compose.yaml up -d" in log, "every project is brought up, changed or not"
    assert log.strip().endswith("image prune -f"), "old layers are pruned last"


def test_an_unhealthy_recreated_container_is_a_counted_failure(tmp_path):
    docker, _ = fake_docker(tmp_path, health="unhealthy")
    r = source_and_run(tmp_path, 'update_containers; echo "failures=$FAILURES"',
                       DRY_RUN="0", DOCKER=str(docker), CONTAINER_HEALTH_WAIT="0")
    assert "WARNING: metube is 'unhealthy' 0s after the update" in r.stdout
    assert "failures=1" in r.stdout


def test_a_failed_pull_keeps_the_current_image_and_is_counted(tmp_path):
    docker, calls = fake_docker(tmp_path, pull_fails=True)
    r = source_and_run(tmp_path, 'update_containers; echo "updated=[$UPDATED_CONTAINERS] failures=$FAILURES"',
                       DRY_RUN="0", DOCKER=str(docker), CONTAINER_HEALTH_WAIT="0")
    assert "WARNING: metube: image pull failed, keeping the current image: pull access denied" in r.stdout
    assert "updated=[] failures=2" in r.stdout, "both projects failed to pull"
    assert " up -d" not in calls.read_text(), "nothing is recreated after a failed pull"


def test_updated_containers_are_named_in_the_ok_push(tmp_path):
    script, calls = fake_notify(tmp_path)
    source_and_run(tmp_path, 'UPDATED_CONTAINERS="metube ntfy"; notify_summary', DRY_RUN="0", NOTIFY=str(script))
    assert "Containers updated: metube ntfy" in calls.read_text()


def test_missing_docker_is_skipped_quietly(tmp_path):
    r = source_and_run(tmp_path, 'update_containers; echo "failures=$FAILURES"', DRY_RUN="0", DOCKER=str(tmp_path / "nope"))
    assert "docker is not installed" in r.stdout and "failures=0" in r.stdout
