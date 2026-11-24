"""
security-upgrades: the daily security-fix job, exercised in dry-run mode and
by sourcing its helpers, so nothing on the machine changes.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "security-upgrades"
APT_CONF = ROOT / "apt" / "52unattended-upgrades-security-only"


def dry_run(tmp_path, **env):
    e = dict(os.environ, DRY_RUN="1", LOG_DIR=str(tmp_path / "logs"), WEEKDAY="3")
    e.update(env)
    return subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=60)


def source_and_run(tmp_path, snippet, **env):
    e = dict(os.environ, LOG_DIR=str(tmp_path / "logs"), WEEKDAY="3")
    e.update(env)
    (tmp_path / "logs").mkdir(exist_ok=True)
    return subprocess.run(["bash", "-c", f"source {SCRIPT}; {snippet}"], env=e,
                          capture_output=True, text=True, timeout=60)


def fake_notify(tmp_path):
    script = tmp_path / "fake-notify"
    calls = tmp_path / "notify_calls"
    script.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "$*" >> "{calls}"\n')
    script.chmod(0o755)
    return script, calls


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_dry_run_updates_lists_then_runs_unattended_upgrade(tmp_path):
    r = dry_run(tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.index("DRY: /usr/bin/apt-get update -q") < out.index("DRY: /usr/bin/unattended-upgrade -v")
    assert "STATUS: nothing to do" in out
    assert not (tmp_path / "logs" / "security-upgrades.log").exists(), "dry run writes no logs"


def test_sundays_are_left_to_the_weekly_job(tmp_path):
    r = dry_run(tmp_path, WEEKDAY="7")
    assert r.returncode == 0
    assert "STATUS: skipped: Sunday, the weekly job upgrades everything" in r.stdout
    assert "unattended-upgrade" not in r.stdout


def test_report_installed_packages_logs_and_pushes(tmp_path):
    script, calls = fake_notify(tmp_path)
    out = ("Packages that will be upgraded: libssl3t64 openssl\n"
           "Writing dpkg log to /var/log/unattended-upgrades/unattended-upgrades-dpkg.log\n"
           "All upgrades installed\n")
    (tmp_path / "logs").mkdir(exist_ok=True)
    r = subprocess.run(["bash", "-c", f"source {SCRIPT}; report \"$(cat)\" 0"], input=out,
                       env=dict(os.environ, LOG_DIR=str(tmp_path / "logs"), DRY_RUN="0", NOTIFY=str(script)),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "logs" / "security-upgrades.log").read_text()
    assert log.strip().endswith("installed: libssl3t64 openssl") or "installed: libssl3t64 openssl (reboot pending" in log
    call = calls.read_text()
    assert call.startswith("-p low -t lock Security patch installed libssl3t64 openssl")
    # the weekly job's push is "Weekly maintenance OK" with a check mark; this
    # one must not be mistakable for it on the phone
    assert "white_check_mark" not in call and "maintenance" not in call.lower()


def test_report_nothing_to_do_is_quiet(tmp_path):
    script, calls = fake_notify(tmp_path)
    r = source_and_run(tmp_path, 'report "No packages found that can be upgraded unattended and no pending auto-removals" 0',
                       DRY_RUN="0", NOTIFY=str(script))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "logs" / "security-upgrades.log").read_text().strip().endswith("nothing to do")
    assert not calls.exists(), "no push when nothing happened"


def test_report_failure_warns_and_pushes_high(tmp_path):
    script, calls = fake_notify(tmp_path)
    r = source_and_run(tmp_path, 'report "Packages that will be upgraded: foo\nE: something broke" 1; echo "rc=$?"',
                       DRY_RUN="0", NOTIFY=str(script))
    assert "rc=1" in r.stdout
    assert "WARNING: unattended-upgrade failed (rc=1), wanted: foo" in (tmp_path / "logs" / "security-upgrades.log").read_text()
    assert calls.read_text().startswith("-p high -t warning Security patch failed unattended-upgrade rc=1, wanted: foo")


def test_missing_helper_and_missing_unattended_upgrades_are_handled(tmp_path):
    r = source_and_run(tmp_path, 'notify low x "T" "m"; echo "rc=$?"', DRY_RUN="0", NOTIFY=str(tmp_path / "absent"))
    assert "not installed, no notification sent (T)" in r.stdout and "rc=0" in r.stdout
    r = source_and_run(tmp_path, 'UNATTENDED=/nonexistent; main; echo "rc=$?"', DRY_RUN="0")
    assert "rc=1" in r.stdout
    assert "skipped: unattended-upgrades is not installed" in (tmp_path / "logs" / "security-upgrades.log").read_text()


def test_skips_while_the_weekly_job_holds_its_lock(tmp_path):
    import fcntl
    (tmp_path / "logs").mkdir(exist_ok=True)
    fake_uu = tmp_path / "uu"; fake_uu.write_text("#!/bin/bash\necho ran\n"); fake_uu.chmod(0o755)
    with open(tmp_path / "logs" / ".maintenance.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = source_and_run(tmp_path, f'UNATTENDED={fake_uu}; main; echo "rc=$?"', DRY_RUN="0")
    assert "rc=0" in r.stdout, r.stdout
    assert "skipped: the weekly maintenance is running" in (tmp_path / "logs" / "security-upgrades.log").read_text()


def test_apt_update_retries_then_carries_on(tmp_path):
    r = source_and_run(tmp_path, 'APT_UPDATE_WAIT=0; tries=0; run() { tries=$((tries+1)); return 1; }; '
                                 'apt_update; echo "rc=$? tries=$tries"', DRY_RUN="0")
    assert "tries=3" in r.stdout and "rc=1" in r.stdout
    assert "running with the lists we have" in r.stdout


def test_apt_config_only_allows_the_security_origins():
    text = APT_CONF.read_text()
    assert "#clear Unattended-Upgrade::Origins-Pattern;" in text
    origins = [l.strip().strip('";') for l in text.splitlines() if l.strip().startswith('"origin=')]
    assert origins and all("label=Debian-Security" in o for o in origins), origins
    assert 'Unattended-Upgrade::Automatic-Reboot "false";' in text
    assert 'Unattended-Upgrade::Remove-Unused-Kernel-Packages "false";' in text
