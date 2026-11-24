#!/bin/bash
#
# Presence based camera switch, run every ~10s by check_phones_loop.sh.
#
# phone_probe.py checks every phone with several methods at once (ping, TCP,
# ARP). When no phone answers for `threshold` consecutive checks the cameras
# are switched to "enable" (away: armed, recording). As soon as a phone is back
# they are switched to "disable" (home: privacy mode). A switch that does not
# fully apply (a camera is offline) is retried with exponential backoff, and a
# real state change always supersedes a pending retry.
#
# Night mode (night_mode.sh on|tonight [HH:MM-HH:MM]) keeps the "night" camera
# group watching quietly during its window whatever the presence state. This
# script re-syncs the cameras whenever that window opens or closes.
#
# Environment overrides. Used by the test suite, harmless in production:
#   TAPO_BASE_DIR         project directory (default /home/dietpi/tapo-automation)
#   TAPO_TOGGLE_CMD       command applying a state, run as: CMD <enable|disable> all
#   TAPO_TOGGLE_TIMEOUT   hard limit in seconds for one toggle run (default 120)
#   TAPO_PROBE_CMD        presence probe, exit 0 = someone home, 1 = nobody home
#   TAPO_PROBE_TIMEOUT    hard limit in seconds for one probe run (default 15)
#   TAPO_NOW              epoch seconds to use instead of `date +%s`
#   TAPO_TIME             local time (HH:MM) to use instead of `date +%H:%M`
#   TAPO_NOTIFY_CMD       push helper, run as: CMD -p PRIORITY -t TAGS TITLE MESSAGE
#                         (default: /usr/local/bin/ntfy-notify when installed, else no pushes)
#
# Pushes (one per problem, one more when it is over): the cameras not switching
# after several attempts, and the probe itself failing. See README "Notifications".

BASE_DIR="${TAPO_BASE_DIR:-/home/dietpi/tapo-automation}"
STATE_FILE="$BASE_DIR/.tapo_motion_state"          # the state we last ASKED for
PENDING_FILE="$BASE_DIR/.tapo_pending"             # exists while a switch is incomplete
FAIL_COUNT_FILE="$BASE_DIR/.tapo_ping_fail_count"
THRESHOLD_FILE="$BASE_DIR/.tapo_away_threshold"
ENABLE_TIME_FILE="$BASE_DIR/.tapo_enable_time"
RETRY_AFTER_FILE="$BASE_DIR/.tapo_retry_after"
FAIL_STREAK_FILE="$BASE_DIR/.tapo_switch_fail_streak"
NIGHT_MODE_FILE="$BASE_DIR/.tapo_night_mode"       # exists while night mode is on; may hold an expiry epoch
NIGHT_WINDOW_FILE="$BASE_DIR/.tapo_night_window"   # 1 while the night rules are currently applied
PROBE_ERROR_FILE="$BASE_DIR/.tapo_probe_error"     # exists while the probe itself is failing
NOTIFIED_FILE="$BASE_DIR/.tapo_switch_notified"    # exists after a push about the current incomplete switch
LOCK_FILE="$BASE_DIR/.tapo_phones.lock"
LOG_FILE="$BASE_DIR/tapo.log"

# Push a notification: notify PRIORITY TAGS TITLE MESSAGE. Runs in the
# background so a slow ntfy server never delays this 10 s loop; the lock fd is
# closed there so the next run is not blocked either. Failures go to the log.
notify() {
    [ "${#NOTIFY_CMD[@]}" -gt 0 ] || return 0
    log "notification: $3"
    (
        exec 9>&-
        out=$("${NOTIFY_CMD[@]}" -p "$1" -t "$2" "$3" "$4" 2>&1) || log "notification failed: ${out:-no output}"
    ) >/dev/null 2>&1 &
}

# shellcheck source=night_common.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/night_common.sh"

if [ -n "$TAPO_TOGGLE_CMD" ]; then
    read -r -a TOGGLE_CMD <<< "$TAPO_TOGGLE_CMD"
else
    TOGGLE_CMD=("$BASE_DIR/tapo-env/bin/python3" "$BASE_DIR/tapo_toggle.py")
fi
TOGGLE_TIMEOUT="${TAPO_TOGGLE_TIMEOUT:-120}"

if [ -n "$TAPO_PROBE_CMD" ]; then
    read -r -a PROBE_CMD <<< "$TAPO_PROBE_CMD"
else
    PROBE_CMD=("$BASE_DIR/tapo-env/bin/python3" "$BASE_DIR/phone_probe.py")
