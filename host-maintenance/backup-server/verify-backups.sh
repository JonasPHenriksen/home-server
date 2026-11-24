#!/bin/bash
# verify-backups.sh - integrity verification for the snapshot store.
#
# WHY THIS IS CHEAP
#
# The snapshots are hard-linked, so ~470 snapshots share one pool of unique
# data. Verifying is therefore per-INODE, not per-path: each distinct block of
# data only ever needs hashing once, no matter how many snapshots reference it.
#
# And because an unchanged file keeps its inode across snapshots, indexing only
# the three `latest` trees each run is enough to catch every new inode. A week
# where little changed costs almost nothing.
#
# Adds no service and no timer. Run it by hand, or append a call to the backup
# cycle if you want it routine.
#
#   sudo verify-backups.sh --index            # hash inodes never seen before
#   sudo verify-backups.sh --scrub            # re-hash oldest-checked, find bit rot
#   sudo verify-backups.sh --source           # sample newest vs the live laptop
#   sudo verify-backups.sh --report           # coverage stats, no I/O
#
#   --max-seconds N   time-box a run (default 600). Work resumes next time.
#   --sample N        --source only: how many files to compare (default 40)

set -u

STATE_DIR=/var/lib/backup-verify
MANIFEST="$STATE_DIR/inodes.tsv"
BACKUP_ROOT=/home/dietpi/backups
SERVERS=(laptop desktop nextcloud)
SOURCE_HOST=192.168.50.10
SOURCE_USER=backup-server
SSH_KEY=/root/.ssh/id_ed25519_backup-server
declare -A SRC=(
  [laptop]=/mnt/dietpi-backup/data
  [desktop]=/mnt/dietpi-desktop-backup
  [nextcloud]=/mnt/dietpi-desktop-nextcloud-backup
)

MAX_SECONDS=600
SAMPLE=40
MODE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --index|--scrub|--source|--report) MODE="${1#--}" ;;
        --max-seconds) MAX_SECONDS="$2"; shift ;;
        --sample)      SAMPLE="$2"; shift ;;
        *) echo "unknown option: $1"; exit 2 ;;
    esac
    shift
done
[ -n "$MODE" ] || { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ "$(id -u)" = "0" ] || { echo "run with sudo (snapshots are root-owned)"; exit 1; }

mkdir -p -m 700 "$STATE_DIR"   # holds the file manifest (paths + hashes); root-only
touch "$MANIFEST"
START=$(date +%s)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# EPOCHSECONDS is a bash builtin, so this costs no fork. It is called once per
# manifest entry: at ~800us per `date` fork, a half-million-row manifest would
# otherwise burn about seven minutes doing nothing but asking the time.
budget_left() { [ $(( EPOCHSECONDS - START )) -lt "$MAX_SECONDS" ]; }
hms() { printf '%dm%02ds' $(( $1 / 60 )) $(( $1 % 60 )); }

# Current inode -> representative path, for the three newest trees only.
collect_current() {
    : > "$TMP/current"
    for s in "${SERVERS[@]}"; do
        snap=$(readlink -f "$BACKUP_ROOT/$s/latest" 2>/dev/null) || continue
        [ -d "$snap" ] || continue
        # Skip paths containing a newline; they would corrupt the line format.
        find "$snap" -type f -printf '%i\t%s\t%T@\t%p\n' 2>/dev/null \
            | grep -v $'\r' >> "$TMP/current"
    done
    sort -k1,1 -u -o "$TMP/current" "$TMP/current"
}

case "$MODE" in

