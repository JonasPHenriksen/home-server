#!/bin/bash
# DietPi weekly backup + update + cleanup. Runs as root from dietpi-boot-backup.service.
# Output goes to /var/local/dietpi-backup-sync.log (systemd drop-in), because the journal and
# /var/log are RAM-only on this DietPi and vanish at reboot.
#
# Order:
#   1. local DietPi-Backup (system) to /mnt/dietpi-backup
#   2. system backup  -> remote server (rsync over SSH)
#   3. Nextcloud data -> remote server, straight from the live data directory, in maintenance mode
#   4. updates:  OS (dietpi + apt)  ->  Open WebUI image  ->  Nextcloud point release + apps
#   5. cleanup:  dev-tool caches that grow without bound, docker images, apt cache
#   6. smoke test: Nextcloud and Open WebUI must answer afterwards; Nextcloud's own warnings count too
#   7. mark the run done if both remote syncs succeeded; exit non-zero if anything failed
#   8. report: the whole run goes to the laptop, which pushes what matters (this machine runs
#      no notifications of its own)
#
# When: Saturday 23:00 (dietpi-boot-backup.timer), so the backup server's Sunday run pulls a
# fresh copy from the laptop, plus two minutes after every boot to catch up when the desktop
# was off on Saturday. "Done" means one successful run since the most recent Saturday 23:00.
# It used to be once per ISO week with a Monday 04:00 run, which left the desktop's files up
# to six days old in Sunday's snapshot; and with Saturday as the slot, a Tuesday catch-up
# would have used up the ISO week and skipped the Saturday after it.
#
# Guards, each one here because of an observed failure:
#   - occ runs as www-data; as root Nextcloud refuses and maintenance mode silently never engages
#   - a trap lifts maintenance mode even if the script dies mid-sync
#   - the remote is probed first; unreachable = exit without marking the run, retry next boot
#   - compose gets an explicit project name and env file, else "up -d" hits a name conflict
#   - rsync/ssh have timeouts so a stalled transfer cannot hang the unit forever
#   - apt runs non-interactive with no pty, else it stalls on conffile prompts and floods the log
#   - ollama runs with HOME set: from the unit it has none, and printed a Go panic instead of its version
#   - the GPU check asks Ollama, not the driver: Ollama 0.33 wants driver 570+ for CUDA on this GTX 1070
#     and Debian 13 ships 550, so since Aug 29 it has used the GPU through Vulkan, which nvidia-smi
#     answering (or a CUDA library being installed) never showed
#   - it waits for a synchronised clock first: fake-hwclock (or a flat CMOS battery) can start this
#     machine hours or weeks in the past, and the run begins two minutes after boot
#
# NOT auto-updated on purpose: Ollama. It installs via a remote shell script; running that as root
# every week is a supply-chain risk not worth taking. The version is logged so you can see it drift.
# Update it by hand when you want to:  curl -fsSL https://ollama.com/install.sh | sh
# Nor a new Nextcloud major version (35 -> 36): the report flags it until you install it by hand. Its
# point releases do go in (see nc_core_update).
#
# Sourcing the script defines everything without running it (tests).

REMOTE_IP="192.168.50.10"
REMOTE_USER="desktop"   # its own login on the laptop, locked to backup-gate push
SSH_KEY="/root/.ssh/id_ed25519_dietpi"
SSH_OPTS="-i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -o StrictHostKeyChecking=accept-new"
REMOTE_SYSTEM_DIR="/mnt/dietpi-desktop-backup"
REMOTE_NC_DIR="/mnt/dietpi-desktop-nextcloud-backup"
REMOTE_REPORT_DIR="/mnt/dietpi_userdata/logs/desktop"   # the laptop's machine-reports reads it

NC_DATA="/mnt/dietpi_userdata/nextcloud_data"
NC_EXCLUDES=(--exclude='appdata_*' --exclude='updater-*' --exclude='*.log' --exclude='index.html')
OCC="sudo -u www-data php /var/www/nextcloud/occ"
NC_UPDATER="/var/www/nextcloud/updater/updater.phar"

