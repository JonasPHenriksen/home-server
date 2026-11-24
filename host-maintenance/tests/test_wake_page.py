"""
wake-page: the HTTPS page that shows whether the home's computers are on,
wakes them, and keeps their list. The server runs for real, over TLS with
certificates made by home-certs, with fake ping, ip and wakeonlan standing in
for the network.
"""
import http.client
import http.server
import importlib.machinery
import importlib.util
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "wake-page"
PASSWORD = "correct horse battery"


def load():
    loader = importlib.machinery.SourceFileLoader("wake_page", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("wake_page", loader))
    loader.exec_module(module)
    return module


def fake_network(tmp):
    """ping answers for addresses in up/. ip reports the ARP states listed in
    arp/<address>, one per call and the last one for good, each maybe with a
    MAC after it; an address without a file has FAILED, as after an unanswered
    probe. wakeonlan writes to woken, ssh to ssh_calls."""
    bin_ = tmp / "bin"
    bin_.mkdir()
    (tmp / "up").mkdir()
    (tmp / "arp").mkdir()
    (bin_ / "ping").write_text(f'#!/bin/bash\n[ -e "{tmp}/up/${{@: -1}}" ]\n')
    (bin_ / "ip").write_text(
        f'#!/bin/bash\nf="{tmp}/arp/${{@: -1}}"\n'
        '[ -e "$f" ] || { printf \'[{"dst":"%s","dev":"eth0","state":["FAILED"]}]\\n\' "${@: -1}"; exit 0; }\n'
        'line=$(head -1 "$f"); [ "$(wc -l < "$f")" -gt 1 ] && sed -i 1d "$f"\n'
        'read -r s mac <<< "$line"\n'
        'if [ -n "$mac" ]; then printf \'[{"dst":"%s","dev":"eth0","lladdr":"%s","state":["%s"]}]\\n\' "${@: -1}" "$mac" "$s"\n'
        'else printf \'[{"dst":"%s","dev":"eth0","state":["%s"]}]\\n\' "${@: -1}" "$s"; fi\n')
    (bin_ / "wakeonlan").write_text(f'#!/bin/bash\necho "$@" >> "{tmp}/woken"\n')
    # ssh: records its arguments; says ssh_err on stderr and exits with ssh_rc, if those exist
    (bin_ / "ssh").write_text(f'#!/bin/bash\necho "$@" >> "{tmp}/ssh_calls"\n'
                              f'[ -e "{tmp}/ssh_err" ] && cat "{tmp}/ssh_err" >&2\n'
                              f'exit $(cat "{tmp}/ssh_rc" 2>/dev/null || echo 0)\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    return f"{bin_}:{os.environ['PATH']}"


@pytest.fixture
def env(tmp_path):
    return dict(os.environ, WAKE_PAGE_DIR=str(tmp_path / "conf"), HOME_TLS_DIR=str(tmp_path / "tls"),
                PATH=fake_network(tmp_path))


def form(**fields):
    return urllib.parse.urlencode(fields)


def state(watcher, name):
    return watcher.snapshot()[1][name]


def wake_page(env, *args, stdin=""):
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, input=stdin,
                          capture_output=True, text=True, timeout=60)


class Client:
    """Talks to the page over TLS, trusting only its own CA, as a phone would."""

    def __init__(self, port, ca):
        self.port, self.cookie = port, None
        self.tls = ssl.create_default_context(cafile=str(ca))

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=10)
        # the certificate is for the laptop's names, so check it against one of them
        conn.sock = self.tls.wrap_socket(socket.create_connection(("127.0.0.1", self.port), timeout=10),
                                         server_hostname="dietpi-laptop")
        headers = dict(headers or {})
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookie:
            headers["Cookie"] = self.cookie
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        r.text = r.read().decode()
        conn.close()
        return r

    def login(self, password=PASSWORD):
        r = self.request("POST", "/login", "password=" + urllib.parse.quote(password))
        if r.status == 303:
            self.cookie = r.getheader("Set-Cookie").split(";")[0]
        return r

    def wait_for(self, *texts):
        deadline = time.time() + 10
        while True:
            page = self.request("GET", "/").text
            if all(t in page for t in texts) or time.time() > deadline:
                return page
            time.sleep(0.1)