fi
PROBE_TIMEOUT="${TAPO_PROBE_TIMEOUT:-15}"

if [ -n "$TAPO_NOTIFY_CMD" ]; then
    read -r -a NOTIFY_CMD <<< "$TAPO_NOTIFY_CMD"
elif [ -x /usr/local/bin/ntfy-notify ]; then
    NOTIFY_CMD=(/usr/local/bin/ntfy-notify)
else
    NOTIFY_CMD=()
fi

THRESHOLD_START=3     # checks (~10s each) of no phones before arming
THRESHOLD_STEP=3      # raise by this after each false trigger
THRESHOLD_MAX=120     # hard cap (~20 min)
SHORT_TRIP_SECS=900   # on->off within this window counts as a false trigger

COOLDOWN_BASE=60      # wait before retrying the cameras that did not switch
COOLDOWN_MAX=900
NOTIFY_AFTER_STREAK=4 # incomplete switches in a row before a push (60+120+240 s of retries)

LOG_MAX_LINES=2000
LOG_KEEP_LINES=1000

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [phones] $1" >> "$LOG_FILE"
    if [ "$(wc -l < "$LOG_FILE")" -gt "$LOG_MAX_LINES" ]; then
        tail -n "$LOG_KEEP_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
    fi
}

read_file() {
    local val=""
    [ -f "$1" ] && val=$(cat "$1")
    if [ -n "$val" ]; then echo "$val"; else echo "$2"; fi
}

# Only one instance at a time. A second one (manual run, slow toggle) just leaves.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    exit 0
fi

now="${TAPO_NOW:-$(date +%s)}"
now_min=$(to_minutes "${TAPO_TIME:-$(date +%H:%M)}")

# tapo_toggle.py only touches cameras that are out of sync and exits non-zero
# if any of them are still out of sync afterwards. The timeout guard makes sure
# a hanging camera can never block this loop for good.
apply_state() {
    local target="$1" now="$2" rc streak cooldown

    timeout -k 10 "$TOGGLE_TIMEOUT" "${TOGGLE_CMD[@]}" "$target" all
    rc=$?

    if [ "$rc" -eq 0 ]; then
        rm -f "$PENDING_FILE" "$RETRY_AFTER_FILE" "$FAIL_STREAK_FILE"
        log "switch to $target fully applied"
        if [ -f "$NOTIFIED_FILE" ]; then
            rm -f "$NOTIFIED_FILE"
            notify low white_check_mark "Cameras in sync again" "Switch to $target fully applied."
        fi
        return
    fi

    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        log "toggle did not finish within ${TOGGLE_TIMEOUT}s and was killed"
    fi

    touch "$PENDING_FILE"
    streak=$(read_file "$FAIL_STREAK_FILE" 0)
    streak=$((streak + 1))
    cooldown=$((COOLDOWN_BASE * 2 ** (streak - 1)))
    [ "$cooldown" -gt "$COOLDOWN_MAX" ] && cooldown=$COOLDOWN_MAX
    echo "$streak" > "$FAIL_STREAK_FILE"
    echo "$((now + cooldown))" > "$RETRY_AFTER_FILE"
    log "switch to $target incomplete (rc=$rc, streak $streak), retrying the rest in ${cooldown}s"
    if [ "$streak" -ge "$NOTIFY_AFTER_STREAK" ] && [ ! -f "$NOTIFIED_FILE" ]; then
        touch "$NOTIFIED_FILE"
        notify high warning "Cameras not switching to $target" \
            "$streak attempts failed (rc=$rc); a camera is probably offline. Retrying every ${cooldown}s, see tapo.log."
    fi
}

# Night mode is on while its flag file exists (see night_common.sh for the
# format). night_mode.sh tonight stores an expiry; once that passes the flag is
# removed here.
night_mode_active() {
    read_night_mode "$NIGHT_MODE_FILE" || return 1
    if [ -n "$NM_EXPIRES" ] && [ "$NM_EXPIRES" -le "$now" ]; then
        rm -f "$NIGHT_MODE_FILE"
        log "night mode expired, switched off"
        return 1
    fi
    return 0
}

fail_count=$(read_file "$FAIL_COUNT_FILE" 0)
threshold=$(read_file "$THRESHOLD_FILE" "$THRESHOLD_START")
last_state=$(read_file "$STATE_FILE" "")

