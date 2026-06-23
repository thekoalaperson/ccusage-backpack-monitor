#!/usr/bin/env bash
# SessionStart hook: open an iTerm2 side pane with a live ccusage readout
# scoped to THIS Claude Code session, and remember the pane so we can close it.
#
# Config (env vars, all optional):
#   CBM_POLL   seconds between cheap file-change checks (default 3)
#   CBM_SPLIT  "vertically" (side-by-side) or "horizontally" (default vertically)
#
# Notes:
#  - First run triggers a one-time macOS Automation prompt (allow iTerm).
#  - Must print nothing to stdout: SessionStart stdout is injected into context.

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"

input="$(cat)"
cbm_is_iterm || exit 0   # only iTerm2 is supported; no-op elsewhere

sid="$(printf '%s' "$input"   | cbm_json_field session_id)"
trans="$(printf '%s' "$input" | cbm_json_field transcript_path)"
[ -z "$sid" ] && exit 0

state="$(cbm_state_dir)"
# Self-pruning: drop stale pane-id files orphaned by crashes/reboots (>1 day).
find "$state" -name '*.pane' -mtime +1 -delete 2>/dev/null

watcher="$here/watch.sh"
poll="${CBM_POLL:-3}"
split="${CBM_SPLIT:-vertically}"

paneid="$(/usr/bin/osascript 2>/dev/null <<OSA
tell application "iTerm2"
  tell current session of current window
    set newSession to (split $split with same profile command "$watcher $sid $trans $poll")
  end tell
  id of newSession
end tell
OSA
)"

[ -n "$paneid" ] && printf '%s' "$paneid" > "$state/$sid.pane"
exit 0
