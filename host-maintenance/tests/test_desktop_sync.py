"""
desktop/dietpi-backup-sync.sh: the desktop's weekly job, which runs on another
machine. Only its own logic is exercised here - the Saturday slot, the clock
wait, the GPU check, the report and how it reaches the laptop - by sourcing it,
which runs nothing.
"""
import calendar
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "desktop" / "dietpi-backup-sync.sh"


def utc(y, mo, d, h=0, mi=0):
    return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0))


def sourced(snippet, tmp_path, **env):
    e = dict(os.environ, TZ="UTC", LAST_RUN_FILE=str(tmp_path / "last_run"), **env)
    return subprocess.run(["bash", "-c", f"source {SCRIPT}; {snippet}"], env=e,
                          capture_output=True, text=True, timeout=60)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_the_weekly_slot_is_the_most_recent_saturday_23_00(tmp_path):
    cases = {
        utc(2026, 9, 26, 22, 59): utc(2026, 9, 19, 23),   # Saturday before 23:00: last week's
        utc(2026, 9, 26, 23, 0): utc(2026, 9, 26, 23),    # Saturday 23:00 itself
        utc(2026, 9, 27, 1, 47): utc(2026, 9, 26, 23),    # Sunday night, as the laptop's job starts
        utc(2026, 9, 29, 12, 0): utc(2026, 9, 26, 23),    # Tuesday
        utc(2026, 10, 2, 23, 30): utc(2026, 9, 26, 23),   # Friday late
    }
    for now, slot in cases.items():
        r = sourced(f"last_slot {now}", tmp_path)
        assert r.stdout.strip() == str(slot), (now, r.stdout, r.stderr)


def test_one_successful_run_per_saturday_to_saturday_week(tmp_path):
    last = tmp_path / "last_run"
    assert sourced(f"already_done {utc(2026, 9, 29)} && echo done || echo due", tmp_path).stdout.strip() == "due", \
        "never ran"
    last.write_text(f"{utc(2026, 9, 29, 8)}\n")    # a Tuesday catch-up run
    assert sourced(f"already_done {utc(2026, 10, 1)} && echo done || echo due", tmp_path).stdout.strip() == "done"
    assert sourced(f"already_done {utc(2026, 10, 3, 23)} && echo done || echo due", tmp_path).stdout.strip() == "due", \
        "the Saturday after a catch-up still runs"
    last.write_text("2026-39\n")                   # the old ISO-week marker
    assert sourced(f"already_done {utc(2026, 9, 29)} && echo done || echo due", tmp_path).stdout.strip() == "due"
    last.write_text(f"{utc(2027, 1, 1)}\n")       # written by a run on a clock that was months ahead
    assert sourced(f"already_done {utc(2026, 9, 29)} && echo done || echo due", tmp_path).stdout.strip() == "due", \
        "a mark from the future would skip every week until then"


def test_it_waits_for_a_synced_clock_and_only_then_trusts_the_week_check(tmp_path):
    flag = tmp_path / "synchronized"
    flag.touch()
    r = sourced("CLOCK_WAIT_SECS=0; wait_for_clock; echo \"$CLOCK_SYNCED ${#FAILURES[@]}\"", tmp_path, TIMESYNC_FLAG=str(flag))
    assert r.stdout.strip().splitlines()[-1] == "yes 0", r.stdout
    flag.unlink()
    r = sourced("CLOCK_WAIT_SECS=0; wait_for_clock; echo \"$CLOCK_SYNCED ${#FAILURES[@]}\"", tmp_path, TIMESYNC_FLAG=str(flag))
    assert r.stdout.strip().splitlines()[-1] == "no 1", r.stdout
    assert "FAILED: clock not synchronised after 0 min" in r.stdout

    (tmp_path / "last_run").write_text(f"{utc(2026, 9, 29, 8)}\n")    # done this week
    due = f"due_now {utc(2026, 10, 1)} && echo due || echo done"
    assert sourced(f"CLOCK_SYNCED=yes; {due}", tmp_path).stdout.strip() == "done"
    assert sourced(f"CLOCK_SYNCED=no; {due}", tmp_path).stdout.strip() == "due", \
        "an unsynced clock can't tell this week from last week: run, a second run costs nothing"


