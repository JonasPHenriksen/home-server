"""
tapo_toggle.py: parallel camera configuration with a hard deadline and
per-camera sync state. Uses a fake Tapo class, no camera or pytapo needed.
"""
import collections
import json
import logging
import threading
import time

import pytest

import tapo_toggle as tt
from conftest import CAM_3, CAM_2, CAM_1, write_config

SETTERS = ["setAlarm", "setMotionDetection", "setNotificationsEnabled",
           "setPersonDetection", "setTamperDetection"]


def noop_sleep(_secs):
    pass


class FakeCam:
    def __init__(self, ip, scenario, fail_on=None):
        self.ip = ip
        self.scenario = scenario
        self.fail_on = fail_on

    def _call(self, name, value):
        self.scenario.record(self.ip, name, value)
        if self.fail_on == name:
            raise ConnectionError(f"{name} failed half way")

    def setPrivacyMode(self, v): self._call("setPrivacyMode", v)
    def setAlarm(self, v): self._call("setAlarm", v)
    def setMotionDetection(self, v): self._call("setMotionDetection", v)
    def setNotificationsEnabled(self, v): self._call("setNotificationsEnabled", v)
    def setPersonDetection(self, v): self._call("setPersonDetection", v)
    def setTamperDetection(self, v): self._call("setTamperDetection", v)


class Scenario:
    """Per-IP behaviour, one step per connection attempt (the last step repeats).

    Steps: "ok", "unreachable", "auth", "hang", "fail_mid", ("slow", seconds).
    """

    def __init__(self, **per_ip):
        self.steps = {ip: (v if isinstance(v, list) else [v]) for ip, v in per_ip.items()}
        self.calls = []
        self.attempts = collections.Counter()
        self.lock = threading.Lock()

    def record(self, ip, name, value):
        with self.lock:
            self.calls.append((ip, name, value))

    def calls_for(self, ip):
        return [(n, v) for i, n, v in self.calls if i == ip]

    def factory(self, ip, user, password):
        assert (user, password) == ("admin", "secret")
        with self.lock:
            self.attempts[ip] += 1
            steps = self.steps.get(ip, ["ok"])
            step = steps[min(self.attempts[ip] - 1, len(steps) - 1)]
        if step == "unreachable":
            raise ConnectionError("HTTPSConnectionPool: No route to host")
        if step == "auth":
            raise Exception("Invalid authentication data")
        if step == "hang":
            threading.Event().wait(30)  # never returns within any test
            raise RuntimeError("unreachable")
        if isinstance(step, tuple) and step[0] == "slow":
            time.sleep(step[1])
        return FakeCam(ip, self, fail_on="setAlarm" if step == "fail_mid" else None)


def run(base, action="enable", group="all", scenario=None, **kw):
    scenario = scenario or Scenario()
    kw.setdefault("sleep", noop_sleep)
    kw.setdefault("is_night", False)
    rc = tt.run(action, group, str(base), camera_factory=scenario.factory, **kw)
    return rc, scenario


def state(base):
    p = base / ".tapo_cam_state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def expected_calls(target):
    return ([("setPrivacyMode", False)]
            + [(name, target["alarm"] if name == "setAlarm" else target["features"]) for name in SETTERS]
            + [("setPrivacyMode", target["privacy"])])


# --------------------------------------------------------------------------- pure logic

@pytest.mark.parametrize("enable,night_cam,is_night,expected", [
    (True, False, False, {"privacy": False, "alarm": True, "features": True}),
    (False, False, False, {"privacy": True, "alarm": False, "features": False}),
    (True, False, True, {"privacy": False, "alarm": True, "features": True}),
    (False, False, True, {"privacy": True, "alarm": False, "features": False}),
    (True, True, False, {"privacy": False, "alarm": True, "features": True}),
    (False, True, False, {"privacy": True, "alarm": False, "features": False}),
    # the night camera watches quietly at night even when we are home
    (True, True, True, {"privacy": False, "alarm": False, "features": True}),
    (False, True, True, {"privacy": False, "alarm": False, "features": True}),
])
def test_target_settings(enable, night_cam, is_night, expected):
    assert tt.target_settings(enable, night_cam, is_night) == expected


def test_signature_is_unique_per_combination():
    sigs = {tt.signature(tt.target_settings(e, n, i))
            for e in (True, False) for n in (True, False) for i in (True, False)}
    assert sigs == {"p0a1f1", "p1a0f0", "p0a0f1"}


