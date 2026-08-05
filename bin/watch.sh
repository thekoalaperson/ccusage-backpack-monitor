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

# $LINES/$COLUMNS are inherited from whatever opened this pane, and in a split
# they describe the WINDOW: taller and wider than the pane we actually own.
# Everything downstream believes them if they exist -- `tput lines` does,
# python's shutil.get_terminal_size() does -- and painting a pane to the
# window's height is a scroll, which is how a frame reaches the scrollback.
# Drop them once, here, and let every consumer ask the tty instead.
unset LINES COLUMNS

# ---------------------------------------------------------------------------
# Screen handling. The panel redraws in place, so it belongs on the ALTERNATE
# screen buffer -- the one vim, less and htop use -- which leaves whatever the
# pane showed before intact underneath.
#
# What the alternate buffer does NOT do is stop the ghost frames, and believing
# it did cost two rounds of this bug. A terminal banks a line into scrollback
# when the line leaves the screen, and erasing the screen counts: iTerm2's
# "save lines to scrollback in alternate screen mode" is ON by default, so an
# erase-down here banked the very frame it erased, once per redraw, with
# nothing to drop the pile. That was strictly worse than the primary buffer,
# where macOS `clear` at least dropped its own scrollback (\033[3J) before
# banking the frame -- which is why the original bug showed exactly one ghost.
#
# The cure is therefore not a buffer but a discipline: erase nothing. After the
# single clear below, the panel addresses each row absolutely, overwrites it and
# erases only to its own end, never emitting a newline. Nothing leaves the
# screen, so nothing can be banked. Everything here is now just etiquette:
# borrow a buffer, hide the cursor, and hand both back on the way out.
#
# CBM_ALTSCREEN=0 opts out of the buffer; that path drops scrollback explicitly
# (\033[3J AFTER the erase, or it drops everything but the frame just banked).
# ---------------------------------------------------------------------------
altscreen=0
if [ "${CBM_ALTSCREEN:-1}" != "0" ] && [ -t 1 ] &&
   [ -n "${TERM:-}" ] && [ "${TERM:-}" != dumb ]; then
  altscreen=1
fi

screen_enter() {
  [ "$altscreen" = 1 ] || return 0
  printf '\033[?1049h\033[?25l'      # alt buffer, hide the cursor
}

# Always safe to call, including twice: the pane may become a shell at any
# point (q, Ctrl-C, a missing data source) and must not inherit a hidden cursor
# or a screen buffer the shell knows nothing about.
screen_leave() {
  [ "$altscreen" = 1 ] || return 0
  altscreen=0
  printf '\033[?25h\033[?1049l'
}

screen_clear() {
  printf '\033[H\033[2J\033[3J'      # erase, THEN drop the scrollback it banked
}

# Wipe the screen the way the panel draws it: every row addressed, overwritten
# and erased only to its own end, with no newline and no screen-wide erase.
# Slower than \033[2J and that is the entire point -- an erase is how a frame
# gets handed to the terminal to bank, which is the bug this file is about.
screen_blank() {
  local rows r
  rows="$(tput lines 2>/dev/null)"
  case "$rows" in ''|*[!0-9]*) rows="${LINES:-24}" ;; esac
  case "$rows" in ''|*[!0-9]*) rows=24 ;; esac
  printf '\033[?7l'
  r=1
  while [ "$r" -le "$rows" ]; do printf '\033[%d;1H\033[K' "$r"; r=$((r + 1)); done
  printf '\033[H\033[?7h'
}

# Start a fresh screen. On the alternate buffer nothing may be erased, ever; the
# buffer arrives blank anyway, so this is belt and braces. Without it we DO want
# a real clear, to drop both the shell's leftovers and any scrollback banked by
# an older build of this script.
screen_reset() {
  if [ "$altscreen" = 1 ]; then screen_blank; else screen_clear; fi
}