def fake_bin(tmp_path, **scripts):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    for name, body in scripts.items():
        (b / name).write_text(f"#!/bin/bash\n{body}\n")
        (b / name).chmod(0o755)
    return f"{b}:{os.environ['PATH']}"


def test_a_boot_on_a_rewound_clock_still_runs(tmp_path):
    # fake-hwclock starts the desktop at the time it was last switched off. A run done last week
    # would then look like this week's, and the job would exit before timesyncd put the clock right.
    path = fake_bin(tmp_path, ssh="exit 255")
    flag = tmp_path / "synchronized"
    flag.touch()
    (tmp_path / "last_run").write_text(f"{int(time.time())}\n")
    main = f"LOGFILE={tmp_path}/log; CLOCK_WAIT_SECS=0; main"
    r = sourced(main, tmp_path, TIMESYNC_FLAG=str(flag), PATH=path)
    assert r.returncode == 0 and "Already ran since" in r.stdout, r.stdout + r.stderr
    flag.unlink()
    r = sourced(main, tmp_path, TIMESYNC_FLAG=str(flag), PATH=path)
    assert "FAILED: clock not synchronised" in r.stdout
    assert "not reachable over SSH" in r.stdout and r.returncode == 1, "it went on with the run"


def test_the_gpu_check_asks_ollama_which_library_it_uses(tmp_path):
    journal = tmp_path / "journal"
    path = fake_bin(tmp_path, **{"nvidia-smi": f"exit $(cat {tmp_path}/smi)",
                                 "systemctl": f"exit $(cat {tmp_path}/active)",
                                 "journalctl": f"cat {journal}"})
    check = 'gpu_check; echo "GPU=$GPU OLLAMA_GPU=$OLLAMA_GPU"'
    at_boot = ('time=2026-09-26T07:31:02.101Z level=WARN source=cuda_compat.go:70 msg="NVIDIA driver too old" '
               'device="NVIDIA GeForce GTX 1070" compute=6.1 driver=550 required_driver="570 or newer"\n'
               'time=2026-09-26T07:31:05.426Z level=INFO source=types.go:32 msg="inference compute" id=0 '
               'library=CUDA compute=6.1 name=CUDA0 description="NVIDIA GeForce GTX 1070"\n')
    restart = ('time=2026-09-26T08:09:40.426Z level=INFO source=types.go:32 msg="inference compute" id=0 filter_id=0 '
               'library=Vulkan compute=0.0 name=Vulkan0 description="NVIDIA GeForce GTX 1070" libdirs=ollama,vulkan '
               'driver=0.0 pci_id=0000:01:00.0 type=discrete total="8.2 GiB" available="8.1 GiB"\n')
    (tmp_path / "smi").write_text("0")
    (tmp_path / "active").write_text("0")

    journal.write_text(at_boot + restart)       # the last start counts
    r = sourced(check, tmp_path, PATH=path)
    assert "GPU=ok OLLAMA_GPU=Vulkan" in r.stdout and "FAILED" not in r.stdout, r.stdout + r.stderr
    assert "Smoke test GPU: ok, Ollama uses it through Vulkan" in r.stdout

    journal.write_text('msg="inference compute" id=cpu library=cpu compute="" name=cpu description=cpu\n')
    r = sourced(check, tmp_path, PATH=path)
    assert "GPU=cpu" in r.stdout and "FAILED: smoke test GPU (Ollama runs on the CPU" in r.stdout

    journal.write_text("")                      # aged out of the RAM journal
    r = sourced(check, tmp_path, PATH=path)
    assert "GPU=unknown OLLAMA_GPU=unknown" in r.stdout and "FAILED" not in r.stdout

    (tmp_path / "active").write_text("3")
    r = sourced(check, tmp_path, PATH=path)
    assert "GPU=failed" in r.stdout and "FAILED: smoke test GPU (the ollama service is not running)" in r.stdout

    (tmp_path / "smi").write_text("9")
    r = sourced(check, tmp_path, PATH=path)
    assert "GPU=failed" in r.stdout and "FAILED: smoke test GPU (nvidia module not loaded" in r.stdout
    assert "libcuda" not in SCRIPT.read_text().split("gpu_check(){")[1].split("\n}\n")[0], \
        "Vulkan doesn't use the CUDA library, so the check must not want it"


