"""
ntfy-notify: the push helper, run against a small fake ntfy server on
localhost, so no real notification is ever sent from the tests.
"""
import http.server
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ntfy-notify"

HOST_TAG = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip()


class Capture(http.server.BaseHTTPRequestHandler):
    requests = []
    status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        Capture.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(Capture.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def server():
    Capture.requests = []
    Capture.status = 200
    srv = http.server.HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


# The presence check is run for real, against addresses whose behaviour is
# certain: loopback always answers, and 192.0.2.1 is TEST-NET-1, reserved for
# documentation and routed nowhere, so every method fails on it.
PHONE_HOME = "127.0.0.1"
PHONE_AWAY = "192.0.2.1"


def write_conf(tmp_path, url, topic="alerts", token="tk_test", phone=None, spool=None, name="ntfy-notify.conf"):
    p = tmp_path / name
    text = f'# comment\nNTFY_URL="{url}"\nNTFY_TOPIC = {topic}\nNTFY_TOKEN={token}\n'
    if phone is not None:
        text += f"NTFY_PHONE_IP={phone}\n"
    if spool is not None:
        Path(spool).mkdir(parents=True, exist_ok=True)
        text += f"NTFY_SPOOL_DIR={spool}\n"
    p.write_text(text)
    return p


def held(spool):
    """The held messages, oldest first, each parsed into (headers, body)."""
    out = []
    for f in sorted(Path(spool).glob("*.msg")):
        head, _, body = f.read_text().partition("\n\n")
        out.append((dict(l.split("=", 1) for l in head.splitlines()), body))
    return out


def run(conf, *args, stdin="", **env):
    e = dict(os.environ, NTFY_NOTIFY_CONF=str(conf), **env)
    return subprocess.run(["bash", str(SCRIPT), *args], input=stdin, env=e,
                          capture_output=True, text=True, timeout=60)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_sends_title_priority_tags_and_body(server, tmp_path):
    conf = write_conf(tmp_path, server.url)
    r = run(conf, "-p", "high", "-t", "warning,disk", "Disk trouble", "reallocated=3", "pending=1")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == "", "silent on success"
    [req] = Capture.requests
    assert req["path"] == "/alerts"
    h = req["headers"]
    assert h["Authorization"] == "Bearer tk_test"
    assert h["Title"] == "Disk trouble"
    assert h["Priority"] == "high"
    assert h["Tags"] == f"{HOST_TAG},warning,disk", "host name first, then the caller's tags"
    assert h["Markdown"] == "no"
    assert "Click" not in h
    assert req["body"] == "reallocated=3 pending=1"


def test_defaults_and_stdin_body(server, tmp_path):
    conf = write_conf(tmp_path, server.url)
    r = run(conf, "Just a title", stdin="line one\nline two\n")
    assert r.returncode == 0, r.stderr
    [req] = Capture.requests
    assert req["headers"]["Priority"] == "default"
    assert req["headers"]["Tags"] == HOST_TAG
    assert req["body"] == "line one\nline two"


def test_dash_reads_stdin_and_topic_and_click_can_be_set(server, tmp_path):
    conf = write_conf(tmp_path, server.url)
    r = run(conf, "-T", "other", "-c", "http://example.test/x", "T", "-", stdin="from stdin")
    assert r.returncode == 0, r.stderr
    [req] = Capture.requests
    assert req["path"] == "/other"
    assert req["headers"]["Click"] == "http://example.test/x"
    assert req["body"] == "from stdin"


def test_server_refusal_is_reported_with_exit_2(server, tmp_path):
    Capture.status = 403
    conf = write_conf(tmp_path, server.url)
    r = run(conf, "T", "m")
    assert r.returncode == 2
    assert "answered HTTP 403" in r.stderr


def test_unreachable_server_fails_without_hanging(tmp_path):
    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    conf = write_conf(tmp_path, f"http://127.0.0.1:{port}")
    r = run(conf, "T", "m")
    assert r.returncode == 2
    assert "HTTP 000" in r.stderr


@pytest.mark.parametrize("args, expected", [
    ((), "a title is required"),
    (("-p", "loud", "T", "m"), "bad priority"),
    (("-x", "T"), "unknown option"),
])
def test_usage_errors_exit_1(server, tmp_path, args, expected):
    conf = write_conf(tmp_path, server.url)
    r = run(conf, *args)
    assert r.returncode == 1
    assert expected in r.stderr
    assert Capture.requests == []


def test_missing_or_incomplete_config_exits_1(server, tmp_path):
    r = run(tmp_path / "nope.conf", "T", "m")
    assert r.returncode == 1 and "cannot read" in r.stderr
    conf = tmp_path / "half.conf"
    conf.write_text("NTFY_URL=http://127.0.0.1:1\n")
    r = run(conf, "T", "m")
    assert r.returncode == 1 and "must all be set" in r.stderr
    assert Capture.requests == []


def test_config_is_read_not_sourced(server, tmp_path):
    conf = tmp_path / "evil.conf"
    marker = tmp_path / "executed"
    conf.write_text(f"NTFY_URL={server.url}\nNTFY_TOPIC=alerts\nNTFY_TOKEN=tk_x\n$(touch {marker})\n`touch {marker}`\n")
    r = run(conf, "T", "m")
    assert r.returncode == 0, r.stderr
    assert not marker.exists(), "a config file must never run code"


def test_dry_run_prints_and_sends_nothing(server, tmp_path):
    conf = write_conf(tmp_path, server.url)
    r = run(conf, "-p", "urgent", "-t", "rotating_light", "Title here", "body here", NTFY_NOTIFY_DRY_RUN="1")
    assert r.returncode == 0
    assert f"POST {server.url}/alerts priority=urgent tags={HOST_TAG},rotating_light title=Title here" in r.stdout
    assert "body here" in r.stdout
    assert Capture.requests == []


# --------------------------------------------------------------------------- holding until the phone is home

def test_a_message_is_held_while_the_phone_is_away(server, tmp_path):
    spool = tmp_path / "spool"
    conf = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool)
    r = run(conf, "-p", "high", "-t", "warning", "-c", "http://x.test/", "Disk trouble", "reallocated=3")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == "", "holding is silent too"
    assert Capture.requests == [], "nothing is published while the phone is away"
    [(h, body)] = held(spool)
    assert h["priority"] == "high"
    assert h["tags"] == f"{HOST_TAG},warning"
    assert h["title"] == "Disk trouble"
    assert h["click"] == "http://x.test/"
    assert h["topic"] == "alerts"
    assert int(h["created"]) > 0
    assert body == "reallocated=3"


