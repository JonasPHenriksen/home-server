"""
service-watch: the two-minute health check, run against fake systemctl, docker,
dig, tailscale, ping, df and pgrep, with a fake ntfy-notify that records every
call, so nothing on the machine is touched and nothing is ever sent.
"""
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "service-watch"

TAILSCALE = '{\n  "BackendState": "Running",\n  "Self": {\n    "Online": %s\n  }\n}\n'

FAKES = {
    "systemctl": r'''#!/bin/bash
if [ "$1" = is-active ]; then
    shift
    for u in "$@"; do
        s=$(awk -v u="$u" '$1 == u {print $2}' "$FAKE_STATE/services"); echo "${s:-active}"
    done
elif [ "$1" = --failed ]; then
    cat "$FAKE_STATE/failed"
fi
''',
    "docker": '#!/bin/bash\n[ -e "$FAKE_STATE/docker_broken" ] && exit 1\ncat "$FAKE_STATE/containers"\n',
    "dig": '#!/bin/bash\ncase " $* " in *" -p 5335 "*) cat "$FAKE_STATE/unbound" ;; *) cat "$FAKE_STATE/pihole" ;; esac\n',
    "tailscale": '#!/bin/bash\ncat "$FAKE_STATE/tailscale"\n',
    "ping": '#!/bin/bash\n[ ! -e "$FAKE_STATE/offline" ]\n',
    "df": '#!/bin/bash\necho Avail\necho " $(cat "$FAKE_STATE/free_gb")"\n',
    "pgrep": '#!/bin/bash\n[ -e "$FAKE_STATE/maintenance" ]\n',
}

# every call as its argument list: fields end in \x1f, calls in \x1e
NOTIFY = '''#!/bin/bash
printf '%s\\x1f' "$@" >> "$FAKE_STATE/calls"; printf '\\x1e' >> "$FAKE_STATE/calls"
[ ! -e "$FAKE_STATE/notify_fails" ]
'''



class Box:
    def __init__(self, tmp_path):
        self.fake = tmp_path / "fake"
        self.fake.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in FAKES.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.notify = tmp_path / "notify"
        self.notify.write_text(NOTIFY)
        self.notify.chmod(0o755)
        self.state = tmp_path / "state"
        self.log = tmp_path / "watch.log"
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_STATE=str(self.fake),
                        STATE_DIR=str(self.state), LOG=str(self.log), NOTIFY=str(self.notify))
        self.healthy()

    def healthy(self):
        self.write("services", "")
        self.write("failed", "")
        self.write("containers", "metube\tUp 6 days (healthy)\nmetube-login\tUp 6 days\nntfy\tUp 6 days (healthy)\n")
        self.write("pihole", "0.0.0.0\n")
        self.write("unbound", "104.21.3.215\n")
        self.write("tailscale", TAILSCALE % "true")
        self.write("free_gb", "758\n")
        for flag in ("offline", "maintenance", "docker_broken", "notify_fails"):
            (self.fake / flag).unlink(missing_ok=True)

    def write(self, name, text):
        (self.fake / name).write_text(text)

    def flag(self, name, on=True):
        (self.fake / name).touch() if on else (self.fake / name).unlink(missing_ok=True)

    def service(self, name, state):
        lines = [l for l in (self.fake / "services").read_text().splitlines() if not l.startswith(name + " ")]
        self.write("services", "".join(l + "\n" for l in lines + [f"{name} {state}"]))

    def run(self, *args, now=None, boot=None):
        env = dict(self.env)
        if now is not None:
            env["NOW"] = str(now)
        if boot is not None:
            env["BOOT_TIME"] = str(boot)
        r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stderr == "", r.stderr
        return r

    def calls(self):
        f = self.fake / "calls"
        return [c.split("\x1f")[:-1] for c in f.read_text().split("\x1e") if c] if f.exists() else []

    def problems(self):
        f = self.state / "problems.tsv"
        return f.read_text() if f.exists() else ""


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_all_healthy_is_silent(tmp_path):
    box = Box(tmp_path)
    for _ in range(3):
        box.run()
    assert box.calls() == []
    assert box.problems() == ""
    assert not box.log.exists(), "quiet rounds leave no log lines"


