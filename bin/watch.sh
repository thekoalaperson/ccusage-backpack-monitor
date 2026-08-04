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
mode="${4:-manual}"   # 'auto' = opened by the SessionStart hook
case "$poll" in ''|*[!0-9]*) poll=3 ;; esac
[ "$poll" -lt 1 ] && poll=1
py="$(cbm_python)"
ccu="$(cbm_ccusage)"

# Ctrl-C drops to an interactive shell instead of closing the pane.
trap 'cbm_exec_shell' INT

if [ -z "$sid" ]; then
  echo "ccusage-backpack-monitor: no session id passed; opening a shell."
  cbm_exec_shell
fi

# The panel reads transcripts directly, so python3 alone is enough. Only when
# python3 is missing too do we fall back to ccusage, and only when BOTH are
# missing is there no data source at all.
if [ -z "$py" ] && [ -z "$ccu" ]; then
  clear
  cbm_no_source_msg
  printf '\n  (this pane is now a shell — paste a command above to fix it)\n\n'
  cbm_exec_shell
fi

gray() { printf '\033[90m%s\033[0m\n' "$1"; }

locate() {
  if [ -n "$transcript" ] && [ -f "$transcript" ]; then return; fi
  transcript="$(find -L "$HOME/.claude/projects" -name "$sid.jsonl" 2>/dev/null | head -1)"
}

# Rich python panel when available; otherwise fall back to plain ccusage output.
render() {
  if [ -n "$py" ] && [ -f "$here/../lib/render.py" ]; then
    "$py" "$here/../lib/render.py" "$sid" "$transcript" 2>/dev/null && return
  fi
  [ -n "$ccu" ] || return 0
  eval "$ccu session -i $(cbm_shq "$sid")" 2>&1
  gray ""
  gray "session ${sid:0:8}  |  live (updates on change)  |  Ctrl-C to stop"
}

# Change signature: the session transcript PLUS the directory its subagents
# write into. During a workflow fan-out the main transcript can sit still for
# minutes while agents spend money, so watching it alone would freeze the panel.
# Uses cbm_stat_sig so it stays portable across GNU and BSD stat.
signature() {
  local agents="${transcript%.jsonl}/subagents" f
  cbm_stat_sig "$transcript"
  if [ -d "$agents" ]; then
    # -newer than the transcript would miss idle agents; just fold them all in.
    find "$agents" -name '*.jsonl' -print 2>/dev/null | while IFS= read -r f; do
      cbm_stat_sig "$f"
    done
  fi
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

  # The race the hook cannot win: at SessionStart a teammate's transcript may
  # still be empty, which is indistinguishable from a brand-new human session.
  # Once the file has content the answer is unambiguous, so an auto-opened pane
  # retires itself here. Only 'auto' panes do this — a pane the user asked for
  # stays put and says whose agent it is, which is the existing behaviour.
  if [ "$mode" = auto ] && cbm_is_agent_session "$transcript"; then
    cbm_close_own_pane "$sid"
    exit 0
  fi

  # Cheap change signature: mtime-size per file (portable: GNU stat, then BSD).
  # No render unless something actually changed.
  sig="$(signature)"
  if [ "$sig" != "$last" ]; then
    last="$sig"
    clear
    render
  fi
  sleep "$poll"
done
