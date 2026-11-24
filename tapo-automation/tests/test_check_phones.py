"""
check_phones_and_toggle.sh: the presence state machine, run for real in bash
against a fake ping and a fake toggle command.
"""
import time

from conftest import wait_until

import pytest

NOW = 1_800_000_000
COOLDOWN_BASE = 60
COOLDOWN_MAX = 900
THRESHOLD_START = 3
THRESHOLD_STEP = 3
THRESHOLD_MAX = 120


def test_fresh_install_with_phone_home_applies_disable_once(phones):
    phones.phones_home("10.0.0.1")
    r = phones.run(now=NOW)
    assert r.returncode == 0, r.stderr
    assert phones.toggle_calls() == ["disable all"]
    assert phones.read(".tapo_motion_state") == "disable"
    assert phones.read(".tapo_ping_fail_count") == "0"
    assert "state change: '' -> 'disable'" in phones.log()

    r = phones.run(now=NOW + 10)
    assert r.returncode == 0
    assert phones.toggle_calls() == ["disable all"], "nothing to do when already home"


def test_arms_only_after_threshold_consecutive_misses(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_away()
    for i in range(1, THRESHOLD_START):
        phones.run(now=NOW + 10 * i)
        assert phones.toggle_calls() == []
        assert phones.read(".tapo_ping_fail_count") == str(i)

    phones.run(now=NOW + 10 * THRESHOLD_START)
    assert phones.toggle_calls() == ["enable all"]
    assert phones.read(".tapo_motion_state") == "enable"
    assert phones.read(".tapo_enable_time") == str(NOW + 10 * THRESHOLD_START)
    assert not phones.exists(".tapo_pending")


def test_single_phone_answering_counts_as_home(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_ping_fail_count", "2")
    phones.phones_home("10.0.0.2")  # only the second phone answers
    phones.run(now=NOW)
    assert phones.toggle_calls() == []
    assert phones.read(".tapo_ping_fail_count") == "0"


def test_phone_home_disables_immediately(phones):
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_enable_time", str(NOW - 5000))
    phones.write(".tapo_ping_fail_count", "40")
    phones.phones_home("10.0.0.1")
    phones.run(now=NOW)
    assert phones.toggle_calls() == ["disable all"]
    assert phones.read(".tapo_motion_state") == "disable"
    assert phones.read(".tapo_ping_fail_count") == "0"
    assert phones.read(".tapo_away_threshold") is None, "a long trip must not raise the threshold"


def test_short_trip_raises_threshold(phones):
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_enable_time", str(NOW - 300))
    phones.phones_home("10.0.0.1")
    phones.run(now=NOW)
    assert phones.read(".tapo_away_threshold") == str(THRESHOLD_START + THRESHOLD_STEP)
    assert "cameras were only on for 300s, raising away threshold to 6 checks" in phones.log()


def test_threshold_is_capped(phones):
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_enable_time", str(NOW - 10))
    phones.write(".tapo_away_threshold", str(THRESHOLD_MAX - 1))
    phones.phones_home("10.0.0.1")
    phones.run(now=NOW)
    assert phones.read(".tapo_away_threshold") == str(THRESHOLD_MAX)


def test_raised_threshold_is_honoured(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_away_threshold", "5")
    phones.phones_away()
    for i in range(1, 5):
        phones.run(now=NOW + i)
        assert phones.toggle_calls() == []
    phones.run(now=NOW + 5)
    assert phones.toggle_calls() == ["enable all"]


# --------------------------------------------------------------------------- incomplete switches

def test_failed_switch_becomes_pending_with_backoff(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_ping_fail_count", "2")
    phones.phones_away()
    phones.toggle_rc(1)
    phones.run(now=NOW)
    assert phones.toggle_calls() == ["enable all"]
    assert phones.read(".tapo_motion_state") == "enable", "intent is recorded even if cameras fail"
    assert phones.exists(".tapo_pending")
    assert phones.read(".tapo_switch_fail_streak") == "1"
    assert phones.read(".tapo_retry_after") == str(NOW + COOLDOWN_BASE)
    assert "switch to enable incomplete (rc=1, streak 1), retrying the rest in 60s" in phones.log()


def test_pending_switch_waits_for_the_cooldown(phones):
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_ping_fail_count", "10")
    phones.write(".tapo_pending", "")
    phones.write(".tapo_switch_fail_streak", "1")
    phones.write(".tapo_retry_after", str(NOW + 30))
    phones.phones_away()
    phones.run(now=NOW)
    assert phones.toggle_calls() == []
    phones.run(now=NOW + 29)
    assert phones.toggle_calls() == []
    phones.run(now=NOW + 30)
    assert phones.toggle_calls() == ["enable all"]
    assert "retrying incomplete switch to 'enable'" in phones.log()


def test_backoff_doubles_and_is_capped(phones):
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_ping_fail_count", "10")
    phones.write(".tapo_pending", "")
    phones.phones_away()
    phones.toggle_rc(1)
    now = NOW
    expected = [60, 120, 240, 480, 900, 900]
    for streak, cooldown in enumerate(expected, start=1):
        phones.write(".tapo_retry_after", str(now))
        phones.run(now=now)
        assert phones.read(".tapo_switch_fail_streak") == str(streak)
        assert phones.read(".tapo_retry_after") == str(now + cooldown)
        now += cooldown
    assert len(phones.toggle_calls()) == len(expected)


def test_success_clears_pending_state(phones):
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_ping_fail_count", "10")
    phones.write(".tapo_pending", "")
    phones.write(".tapo_switch_fail_streak", "4")
    phones.write(".tapo_retry_after", str(NOW))
    phones.phones_away()
    phones.run(now=NOW)
    assert phones.toggle_calls() == ["enable all"]
    assert not phones.exists(".tapo_pending")
    assert not phones.exists(".tapo_switch_fail_streak")
    assert not phones.exists(".tapo_retry_after")
    assert "switch to enable fully applied" in phones.log()


def test_state_change_supersedes_pending_retry(phones):
    """Coming home while an 'enable' is still retrying must switch to disable now."""
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_enable_time", str(NOW - 5000))
    phones.write(".tapo_ping_fail_count", "10")
    phones.write(".tapo_pending", "")
    phones.write(".tapo_switch_fail_streak", "6")
    phones.write(".tapo_retry_after", str(NOW + 800))
    phones.phones_home("10.0.0.1")
    phones.run(now=NOW)
    assert phones.toggle_calls() == ["disable all"]
    assert phones.read(".tapo_motion_state") == "disable"
    assert not phones.exists(".tapo_pending")
    assert not phones.exists(".tapo_switch_fail_streak")


def test_state_change_resets_backoff_even_when_it_fails_too(phones):
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_enable_time", str(NOW - 5000))
    phones.write(".tapo_pending", "")
    phones.write(".tapo_switch_fail_streak", "6")
    phones.write(".tapo_retry_after", str(NOW + 800))
    phones.phones_home("10.0.0.1")
    phones.toggle_rc(1)
    phones.run(now=NOW)
    assert phones.toggle_calls() == ["disable all"]
    assert phones.read(".tapo_switch_fail_streak") == "1", "streak restarts with the new target"
    assert phones.read(".tapo_retry_after") == str(NOW + COOLDOWN_BASE)


def test_pending_disable_is_retried_while_home(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_pending", "")
    phones.write(".tapo_switch_fail_streak", "1")
    phones.write(".tapo_retry_after", str(NOW))
    phones.phones_home("10.0.0.1")
    phones.run(now=NOW)
    assert phones.toggle_calls() == ["disable all"]


# --------------------------------------------------------------------------- robustness

def test_hanging_toggle_is_killed_by_the_timeout_guard(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_ping_fail_count", "2")
    phones.phones_away()
    phones.toggle_sleep(30)
    phones.toggle_timeout = 1
    started = time.monotonic()
    r = phones.run(now=NOW, timeout=20)
    elapsed = time.monotonic() - started
    assert r.returncode == 0
    assert elapsed < 8, f"loop was blocked for {elapsed:.1f}s"
    assert phones.exists(".tapo_pending")
    assert "toggle did not finish within 1s and was killed" in phones.log()
    assert "incomplete (rc=124" in phones.log()


def test_second_instance_exits_while_the_first_holds_the_lock(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_ping_fail_count", "2")
    phones.phones_away()
    lock = phones.hold_lock(".tapo_phones.lock")
    try:
        r = phones.run(now=NOW)
    finally:
        lock.close()
    assert r.returncode == 0
    assert phones.toggle_calls() == []
    assert phones.read(".tapo_ping_fail_count") == "2", "a skipped run must not touch state"


def test_log_is_trimmed(phones):
    (phones.base / "tapo.log").write_text("".join(f"old line {i}\n" for i in range(2500)))
    phones.phones_home("10.0.0.1")
    phones.run(now=NOW)
    lines = phones.log().splitlines()
    assert len(lines) <= 1001
    assert "state change" in lines[-2]
    assert "fully applied" in lines[-1]


def test_no_temp_files_left_behind(phones, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    phones.phones_home("10.0.0.1")
    r = phones.run(now=NOW)
    assert r.returncode == 0
    assert list((tmp_path / "tmp").iterdir()) == []


def test_runs_with_default_environment_paths(phones, monkeypatch):
    """Without TAPO_NOW the script must still run (uses date +%s)."""
    phones.phones_home("10.0.0.1")
    r = phones.run()
    assert r.returncode == 0
    assert phones.toggle_calls() == ["disable all"]


# --------------------------------------------------------------------------- probe failures

def test_probe_error_is_fail_safe_and_logged_once(phones):
    """A broken detector must never arm the cameras."""
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_enable_time", str(NOW - 5000))
    phones.write(".tapo_ping_fail_count", "10")
    phones.probe_rc(2)
    phones.run(now=NOW)
    assert phones.toggle_calls() == ["disable all"], "probe failure counts as somebody home"
    assert phones.read(".tapo_ping_fail_count") == "0"
    assert phones.exists(".tapo_probe_error")
    assert phones.log().count("phone probe failed (rc=2): probe exploded; assuming someone is home") == 1

    phones.run(now=NOW + 10)
    phones.run(now=NOW + 20)
    assert phones.log().count("phone probe failed") == 1, "logged once, not every 10 s"

    (phones.base / "probe_rc").unlink()
    phones.phones_home("10.0.0.1")
    phones.run(now=NOW + 30)
    assert not phones.exists(".tapo_probe_error")
    assert "phone probe working again" in phones.log()


def test_hanging_probe_is_killed_and_treated_as_home(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_ping_fail_count", "2")
    phones.probe_sleep(30)
    phones.probe_timeout = 1
    started = time.monotonic()
    r = phones.run(now=NOW, timeout=20)
    assert r.returncode == 0
    assert time.monotonic() - started < 8
    assert phones.toggle_calls() == []
    assert phones.read(".tapo_ping_fail_count") == "0"
    assert "phone probe failed (rc=124)" in phones.log()


def test_state_change_log_includes_the_probe_summary(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_ping_fail_count", "2")
    phones.phones_away()
    phones.run(now=NOW)
    assert "probe: away: nobody answered)" in phones.log()
    phones.phones_home("10.0.0.2")
    phones.run(now=NOW + 10)
    assert "probe: home: 10.0.0.2 via fake)" in phones.log()


# --------------------------------------------------------------------------- night mode window

def night_on(phones, expiry=None):
    phones.write(".tapo_night_mode", "" if expiry is None else str(expiry))


def test_night_window_is_ignored_while_night_mode_is_off(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    for hour in (20, 21, 23, 1, 2):
        phones.run(now=NOW, hour=hour)
    assert phones.toggle_calls() == []
    assert phones.read(".tapo_night_window", "0") == "0"


def test_night_window_opening_and_closing_resyncs_the_cameras(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    night_on(phones)

    phones.run(now=NOW, hour=20)
    assert phones.toggle_calls() == []
    assert phones.read(".tapo_night_window", "0") == "0", "missing file means 'window closed'"

    phones.run(now=NOW + 3600, hour=21)
    assert phones.toggle_calls() == ["disable all"], "the toggle recomputes the night group from the state"
    assert phones.read(".tapo_night_window") == "1"
    assert "night window 21:00-02:00 opened, re-syncing cameras for 'disable'" in phones.log()

    phones.run(now=NOW + 7200, hour=23)
    assert phones.toggle_calls() == ["disable all"], "no repeat while the window stays open"

    phones.run(now=NOW + 5 * 3600, hour=2)
    assert phones.toggle_calls() == ["disable all", "disable all"]
    assert phones.read(".tapo_night_window") == "0"
    assert "night window closed, re-syncing cameras for 'disable'" in phones.log()


def test_night_window_closing_while_away_keeps_the_cameras_armed(phones):
    """The old night_cams.sh put the night camera into privacy mode at 02:00 even when nobody was home."""
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_ping_fail_count", "50")
    phones.write(".tapo_night_window", "1")
    phones.phones_away()
    night_on(phones)
    phones.run(now=NOW, hour=2)
    assert phones.toggle_calls() == ["enable all"]


def test_switching_night_mode_on_during_the_window_applies_within_one_run(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    phones.run(now=NOW, hour=22)
    assert phones.toggle_calls() == []
    night_on(phones)
    phones.run(now=NOW + 10, hour=22)
    assert phones.toggle_calls() == ["disable all"]
    (phones.base / ".tapo_night_mode").unlink()  # night_mode.sh off
    phones.run(now=NOW + 20, hour=22)
    assert phones.toggle_calls() == ["disable all", "disable all"]
    assert phones.read(".tapo_night_window") == "0"


def test_expired_night_mode_is_switched_off(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_night_window", "1")
    phones.phones_home("10.0.0.1")
    night_on(phones, expiry=NOW - 1)
    phones.run(now=NOW, hour=1)
    assert not phones.exists(".tapo_night_mode")
    assert "night mode expired, switched off" in phones.log()
    assert phones.toggle_calls() == ["disable all"], "window closes with the expiry"
    assert phones.read(".tapo_night_window") == "0"


def test_night_mode_with_future_expiry_is_active(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    night_on(phones, expiry=NOW + 3600)
    phones.run(now=NOW, hour=22)
    assert phones.toggle_calls() == ["disable all"]
    assert phones.exists(".tapo_night_mode")


def test_state_change_and_window_change_together_apply_once(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_ping_fail_count", "2")
    phones.phones_away()
    night_on(phones)
    phones.run(now=NOW, hour=21)
    assert phones.toggle_calls() == ["enable all"]
    assert phones.read(".tapo_night_window") == "1"
    phones.run(now=NOW + 10, hour=21)
    assert phones.toggle_calls() == ["enable all"], "window already recorded with the state change"


def test_window_change_supersedes_a_pending_retry(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_pending", "")
    phones.write(".tapo_switch_fail_streak", "5")
    phones.write(".tapo_retry_after", str(NOW + 800))
    phones.phones_home("10.0.0.1")
    night_on(phones)
    phones.run(now=NOW, hour=21)
    assert phones.toggle_calls() == ["disable all"]
    assert not phones.exists(".tapo_pending")
    assert not phones.exists(".tapo_switch_fail_streak")


def test_window_change_before_any_state_does_nothing(phones):
    """Fresh install at night with nobody home yet: no state to re-apply."""
    phones.phones_away()
    night_on(phones)
    phones.run(now=NOW, hour=22)
    assert phones.toggle_calls() == []
    assert not phones.exists(".tapo_motion_state")


def test_night_window_failure_uses_the_normal_backoff(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    night_on(phones)
    phones.toggle_rc(1)
    phones.run(now=NOW, hour=21)
    assert phones.toggle_calls() == ["disable all"]
    assert phones.exists(".tapo_pending")
    assert phones.read(".tapo_retry_after") == str(NOW + COOLDOWN_BASE)
    phones.run(now=NOW + COOLDOWN_BASE, hour=21)
    assert len(phones.toggle_calls()) == 2, "retried through the pending mechanism, not the window logic"


# --------------------------------------------------------------------------- configurable window

def night_on_window(phones, start, end, expiry=None):
    lines = [f"start={start}", f"end={end}"]
    if expiry is not None:
        lines.append(f"expires={expiry}")
    (phones.base / ".tapo_night_mode").write_text("\n".join(lines) + "\n")


def window_open(phones, time, **kw):
    """Run once at `time` and report whether the loop considers the window open."""
    (phones.base / ".tapo_night_window").unlink(missing_ok=True)
    phones.calls_file.unlink(missing_ok=True)
    phones.run(now=NOW, time=time, **kw)
    return phones.read(".tapo_night_window", "0") == "1"


def test_custom_window_crossing_midnight(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    night_on_window(phones, "20:30", "03:15")
    assert window_open(phones, "20:29") is False
    assert window_open(phones, "20:30") is True
    assert window_open(phones, "23:59") is True
    assert window_open(phones, "00:00") is True
    assert window_open(phones, "03:14") is True
    assert window_open(phones, "03:15") is False
    assert window_open(phones, "12:00") is False
    assert "night window 20:30-03:15 opened" in phones.log()


def test_custom_window_within_one_day(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    night_on_window(phones, "00:30", "05:00")
    assert window_open(phones, "23:00") is False
    assert window_open(phones, "00:29") is False
    assert window_open(phones, "00:30") is True
    assert window_open(phones, "04:59") is True
    assert window_open(phones, "05:00") is False


def test_window_change_alone_triggers_a_resync(phones):
    """Re-running `night_mode.sh on` with a new window that covers now must apply."""
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    night_on_window(phones, "21:00", "02:00")
    phones.run(now=NOW, time="20:00")
    assert phones.toggle_calls() == []
    night_on_window(phones, "19:30", "02:00")
    phones.run(now=NOW + 10, time="20:00")
    assert phones.toggle_calls() == ["disable all"]
    assert "night window 19:30-02:00 opened" in phones.log()


def test_malformed_window_falls_back_to_the_default(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    (phones.base / ".tapo_night_mode").write_text("start=25:99\nend=nonsense\n")
    assert window_open(phones, "20:59") is False
    assert window_open(phones, "21:00") is True
    assert window_open(phones, "01:59") is True
    assert window_open(phones, "02:00") is False


def test_legacy_flag_formats_still_mean_default_window(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.phones_home("10.0.0.1")
    (phones.base / ".tapo_night_mode").write_text("")
    assert window_open(phones, "22:00") is True
    (phones.base / ".tapo_night_mode").write_text(f"{NOW + 3600}\n")
    assert window_open(phones, "22:00") is True
    assert window_open(phones, "03:00") is False


def test_expires_key_is_honoured(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_night_window", "1")
    phones.phones_home("10.0.0.1")
    night_on_window(phones, "21:00", "02:00", expiry=NOW - 1)
    phones.run(now=NOW, time="23:00")
    assert not phones.exists(".tapo_night_mode")
    assert "night mode expired" in phones.log()
    assert phones.toggle_calls() == ["disable all"]


# --------------------------------------------------------------------------- loop start (boot resync)

ONCE = {"TAPO_LOOP_ONCE": "1"}


def test_loop_start_reapplies_the_last_state_to_every_camera(loop):
    loop.write(".tapo_motion_state", "disable")
    loop.write(".tapo_cam_state.json", '{"1.2.3.4": "p1a0f0"}')
    loop.write(".tapo_switch_fail_streak", "3")
    loop.write(".tapo_retry_after", str(NOW + 900))
    loop.phones_home("10.0.0.1")
    r = loop.run(now=NOW, extra=ONCE)
    assert r.returncode == 0, r.stderr
    assert loop.toggle_calls() == ["disable all"], "one full re-apply of the last state"
    assert not loop.exists(".tapo_cam_state.json"), "belief about the cameras is dropped first"
    assert not loop.exists(".tapo_pending"), "cleared again once the toggle succeeded"
    assert "loop started, re-syncing every camera to 'disable'" in loop.log()


def test_loop_start_resync_retries_through_the_pending_machinery(loop):
    loop.write(".tapo_motion_state", "enable")
    loop.write(".tapo_ping_fail_count", "10")
    loop.phones_away()
    loop.toggle_rc(1)
    loop.run(now=NOW, extra=ONCE)
    assert loop.toggle_calls() == ["enable all"]
    assert loop.exists(".tapo_pending")
    assert loop.read(".tapo_switch_fail_streak") == "1"
    assert loop.read(".tapo_retry_after") == str(NOW + COOLDOWN_BASE)


def test_loop_start_without_a_known_state_does_nothing_special(loop):
    loop.phones_home("10.0.0.1")
    loop.run(now=NOW, extra=ONCE)
    assert "loop started" not in loop.log()
    assert loop.toggle_calls() == ["disable all"], "just the normal first-run behaviour"


# --------------------------------------------------------------------------- notifications

def test_a_long_incomplete_switch_is_pushed_once_and_its_recovery_too(phones):
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_ping_fail_count", "10")
    phones.write(".tapo_pending", "")
    phones.phones_away()
    phones.toggle_rc(1)
    now = NOW
    for streak, cooldown in enumerate([60, 120, 240, 480, 900], start=1):
        phones.write(".tapo_retry_after", str(now))
        phones.run(now=now)
        now += cooldown
        if streak < 4:
            time.sleep(0.05)
            assert phones.notify_calls() == [], f"no push yet at streak {streak}"
    calls = phones.wait_for_notify_calls(1)
    assert calls[0].startswith("-p high -t warning Cameras not switching to enable 4 attempts failed (rc=1)")
    assert "Retrying every 480s" in calls[0]
    assert len(calls) == 1, "streak 5 does not push again"
    assert phones.exists(".tapo_switch_notified")
    assert "notification: Cameras not switching to enable" in phones.log()

    (phones.base / "toggle_rc").unlink()  # the camera is back
    phones.write(".tapo_retry_after", str(now))
    phones.run(now=now)
    calls = phones.wait_for_notify_calls(2)
    assert calls[1].startswith("-p low -t white_check_mark Cameras in sync again Switch to enable fully applied.")
    assert not phones.exists(".tapo_switch_notified")
    assert not phones.exists(".tapo_pending")


def test_a_state_change_during_a_pushed_outage_does_not_push_again(phones):
    # already pushed, then the phones come home while the camera is still down
    phones.write(".tapo_motion_state", "enable")
    phones.write(".tapo_pending", "")
    phones.write(".tapo_switch_fail_streak", "5")
    phones.write(".tapo_switch_notified", "")
    phones.phones_home("10.0.0.1")
    phones.toggle_rc(1)
    for i in range(6):
        phones.write(".tapo_retry_after", str(NOW + 10 * i))
        phones.run(now=NOW + 10 * i)
    assert phones.toggle_calls()[0] == "disable all"
    assert phones.read(".tapo_switch_fail_streak") == "6", "the streak restarted with the new target"
    time.sleep(0.1)
    assert phones.notify_calls() == []
    assert phones.exists(".tapo_switch_notified")


def test_a_short_incomplete_switch_is_not_pushed(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.write(".tapo_ping_fail_count", "2")
    phones.phones_away()
    phones.toggle_rc(1)
    phones.run(now=NOW)
    phones.write(".tapo_retry_after", str(NOW + 60))
    phones.run(now=NOW + 60)
    (phones.base / "toggle_rc").unlink()
    phones.write(".tapo_retry_after", str(NOW + 180))
    phones.run(now=NOW + 180)
    assert "switch to enable fully applied" in phones.log()
    time.sleep(0.1)
    assert phones.notify_calls() == []


def test_a_broken_probe_is_pushed_once_and_its_recovery_too(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.probe_rc(2)
    phones.run(now=NOW)
    phones.run(now=NOW + 10)
    calls = phones.wait_for_notify_calls(1)
    assert calls[0].startswith("-p high -t warning Presence probe broken phone_probe.py failed (rc=2): probe exploded")
    time.sleep(0.1)
    assert len(phones.notify_calls()) == 1
    (phones.base / "probe_rc").unlink()
    phones.phones_home("10.0.0.1")
    phones.run(now=NOW + 20)
    calls = phones.wait_for_notify_calls(2)
    assert calls[1].startswith("-p low -t white_check_mark Presence probe working again")


def test_a_missing_helper_is_logged_and_nothing_else_breaks(phones):
    phones.write(".tapo_motion_state", "disable")
    phones.probe_rc(2)
    r = phones.run(now=NOW, extra={"TAPO_NOTIFY_CMD": str(phones.base / "missing-helper")})
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == ""
    assert "phone probe failed (rc=2)" in phones.log()
    wait_until(lambda: "notification failed:" in phones.log(), message="the failure to be logged")
    assert "missing-helper" in phones.log()