def test_a_failed_service_is_pushed_on_the_second_round_and_once_when_fixed(tmp_path):
    box = Box(tmp_path)
    box.service("pihole-FTL", "failed")
    box.run(now=1000)
    assert box.calls() == [], "one round is not enough"
    box.run(now=1120)
    (call,) = box.calls()
    assert call[:5] == ["-p", "high", "-t", "warning", "pihole-FTL has failed"]
    assert call[5] == "Since 00:16 UTC. Look: systemctl status pihole-FTL"
    box.run(now=1240)
    assert len(box.calls()) == 1, "pushed once, not every round"
    box.service("pihole-FTL", "active")
    box.run(now=1000 + 12 * 60)
    fixed = box.calls()[1]
    assert fixed[:5] == ["-p", "low", "-t", "white_check_mark", "pihole-FTL is OK again"]
    assert fixed[5] == "Was: pihole-FTL has failed. For 12 min, since 00:16 UTC."
    assert box.problems() == ""


def test_a_blip_is_never_pushed(tmp_path):
    box = Box(tmp_path)
    box.service("unbound", "activating")
    box.run()
    box.service("unbound", "active")
    box.run()
    box.run()
    assert box.calls() == []
    log = box.log.read_text()
    assert "seen: unbound keeps restarting" in log and "cleared before it was pushed" in log


def test_several_problems_share_one_push(tmp_path):
    box = Box(tmp_path)
    box.service("dropbear", "inactive")
    box.write("containers", "metube\tExited (137) 3 minutes ago\nmetube-login\tUp 6 days\nntfy\tUp 6 days (healthy)\n")
    box.run()
    box.run()
    (call,) = box.calls()
    assert call[4] == "2 problems"
    assert "- dropbear is stopped (since" in call[5]
    assert "- The metube container is down (Exited (137) 3 minutes ago) (since" in call[5]
    box.healthy()
    box.run()
    assert box.calls()[1][4] == "2 problems fixed"


def test_what_depends_on_a_broken_thing_is_not_reported_twice(tmp_path):
    box = Box(tmp_path)
    box.service("pihole-FTL", "failed")
    box.write("pihole", "")
    box.service("docker", "failed")
    box.flag("docker_broken")
    for _ in range(3):
        box.run()
    (call,) = box.calls()
    assert call[4] == "2 problems"
    assert "pihole-FTL has failed" in call[5] and "docker has failed" in call[5]
    assert "not answering" not in call[5] and "container" not in call[5]


def test_no_internet_is_reported_alone_and_after_three_rounds(tmp_path):
    box = Box(tmp_path)
    box.flag("offline")
    box.write("unbound", "")
    box.write("tailscale", TAILSCALE % "false")
    box.run()
    box.run()
    assert box.calls() == [], "short outages happen"
    box.run()
    (call,) = box.calls()
    assert call[4] == "No internet connection (9.9.9.9 1.1.1.1 do not answer)"
    assert "unbound" not in call[5] and "Tailscale" not in call[5]


def test_containers_missing_or_unhealthy(tmp_path):
    box = Box(tmp_path)
    box.write("containers", "metube-login\tUp 2 hours\nntfy\tUp 2 hours (unhealthy)\n")
    box.run()
    box.run()
    body = box.calls()[0][5]
    assert "The metube container is missing" in body and "The ntfy container is unhealthy" in body


def test_pihole_not_blocking_gets_half_an_hour(tmp_path):
    box = Box(tmp_path)
    box.write("pihole", "142.250.74.66\n")
    for _ in range(14):
        box.run()
    assert box.calls() == [], "pausing blocking for a few minutes is normal"
    box.run()
    (call,) = box.calls()
    assert call[4] == "Pi-hole is not blocking ads (ad.doubleclick.net -> 142.250.74.66)"


def test_pihole_not_answering(tmp_path):
    box = Box(tmp_path)
    box.write("pihole", "")
    box.run()
    box.run()
    assert box.calls()[0][4] == "Pi-hole is not answering DNS queries"


def test_other_failed_units_are_reported_and_the_watched_ones_only_once(tmp_path):
    box = Box(tmp_path)
    box.service("cam_recorder", "failed")
    box.write("failed", "archive-journal.service loaded failed failed Copy new journal entries\n"
                        "cam_recorder.service loaded failed failed Tapo recorder\n")
    box.run()
    box.run()
    body = box.calls()[0][5]
    assert "archive-journal.service has failed" in body and "cam_recorder has failed" in body
    assert "cam_recorder.service" not in body
    box.write("failed", "cam_recorder.service loaded failed failed Tapo recorder\n")
    box.run()
    assert box.calls()[1][4] == "archive-journal.service is OK again"


