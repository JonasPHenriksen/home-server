#!/bin/bash
#
# Switch night mode on or off, optionally with the time window it applies to.
#
#   night_mode.sh on [HH:MM-HH:MM]        every night until switched off
#   night_mode.sh tonight [HH:MM-HH:MM]   until the window next closes, then off by itself
#   night_mode.sh off
#   night_mode.sh status
#
# The default window is 21:00-02:00. A window may cross midnight. Without a
# window argument the default is used, so give the window whenever the season
# calls for a different one, e.g. `night_mode.sh on 20:00-04:00`.
#
# With night mode on, the cameras in the "night" group (config.json) watch
# quietly during the window whatever the presence state: privacy mode off,
# motion detection on, alarm off. Outside the window they follow presence like
# every other camera. The presence loop notices changes within about 10 s and
# re-syncs the cameras; nothing is touched from here.
#
# Environment overrides for the tests: TAPO_BASE_DIR, TAPO_NOW.

BASE_DIR="${TAPO_BASE_DIR:-/home/dietpi/tapo-automation}"
FLAG="$BASE_DIR/.tapo_night_mode"
LOG_FILE="$BASE_DIR/tapo.log"

# shellcheck source=night_common.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/night_common.sh"

now="${TAPO_NOW:-$(date +%s)}"
now_min=$(to_minutes "$(date -d "@$now" +%H:%M)")
ZONE=$(date -d "@$now" +%Z)   # all times are host time

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [night] $1" >> "$LOG_FILE"
}

usage() {
    echo "usage: $(basename "$0") on|tonight [HH:MM-HH:MM] | off | status" >&2
    echo "       default window ${NIGHT_DEFAULT_START}-${NIGHT_DEFAULT_END}, times in host time ($ZONE)" >&2
    exit 2
}

fmt_epoch() {
    date -d "@$1" '+%Y-%m-%d %H:%M'
}

# epoch of the next moment the window closes
next_close_epoch() {
    local day today_end
    day=$(date -d "@$now" +%F)
    today_end=$(date -d "$day $WINDOW_END" +%s)
    if [ "$today_end" -le "$now" ]; then
        echo $((today_end + 86400))
    else
        echo "$today_end"
    fi
}

write_flag() {
    local expires="$1"
    {
        echo "start=$WINDOW_START"
        echo "end=$WINDOW_END"
        [ -n "$expires" ] && echo "expires=$expires"
    } > "$FLAG.tmp"
    mv "$FLAG.tmp" "$FLAG"
}

window_state() {
    if in_window "$now_min" "$(to_minutes "$1")" "$(to_minutes "$2")"; then
        echo "window active now"
    else
        echo "window opens at $1"
    fi
}

status() {
    if ! read_night_mode "$FLAG"; then
        echo "night mode: OFF"
        return
    fi
    local state
    state=$(window_state "$NM_START" "$NM_END")
    if [ -n "$NM_EXPIRES" ]; then
        if [ "$NM_EXPIRES" -le "$now" ]; then
            echo "night mode: OFF (expired $(fmt_epoch "$NM_EXPIRES"), cleanup pending)"
        else
            echo "night mode: ON ${NM_START}-${NM_END} ${ZONE} until $(fmt_epoch "$NM_EXPIRES") ($state)"
        fi
    else
        echo "night mode: ON ${NM_START}-${NM_END} ${ZONE}, every night until switched off ($state)"
    fi
}

explain() {
    echo "The night cameras watch quietly (no alarm) between $WINDOW_START and $WINDOW_END $ZONE,"
    echo "whether anyone is home or not. The presence loop applies this within about 10 s."
}

action="${1:-status}"
window_arg="${2:-}"

case "$action" in
    on|tonight)
        [ $# -le 2 ] || usage
        if [ -n "$window_arg" ]; then
            parse_window "$window_arg" || { echo "bad window '$window_arg', expected HH:MM-HH:MM" >&2; usage; }
        else
            WINDOW_START="$NIGHT_DEFAULT_START"
            WINDOW_END="$NIGHT_DEFAULT_END"
        fi
        if [ "$action" = "on" ]; then
            write_flag ""
            log "night mode switched ON, window ${WINDOW_START}-${WINDOW_END}"
            echo "night mode: ON ${WINDOW_START}-${WINDOW_END} ${ZONE}, every night until switched off ($(window_state "$WINDOW_START" "$WINDOW_END"))"
        else
            expires=$(next_close_epoch)
            write_flag "$expires"
            log "night mode switched ON until $(fmt_epoch "$expires"), window ${WINDOW_START}-${WINDOW_END}"
            echo "night mode: ON ${WINDOW_START}-${WINDOW_END} ${ZONE} until $(fmt_epoch "$expires"), then off again ($(window_state "$WINDOW_START" "$WINDOW_END"))"
        fi
        explain
        ;;
    off)
        [ $# -le 1 ] || usage
        rm -f "$FLAG"
        log "night mode switched OFF"
        echo "night mode: OFF"
        echo "The presence loop puts the night cameras back to normal within about 10 s."
        ;;
    status)
        [ $# -le 1 ] || usage
        status
        ;;
    *)
        usage
        ;;
esac
