#!/bin/bash
#
# Shared helpers for night mode, sourced by night_mode.sh and
# check_phones_and_toggle.sh. tapo_toggle.py reads the same file format.
#
# .tapo_night_mode exists while night mode is on and holds key=value lines:
#   start=21:00          window opens (HH:MM)
#   end=02:00            window closes (HH:MM); may be earlier than start, i.e. past midnight
#   expires=1788700000   optional: epoch after which night mode switches itself off
# An empty file, or one holding only an epoch, means "on with the default window".

NIGHT_DEFAULT_START="21:00"
NIGHT_DEFAULT_END="02:00"

# "7", "07", "7:30", "21:00" are accepted; seconds are not
valid_time() {
    [[ "$1" =~ ^([01]?[0-9]|2[0-3])(:[0-5][0-9])?$ ]]
}

# normalise to HH:MM
norm_time() {
    local h="${1%%:*}" m="00"
    [[ "$1" == *:* ]] && m="${1#*:}"
    printf '%02d:%02d' "$((10#$h))" "$((10#$m))"
}

# HH:MM -> minutes since midnight
to_minutes() {
    local t
    t=$(norm_time "$1")
    echo $((10#${t%%:*} * 60 + 10#${t#*:}))
}

# "HH:MM-HH:MM" -> WINDOW_START / WINDOW_END (normalised). Returns 1 when malformed.
parse_window() {
    local w="$1" s e
    [[ "$w" == *-* ]] || return 1
    s="${w%%-*}"
    e="${w#*-}"
    valid_time "$s" && valid_time "$e" || return 1
    WINDOW_START=$(norm_time "$s")
    WINDOW_END=$(norm_time "$e")
    [ "$WINDOW_START" != "$WINDOW_END" ] || return 1
}

# Read the flag file into NM_START, NM_END (HH:MM), NM_START_MIN, NM_END_MIN and
# NM_EXPIRES (epoch or empty). Returns 1 when the file does not exist (mode off).
read_night_mode() {
    local file="$1" line key val
    [ -f "$file" ] || return 1
    NM_START="$NIGHT_DEFAULT_START"
    NM_END="$NIGHT_DEFAULT_END"
    NM_EXPIRES=""
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line//[[:space:]]/}"
        [ -z "$line" ] && continue
        if [[ "$line" == *=* ]]; then
            key="${line%%=*}"
            val="${line#*=}"
            case "$key" in
                start)   valid_time "$val" && NM_START=$(norm_time "$val") ;;
                end)     valid_time "$val" && NM_END=$(norm_time "$val") ;;
                expires) [[ "$val" =~ ^[0-9]+$ ]] && NM_EXPIRES="$val" ;;
            esac
        elif [[ "$line" =~ ^[0-9]+$ ]]; then
            NM_EXPIRES="$line"
        fi
    done < "$file"
    [ "$NM_START" = "$NM_END" ] && NM_END="$NIGHT_DEFAULT_END"
    NM_START_MIN=$(to_minutes "$NM_START")
    NM_END_MIN=$(to_minutes "$NM_END")
    return 0
}

# in_window NOW_MIN START_MIN END_MIN: is NOW inside the window? Handles
# windows that cross midnight (start > end).
in_window() {
    local now="$1" start="$2" end="$3"
    if [ "$start" -lt "$end" ]; then
        [ "$now" -ge "$start" ] && [ "$now" -lt "$end" ]
    else
        [ "$now" -ge "$start" ] || [ "$now" -lt "$end" ]
    fi
}
