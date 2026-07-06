#!/usr/bin/env bash
# Live, change-driven ccusage readout for one Claude Code session.
# Usage: watch.sh <session-id> [transcript-path] [poll-seconds]
#
# Cheaply stats this session's transcript every <poll> seconds and only runs
# ccusage when the file actually changed -> near-live but idle-cheap.
# Run as its own program (the terminal execs a script path, not a shell loop).

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
cbm_fix_path

sid="$1"
transcript="$2"
poll="${3:-3}"
case "$poll" in ''|*[!0-9]*) poll=3 ;; esac
[ "$poll" -lt 1 ] && poll=1
ccu="$(cbm_ccusage)"
CBM_PY="$(command -v python3 2>/dev/null)"   # PATH-resolved (not /usr/bin only)

# Ctrl-C drops to an interactive shell instead of closing the pane.
trap 'cbm_exec_shell' INT

if [ -z "$sid" ]; then
  echo "ccusage-backpack-monitor: no session id passed; opening a shell."
  cbm_exec_shell
fi

# No ccusage and no runtime to bootstrap it: there's no data source, so don't
# loop forever on "waiting...". Explain the fix, then become a normal shell so
# the user can run `brew install ccusage` right here.
if [ -z "$ccu" ]; then
  clear
  cbm_no_ccusage_msg
  printf '\n  (this pane is now a shell — paste the command above to fix it)\n\n'
  cbm_exec_shell
fi

gray() { printf '\033[90m%s\033[0m\n' "$1"; }

locate() {
  if [ -n "$transcript" ] && [ -f "$transcript" ]; then return; fi
  transcript="$(find "$HOME/.claude/projects" -name "$sid.jsonl" 2>/dev/null | head -1)"
}

# Rich python panel when available; otherwise fall back to plain ccusage output.
render() {
  if [ -n "$CBM_PY" ] && [ -f "$here/../lib/render.py" ]; then
    CBM_CCUSAGE="$ccu" "$CBM_PY" "$here/../lib/render.py" "$sid" "$transcript" 2>/dev/null && return
  fi
  eval "$ccu session -i $sid --offline" 2>&1
  gray ""
  gray "session ${sid:0:8}  |  live (updates on change)  |  Ctrl-C to stop"
}

# Sentinel (not "") so the first iteration always renders — and so a host where
# stat yields no signature still renders once before idling, rather than never.
last="__init__"
while true; do
  locate
  if [ -z "$transcript" ] || [ ! -f "$transcript" ]; then
    clear
    gray "waiting for session ${sid:0:8} data..."
    sleep "$poll"
    continue
  fi

  # Cheap change signature: mtime-size (portable: GNU stat, then BSD). No render
  # unless changed.
  sig="$(cbm_stat_sig "$transcript")"
  if [ "$sig" != "$last" ]; then
    last="$sig"
    clear
    render
  fi
  sleep "$poll"
done