def smart_output(model, serial, realloc, pending=0, uncorr=0):
    return (f"Device Model:     {model}\nSerial Number:    {serial}\n"
            "SMART overall-health self-assessment test result: PASSED\n"
            "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE\n"
            f"  5 Reallocated_Sector_Ct   0x0033   100   100   005    Pre-fail  Always       -       {realloc}\n"
            f"197 Current_Pending_Sector  0x0022   100   100   000    Old_age   Always       -       {pending}\n"
            f"198 Offline_Uncorrectable   0x0008   100   100   000    Old_age   Offline      -       {uncorr}\n")


def test_the_smart_check_only_reads_the_counters(tmp_path):
    path = fake_bin(tmp_path,
                    lsblk=r'printf "sda disk\nsdb disk\nzram0 disk\nsr0 rom\n"',
                    smartctl=(f'echo "$*" >> {tmp_path}/smartctl_args; d=$(basename "${{@: -1}}"); '
                              f'cat {tmp_path}/out_$d; exit $(cat {tmp_path}/rc_$d 2>/dev/null || echo 0)'))
    check = 'smart_check; echo "SMART=$SMART"'

    def disk(name, model, serial, realloc=0, pending=0, uncorr=0, rc=0):
        (tmp_path / f"out_{name}").write_text(smart_output(model, serial, realloc, pending, uncorr))
        (tmp_path / f"rc_{name}").write_text(str(rc))

    def ssd(**counters):
        disk("sda", "PNY CS900 480GB SSD", "PNY0000001", **counters)

    def toshiba(**counters):
        disk("sdb", "TOSHIBA DT01ACA300", "EXAMPLE2", **counters)

    ssd()
    toshiba(realloc=48, pending=8)              # 4 Oct 2026: 48 reallocated, up from 35 a week earlier
    r = sourced(check, tmp_path, PATH=path)
    assert "SMART=failed" in r.stdout, r.stdout + r.stderr
    assert ("FAILED: SMART TOSHIBA DT01ACA300: smartctl status 0, reallocated 48, pending 8, uncorrectable 0"
            in r.stdout), "unplugged on 17 Oct 2026, and plugged back in the dying disk must warn again"
    assert "SMART PNY CS900 480GB SSD: ok (reallocated 0, pending 0)" in r.stdout
    args = (tmp_path / "smartctl_args").read_text()
    assert args.splitlines() == ["-i -H -A /dev/sda", "-i -H -A /dev/sdb"], "real disks only: no zram, no DVD drive"
    assert "-t" not in args.split(), "light on purpose: it must never start a self-test"

    toshiba()                                   # a healthy disk in its place
    allowed = 'REALLOC_OK=([PNY0000001]=2); PENDING_OK=([PNY0000001]=1); ' + check
    ssd(realloc=2, pending=1)                   # a known count allowed for that serial: only growth warns
    r = sourced(allowed, tmp_path, PATH=path)
    assert "SMART=ok" in r.stdout and "SMART PNY CS900 480GB SSD: ok (reallocated 2, pending 1)" in r.stdout, r.stdout
    for worse, shown in ((dict(realloc=3, pending=1), "reallocated 3"), (dict(realloc=2, pending=2), "pending 2"),
                         (dict(realloc=2, pending=1, uncorr=1), "uncorrectable 1")):
        ssd(**worse)                            # worse than its allowance, or anything unreadable for good
        r = sourced(allowed, tmp_path, PATH=path)
        assert "SMART=failed" in r.stdout and "FAILED: SMART PNY CS900 480GB SSD: smartctl status 0" in r.stdout, worse
        assert shown in r.stdout, worse

    ssd(pending=1)                              # without an allowance the first pending sector fails
    r = sourced(check, tmp_path, PATH=path)
    assert "SMART=failed" in r.stdout and "FAILED: SMART PNY CS900 480GB SSD: smartctl status 0" in r.stdout

    ssd(rc=8)                                   # smartctl's own verdict: the disk is failing
    r = sourced(check, tmp_path, PATH=path)
    assert "SMART=failed" in r.stdout and "FAILED: SMART PNY CS900 480GB SSD: smartctl status 8" in r.stdout

    ssd(rc=4)                                   # one ATA command failed, which says nothing about the disk
    r = sourced(check, tmp_path, PATH=path)
    assert "SMART=ok" in r.stdout and "FAILED" not in r.stdout

    (tmp_path / "bin" / "smartctl").unlink()    # smartmontools missing (the real one lives in /usr/sbin)
    r = sourced(check, tmp_path, PATH=f"{tmp_path}/bin:/usr/bin:/bin")
    assert "SMART=failed" in r.stdout and "FAILED: SMART check (smartctl is not installed" in r.stdout