def test_a_nearly_full_disk_and_a_disconnected_tailscale(tmp_path):
    box = Box(tmp_path)
    box.write("free_gb", "7\n")
    box.write("tailscale", TAILSCALE % "false")
    box.run()
    box.run()
    assert box.calls()[0][4] == "The disk is almost full: 7 GB free on /"
    box.run()
    assert box.calls()[1][4] == "Tailscale is not connected to the tailnet", "Tailscale waits one round longer"


def test_nothing_is_checked_while_maintenance_runs(tmp_path):
    box = Box(tmp_path)
    box.service("docker", "inactive")
    box.flag("maintenance")
    for _ in range(3):
        box.run()
    assert box.calls() == []
    assert box.problems() == ""
    assert (box.state / "alive").exists(), "busy, not dead"


def test_a_push_that_fails_is_retried(tmp_path):
    box = Box(tmp_path)
    box.service("ufw", "inactive")
    box.flag("notify_fails")
    box.run()
    box.run()
    assert len(box.calls()) == 1, "attempted"
    box.flag("notify_fails", False)
    box.run()
    assert len(box.calls()) == 2 and box.calls()[1][4] == "ufw is stopped"
    box.run()
    assert len(box.calls()) == 2


def test_an_unexpected_shutdown_is_pushed_once_after_the_boot(tmp_path):
    box = Box(tmp_path)
    box.run(boot=500)
    assert box.calls() == [], "the first round ever has nothing to compare with"
    os.utime(box.state / "alive", (1_000_000, 1_000_000))     # last seen running
    box.run(boot=1_000_600)                                   # up again ten minutes later, no clean mark
    (call,) = box.calls()
    assert call[:5] == ["-p", "high", "-t", "electric_plug", "Restarted after an unexpected shutdown"]
    seen = time.strftime("%a %d %b %H:%M UTC", time.gmtime(1_000_000))
    back = time.strftime("%a %d %b %H:%M UTC", time.gmtime(1_000_600))
    assert call[5].startswith(f"Last seen running {seen}, up again since {back}.")
    box.run(boot=1_000_600)
    assert len(box.calls()) == 1, "only the first round after a boot looks"


def test_a_clean_shutdown_is_silent(tmp_path):
    box = Box(tmp_path)
    box.run()
    os.utime(box.state / "alive", (1_000_000, 1_000_000))
    box.run("--shutdown")
    os.utime(box.state / "clean-shutdown", (1_000_060, 1_000_060))
    box.run(boot=1_000_120)
    assert box.calls() == []
    assert not (box.state / "clean-shutdown").exists(), "used up"
    assert "booted after a clean shutdown" in box.log.read_text()


def test_status_shows_problems_and_never_pushes(tmp_path):
    box = Box(tmp_path)
    box.service("earlyoom", "failed")
    out = box.run("--status").stdout
    assert re.search(r"^PROBLEM  service:earlyoom +earlyoom has failed", out, re.M)
    assert "ok       dns:answer" in out
    assert box.calls() == [] and box.problems() == ""


def test_watches_the_same_services_as_the_login_summary():
    def services(path):
        return re.search(r"^SERVICES=\(([^)]*)\)", path.read_text(), re.M).group(1).split()
    assert services(SCRIPT) == services(ROOT / "login-summary")


def test_units():
    timer = (ROOT / "service-watch.timer").read_text()
    assert "OnBootSec=3min" in timer and "OnUnitActiveSec=2min" in timer
    assert "ExecStart=/usr/local/bin/service-watch\n" in (ROOT / "service-watch.service").read_text()
    hook = (ROOT / "service-watch-shutdown.service").read_text()
    assert "ExecStop=/usr/local/bin/service-watch --shutdown" in hook and "RemainAfterExit=yes" in hook


def test_the_container_list_is_left_for_the_login_summary(tmp_path):
    """dietpi may not ask docker itself, so the login screen reads this file."""
    box = Box(tmp_path)
    box.run()
    listing = (box.state / "containers.tsv").read_text()
    assert listing.startswith("metube\tUp 6 days (healthy)") and "metube-login" in listing