OWUI_DIR="/opt/open-webui"
COMPOSE=(docker compose --project-directory "$OWUI_DIR" --env-file "$OWUI_DIR/.env" -f "$OWUI_DIR/compose.yml")

USER_HOME="/home/dietpi"
KEEP_TOOL_VERSIONS=2          # how many Warp / VS Code server versions to keep
NPM_CACHE_MAX_MB=200
# Reallocated and pending sectors a disk may have before the SMART check fails, by serial number (sda
# and sdb can swap between boots), so that a known count warns only when it grows. No disk needs one
# now: the Toshiba (once the Samba disk, 35 -> 48 reallocated between 2026-09-27 and 2026-10-04) was
# unplugged on 2026-10-17, and plugged back in it fails the check again. Raise a number only after
# checking why it grew.
declare -A REALLOC_OK=()
declare -A PENDING_OK=()

LOGFILE="/var/local/dietpi-backup-sync.log"
LOG_MAX_BYTES=$((1024 * 1024))
LOG_KEEP_LINES=2000

DATE_STAMP=$(date +"%Y-%m-%d_%H-%M")
LAST_RUN_FILE="${LAST_RUN_FILE:-/var/local/last_backup_run}"   # epoch of the last run whose two syncs succeeded
TIMESYNC_FLAG="${TIMESYNC_FLAG:-/run/systemd/timesync/synchronized}"   # timesyncd creates it once synced
CLOCK_WAIT_SECS=300

# Checksummed in the report, so the laptop notices when this machine and the repository drift apart.
INSTALLED_FILES=(/usr/local/bin/dietpi-backup-sync.sh /etc/systemd/system/dietpi-boot-backup.timer
                 /etc/systemd/system/dietpi-boot-backup.service)

export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -q -o Dpkg::Use-Pty=0 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

# What the report says about this run, filled in as it goes.
FAILURES=()
RUN_LOG=()
RUN_STARTED=$(date +%s)
LOCAL_RESULT=""; SYS_RESULT=""; NC_RESULT=""
SYS_FILES=""; SYS_BYTES=""; NC_FILES=""; NC_BYTES=""
NC_CODE=""; OWUI_CODE=""; GPU=""; OLLAMA_GPU=""; OLLAMA_VERSION=""; NC_VERSION=""; DISK_FREE=""; RUN_DONE=no
CLOCK_SYNCED=unknown
SMART=""

log(){ local line="[$(date '+%F %T')] $*"; echo "$line"; RUN_LOG+=("$line"); }
fail(){ FAILURES+=("$1"); log "FAILED: $1"; }

iso(){ date -u -d "@$1" '+%Y-%m-%dT%H:%M:%SZ'; }

# The most recent Saturday 23:00 (local time) at or before the epoch $1.
last_slot(){
    local now="$1" today back slot
    today=$(date -d "@$now" +%F)
    back=$(( ( $(date -d "@$now" +%u) + 1 ) % 7 ))          # days since Saturday: Sat 0, Sun 1 ... Fri 6
    slot=$(date -d "$(date -d "$today -$back days" +%F) 23:00" +%s)
    [ "$slot" -gt "$now" ] && slot=$(date -d "$(date -d "$today -$(( back + 7 )) days" +%F) 23:00" +%s)
    echo "$slot"
}

# Has a run succeeded since the most recent Saturday 23:00? A mark from the future doesn't count: only
# a run on a clock that was ahead writes one, and it would skip every week until that date.
already_done(){
    local last now="${1:-$(date +%s)}"
    last=$(cat "$LAST_RUN_FILE" 2>/dev/null)
    [[ $last =~ ^[0-9]+$ ]] && [ "$last" -ge "$(last_slot "$now")" ] && [ "$last" -le "$now" ]
}

