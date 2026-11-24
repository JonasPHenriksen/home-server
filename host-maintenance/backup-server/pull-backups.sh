#!/bin/bash
#
# pull-backups.sh - runs at boot on the backup server (192.168.50.30).
#
# Installed as /usr/local/sbin/ssh-backups-cycle.sh and started as root by
# ssh-backups.service, which ssh-backups.timer fires five minutes after boot
# (that delay also gives NTP time to correct the clock, which fake-hwclock
# restores to the last shutdown time at boot; snapshot names use the clock).
#
# The laptop (192.168.50.10) wakes this machine after its own weekly backup.
# This script pulls three trees from the laptop into timestamped snapshots
# that share unchanged files through hard links (--link-dest), updates the
# system, and powers off again when nobody is logged in.
#
# Compared with the original:
#   * waits for the laptop to be reachable and up for a while first (it reboots
#     shortly after sending the wake-up),
#   * prunes the oldest snapshots when the disk is low instead of aborting,
#   * retries once on rsync exit 23 (files changed under it) and keeps the
#     snapshot with a warning if it happens again, instead of discarding it,
#   * no compression on the LAN, I/O timeouts so a vanished source cannot hang
#     the run, and the desktop's re-downloadable container images excluded,
#   * non-interactive APT with a retry on `update`, failures counted and
#     reported instead of ignored, a lock so two runs cannot overlap, and a
#     log line when a run is interrupted (which used to leave no trace).
#
# This machine deliberately runs nothing beyond what the backup needs, not even
# the push notifications the laptop sends: it is awake about half an hour a
# week, and every extra moving part is one more thing that can strand a cycle.
# Instead, as the very last thing before the power-off, the whole run goes to
# the laptop as a report (send_report): this run's log, the numbers per tree,
# verification, disk health and a short SMART self-test started at the
# beginning, snapshot counts, and checksums of the installed scripts. Nobody
# has to boot this machine to see what happened; the laptop reads the report
# and pushes what matters. It travels the way the heartbeat does, this machine
# writing to the laptop, never the other way round.
#
# The run also waits for NTP before naming anything: fake-hwclock sets the
# clock back to the last shutdown at every boot, and only timesyncd puts it
# right.
#
# DRY_RUN=1 prints commands instead of running them. Functions can be sourced
# for tests: `source pull-backups.sh` does not run main.

set -u
export DEBIAN_FRONTEND=noninteractive   # never wait for a dpkg prompt

# --- CONFIGURATION ---
SOURCE_HOST="${SOURCE_HOST:-192.168.50.10}"
SOURCE_USER="backup-server"   # its own login on the laptop, locked to backup-gate pull
SSH_KEY="${SSH_KEY:-/root/.ssh/id_ed25519_backup-server}"
DRIVE="${DRIVE:-/dev/disk/by-id/ata-ST1000DM003-1CH162_EXAMPLE1}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/dietpi/backups}"
# Root appends to this every run, so it must live in a root-owned directory:
# in dietpi's home, dietpi could swap it for a symlink and redirect the writes.
# Not under /var/log either - that is a tmpfs the hourly dietpi-logclear zeroes
# (see README "Keeping the journal"). /var/lib is root-owned and persistent.
LOG="${LOG:-/var/lib/ssh-backups/cycle.log}"
MAX_LOG_BYTES=$((5 * 1024 * 1024))

MIN_FREE_GB=50            # prune old snapshots below this, abort if still below
KEEP_MIN_SNAPSHOTS=8      # never prune a server below this many snapshots
WAIT_FOR_SOURCE_SECS=1200 # how long to wait for the laptop to come (back) up
SOURCE_MIN_UPTIME=120     # and to have been up this long (past its reboot)
RSYNC_TIMEOUT=900         # seconds without I/O before rsync gives up
IDLE_BEFORE_POWEROFF=900

# One run should never move this much unless something is wrong upstream.
CHURN_XFER_ALARM_GB=20    # transferred more than this in a single run
# Mass deletion is measured by comparing the new snapshot's file count with
# the last complete one. rsync's own "deleted files" counter cannot be used:
# every snapshot is built in a fresh directory, so --delete never removes a
# thing and the counter is always zero. 10 % was too tight: on 2026-10-11 the
# laptop's routine kernel swap set it off (one kernel's modules are about a
# tenth of that tree's files).
CHURN_DELETE_PCT=20       # alarm when the new snapshot has this % fewer files...
CHURN_DELETE_MIN=1000     # ...and at least this many fewer (no noise on small trees)

# Where the cycle's status file is written on the source host.
HEARTBEAT_REMOTE="${HEARTBEAT_REMOTE:-/mnt/dietpi_userdata/logs/backup-server/heartbeat}"
# And the full report of each run; the laptop keeps the history.
REPORT_REMOTE_DIR="${REPORT_REMOTE_DIR:-/mnt/dietpi_userdata/logs/backup-server}"

# timesyncd creates this once the clock has been synchronised.
TIMESYNC_FLAG="${TIMESYNC_FLAG:-/run/systemd/timesync/synchronized}"
CLOCK_WAIT_SECS=600

# Installed copies of this repository's files, checksummed in every report so
# the laptop notices when this machine and the repository drift apart.
INSTALLED_FILES=(/usr/local/sbin/ssh-backups-cycle.sh /usr/local/sbin/verify-backups.sh
                 /etc/systemd/system/ssh-backups.service /etc/systemd/system/ssh-backups.timer
                 /etc/systemd/system/wol.service)

# Integrity verification, run in the idle window at the end of a cycle.
VERIFY_TOOL="${VERIFY_TOOL:-/usr/local/sbin/verify-backups.sh}"
VERIFY_MANIFEST="${VERIFY_MANIFEST:-/var/lib/backup-verify/inodes.tsv}"
VERIFY_SOURCE_SAMPLE=40   # files per tree compared against the live source

