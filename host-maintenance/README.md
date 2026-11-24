# host-maintenance

Maintenance, backups, notifications and a few small web front-ends for a
DietPi home server, plus the scripts it coordinates on two other machines.
Plain bash and Python, systemd units, and a pytest suite that runs without
root or network.

> Sanitized copy: addresses are examples, keys are placeholders, and notes
> about the specific deployment are left out. See the top-level README.

## The machines

| Role | Example address | Runs |
| --- | --- | --- |
| Server (always on) | `192.168.50.10`, tailnet `100.64.0.10` | Everything here except the two folders below: Pi-hole + unbound, ntfy, the camera system, the maintenance jobs. |
| Backup server | `192.168.50.30` | [`backup-server/`](backup-server/): off except for a weekly run, woken over the network. |
| Desktop | `192.168.50.20` | [`desktop/`](desktop/): switched on when needed. Nextcloud, Open WebUI, Ollama. |

The server's `dietpi-backup` and the desktop's mirrors end up as versioned
snapshots on the backup server, so every machine's system tree is on a disk
in a different box.

## Contents

| Area | Files |
| --- | --- |
| [Weekly maintenance](#weekly-maintenance) | `system-and-pihole-updater`, `wake-backup-server` + `.service`, `dietpi-backup_inc_exc` |
| [Daily security updates](#daily-security-updates) | `security-upgrades`, `apt/52unattended-upgrades-security-only` |
| [The backup server](#the-backup-server) | `backup-server/` |
| [The desktop](#the-desktop) | `desktop/` |
| [Restricted backup logins](#restricted-backup-logins) | `backup-gate`, `backup-logins/`, `backup-trees/` |
| [Reports and heartbeat](#reports-and-heartbeat) | `machine-reports`, `check-backup-heartbeat` + units |
| [Service watch](#service-watch) | `service-watch` + units |
| [Notifications](#notifications) | `ntfy-notify`, `ntfy/`, `ntfy-spool-flush.*` |
| [Login summary](#login-summary) | `login-summary` |
| [Docker ports and ufw](#docker-ports-and-ufw) | `tests/test_docker_ports.py`, the compose files |
| [HTTPS with a single-use CA](#https-with-a-single-use-ca) | `home-certs` |
| [The wake page](#the-wake-page) | `wake-page` + `.service` |
| [Keeping the journal](#keeping-the-journal) | `archive-journal` + units |
| [Locking down /run/dietpi](#locking-down-rundietpi) | `dietpi-run-private.conf` |

## Weekly maintenance

`system-and-pihole-updater` runs from `cron.weekly`:

1. `dietpi-backup` to a local disk (three rotating copies), then a request to
   wake the backup server. The server reboots a few minutes later, so the
   request is carried across the reboot in a flag file and
   `wake-backup-server.service` sends the Wake-on-LAN packet once the server
   is back up. It pings first: a backup server that was left on would not run
   its cycle, and that goes into the weekly push instead.
2. SMART: overall health, reallocated, pending and uncorrectable sectors,
   temperature, last week's self-test result, then a new short self-test.
3. `dietpi-update` and `apt-get dist-upgrade`.
4. Purge every kernel except the running one and the newest.
5. `pihole -up` and `pihole -g`.
6. Every Docker Compose project is pulled, containers whose image changed are
   recreated, and one that isn't healthy after 90 seconds is a failure.
7. Failed steps are pushed at once. The "all good" summary waits for the
   backup server's report (see [Reports and heartbeat](#reports-and-heartbeat)).
   Then it reboots.

Every APT call runs non-interactively with `--force-confdef --force-confold`,
so a package with a new config file can never leave the job waiting for an
answer in the middle of the night. `apt-get update` is retried. A failing step
is logged, counted and skipped rather than ending the run: the backup is
already taken by then, and skipping the reboot after a kernel upgrade would be
worse. A lock keeps a manual run from overlapping cron, `DRY_RUN=1` prints
every command instead of running it, and `NO_REBOOT=1` stops before the
reboot.

## Daily security updates

`security-upgrades` runs `unattended-upgrade` limited to Debian's security
suite (`apt/52unattended-upgrades-security-only`) and pushes "Security patch
installed" with the package names. It deliberately never says "maintenance":
when the daily and weekly pushes looked alike, a security patch on a Saturday
was read as the weekly job running a day early.

## The backup server

`backup-server/pull-backups.sh` runs as root on the backup server, five
minutes after boot. For each tree (the server's backup and the desktop's two
mirrors) it pulls a timestamped snapshot with `rsync --link-dest` against the
previous one, so unchanged files cost nothing. Then it updates, verifies, and
powers off.

- **Two pointers per tree.** `latest` always advances, so deduplication keeps
  working. `last-complete` only follows snapshots with nothing known missing:
  restore from that one when you need a whole tree.
- **Prunes instead of failing** when free space drops below 50 GB: oldest
  first, round-robin over the trees, never below 8 snapshots per tree, and
  never a snapshot a pointer refers to.
- **rsync exit codes.** 23 (files changed or unreadable mid-copy) is retried
  once; 24 (files vanished) is normal on a live system and only noted.
- **Churn alarms.** More than 20 GB moved in one run, or a snapshot with 20%
  (and at least 1,000) fewer files than the last complete one, is a failure:
  the fingerprint of ransomware or a mass deletion upstream, caught while the
  older snapshots still hold good copies. The alarm names the five directories
  that lost the most files, which usually tells a cleanup from an attack.
- **Heartbeat and report.** It writes a heartbeat to the server right after
  the pulls and a full report as the very last step: per tree the files and
  bytes moved, verification, SMART, free space, the power-off decision, the
  run's log, journal warnings, and checksums of its installed scripts, sudoers,
  firewall and key files. Nobody needs to boot the machine to see how the run
  went.
- **Upgrades that never remove packages.** `apt-get upgrade --with-new-pkgs`
  still takes new kernel packages but never removes anything, the one part of
  `dist-upgrade` that can strand an unattended machine.
- **The health check gates the power-off.** After upgrading it checks that the
  newest kernel has an initramfs and a GRUB entry, the network config and root
  fstab entry exist, the SSH server is enabled with its host key, the script
  still parses, and no unit has failed. If anything is wrong the machine stays
  on: a running broken machine can be fixed over SSH, a powered-off one needs
  a keyboard.
- **Verification in the idle window.** `verify-backups.sh` compares a sample
  of the newest snapshot byte for byte against the live source (over the same
  read-only login), hashes new inodes into a manifest, and re-reads the
  least recently verified data to catch bit rot. Hard links mean each unique
  file is hashed once, however many snapshots share it.
- **Clock first.** Snapshot names come from the clock, so the run waits for
  time sync before naming anything.
- Snapshots belong to root and are bind-mounted onto themselves
  `nosuid,nodev,noexec` (`home-dietpi-backups.mount`): they hold every
  machine's setuid programs, old versions included.
- `wol.service` re-arms Wake-on-LAN at every boot, since network drivers don't
  keep it across a power cycle.

```bash
# preview on any machine, no network or root needed
DRY_RUN=1 BACKUP_ROOT=/tmp/bs LOG=/tmp/bs.log bash backup-server/pull-backups.sh
```

## The desktop

`desktop/dietpi-backup-sync.sh` runs as root on the desktop on Saturday nights
and two minutes after every boot, and does its work once per week:

1. A local `dietpi-backup`.
2. That system tree, and the live Nextcloud data (in maintenance mode, with a
   trap that lifts it if the script dies), pushed to the server.
3. Its own updates: DietPi, APT, the Open WebUI image, and Nextcloud's point
   releases through Nextcloud's own updater, but only when this run's local
   backup succeeded, since that backup is the way back. A new major version is
   reported, not installed.
4. Smoke tests: Nextcloud answers and is out of maintenance mode, Open WebUI
   answers, and Ollama really uses the GPU (it asks Ollama which compute
   library it found, rather than trusting `nvidia-smi`).
5. A light SMART check of every disk, with per-serial allowances for known
   reallocated sectors, so a known count only warns when it grows.
6. A report to the server.

"Once per week" is counted from Saturday 23:00 rather than per ISO week, so a
catch-up run on a Tuesday doesn't use up the week and skip the Saturday after
it. The run is marked done only when both pushes succeeded, so a failed run
retries at the next boot. It also checks Nextcloud's own setup checks and its
log, minus a short list of known-harmless messages, and reports the rest.

Ollama listens only on the docker0 address (`ollama-override.conf`), the one
path the Open WebUI container uses, so nothing else reaches it.

## Restricted backup logins

The backup server and the desktop each log in to the server with an account of
their own, `backup-server` and `desktop`, and each key can do exactly its
machine's job. Before this, both logged in as `dietpi` and ran `sudo rsync`
with no password and no limits. rsync as root can overwrite any file or start
any program, so every one of those logins was root.

Each key's only command is `backup-gate`, forced in its `authorized_keys`
together with `restrict`. The gate compares what the other machine asks for
against a short list, and refuses and logs anything else:

- `backup-server` (`backup-gate pull`) may read the three backup trees as root
  through `rrsync -ro /`, write its heartbeat and reports into its own log
  folder, ask the uptime, and read its own install files from this repo.
- `desktop` (`backup-gate push`) may write its two mirrors as root through
  `rrsync -wo` on each, write its reports, and read its own install files.

`rrsync` is rsync's own restricted server: it checks every option and path
against its mode and directory. The gate adds that each request names exactly
one allowed tree, since a second source in front of an allowed one would be
read as root too: in rsync's server command every word after the first lone
`.` is a path, so the gate wants exactly one `.`, second to last. And each
`rrsync` is confined to the one folder the request is for, with the path made
relative to it, so even a request that slipped past the gate couldn't leave
that folder. Heartbeats and reports are written by the gate itself, from
stdin, to fixed paths: the command the other machine sends is only compared,
never run. Those `rrsync` calls, with fixed arguments, are the only sudo the
two accounts get (`backup-logins/sudoers`). Dropbear has no `from=` option for
`authorized_keys`, so the gate checks the client's address itself.

The accounts have no password (`*`, not `!`: dropbear treats a `!` account as
locked for keys too), `/bin/sh` only so dropbear can start the forced command,
and home folders owned by root, so neither can change its own
`authorized_keys`. The backup trees on the server are bind-mounted
`nosuid,nodev,noexec` (`backup-trees/*.mount`), so the setuid programs inside
them are plain files.

The switch kept the backups working at every step: `backup-logins/install`
added the new way next to the old one, each machine switched and passed a real
run, and only then did `backup-logins/finish` take out the old passwordless
rsync.

## Reports and heartbeat

The backup server is awake for about half an hour a week, so it can't warn
anyone that it has died: a failed upgrade, a dead disk and an unplugged cable
all look like nothing at all. The server is always on, so the alarm lives
there. `check-backup-heartbeat` runs daily and pushes when the heartbeat is
more than nine days old.

`machine-reports` reads the reports the other two machines write, every five
minutes:

- **One weekly push for all three machines**, as soon as the backup server's
  report is in (or 2 h 15 min after the weekly job without it):

  ```
  Weekly maintenance OK
  Server: backup, updates and Pi-hole done, containers updated: ntfy, disk PASSED.
  Backup server: 3 backups OK, 1.9 GB new, verify OK, disk OK, 307 GB free.
  Desktop: last on Sat 26 Sep 23:06 UTC (3 h ago): backed up and updated, checks OK.
  ```

- Any other report is pushed only when it shows a problem.
- Reports are written by other machines and this runs as root, so a report
  value only reaches bash arithmetic as a checked whole number (bash would
  evaluate `PATH[$(cmd)]` there). A test feeds that through every field.
- **Problems** are the run's WARNING, ERROR, CHURN and FAILED lines, an
  unhealthy disk, low space on the backup disk, a pulled copy more than eight
  days old, failed units, a backup server that stayed on, a desktop that
  hasn't run for 30 days, and installed scripts whose checksum differs from
  this repo's, which is how the server notices that a machine still runs an
  old version.

## Service watch

`service-watch` runs every two minutes and pushes when something breaks, and
once more when it's fixed: services and failed units, Pi-hole blocking and
unbound resolving, the internet, Tailscale, the containers, and free disk
space.

- A problem has to be seen on two rounds in a row (three for the internet and
  Tailscale, half an hour for Pi-hole not blocking, since pausing it is
  normal), so a service restarting itself is not news.
- Whatever broke in the same round shares one push, and checks that depend on
  something already down are skipped, so a dead Pi-hole is one push, not three.
- Nothing is checked while maintenance runs, since that restarts services on
  purpose.
- After an unexpected shutdown it pushes once the machine is back:
  `service-watch-shutdown.service` marks every clean shutdown, and a missing
  mark at boot means a power cut or a freeze.

## Notifications

Every unattended job pushes through a self-hosted [ntfy](https://ntfy.sh)
server, with `ntfy-notify` as the shared helper:

```
ntfy-notify [-p min|low|default|high|urgent] [-t tag,tag] [-T topic] [-c url] TITLE [MESSAGE]
```

It reads `/etc/ntfy-notify.conf` (parsed, never sourced), sends with a timeout
and retries, and is silent on success. `NTFY_NOTIFY_DRY_RUN=1` prints the
request instead.

**Home only, by design.** Nothing can be fixed from outside the house, so the
server's `base-url` is its LAN address and the phone only reaches it at home.
iOS can't poll in the background, so the server sends an empty "poll now" ping
through ntfy.sh's relay to Apple, and the app then fetches the message from
the home server. The relay's channel is a hash of `base-url` and topic, and
anyone can subscribe to it, so the topic should be a long random name: the
pings carry no content, but their timing does.

**Held until the phone is home.** A push that arrives while the phone is away
shows as a bare "New message", because the app can't reach the server, and
Apple gives it one attempt. So `ntfy-notify` checks first whether the phone is
on the network (ping, then a TCP connect, then the ARP table, the same probe
as the camera system) and otherwise writes the message to a spool. A timer
delivers the spool, oldest first and stamped with the time each was raised,
once the phone is back.

- A send that fails because the server is down is held and retried; a 4xx is
  the server's final answer and is dropped.
- Held messages expire after a week and the spool is capped at 200, so a long
  holiday doesn't end in a wall of stale alerts.
- An empty spool costs nothing: the timer returns before probing.
- **Fail-safe**: a missing or unwritable spool sends at once instead of
  swallowing the alert.
- The flush runs as an unprivileged user, not root: the spool is writable by
  that user's group, and whatever reads a folder like that and acts on it must
  not be root. Root's jobs write into it with `noclobber`, so a planted symlink
  is never followed, and the flush locks the folder itself rather than a lock
  file inside it.

**The server** (`ntfy/`) requires a login for everything, runs on the host's
network so ufw applies, and falls back to a public resolver for its own
lookups, so a dead Pi-hole can't silence the push that reports it. Docker's
journald driver files stderr as `err`, so ntfy's once-a-minute stats line
arrived in the journal as an error 1,400 times a day; a `log-level-overrides`
entry silences just that line.

One debugging note worth keeping: the iOS app registers through Firebase, and
common Pi-hole blocklists cover the endpoints it needs. The server accepts
every message, the relay accepts every ping, nothing looks wrong anywhere, and
the phone simply isn't subscribed. Allowing
`firebaseinstallations.googleapis.com`, `fcmtoken.googleapis.com` and
`fcm.googleapis.com` fixes it.

## Login summary

`login-summary` replaces the DietPi banner with one screen at SSH login: load,
memory, temperature and disk; SMART; LAN and Tailscale; every service with a
tick or a cross; a live check that Pi-hole blocks and unbound resolves; the
containers; the camera system; pending updates; the last weekly run and
backups; and notices held for the phone. It needs no root, reads only files and
fast local commands, takes under half a second, and wraps to the terminal
width. Anything off is yellow or red; the rest stays quiet.

## Docker ports and ufw

Docker publishes a container port by rewriting the destination in the `nat`
table, before ufw's input rules are consulted, so a published port is open to
anything that can reach the machine, whatever ufw says. So nothing here is
published beyond `127.0.0.1`, and
[`tests/test_docker_ports.py`](tests/test_docker_ports.py) fails if a compose
file does. Containers the house has to reach (`ntfy`) run with `network_mode: host` and listen like any other program, so
ufw decides for them, and `sudo ufw status` stays the complete list of what
other devices can reach. Every rule has a comment, so the list reads on its
own.

An earlier attempt filled Docker's `DOCKER-USER` chain instead, matching each
packet's source. `FORWARD` carries the replies too, still from the container's
address, so every reply fell into the final DROP, and for eleven days the
containers only answered the server itself. No check showed it, because the
server's own connections to a published port never pass through `FORWARD`.
The lesson: test firewall changes from another device.

## HTTPS with a single-use CA

`home-certs` makes one certificate for each machine's addresses and names,
signed by a private CA whose key it deletes the moment they're made (the key
only ever exists in RAM-backed `/tmp`). Each device trusts the CA once and with
it every home site, and the CA can never sign anything else. The price is that
renewal means a new CA, trusted again. The certificates last 825 days, the most
iOS accepts, and the wake page warns in the last 30.

## The wake page

`wake-page` shows whether the home's computers are on and for how long, and
wakes them, from anywhere on the tailnet. Wake-on-LAN is a broadcast and
Tailscale doesn't carry broadcasts, so the always-on server sends it. One
Python file, standard library only, running as `dietpi` without root.

- **What's on**: ping, or for a machine whose firewall ignores ping, the
  kernel's ARP table (a machine that's on answers ARP whatever its firewall
  says). Two missed checks in a row before "on" turns to "off".
- **Devices** are added, edited and removed on the page, with the MAC address
  looked up from the ARP table when left empty.
- **Shut down**, for machines set up for it: the page logs in over SSH with
  its own key and sends no command. The target's `authorized_keys` forces the
  only one that key may run, `sudo -n /sbin/shutdown -h now`. If the server
  were ever taken over, switching the desktop off is the most that key could
  do.
- **Security**: HTTPS only; the password kept as a scrypt hash, one try at a
  time with a delay; a signed `HttpOnly`, `Secure` cookie; forms accepted only
  from the page's own origin, because browsers treat every port of a host as
  one site, so a script-injection bug in any other page on the server could
  otherwise press these buttons; a Content-Security-Policy that allows no
  scripts; and nothing reaches a command but a validated IP or MAC address,
  never a shell.

## Keeping the journal

DietPi keeps `/var/log` in a 50 MB tmpfs, so the journal dies with the
machine, which is exactly when you want to read it. Creating
`/var/log/journal` would be unsafe: DietPi's hourly log cleaner truncates
everything under `/var/log`. So `archive-journal` copies new entries to the
data disk every 15 minutes and once on the way down, using
`journalctl --cursor-file` to resume exactly where the last run stopped, one
file per day, kept 30 days.

## Locking down /run/dietpi

DietPi ships `/run/dietpi` world-writable, and its login banner runs files from
it in every interactive shell, root's included. So any local account or
service could leave a file for the next root shell to run.
`dietpi-run-private.conf` (a tmpfiles.d override) sets it to `0755` at every
boot. It's a separate file because `dietpi-update` would overwrite an edit to
DietPi's own.

## Tests

```bash
python3 -m venv maint-env && ./maint-env/bin/pip install -r requirements-dev.txt
```

```bash
./maint-env/bin/python -m pytest
```

Some of what they cover:

- The weekly and backup jobs in dry-run mode: the kernel purge never selects
  the running or newest kernel, pruning removes the oldest first and never a
  pointer's target, the wake-up is deferred when the boot unit exists.
- `backup-gate` against a fake `rrsync`, and end to end with the real `rsync`
  and `rrsync` through a fake `ssh` that behaves like dropbear: every request
  the two machines really send gets through, and a shell, a second source, a
  path outside the trees, a push anywhere but the mirrors, a report under
  another name, and a key from the wrong address are all refused.
- `service-watch` against fake `systemctl`, `docker`, `dig`, `tailscale`,
  `ping`, `df` and `pgrep`: the two-round wait, one push for several problems,
  dependent checks staying quiet, a power cut told apart from a clean
  shutdown.
- `ntfy-notify` against a fake ntfy server on localhost, and its holding logic
  with the real presence probe pointed at loopback ("home") and an unroutable
  address ("away").
- The login summary against fake state, every line checked to fit the
  terminal width.
- No compose file publishes a port beyond `127.0.0.1`.
