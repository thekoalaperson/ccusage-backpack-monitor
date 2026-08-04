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

# ---------------------------------------------------------------------------
# Tabs. The panel draws a pinned header on every tab, so switching is for
# digging, not for watching -- which matters because the pane has to be focused
# before it sees a keystroke at all.
# ---------------------------------------------------------------------------
tab=0
ntabs=6
# Interactive only when stdin is a terminal AND tabs are enabled. Without the
# tty test `read` would fail instantly instead of blocking, turning the poll
# into a busy loop -- the exact opposite of this tool's promise.
interactive=0
if [ "${CBM_TABS:-1}" != "0" ] && [ -t 0 ]; then interactive=1; fi

# Rich python panel when available; otherwise fall back to plain ccusage output.
render() {
  if [ -n "$py" ] && [ -f "$here/../lib/render.py" ]; then
    "$py" "$here/../lib/render.py" "$sid" "$transcript" "$tab" 2>/dev/null && return
  fi
  [ -n "$ccu" ] || return 0
  eval "$ccu session -i $(cbm_shq "$sid")" 2>&1
  gray ""
  gray "session ${sid:0:8}  |  live (updates on change)  |  Ctrl-C to stop"
}

# Wait up to $poll seconds for a keystroke, printing nothing. Sets $key to a
# logical name ('' when it simply timed out, which is the normal idle path).
#
# This read REPLACES the sleep -- it is the poll interval, not an addition to
# it. Idle cost is unchanged: one timed read, then the same cheap stat check.
#
# bash 3.2 (what macOS ships) rejects a fractional `-t`, so the tail of an
# escape sequence is read with `-t 1`; those bytes always arrive in the same
# burst as the ESC, so it returns immediately rather than waiting.
read_key() {
  key=""
  local rest=""
  read -rsn1 -t "$poll" key 2>/dev/null || { key=""; return 0; }
  case "$key" in
    $'\033')
      read -rsn2 -t 1 rest 2>/dev/null
      # '[' is the normal cursor-key mode; 'O' is application cursor mode,
      # which tmux and some terminals send instead. Both must work.
      case "$rest" in
        '[C'|'OC') key=right ;;
        '[D'|'OD') key=left  ;;
        '[B'|'OB') key=right ;;   # down == next, up == prev: same axis, one strip
        '[A'|'OA') key=left  ;;
        *) key="" ;;
      esac
      ;;
    $'\t') key=right ;;
  esac
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

# A resize changes the width every row is fitted to, so the panel must be
# redrawn or it stays laid out for the old pane.
resized=0
help_once=0
trap 'resized=1' WINCH

# Sentinel (not "") so the first iteration always renders — and so a host where
# stat yields no signature still renders once before idling, rather than never.
last="__init__"
force=0
while true; do
  locate
  if [ -z "$transcript" ] || [ ! -f "$transcript" ]; then
    clear
    gray "waiting for session ${sid:0:8} data..."
    if [ "$interactive" = 1 ]; then read_key; else sleep "$poll"; fi
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
  # No render unless something actually changed -- or the user asked for a
  # different view, which must feel instant rather than wait for the next turn.
  sig="$(signature)"
  if [ "$sig" != "$last" ] || [ "$force" = 1 ] || [ "$resized" = 1 ]; then
    last="$sig"
    force=0
    resized=0
    clear
    # The key list is a one-shot: shown on the render right after `?`, gone on
    # the next one, so it can never become clutter in a 25%-width pane.
    if [ "$help_once" = 1 ]; then
      CBM_HELP=1 render
      help_once=0
    else
      render
    fi
  fi

  if [ "$interactive" != 1 ]; then
    sleep "$poll"
    continue
  fi

  read_key
  case "$key" in
    right)   tab=$(( (tab + 1) % ntabs ));         force=1 ;;
    left)    tab=$(( (tab - 1 + ntabs) % ntabs )); force=1 ;;
    [1-6])   tab=$(( key - 1 ));                   force=1 ;;
    r|R)                                           force=1 ;;
    '?'|h|H) help_once=1;                          force=1 ;;
    q|Q)     clear; cbm_exec_shell ;;
  esac
done