@pytest.mark.parametrize("text,minutes", [("21", 1260), ("21:00", 1260), ("7:05", 425), ("00:00", 0), ("23:59", 1439)])
def test_parse_hhmm(text, minutes):
    assert tt.parse_hhmm(text) == minutes


@pytest.mark.parametrize("text", ["24:00", "21:60", "abc", "", "-1", "21:"])
def test_parse_hhmm_rejects_garbage(text):
    with pytest.raises(ValueError):
        tt.parse_hhmm(text)


@pytest.mark.parametrize("now,start,end,expected", [
    ("22:00", "21:00", "02:00", True), ("20:59", "21:00", "02:00", False), ("21:00", "21:00", "02:00", True),
    ("01:59", "21:00", "02:00", True), ("02:00", "21:00", "02:00", False), ("12:00", "21:00", "02:00", False),
    ("00:30", "00:30", "05:00", True), ("04:59", "00:30", "05:00", True), ("05:00", "00:30", "05:00", False),
    ("23:00", "00:30", "05:00", False),
])
def test_in_window(now, start, end, expected):
    assert tt.in_window(tt.parse_hhmm(now), tt.parse_hhmm(start), tt.parse_hhmm(end)) is expected


@pytest.mark.parametrize("msg,expected", [
    ("Invalid authentication data", True),
    ("Temporary Suspension: Try again in 1800 seconds", True),
    ("bad credentials", True),
    ("No route to host", False),
    ("Read timed out", False),
])
def test_is_auth_error(msg, expected):
    assert tt.is_auth_error(Exception(msg)) is expected


# --------------------------------------------------------------------------- happy path

def test_enable_configures_every_camera_and_records_state(base):
    rc, sc = run(base, "enable")
    assert rc == 0
    armed = tt.target_settings(True, False, False)
    for ip in (CAM_1, CAM_2, CAM_3):
        assert sc.calls_for(ip) == expected_calls(armed)
    assert state(base) == {CAM_1: "p0a1f1", CAM_2: "p0a1f1", CAM_3: "p0a1f1"}


def test_disable_at_night_keeps_night_camera_watching(base):
    rc, sc = run(base, "disable", is_night=True)
    assert rc == 0
    assert sc.calls_for(CAM_1) == expected_calls(tt.target_settings(False, False, True))
    assert sc.calls_for(CAM_3) == expected_calls(tt.target_settings(False, True, True))
    assert state(base) == {CAM_1: "p1a0f0", CAM_2: "p1a0f0", CAM_3: "p0a0f1"}


def test_night_group_only_touches_night_cameras(base):
    rc, sc = run(base, "enable", "night")
    assert rc == 0
    assert set(sc.attempts) == {CAM_3}
    assert state(base) == {CAM_3: "p0a1f1"}


def test_cameras_are_configured_in_parallel(base):
    slow = ("slow", 0.4)
    started = time.monotonic()
    rc, sc = run(base, scenario=Scenario(**{CAM_1: slow, CAM_2: slow, CAM_3: slow}))
    elapsed = time.monotonic() - started
    assert rc == 0
    assert elapsed < 0.4 * 3, f"cameras were configured serially ({elapsed:.2f}s)"


def test_settle_delays_are_used_between_privacy_changes(base):
    sleeps = []
    rc, _ = run(base, scenario=Scenario(**{CAM_1: "ok"}), sleep=sleeps.append)
    assert rc == 0
    assert sleeps.count(tt.SETTLE_DELAY) == 2 * 3  # two settle pauses per camera


# --------------------------------------------------------------------------- sync state

def test_in_sync_cameras_are_skipped(base, caplog):
    (base / ".tapo_cam_state.json").write_text(json.dumps({CAM_1: "p0a1f1", CAM_2: "p0a1f1"}))
    with caplog.at_level(logging.INFO):
        rc, sc = run(base, "enable")
    assert rc == 0
    assert set(sc.attempts) == {CAM_3}
    assert "already at p0a1f1, skipping" in caplog.text
    assert state(base) == {CAM_1: "p0a1f1", CAM_2: "p0a1f1", CAM_3: "p0a1f1"}


def test_force_reconfigures_in_sync_cameras(base):
    (base / ".tapo_cam_state.json").write_text(json.dumps({CAM_1: "p0a1f1", CAM_2: "p0a1f1", CAM_3: "p0a1f1"}))
    rc, sc = run(base, "enable", force=True)
    assert rc == 0
    assert set(sc.attempts) == {CAM_1, CAM_2, CAM_3}


def test_corrupt_state_file_is_treated_as_empty(base):
    (base / ".tapo_cam_state.json").write_text("{not json")
    rc, sc = run(base, "enable")
    assert rc == 0
    assert set(sc.attempts) == {CAM_1, CAM_2, CAM_3}


