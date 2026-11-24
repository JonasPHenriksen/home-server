"""
backup-gate: the forced command of the backup server's and the desktop's keys.
First against a fake rrsync that records what it was asked, then end to end:
the real rsync client, the real rrsync and rsync server, through a fake ssh that
does what dropbear does (the remote command lands in SSH_ORIGINAL_COMMAND). The
option strings are the ones the two clients really send, from the laptop's
sudo log.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "backup-gate"

# What the clients send today (sudo log, 2026-09-26), minus "sudo".
PULL = "rsync --server --sender -lHogDtpXrSe.iLsfxCIvu --timeout=900 --numeric-ids --info=STATS2 . "
VERIFY = "rsync --server --sender -ncRe.LsfxCIvu --files-from=- --from0 . "
PUSH = "rsync --server -lHogDtpXrSze.iLsfxCIvu --timeout=600 --delete --partial --numeric-ids --info=STATS2 . "
PUSH_NC = "rsync --server -ytrSze.iLsfxCIvu --timeout=600 --delete --partial --info=STATS2 . "
INSTALL = "rsync --server --sender -logDtprze.iLsfxCIvu . "

FAKE_RRSYNC = """#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
printf 'CMD=%s\\n' "$SSH_ORIGINAL_COMMAND" >> "$FAKE_LOG"
"""
FAKE_SUDO = """#!/bin/bash
[ "$1" = -n ] && shift
echo "sudo $*" >> "$FAKE_LOG"
exec "$@"
"""
FAKE_LOGGER = """#!/bin/bash
echo "logger $*" >> "$FAKE_LOG"
"""
FAKE_SSH = """#!/bin/bash
shift                       # the host name; the rest is the remote command
export SSH_ORIGINAL_COMMAND="$*"
exec "$GATE_BIN" "$GATE_ROLE"
"""


def script(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def lap(tmp_path):
    """A scratch laptop: logs, repo, the three backup trees, and the fakes."""
    class Lap:
        pass
    l = Lap()
    l.root = tmp_path
    l.logs = tmp_path / "logs"
    (l.logs / "backup-server").mkdir(parents=True)
    (l.logs / "desktop").mkdir()
    l.repo = tmp_path / "repo"
    for sub, name in (("backup-server", "pull-backups.sh"), ("desktop", "dietpi-backup-sync.sh")):
        (l.repo / sub).mkdir(parents=True)
        (l.repo / sub / name).write_text(f"# {name}\n")
    (l.repo / "README.md").write_text("readme\n")
    l.own = tmp_path / "mnt" / "dietpi-backup" / "data"
    l.mirror = tmp_path / "mnt" / "dietpi-desktop-backup"
    l.nc = tmp_path / "mnt" / "dietpi-desktop-nextcloud-backup"
    for t in (l.own, l.mirror, l.nc):
        t.mkdir(parents=True)
        (t / "etc").mkdir()
        (t / "etc" / "hostname").write_text(f"{t.name}\n")
    l.log = tmp_path / "fake.log"
    l.fake_rrsync = script(tmp_path / "rrsync", FAKE_RRSYNC)
    l.fake_sudo = script(tmp_path / "sudo", FAKE_SUDO)
    l.logger = script(tmp_path / "logger", FAKE_LOGGER)
    l.ssh = script(tmp_path / "ssh", FAKE_SSH)

    def env(role="pull", rrsync=None, sudo=""):
        e = dict(os.environ, SUDO=sudo, LOGGER=str(l.logger), RRSYNC=str(rrsync or l.fake_rrsync),
                 LOGS=str(l.logs), REPO=str(l.repo), FAKE_LOG=str(l.log),
                 PULL_TREES=f"{l.own}/ {l.mirror}/ {l.nc}/", PUSH_TREES=f"{l.mirror}/ {l.nc}/",
                 GATE_BIN=str(GATE), GATE_ROLE=role,
                 SSH_CLIENT=("192.168.50.20" if role == "push" else "192.168.50.30") + " 40522 22")
        e.pop("SSH_ORIGINAL_COMMAND", None)
        return e

    def gate(role, cmd, stdin="", **kw):
        e = env(role, **kw)
        if cmd is not None:
            e["SSH_ORIGINAL_COMMAND"] = cmd
        return subprocess.run(["bash", str(GATE), role], env=e, input=stdin,
                              capture_output=True, text=True, timeout=60)

    l.env, l.gate = env, gate
    l.calls = lambda: l.log.read_text().splitlines() if l.log.exists() else []
    return l


def refused(r, lap):
    return r.returncode != 0 and "not allowed" in r.stderr and any("refused" in c for c in lap.calls())


# --------------------------------------------------------------------------- what is let through

def test_script_parses():
    assert subprocess.run(["bash", "-n", str(GATE)]).returncode == 0


def test_true_checks_the_connection(lap):
    assert lap.gate("pull", "true").returncode == 0
    assert lap.gate("push", "true").returncode == 0


def test_the_backup_server_reads_each_tree_through_rrsync_read_only(lap):
    for tree in (lap.own, lap.mirror, lap.nc):
        lap.log.unlink(missing_ok=True)
        r = lap.gate("pull", PULL + f"{tree}/", sudo=str(lap.fake_sudo))
        assert r.returncode == 0, r.stderr
        calls = lap.calls()
        assert calls[0] == f"sudo {lap.fake_rrsync} -ro {tree}", "confined to that one tree"
        # rrsync -ro TREE wants paths relative to TREE: the tree becomes "."
        assert calls[2] == f"CMD={PULL}.", calls


def test_the_verify_pass_reads_a_file_list(lap):
    assert lap.gate("pull", VERIFY + f"{lap.own}/").returncode == 0


def test_the_desktop_writes_each_mirror_through_rrsync_write_only(lap):
    for tree, cmd in ((lap.mirror, PUSH), (lap.nc, PUSH_NC)):
        lap.log.unlink(missing_ok=True)
        r = lap.gate("push", cmd + f"{tree}/", sudo=str(lap.fake_sudo))
        assert r.returncode == 0, r.stderr
        calls = lap.calls()
        assert calls[0] == f"sudo {lap.fake_rrsync} -wo {tree}", calls
        # rrsync -wo DIR wants paths relative to DIR: the mirror becomes "."
        assert calls[2] == f"CMD={cmd}.", calls


def test_the_heartbeat_lands_whole_in_its_own_place(lap):
    hb = lap.logs / "backup-server" / "heartbeat"
    cmd = f"cat > '{hb}.tmp' && mv -f '{hb}.tmp' '{hb}'"
    r = lap.gate("pull", cmd, stdin="last_cycle=x\nstatus=ok\n")
    assert r.returncode == 0, r.stderr
    assert hb.read_text() == "last_cycle=x\nstatus=ok\n"
    assert [p.name for p in hb.parent.iterdir()] == ["heartbeat"], "a temp file was left behind"


def test_each_machine_sends_its_report_into_its_own_folder(lap):
    for role, sub, name in (("pull", "backup-server", "report-2026-09-27_01-58-12.txt"),
                            ("push", "desktop", "report-2026-09-26_15-15.txt")):
        d = lap.logs / sub
        cmd = f"mkdir -p '{d}' && cat > '{d}/.{name}' && mv -f '{d}/.{name}' '{d}/{name}'"
        r = lap.gate(role, cmd, stdin=f"status=ok\n{role}\n")
        assert r.returncode == 0, r.stderr
        assert (d / name).read_text() == f"status=ok\n{role}\n"


def test_the_backup_server_asks_the_uptime(lap):
    r = lap.gate("pull", "cut -d. -f1 /proc/uptime")
    assert r.returncode == 0 and r.stdout.strip().isdigit()


def test_install_reads_need_no_sudo_and_stay_in_their_own_folder(lap):
    for role, sub, name in (("pull", "backup-server", "pull-backups.sh"),
                            ("push", "desktop", "dietpi-backup-sync.sh")):
        lap.log.unlink(missing_ok=True)
        r = lap.gate(role, INSTALL + f"{lap.repo}/{sub}/{name}", sudo=str(lap.fake_sudo))
        assert r.returncode == 0, r.stderr
        assert lap.calls()[0] == f"-ro {lap.repo}/{sub}", "confined to the role's own folder"
        assert lap.calls()[1] == f"CMD={INSTALL}{name}", "the path relative to that folder"
        assert not any(c.startswith("sudo") for c in lap.calls()), "an install read must not use sudo"


# --------------------------------------------------------------------------- what is refused

@pytest.mark.parametrize("role", ["pull", "push"])
@pytest.mark.parametrize("cmd", [
    None,                                       # an interactive login
    "sh", "bash -i", "id", "cat /etc/shadow",
    "cut -d. -f1 /proc/uptime; id",
    "sudo rsync --server --sender -lHogDtpXrSe.iLsfxCIvu . /",
    "scp -f /etc/shadow",
    "/usr/lib/sftp-server",
])
def test_anything_else_is_refused_and_logged(lap, role, cmd):
    assert refused(lap.gate(role, cmd), lap)
    assert not any(c.startswith(("sudo", "-ro", "-wo", "CMD=")) for c in lap.calls()), lap.calls()


def test_the_backup_server_reads_nothing_but_the_trees(lap):
    for path in ("/etc/", "/", f"{lap.own.parent}/", f"{lap.own}", "/root/.ssh/"):
        lap.log.unlink(missing_ok=True)
        assert refused(lap.gate("pull", PULL + path), lap), path


def test_one_path_at_a_time(lap):
    """A second source before an allowed tree would be read as root too."""
    assert refused(lap.gate("pull", PULL.replace(" . ", " . /etc/ ") + f"{lap.own}/"), lap)
    assert refused(lap.gate("push", PUSH.replace(" . ", " . /etc/ ") + f"{lap.mirror}/"), lap)
    # rsync laptop:/etc/shadow :. :TREE/ sends ". /etc/shadow . TREE/", which also
    # ends in ". TREE/": every word after the first "." is a path
    assert refused(lap.gate("pull", PULL + f"/etc/shadow . {lap.own}/"), lap)
    assert refused(lap.gate("push", PUSH + f"/etc/ . {lap.mirror}/"), lap)
    assert refused(lap.gate("push", INSTALL + f"/etc/shadow . {lap.repo}/desktop/dietpi-backup-sync.sh"), lap)
    assert refused(lap.gate("pull", INSTALL + f"/etc/shadow . {lap.repo}/backup-server/pull-backups.sh"), lap)
    assert not any(c.startswith(("sudo", "-ro", "-wo", "CMD=")) for c in lap.calls()), lap.calls()


def test_the_backup_server_cannot_write(lap):
    assert refused(lap.gate("pull", PUSH + f"{lap.mirror}/"), lap)


def test_the_desktop_writes_nothing_but_its_mirrors(lap):
    for path in (f"{lap.own}/", "/etc/", "/", f"{lap.logs}/", f"{lap.mirror}"):
        lap.log.unlink(missing_ok=True)
        assert refused(lap.gate("push", PUSH + path), lap), path


def test_the_desktop_cannot_read_the_backups(lap):
    assert refused(lap.gate("push", PULL + f"{lap.mirror}/"), lap)
    assert refused(lap.gate("push", PULL + f"{lap.own}/"), lap)


def test_the_desktop_gets_no_uptime_and_no_heartbeat(lap):
    hb = lap.logs / "backup-server" / "heartbeat"
    assert refused(lap.gate("push", "cut -d. -f1 /proc/uptime"), lap)
    assert refused(lap.gate("push", f"cat > '{hb}.tmp' && mv -f '{hb}.tmp' '{hb}'", stdin="x"), lap)
    assert not hb.exists()


def test_files_go_nowhere_but_their_fixed_places(lap):
    target = lap.root / "elsewhere"
    assert refused(lap.gate("pull", f"cat > '{target}.tmp' && mv -f '{target}.tmp' '{target}'", stdin="x"), lap)
    d = lap.logs / "desktop"
    for bad in ("../../x.txt", "report-evil.sh", "report-2026-09-26_15-15.txt.sh", "report-x.txt"):
        cmd = f"mkdir -p '{d}' && cat > '{d}/.{bad}' && mv -f '{d}/.{bad}' '{d}/{bad}'"
        assert refused(lap.gate("push", cmd, stdin="x"), lap), bad
    # a report into the other machine's folder
    b = lap.logs / "backup-server"
    name = "report-2026-09-26_15-15.txt"
    assert refused(lap.gate("push", f"mkdir -p '{b}' && cat > '{b}/.{name}' && mv -f '{b}/.{name}' '{b}/{name}'",
                            stdin="x"), lap)
    assert not target.exists() and list(b.iterdir()) == []


def test_install_reads_stay_in_their_own_folder(lap):
    assert refused(lap.gate("push", INSTALL + f"{lap.repo}/backup-server/pull-backups.sh"), lap)
    assert refused(lap.gate("push", INSTALL + f"{lap.repo}/desktop/../README.md"), lap)
    assert refused(lap.gate("pull", INSTALL + f"{lap.repo}/README.md"), lap)
    assert refused(lap.gate("pull", INSTALL + f"{lap.repo}/desktop/dietpi-backup-sync.sh"), lap)


def test_each_key_works_only_from_its_own_machine(lap):
    """dropbear has no from= option, so the gate checks where the client is."""
    for role, other in (("pull", "192.168.50.20"), ("pull", "192.168.50.40"), ("pull", "100.64.0.40"),
                        ("push", "192.168.50.30"), ("push", "192.168.50.1")):
        e = lap.env(role)
        e.update(SSH_ORIGINAL_COMMAND="true", SSH_CLIENT=f"{other} 40522 22")
        r = subprocess.run(["bash", str(GATE), role], env=e, capture_output=True, text=True)
        assert r.returncode != 0 and "only from" in r.stderr, (role, other)
    e = lap.env("pull")
    e.pop("SSH_CLIENT")
    e["SSH_ORIGINAL_COMMAND"] = "true"
    assert subprocess.run(["bash", str(GATE), "pull"], env=e, capture_output=True).returncode != 0


def test_an_unknown_role_is_refused(lap):
    assert refused(lap.gate("admin", "id"), lap)
    assert refused(lap.gate("admin", "true"), lap)


# --------------------------------------------------------------------------- end to end, real rsync

needs_rsync = pytest.mark.skipif(not (shutil.which("rsync") and Path("/usr/bin/rrsync").exists()),
                                 reason="needs rsync and /usr/bin/rrsync")


def rsync(lap, role, *args):
    e = lap.env(role, rrsync="/usr/bin/rrsync")
    return subprocess.run(["rsync", "-e", str(lap.ssh), *args], env=e,
                          capture_output=True, text=True, timeout=120)


@needs_rsync
def test_real_push_fills_and_prunes_the_mirror(lap):
    src = lap.root / "desktop-root"
    (src / "etc").mkdir(parents=True)
    (src / "etc" / "hostname").write_text("DietPi\n")
    (src / "home").mkdir()
    (src / "home" / "note").write_text("hi\n")
    (lap.mirror / "gone-from-the-desktop").write_text("old\n")
    r = rsync(lap, "push", "-lHogDtpXrSz", "--timeout=600", "--delete", "--partial", "--numeric-ids",
              "--info=STATS2", f"{src}/", f"desktop:{lap.mirror}/")
    assert r.returncode == 0, r.stderr
    assert (lap.mirror / "home" / "note").read_text() == "hi\n"
    assert (lap.mirror / "etc" / "hostname").read_text() == "DietPi\n"
    assert not (lap.mirror / "gone-from-the-desktop").exists(), "--delete must reach the mirror"
    # the Nextcloud tree goes with its own options
    r = rsync(lap, "push", "-ytrSz", "--timeout=600", "--delete", "--partial", "--info=STATS2",
              f"{src}/", f"desktop:{lap.nc}/")
    assert r.returncode == 0, r.stderr
    assert (lap.nc / "home" / "note").read_text() == "hi\n"


@needs_rsync
def test_real_push_elsewhere_fails_and_writes_nothing(lap):
    src = lap.root / "desktop-root"
    src.mkdir()
    (src / "evil").write_text("x\n")
    for dest in (f"{lap.own}/", f"{lap.logs}/"):
        r = rsync(lap, "push", "-rt", f"{src}/", f"desktop:{dest}")
        assert r.returncode != 0
    assert not (lap.own / "evil").exists() and not (lap.logs / "evil").exists()


@needs_rsync
def test_real_pull_and_verify(lap):
    dest = lap.root / "snapshot"
    r = rsync(lap, "pull", "-lHogDtpXrS", "--timeout=900", "--numeric-ids", "--info=STATS2",
              f"laptop:{lap.own}/", f"{dest}/")
    assert r.returncode == 0, r.stderr
    assert (dest / "etc" / "hostname").read_text() == "data\n"
    lst = lap.root / "list"
    lst.write_bytes(b"etc/hostname\0")
    r = rsync(lap, "pull", "-ncR", f"--files-from={lst}", "--from0", f"laptop:{lap.own}/", f"{dest}/")
    assert r.returncode == 0, r.stderr


@needs_rsync
def test_real_pull_of_anything_else_fails(lap):
    r = rsync(lap, "pull", "-r", f"laptop:{lap.logs}/", f"{lap.root / 'x'}/")
    assert r.returncode != 0 and not (lap.root / "x").exists()


@needs_rsync
def test_real_pull_of_a_second_path_before_a_tree_fails(lap):
    """What a stock client sends for laptop:/SECRET :. :TREE/ (it got through before).
    No -r: against a broken gate, "." under rrsync -ro / is the whole disk."""
    secret = lap.logs / "secret"
    secret.write_text("not yours\n")
    dest = lap.root / "got"
    dest.mkdir()
    r = rsync(lap, "pull", f"laptop:{secret}", ":.", f":{lap.own}/", f"{dest}/")
    assert r.returncode != 0
    assert not (dest / "secret").exists()
    r = rsync(lap, "push", f"desktop:{secret}", ":.", f":{lap.repo}/desktop/dietpi-backup-sync.sh", f"{dest}/")
    assert r.returncode != 0
    assert not (dest / "secret").exists()


@needs_rsync
def test_real_install_read(lap):
    out = lap.root / "got.sh"
    r = rsync(lap, "push", f"desktop:{lap.repo}/desktop/dietpi-backup-sync.sh", str(out))
    assert r.returncode == 0, r.stderr
    assert out.read_text() == "# dietpi-backup-sync.sh\n"