# The week check, the report's name and its times all use the clock, and this run starts two minutes
# after boot, possibly before timesyncd has put a rewound clock right. Wait up to five minutes. If it
# still isn't synced, that is a failure the laptop hears about, and the week check is not trusted:
# running twice in a week costs nothing, skipping a week because the clock said "last Tuesday" does.
wait_for_clock(){
    local waited=0
    while [ ! -e "$TIMESYNC_FLAG" ] && [ "$waited" -lt "$CLOCK_WAIT_SECS" ]; do
        sleep 10; waited=$((waited + 10))
    done
    if [ -e "$TIMESYNC_FLAG" ]; then
        CLOCK_SYNCED=yes
        [ "$waited" -gt 0 ] && log "Clock synchronised after ${waited}s"
    else
        CLOCK_SYNCED=no
        fail "clock not synchronised after $((CLOCK_WAIT_SECS / 60)) min: this report's times may be off, and it ran without checking whether this week was already done"
    fi
    return 0
}

# Run now? Yes unless a run already succeeded this week - and only a synced clock can say that.
due_now(){ [ "$CLOCK_SYNCED" != yes ] || ! already_done "$@"; }

# nvidia-smi answering only proves the kernel driver. Whether Ollama uses the GPU, and through which
# library, is in the "inference compute" line it logs at every start: the last one of this boot counts
# (a restart logs a new one). CUDA or Vulkan is fine, the CPU is not. The journal is in RAM and capped,
# so after a few days of uptime the line may be gone: that is "unknown", not a failure.
gpu_check(){
    local line
    if ! nvidia-smi -L >/dev/null 2>&1; then
        GPU=failed
        fail "smoke test GPU (nvidia module not loaded - dkms build may have failed after a kernel update)"
        return 0
    fi
    if ! systemctl is-active --quiet ollama; then
        GPU=failed
        fail "smoke test GPU (the ollama service is not running)"
        return 0
    fi
    line=$(journalctl -u ollama -b -o cat --no-pager 2>/dev/null | grep 'msg="inference compute"' | tail -n 1)
    OLLAMA_GPU=$(sed -n 's/.* library="\{0,1\}\([^" ]*\).*/\1/p' <<< "$line")
    case "${OLLAMA_GPU,,}" in
        "")  GPU=unknown; OLLAMA_GPU=unknown
             log "Smoke test GPU: driver ok; which library Ollama uses is unknown (its start is no longer in the journal)" ;;
        cpu) GPU=cpu
             fail "smoke test GPU (Ollama runs on the CPU: it found no usable GPU when it started)" ;;
        *)   GPU=ok
             log "Smoke test GPU: ok, Ollama uses it through $OLLAMA_GPU" ;;
    esac
}

# SMART, light on purpose: it only reads what each disk already reports (about a second per disk)
# and never starts a self-test. It fails the run on the overall health verdict, on any
# offline-uncorrectable sector (198), or on more pending (197) or reallocated (5) sectors than that
# disk's allowance in PENDING_OK / REALLOC_OK. Plain `smartctl /dev/sdX` finds these SATA disks
# by itself; don't pass it `smartctl --scan` output, which labels them `-d scsi` and hides the ATA
# counters.
smart_check(){
    local dev out rc model serial realloc pending uncorr
    SMART=ok
    if ! command -v smartctl >/dev/null; then
        SMART=failed
        fail "SMART check (smartctl is not installed: apt install smartmontools)"
        return 0
    fi
    for dev in $(lsblk -dno NAME,TYPE | awk '$2 == "disk" && $1 !~ /^zram/ {print "/dev/" $1}'); do
        out=$(smartctl -i -H -A "$dev" 2>&1); rc=$?
        model=$(sed -n 's/^Device Model: *//p' <<< "$out")
        serial=$(sed -n 's/^Serial Number: *//p' <<< "$out")
        realloc=$(awk '$1 == 5 {print $10 + 0}' <<< "$out")
        pending=$(awk '$1 == 197 {print $10 + 0}' <<< "$out")
        uncorr=$(awk '$1 == 198 {print $10 + 0}' <<< "$out")
        # smartctl's exit bits: 1 bad command line, 2 open failed, 8 disk failing, 16 prefail attribute at threshold
        if (( rc & 27 )) || (( ${uncorr:-0} > 0 || ${pending:-0} > ${PENDING_OK[${serial:-none}]:-0} \
              || ${realloc:-0} > ${REALLOC_OK[${serial:-none}]:-0} )); then
            SMART=failed
            fail "SMART ${model:-$dev}: smartctl status $rc, reallocated ${realloc:-n/a}, pending ${pending:-n/a}, uncorrectable ${uncorr:-n/a}"
        else
            log "SMART ${model:-$dev}: ok (reallocated ${realloc:-n/a}, pending ${pending:-n/a})"
        fi
    done
}