# -y so nothing waits for an answer, and keep our own config files when a package
# ships a new one (the packaged version lands next to it as *.dpkg-dist).
APT_OPTS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
APT_UPDATE_TRIES=3
APT_UPDATE_WAIT=30

FAILURES=0
CURRENT_STEP="startup"
FINISHED=0
HEALTHY=1

# What the report says about this run, filled in as it goes.
declare -A T_RESULT=() T_XFER_FILES=() T_XFER_BYTES=() T_TOTAL=() T_SECONDS=() \
           T_FILES=() T_FILES_BEFORE=() T_SOURCE_TIME=()
CLOCK_SYNCED=unknown
CLOCK_WAITED=0
SOURCE_UPTIME=""
VERIFY_SOURCE=skipped
VERIFY_SCRUB=skipped
DECISION=""
RUN_TIMESTAMP=""
RUN_STARTED=0
LOG_START_LINE=0

DRY_RUN="${DRY_RUN:-0}"

# "NAME|REMOTE_PATH|EXCLUDES (comma separated rsync patterns)"
SERVERS=(
  "laptop|/mnt/dietpi-backup/data/|"
  "desktop|/mnt/dietpi-desktop-backup/|/var/lib/containerd"
  "nextcloud|/mnt/dietpi-desktop-nextcloud-backup/|"
)

SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new
          -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4)

# --- helpers ---------------------------------------------------------------

log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG"
}

run() {
    if [ "$DRY_RUN" = 1 ]; then echo "DRY: $*"; else "$@"; fi
}

# Run one step. A failure is logged and counted, but never aborts the cycle:
# the machine still has to update itself and power off again.
run_step() {
    local name="$1"; shift
    CURRENT_STEP="$name"
    # `&&` (not `if`) so $? below is the failed command's status: an `if` block
    # whose condition fails and which has no else branch itself returns 0.
    run "$@" && return 0
    local rc=$?
    log_message "WARNING: $name failed (rc=$rc), continuing"
    FAILURES=$((FAILURES + 1))
    return "$rc"
}

# A single mirror hiccup must not cost the week's updates.
apt_update() {
    local try
    CURRENT_STEP="apt-get update"
    for try in $(seq 1 "$APT_UPDATE_TRIES"); do
        if run sudo apt-get update -q; then
            return 0
        fi
        if [ "$try" -lt "$APT_UPDATE_TRIES" ]; then
            log_message "apt-get update failed (attempt $try/$APT_UPDATE_TRIES), retrying in ${APT_UPDATE_WAIT}s..."
            [ "$APT_UPDATE_WAIT" -gt 0 ] && sleep "$APT_UPDATE_WAIT"
        fi
    done
    log_message "WARNING: apt-get update failed $APT_UPDATE_TRIES times, skipping the upgrade"
    FAILURES=$((FAILURES + 1))
    return 1
}

# A cycle cut short by a power-off used to leave nothing in the log.
on_exit() {
    [ "$FINISHED" = 1 ] && return
    log_message "ABORTED during: $CURRENT_STEP (interrupted or shut down)"
}

rotate_log() {
    if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt "$MAX_LOG_BYTES" ]; then
        mv -f "$LOG" "$LOG.1"
        log_message "Log rotated (previous log saved as $(basename "$LOG").1)"
    fi
}

free_gb() {
    if [ -n "${FREE_GB_OVERRIDE:-}" ]; then echo "$FREE_GB_OVERRIDE"; return; fi
    echo $(( $(df --output=avail "$BACKUP_ROOT" | tail -1) / 1024 / 1024 ))
}

# Timestamped snapshot directories of one server, oldest first.
snapshots_of() {
    find "$1" -maxdepth 1 -mindepth 1 -type d -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_*' -printf '%f\n' 2>/dev/null | sort
}

# Delete the oldest snapshots, round-robin over the servers, until free space is
# back above MIN_FREE_GB. Never touches the newest KEEP_MIN_SNAPSHOTS of a
# server or the snapshot `latest` points at. Sets PRUNED_COUNT.
PRUNED_COUNT=0
prune_snapshots() {
    local round name dir victim latest_target complete_target progress
    PRUNED_COUNT=0
    for round in $(seq 1 200); do
        [ "$(free_gb)" -lt "$MIN_FREE_GB" ] || break
        progress=0
        for entry in "${SERVERS[@]}"; do
            IFS="|" read -r name _ _ <<< "$entry"
            dir="$BACKUP_ROOT/$name"
            [ -d "$dir" ] || continue
            [ "$(snapshots_of "$dir" | wc -l)" -gt "$KEEP_MIN_SNAPSHOTS" ] || continue
            latest_target=$(basename "$(readlink -f "$dir/latest" 2>/dev/null)" 2>/dev/null)
            complete_target=$(basename "$(readlink -f "$dir/last-complete" 2>/dev/null)" 2>/dev/null)
            # The oldest snapshot that is neither pointer's target. Taking only
            # the very oldest and skipping the tree when it was protected meant
            # a tree whose oldest snapshot is last-complete was never pruned.
            victim=""
            while IFS= read -r cand; do
                if [ "$cand" != "$latest_target" ] && [ "$cand" != "$complete_target" ]; then
                    victim="$cand"; break
                fi
            done < <(snapshots_of "$dir")
            [ -n "$victim" ] || continue
            log_message "PRUNE: $name/$victim ($(free_gb) GB free < ${MIN_FREE_GB} GB)"
            run rm -rf "$dir/$victim"
            PRUNED_COUNT=$((PRUNED_COUNT + 1))
            progress=1
            [ "$(free_gb)" -ge "$MIN_FREE_GB" ] && break
        done
        [ "$progress" = 1 ] || break
        [ "$DRY_RUN" = 1 ] && break   # nothing is really removed, one pass is enough
    done
}