# A fake `sudo -u www-data php ...`: occ status, update:check and setupchecks, and the updater, which
# "installs" the version in TMP/installs when it succeeds.
FAKE_NEXTCLOUD = r'''shift 3; echo "$*" >> TMP/calls
case "$*" in
    *"occ status"*)       v=$(cat TMP/version); [ -n "$v" ] && printf '{"installed":true,"versionstring":"%s"}\n' "$v" ;;
    *"occ update:check"*) cat TMP/check ;;
    *"occ setupchecks"*)  cat TMP/setupchecks ;;
    *updater.phar*)       rc=$(cat TMP/updater_rc); [ "$rc" = 0 ] && cp TMP/installs TMP/version; exit "$rc" ;;
esac'''

POINT_RELEASE = ("Nextcloud 35.0.3 is available. Get more information on how to update at "
                 "https://docs.nextcloud.com/server/35/admin_manual/maintenance/upgrade.html.\n"
                 "Update for files_pdfviewer to version 5.0.1 is available.\n2 updates available\n")


def nextcloud(tmp_path, version="35.0.2", check="Everything up to date\n", installs="35.0.3", updater_rc=0,
              setupchecks="\tsystem:\n\t\t✓ Checking for PHP default charset: UTF-8\n"):
    for name, value in dict(version=version, check=check, installs=installs, updater_rc=updater_rc,
                            setupchecks=setupchecks, calls="").items():
        (tmp_path / name).write_text(str(value))
    return fake_bin(tmp_path, sudo=FAKE_NEXTCLOUD.replace("TMP", str(tmp_path)))


def test_nextcloud_point_releases_go_in_and_a_new_major_waits(tmp_path):
    def core(local_result=0, **state):
        r = sourced(f"LOCAL_RESULT={local_result}; nc_core_update", tmp_path, PATH=nextcloud(tmp_path, **state))
        calls = (tmp_path / "calls").read_text().splitlines()
        return r.stdout + r.stderr, [c for c in calls if "updater.phar" in c]

    out, updater = core()
    assert "Nextcloud core: 35.0.2, no update" in out and "FAILED" not in out and not updater, out

    out, updater = core(check=POINT_RELEASE)                    # after this run's backup: in it goes
    assert updater == ["/var/www/nextcloud/updater/updater.phar --no-interaction --no-backup"], updater
    assert "Nextcloud core: updated 35.0.2 -> 35.0.3" in out and "FAILED" not in out, out

    out, updater = core(local_result=1, check=POINT_RELEASE)    # no backup to go back to
    assert not updater and "Nextcloud core: 35.0.3 left alone, this run has no local backup" in out
    assert "FAILED" not in out, "the failed backup is a problem of its own already"

    out, updater = core(version="35.0.4", check=POINT_RELEASE.replace("35.0.3", "36.0.1"))
    assert not updater, "a new major version waits for the owner"
    assert "FAILED: nextcloud major version 36.0.1 is out: install it by hand" in out, out

    out, updater = core(check=POINT_RELEASE, updater_rc=1)
    assert "FAILED: nextcloud core update 35.0.2 -> 35.0.3 (updater exit 1;" in out, out

    out, updater = core(check=POINT_RELEASE, installs="36.0.0")  # a major that slips in is never quiet
    assert "FAILED: nextcloud core: the updater installed 36.0.0, a new major version" in out, out

    out, updater = core(version="", check=POINT_RELEASE)         # occ itself broken
    assert not updater and "FAILED: nextcloud core update (occ status or update:check failed)" in out, out


