#!/usr/bin/env bash
# Shared helpers for ccusage-backpack-monitor.
# Sourced by bin/open-pane.sh, bin/close-pane.sh, and bin/watch.sh.

# Resolve how to invoke ccusage. Prints a command string that may contain
# spaces (e.g. the npx fallback), so callers should run it via `eval`.
cbm_ccusage() {
  if command -v ccusage >/dev/null 2>&1; then printf 'ccusage'; return 0; fi
  local d
  for d in /opt/homebrew/bin /usr/local/bin "$HOME/.bun/bin" "$HOME/.deno/bin" "$HOME/.local/bin"; do
    if [ -x "$d/ccusage" ]; then printf '%s/ccusage' "$d"; return 0; fi
  done
  # No ccusage binary installed. Fall back to a package runner, but ONLY if one
  # actually exists. Otherwise print nothing and return 1, so callers can show a
  # clear "install ccusage" message instead of emitting an unrunnable command
  # that dies with a cryptic "npx: command not found".
  if command -v npx  >/dev/null 2>&1; then printf 'npx -y ccusage@latest';         return 0; fi
  if command -v bunx >/dev/null 2>&1; then printf 'bunx ccusage@latest';            return 0; fi
  if command -v deno >/dev/null 2>&1; then printf 'deno run -A npm:ccusage@latest'; return 0; fi
  return 1
}

# Human-facing explanation shown when ccusage can't be run at all. Centralized
# here so the in-pane watcher and the SessionStart hook surface the SAME fix.
# $1 = "plain" (default, multi-line for the pane) or "oneline" (for hook output).
cbm_no_ccusage_msg() {
  if [ "$1" = "oneline" ]; then
    printf 'ccusage-backpack-monitor: ccusage not found (and no node/bun/deno to run it). Install it with: brew install ccusage'
    return
  fi
  cat <<'MSG'
ccusage-backpack-monitor

  ccusage is this panel's data source, but it isn't installed — and no JS
  runtime (node / bun / deno) was found to run it. So there's nothing to show.

  Fix it (this also pulls in node):

      brew install ccusage

  No Homebrew yet? Install it first, then run the line above:

      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  Then start a fresh claude, or run  /ccusage-backpack-monitor:ccusage-monitor
  Docs: https://ccusage.com/guide/installation
MSG
}

# Ensure common bin dirs are on PATH. Needed because the terminal execs the
# watcher with a bare, non-login shell that lacks Homebrew/npm paths.
cbm_fix_path() {
  export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.bun/bin:$HOME/.local/bin:$PATH"
}

# Directory for tiny pane-id state files. MUST be identical across contexts:
# the SessionStart/SessionEnd hooks and the /ccusage-monitor command all read it.
# We deliberately do NOT use $CLAUDE_PLUGIN_DATA — it's set in hook context but
# not in slash-command (!-bash) context, which would split the open and close
# sides into different dirs so the close hook couldn't find the command's pane.
cbm_state_dir() {
  local d="${XDG_STATE_HOME:-$HOME/.local/state}/ccusage-backpack-monitor"
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
# stash the new pane id. Idempotent. Shared by the SessionStart hook and the
# /ccusage-monitor command. Returns: 0 opened, 2 already open, 1 not opened —
# so callers need no extra cbm_pane_alive queries to report the outcome.
cbm_open_pane() {
  local sid="$1" trans="$2"
  [ -z "$sid" ] && return 1
  cbm_is_iterm || return 1

  local libdir watcher state
  libdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  watcher="$libdir/../bin/watch.sh"
  state="$(cbm_state_dir)"

  # Self-pruning: drop stale pane-id files orphaned by crashes/reboots (>1 day).
  find "$state" -name '*.pane' -mtime +1 -delete 2>/dev/null

  # Idempotency: if a live pane already exists for this session, don't reopen.
  local prev="$state/$sid.pane"
  if [ -f "$prev" ] && cbm_pane_alive "$(cat "$prev" 2>/dev/null)"; then
    return 2
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

  [ -z "$paneid" ] && return 1
  printf '%s' "$paneid" > "$state/$sid.pane"
  return 0
}

# Single-quote a string for safe use as one shell word.
cbm_shq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# Quote $1 as a JSON string literal (escapes backslash and double-quote). Our
# messages are single-line, so this is sufficient for embedding in hook output.
cbm_json_quote() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}