def test_failure_half_way_forgets_the_camera(base):
    """Crash safety: a camera we started touching must be retried next run."""
    (base / ".tapo_cam_state.json").write_text(json.dumps({CAM_1: "p1a0f0", CAM_2: "p1a0f0", CAM_3: "p1a0f0"}))
    rc, sc = run(base, "enable", scenario=Scenario(**{CAM_2: "fail_mid"}))
    assert rc == 1
    st = state(base)
    assert CAM_2 not in st, "a half configured camera must not be marked as in sync"
    assert st[CAM_1] == "p0a1f1" and st[CAM_3] == "p0a1f1"
    # the next run only touches the camera that failed
    rc, sc = run(base, "enable")
    assert rc == 0
    assert set(sc.attempts) == {CAM_2}
    assert state(base)[CAM_2] == "p0a1f1"


# --------------------------------------------------------------------------- failure isolation

def test_unreachable_camera_does_not_block_the_others(base, caplog):
    with caplog.at_level(logging.INFO):
        rc, sc = run(base, "enable", scenario=Scenario(**{CAM_2: "unreachable"}))
    assert rc == 1
    assert sc.attempts[CAM_2] == tt.MAX_RETRIES
    assert sc.attempts[CAM_1] == 1 and sc.attempts[CAM_3] == 1
    st = state(base)
    assert CAM_2 not in st
    assert st[CAM_1] == "p0a1f1" and st[CAM_3] == "p0a1f1"
    assert "still out of sync: cam_2" in caplog.text
    assert "leaving it out of sync for the next run" in caplog.text


def test_retry_succeeds_on_second_attempt(base):
    sleeps = []
    rc, sc = run(base, scenario=Scenario(**{CAM_1: ["unreachable", "ok"]}), sleep=sleeps.append)
    assert rc == 0
    assert sc.attempts[CAM_1] == 2
    assert tt.BASE_DELAY in sleeps
    assert state(base)[CAM_1] == "p0a1f1"


def test_retry_delays_double(base):
    sleeps = []
    rc, _ = run(base, scenario=Scenario(**{CAM_1: "unreachable"}), sleep=sleeps.append,
                deadline_secs=1000)
    assert rc == 1
    retry_sleeps = [s for s in sleeps if s != tt.SETTLE_DELAY]
    assert retry_sleeps == [tt.BASE_DELAY, tt.BASE_DELAY * 2]


def test_auth_error_is_not_retried(base, caplog):
    with caplog.at_level(logging.ERROR):
        rc, sc = run(base, scenario=Scenario(**{CAM_1: "auth"}))
    assert rc == 1
    assert sc.attempts[CAM_1] == 1
    assert "NOT retrying to avoid lockout" in caplog.text
    assert CAM_1 not in state(base)


def test_hanging_camera_hits_the_deadline(base, caplog):
    started = time.monotonic()
    with caplog.at_level(logging.ERROR):
        rc, sc = run(base, scenario=Scenario(**{CAM_2: "hang"}), deadline_secs=0.5)
    elapsed = time.monotonic() - started
    assert rc == 1
    assert elapsed < 2.0, f"deadline not enforced ({elapsed:.1f}s)"
    assert "deadline of 0s reached, still busy: cam_2" in caplog.text or \
           "deadline of 1s reached, still busy: cam_2" in caplog.text
    st = state(base)
    assert CAM_2 not in st
    assert st[CAM_1] == "p0a1f1" and st[CAM_3] == "p0a1f1"


def test_result_arriving_after_the_deadline_is_ignored(base):
    rc, sc = run(base, scenario=Scenario(**{CAM_2: ("slow", 0.8)}), deadline_secs=0.3)
    assert rc == 1
    time.sleep(1.0)  # the slow worker finishes now
    assert CAM_2 not in state(base), "late results must not be written after the run reported"


def test_no_retry_when_deadline_is_too_close(base, caplog):
    class Clock:
        t = 100.0

        def __call__(self):
            return self.t

    # deadline 3s away; the first retry would need 2s + margin, the second 4s: only 1 attempt happens
    with caplog.at_level(logging.ERROR):
        rc, sc = run(base, scenario=Scenario(**{CAM_1: "unreachable"}), clock=Clock(), deadline_secs=1.5)
    assert rc == 1
    assert sc.attempts[CAM_1] == 1
    assert "unreachable after 1 attempt(s)" in caplog.text


