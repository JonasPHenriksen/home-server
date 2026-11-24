"""
phone_probe.py: parallel multi-method presence detection.
"""
import json
import os
import socket
import textwrap
import threading
import time

import pytest

import phone_probe as pp
from conftest import write_config

P1 = pp.Phone("phone_1", "10.0.0.1")
P2 = pp.Phone("phone_2", "10.0.0.2")
P3 = pp.Phone("phone_3", "10.0.0.3")


def cfg(**kw):
    return pp.parse_presence(kw)


class FakeProber:
    """Scripted answers: per (phone name, method) -> bool or (bool, delay seconds)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.cleaned = False
        self.lock = threading.Lock()

    def _answer(self, phone, method):
        with self.lock:
            self.calls.append((phone.name, method, time.monotonic()))
        spec = self.answers.get((phone.name, method), False)
        if isinstance(spec, tuple):
            ok, delay = spec
            time.sleep(delay)
            return ok
        return spec

    def icmp(self, phone):
        return self._answer(phone, "icmp")

    def tcp(self, phone, port):
        return self._answer(phone, f"tcp:{port}")

    def arp(self, phone):
        return self._answer(phone, "arp")

    def cleanup(self):
        self.cleaned = True

    def methods_called(self):
        return {(n, m) for n, m, _ in self.calls}


# --------------------------------------------------------------------------- orchestration

def test_icmp_answer_means_home():
    fake = FakeProber({("phone_1", "icmp"): True})
    r = pp.probe([P1, P2], cfg(), fake)
    assert r.home and r.found == {"phone_1": "icmp"}
    assert fake.cleaned


def test_tcp_refused_or_open_means_home_when_ping_is_ignored():
    fake = FakeProber({("phone_1", "icmp"): False, ("phone_1", "tcp:62078"): True})
    r = pp.probe([P1], cfg(), fake)
    assert r.found == {"phone_1": "tcp:62078"}


def test_arp_is_the_fallback_and_runs_after_the_active_probes():
    fake = FakeProber({("phone_2", "arp"): True})
    r = pp.probe([P1, P2], cfg(), fake)
    assert r.found == {"phone_2": "arp"}
    arp_times = [t for n, m, t in fake.calls if m == "arp"]
    other_times = [t for n, m, t in fake.calls if m != "arp"]
    assert min(arp_times) >= max(other_times)


def test_arp_is_skipped_when_something_already_answered():
    fake = FakeProber({("phone_1", "icmp"): True, ("phone_2", "arp"): True})
    r = pp.probe([P1, P2], cfg(), fake)
    assert r.found == {"phone_1": "icmp"}
    assert ("phone_2", "arp") not in fake.methods_called()


def test_nobody_home():
    fake = FakeProber({})
    r = pp.probe([P1, P2, P3], cfg(), fake)
    assert not r.home and r.found == {}
    assert fake.methods_called() == {(p.name, m) for p in (P1, P2, P3) for m in ("icmp", "tcp:62078", "arp")}
    assert all(v is False for v in r.details.values())


def test_all_phones_and_methods_run_in_parallel():
    slow = (False, 0.4)
    answers = {(p.name, m): slow for p in (P1, P2, P3) for m in ("icmp", "tcp:62078", "tcp:443")}
    fake = FakeProber(answers)
    started = time.monotonic()
    r = pp.probe([P1, P2, P3], cfg(tcp_ports=[62078, 443], timeout_seconds=2), fake)
    elapsed = time.monotonic() - started
    assert not r.home
    assert len(fake.calls) == 9 + 3  # 9 active probes plus 3 arp lookups
    assert elapsed < 0.4 * 3, f"probes did not run in parallel ({elapsed:.2f}s)"


def test_first_answer_wins_without_waiting_for_slow_methods():
    fake = FakeProber({("phone_1", "icmp"): (True, 0.05), ("phone_2", "icmp"): (False, 2.0),
                       ("phone_1", "tcp:62078"): (False, 2.0), ("phone_2", "tcp:62078"): (False, 2.0)})
    started = time.monotonic()
    r = pp.probe([P1, P2], cfg(timeout_seconds=3), fake)
    elapsed = time.monotonic() - started
    assert r.found == {"phone_1": "icmp"}
    assert elapsed < 0.5, f"did not stop at the first hit ({elapsed:.2f}s)"
    assert r.details["phone_2/icmp"] is None, "unfinished probes are reported as such"


def test_overall_timeout_bounds_a_hanging_method():
    fake = FakeProber({("phone_1", "icmp"): (True, 10.0)})  # would answer, but far too late
    started = time.monotonic()
    r = pp.probe([P1], cfg(timeout_seconds=0.3, methods=["icmp"]), fake)
    elapsed = time.monotonic() - started
    assert not r.home
    assert elapsed < 1.5


def test_only_configured_methods_are_used():
    fake = FakeProber({("phone_1", "tcp:62078"): True, ("phone_1", "arp"): True})
    r = pp.probe([P1], cfg(methods=["icmp"]), fake)
    assert not r.home
    assert fake.methods_called() == {("phone_1", "icmp")}


def test_extra_tcp_ports_each_get_a_probe():
    fake = FakeProber({("phone_1", "tcp:5555"): True})
    r = pp.probe([P1], cfg(methods=["tcp"], tcp_ports=[62078, 5555]), fake)
    assert r.found == {"phone_1": "tcp:5555"}


def test_a_crashing_method_counts_as_no_answer():
    class Broken(FakeProber):
        def icmp(self, phone):
            raise RuntimeError("boom")

        def arp(self, phone):
            raise RuntimeError("boom")

    r = pp.probe([P1], cfg(), Broken({("phone_1", "tcp:62078"): False}))
    assert not r.home
    assert r.details["phone_1/icmp"] is False


def test_summary_lines():
    fake = FakeProber({("phone_1", "icmp"): True})
    c = cfg()
    r = pp.probe([P1, P2], c, fake)
    assert pp.summary(r, [P1, P2], c).startswith("home: phone_1 via icmp (")
    r = pp.probe([P1, P2], c, FakeProber({}))
    assert pp.summary(r, [P1, P2], c).startswith("away: no answer from phone_1, phone_2 via icmp,tcp:62078,arp (")


# --------------------------------------------------------------------------- the real methods

def test_tcp_open_port_is_home():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert pp.Prober(cfg(timeout_seconds=1)).tcp(pp.Phone("me", "127.0.0.1"), port) is True
    finally:
        srv.close()


def test_tcp_refused_port_is_home():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()  # nothing listens here any more -> RST
    assert pp.Prober(cfg(timeout_seconds=1)).tcp(pp.Phone("me", "127.0.0.1"), port) is True


def test_tcp_silence_is_away():
    # 10.255.255.1 is a private address nothing on this LAN answers for; the
    # probe must give up after the timeout, not hang
    started = time.monotonic()
    assert pp.Prober(cfg(timeout_seconds=0.3)).tcp(pp.Phone("x", "10.255.255.1"), 62078) is False
    assert time.monotonic() - started < 2


def fake_ping(tmp_path, body):
    p = tmp_path / "ping"
    p.write_text("#!/bin/bash\n" + textwrap.dedent(body))
    p.chmod(0o755)
    return str(p)


def test_icmp_uses_ping_exit_code(tmp_path):
    ok = fake_ping(tmp_path, 'echo "$@" > "%s/ping_args"; exit 0\n' % tmp_path)
    prober = pp.Prober(cfg(ping=ok, timeout_seconds=1.5))
    assert prober.icmp(P1) is True
    assert (tmp_path / "ping_args").read_text().split() == ["-c", "1", "-W", "2", "10.0.0.1"]
    bad = fake_ping(tmp_path, "exit 1\n")
    assert pp.Prober(cfg(ping=bad)).icmp(P1) is False
    assert pp.Prober(cfg(ping="/nonexistent/ping")).icmp(P1) is False


def test_icmp_kills_a_ping_that_outlives_the_timeout(tmp_path):
    slow = fake_ping(tmp_path, "sleep 30\nexit 0\n")
    prober = pp.Prober(cfg(ping=slow, timeout_seconds=0.2))
    started = time.monotonic()
    assert prober.icmp(P1) is False
    assert time.monotonic() - started < 3
    assert all(p.poll() is not None for p in prober._procs)


def test_cleanup_kills_pings_still_running_after_an_early_exit(tmp_path):
    slow = fake_ping(tmp_path, "sleep 30\nexit 0\n")
    prober = pp.Prober(cfg(ping=slow, timeout_seconds=5))
    t = threading.Thread(target=prober.icmp, args=(P1,), daemon=True)
    t.start()
    time.sleep(0.2)
    assert prober._procs and prober._procs[0].poll() is None
    proc = prober._procs[0]
    prober.cleanup()
    t.join(2)
    assert proc.poll() is not None, "ping was not killed"


def test_arp_reads_the_neighbour_table(tmp_path):
    def fake_ip(output):
        p = tmp_path / "ip"
        p.write_text(f"#!/bin/bash\necho '{output}'\n")
        p.chmod(0o755)
        return str(p)

    reachable = fake_ip("10.0.0.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE")
    assert pp.Prober(cfg(ip=reachable)).arp(P1) is True
    stale = fake_ip("10.0.0.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff STALE")
    assert pp.Prober(cfg(ip=stale)).arp(P1) is False
    failed = fake_ip("10.0.0.1 dev eth0 FAILED")
    assert pp.Prober(cfg(ip=failed)).arp(P1) is False
    assert pp.Prober(cfg(ip=fake_ip(""))).arp(P1) is False
    assert pp.Prober(cfg(ip="/nonexistent/ip")).arp(P1) is False


# --------------------------------------------------------------------------- config & cli

def test_load_config(tmp_path):
    write_config(tmp_path, phones={"a": {"ip": "1.1.1.1"}, "b": "2.2.2.2"},
                 presence={"tcp_ports": [80], "timeout_seconds": 2})
    phones, c = pp.load_config(str(tmp_path))
    assert phones == [pp.Phone("a", "1.1.1.1"), pp.Phone("b", "2.2.2.2")]
    assert c.tcp_ports == [80] and c.timeout_seconds == 2.0 and c.methods == ["icmp", "tcp", "arp"]


@pytest.mark.parametrize("phones,presence,match", [
    ({}, {}, "non-empty"),
    ({"a": {}}, {}, "needs an 'ip'"),
    ({"a": {"ip": "1"}}, {"methods": ["sonar"]}, "presence.methods"),
    ({"a": {"ip": "1"}}, {"methods": []}, "presence.methods"),
    ({"a": {"ip": "1"}}, {"tcp_ports": [70000]}, "port numbers"),
    ({"a": {"ip": "1"}}, {"timeout_seconds": 0}, "positive number"),
    ({"a": {"ip": "1"}}, {"bogus": 1}, "unknown presence option"),
])
def test_config_errors(tmp_path, phones, presence, match):
    write_config(tmp_path, phones=phones, presence=presence)
    with pytest.raises(pp.ConfigError, match=match):
        pp.load_config(str(tmp_path))


def test_missing_config_file(tmp_path):
    with pytest.raises(pp.ConfigError, match="not found"):
        pp.load_config(str(tmp_path))


def test_main_exit_codes_and_output(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, phones={"a": {"ip": "1.1.1.1"}})
    monkeypatch.setattr(pp, "Prober", lambda c: FakeProber({("a", "tcp:62078"): True}))
    assert pp.main(["--base-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.startswith("home: a via tcp:62078")

    monkeypatch.setattr(pp, "Prober", lambda c: FakeProber({}))
    assert pp.main(["--base-dir", str(tmp_path), "--json", "--verbose"]) == 1
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert data["home"] is False and data["methods"] == ["icmp", "tcp:62078", "arp"]
    assert "a/icmp: no" in err

    assert pp.main(["--base-dir", str(tmp_path / "nowhere")]) == 2
    assert "config error" in capsys.readouterr().err


def test_main_accepts_explicit_ips_without_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pp, "Prober", lambda c: FakeProber({("9.9.9.9", "icmp"): True}))
    assert pp.main(["--base-dir", str(tmp_path), "9.9.9.9", "8.8.8.8"]) == 0
    assert "home: 9.9.9.9 via icmp" in capsys.readouterr().out


def test_timeout_override(tmp_path, monkeypatch):
    write_config(tmp_path, phones={"a": {"ip": "1.1.1.1"}}, presence={"timeout_seconds": 5})
    seen = {}

    def factory(c):
        seen["timeout"] = c.timeout_seconds
        return FakeProber({})

    monkeypatch.setattr(pp, "Prober", factory)
    pp.main(["--base-dir", str(tmp_path), "--timeout", "0.7"])
    assert seen["timeout"] == 0.7
