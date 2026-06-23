#!/usr/bin/env bash
# Shared helpers for ccusage-backpack-monitor.
# Sourced by bin/open-pane.sh, bin/close-pane.sh, and bin/watch.sh.

# Resolve how to invoke ccusage. Prints a command string that may contain
# spaces (e.g. the npx fallback), so callers should run it via `eval`.
cbm_ccusage() {
  if command -v ccusage >/dev/null 2>&1; then printf 'ccusage'; return; fi
  local d
  for d in /opt/homebrew/bin /usr/local/bin "$HOME/.bun/bin" "$HOME/.deno/bin" "$HOME/.local/bin"; do
    if [ -x "$d/ccusage" ]; then printf '%s/ccusage' "$d"; return; fi
  done
  printf 'npx -y ccusage@latest'
}

# Ensure common bin dirs are on PATH. Needed because the terminal execs the
# watcher with a bare, non-login shell that lacks Homebrew/npm paths.
cbm_fix_path() {
  export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.bun/bin:$HOME/.local/bin:$PATH"
}

# Directory for tiny pane-id state files. Prefers the plugin's persistent data
# dir (set by Claude Code); falls back to an XDG-style path for standalone use.
cbm_state_dir() {
  local d="${CLAUDE_PLUGIN_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/ccusage-backpack-monitor}"
  mkdir -p "$d" 2>/dev/null
  printf '%s' "$d"
}

# Are we running under iTerm2? It's the only terminal we can script today, so
# the hooks no-op gracefully everywhere else.
cbm_is_iterm() {
  [ "$TERM_PROGRAM" = "iTerm.app" ] || [ -n "$ITERM_SESSION_ID" ]
}

# Read one top-level string field from hook JSON supplied on stdin.
# Pure shell (no python3) so the hooks work even without python3 installed —
# the rich panel still degrades to plain text in that case, as documented.
cbm_json_field() {
  sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
}

# Is the iTerm pane with id $1 currently alive?
cbm_pane_alive() {
  [ -n "$1" ] || return 1
  local r
  r="$(/usr/bin/osascript 2>/dev/null <<OSA
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "$1" then return "1"
      end repeat
    end repeat
  end repeat
end tell
return "0"
OSA
)"
  [ "$r" = "1" ]
}

# Open the monitor pane for session <sid> (transcript <trans>, may be empty) and
# stash the new pane id. Idempotent: no-op if a live pane already exists for it.
# Shared by the SessionStart hook and the /ccusage-monitor command.
cbm_open_pane() {
  local sid="$1" trans="$2"
  [ -z "$sid" ] && return 0
  cbm_is_iterm || return 0

  local libdir watcher state
  libdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  watcher="$libdir/../bin/watch.sh"
  state="$(cbm_state_dir)"

  # Self-pruning: drop stale pane-id files orphaned by crashes/reboots (>1 day).
  find "$state" -name '*.pane' -mtime +1 -delete 2>/dev/null

  # Idempotency: if a live pane already exists for this session, don't reopen.
  local prev="$state/$sid.pane"
  if [ -f "$prev" ] && cbm_pane_alive "$(cat "$prev" 2>/dev/null)"; then
    return 0
  fi

  local poll="${CBM_POLL:-3}" split="${CBM_SPLIT:-vertically}"
  case "$poll" in ''|*[!0-9]*) poll=3 ;; esac
  [ "$poll" -lt 1 ] && poll=1
  case "$split" in vertically|horizontally) ;; *) split=vertically ;; esac

  # Single-quote each arg for the shell (handles spaces/metachars), then escape
  # the whole string for the AppleScript double-quoted literal (\ and ").
  local cmd osa_cmd paneid
  cmd="$watcher $(cbm_shq "$sid") $(cbm_shq "$trans") $(cbm_shq "$poll")"
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
}

# Single-quote a string for safe use as one shell word.
cbm_shq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