def test_nextcloud_overview_warnings_and_errors_become_problems(tmp_path):
    path = nextcloud(tmp_path, setupchecks=(
        "\tsystem:\n"
        "\t\t✓ Checking for PHP default charset: UTF-8\n"
        "\t\t⚠ Errors in the log: 2 errors in the logs since 10 October 2026 at 09:12:39 UTC\n"
        "\t\tℹ Cron last run: Last background job execution ran 2 minutes ago.\n"
        "\t\t⚠ Database missing indices: Detected some missing optional indices. Use the command "
        "`occ db:add-missing-indices` to add them.\n"
        'Missing optional index "fs_name_hash" in table "filecache".\n'
        "\tnetwork:\n"
        "\t\t✗ Internet connectivity: This server has no working internet connection: Multiple endpoints "
        "could not be reached.\n"))
    path = fake_bin(tmp_path, php="exit 0")                     # nothing in the log
    r = sourced("nc_checks", tmp_path, PATH=path, LC_ALL="C.UTF-8")
    failed = [l.split("FAILED: ", 1)[1] for l in r.stdout.splitlines() if "FAILED: " in l]
    assert failed == [
        "nextcloud overview warning: Database missing indices: Detected some missing optional indices. "
        "Use the command `occ db:add-missing-indices` to add them.",
        "nextcloud overview error: Internet connectivity: This server has no working internet connection: "
        "Multiple endpoints could not be reached."], r.stdout + r.stderr


def test_the_overview_s_cron_last_run_never_counts(tmp_path):
    # 18 Oct 2026, a run right after a night off. The desktop is switched on when needed, so after nearly
    # every boot the background jobs last ran hours ago, and every such run, and every Sunday push, would warn.
    path = nextcloud(tmp_path, setupchecks=(
        "\tsystem:\n"
        "\t\t✗ Cron last run: Last background job execution ran 10 hours ago. Something seems wrong. "
        "Check the background job settings.\n"
        "\tnetwork:\n"
        "\t\t✗ Internet connectivity: This server has no working internet connection: Multiple endpoints "
        "could not be reached.\n"))
    path = fake_bin(tmp_path, php="exit 0")                     # nothing in the log
    r = sourced("nc_checks", tmp_path, PATH=path, LC_ALL="C.UTF-8")
    failed = [l.split("FAILED: ", 1)[1] for l in r.stdout.splitlines() if "FAILED: " in l]
    assert failed == [
        "nextcloud overview error: Internet connectivity: This server has no working internet connection: "
        "Multiple endpoints could not be reached."], r.stdout + r.stderr