# The version Nextcloud runs (35.0.2).
nc_version(){ $OCC status --output=json 2>/dev/null | sed -n 's/.*"versionstring":"\([^"]*\)".*/\1/p'; }

# Nextcloud's own code, point releases only: 35.0.2 -> 35.0.3 is bug and security fixes and goes in
# unattended, after this run's backups. A new major version (35 -> 36) waits for you. The update server
# offers it once the last point release is in, and it disables apps that aren't ready for it and brings
# new admin warnings, so read its notes and start it from Administration settings > Overview. Until you
# do, every run reports it as a problem, so the laptop pushes it.
#   - update:check names the version the updater would install (both ask the same update server); if a
#     new major gets in anyway, the run fails so you hear about it
#   - --no-backup: otherwise the updater keeps a full copy of the old code in the data directory for every
#     update, and never deletes one. The way back is this run's DietPi-Backup instead (code and database,
#     taken with the services stopped), so without that one there is no update.
nc_core_update(){
    local before out offered after
    before=$(nc_version)
    if [ -z "$before" ] || ! out=$($OCC update:check 2>&1); then
        fail "nextcloud core update (occ status or update:check failed)"
        return 0
    fi
    offered=$(grep -m1 ' is available. Get more information' <<< "$out" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)
    if [ -z "$offered" ]; then
        log "Nextcloud core: $before, no update"
    elif [ "${offered%%.*}" != "${before%%.*}" ]; then
        fail "nextcloud major version $offered is out: install it by hand (Administration settings > Overview)"
    elif [ "$LOCAL_RESULT" != 0 ]; then
        log "Nextcloud core: $offered left alone, this run has no local backup to go back to"
    elif sudo -u www-data php "$NC_UPDATER" --no-interaction --no-backup; then
        after=$(nc_version)
        if [ "${after%%.*}" != "${before%%.*}" ]; then
            fail "nextcloud core: the updater installed $after, a new major version, not $offered - check its apps and admin warnings"
        elif [ "$after" = "$before" ]; then
            log "Nextcloud core: $offered announced, but the updater found nothing to install"
        else
            log "Nextcloud core: updated $before -> $after"
        fi
    else
        fail "nextcloud core update $before -> $offered (updater exit $?; details in updater.log in the data directory)"
    fi
}