# ---- presence: one probe run checks every phone with every method in parallel
probe_out=$(timeout -k 2 "$PROBE_TIMEOUT" "${PROBE_CMD[@]}" 2>&1)
probe_rc=$?
case "$probe_rc" in
    0) phone_home=true ;;
    1) phone_home=false ;;
    *)
        # A broken detector must never arm the cameras: assume someone is home,
        # and say so once rather than every 10 seconds.
        phone_home=true
        if [ ! -f "$PROBE_ERROR_FILE" ]; then
            log "phone probe failed (rc=$probe_rc): ${probe_out:-no output}; assuming someone is home"
            touch "$PROBE_ERROR_FILE"
            notify high warning "Presence probe broken" \
                "phone_probe.py failed (rc=$probe_rc): ${probe_out:-no output}. Assuming someone is home, so the cameras will not arm until it works again."
        fi
        ;;
esac
if [ "$probe_rc" -le 1 ] && [ -f "$PROBE_ERROR_FILE" ]; then
    rm -f "$PROBE_ERROR_FILE"
    log "phone probe working again"
    notify low white_check_mark "Presence probe working again" "phone_probe.py answers again, presence control resumed."
fi

# ---- night window
night_now=0
night_window=""
if night_mode_active; then
    night_window="${NM_START}-${NM_END}"
    in_window "$now_min" "$NM_START_MIN" "$NM_END_MIN" && night_now=1
fi
last_night=$(read_file "$NIGHT_WINDOW_FILE" 0)

desired_state=""
if $phone_home; then
    desired_state="disable"
    fail_count=0
else
    fail_count=$((fail_count + 1))
    [ "$fail_count" -ge "$threshold" ] && desired_state="enable"
fi

echo "$fail_count" > "$FAIL_COUNT_FILE"

if [ -n "$desired_state" ] && [ "$desired_state" != "$last_state" ]; then
    state_changed=true
    target="$desired_state"
else
    state_changed=false
    # nothing new to do; only relevant for finishing an incomplete switch
    target="${desired_state:-$last_state}"
fi

[ -z "$target" ] && exit 0

if $state_changed; then
    if [ "$target" = "disable" ] && [ "$last_state" = "enable" ]; then
        enabled_at=$(read_file "$ENABLE_TIME_FILE" 0)
        elapsed=$((now - enabled_at))
        if [ "$enabled_at" -gt 0 ] && [ "$elapsed" -lt "$SHORT_TRIP_SECS" ]; then
            threshold=$((threshold + THRESHOLD_STEP))
            [ "$threshold" -gt "$THRESHOLD_MAX" ] && threshold=$THRESHOLD_MAX
            echo "$threshold" > "$THRESHOLD_FILE"
            log "cameras were only on for ${elapsed}s, raising away threshold to $threshold checks"
        fi
    fi

    log "state change: '$last_state' -> '$target' (fail_count=$fail_count, threshold=$threshold, probe: ${probe_out:-n/a})"

    # Record the intent BEFORE touching any camera. If some of them fail we must
    # still notice the next change and act on it, instead of thinking we are
    # already in the old state forever. cam_recorder.py follows this file too.
    echo "$target" > "$STATE_FILE"
    [ "$target" = "enable" ] && echo "$now" > "$ENABLE_TIME_FILE"
    echo "$night_now" > "$NIGHT_WINDOW_FILE"

    # A real state change supersedes any pending retry, so drop the backoff and
    # act immediately rather than waiting out a cooldown from an earlier failure.
    rm -f "$RETRY_AFTER_FILE" "$FAIL_STREAK_FILE"

    apply_state "$target" "$now"

elif [ "$night_now" != "$last_night" ]; then
    # The night window opened or closed (or night mode was switched) while the
    # presence state stayed the same: tapo_toggle.py recomputes every camera's
    # target from the current state, so re-applying it moves just the night group.
    echo "$night_now" > "$NIGHT_WINDOW_FILE"
    if [ "$night_now" = 1 ]; then
        log "night window $night_window opened, re-syncing cameras for '$target'"
    else
        log "night window closed, re-syncing cameras for '$target'"
    fi
    rm -f "$RETRY_AFTER_FILE" "$FAIL_STREAK_FILE"

    apply_state "$target" "$now"

elif [ -f "$PENDING_FILE" ]; then
    retry_after=$(read_file "$RETRY_AFTER_FILE" 0)
    if [ "$now" -ge "$retry_after" ]; then
        log "retrying incomplete switch to '$target'"
        apply_state "$target" "$now"
    fi
fi
