#!/bin/bash
#
# Runs the presence check forever. Started by systemd (tapo_presence.service).
# The inner script is re-read on every run, so edits take effect within 10 s.
#
# Environment overrides for the tests: TAPO_BASE_DIR, TAPO_LOOP_ONCE=1 (one pass).

BASE_DIR="${TAPO_BASE_DIR:-/home/dietpi/tapo-automation}"
SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

# After a (re)start, e.g. following a power cut, the cameras may have been
# power-cycled too and may no longer match what .tapo_cam_state.json believes.
# Forget that belief and mark the last requested state as pending, so the
# first pass below re-applies it to every camera through the normal retry and
# backoff machinery.
if [ -s "$BASE_DIR/.tapo_motion_state" ]; then
    rm -f "$BASE_DIR/.tapo_cam_state.json" "$BASE_DIR/.tapo_retry_after" "$BASE_DIR/.tapo_switch_fail_streak"
    touch "$BASE_DIR/.tapo_pending"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [phones] loop started, re-syncing every camera to '$(cat "$BASE_DIR/.tapo_motion_state")'" >> "$BASE_DIR/tapo.log"
fi

while true; do
    "$SCRIPT_DIR/check_phones_and_toggle.sh"
    [ -n "$TAPO_LOOP_ONCE" ] && exit 0
    sleep 10
done