index)
    echo "Indexing new inodes (budget $(hms "$MAX_SECONDS"))..."
    collect_current
    cut -f1 "$MANIFEST" | sort -u > "$TMP/known"
    # inodes present now but never hashed
    join -v1 -1 1 -2 1 -t$'\t' "$TMP/current" "$TMP/known" > "$TMP/todo" 2>/dev/null \
        || awk -F'\t' 'NR==FNR{k[$1];next} !($1 in k)' "$TMP/known" "$TMP/current" > "$TMP/todo"
    total=$(wc -l < "$TMP/todo")
    echo "  inodes in newest trees: $(wc -l < "$TMP/current")"
    echo "  already indexed:        $(wc -l < "$TMP/known")"
    echo "  new to hash:            $total"
    done_n=0; bytes=0
    while IFS=$'\t' read -r ino size mtime path; do
        budget_left || { echo "  budget reached, resuming next run"; break; }
        sum=$(sha256sum -- "$path" 2>/dev/null | cut -d' ' -f1)
        [ -n "$sum" ] || continue
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$ino" "$size" "$mtime" "$sum" "$EPOCHSECONDS" "$path" >> "$MANIFEST"
        done_n=$((done_n+1)); bytes=$((bytes+size))
    done < "$TMP/todo"
    echo "  hashed $done_n inodes ($(numfmt --to=iec $bytes)) in $(hms $(( $(date +%s) - START )))"
    ;;

scrub)
    echo "Scrubbing least-recently-verified inodes (budget $(hms "$MAX_SECONDS"))..."
    [ -s "$MANIFEST" ] || { echo "  manifest empty - run --index first"; exit 1; }
    sort -t$'\t' -k5,5n "$MANIFEST" > "$TMP/byage"
    checked=0; ok=0; bad=0; gone=0; bytes=0
    : > "$TMP/updated"
    while IFS=$'\t' read -r ino size mtime sum lastv path; do
        if ! budget_left; then
            # Budget spent. Write this row back, then drain the remainder in a
            # single operation -- the loop inherits the redirected stdin, so cat
            # copies every row still unread. Doing that row by row through bash
            # took longer than the scrubbing itself on a 538k-row manifest.
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$ino" "$size" "$mtime" "$sum" "$lastv" "$path" >> "$TMP/updated"
            cat >> "$TMP/updated"
            break
        fi
        if [ ! -e "$path" ]; then
            # Its snapshot was pruned. Drop the entry; the data is gone on purpose.
            gone=$((gone+1)); continue
        fi
        now=$(sha256sum -- "$path" 2>/dev/null | cut -d' ' -f1)
        checked=$((checked+1)); bytes=$((bytes+size))
        if [ "$now" = "$sum" ]; then
            ok=$((ok+1))
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$ino" "$size" "$mtime" "$sum" "$EPOCHSECONDS" "$path" >> "$TMP/updated"
        else
            # Content differs. That is only corruption if the metadata is
            # UNCHANGED, because any real rewrite moves the size or the mtime.
            # Size is checked first (it catches same-second rewrites, which a
            # whole-second mtime comparison misses), and mtime is compared
            # numerically at full precision so the differing trailing digits of
            # find's %T@ and stat's %.9Y do not register as a change.
            cur_size=$(stat -c '%s' -- "$path" 2>/dev/null)
            cur_mtime=$(stat -c '%.9Y' -- "$path" 2>/dev/null)
            if [ "${cur_size:-unknown}" != "$size" ] || \
               [ "$(awk -v a="$mtime" -v b="${cur_mtime:-0}" 'BEGIN{print (a==b)?1:0}')" = "0" ]; then
                # Legitimately rewritten since indexing; re-baseline the entry.
                printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$ino" "${cur_size:-$size}" "${cur_mtime:-$mtime}" "$now" "$EPOCHSECONDS" "$path" >> "$TMP/updated"
            else
                bad=$((bad+1))
                echo "  CORRUPT: $path"
                echo "           expected $sum"
                echo "           got      $now"
                printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$ino" "$size" "$mtime" "$sum" "$EPOCHSECONDS" "$path" >> "$TMP/updated"
            fi
        fi
    done < "$TMP/byage"
    mv "$TMP/updated" "$MANIFEST"
    echo "  verified $checked inodes ($(numfmt --to=iec $bytes)), $ok good, $bad CORRUPT, $gone pruned away"
    echo "  elapsed $(hms $(( $(date +%s) - START )))"
    # Explicit if/exit: a trailing `[ ... ] && exit 1` makes the whole script
    # exit 1 when the test is false, because it is the last command to run.
    if [ "$bad" -gt 0 ]; then exit 1; fi
    exit 0
    ;;