def test_all_cameras_down_reports_every_name(base, caplog):
    with caplog.at_level(logging.ERROR):
        rc, sc = run(base, scenario=Scenario(**{CAM_1: "unreachable", CAM_2: "unreachable", CAM_3: "unreachable"}))
    assert rc == 1
    assert state(base) == {}
    line = [l for l in caplog.text.splitlines() if "still out of sync" in l][0]
    assert "cam_1" in line and "cam_2" in line and "cam_3" in line


# --------------------------------------------------------------------------- config & cli

def test_missing_config_is_a_config_error(tmp_path):
    (tmp_path / ".tapo_credentials").write_text("a\nb\n")
    with pytest.raises(tt.ConfigError, match="config.json not found"):
        tt.run("enable", "all", str(tmp_path), camera_factory=Scenario().factory)


def test_invalid_config_json(tmp_path):
    (tmp_path / "config.json").write_text("{oops")
    (tmp_path / ".tapo_credentials").write_text("a\nb\n")
    with pytest.raises(tt.ConfigError, match="not valid JSON"):
        tt.run("enable", "all", str(tmp_path), camera_factory=Scenario().factory)


def test_camera_without_ip_is_rejected(tmp_path):
    write_config(tmp_path, cameras={"x": {"group": "always"}})
    with pytest.raises(tt.ConfigError, match="needs an 'ip'"):
        tt.load_config(str(tmp_path))


def test_unknown_group_is_rejected(tmp_path):
    write_config(tmp_path, cameras={"x": {"ip": "1.2.3.4", "group": "weekend"}})
    with pytest.raises(tt.ConfigError, match="unknown group"):
        tt.load_config(str(tmp_path))


def test_group_defaults_to_always(tmp_path):
    write_config(tmp_path, cameras={"x": {"ip": "1.2.3.4"}})
    assert tt.load_config(str(tmp_path)) == [tt.Camera("x", "1.2.3.4", night=False)]


def test_missing_or_incomplete_credentials(tmp_path):
    write_config(tmp_path)
    with pytest.raises(tt.ConfigError, match="credentials not found"):
        tt.run("enable", "all", str(tmp_path), camera_factory=Scenario().factory)
    (tmp_path / ".tapo_credentials").write_text("admin\n")
    with pytest.raises(tt.ConfigError, match="username on line 1"):
        tt.run("enable", "all", str(tmp_path), camera_factory=Scenario().factory)


def test_main_exit_codes(base, monkeypatch):
    sc = Scenario(**{CAM_2: "unreachable"})
    monkeypatch.setattr(tt, "default_camera_factory", sc.factory)
    monkeypatch.setattr(tt, "SETTLE_DELAY", 0)
    monkeypatch.setattr(tt, "BASE_DELAY", 0)
    assert tt.main(["disable", "all", "--base-dir", str(base)]) == 1
    assert tt.main(["disable", "all", "--base-dir", str(base)]) == 1  # second camera still down
    sc.steps[CAM_2] = ["ok"]
    assert tt.main(["disable", "all", "--base-dir", str(base)]) == 0
    assert (base / "tapo.log").exists()
    assert state(base) == {CAM_1: "p1a0f0", CAM_2: "p1a0f0", CAM_3: "p1a0f0"}


def test_main_reports_config_errors_with_exit_2(tmp_path):
    assert tt.main(["enable", "all", "--base-dir", str(tmp_path)]) == 2
    assert "config error" in (tmp_path / "tapo.log").read_text()


def test_main_rejects_bad_arguments(base):
    with pytest.raises(SystemExit) as exc:
        tt.main(["toggle", "all", "--base-dir", str(base)])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        tt.main(["enable", "--base-dir", str(base)])


def test_base_dir_from_environment(base, monkeypatch):
    monkeypatch.setenv("TAPO_BASE_DIR", str(base))
    monkeypatch.setattr(tt, "default_camera_factory", Scenario().factory)
    monkeypatch.setattr(tt, "SETTLE_DELAY", 0)
    assert tt.main(["enable", "night"]) == 0
    assert state(base) == {CAM_3: "p0a1f1"}


def test_log_is_trimmed(base):
    log_file = base / "tapo.log"
    log_file.write_text("".join(f"line {i}\n" for i in range(tt.LOG_MAX_LINES + 500)))
    tt.trim_log(str(log_file))
    lines = log_file.read_text().splitlines()
    assert len(lines) == tt.LOG_KEEP_LINES
    assert lines[-1] == f"line {tt.LOG_MAX_LINES + 499}"


# --------------------------------------------------------------------------- night mode flag

from datetime import datetime  # noqa: E402