def test_a_message_is_sent_at_once_while_the_phone_is_home(server, tmp_path):
    spool = tmp_path / "spool"
    conf = write_conf(tmp_path, server.url, phone=PHONE_HOME, spool=spool)
    r = run(conf, "All good", "nothing to report")
    assert r.returncode == 0, r.stderr
    assert len(Capture.requests) == 1
    assert Capture.requests[0]["body"] == "nothing to report"
    assert held(spool) == [], "nothing is held when it could be sent"


def test_flush_sends_held_messages_oldest_first_and_empties_the_spool(server, tmp_path):
    spool = tmp_path / "spool"
    away = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool, name="away.conf")
    home = write_conf(tmp_path, server.url, phone=PHONE_HOME, spool=spool, name="home.conf")
    assert run(away, "-p", "urgent", "First", "one").returncode == 0
    assert run(away, "Second", "two").returncode == 0
    assert len(held(spool)) == 2
    assert Capture.requests == []

    r = run(home, "--flush")
    assert r.returncode == 0, r.stderr
    assert "sent 2 held message(s)" in r.stdout
    assert held(spool) == [], "the spool is emptied"
    assert [q["headers"]["Title"] for q in Capture.requests] == ["First", "Second"]
    assert Capture.requests[0]["headers"]["Priority"] == "urgent"
    first = Capture.requests[0]["body"]
    assert first.startswith("one\n\n(raised "), first
    assert first.rstrip().endswith(")")


def test_flush_holds_everything_back_while_the_phone_is_still_away(server, tmp_path):
    spool = tmp_path / "spool"
    away = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool)
    run(away, "Queued", "body")
    r = run(away, "--flush")
    assert r.returncode == 0 and r.stdout == ""
    assert Capture.requests == []
    assert len(held(spool)) == 1, "still waiting"


def test_flush_keeps_messages_the_server_could_not_take_yet(server, tmp_path):
    spool = tmp_path / "spool"
    away = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool, name="away.conf")
    home = write_conf(tmp_path, server.url, phone=PHONE_HOME, spool=spool, name="home.conf")
    run(away, "One", "a")
    run(away, "Two", "b")
    Capture.status = 503
    r = run(home, "--flush")
    assert r.returncode == 0
    assert len(held(spool)) == 2, "a 503 leaves everything queued for the next run"
    # curl retries a 5xx twice, so one message means several requests; what
    # matters is that the second message was never attempted
    assert {q["headers"]["Title"] for q in Capture.requests} == {"One"}, "stops after the first failure"

    Capture.status = 200
    r = run(home, "--flush")
    assert "sent 2 held message(s)" in r.stdout
    assert held(spool) == []


def test_flush_drops_a_message_the_server_refuses_for_good(server, tmp_path):
    spool = tmp_path / "spool"
    away = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool, name="away.conf")
    home = write_conf(tmp_path, server.url, phone=PHONE_HOME, spool=spool, name="home.conf")
    run(away, "Bad token", "a")
    Capture.status = 403
    r = run(home, "--flush")
    assert "dropped 1 held message(s)" in r.stdout
    assert held(spool) == [], "a 4xx is final, retrying it forever would be pointless"