source)
    echo "Comparing newest snapshots against the live source (sample $SAMPLE each)..."
    fail=0
    for s in "${SERVERS[@]}"; do
        snap=$(readlink -f "$BACKUP_ROOT/$s/latest" 2>/dev/null)
        [ -d "$snap" ] || { echo "  $s: no snapshot"; continue; }
        find "$snap" -type f -size +4k -size -2000k 2>/dev/null | head -8000 \
            | shuf -n "$SAMPLE" | sed "s|^$snap/||" > "$TMP/list"
        n=$(wc -l < "$TMP/list")
        [ "$n" -gt 0 ] || { echo "  $s: nothing to sample"; continue; }
        # Compare with rsync itself, over the same "sudo rsync" path the backup
        # uses. An earlier version ran md5sum over SSH as the unprivileged
        # user, which cannot read root-only trees (var/lib/containerd, /root)
        # and so reported every file in them as a mismatch. The laptop grants
        # NOPASSWD for /usr/bin/rsync and nothing else, so rsync is the only
        # tool that can read the source exactly as the backup does.
        #
        # -n never writes; --checksum forces a real content comparison instead
        # of the size/mtime shortcut; the --no-* options keep metadata
        # differences (deliberate, for the nextcloud tree) from being reported
        # as content differences.
        rsync -n -i --checksum --no-perms --no-owner --no-group --no-times \
              --files-from="$TMP/list" \
              -e "ssh -i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=10" \
              "$SOURCE_USER@$SOURCE_HOST:${SRC[$s]}/" "$snap/" > "$TMP/cmp" 2>&1
        rc=$?
        # Any itemised ">f" line is a file whose content does not match.
        diffs=$(grep -c '^>f' "$TMP/cmp" 2>/dev/null || true)
        errs=$(grep -ciE 'rsync error|rsync:|permission denied|no such file' "$TMP/cmp" 2>/dev/null || true)
        same=$(( n - diffs ))
        printf '  %-10s %s/%s identical\n' "$s" "$same" "$n"
        if [ "$diffs" -gt 0 ] || [ "$errs" -gt 0 ] || [ "$rc" -ne 0 ]; then
            fail=$((fail+1))
            [ "$rc" -ne 0 ] && echo "      rsync exited $rc"
            grep -E '^>f|rsync error|rsync:|Permission denied|No such file' "$TMP/cmp" \
                | head -6 | sed 's/^/      /'
        fi
    done
    if [ "$fail" -gt 0 ]; then exit 1; fi
    exit 0
    ;;

report)
    n=$(wc -l < "$MANIFEST")
    if [ "$n" = "0" ]; then echo "Manifest empty - run --index first."; exit 0; fi
    bytes=$(cut -f2 "$MANIFEST" | awk '{s+=$1} END{print s+0}')
    oldest=$(cut -f5 "$MANIFEST" | sort -n | head -1)
    newest=$(cut -f5 "$MANIFEST" | sort -n | tail -1)
    echo "Indexed inodes:     $n"
    echo "Unique data hashed: $(numfmt --to=iec "$bytes")"
    echo "Oldest verification: $(date -d "@$oldest" '+%Y-%m-%d %H:%M')"
    echo "Newest verification: $(date -d "@$newest" '+%Y-%m-%d %H:%M')"
    echo "Incomplete snapshots present:"
    found=0
    for s in "${SERVERS[@]}"; do
        while IFS= read -r m; do echo "  $m"; found=1; done < <(find "$BACKUP_ROOT/$s" -maxdepth 2 -name .rsync-incomplete 2>/dev/null)
    done
    [ "$found" = "0" ] && echo "  none"
    ;;
esac
