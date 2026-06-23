#!/usr/bin/env bash
# SessionEnd hook: close the iTerm2 pane that open-pane.sh created for this
# session, using the pane id stashed at open time.

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"

input="$(cat)"
cbm_is_iterm || exit 0

sid="$(printf '%s' "$input" | cbm_json_field session_id)"
[ -z "$sid" ] && exit 0

state="$(cbm_state_dir)"
f="$state/$sid.pane"
[ -f "$f" ] || exit 0
paneid="$(cat "$f")"
rm -f "$f"
[ -z "$paneid" ] && exit 0

/usr/bin/osascript >/dev/null 2>&1 <<OSA
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "$paneid" then close s
      end repeat
    end repeat
  end repeat
end tell
OSA
exit 0