# Wait until the laptop answers over SSH and has been up for SOURCE_MIN_UPTIME.
wait_for_source() {
    if [ "$DRY_RUN" = 1 ]; then echo "DRY: wait for $SOURCE_HOST (ssh uptime probe)"; return 0; fi
    local deadline=$(( $(date +%s) + WAIT_FOR_SOURCE_SECS )) up
    while [ "$(date +%s)" -lt "$deadline" ]; do
        up=$(ssh "${SSH_OPTS[@]}" "$SOURCE_USER@$SOURCE_HOST" 'cut -d. -f1 /proc/uptime' 2>/dev/null)
        if [[ "$up" =~ ^[0-9]+$ ]]; then
            if [ "$up" -ge "$SOURCE_MIN_UPTIME" ]; then
                SOURCE_UPTIME=$up
                log_message "Source $SOURCE_HOST reachable, up for ${up}s."
                return 0
            fi
            log_message "Source $SOURCE_HOST just booted (${up}s), waiting for it to settle..."
        fi
        sleep 20
    done
    log_message "CRITICAL: source $SOURCE_HOST not reachable within ${WAIT_FOR_SOURCE_SECS}s."
    return 1
}

disk_health() {
    if [ "$DRY_RUN" = 1 ]; then echo "DRY: sudo smartctl -H -A $DRIVE"; return 0; fi
    local out
    out=$(sudo smartctl -H -A "$DRIVE" 2>&1)
    if ! echo "$out" | grep -q "PASSED"; then
        log_message "CRITICAL: Disk health check FAILED for $DRIVE. Aborting."
        return 1
    fi
    log_message "Disk health: PASSED, reallocated=$(echo "$out" | awk '$1==5{print $10}') pending=$(echo "$out" | awk '$1==197{print $10}') hours=$(echo "$out" | awk '$1==9{print $10}')"
}

# The drive had never run a self-test since it was new. A short one takes two
# minutes, runs inside the drive alongside normal I/O, and its result is in the
# drive's self-test log by the time the report is written.
start_selftest() {
    if run sudo smartctl -t short "$DRIVE"; then
        log_message "SMART: short self-test started"
    else
        log_message "WARNING: SMART short self-test could not be started"
    fi
}

# fake-hwclock sets the clock back to the last shutdown at every boot, and the
# snapshot names, the log and the report all use it. Wait for timesyncd, but do
# not make the backup depend on it: an unsynchronised run still backs up.
wait_for_clock() {
    if [ "$DRY_RUN" = 1 ]; then echo "DRY: wait for $TIMESYNC_FLAG"; CLOCK_SYNCED=yes; return 0; fi
    local waited=0
    while [ ! -e "$TIMESYNC_FLAG" ] && [ "$waited" -lt "$CLOCK_WAIT_SECS" ]; do
        sleep 10; waited=$((waited + 10))
    done
    CLOCK_WAITED=$waited
    if [ -e "$TIMESYNC_FLAG" ]; then
        CLOCK_SYNCED=yes
    else
        CLOCK_SYNCED=no
        log_message "WARNING: clock still not synchronised after ${CLOCK_WAIT_SECS}s; snapshot names may carry the wrong date"
    fi
}


# --- health check ----------------------------------------------------------

