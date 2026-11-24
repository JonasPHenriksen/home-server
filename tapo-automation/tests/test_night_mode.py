"""
night_mode.sh: the on/off/tonight switch for the night camera rules, with an
optional time window.
"""
import subprocess
import time

import pytest

NOW = 1_800_000_000


def local_epoch(day, hhmm):
    return int(subprocess.check_output(["date", "-d", f"{day} {hhmm}", "+%s"]).strip())


def local_day(epoch):
    return subprocess.check_output(["date", "-d", f"@{epoch}", "+%F"]).decode().strip()


def local_hhmm(epoch):
    return subprocess.check_output(["date", "-d", f"@{epoch}", "+%H:%M"]).decode().strip()


def flag(night_mode):
    return dict(line.split("=", 1) for line in (night_mode.base / ".tapo_night_mode").read_text().split())


def test_status_is_off_by_default(night_mode):
    r = night_mode.run(now=NOW, args=["status"])
    assert r.returncode == 0
    assert r.stdout.strip() == "night mode: OFF"
    r = night_mode.run(now=NOW)  # no argument = status
    assert r.stdout.strip() == "night mode: OFF"


def test_on_writes_the_default_window(night_mode):
    r = night_mode.run(now=NOW, args=["on"])
    assert r.returncode == 0, r.stderr
    assert flag(night_mode) == {"start": "21:00", "end": "02:00"}
    assert r.stdout.startswith("night mode: ON 21:00-02:00 UTC, every night until switched off (window")
    assert "between 21:00 and 02:00 UTC" in r.stdout
    assert "[night] night mode switched ON, window 21:00-02:00" in night_mode.log()
    assert night_mode.run(now=NOW, args=["status"]).stdout.startswith(
        "night mode: ON 21:00-02:00 UTC, every night until switched off (window")


def test_on_with_a_custom_window(night_mode):
    r = night_mode.run(now=NOW, args=["on", "20:00-04:30"])
    assert r.returncode == 0, r.stderr
    assert flag(night_mode) == {"start": "20:00", "end": "04:30"}
    assert "night mode: ON 20:00-04:30 UTC" in r.stdout
    assert "between 20:00 and 04:30 UTC" in r.stdout


def test_window_times_are_normalised(night_mode):
    night_mode.run(now=NOW, args=["on", "7-9:05"])
    assert flag(night_mode) == {"start": "07:00", "end": "09:05"}


@pytest.mark.parametrize("bad", ["20:00", "24:00-02:00", "21:00-02:60", "21:00-21:00", "evening", "20:00-", "-02:00"])
def test_bad_window_is_rejected(night_mode, bad):
    r = night_mode.run(now=NOW, args=["on", bad])
    assert r.returncode == 2
    assert "bad window" in r.stderr or "usage" in r.stderr
    assert not night_mode.exists(".tapo_night_mode")


def test_status_says_whether_the_window_is_active(night_mode):
    day = local_day(NOW)
    night_mode.run(now=local_epoch(day, "22:00"), args=["on"])
    assert "(window active now)" in night_mode.run(now=local_epoch(day, "22:00"), args=["status"]).stdout
    assert "(window opens at 21:00)" in night_mode.run(now=local_epoch(day, "12:00"), args=["status"]).stdout


def test_off_removes_the_flag(night_mode):
    night_mode.run(now=NOW, args=["on"])
    r = night_mode.run(now=NOW, args=["off"])
    assert r.returncode == 0
    assert not night_mode.exists(".tapo_night_mode")
    assert "night mode: OFF" in r.stdout
    assert "[night] night mode switched OFF" in night_mode.log()


def test_tonight_expires_when_the_window_next_closes(night_mode):
    day = local_day(NOW)
    at_22 = local_epoch(day, "22:00")
    r = night_mode.run(now=at_22, args=["tonight"])
    assert r.returncode == 0, r.stderr
    f = flag(night_mode)
    assert f["start"] == "21:00" and f["end"] == "02:00"
    assert int(f["expires"]) == local_epoch(day, "02:00") + 86400
    assert "then off again" in r.stdout
    assert "night mode: ON 21:00-02:00 UTC until" in night_mode.run(now=at_22, args=["status"]).stdout


def test_tonight_after_midnight_expires_the_same_morning(night_mode):
    day = local_day(NOW)
    at_1 = local_epoch(day, "01:00")
    night_mode.run(now=at_1, args=["tonight"])
    assert int(flag(night_mode)["expires"]) == local_epoch(day, "02:00")


def test_tonight_with_a_custom_window_expires_at_its_end(night_mode):
    day = local_day(NOW)
    at_22 = local_epoch(day, "22:00")
    night_mode.run(now=at_22, args=["tonight", "21:30-03:45"])
    f = flag(night_mode)
    assert f["start"] == "21:30" and f["end"] == "03:45"
    assert int(f["expires"]) == local_epoch(day, "03:45") + 86400
    # a window that has not opened yet today still expires at its next close, today
    at_10 = local_epoch(day, "10:00")
    night_mode.run(now=at_10, args=["tonight", "11:00-12:00"])
    assert int(flag(night_mode)["expires"]) == local_epoch(day, "12:00")


def test_status_reports_an_expired_flag(night_mode):
    night_mode.write(".tapo_night_mode", f"start=21:00\nend=02:00\nexpires={NOW - 60}")
    r = night_mode.run(now=NOW, args=["status"])
    assert r.stdout.startswith("night mode: OFF (expired")


def test_status_understands_legacy_flag_files(night_mode):
    (night_mode.base / ".tapo_night_mode").write_text("")
    assert night_mode.run(now=NOW, args=["status"]).stdout.startswith("night mode: ON 21:00-02:00 UTC, every night")
    (night_mode.base / ".tapo_night_mode").write_text(f"{NOW + 3600}\n")
    assert night_mode.run(now=NOW, args=["status"]).stdout.startswith("night mode: ON 21:00-02:00 UTC until")


def test_bad_arguments(night_mode):
    assert night_mode.run(now=NOW, args=["maybe"]).returncode == 2
    assert night_mode.run(now=NOW, args=["off", "now"]).returncode == 2
    assert night_mode.run(now=NOW, args=["on", "21:00-02:00", "extra"]).returncode == 2


def test_runs_with_the_real_clock(night_mode):
    r = night_mode.run(args=["tonight"])
    assert r.returncode == 0, r.stderr
    expires = int(flag(night_mode)["expires"])
    assert time.time() < expires <= time.time() + 86400 + 3600
    assert local_hhmm(expires) == "02:00"
