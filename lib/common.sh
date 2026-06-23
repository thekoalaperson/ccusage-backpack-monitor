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