def run_server(tmp_path, env, machines=None):
    """Start the page for real; machines, when given, is written as its list first."""
    assert subprocess.run([str(ROOT / "home-certs")], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True).returncode == 0
    assert wake_page(env, "--set-password", stdin=PASSWORD + "\n").returncode == 0
    if machines is not None:
        (tmp_path / "conf" / "machines.json").write_text(json.dumps(machines))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env.update(WAKE_PAGE_PORT=str(port), WAKE_PAGE_BIND="127.0.0.1", WAKE_PAGE_CHECK_SECS="0.1")
    with open(tmp_path / "log", "w") as log:
        proc = subprocess.Popen([sys.executable, str(SCRIPT)], env=env, stderr=log)
    deadline = time.time() + 10
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            assert time.time() < deadline and proc.poll() is None, (tmp_path / "log").read_text()
            time.sleep(0.05)
    yield Client(port, tmp_path / "tls" / "ca.crt"), tmp_path
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def server(tmp_path, env):
    yield from run_server(tmp_path, env)


@pytest.fixture
def desktop_server(tmp_path, env):
    """The desktop without its site checks, so nothing reaches out to the real one, and the PC."""
    machines = json.loads(json.dumps(load().DEFAULT_MACHINES))
    del machines["desktop"]["services"]
    yield from run_server(tmp_path, env, machines)


# --- setting it up ---------------------------------------------------------

def test_only_a_hash_of_the_password_is_kept(env, tmp_path):
    assert wake_page(env, "--set-password", stdin=PASSWORD + "\n").returncode == 0
    f = tmp_path / "conf" / "password"
    text = f.read_text()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert text.startswith("scrypt$") and PASSWORD not in text
    assert wake_page(env, "--set-password", stdin="short\n").returncode != 0
    assert f.read_text() == text, "a refused password changes nothing"


def test_it_will_not_start_half_set_up(env):
    r = wake_page(env)
    assert r.returncode == 2 and "laptop.crt" in r.stderr and "password" in r.stderr


# --- telling what is on ----------------------------------------------------

def test_the_desktop_goes_by_ping_the_pc_by_arp(tmp_path, monkeypatch):
    wp = load()
    monkeypatch.setenv("PATH", fake_network(tmp_path))
    monkeypatch.setattr(wp.time, "sleep", lambda s: None)
    up, arp = tmp_path / "up", tmp_path / "arp"
    (up / "192.168.50.20").touch()
    assert wp.is_on("192.168.50.20", "ping") is True
    (up / "192.168.50.20").unlink()
    (arp / "192.168.50.20").write_text("REACHABLE\n")
    assert wp.is_on("192.168.50.20", "ping") is False, "a machine that answers ping goes by ping alone"
    pc = arp / "192.168.50.21"
    pc.write_text("REACHABLE\n")
    assert wp.is_on("192.168.50.21", "arp") is True
    pc.write_text("FAILED\n")
    assert wp.is_on("192.168.50.21", "arp") is False
    pc.write_text("DELAY\nPROBE\nREACHABLE\n")
    assert wp.is_on("192.168.50.21", "arp") is True, "waits for the kernel's verdict"
    pc.write_text("DELAY\n")
    assert wp.is_on("192.168.50.21", "arp") is None, "no verdict, no answer"
    (up / "192.168.50.21").touch()
    assert wp.is_on("192.168.50.21", "arp") is True, "answering ping counts too"


def test_on_for_counts_from_the_first_check_that_saw_it(tmp_path):
    wp = load()
    w = wp.Watcher(tmp_path)
    t = 1_000_000.0
    w.record("desktop", True, t)                 # just started: on, but since when?
    assert wp.describe(state(w, "desktop"), t + 30) == "On"
    assert wp.describe(state(w, "desktop"), t + 300) == "On for at least 5 min"
    w.record("desktop", None, t + 15)            # a check that could not tell changes nothing
    w.record("desktop", False, t + 30)           # one miss is not enough
    assert state(w, "desktop")["on"] is True
    w.record("desktop", False, t + 45)           # two in a row: off since the first of them
    s = state(w, "desktop")
    assert (s["on"], s["since"]) == (False, t + 30)
    assert wp.describe(s, t + 30 + 123 * 60) == "Off for 2 h 3 min"
    w.record("desktop", True, t + 100)
    assert wp.describe(state(w, "desktop"), t + 100 + 26 * 3600) == "On for 1 day 2 h"
    assert wp.describe(state(w, "desktop"), t + 100 + 20) == "On for less than a minute"


def test_a_quick_restart_keeps_the_times_a_long_break_forgets_them(tmp_path):
    wp = load()
    now = time.time()
    w = wp.Watcher(tmp_path)
    w.state["desktop"].update(on=True, since=now - 3600, exact=True, wake=now - 4000)
    w.save(now - 60)                              # say, the weekly reboot
    assert wp.describe(state(wp.Watcher(tmp_path), "desktop"), now) == "On for 1 h"
    w.save(now - 3600)                            # the laptop was away for an hour
    s = state(wp.Watcher(tmp_path), "desktop")
    assert (s["on"], s["since"], s["wake"]) == (None, None, now - 4000)