def test_nextcloud_log_errors_count_from_the_last_run(tmp_path):
    msg = 'Could not create folder "/helle/files/Billeder/Sommerferie på Færøerne"'
    nextcloud(tmp_path)
    path = fake_bin(tmp_path, php=f'printf "%s\\n" "$@" > {tmp_path}/php_args; cat {tmp_path}/php_out')
    (tmp_path / "php_out").write_text(f"3 {msg}")
    (tmp_path / "last_run").write_text(f"{utc(2026, 10, 17, 23, 5)}\n")
    r = sourced(REPORT_RUN + "nc_checks; build_report", tmp_path, PATH=path, LC_ALL="C.UTF-8")
    line = (f'FAILED: nextcloud log: 3 error(s) since Sat 17 Oct 23:05, most often "{msg}" '
            "(Administration settings > Logging)")
    problems = r.stdout.split("===== problems\n")[1].split("\n=====")[0]
    assert line in problems, r.stdout + r.stderr
    args = (tmp_path / "php_args").read_text().splitlines()
    assert args[-2:] == ["/mnt/dietpi_userdata/nextcloud_data/nextcloud.log", str(utc(2026, 10, 17, 23, 5))]

    (tmp_path / "last_run").unlink()                            # no mark yet: the last seven days
    (tmp_path / "php_out").write_text("")
    r = sourced("nc_checks", tmp_path, PATH=path, LC_ALL="C.UTF-8")
    assert "FAILED" not in r.stdout, r.stdout
    since = int((tmp_path / "php_args").read_text().splitlines()[-1])
    assert abs(since - (time.time() - 7 * 86400)) < 60

    code = SCRIPT.read_text().split("nc_checks(){")[1].split("\n}\n")[0]
    assert "mb_strcut(" in code and "substr(" not in code, \
        "a cut inside a character makes the line invalid UTF-8, and grep drops it from the report and the push"
    # 17 Oct 2026: "The requested uri(/mnt/dietpi_userdata/nextcloud_data/.ncdata) cannot be processed by
    # the script '/nextcloud/index.php')" is what setupchecks' own data folder check leaves in the log, so
    # counting it would make every run, and every Sunday push, warn. There is no PHP on the laptop, so
    # this can only look for the skip.
    assert 'str_contains($msg, "/.ncdata) cannot be processed by the script")' in code, \
        "setupchecks above logs that error itself on every run"


def test_everyday_nextcloud_log_errors_never_count():
    # 24 Oct 2026: a week of everyday use left 36 errors in nextcloud.log, all of three kinds, and both
    # reports that day pushed them. Each skip is a piece of text, here with the message as Nextcloud logs
    # it: as in that log. The duplicate entry's key name was cut off in the report; the only unique
    # index of a storage number and an md5 is the file list's fs_storage_path_hash. There is no PHP on
    # the laptop, so this checks that each skip is in the scan and that its text is in the message.
    code = SCRIPT.read_text().split("nc_checks(){")[1].split("\n}\n")[0]
    skipped = {
        "Unable to open preview stream at ":
            "Unable to open preview stream at /mnt/dietpi_userdata/nextcloud_data/appdata_ocexample0001/preview/"
            "2/d/1/a/a/1/d/565711/64-64-crop.jpg",
        "(from Nextcloud client)":
            "Expected filesize of 3779674 bytes but read (from Nextcloud client) and wrote (to Nextcloud storage) "
            "1785856 bytes. Could either be a network problem on the sending side or a problem writing to the "
            "storage on the server side.",
    }
    for text, message in skipped.items():
        assert f'str_contains($msg, "{text}")' in code and text in message, text

    duplicate = ("An exception occurred while executing a query: SQLSTATE[23000]: Integrity constraint violation: "
                 "1062 Duplicate entry '3-cb93fc10b204c362af6d6ed85b798b43' for key 'fs_storage_path_hash'")
    both = '(str_contains($msg, "1062 Duplicate entry") && str_contains($msg, "fs_storage_path_hash"))'
    assert both in code, "only that index: a duplicate anywhere else still counts"
    assert "1062 Duplicate entry" in duplicate and "fs_storage_path_hash" in duplicate


REPORT_RUN = ('DATE_STAMP=2026-09-26_23-00; RUN_STARTED=$(date -d "2026-09-26 23:00:05" +%s); '
              'LOCAL_RESULT=0; SYS_RESULT=0; NC_RESULT=0; SYS_FILES=7561; SYS_BYTES=1930766069; '
              'NC_FILES=2; NC_BYTES=15099440; NC_CODE=200; OWUI_CODE=200; GPU=ok; RUN_DONE=yes; '
              'DISK_FREE=253G; OLLAMA_VERSION="ollama version is 0.33.2"; NC_VERSION=34.0.2; CLOCK_SYNCED=yes; '
              'OLLAMA_GPU=Vulkan; SMART=ok; '
              f'INSTALLED_FILES=({SCRIPT}); ')