# What Nextcloud itself would put in front of you, reported as problems so the laptop pushes them: the
# warnings and errors on Administration settings > Overview, and errors in its log since the last run.
# The overview's "Cron last run" is left out: it turns into an error once the background jobs last ran
# over an hour ago, which on this desktop, switched on when needed, is true after nearly every boot
# (18 October: "ran 10 hours ago"), and they can't catch up while this job holds Nextcloud in
# maintenance mode. Nextcloud's own cron runs them again once the job is done.
# The overview's own "Errors in the log" is replaced by that log count, which leaves out the kinds
# that never mean anything here: a sync client knocking while this job holds Nextcloud in maintenance
# mode (sabre logs every such request as an error); "not allowed on Oracle", a warning for Oracle
# databases about queries that run fine on this MariaDB; and the data folder check that setupchecks
# above runs every time, which asks for .ncdata over the web to prove it can't be fetched and logs
# the refusal as an error ("The requested uri(.../.ncdata) cannot be processed by the script").
# Also three that everyday use brings (24 October: all 36 errors of a week were these): a thumbnail
# whose file is missing ("Unable to open preview stream at", at worst a grey square); an upload cut
# off on the way in ("Expected filesize of ... but read (from Nextcloud client)", translated for each
# user), whose part file Nextcloud throws away while the client sends it
# again; and two requests registering the same file at once (1062 Duplicate entry on the file list's
# fs_storage_path_hash), where one wins and the other is retried.
# The most frequent message is cut with mb_strcut, never inside a character: grep drops a line that
# is not valid UTF-8, here in the report and again in machine-reports on the laptop, so the problem
# would vanish without a push.
nc_checks(){
    local line since errs
    while IFS= read -r line; do
        fail "nextcloud overview $line"
    done < <($OCC setupchecks 2>/dev/null \
               | sed -n -e $'s/^\t\t⚠ /warning: /p' -e $'s/^\t\t✗ /error: /p' \
               | grep -vE '^warning: Errors in the log:|^(warning|error): Cron last run:')

    since=$(cat "$LAST_RUN_FILE" 2>/dev/null)
    [[ $since =~ ^[0-9]+$ ]] || since=$(( $(date +%s) - 7 * 86400 ))
    errs=$(php -r '
        $since = (int)$argv[2]; $n = 0; $by = [];
        foreach ([$argv[1] . ".1", $argv[1]] as $f) {
            if (!is_readable($f)) continue;
            foreach (new SplFileObject($f) as $line) {
                $e = json_decode($line, true);
                if (!is_array($e) || ($e["level"] ?? 0) < 3 || (int)strtotime($e["time"] ?? "") < $since) continue;
                $msg = preg_replace("/\s+/", " ", (string)($e["message"] ?? ""));
                if (str_ends_with((string)($e["exception"]["Exception"] ?? ""), "ServerMaintenanceMode")
                    || str_contains($msg, "not allowed on Oracle")
                    || str_contains($msg, "/.ncdata) cannot be processed by the script")
                    || str_contains($msg, "Unable to open preview stream at ")
                    || str_contains($msg, "(from Nextcloud client)")
                    || (str_contains($msg, "1062 Duplicate entry") && str_contains($msg, "fs_storage_path_hash"))) continue;
                $n++;
                $by[$msg] = ($by[$msg] ?? 0) + 1;
            }
        }
        if ($n) { arsort($by); echo $n, " ", mb_strcut((string)array_key_first($by), 0, 160, "UTF-8"); }
    ' "$NC_DATA/nextcloud.log" "$since")
    [ -n "$errs" ] && fail "nextcloud log: ${errs%% *} error(s) since $(date -d "@$since" '+%a %d %b %H:%M'), most often \"${errs#* }\" (Administration settings > Logging)"
    return 0
}

# A number from rsync's --info=stats2 summary, without the thousands commas.
rsync_stat(){ awk -F': *' -v k="$1" '$1 == k {gsub(/[^0-9]/, "", $2); print $2; exit}' "$2"; }

# Keep the log small. apt and dietpi-update emit carriage-return progress and ANSI colour, which is
# why 2 runs once reached 3.5 MB in 392 lines. Rewriting is safe: systemd opened the file O_APPEND.
trim_log(){
    if [ -f "$LOGFILE" ] && [ "$(stat -c %s "$LOGFILE")" -gt "$LOG_MAX_BYTES" ]; then
        sed -e 's/\r/\n/g' -e 's/\x1b\[[0-9;]*[A-Za-z]//g' "$LOGFILE" \
          | grep -vE '^\(Reading database|^Preparing to unpack|^Unpacking |^Selecting previously' \
          | tail -n "$LOG_KEEP_LINES" > "$LOGFILE.trim" \
          && cat "$LOGFILE.trim" > "$LOGFILE"
        rm -f "$LOGFILE.trim"
    fi
}

# Warp and VS Code keep every remote-server version they have ever downloaded. They had
# accumulated 12 and 4 versions (5.6 GB) before being cleared out by hand once.
prune_versions(){ # $1 = directory of versioned entries, $2 = label
    # These live under dietpi's home, so delete as dietpi, not root: dietpi could
    # make the entry a symlink and have root delete files elsewhere otherwise.
    [ -d "$1" ] || return 0
    local removed=0
    while IFS= read -r old; do
        [ -z "$old" ] && continue
        sudo -u dietpi rm -rf -- "$old" && removed=$((removed+1))
    done < <(ls -1dt "$1"/* 2>/dev/null | tail -n +$((KEEP_TOOL_VERSIONS + 1)))
    [ $removed -gt 0 ] && log "Cleanup: removed $removed old $2 version(s), kept newest $KEEP_TOOL_VERSIONS"
    return 0
}

# Everything about this run, for the laptop: key=value lines first (machine-reports reads them),
# then the lines that went wrong and this run's own log lines (not the apt noise around them).
build_report(){
    local f
    echo "# desktop report, format 1"
    echo "host=$(hostname)"
    echo "run=$DATE_STAMP"
    echo "started=$(iso "$RUN_STARTED")"
    echo "finished=$(iso "$(date +%s)")"
    echo "status=$([ ${#FAILURES[@]} -eq 0 ] && echo ok || echo failed)"
    echo "failures=${#FAILURES[@]}"
    echo "local_backup=$([ "$LOCAL_RESULT" = 0 ] && echo ok || echo "failed ($LOCAL_RESULT)")"
    echo "system_sync=$([ "$SYS_RESULT" = 0 ] && echo ok || echo "failed (rsync exit $SYS_RESULT)")"
    echo "system_files_transferred=$SYS_FILES"
    echo "system_bytes_transferred=$SYS_BYTES"
    echo "nextcloud_sync=$([ "$NC_RESULT" = 0 ] && echo ok || echo "failed (rsync exit $NC_RESULT)")"
    echo "nextcloud_files_transferred=$NC_FILES"
    echo "nextcloud_bytes_transferred=$NC_BYTES"
    echo "smoke_nextcloud=$NC_CODE"
    echo "smoke_openwebui=$OWUI_CODE"
    echo "smoke_gpu=$GPU"
    echo "ollama_gpu=$OLLAMA_GPU"
    echo "smart=$SMART"
    echo "clock_synced=$CLOCK_SYNCED"
    echo "run_done=$RUN_DONE"
    echo "disk_free=$DISK_FREE"
    echo "ollama=$OLLAMA_VERSION"
    echo "nextcloud=$NC_VERSION"
    for f in "${INSTALLED_FILES[@]}"; do
        echo "sha256_$(basename "$f")=$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)"
    done
    echo
    echo "===== problems"
    if [ ${#FAILURES[@]} -eq 0 ]; then echo "none"; else printf '%s\n' "${RUN_LOG[@]}" | grep 'FAILED: '; fi
    echo
    echo "===== log"
    printf '%s\n' "${RUN_LOG[@]}"
}

# Written to a temporary name and renamed, so the laptop never reads half a report. Tried three
# times a minute apart; not counted as a failure (the laptop notices a desktop that goes quiet).
send_report(){
    local name="report-$DATE_STAMP.txt" try
    for try in 1 2 3; do
        if build_report | ssh $SSH_OPTS "$REMOTE_USER@$REMOTE_IP" \
             "mkdir -p '$REMOTE_REPORT_DIR' && cat > '$REMOTE_REPORT_DIR/.$name' && mv -f '$REMOTE_REPORT_DIR/.$name' '$REMOTE_REPORT_DIR/$name'"
        then
            log "Report sent to $REMOTE_IP ($name)"
            return 0
        fi
        [ $try -lt 3 ] && sleep 60
    done
    log "Could not send the report to $REMOTE_IP"
}

main(){
    local slot out
    trim_log
    log "===== dietpi-backup-sync start ====="
    wait_for_clock
    RUN_STARTED=$(date +%s)                       # again, now that the clock can be trusted
    DATE_STAMP=$(date -d "@$RUN_STARTED" +"%Y-%m-%d_%H-%M")
    slot=$(last_slot "$RUN_STARTED")
    log "This week's slot: $(date -d "@$slot" '+%a %F %H:%M')"

    if ! due_now "$RUN_STARTED"; then
        log "Already ran since $(date -d "@$slot" '+%a %d %b %H:%M'). Exiting."
        exit 0
    fi

    if ! ssh $SSH_OPTS "$REMOTE_USER@$REMOTE_IP" true; then
        log "Remote $REMOTE_IP not reachable over SSH. Skipping this run, will retry next boot."
        exit 1
    fi

    # ---------------------------------------------------------------- 1. local system backup
    /boot/dietpi/dietpi-backup 1
    LOCAL_RESULT=$?
    [ $LOCAL_RESULT -eq 0 ] || fail "local dietpi-backup"

    # ---------------------------------------------------------------- 2. system backup -> remote
    # The output goes to a file first so the report can quote rsync's numbers, then into the log.
    out=$(mktemp)
    rsync -azHX -S --numeric-ids --delete --partial --timeout=600 --info=stats2 \
      -e "ssh $SSH_OPTS" \
      /mnt/dietpi-backup/data/ \
      "$REMOTE_USER@$REMOTE_IP:$REMOTE_SYSTEM_DIR/" > "$out" 2>&1
    SYS_RESULT=$?
    cat "$out"
    SYS_FILES=$(rsync_stat 'Number of regular files transferred' "$out")
    SYS_BYTES=$(rsync_stat 'Total transferred file size' "$out")
    if [ $SYS_RESULT -eq 0 ]; then log "System sync to remote: SUCCESS"; else fail "system sync to remote (rsync exit $SYS_RESULT)"; fi

    # ---------------------------------------------------------------- 3. Nextcloud data -> remote
    $OCC maintenance:mode --on
    trap '$OCC maintenance:mode --off' EXIT

    rsync -rtz -S --delete --fuzzy --no-owner --no-group --chmod=D2775,F664 --partial --timeout=600 --info=stats2 \
      -e "ssh $SSH_OPTS" \
      "${NC_EXCLUDES[@]}" \
      "$NC_DATA/" \
      "$REMOTE_USER@$REMOTE_IP:$REMOTE_NC_DIR/" > "$out" 2>&1
    NC_RESULT=$?
    cat "$out"
    NC_FILES=$(rsync_stat 'Number of regular files transferred' "$out")
    NC_BYTES=$(rsync_stat 'Total transferred file size' "$out")
    rm -f "$out"

    $OCC maintenance:mode --off
    trap - EXIT
    if [ $NC_RESULT -eq 0 ]; then log "Nextcloud sync to remote: SUCCESS"; else fail "nextcloud sync to remote (rsync exit $NC_RESULT)"; fi

    # ---------------------------------------------------------------- 4. updates
    /boot/dietpi/dietpi-update 1
    apt-get update -q || fail "apt-get update"
    apt-get "${APT_OPTS[@]}" dist-upgrade || fail "apt-get dist-upgrade"
    apt-get "${APT_OPTS[@]}" autoremove
    apt-get -q clean

    # 4b. Open WebUI: pull the current image; "up -d" recreates only if the image changed
    if [ -f "$OWUI_DIR/compose.yml" ]; then
        if "${COMPOSE[@]}" pull -q && "${COMPOSE[@]}" up -d; then
            log "Open WebUI: image current, container up to date"
        else
            fail "Open WebUI update (container left as is)"
        fi
    fi

    # 4c. Nextcloud: its own code (point releases only, see nc_core_update), then its apps. apt
    #     updates neither, so without this they never get security fixes.
    nc_core_update
    if NC_APPS=$($OCC app:update --all 2>&1); then
        if [ -n "$(echo "$NC_APPS" | grep -vE '^\s*$')" ]; then
            log "Nextcloud apps updated:"; echo "$NC_APPS" | sed 's/^/    /'
        else
            log "Nextcloud apps: already current"
        fi
    else
        fail "nextcloud app:update"
    fi
    # Indices, columns and primary keys that new versions of Nextcloud and its apps ask for. Upgrades
    # leave them out because on a big server adding them can take hours; here it takes seconds, and
    # without it a major upgrade leaves a "missing indices" warning in the admin overview.
    for c in db:add-missing-indices db:add-missing-columns db:add-missing-primary-keys; do
        if NC_DB=$($OCC "$c" 2>&1); then
            grep -q 'updated successfully' <<< "$NC_DB" && { log "Nextcloud $c:"; echo "$NC_DB" | sed 's/^/    /'; }
        else
            fail "nextcloud $c"
        fi
    done

    # 4d. Ollama is a manual install, deliberately not auto-updated (see header)
    OLLAMA_VERSION=$(HOME=/root ollama --version 2>&1 | head -1)
    NC_VERSION=$(nc_version)
    log "Ollama version: $OLLAMA_VERSION | Nextcloud: $NC_VERSION"

    # ---------------------------------------------------------------- 5. cleanup
    prune_versions "$USER_HOME/.warp/remote-server" "Warp remote-server"
    # VS Code keeps one standalone binary per release plus a cli dir per release
    if [ -d "$USER_HOME/.vscode-server" ]; then
        n=0
        while IFS= read -r old; do [ -n "$old" ] && sudo -u dietpi rm -f -- "$old" && n=$((n+1)); done \
          < <(ls -1t "$USER_HOME"/.vscode-server/code-* 2>/dev/null | tail -n +$((KEEP_TOOL_VERSIONS + 1)))
        [ $n -gt 0 ] && log "Cleanup: removed $n old VS Code server binary/binaries"
    fi
    # npm cache grows unbounded; npm prunes it safely itself
    if command -v npm >/dev/null && [ -d "$USER_HOME/.npm" ]; then
        sz=$(du -sm "$USER_HOME/.npm" 2>/dev/null | cut -f1)
        if [ "${sz:-0}" -gt "$NPM_CACHE_MAX_MB" ]; then
            sudo -u dietpi -H npm cache clean --force >/dev/null 2>&1
            log "Cleanup: npm cache was ${sz} MB, now $(du -sm "$USER_HOME/.npm" 2>/dev/null | cut -f1) MB"
        fi
    fi
    docker image prune -f >/dev/null 2>&1
    DISK_FREE=$(df -h / | awk 'NR==2{print $4}')
    log "Cleanup: disk now $(df -h / | awk 'NR==2{print $4" free ("$5" used)"}'), home $(du -sh "$USER_HOME" 2>/dev/null | cut -f1)"

    # ---------------------------------------------------------------- 6. smoke test
    # status.php answers 200 in maintenance mode too, so its JSON has to say Nextcloud is usable
    NC_STATUS=$(curl -sk -m 15 -w '%{http_code}' https://127.0.0.1/nextcloud/status.php)
    NC_CODE=${NC_STATUS: -3}
    if [ "$NC_CODE" != 200 ]; then
        fail "smoke test Nextcloud (HTTP $NC_CODE)"
    elif [[ $NC_STATUS != *'"maintenance":false'* || $NC_STATUS != *'"needsDbUpgrade":false'* ]]; then
        fail "smoke test Nextcloud (HTTP 200, but in maintenance mode or waiting for occ upgrade)"
    else
        log "Smoke test Nextcloud: HTTP 200"
    fi
    for i in $(seq 1 20); do
        OWUI_CODE=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/)
        [ "$OWUI_CODE" = "200" ] && break
        sleep 6
    done
    [ "$OWUI_CODE" = "200" ] && log "Smoke test Open WebUI: HTTP 200" || fail "smoke test Open WebUI (HTTP $OWUI_CODE)"
    gpu_check
    smart_check
    nc_checks

    # ---------------------------------------------------------------- 7. result
    if [ $SYS_RESULT -eq 0 ] && [ $NC_RESULT -eq 0 ]; then
        date +%s > "$LAST_RUN_FILE"
        RUN_DONE=yes
        log "Run marked done (both syncs succeeded); next one Saturday 23:00."
    else
        log "Run NOT marked done (system=$SYS_RESULT nextcloud=$NC_RESULT). Will retry next boot."
    fi

    if [ ${#FAILURES[@]} -eq 0 ]; then
        log "===== dietpi-backup-sync end: OK ====="
    else
        log "===== dietpi-backup-sync end: ${#FAILURES[@]} problem(s): ${FAILURES[*]} ====="
    fi

    # ---------------------------------------------------------------- 8. report
    send_report
    [ ${#FAILURES[@]} -eq 0 ]
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main
    exit $?
fi