def test_flush_drops_messages_older_than_a_week(server, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    home = write_conf(tmp_path, server.url, phone=PHONE_HOME, spool=spool)
    stale = int(time.time()) - 8 * 86400
    fresh = int(time.time()) - 60
    (spool / f"{stale}000000000-1.msg").write_text(
        f"priority=high\ntags=x\ntitle=Ancient\nclick=\ntopic=alerts\ncreated={stale}\n\nold news")
    (spool / f"{fresh}000000000-1.msg").write_text(
        f"priority=high\ntags=x\ntitle=Recent\nclick=\ntopic=alerts\ncreated={fresh}\n\nstill relevant")
    r = run(home, "--flush")
    assert "sent 1 held message(s)" in r.stdout
    assert "dropped 1 held message(s)" in r.stdout
    assert [q["headers"]["Title"] for q in Capture.requests] == ["Recent"]
    assert held(spool) == []


def test_a_forged_created_field_is_never_run_and_expires_unsent(server, tmp_path):
    """Anyone in group dietpi can write the spool. created= goes into $(( )), where
    bash would run PATH[$(cmd)] (PATH: under set -u an unset name stops bash first)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    home = write_conf(tmp_path, server.url, phone=PHONE_HOME, spool=spool)
    marker = tmp_path / "ran"
    (spool / "1000000000000000000-1.msg").write_text(
        f"priority=high\ntags=x\ntitle=Forged\nclick=\ntopic=alerts\ncreated=PATH[$(touch {marker})]\n\nbody")
    r = run(home, "--flush")
    assert not marker.exists(), "the created field was run as code"
    assert r.returncode == 0 and "dropped 1 held message(s)" in r.stdout
    assert Capture.requests == [] and held(spool) == []


def test_flush_never_writes_through_a_planted_lock_symlink(server, tmp_path):
    """The old lock was a file in the spool, opened with >, so a symlink there
    made the flusher truncate whatever it pointed at."""
    spool = tmp_path / "spool"
    away = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool, name="away.conf")
    home = write_conf(tmp_path, server.url, phone=PHONE_HOME, spool=spool, name="home.conf")
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    (spool / ".flush.lock").symlink_to(victim)
    run(away, "Queued", "body")
    r = run(home, "--flush")
    assert "sent 1 held message(s)" in r.stdout
    assert victim.read_text() == "precious\n"


def test_a_flush_already_running_is_left_alone(server, tmp_path):
    spool = tmp_path / "spool"
    away = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool, name="away.conf")
    home = write_conf(tmp_path, server.url, phone=PHONE_HOME, spool=spool, name="home.conf")
    run(away, "Queued", "body")
    holder = subprocess.Popen(["bash", "-c", 'exec 7<"$1"; flock 7; sleep 30', "_", str(spool)])
    try:
        time.sleep(0.3)
        r = run(home, "--flush")
        assert r.returncode == 0 and r.stdout == ""
        assert Capture.requests == [] and len(held(spool)) == 1
    finally:
        holder.kill()
        holder.wait()
    assert "sent 1 held message(s)" in run(home, "--flush").stdout


def test_held_files_are_readable_by_the_dietpi_flusher_whatever_the_umask(server, tmp_path):
    spool = tmp_path / "spool"
    away = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool)
    e = dict(os.environ, NTFY_NOTIFY_CONF=str(away))
    subprocess.run(["bash", "-c", 'umask 077; exec bash "$0" Queued body', str(SCRIPT)], env=e, check=True, timeout=60)
    (f,) = spool.glob("*.msg")
    assert f.stat().st_mode & 0o777 == 0o644


def test_the_flush_timer_runs_as_dietpi_not_root():
    unit = (ROOT / "ntfy-spool-flush.service").read_text()
    assert "\nUser=dietpi\n" in unit and "\nGroup=dietpi\n" in unit


def test_flush_on_an_empty_spool_does_nothing_at_all(server, tmp_path):
    spool = tmp_path / "spool"
    conf = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool)
    r = run(conf, "--flush")
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""
    assert Capture.requests == []


def test_nothing_is_held_when_no_phone_is_configured(server, tmp_path):
    """The backup server's case: awake once a week, so holding would mean a week's delay."""
    spool = tmp_path / "spool"
    conf = write_conf(tmp_path, server.url, phone="", spool=spool)
    r = run(conf, "Weekly result", "all good")
    assert r.returncode == 0, r.stderr
    assert len(Capture.requests) == 1
    assert held(spool) == []


def test_nothing_is_held_when_the_spool_directory_is_missing(server, tmp_path):
    """Failsafe: an uninstalled spool must never swallow a notification."""
    conf = write_conf(tmp_path, server.url, phone=PHONE_AWAY)
    conf.write_text(conf.read_text() + f"NTFY_SPOOL_DIR={tmp_path / 'absent'}\n")
    r = run(conf, "Important", "body")
    assert r.returncode == 0, r.stderr
    assert len(Capture.requests) == 1, "sent immediately rather than lost"


def test_a_send_that_fails_now_is_held_for_the_next_flush(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    spool = tmp_path / "spool"
    conf = write_conf(tmp_path, f"http://127.0.0.1:{port}", phone=PHONE_HOME, spool=spool)
    r = run(conf, "Server was down", "body")
    assert r.returncode == 0
    assert "held it for the next flush" in r.stderr
    [(h, body)] = held(spool)
    assert h["title"] == "Server was down" and body == "body"


def test_status_reports_the_queue_and_the_phone(server, tmp_path):
    spool = tmp_path / "spool"
    away = write_conf(tmp_path, server.url, phone=PHONE_AWAY, spool=spool, name="away.conf")
    r = run(away, "--status")
    assert "nothing held" in r.stdout
    assert f"phone {PHONE_AWAY}: away" in r.stdout
    run(away, "Queued", "x")
    r = run(away, "--status")
    assert "1 message(s) held since" in r.stdout
    home = write_conf(tmp_path, server.url, phone=PHONE_HOME, spool=spool, name="home.conf")
    assert f"phone {PHONE_HOME}: home" in run(home, "--status").stdout


# --------------------------------------------------------------------------- the neighbour-table state machine

def fake_net(tmp_path, states):
    """A PATH where `ping` always fails and `ip neigh` returns `states` in turn.

    The last state repeats once the list runs out. Returns (PATH, ping_calls_file).
    Used with a phone address whose port 62078 refuses instantly, so the only
    thing left deciding the answer is the neighbour table.
    """
    binp = tmp_path / "fakebin"; binp.mkdir(exist_ok=True)
    calls = tmp_path / "ping_calls"
    (binp / "ping").write_text("#!/bin/bash\n" + f'echo called >> "{calls}"\n' + "exit 1\n")
    (binp / "ping").chmod(0o755)
    (tmp_path / "states").write_text("\n".join(states) + "\n")
    (binp / "ip").write_text(
        "#!/bin/bash\n"
        + f'n=$(cat "{tmp_path}/ip_calls" 2>/dev/null || echo 0)\n'
        + f'echo $((n + 1)) > "{tmp_path}/ip_calls"\n'
        + f'sed -n "$((n + 1))p" "{tmp_path}/states" | grep . || tail -1 "{tmp_path}/states"\n')
    (binp / "ip").chmod(0o755)
    return f"{binp}:{os.environ['PATH']}", calls


def presence(tmp_path, states, arp_wait="2"):
    """What --status says about the phone, given those neighbour states."""
    path, calls = fake_net(tmp_path, states)
    conf = write_conf(tmp_path, "http://127.0.0.1:1", phone="127.0.0.1", spool=tmp_path / "spool")
    r = run(conf, "--status", PATH=path, NTFY_NOTIFY_ARP_WAIT=arp_wait)
    assert r.returncode == 0, r.stderr
    return ("home" if ": home" in r.stdout else "away"), calls


REACHABLE = "192.168.50.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
STALE = "192.168.50.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff STALE"


def test_a_confirmed_neighbour_is_taken_without_sending_anything(tmp_path):
    verdict, ping_calls = presence(tmp_path, [REACHABLE])
    assert verdict == "home"
    assert not ping_calls.exists(), "a confirmed entry needs no probing at all"


def test_an_unconfirmed_neighbour_that_resolves_counts_as_home(tmp_path):
    """STALE means the address is known but unconfirmed; the kernel is given time to decide."""
    verdict, ping_calls = presence(tmp_path, [
        STALE,
        "192.168.50.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff DELAY",
        "192.168.50.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff PROBE",
        REACHABLE,
    ], arp_wait="5")
    assert verdict == "home"
    assert ping_calls.exists(), "it did try to provoke an answer first"


def test_an_unconfirmed_neighbour_that_never_resolves_counts_as_away(tmp_path):
    verdict, _ = presence(tmp_path, [STALE], arp_wait="2")
    assert verdict == "away"


@pytest.mark.parametrize("state, why", [
    ("192.168.50.1 dev eth0 FAILED", "a failed entry is a definite no"),
    ("192.168.50.1 dev eth0 INCOMPLETE", "never resolved"),
    ("", "no entry at all"),
])
def test_a_phone_that_is_really_gone_is_reported_away_at_once(tmp_path, state, why):
    start = time.time()
    verdict, _ = presence(tmp_path, [state], arp_wait="30")
    assert verdict == "away", why
    assert time.time() - start < 20, "it must not sit through the whole wait for a definite answer"
