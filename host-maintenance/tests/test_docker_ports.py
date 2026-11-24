"""
Docker's published ports skip ufw (README, "Docker ports and ufw"). So nothing in
a compose file here is published beyond 127.0.0.1, and a container other devices
have to reach runs on the host's network, where ufw decides as for any other
program. That keeps `sudo ufw status` the whole list of what the house can reach.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = sorted([*ROOT.glob("*/compose.yaml"), *ROOT.glob("*/*.compose.yml")])


def port_mappings(path):
    """Every entry under a ports: key in one compose file, as written."""
    lines = [l for l in path.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    found = []
    for i, line in enumerate(lines):
        if line.strip() != "ports:":
            continue
        indent = len(line) - len(line.lstrip())
        for item in lines[i + 1:]:
            if len(item) - len(item.lstrip()) <= indent:
                break
            found.append(item.split("#")[0].strip().lstrip("-").strip().strip("'\""))
    return found


def test_every_compose_file_is_checked():
    names = {p.relative_to(ROOT).as_posix() for p in COMPOSE_FILES}
    assert {"ntfy/compose.yaml",
            "desktop/open-webui.compose.yml"} <= names, names


def test_nothing_is_published_beyond_localhost():
    for path in COMPOSE_FILES:
        for mapping in port_mappings(path):
            assert mapping.startswith("127.0.0.1:"), f"{path.relative_to(ROOT)}: {mapping} would skip ufw"


def test_a_port_open_to_the_network_would_be_caught(tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text('services:\n  x:\n    ports:\n      - "8081:8081"   # note\n'
                       "      - 127.0.0.1:1:1\n    volumes:\n      - ./a:/a\n")
    assert port_mappings(compose) == ["8081:8081", "127.0.0.1:1:1"]


def test_ntfy_is_on_the_hosts_network_and_its_plain_port_stays_local():
    """With no published ports, server.yml alone keeps plain HTTP on the laptop."""
    compose = (ROOT / "ntfy" / "compose.yaml").read_text()
    conf = (ROOT / "ntfy" / "server.yml").read_text()
    assert re.search(r"^    network_mode: host$", compose, re.M)
    assert re.search(r'^listen-http: "127\.0\.0\.1:8082"$', conf, re.M), "plain HTTP must stay on 127.0.0.1"
    assert re.search(r'^listen-https: ":8490"$', conf, re.M)
    assert re.search(r'^base-url: "https://192\.168\.50\.10:8490"$', conf, re.M), "the phone's address"
    assert "http://127.0.0.1:8082/v1/health" in compose, "the health check follows the plain port"
    assert "NTFY_URL=http://127.0.0.1:8082" in (ROOT / "ntfy" / "ntfy-notify.conf.example").read_text()
