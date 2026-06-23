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
ccu="$(cbm_ccusage)"

# Ctrl-C drops to an interactive shell instead of closing the pane.
trap 'exec "${SHELL:-/bin/zsh}" -il' INT

if [ -z "$sid" ]; then
  echo "ccusage-backpack-monitor: no session id passed; opening a shell."
  exec "${SHELL:-/bin/zsh}" -il
fi

gray() { printf '\033[90m%s\033[0m\n' "$1"; }

locate() {
  if [ -n "$transcript" ] && [ -f "$transcript" ]; then return; fi
  transcript="$(find "$HOME/.claude/projects" -name "$sid.jsonl" 2>/dev/null | head -1)"
}

# Rich python panel when available; otherwise fall back to plain ccusage output.
render() {
  if [ -x /usr/bin/python3 ] && [ -f "$here/../lib/render.py" ]; then
    CBM_CCUSAGE="$ccu" /usr/bin/python3 "$here/../lib/render.py" "$sid" "$transcript" 2>/dev/null && return
  fi
  eval "$ccu session -i $sid --offline" 2>&1
  gray ""
  gray "session ${sid:0:8}  |  live (updates on change)  |  Ctrl-C to stop"
}

last=""
while true; do
  locate
  if [ -z "$transcript" ] || [ ! -f "$transcript" ]; then
    clear
    gray "waiting for session ${sid:0:8} data..."
    sleep "$poll"
    continue
  fi

  # Cheap change signature: mtime-size (BSD stat). No render unless changed.
  sig="$(stat -f '%m-%z' "$transcript" 2>/dev/null)"
  if [ "$sig" != "$last" ]; then
    last="$sig"
    clear
    render
  fi
  sleep "$poll"
done
