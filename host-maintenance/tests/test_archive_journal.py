"""
archive-journal: the incremental copy of the volatile systemd journal onto the
data disk, exercised against a fake journalctl so no real journal is read and
nothing outside the temp directory is written.
"""
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "archive-journal"

# Stands in for journalctl: honours --cursor-file, and answers differently once
# a cursor exists, which is how the real one behaves and the whole point here.
FAKE_JOURNALCTL = '''#!/usr/bin/env python3
import os, sys

if os.environ.get("FAKE_FAIL"):
    sys.exit(1)

cursor = None
for a in sys.argv[1:]:
    if a.startswith("--cursor-file="):
        cursor = a.split("=", 1)[1]

seen = os.path.exists(cursor) and open(cursor).read().strip() if cursor else False
sys.stdout.write(os.environ.get("FAKE_NEXT" if seen else "FAKE_FIRST", ""))
if cursor:
    open(cursor, "w").write("s=abc;i=1\\n")
sys.exit(0)
'''

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture
def arch(tmp_path):
    fake = tmp_path / "journalctl"
    fake.write_text(FAKE_JOURNALCTL)
    fake.chmod(0o755)
    archive = tmp_path / "journal"

    def run(*argv, **env):
        e = dict(os.environ, JOURNALCTL=str(fake), ARCHIVE_DIR=str(archive),
                 FAKE_FIRST="boot line 1\nboot line 2\n", FAKE_NEXT="")
        e.pop("FAKE_FAIL", None)
        e.update(env)
        return subprocess.run(["bash", str(SCRIPT), *argv], env=e,
                              capture_output=True, text=True, timeout=60)

    run.dir = archive
    run.today = archive / f"journal-{TODAY}.log"
    run.cursor = archive / ".cursor"
    return run


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_the_first_run_copies_the_journal_and_records_where_it_stopped(arch):
    r = arch()
    assert r.returncode == 0, r.stderr
    assert arch.today.read_text() == "boot line 1\nboot line 2\n"
    assert arch.cursor.exists(), "no cursor written, the next run would copy everything again"


def test_the_file_is_named_for_todays_utc_date(arch):
    arch()
    assert [p.name for p in arch.dir.glob("journal-*.log")] == [f"journal-{TODAY}.log"]


def test_a_quiet_run_adds_nothing_and_leaves_no_empty_file(arch):
    arch()                                   # first run writes the two lines
    before = arch.today.read_text()
    r = arch()                               # cursor now exists, fake returns nothing
    assert r.returncode == 0
    assert arch.today.read_text() == before, "a quiet run duplicated history"
    assert r.stdout.strip() == "", "a quiet run should say nothing"


def test_nothing_is_copied_twice_across_runs(arch):
    arch()
    arch(FAKE_NEXT="later line\n")
    assert arch.today.read_text() == "boot line 1\nboot line 2\nlater line\n"


def test_a_quiet_first_run_creates_no_file_at_all(arch):
    r = arch(FAKE_FIRST="")
    assert r.returncode == 0
    assert list(arch.dir.glob("journal-*.log")) == []


def test_a_failing_journalctl_is_reported_not_swallowed(arch):
    r = arch(FAKE_FAIL="1")
    assert r.returncode != 0
    assert "journalctl failed" in r.stderr


def test_old_days_are_pruned_and_today_is_kept(arch):
    arch()
    old = arch.dir / "journal-2020-01-01.log"
    old.write_text("ancient\n")
    long_ago = time.time() - 5 * 86400
    os.utime(old, (long_ago, long_ago))
    arch(RETAIN_DAYS="1")
    assert not old.exists(), "an old day survived the prune"
    assert arch.today.exists(), "the prune took today's file"


def test_retention_keeps_a_file_that_is_still_young_enough(arch):
    arch()
    recent = arch.dir / "journal-2020-01-02.log"
    recent.write_text("recent\n")
    two_days = time.time() - 2 * 86400
    os.utime(recent, (two_days, two_days))
    arch(RETAIN_DAYS="30")
    assert recent.exists(), "a file inside the retention window was deleted"


def test_status_reports_and_writes_nothing(arch):
    arch()
    before = sorted(p.name for p in arch.dir.iterdir())
    r = arch("--status")
    assert r.returncode == 0
    assert f"journal-{TODAY}.log" in r.stdout
    assert sorted(p.name for p in arch.dir.iterdir()) == before


def test_status_before_the_first_run_says_so(arch):
    r = arch("--status")
    assert r.returncode == 0 and "no archive yet" in r.stdout


def test_an_unknown_argument_is_refused(arch):
    r = arch("--purge-everything")
    assert r.returncode != 0 and "unknown argument" in r.stderr