# exec replaces this process, so the EXIT trap never runs on that path.
shell_out() { screen_leave; cbm_exec_shell; }

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
#
# The panel paints itself in place (cursor home, every row overwritten and
# erased to its own end), so it needs no clear beforehand. The ccusage fallback
# is plain text with no cursor control, so that branch has to wipe first -- and
# it is the one path here that genuinely scrolls, being output of unknown
# length. It only runs when python3 is missing entirely.
render() {
  if [ -n "$py" ] && [ -f "$here/../lib/render.py" ]; then
    "$py" "$here/../lib/render.py" "$sid" "$transcript" "$tab" 2>/dev/null && return
  fi
  [ -n "$ccu" ] || return 0
  screen_reset
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

# From here on the panel owns the screen, so every exit path has to hand it
# back: the trap covers signals and normal exits, shell_out covers the two
# paths that exec away instead of exiting.
screen_enter
trap 'screen_leave' EXIT
trap 'shell_out' INT

# The only screen-wide wipe of the whole run. After this the panel overwrites
# itself in place, so nothing is ever handed to the terminal to dispose of.
screen_reset

# A pane can stay open for days, and bash reads this script from an open file
# descriptor by offset as it goes -- so an updated or patched watcher cannot
# take effect in a pane that is already running, no matter what is on disk.
# That is not a footnote: it is why two fixes for the ghost frame above looked
# like they had failed. Notice our own file changing and re-exec into it.
self_sig="$(cbm_stat_sig "$0" 2>/dev/null)"

# Sentinel (not "") so the first iteration always renders — and so a host where
# stat yields no signature still renders once before idling, rather than never.
last="__init__"
force=0
waiting=0
retire_ticks=0
retire_misses=0
while true; do
  locate
  if [ -z "$transcript" ] || [ ! -f "$transcript" ]; then
    # Clear once on the way into this state, then just overwrite the row --
    # erasing per poll would bank a frame per poll, the panel's own bug at a
    # slower tempo. \033[K after the text, so a longer previous row can't show
    # through the end of a shorter one.
    [ "$waiting" = 1 ] || { waiting=1; screen_reset; }
    printf '\033[H\033[90mwaiting for session %s data...\033[0m\033[K' "${sid:0:8}"
    if [ "$interactive" = 1 ]; then read_key; else sleep "$poll"; fi
    continue
  fi
  waiting=0

  # See ourselves out when the session we follow is over.
  #
  # Closing this pane is the SessionEnd hook's job, but a hook is not a
  # guarantee: it runs while the session tears down and can be cut short, and
  # then the pane stays on screen after the session that owns it is gone. The
  # pane is the one thing still running at that point, so it checks.
  #
  # Costs one grep a minute, and only when there is a transcript to watch. Three
  # consecutive misses before acting, so a registry that briefly does not list
  # us -- a resume, a rewritten entry -- cannot retire a live pane.
  retire_ticks=$((retire_ticks + 1))
  if [ "$retire_ticks" -ge "${CBM_RETIRE_CHECK:-20}" ]; then
    retire_ticks=0
    cbm_session_is_live "$sid"
    if [ $? = 1 ]; then
      retire_misses=$((retire_misses + 1))
      if [ "$retire_misses" -ge 3 ]; then
        screen_leave
        cbm_close_own_pane "$sid"
        exit 0
      fi
    else
      retire_misses=0
    fi
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
    # Checked here rather than in the poll: this branch had already decided to
    # do work, so picking up an update costs one stat on a path that is not the
    # idle one. `bash -n` guards against exec'ing a half-written file, and a
    # missing or empty $0 (an upgrade that moved the version directory out from
    # under us) simply means carry on. We do NOT leave the alternate screen
    # first: the successor re-enters it immediately, and switching out and back
    # is itself a frame the terminal could bank.
    new_sig="$(cbm_stat_sig "$0" 2>/dev/null)"
    if [ -n "$new_sig" ] && [ -n "$self_sig" ] && [ "$new_sig" != "$self_sig" ] &&
       [ -s "$0" ] && "${BASH:-bash}" -n "$0" 2>/dev/null; then
      exec "${BASH:-bash}" "$0" "$@"
    fi
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
    q|Q)     shell_out ;;
  esac
done