def test_read_night_mode_formats(base):
    flag = base / ".tapo_night_mode"
    assert tt.read_night_mode(str(base)) is None
    flag.write_text("")
    assert tt.read_night_mode(str(base)) == tt.NightMode(1260, 120, None)
    flag.write_text("1000\n")
    assert tt.read_night_mode(str(base)) == tt.NightMode(1260, 120, 1000)
    flag.write_text("start=20:30\nend=03:15\nexpires=555\n")
    mode = tt.read_night_mode(str(base))
    assert mode == tt.NightMode(20 * 60 + 30, 3 * 60 + 15, 555)
    assert mode.window == "20:30-03:15"
    flag.write_text("start=25:00\nend=xx\n")
    assert tt.read_night_mode(str(base)) == tt.NightMode(1260, 120, None), "garbage falls back to the default"
    flag.write_text("start=21:00\nend=21:00\n")
    assert tt.read_night_mode(str(base)) == tt.NightMode(1260, 120, None), "empty window falls back"


def test_night_rules_apply_follows_flag_window_and_expiry(base):
    flag = base / ".tapo_night_mode"
    at_23 = datetime(2026, 9, 6, 23, 0)
    at_12 = datetime(2026, 9, 6, 12, 0)
    assert tt.night_rules_apply(str(base), at_23) is False, "off without the flag"
    flag.write_text("")
    assert tt.night_rules_apply(str(base), at_23) is True
    assert tt.night_rules_apply(str(base), at_12) is False
    flag.write_text("start=11:00\nend=13:00\n")
    assert tt.night_rules_apply(str(base), at_12) is True
    assert tt.night_rules_apply(str(base), at_23) is False
    flag.write_text(f"start=21:00\nend=02:00\nexpires={int(at_23.timestamp()) - 1}\n")
    assert tt.night_rules_apply(str(base), at_23) is False, "expired"
    flag.write_text(f"start=21:00\nend=02:00\nexpires={int(at_23.timestamp()) + 1}\n")
    assert tt.night_rules_apply(str(base), at_23) is True


def test_run_without_night_mode_treats_night_cameras_normally_at_night(base):
    rc, sc = run(base, "disable", is_night=None, now=datetime(2026, 9, 6, 23, 0))
    assert rc == 0
    assert state(base) == {CAM_1: "p1a0f0", CAM_2: "p1a0f0", CAM_3: "p1a0f0"}


def test_run_with_night_mode_keeps_the_night_camera_watching(base, caplog):
    (base / ".tapo_night_mode").write_text("start=21:00\nend=02:00\n")
    with caplog.at_level(logging.INFO):
        rc, sc = run(base, "disable", is_night=None, now=datetime(2026, 9, 6, 23, 0))
    assert rc == 0
    assert state(base) == {CAM_1: "p1a0f0", CAM_2: "p1a0f0", CAM_3: "p0a0f1"}
    assert "NightRules=on" in caplog.text
    # switching night mode off and re-applying the same state only moves the night camera
    (base / ".tapo_night_mode").unlink()
    rc, sc = run(base, "disable", is_night=None, now=datetime(2026, 9, 6, 23, 0))
    assert rc == 0
    assert set(sc.attempts) == {CAM_3}
    assert state(base)[CAM_3] == "p1a0f0"


def test_night_rules_beat_the_away_state_for_night_cameras(base):
    """Away at night with night mode on: the night camera watches without alarm, the rest arm."""
    (base / ".tapo_night_mode").write_text("start=21:00\nend=02:00\n")
    rc, sc = run(base, "enable", is_night=None, now=datetime(2026, 9, 6, 23, 0))
    assert rc == 0
    assert state(base) == {CAM_1: "p0a1f1", CAM_2: "p0a1f1", CAM_3: "p0a0f1"}
    # when the window closes the same away state arms the night camera too
    rc, sc = run(base, "enable", is_night=None, now=datetime(2026, 9, 7, 2, 0))
    assert rc == 0
    assert set(sc.attempts) == {CAM_3}
    assert state(base)[CAM_3] == "p0a1f1"


def test_run_honours_a_custom_window(base):
    (base / ".tapo_night_mode").write_text("start=19:30\nend=04:00\n")
    rc, _ = run(base, "disable", is_night=None, now=datetime(2026, 9, 6, 19, 45))
    assert rc == 0 and state(base)[CAM_3] == "p0a0f1"
    rc, _ = run(base, "disable", is_night=None, now=datetime(2026, 9, 7, 4, 0))
    assert rc == 0 and state(base)[CAM_3] == "p1a0f0"