# Everything here must still be true for this machine to come back on its own
# SSH connections that mean someone is logged in, printed as "<logins> <claude>".
# Each connection is a dropbear whose parent is the listening dropbear. The
# Claude app's own connection doesn't count: it runs nothing but the app's
# bridge (from ~/.claude/remote/) and an sftp-server, and the app reconnects by
# itself after every boot, which kept this machine on until someone closed the
# app. Anything else counts - a login shell above all, from any SSH client,
# including the app's terminal, which connects like one.
# Programs are matched by what they run, not by words in their arguments: a
# `sudo bash -c '... pgrep dropbear'` is not a connection.
ssh_sessions() {
    ps -eo pid=,ppid=,args= | awk '
        function exe(p,   w) { if (!(p in cmd)) return ""; split(cmd[p], w, " "); return w[1] }
        { p = $1; parent[p] = $2; $1 = $2 = ""; sub(/^ +/, ""); cmd[p] = $0 }
        END {
            for (c in cmd) {
                if (exe(c) !~ /(^|\/)dropbear$/ || exe(parent[c]) !~ /(^|\/)dropbear$/) continue
                app = 0; other = 0
                for (k in cmd) if (parent[k] == c) {
                    if (exe(k) ~ /\/\.claude\/remote\//) app = 1
                    else if (exe(k) !~ /(^|\/)sftp-server$/) other = 1
                }
                if (app && !other) claude++; else logins++
            }
            print logins + 0, claude + 0
        }'
}

# after a reboot. It runs after the weekly upgrade and gates the power-off:
# a box that is broken but still running can be fixed over SSH, whereas the
# same box powered off needs physical access.
HEALTH_PROBLEMS=0

hc_fail() {
    HEALTH_PROBLEMS=$((HEALTH_PROBLEMS + 1))
    log_message "HEALTH: FAIL - $1"
}

health_check() {
    local newest failed
    HEALTH_PROBLEMS=0
    CURRENT_STEP="health check"
    if [ "$DRY_RUN" = 1 ]; then
        # The checks below inspect the machine they run on; a dry run elsewhere
        # would report that machine's kernel, key and grub, not this one's.
        echo "DRY: health check (kernel+initramfs in grub, static eth0, fstab root, dropbear, pull key, failed units)"
        return 0
    fi

    # A kernel with no initramfs, or one grub does not know about, is the
    # classic way an unattended upgrade produces a machine that never returns.
    newest=$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's|.*/vmlinuz-||' | sort -V | tail -1)
    if [ -z "$newest" ]; then
        hc_fail "no kernel present in /boot"
    else
        [ -s "/boot/initrd.img-$newest" ] || hc_fail "kernel $newest has no initramfs"
        # Distinguish "grub.cfg says nothing about this kernel" from "grub.cfg
        # could not be read at all" -- reporting the second as the first sends
        # you looking for the wrong problem.
        if [ ! -r /boot/grub/grub.cfg ]; then
            hc_fail "/boot/grub/grub.cfg is missing or unreadable"
        elif ! grep -q "vmlinuz-$newest" /boot/grub/grub.cfg; then
            hc_fail "kernel $newest is not in grub.cfg"
        fi
    fi

    # Network and root filesystem: without these it boots to nothing reachable.
    grep -q 'iface eth0 inet static' /etc/network/interfaces 2>/dev/null \
        || hc_fail "static eth0 config missing from /etc/network/interfaces"
    grep -qE '^[[:blank:]]*UUID=[^[:blank:]]+[[:blank:]]+/[[:blank:]]' /etc/fstab \
        || hc_fail "root filesystem entry missing from /etc/fstab"

    # SSH is the only way in.
    systemctl is-enabled dropbear >/dev/null 2>&1 || hc_fail "dropbear is not enabled"
    [ -s /etc/dropbear/dropbear_ed25519_host_key ] || hc_fail "dropbear host key missing"

    # The backup must still be able to run next week.
    bash -n "$0" 2>/dev/null || hc_fail "this script no longer parses"
    [ -r "$SSH_KEY" ] || hc_fail "pull key $SSH_KEY is unreadable"

    failed=$(systemctl --failed --no-legend --no-pager 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
    [ -z "$failed" ] || hc_fail "failed units: $failed"

    if [ "$HEALTH_PROBLEMS" -eq 0 ]; then
        log_message "HEALTH: all checks passed"
        return 0
    fi
    log_message "CRITICAL: $HEALTH_PROBLEMS health check(s) failed - see HEALTH lines above"
    return 1
}


# --- heartbeat -------------------------------------------------------------

# Record the outcome of this cycle on the source host. Nothing here can warn
# anyone that this machine died, because a dead machine sends no warnings and
# this one is powered off roughly 99% of the week. The laptop is always on, so
# it is the only place a "the backup stopped happening" alarm can live.
send_heartbeat() {
    local status="$1" payload
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY: heartbeat ($status) -> $SOURCE_HOST:$HEARTBEAT_REMOTE"
        return 0
    fi
    payload=$(printf 'last_cycle=%s\nstatus=%s\nfailures=%s\nfree_gb=%s\nhost=%s\n' \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$status" "$FAILURES" "$(free_gb)" "$(hostname)")
    # Written to a temp name and renamed, so the laptop never reads a half file.
    if printf '%s\n' "$payload" | ssh "${SSH_OPTS[@]}" "$SOURCE_USER@$SOURCE_HOST" \
         "cat > '$HEARTBEAT_REMOTE.tmp' && mv -f '$HEARTBEAT_REMOTE.tmp' '$HEARTBEAT_REMOTE'" 2>/dev/null
    then
        log_message "Heartbeat sent to $SOURCE_HOST ($status, $FAILURES failure(s))"
    else
        # Not counted as a cycle failure: the laptop will notice the silence by
        # itself, which is the behaviour we want anyway.
        log_message "WARNING: could not send heartbeat to $SOURCE_HOST"
    fi
}


# --- report ----------------------------------------------------------------

# A probe of this machine's state for the report. Dry runs do not look: the
# machine they run on is not this one. A probe that fails prints nothing.
probe() {
    if [ "$DRY_RUN" = 1 ]; then echo "(dry run)"; return 0; fi
    "$@" 2>/dev/null
}

this_run_log() { tail -n +"$(( LOG_START_LINE + 1 ))" "$LOG" 2>/dev/null; }

iso() { [ -n "${1:-}" ] && date -u -d "@$1" '+%Y-%m-%dT%H:%M:%SZ'; }

# Everything about this run, for the laptop. key=value lines first, which the
# laptop parses, then readable sections. It must work on a sick machine too, so
# nothing in here may stop it.
build_report() {
    local now smart selftest entry t latest complete snaps
    now=$(date +%s)
    smart=$(probe sudo smartctl -H -A -l selftest "$DRIVE")
    selftest=$(awk '/^# 1 / {$1 = $2 = ""; sub(/^ +/, ""); print; exit}' <<< "$smart")

    echo "# backup server report, format 1"
    echo "host=$(hostname)"
    echo "run=$RUN_TIMESTAMP"
    echo "started=$(iso "$RUN_STARTED")"
    echo "finished=$(iso "$now")"
    echo "booted=$(iso $(( now - $(cut -d. -f1 /proc/uptime) )))"   # not `who -b`: see wait_for_clock
    echo "clock_synced=$CLOCK_SYNCED"
    echo "clock_waited=$CLOCK_WAITED"
    echo "source_uptime=$SOURCE_UPTIME"
    echo "status=$([ "$FAILURES" -eq 0 ] && echo ok || echo failed)"
    echo "failures=$FAILURES"
    echo "health=$([ "$HEALTHY" = 1 ] && echo ok || echo failed)"
    echo "decision=$DECISION"
    echo "free_gb=$(free_gb)"
    if [ -z "$smart" ]; then echo "disk_health=unavailable"
    elif grep -q PASSED <<< "$smart"; then echo "disk_health=PASSED"
    else echo "disk_health=FAILED"; fi
    echo "disk_reallocated=$(awk '$1 == 5 {print $10}' <<< "$smart")"
    echo "disk_pending=$(awk '$1 == 197 {print $10}' <<< "$smart")"
    echo "disk_uncorrectable=$(awk '$1 == 198 {print $10}' <<< "$smart")"
    echo "disk_hours=$(awk '$1 == 9 {print $10}' <<< "$smart")"
    echo "disk_temp=$(awk '$1 == 194 {print $10}' <<< "$smart")"
    echo "selftest=$selftest"
    echo "verify_source=$VERIFY_SOURCE"
    echo "verify_scrub=$VERIFY_SCRUB"
    echo "upgradable=$(probe apt list --upgradable | grep -c 'upgradable from')"
    echo "failed_units=$(probe systemctl --failed --no-legend --plain | grep -c .)"
    # The router's IGMP queries are blocked and logged every 15-30 s; left in,
    # they would bury every warning that means something.
    echo "journal_warnings=$(probe journalctl -b -p warning --no-pager -q | grep -v 'UFW BLOCK' | grep -c .)"
    echo "wake_on=$(probe ethtool eth0 | awk '$1 == "Wake-on:" {print $2}')"
    for entry in "${SERVERS[@]}"; do
        IFS="|" read -r t _ _ <<< "$entry"
        snaps=$(snapshots_of "$BACKUP_ROOT/$t")
        latest=$(readlink "$BACKUP_ROOT/$t/latest" 2>/dev/null)
        complete=$(readlink "$BACKUP_ROOT/$t/last-complete" 2>/dev/null)
        echo "tree_${t}=${T_RESULT[$t]:-skipped}"
        echo "tree_${t}_files_transferred=${T_XFER_FILES[$t]:-}"
        echo "tree_${t}_bytes_transferred=${T_XFER_BYTES[$t]:-}"
        echo "tree_${t}_total_bytes=${T_TOTAL[$t]:-}"
        echo "tree_${t}_seconds=${T_SECONDS[$t]:-}"
        echo "tree_${t}_files=${T_FILES[$t]:-}"
        echo "tree_${t}_files_before=${T_FILES_BEFORE[$t]:-}"
        echo "tree_${t}_source_time=$(iso "${T_SOURCE_TIME[$t]:-}")"
        echo "tree_${t}_snapshots=$(grep -c . <<< "$snaps")"
        echo "tree_${t}_oldest=$(head -n 1 <<< "$snaps")"
        echo "tree_${t}_latest=${latest}"
        echo "tree_${t}_last_complete=${complete}"
    done
    for t in "${INSTALLED_FILES[@]}"; do
        echo "sha256_$(basename "$t")=$(probe sha256sum "$t" | cut -d' ' -f1)"
    done

    echo
    echo "===== problems"
    this_run_log | grep -E 'WARNING|ERROR|CRITICAL|CHURN|ABORTED|HEALTH: FAIL' || echo "none"
    echo
    echo "===== log"
    this_run_log
    echo
    echo "===== smart"
    echo "${smart:-unavailable}"
    echo
    echo "===== verify"
    if [ -x "$VERIFY_TOOL" ]; then probe timeout 120 "$VERIFY_TOOL" --report; else echo "not installed"; fi
    echo
    echo "===== system"
    echo "DietPi $(sed -n 's/^G_DIETPI_VERSION_\(CORE\|SUB\|RC\)=//p' /boot/dietpi/.version 2>/dev/null | paste -sd.), Debian $(cat /etc/debian_version 2>/dev/null), kernel $(uname -r)"
    echo "failed units: $(probe systemctl --failed --no-legend --plain | awk '{print $1}' | paste -sd' ')"
    echo "--- packages changed during this run"
    probe awk -v since="$(date -d "@$RUN_STARTED" '+%Y-%m-%d  %H:%M:%S')" 'BEGIN {RS = ""}
        match($0, /Start-Date: [0-9-]+  [0-9:]+/) && substr($0, RSTART + 12, RLENGTH - 12) >= since {print $0 "\n"}' \
        /var/log/apt/history.log
    echo "--- journal warnings this boot, UFW BLOCK left out (last 30)"
    probe journalctl -b -p warning --no-pager -q | grep -v 'UFW BLOCK' | tail -n 30
    echo "--- firewall"
    probe ufw status verbose
    echo
    echo "===== checksums"
    probe sha256sum "${INSTALLED_FILES[@]}" /etc/sudoers.d/* /etc/ufw/user.rules /etc/sysctl.d/*.conf
    echo "--- authorized keys"
    probe ssh-keygen -lf /home/dietpi/.ssh/authorized_keys
    probe ssh-keygen -lf /root/.ssh/authorized_keys
}

# The very last thing before the power-off decision is carried out. Written to
# a temporary name and renamed, so the laptop never reads half a report. Not
# counted as a failure when it cannot be delivered: the heartbeat went earlier,
# and the laptop pushes when a scheduled run leaves no report.
send_report() {
    local name="report-$RUN_TIMESTAMP.txt"
    if [ "$DRY_RUN" = 1 ]; then
        echo "DRY: report ($(build_report | wc -l) lines) -> $SOURCE_HOST:$REPORT_REMOTE_DIR/$name"
        return 0
    fi
    if build_report | ssh "${SSH_OPTS[@]}" "$SOURCE_USER@$SOURCE_HOST" \
         "mkdir -p '$REPORT_REMOTE_DIR' && cat > '$REPORT_REMOTE_DIR/.$name' && mv -f '$REPORT_REMOTE_DIR/.$name' '$REPORT_REMOTE_DIR/$name'" 2>/dev/null
    then
        log_message "Report sent to $SOURCE_HOST ($name)"
    else
        log_message "WARNING: could not send the report to $SOURCE_HOST"
    fi
}


# --- verification ----------------------------------------------------------

# Runs in the window that used to be a plain sleep. The login grace period is
# preserved: whatever time verification does not use is still slept away.
run_verification() {
    local budget="$1" started elapsed remaining vlog failures_at_start
    CURRENT_STEP="verification"
    started=$(date +%s)
    failures_at_start="$FAILURES"

    if [ ! -x "$VERIFY_TOOL" ]; then
        VERIFY_SOURCE=not-installed VERIFY_SCRUB=not-installed
        log_message "VERIFY: $VERIFY_TOOL not installed, idling instead."
        run sleep "$budget"
        return 0
    fi

    vlog=$(mktemp)

    # 1. Does what we just stored actually match the source? This only works
    #    while the laptop is still awake, so it goes first.
    if run "$VERIFY_TOOL" --source --sample "$VERIFY_SOURCE_SAMPLE" > "$vlog" 2>&1; then
        VERIFY_SOURCE=ok
        log_message "VERIFY: sampled files match the live source"
    else
        VERIFY_SOURCE=mismatch
        log_message "CRITICAL: VERIFY - snapshot does NOT match the live source"
        FAILURES=$((FAILURES + 1))
    fi
    sed 's/^/    verify> /' "$vlog" >> "$LOG"

    # 2. Extend the hash baseline with anything new.
    elapsed=$(( $(date +%s) - started )); remaining=$(( budget - elapsed ))
    if [ "$remaining" -ge 60 ]; then
        run "$VERIFY_TOOL" --index --max-seconds "$remaining" > "$vlog" 2>&1 \
            || log_message "WARNING: VERIFY - indexing reported a problem"
        sed 's/^/    verify> /' "$vlog" >> "$LOG"
    fi

    # 3. Re-read the oldest-verified data looking for bit rot. Pointless until
    #    the baseline has something in it, and an empty baseline is not a fault.
    elapsed=$(( $(date +%s) - started )); remaining=$(( budget - elapsed ))
    VERIFY_SCRUB=not-run
    if [ "$remaining" -ge 60 ] && [ -s "$VERIFY_MANIFEST" ]; then
        if run "$VERIFY_TOOL" --scrub --max-seconds "$remaining" > "$vlog" 2>&1; then
            VERIFY_SCRUB=ok
            log_message "VERIFY: scrub found no corruption"
        else
            VERIFY_SCRUB=corrupt
            log_message "CRITICAL: VERIFY - scrub found CORRUPT data"
            FAILURES=$((FAILURES + 1))
        fi
        sed 's/^/    verify> /' "$vlog" >> "$LOG"
    fi
    rm -f "$vlog"

    # The heartbeat was sent right after the backups and said "ok". If
    # verification has since found something, the laptop needs to hear about it,
    # otherwise corruption stays invisible until a restore fails.
    if [ "$FAILURES" -ne "$failures_at_start" ]; then
        log_message "VERIFY: raised $(( FAILURES - failures_at_start )) problem(s), updating the heartbeat"
        send_heartbeat failed
    fi

    # Preserve the original purpose of this window.
    elapsed=$(( $(date +%s) - started )); remaining=$(( budget - elapsed ))
    if [ "$remaining" -gt 0 ]; then
        echo "Verification done with ${remaining}s of the window left; idling."
        run sleep "$remaining"
    fi
    return 0
}

# --- one server ------------------------------------------------------------

backup_server() {
    local name="$1" remote_path="$2" excludes="$3" timestamp="$4"
    local server_dir="$BACKUP_ROOT/$name" temp="$BACKUP_ROOT/$name/inprogress_tmp"
    local final="$BACKUP_ROOT/$name/$timestamp" latest="$BACKUP_ROOT/$name/latest"
    local complete="$BACKUP_ROOT/$name/last-complete"
    local error_log="$server_dir/last_error.log" warn_log="$server_dir/last_warning.log"
    local -a exclude_args=() link_args=() mode_args=()
    local prev run_log exit_code attempt summary xfer_bytes baseline old_count new_count dropped

    mkdir -p "$server_dir"

    if [ -n "$excludes" ]; then
        IFS=',' read -ra patterns <<< "$excludes"
        for p in "${patterns[@]}"; do exclude_args+=( --exclude="$p" ); done
    fi

    if [ -e "$latest" ]; then
        prev=$(readlink -f "$latest")
        [ -n "$prev" ] && [ -d "$prev" ] && link_args=( --link-dest="$prev" )
    fi

    CURRENT_STEP="pull $name"
    if [ "$name" = "nextcloud" ]; then
        log_message "Starting $name (relaxed fuzzy mode, lazy permissions)..."
        # -l added: without it rsync SKIPS symlinks, and it reports that as
        # information rather than an error, so they would vanish silently.
        mode_args=( -r -l -t --fuzzy --no-owner --no-group --chmod=D2775,F664 )
    else
        log_message "Starting $name (standard archive mode)..."
        mode_args=( -a -H -X --numeric-ids )
    fi

    local -a cmd=( rsync "${mode_args[@]}" -S --delete --info=stats2 --timeout="$RSYNC_TIMEOUT"
                   -e "ssh ${SSH_OPTS[*]}"
                   "${link_args[@]}" "${exclude_args[@]}"
                   "$SOURCE_USER@$SOURCE_HOST:$remote_path" "$temp" )
    local started
    started=$(date +%s)
    run_log=$(mktemp)
    for attempt in 1 2; do
        if [ "$DRY_RUN" = 1 ]; then
            echo "DRY: ${cmd[*]}"
            exit_code=0
        else
            "${cmd[@]}" > "$run_log" 2>&1
            exit_code=$?
        fi
        if [ "$exit_code" -eq 23 ] && [ "$attempt" -eq 1 ]; then
            log_message "WARNING: $name reported changed/unreadable files (exit 23), retrying once in 60s..."
            sleep 60
            continue
        fi
        break
    done

    case "$exit_code" in
        0|23|24)
            [ -d "$final" ] && rm -rf "$final"
            run mv "$temp" "$final"
            # Atomic swap. Previously a crash between the rm and the ln left no
            # `latest` at all, which cost the next run its --link-dest and turned
            # the following backup into a full re-copy.
            run ln -sfn "$(basename "$final")" "$latest.new"
            run mv -Tf "$latest.new" "$latest"
            rm -f "$error_log"
            summary=$(grep -iE 'files transferred|total transferred file size|speedup' "$run_log" | tr '\n' ' ' | tr -s ' ')
            T_RESULT[$name]=ok
            T_XFER_FILES[$name]=$(rsync_stat 'Number of regular files transferred' "$run_log")
            T_XFER_BYTES[$name]=$(rsync_stat 'Total transferred file size' "$run_log")
            T_TOTAL[$name]=$(rsync_stat 'Total file size' "$run_log")

            # Mass change upstream: the fingerprint of ransomware or an
            # accidental bulk delete. --delete has already mirrored it into this
            # snapshot, but every older snapshot still holds the good copies, so
            # the useful thing is to say so before pruning can reach them.
            xfer_bytes=$(awk -F': *' '/Total transferred file size/{gsub(/[^0-9]/,"",$2); print $2; exit}' "$run_log")
            if [ "${xfer_bytes:-0}" -gt $(( CHURN_XFER_ALARM_GB * 1024 * 1024 * 1024 )) ]; then
                log_message "CHURN ALARM: $name transferred $(( xfer_bytes / 1073741824 )) GB in one run (limit ${CHURN_XFER_ALARM_GB} GB). Check the source for mass encryption or bulk change."
                FAILURES=$((FAILURES + 1))
            fi
            # Baseline is the last COMPLETE snapshot (what a restore would use),
            # falling back to whatever --link-dest used. last-complete has not
            # been advanced yet at this point, so it still names the previous one.
            # Skipped in dry runs, where $final was never created and would count
            # as zero files. The count is logged every run, not only on an alarm,
            # so the report can show the trend. The snapshot's own mtime is the
            # source directory's, which dietpi-backup touches when it finishes:
            # it says how old the copy this run pulled really is.
            if [ "$DRY_RUN" != 1 ]; then
                new_count=$(find "$final" -type f 2>/dev/null | wc -l)
                T_FILES[$name]=$new_count
                T_SOURCE_TIME[$name]=$(stat -c %Y "$final" 2>/dev/null)
                baseline=$(readlink -f "$complete" 2>/dev/null || true)
                [ -n "$baseline" ] && [ -d "$baseline" ] || baseline="${prev:-}"
                if [ -n "$baseline" ] && [ -d "$baseline" ] && [ "$baseline" != "$final" ]; then
                    old_count=$(find "$baseline" -type f 2>/dev/null | wc -l)
                    T_FILES_BEFORE[$name]=$old_count
                    dropped=$(( old_count - new_count ))
                    log_message "FILES: $name $old_count -> $new_count ($(( new_count - old_count )))"
                    if [ "$old_count" -gt 0 ] && [ "$dropped" -ge "$CHURN_DELETE_MIN" ] \
                       && [ $(( dropped * 100 / old_count )) -ge "$CHURN_DELETE_PCT" ]; then
                        log_message "CHURN ALARM: $name has $dropped fewer files than the last complete snapshot ($old_count -> $new_count, -$(( dropped * 100 / old_count ))%). Mass deletion or encryption upstream? Older snapshots still hold the files. Biggest drops: $(shrinking_dirs "$baseline" "$final")"
                        FAILURES=$((FAILURES + 1))
                    fi
                fi
            fi
            if [ "$exit_code" -eq 24 ]; then
                # Files vanished at the source mid-transfer. Normal on a live
                # system: they simply were not there to copy, so the snapshot is
                # still a consistent view of what did exist. Not marked.
                log_message "WARNING: $name finished, some source files vanished (exit 24). Snapshot kept."
            elif [ "$exit_code" -eq 23 ]; then
                cp "$run_log" "$warn_log"
                # Mark the snapshot itself. The sidecar log alone was easy to miss,
                # so a restore could silently use a short copy.
                {
                    echo "INCOMPLETE SNAPSHOT"
                    echo "server:    $name"
                    echo "timestamp: $timestamp"
                    echo "reason:    rsync exit 23 (files could not be transferred), twice"
                    echo "details:   $warn_log"
                } > "$final/.rsync-incomplete" 2>/dev/null
                T_RESULT[$name]=incomplete
                log_message "WARNING: $name finished with files that could not be transferred (exit 23, twice). Snapshot kept and MARKED .rsync-incomplete, details in $warn_log."
            else
                rm -f "$warn_log"
            fi

            # `latest` always advances so --link-dest keeps deduplicating, but
            # `last-complete` only follows snapshots with nothing known missing.
            # Restore from `last-complete` when you want a guaranteed-whole tree.
            if [ ! -e "$final/.rsync-incomplete" ]; then
                run ln -sfn "$(basename "$final")" "$complete.new"
                run mv -Tf "$complete.new" "$complete"
            fi
            log_message "SUCCESS: $name -> $timestamp ${summary:+| $summary}"
            ;;
        *)
            log_message "ERROR: $name backup FAILED (rsync exit $exit_code)."
            grep -iE 'rsync:|rsync error|error:|permission denied|no such file|connection|timed out|refused' \
                "$run_log" | tail -n 15 | while IFS= read -r line; do log_message "    rsync> $line"; done
            cp "$run_log" "$error_log"
            log_message "    Full output: $error_log (partial transfer kept in inprogress_tmp for the next run)"
            FAILURES=$((FAILURES + 1))
            T_RESULT[$name]=failed
            ;;
    esac
    T_SECONDS[$name]=$(( $(date +%s) - started ))
    rm -f "$run_log"
}

# A number from rsync's --info=stats2 summary, without the thousands commas.
rsync_stat() {
    awk -F': *' -v k="$1" '$1 == k {gsub(/[^0-9]/, "", $2); print $2; exit}' "$2"
}

# Files per directory, three levels deep: "usr/lib/modules<TAB>21255".
count_by_dir() {
    (cd "$1" && find . -type f 2>/dev/null) \
        | awk -F/ '{k = $2; if (NF > 3) k = k "/" $3; if (NF > 4) k = k "/" $4; n[k]++}
                   END {for (k in n) print k "\t" n[k]}' \
        | LC_ALL=C sort -t $'\t' -k1,1
}

# For a churn alarm: the five directories that lost the most files, e.g.
# "usr/lib/modules -21255, home/dietpi/.vscode-server -11843". That is usually
# enough to tell a clean-up from an attack without booting this machine.
shrinking_dirs() {
    LC_ALL=C join -t $'\t' -a 1 -e 0 -o 0,1.2,2.2 <(count_by_dir "$1") <(count_by_dir "$2") \
        | awk -F'\t' '$3 < $2 {print $3 - $2 "\t" $1}' | sort -n | head -n 5 \
        | awk -F'\t' '{printf "%s%s %d", (NR > 1 ? ", " : ""), $2, $1} END {print ""}'
}

# --- main ------------------------------------------------------------------

main() {
    local name remote_path excludes
    trap on_exit EXIT
    mkdir -p -m 700 "$(dirname "$LOG")"
    rotate_log
    mkdir -p "$BACKUP_ROOT"
    [ -f "$LOG" ] && LOG_START_LINE=$(wc -l < "$LOG")
    wait_for_clock
    RUN_STARTED=$(date +%s)
    RUN_TIMESTAMP=$(date -d "@$RUN_STARTED" +%Y-%m-%d_%H-%M-%S)
    log_message "--- Backup Cycle Started ---"
    start_selftest

    if [ "$(free_gb)" -lt "$MIN_FREE_GB" ]; then
        prune_snapshots
        log_message "Low disk space: pruned $PRUNED_COUNT old snapshot(s), $(free_gb) GB free now."
    fi

    # All three skip every pull, and a cycle that backed up nothing has failed.
    # They used to leave FAILURES at 0, so the heartbeat said "ok" and the laptop
    # stayed quiet while no backups were being made.
    if [ "$(free_gb)" -lt "$MIN_FREE_GB" ]; then
        log_message "CRITICAL: Low Disk Space! ($(free_gb) GB). Aborting backups."
        FAILURES=$((FAILURES + 1))
    elif ! disk_health; then
        FAILURES=$((FAILURES + 1))
    elif ! wait_for_source; then
        FAILURES=$((FAILURES + 1))
    else
        for entry in "${SERVERS[@]}"; do
            IFS="|" read -r name remote_path excludes <<< "$entry"
            backup_server "$name" "$remote_path" "$excludes" "$RUN_TIMESTAMP"
        done
    fi
    if [ "$FAILURES" -eq 0 ]; then
        log_message "--- Backup Cycle Finished ($(free_gb) GB free) ---"
        send_heartbeat ok
    else
        log_message "--- Backup Cycle Finished with $FAILURES failure(s) ($(free_gb) GB free) ---"
        send_heartbeat failed
    fi

    # --- system update ---
    local dietpi_before dietpi_after
    dietpi_before=$(tr "\n" " " < /boot/dietpi/.version 2>/dev/null)
    run_step "dietpi-update" sudo /boot/dietpi/dietpi-update 1
    dietpi_after=$(tr "\n" " " < /boot/dietpi/.version 2>/dev/null)
    [ "$dietpi_before" = "$dietpi_after" ] \
        || log_message "DietPi version changed: ${dietpi_before}-> ${dietpi_after}"
    if apt_update; then
        # `upgrade --with-new-pkgs` rather than `dist-upgrade`. It still pulls
        # new kernel packages -- Debian ships kernel security fixes as NEW
        # versioned packages, which a plain `upgrade` would skip -- but it will
        # never REMOVE a package, and removal is the part of dist-upgrade that
        # can strand a machine nobody is watching.
        run_step "upgrade" sudo apt-get "${APT_OPTS[@]}" upgrade --with-new-pkgs
    fi
    run_step "autoremove" sudo apt-get "${APT_OPTS[@]}" autoremove --purge
    run sudo apt-get -q clean

    # Can this machine still come back on its own? Decided before the
    # power-off, and allowed to veto it.
    if health_check; then
        HEALTHY=1
    else
        HEALTHY=0
        FAILURES=$((FAILURES + 1))
    fi

    # --- verify (inside the old idle window), then power off unless
    #     someone is logged in ---
    echo "Routine finished. Verifying for up to $((IDLE_BEFORE_POWEROFF / 60)) minutes before the shutdown check..."
    run_verification "$IDLE_BEFORE_POWEROFF"
    local ssh_logins claude_conns openssh_active physical_user
    read -r ssh_logins claude_conns < <(ssh_sessions)
    openssh_active=$(pgrep -f "sshd:" | wc -l)
    physical_user=$(pgrep -t tty1 bash | wc -l)
    log_message "Status - SSH logins: ${ssh_logins:-0} (Claude app, not counted: ${claude_conns:-0}), OpenSSH: $openssh_active, Physical: $physical_user"
    FINISHED=1
    if [ "${ssh_logins:-0}" -gt 0 ] || [ "$openssh_active" -gt 0 ] || [ "$physical_user" -gt 0 ]; then
        DECISION=stay-awake-user
        log_message "User detected. Staying awake."
    elif [ "$HEALTHY" != "1" ]; then
        # Deliberately do NOT power off. Reaching a broken machine that is still
        # running only needs SSH; reaching one that shut down needs a keyboard.
        DECISION=stay-awake-unhealthy
        log_message "CRITICAL: health check failed - STAYING AWAKE so this machine stays reachable over SSH."
    else
        DECISION=poweroff
        log_message "No active users. Powering off."
    fi
    # After the decision, so the report holds the whole run, and before the
    # power-off, so it can still leave.
    send_report
    if [ "$DECISION" = poweroff ]; then
        run sudo poweroff
    fi
}

# Sourcing the script defines the helpers without running anything (tests).
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    mkdir -p "$BACKUP_ROOT"

    # One cycle at a time: the timer may fire while a manual run is still going.
    exec 9>"$BACKUP_ROOT/.cycle.lock"
    if ! flock -n 9; then
        echo "another backup cycle is already in progress, exiting" >&2
        exit 0
    fi

    main "$@"
    exit $(( FAILURES > 0 ? 1 : 0 ))
fi
