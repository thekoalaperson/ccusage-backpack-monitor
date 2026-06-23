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

# Idempotency: if a live pane already exists for this session (e.g. a duplicate
# SessionStart), don't open a second one.
prev="$state/$sid.pane"
if [ -f "$prev" ]; then
  pid="$(cat "$prev" 2>/dev/null)"
  if [ -n "$pid" ]; then
    alive="$(/usr/bin/osascript 2>/dev/null <<OSA
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "$pid" then return "1"
      end repeat
    end repeat
  end repeat
end tell
return "0"
OSA
)"
    [ "$alive" = "1" ] && exit 0
  fi
fi

watcher="$here/watch.sh"
poll="${CBM_POLL:-3}"
case "$poll" in ''|*[!0-9]*) poll=3 ;; esac
[ "$poll" -lt 1 ] && poll=1
split="${CBM_SPLIT:-vertically}"
case "$split" in vertically|horizontally) ;; *) split=vertically ;; esac

# Build the shell command for the pane with each arg single-quoted (handles
# spaces and shell metacharacters in the transcript path), then escape the whole
# string for the AppleScript double-quoted literal (\ and ").
shq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
cmd="$watcher $(shq "$sid") $(shq "$trans") $(shq "$poll")"
osa_cmd="$(printf '%s' "$cmd" | sed 's/\\/\\\\/g; s/"/\\"/g')"

paneid="$(/usr/bin/osascript 2>/dev/null <<OSA
tell application "iTerm2"
  tell current session of current window
    set newSession to (split $split with same profile command "$osa_cmd")
  end tell
  id of newSession
end tell
OSA
)"

[ -n "$paneid" ] && printf '%s' "$paneid" > "$state/$sid.pane"
exit 0