def test_what_the_page_says_after_a_wake_up():
    wp = load()
    off = {"on": False, "since": 0.0, "exact": True, "wake": 1000.0}
    assert wp.wake_note(off, 1030) == "Wake-up sent less than a minute ago, waiting for it to answer."
    assert wp.wake_note(off, 1300) == "Still no answer 5 min after the wake-up."
    assert wp.wake_note(off, 2200) == "", "old news after 10 minutes"
    assert wp.wake_note(dict(off, on=True), 1030) == "", "nothing to add once it answers"


class FakeService(http.server.BaseHTTPRequestHandler):
    """/up answers 200, /maintenance is Nextcloud updating, /broken is a 500."""

    def do_GET(self):
        status, body = {"/up": (200, b'{"status":true}'),
                        "/maintenance": (200, b'{"installed":true,"maintenance":true}'),
                        "/page": (200, b"<html>not json</html>")}.get(self.path, (500, b"no"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_whether_the_desktops_sites_answer():
    wp = load()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert wp.service_up(base + "/up") is True
        assert wp.service_up(base + "/page") is True, "answering is enough when it is not JSON"
        assert wp.service_up(base + "/maintenance") is False, "Nextcloud in maintenance is not ready"
        assert wp.service_up(base + "/broken") is False
    finally:
        srv.shutdown()
        srv.server_close()
    with socket.socket() as s:                       # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    assert wp.service_up(f"http://127.0.0.1:{closed}/") is False


def test_the_page_shows_the_sites_only_while_the_desktop_is_on():
    wp = load()
    on = {"on": True, "since": 0.0, "exact": True, "wake": None,
          "services": {"Nextcloud": True, "Open WebUI": False}}
    states = {"desktop": on, "pc": {"on": None, "since": None, "exact": False, "wake": None, "services": {}}}
    page = wp.main_page(wp.DEFAULT_MACHINES, states, 3600.0, None).decode()
    assert '<span class="ok">✓ Nextcloud</span><span class="bad">✗ Open WebUI</span>' in page
    states["desktop"] = dict(on, on=False)
    assert "Nextcloud" not in wp.main_page(wp.DEFAULT_MACHINES, states, 3600.0, None).decode()


def test_the_login_cookie():
    wp = load()
    secret, stored, now = b"k" * 32, wp.hash_password(PASSWORD), 1_000_000
    assert wp.password_ok(PASSWORD, stored) and not wp.password_ok("wrong", stored)
    c = wp.make_cookie(secret, stored, now)
    assert wp.cookie_ok(c, secret, stored, now + 29 * 86400)
    assert not wp.cookie_ok(c, secret, stored, now + 31 * 86400), "a login lasts 30 days"
    assert not wp.cookie_ok(c[:-1] + ("0" if c[-1] != "0" else "1"), secret, stored, now)
    assert not wp.cookie_ok(c, secret, wp.hash_password("another password"), now), "a new password logs all out"
    assert not wp.cookie_ok("9999999999.abc", secret, stored, now)
    assert not wp.cookie_ok("²³.x", secret, stored, now)


# --- the page, for real ----------------------------------------------------

def test_nothing_without_the_password(server):
    c, tmp = server
    r = c.request("GET", "/")
    assert r.status == 303 and r.getheader("Location") == "/login"
    r = c.request("POST", "/wake/desktop", "")
    assert r.status == 303 and r.getheader("Location") == "/login"
    assert not (tmp / "woken").exists()
    r = c.request("GET", "/login")
    assert r.status == 200 and 'type="password"' in r.text
    r = c.request("GET", "/ca.crt")          # public: a new device needs it before it trusts the page
    assert r.status == 200 and "BEGIN CERTIFICATE" in r.text
    r = c.login("wrong password")
    assert r.status == 403 and r.getheader("Set-Cookie") is None and c.cookie is None
    assert "wrong password from 127.0.0.1" in (tmp / "log").read_text()


def test_log_in_see_and_wake(server):
    c, tmp = server
    (tmp / "arp" / "192.168.50.21").write_text("REACHABLE\n")   # the PC answers ARP: on
    r = c.login()
    assert r.status == 303 and r.getheader("Location") == "/"
    for attribute in ("Secure", "HttpOnly", "SameSite=Lax", "Max-Age=2592000"):
        assert attribute in r.getheader("Set-Cookie")
    page = c.wait_for('<span class="chip on"><i></i>On</span>', '<span class="chip"><i></i>Off</span>')
    assert "<b>Desktop</b>" in page and "<b>PC</b>" in page
    r = c.request("POST", "/wake/desktop", "")
    assert r.status == 303 and r.getheader("Location") == "/"
    assert (tmp / "woken").read_text() == "-i 192.168.50.255 02:00:00:00:00:20\n"
    r = c.request("GET", "/")
    assert "Wake-up sent less than a minute ago" in r.text
    assert "no-store" in r.getheader("Cache-Control")
    assert "frame-ancestors 'none'" in r.getheader("Content-Security-Policy")
    assert c.request("POST", "/wake/router", "").status == 404
    assert (tmp / "woken").read_text().count("\n") == 1, "only the machines it knows"
    assert "wake-up sent to Desktop" in (tmp / "log").read_text()


def test_a_failed_wake_up_says_so(server):
    c, tmp = server
    (tmp / "bin" / "wakeonlan").write_text("#!/bin/bash\nexit 1\n")
    c.login()
    r = c.request("POST", "/wake/desktop", "")
    assert r.status == 500 and "Could not send the wake-up to Desktop." in r.text


def test_a_new_password_logs_everyone_out(server, env):
    c, _ = server
    c.login()
    assert c.request("GET", "/").status == 200
    assert wake_page(env, "--set-password", stdin="a different password\n").returncode == 0
    r = c.request("GET", "/")
    assert r.status == 303 and r.getheader("Location") == "/login"
    assert c.login("a different password").status == 303


def test_log_out(server):
    c, _ = server
    c.login()
    r = c.request("POST", "/logout", "")
    assert r.status == 303 and "Max-Age=0" in r.getheader("Set-Cookie")


def test_plain_http_is_dropped_and_the_page_carries_on(server):
    c, _ = server
    with socket.create_connection(("127.0.0.1", c.port), timeout=5) as s:
        s.sendall(b"GET / HTTP/1.0\r\n\r\n")
        try:
            answer = s.recv(100)
        except ConnectionResetError:
            answer = b""
        assert not answer.startswith(b"HTTP"), "no page over plain HTTP"
    assert c.request("GET", "/login").status == 200


# --- the list of devices ---------------------------------------------------

def test_a_devices_fields_are_checked_and_tidied():
    wp = load()
    assert wp.clean_device("  Office \n PC ", " 192.168.50.50", "aa-bb-cc-dd-ee-f0", "ping") == \
        ("Office PC", "192.168.50.50", "AA:BB:CC:DD:EE:F0", "ping")
    assert wp.clean_device("TV", "192.168.50.51", "a0b1c2d3e4f5", "arp")[2] == "A0:B1:C2:D3:E4:F5"
    assert wp.clean_device("TV", "192.168.50.51", "", "arp")[2] == "", "an empty MAC is looked up later"
    for fields, why in [(("", "192.168.50.50", "", "ping"), "name"),
                        (("x" * 41, "192.168.50.50", "", "ping"), "name"),
                        (("TV", "192.168.50.300", "", "ping"), "should look like"),
                        (("TV", "10.0.0.5", "", "ping"), "home network"),
                        (("TV", "192.168.50.255", "", "ping"), "home network"),
                        (("TV", "192.168.50.50", "aa:bb:cc", "ping"), "MAC address should"),
                        (("TV", "192.168.50.50", "01:00:5E:00:00:01", "ping"), "group of devices"),
                        (("TV", "192.168.50.50", "", "telnet"), "ping or ARP")]:
        with pytest.raises(ValueError, match=why):
            wp.clean_device(*fields)
    assert wp.slug("Office PC", {}) == "office-pc"
    assert wp.slug("Office PC!", {"office-pc": 1}) == "office-pc-2"
    assert wp.slug("💻", {}) == "device", "a name with no letters a-z still gets an address"


def test_the_first_start_writes_the_list_and_a_broken_one_is_left_alone(tmp_path, env):
    wp = load()
    wp.Watcher(tmp_path)
    saved = json.loads((tmp_path / "machines.json").read_text())
    assert saved == wp.DEFAULT_MACHINES
    assert stat.S_IMODE((tmp_path / "machines.json").stat().st_mode) == 0o600
    # a broken list stops the page, with a reason, instead of being replaced
    assert subprocess.run([str(ROOT / "home-certs")], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True).returncode == 0
    assert wake_page(env, "--set-password", stdin=PASSWORD + "\n").returncode == 0
    broken = Path(env["WAKE_PAGE_DIR"]) / "machines.json"
    broken.write_text('{"desktop": {"label": "Desktop"}}')
    r = wake_page(env)
    assert r.returncode == 2 and "machines.json" in r.stderr and "desktop" in r.stderr
    assert broken.read_text() == '{"desktop": {"label": "Desktop"}}'


def test_a_removed_device_is_no_longer_checked(tmp_path):
    wp = load()
    w = wp.Watcher(tmp_path)
    token = object()
    w.threads["pc"] = token
    assert w.remove("pc")["label"] == "PC"
    assert "pc" not in json.loads((tmp_path / "machines.json").read_text())
    w.record("pc", True, token=token)                    # a check that was still running
    w.record_services("pc", {"x": True}, token)
    assert "pc" not in w.snapshot()[1] and w.remove("pc") is None


def test_add_wake_and_remove_a_device(server):
    c, tmp = server
    c.login()
    assert 'name="mac"' in c.request("GET", "/add").text
    r = c.request("POST", "/add", form(label="Office PC", ip="192.168.50.50", mac="aa-bb-cc-dd-ee-f0", check="ping"))
    assert r.status == 303 and r.getheader("Location") == "/"
    saved = json.loads((tmp / "conf" / "machines.json").read_text())
    assert saved["office-pc"] == {"label": "Office PC", "ip": "192.168.50.50", "mac": "AA:BB:CC:DD:EE:F0",
                                  "check": "ping"}
    assert list(saved) == ["desktop", "pc", "office-pc"], "new ones go at the end"
    page = c.wait_for("<b>Office PC</b>")
    assert 'href="/remove/office-pc"' in page and 'action="/wake/office-pc"' in page
    assert c.request("POST", "/wake/office-pc", "").status == 303
    assert (tmp / "woken").read_text() == "-i 192.168.50.255 AA:BB:CC:DD:EE:F0\n"
    # what the form refuses, with the values kept for another try
    r = c.request("POST", "/add", form(label="Other", ip="192.168.50.51", mac="AA:BB:CC:DD:EE:F0", check="ping"))
    assert r.status == 400 and "Office PC has that MAC address already." in r.text and 'value="Other"' in r.text
    r = c.request("POST", "/add", form(label="office pc", ip="192.168.50.52", mac="AA:BB:CC:DD:EE:F1", check="ping"))
    assert r.status == 400 and "There is a device called Office PC already." in r.text
    r = c.request("POST", "/add", form(label="Far", ip="10.0.0.5", mac="AA:BB:CC:DD:EE:F2", check="ping"))
    assert r.status == 400 and "home network" in r.text
    # a name is shown as text, never as markup
    r = c.request("POST", "/add", form(label="<i>Lab</i>", ip="192.168.50.53", mac="AA:BB:CC:DD:EE:F3", check="arp"))
    assert r.status == 303
    page = c.request("GET", "/").text
    assert "&lt;i&gt;Lab&lt;/i&gt;" in page and "<i>Lab</i>" not in page
    # removing asks first, then it is gone for good
    r = c.request("GET", "/remove/office-pc")
    assert r.status == 200 and "Remove Office PC?" in r.text and 'action="/remove/office-pc"' in r.text
    assert c.request("POST", "/remove/office-pc", "").status == 303
    assert "office-pc" not in json.loads((tmp / "conf" / "machines.json").read_text())
    assert "Office PC" not in c.request("GET", "/").text
    assert c.request("POST", "/wake/office-pc", "").status == 404
    assert c.request("GET", "/remove/office-pc").status == 404
    assert "Its checks of Nextcloud and Open WebUI go with it" in c.request("GET", "/remove/desktop").text
    log = (tmp / "log").read_text()
    assert "added Office PC (192.168.50.50, AA:BB:CC:DD:EE:F0, by ping), from 127.0.0.1" in log
    assert "removed Office PC (192.168.50.50, AA:BB:CC:DD:EE:F0), from 127.0.0.1" in log


def test_an_empty_mac_address_is_looked_up(server):
    c, tmp = server
    c.login()
    (tmp / "up" / "192.168.50.60").touch()
    (tmp / "arp" / "192.168.50.60").write_text("REACHABLE 11:22:33:44:55:66\n")
    assert c.request("POST", "/add", form(label="TV", ip="192.168.50.60", mac="", check="ping")).status == 303
    assert json.loads((tmp / "conf" / "machines.json").read_text())["tv"]["mac"] == "11:22:33:44:55:66"
    # there, but deaf to ping: the page says to choose ARP
    (tmp / "arp" / "192.168.50.61").write_text("DELAY 11:22:33:44:55:67\nREACHABLE 11:22:33:44:55:67\n")
    r = c.request("POST", "/add", form(label="Gamer", ip="192.168.50.61", mac="", check="ping"))
    assert r.status == 400 and "choose ARP" in r.text
    assert c.request("POST", "/add", form(label="Gamer", ip="192.168.50.61", mac="", check="arp")).status == 303
    r = c.request("POST", "/add", form(label="Ghost", ip="192.168.50.62", mac="", check="ping"))
    assert r.status == 400 and "Nothing answered at 192.168.50.62" in r.text


def test_the_list_only_changes_with_the_password(server):
    c, tmp = server
    before = (tmp / "conf" / "machines.json").read_text()
    for method, path, body in [("GET", "/add", None), ("GET", "/remove/pc", None), ("GET", "/edit/pc", None),
                               ("GET", "/shutdown/desktop", None), ("POST", "/shutdown/desktop", ""),
                               ("POST", "/edit/pc", form(label="X", ip="192.168.50.71", mac="AA:BB:CC:DD:EE:71",
                                                         check="ping")),
                               ("POST", "/add", form(label="X", ip="192.168.50.70", mac="AA:BB:CC:DD:EE:70",
                                                     check="ping")),
                               ("POST", "/remove/pc", "")]:
        r = c.request(method, path, body)
        assert r.status == 303 and r.getheader("Location") == "/login", (method, path)
    assert (tmp / "conf" / "machines.json").read_text() == before


def test_a_site_check_moves_with_its_devices_ip():
    wp = load()
    assert wp.moved("https://192.168.50.20/nextcloud/status.php", "192.168.50.20", "192.168.50.140") == \
        "https://192.168.50.140/nextcloud/status.php"
    assert wp.moved("https://192.168.50.20:3443/health", "192.168.50.20", "192.168.50.140") == \
        "https://192.168.50.140:3443/health"
    assert wp.moved("https://192.168.50.1:3443/health", "192.168.50.20", "192.168.50.140") == \
        "https://192.168.50.1:3443/health", "a site on another machine stays where it is"


def test_a_new_address_starts_its_record_afresh(tmp_path, monkeypatch):
    wp = load()
    started = []
    monkeypatch.setattr(wp.Watcher, "_start", lambda self, name: started.append(name))
    w = wp.Watcher(tmp_path)
    w.record("pc", True, 1000.0)
    w.edit("pc", "Gaming PC", "192.168.50.21", "02:00:00:00:00:21", "arp")
    assert state(w, "pc")["on"] is True and started == [], "a new name changes nothing it knows"
    w.edit("pc", "Gaming PC", "192.168.50.178", "02:00:00:00:00:21", "arp")
    assert state(w, "pc")["on"] is None and started == ["pc"], "another address: checked afresh"
    with pytest.raises(LookupError):
        w.edit("nope", "X", "192.168.50.179", "AA:BB:CC:DD:EE:79", "ping")


def test_edit_a_device(server):
    c, tmp = server
    c.login()
    page = c.request("GET", "/").text
    assert 'href="/edit/pc"' in page and 'href="/edit/desktop"' in page
    page = c.request("GET", "/edit/pc").text
    assert "<h1>Edit PC</h1>" in page and 'action="/edit/pc"' in page
    for value in ('value="PC"', 'value="192.168.50.21"', 'value="02:00:00:00:00:21"', 'value="arp" checked'):
        assert value in page, value
    # a new name: it keeps its place and its address on the page
    r = c.request("POST", "/edit/pc", form(label="Gaming PC", ip="192.168.50.21", mac="02:00:00:00:00:21",
                                           check="arp"))
    assert r.status == 303 and r.getheader("Location") == "/"
    saved = json.loads((tmp / "conf" / "machines.json").read_text())
    assert list(saved) == ["desktop", "pc"] and saved["pc"]["label"] == "Gaming PC"
    assert "<b>Gaming PC</b>" in c.request("GET", "/").text
    # another device's MAC is refused, and the form keeps what was typed
    r = c.request("POST", "/edit/pc", form(label="Gaming PC", ip="192.168.50.21", mac="02:00:00:00:00:20",
                                           check="arp"))
    assert r.status == 400 and "Desktop has that MAC address already." in r.text
    assert "<h1>Edit Gaming PC</h1>" in r.text and 'value="02:00:00:00:00:20"' in r.text
    # a new IP takes the desktop's site checks along
    assert "stay, and move along" in c.request("GET", "/edit/desktop").text
    r = c.request("POST", "/edit/desktop", form(label="Desktop", ip="192.168.50.140", mac="02:00:00:00:00:20",
                                                check="ping"))
    assert r.status == 303
    desktop = json.loads((tmp / "conf" / "machines.json").read_text())["desktop"]
    assert desktop["ip"] == "192.168.50.140" and desktop["mac"] == "02:00:00:00:00:20"
    assert desktop["services"] == {"Nextcloud": "https://192.168.50.140/nextcloud/status.php",
                                   "Open WebUI": "https://192.168.50.140:3443/health"}
    assert c.request("GET", "/edit/nope").status == 404
    assert c.request("POST", "/edit/nope", form(label="X", ip="192.168.50.72", mac="AA:BB:CC:DD:EE:72",
                                                check="ping")).status == 404
    log = (tmp / "log").read_text()
    assert "edited PC: label PC -> Gaming PC, from 127.0.0.1" in log
    assert "edited Desktop: ip 192.168.50.20 -> 192.168.50.140, from 127.0.0.1" in log


def test_forms_are_only_taken_from_the_page_itself(server):
    """Every port of the laptop is one site to a browser, so the cookie alone
    would let Pi-hole's or MeTube's pages post here; the Origin header decides."""
    c, tmp = server
    own = f"https://127.0.0.1:{c.port}"                  # what a browser sends from the page itself
    assert c.login().status == 303
    before = (tmp / "conf" / "machines.json").read_text()
    for headers in [{"Origin": "https://127.0.0.1:8489"},           # Pi-hole's admin, next door
                    {"Origin": "null"},                              # a sandboxed or privacy-stripped page
                    {"Referer": "https://127.0.0.1:8081/some/page"},  # MeTube, where no Origin came along
                    {"Origin": f"http://127.0.0.1:{c.port}"}]:       # the page's address, but without TLS
        r = c.request("POST", "/wake/desktop", "", headers)
        assert r.status == 403 and "only taken from this page" in r.text, headers
        r = c.request("POST", "/add", form(label="X", ip="192.168.50.80", mac="AA:BB:CC:DD:EE:80", check="ping"),
                      headers)
        assert r.status == 403, headers
    assert not (tmp / "woken").exists()
    assert (tmp / "conf" / "machines.json").read_text() == before
    assert c.request("POST", "/login", "password=x", {"Origin": "https://127.0.0.1:8489"}).status == 403
    assert "refused a form from another page (https://127.0.0.1:8489)" in (tmp / "log").read_text()
    # from the page itself, it all works as before
    assert c.request("POST", "/wake/desktop", "", {"Origin": own}).status == 303
    assert c.request("POST", "/wake/desktop", "", {"Referer": own + "/"}).status == 303
    assert (tmp / "woken").read_text().count("\n") == 2


def test_a_shut_down_user_has_to_be_a_user_name(tmp_path):
    wp = load()
    machines = json.loads(json.dumps(wp.DEFAULT_MACHINES))
    machines["desktop"]["shutdown"] = "dietpi; reboot"
    (tmp_path / "machines.json").write_text(json.dumps(machines))
    with pytest.raises(ValueError, match="shutdown must be"):
        wp.load_machines(tmp_path / "machines.json")


def test_what_the_page_says_after_a_shut_down():
    wp = load()
    on = {"on": True, "since": 900.0, "exact": True, "wake": None, "halt": 1000.0, "services": {}}
    assert wp.halt_note(on, 1030) == "Shut-down sent less than a minute ago, waiting for it to go off."
    assert wp.halt_note(on, 1300) == "Still on 5 min after the shut-down."
    assert wp.halt_note(dict(on, on=False, since=1040.0), 1100) == "", "nothing to add once it is off"
    assert wp.halt_note(dict(on, since=1200.0), 1300) == "", "woken again since: old news"
    # and a wake-up from before the shut-down is old news once the device is off again
    off = {"on": False, "since": 1100.0, "exact": True, "wake": 1000.0, "halt": 1050.0, "services": {}}
    assert wp.wake_note(off, 1200) == ""


def test_shut_down_the_desktop(desktop_server):
    c, tmp = desktop_server
    conf = tmp / "conf"
    (conf / "shutdown_key").write_text("not a real key\n")
    (tmp / "up" / "192.168.50.20").touch()                       # the desktop is on
    (tmp / "arp" / "192.168.50.21").write_text("REACHABLE\n")    # and so is the PC
    c.login()
    page = c.wait_for('<a class="btn halt" href="/shutdown/desktop">Shut down</a>')
    assert 'href="/shutdown/pc"' not in page, "only a device set up for it gets a working button"
    assert '<button class="btn halt" disabled title="Not set up for this device">Shut down</button>' in page
    assert "<h1>Shut down Desktop?</h1>" in c.request("GET", "/shutdown/desktop").text
    assert c.request("GET", "/shutdown/pc").status == 404
    assert c.request("POST", "/shutdown/pc", "").status == 404
    r = c.request("POST", "/shutdown/desktop", "")
    assert r.status == 303 and r.getheader("Location") == "/"
    args = (tmp / "ssh_calls").read_text().split()
    for want in ("-T", "IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "StrictHostKeyChecking=accept-new",
                 f"UserKnownHostsFile={conf / 'known_hosts'}", str(conf / "shutdown_key")):
        assert want in args, want
    assert args[-3:] == ["-l", "dietpi", "192.168.50.20"], "no command of its own: the desktop's forced one runs"
    assert "Shut-down sent less than a minute ago, waiting for it to go off." in c.request("GET", "/").text
    assert "shut-down sent to Desktop, from 127.0.0.1" in (tmp / "log").read_text()
    # what goes wrong is shown, the way it came back
    (tmp / "ssh_rc").write_text("1\n")
    (tmp / "ssh_err").write_text("sudo: a password is required\n")
    r = c.request("POST", "/shutdown/desktop", "")
    assert r.status == 500 and "Could not shut down Desktop: sudo: a password is required" in r.text
    # a machine on its way down may close the session before it answers: that counts as sent
    (tmp / "ssh_rc").write_text("255\n")
    (tmp / "ssh_err").write_text("Connection to 192.168.50.20 closed by remote host.\n")
    assert c.request("POST", "/shutdown/desktop", "").status == 303
    # without the key the page says so, instead of trying
    (conf / "shutdown_key").unlink()
    r = c.request("POST", "/shutdown/desktop", "")
    assert r.status == 500 and "no shut-down key" in r.text
    # once it is off, the button asks nothing: there is nothing to shut down
    (tmp / "up" / "192.168.50.20").unlink()
    c.wait_for('<a class="btn halt second" href="/shutdown/desktop">')
    page = c.request("GET", "/shutdown/desktop").text
    assert "<h1>Desktop is off</h1>" in page and 'action="/shutdown/desktop"' not in page


def test_every_card_has_wake_and_shut_down():
    """Green Wake, red Shut down: filled for what makes sense now, outlined otherwise,
    and grey where shutting down is not set up, as on every device added on the page."""
    wp = load()
    machines = json.loads(json.dumps(wp.DEFAULT_MACHINES))
    machines["tv"] = {"label": "TV", "ip": "192.168.50.60", "mac": "11:22:33:44:55:66", "check": "ping"}
    on = dict(wp.new_state(), on=True, since=0.0, exact=True)
    off = dict(wp.new_state(), on=False, since=0.0, exact=True)
    page = wp.main_page(machines, {"desktop": on, "pc": off, "tv": off}, 100.0, None).decode()
    assert '<button class="btn wake second">Wake</button>' in page          # the desktop is on
    assert '<a class="btn halt" href="/shutdown/desktop">Shut down</a>' in page
    assert page.count('<button class="btn wake">Wake</button>') == 2       # the PC and the TV are off
    assert page.count('<button class="btn halt" disabled title="Not set up for this device">Shut down</button>') == 2
    page = wp.main_page(machines, {"desktop": off, "pc": off, "tv": off}, 100.0, None).decode()
    assert '<a class="btn halt second" href="/shutdown/desktop">Shut down</a>' in page


def test_a_card_reads_top_to_bottom():
    """Name, IP, how long on or off, any note, its sites, then Edit and Remove; and Log out
    is a real button under the footer's text."""
    wp = load()
    desktop = json.loads(json.dumps(wp.DEFAULT_MACHINES))["desktop"]
    s = dict(wp.new_state(), on=True, since=50.0, exact=True, halt=60.0, services={"Nextcloud": True})
    page = wp.main_page({"desktop": desktop}, {"desktop": s}, 100.0, None).decode()
    card = page[page.index('<section class="card">'):page.index("</section>")]
    order = ["<b>Desktop</b>", "192.168.50.20", '<span class="chip on"><i></i>On</span> for less than a minute',
             "Shut-down sent",
             "✓ Nextcloud", 'href="/edit/desktop"', 'href="/remove/desktop"']
    positions = [card.index(x) for x in order]
    assert positions == sorted(positions), list(zip(order, positions))
    footer = page[page.index("<footer>"):]
    assert footer.index("Checked from the laptop") < footer.index('<button class="btn logout">Log out</button>')