def keyvalues(report):
    head = report.split("\n=====")[0]
    return dict(l.split("=", 1) for l in head.splitlines() if "=" in l and not l.startswith("#"))


def test_the_report_carries_the_run(tmp_path):
    r = sourced(REPORT_RUN + 'log "System sync to remote: SUCCESS"; build_report', tmp_path)
    assert r.returncode == 0, r.stderr
    kv = keyvalues(r.stdout)
    assert kv["run"] == "2026-09-26_23-00" and kv["started"] == "2026-09-26T23:00:05Z"
    assert kv["status"] == "ok" and kv["failures"] == "0"
    assert (kv["local_backup"], kv["system_sync"], kv["nextcloud_sync"]) == ("ok", "ok", "ok")
    assert kv["system_files_transferred"] == "7561" and kv["nextcloud_bytes_transferred"] == "15099440"
    assert (kv["smoke_nextcloud"], kv["smoke_openwebui"], kv["smoke_gpu"]) == ("200", "200", "ok")
    assert kv["ollama"] == "ollama version is 0.33.2" and kv["run_done"] == "yes" and kv["clock_synced"] == "yes"
    assert kv["ollama_gpu"] == "Vulkan" and kv["smart"] == "ok"
    assert len(kv["sha256_dietpi-backup-sync.sh"]) == 64
    assert "===== problems\nnone\n" in r.stdout
    log = r.stdout.split("===== log\n")[1]
    assert "System sync to remote: SUCCESS" in log


def test_a_failed_run_lists_what_failed(tmp_path):
    r = sourced(REPORT_RUN + 'NC_RESULT=12; fail "nextcloud sync to remote (rsync exit 12)"; '
                'GPU=failed; fail "smoke test GPU (nvidia module not loaded)"; build_report', tmp_path)
    kv = keyvalues(r.stdout)
    assert kv["status"] == "failed" and kv["failures"] == "2"
    assert kv["nextcloud_sync"] == "failed (rsync exit 12)"
    problems = r.stdout.split("===== problems\n")[1].split("\n=====")[0].splitlines()
    assert len(problems) == 2
    assert problems[0].endswith("FAILED: nextcloud sync to remote (rsync exit 12)")


def test_the_report_reaches_the_laptop_under_a_temporary_name_first(tmp_path):
    path = fake_bin(tmp_path, ssh=f'printf "%s\\n" "$@" > {tmp_path}/ssh_args\ncat > {tmp_path}/ssh_stdin')
    r = sourced(REPORT_RUN + "send_report", tmp_path, PATH=path)
    assert r.returncode == 0, r.stderr
    args = (tmp_path / "ssh_args").read_text()
    assert "desktop@192.168.50.10" in args and "-i\n/root/.ssh/id_ed25519_dietpi" in args
    remote = args.strip().splitlines()[-1]
    assert remote == ("mkdir -p '/mnt/dietpi_userdata/logs/desktop' && "
                      "cat > '/mnt/dietpi_userdata/logs/desktop/.report-2026-09-26_23-00.txt' && "
                      "mv -f '/mnt/dietpi_userdata/logs/desktop/.report-2026-09-26_23-00.txt' "
                      "'/mnt/dietpi_userdata/logs/desktop/report-2026-09-26_23-00.txt'")
    assert (tmp_path / "ssh_stdin").read_text().startswith("# desktop report, format 1\n")
    assert "Report sent to 192.168.50.10 (report-2026-09-26_23-00.txt)" in r.stdout


def test_ollama_gets_a_home_and_the_timer_runs_saturday_night():
    assert 'HOME=/root ollama --version' in SCRIPT.read_text()
    timer = (ROOT / "desktop" / "dietpi-boot-backup.timer").read_text()
    assert "OnCalendar=Sat *-*-* 23:00:00" in timer
    assert "OnBootSec=2min" in timer and "Persistent=true" in timer
    assert "Mon" not in timer
