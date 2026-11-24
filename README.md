# home-server

The code behind a small self-hosted home server: a DietPi (Debian) laptop that
filters DNS for the house, sends its own push notifications, serves a few web
pages, records a set of cameras while nobody is home, and maintains and backs
up itself and two other machines.

It's built from plain bash and Python (standard library where it can be),
systemd units and a handful of containers, with 550+ pytest tests that run
without root, network or hardware.

| Folder | What it is |
| --- | --- |
| [`host-maintenance/`](host-maintenance/) | Weekly maintenance and daily security updates, a pull-based backup chain across three machines with restricted SSH logins, health reports, a service watchdog, self-hosted notifications that wait until the phone is home, login-protected web front-ends, a single-use TLS CA and a Wake-on-LAN page. |
| [`tapo-automation/`](tapo-automation/) | Presence detection (ICMP, TCP and ARP in parallel) that arms Tapo cameras when the house is empty, a supervised `ffmpeg -c copy` recorder that replaced Frigate, and a status page with a timeline of clips. |

## Highlights

- **Backups that can't be turned against the server.** The other machines log
  in with keys whose only command is a [gate](host-maintenance/backup-gate)
  that compares each request against a short allow-list and hands it to
  `rrsync` (rsync's restricted mode). Backup trees are bind-mounted
  `nosuid,nodev,noexec`, so nothing copied from another machine can run.
- **Backups that check themselves.** The backup server pulls hard-linked
  snapshots, raises a churn alarm when a run deletes or rewrites too much
  (what ransomware or a bad cleanup looks like), verifies samples byte for byte
  against the source, re-reads old data for bit rot, and stays on instead of
  powering off when its own health check fails.
- **Silence means healthy.** Every unattended job reports. The other machines
  send no pushes themselves: they write reports that the server reads, so one
  weekly message covers all three, and a missing report is noticed too.
- **Docker that can't bypass the firewall.** Published container ports skip
  ufw, so [a test](host-maintenance/tests/test_docker_ports.py) fails if any
  compose file publishes beyond `127.0.0.1`. Containers the house needs to
  reach run on the host's network, where ufw applies.
- **Notifications that arrive properly.** iOS shows a bare "New message" for a
  push it can't fetch, so [`ntfy-notify`](host-maintenance/ntfy-notify) holds
  messages in a spool while the phone is away and delivers the backlog when it
  is back.
- **Fail-safe defaults.** A broken presence probe counts as "someone is home",
  a half-installed spool sends at once instead of swallowing alerts, and a
  machine that fails its health check stays reachable.
- **A recorder at 2% CPU per camera.** One `ffmpeg -c copy` per camera,
  supervised with per-camera backoff, stall detection and fragmented MP4 that
  stays playable after a power cut, replacing a Frigate container.

## About this copy

This is a sanitized snapshot of two private repositories, published to read,
not as a product. Addresses are examples (`192.168.50.0/24` for the LAN,
`100.64.0.0/24` for the tailnet), keys and serial numbers are placeholders,
camera names are numbers, and notes about the specific deployment are left
out. The scripts assume DietPi's paths (`/home/dietpi`,
`/mnt/dietpi_userdata`).

## Running the tests

Each folder is its own project with its own virtualenv:

```bash
cd host-maintenance && python3 -m venv maint-env && maint-env/bin/pip install -r requirements-dev.txt && maint-env/bin/python -m pytest
```

```bash
cd tapo-automation && python3 -m venv tapo-env && tapo-env/bin/pip install -r requirements-dev.txt && tapo-env/bin/python -m pytest
```

The tests use fakes for everything outside the process: `systemctl`, `docker`,
`smartctl`, `journalctl`, `ffmpeg` (one test uses the real one when it's
installed), the cameras, an ntfy server on localhost, and SSH that behaves like
dropbear.
