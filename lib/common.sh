#!/usr/bin/env bash
# Shared helpers for ccusage-backpack-monitor.
# Sourced (by bash) from bin/open-pane.sh, bin/close-pane.sh, bin/open-now.sh,
# and bin/watch.sh.
#
# Terminal support is pluggable. Each "backend" (iterm, tmux, wezterm) implements
# four operations as plain functions named cbm_<op>_<backend>:
#     cbm_detect_<be>   -> 0 if this terminal is active
#     cbm_open_<be>     <cmd> <split>  -> prints an opaque pane handle on stdout
#     cbm_alive_<be>    <handle>       -> 0 if that pane is still open
#     cbm_close_<be>    <handle>       -> close that pane (best-effort)
# A cached cbm_backend() resolves the active backend in priority order, and the
# public verbs (cbm_open_pane / cbm_pane_alive / cbm_pane_close) dispatch to it.
# Adding kitty/Ghostty later = four functions + one word in cbm_backend's loop.

# ---------------------------------------------------------------------------
# OS + environment helpers
# ---------------------------------------------------------------------------

# True on macOS. Used to tailor install hints (brew vs npm) and PATH.
cbm_is_macos() { [ "$(uname -s 2>/dev/null)" = "Darwin" ]; }

# Resolve how to invoke ccusage. Prints a command string that may contain
# spaces (e.g. the npx fallback), so callers should run it via `eval`.
cbm_ccusage() {
  if command -v ccusage >/dev/null 2>&1; then printf 'ccusage'; return 0; fi
  local d
  for d in /opt/homebrew/bin /usr/local/bin "$HOME/.bun/bin" "$HOME/.deno/bin" "$HOME/.local/bin" "$HOME/.npm-global/bin"; do
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
# The install command is OS-aware now that Linux (via tmux/WezTerm) is supported:
# Homebrew on macOS, npm/bun elsewhere.
# $1 = "plain" (default, multi-line for the pane) or "oneline" (for hook output).
cbm_no_ccusage_msg() {
  local install hint
  if cbm_is_macos; then
    install='brew install ccusage'
    hint='No Homebrew yet? Install it first, then run the line above:
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  else
    install='npm install -g ccusage'
    hint='No npm? Install Node.js (your package manager, or https://nodejs.org),
      or use bun:  bun add -g ccusage'
  fi
  if [ "$1" = "oneline" ]; then
    printf 'ccusage-backpack-monitor: ccusage not found (and no node/bun/deno to run it). Install it with: %s' "$install"
    return
  fi
  cat <<MSG
ccusage-backpack-monitor

  ccusage is this panel's data source, but it isn't installed — and no JS
  runtime (node / bun / deno) was found to run it. So there's nothing to show.

  Fix it:

      $install

  $hint

  Then start a fresh claude, or run  /ccusage-backpack-monitor:ccusage-monitor
  Docs: https://ccusage.com/guide/installation
MSG
}

# Ensure common bin dirs are on PATH. Needed because the terminal execs the
# watcher with a bare, non-login shell that lacks Homebrew/npm paths. Covers
# both macOS (Homebrew) and Linux (npm-global / local) install locations.
cbm_fix_path() {
  export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:$HOME/.bun/bin:$HOME/.deno/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/node_modules/.bin:$PATH"
}

# Directory for tiny pane-state files. MUST be identical across contexts:
# the SessionStart/SessionEnd hooks and the /ccusage-monitor command all read it.
# We deliberately do NOT use $CLAUDE_PLUGIN_DATA — it's set in hook context but
# not in slash-command (!-bash) context, which would split the open and close
# sides into different dirs so the close hook couldn't find the command's pane.
cbm_state_dir() {
  local d="${XDG_STATE_HOME:-$HOME/.local/state}/ccusage-backpack-monitor"
  mkdir -p "$d" 2>/dev/null
  printf '%s' "$d"
}

# ---------------------------------------------------------------------------
# Backend resolution + dispatch
# ---------------------------------------------------------------------------

# Detectors: return 0 when that terminal is the active one. Env vars are read
# with ${VAR:-} so the detectors are safe even under `set -u`.
cbm_detect_tmux()    { [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; }
cbm_detect_wezterm() { { [ -n "${WEZTERM_PANE:-}" ] || [ "${TERM_PROGRAM:-}" = "WezTerm" ]; } && command -v wezterm >/dev/null 2>&1; }
cbm_detect_iterm()   { [ "${TERM_PROGRAM:-}" = "iTerm.app" ] || [ -n "${ITERM_SESSION_ID:-}" ]; }

# Back-compat alias: older call sites / external scripts may still ask this.
cbm_is_iterm() { cbm_detect_iterm; }

# Resolve the active backend, in priority order, and memoize it for this process.
# tmux ranks FIRST: $TMUX is set even when tmux runs inside iTerm2/WezTerm, and a
# GUI split there would live outside the multiplexer's pane tree (alive/close,
# which speak tmux, could never reconcile it). The multiplexer owns the layout.
# A pre-set CBM_BACKEND env var forces a backend (handy for tests / overrides);
# it is assigned, never exported, so a child with a different env re-resolves.
cbm_backend() {
  if [ -n "${CBM_BACKEND:-}" ]; then printf '%s' "$CBM_BACKEND"; return 0; fi
  local b
  for b in tmux wezterm iterm; do
    if "cbm_detect_$b"; then CBM_BACKEND="$b"; printf '%s' "$b"; return 0; fi
  done
  return 1
}

# Is any supported terminal active? Replaces cbm_is_iterm at the hook gates.
cbm_is_supported() { cbm_backend >/dev/null 2>&1; }

# Guard: only dispatch to a cbm_<op>_<backend> that actually exists, so a corrupt
# or future-version state tag fails safe (no-op) rather than invoking an
# arbitrary function name.
cbm_dispatch_ok() {  # $1=op (open|alive|close)  $2=backend
  command -v "cbm_${1}_$2" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# State file: two lines — line 1 = backend id, line 2 = opaque handle.
# A backend tag is essential because close-pane.sh (SessionEnd) is a FRESH
# process whose ambient $TMUX/$TERM_PROGRAM may no longer match open time; it
# must close via the RECORDED backend, never by re-detecting. Pre-0.6 files were
# a single line (a bare iTerm session id) — those are read as backend=iterm.
# ---------------------------------------------------------------------------
cbm_state_write() {  # $1=path $2=backend $3=handle
  printf '%s\n%s\n' "$2" "$3" > "$1"
}
cbm_state_backend() {  # $1=path -> backend id (legacy single-line => iterm)
  local first second
  first="$(sed -n 1p "$1" 2>/dev/null)"
  second="$(sed -n 2p "$1" 2>/dev/null)"
  case "$first" in
    tmux|wezterm|iterm) [ -n "$second" ] && { printf '%s' "$first"; return; } ;;
  esac
  printf 'iterm'
}
cbm_state_handle() {  # $1=path -> opaque handle
  local first second
  first="$(sed -n 1p "$1" 2>/dev/null)"
  second="$(sed -n 2p "$1" 2>/dev/null)"
  case "$first" in
    tmux|wezterm|iterm) [ -n "$second" ] && { printf '%s' "$second"; return; } ;;
  esac
  printf '%s' "$first"   # legacy: the whole single line is the iTerm id
}

# ---------------------------------------------------------------------------
# Public verbs (dispatch to the resolved/recorded backend)
# ---------------------------------------------------------------------------

# Is pane <handle> still open? Backend defaults to the current one but callers
# (the idempotency check, close-pane.sh) pass the RECORDED backend explicitly.
cbm_pane_alive() {  # $1=handle  $2=backend(optional)
  [ -n "$1" ] || return 1
  local be="${2:-$(cbm_backend)}"
  [ -n "$be" ] || return 1
  cbm_dispatch_ok alive "$be" || return 1
  "cbm_alive_$be" "$1"
}

# Close pane <handle> via <backend>. Best-effort: unknown backend / vanished CLI
# is a silent no-op (the per-backend impls also swallow errors).
cbm_pane_close() {  # $1=handle  $2=backend(optional)
  [ -n "$1" ] || return 0
  local be="${2:-$(cbm_backend)}"
  [ -n "$be" ] || return 0
  cbm_dispatch_ok close "$be" || return 0
  "cbm_close_$be" "$1"
}

# Open the monitor pane for session <sid> (transcript <trans>, may be empty) and
# stash a backend-tagged state file. Idempotent. Shared by the SessionStart hook
# and the /ccusage-monitor command. Returns: 0 opened, 2 already open, 1 not
# opened — so callers need no extra liveness queries to report the outcome.
cbm_open_pane() {  # $1=sid  $2=trans
  local sid="$1" trans="$2"
  [ -z "$sid" ] && return 1

  local backend
  backend="$(cbm_backend)" || return 1            # no supported terminal -> 1
  cbm_dispatch_ok open "$backend" || return 1

  local libdir watcher state
  libdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  watcher="$libdir/../bin/watch.sh"
  state="$(cbm_state_dir)"

  # Self-pruning: drop stale state files orphaned by crashes/reboots (>1 day).
  find "$state" -name '*.pane' -mtime +1 -delete 2>/dev/null

  # Idempotency: if a live pane already exists for this session, don't reopen.
  # Check the RECORDED backend's handle, not the ambient one.
  local prev="$state/$sid.pane" pb ph
  if [ -f "$prev" ]; then
    pb="$(cbm_state_backend "$prev")"
    ph="$(cbm_state_handle "$prev")"
    if [ -n "$ph" ] && cbm_pane_alive "$ph" "$pb"; then return 2; fi
  fi

  local poll="${CBM_POLL:-3}" split="${CBM_SPLIT:-vertically}"
  case "$poll" in ''|*[!0-9]*) poll=3 ;; esac
  [ "$poll" -lt 1 ] && poll=1
  case "$split" in vertically|horizontally) ;; *) split=vertically ;; esac

  # Build the watcher invocation as a self-contained shell program: each arg is
  # single-quoted (cbm_shq) so a hostile sid/transcript can't break out. Each
  # backend then runs this string through exactly ONE more parse layer (sh -c,
  # or iTerm's shell) — never re-interpreting the user data in its own language.
  # The absolute watcher path is baked in so the new pane finds it regardless
  # of cwd/OS.
  local cmd handle
  cmd="$watcher $(cbm_shq "$sid") $(cbm_shq "$trans") $(cbm_shq "$poll")"
  handle="$("cbm_open_$backend" "$cmd" "$split")" || return 1
  [ -z "$handle" ] && return 1
  cbm_state_write "$prev" "$backend" "$handle"   # overwrites any stale file
  return 0
}

# ---------------------------------------------------------------------------
# Backend: tmux  (Linux + macOS). Handle = a tmux pane id like %3.
# ---------------------------------------------------------------------------
# Split map preserves the iTerm semantics (vertically = side-by-side): tmux's -h
# splits left/right, -v splits top/bottom (the classic tmux naming inversion).
cbm_open_tmux() {  # $1=cmd  $2=split  -> prints %N
  local cmd="$1" dir=-h
  [ "$2" = horizontally ] && dir=-v
  # Build argv so -t <pane> is only added when $TMUX_PANE is set. Everything
  # after `--` is forwarded verbatim to exec as [sh, -c, <cmd>]; tmux does NO
  # word-splitting there, so sh -c is the sole parser of cmd's inner quoting.
  set -- split-window "$dir" -d -P -F '#{pane_id}'
  [ -n "$TMUX_PANE" ] && set -- "$@" -t "$TMUX_PANE"
  set -- "$@" -- /bin/sh -c "$cmd"
  tmux "$@" 2>/dev/null
}
cbm_alive_tmux() {  # $1=handle
  [ -n "$1" ] || return 1
  # -Fxq: whole-line, fixed-string, quiet — so %3 never matches %30 and a leading
  # % is literal.
  tmux list-panes -a -F '#{pane_id}' 2>/dev/null | grep -Fxq "$1"
}
cbm_close_tmux() {  # $1=handle
  [ -n "$1" ] || return 0
  tmux kill-pane -t "$1" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Backend: WezTerm  (Linux + macOS). Handle = an integer pane id like 12.
# ---------------------------------------------------------------------------
cbm_open_wezterm() {  # $1=cmd  $2=split  -> prints <int>
  local cmd="$1" dir=--right pct="${CBM_WEZTERM_PERCENT:-40}"
  [ "$2" = horizontally ] && dir=--bottom
  case "$pct" in ''|*[!0-9]*) pct=40 ;; esac
  # Only pass --pane-id when WEZTERM_PANE is set (never emit --pane-id ''). The
  # post-`--` argv goes straight to exec — same injection-safe model as tmux.
  set -- cli split-pane "$dir" --percent "$pct"
  [ -n "$WEZTERM_PANE" ] && set -- "$@" --pane-id "$WEZTERM_PANE"
  set -- "$@" -- /bin/sh -c "$cmd"
  wezterm "$@" 2>/dev/null
}
cbm_alive_wezterm() {  # $1=handle
  [ -n "$1" ] || return 1
  # JSON is the stable interface (the column layout of the text table drifts
  # across versions). Split on , { } so each "pane_id": N is isolated, then
  # anchor the integer so id 1 != 12. Parse failure => not found (safe: the
  # caller reopens rather than mis-reporting a dead pane as alive).
  wezterm cli list --format json 2>/dev/null \
    | tr ',{}' '\n\n\n' \
    | grep -Eq "\"pane_id\"[[:space:]]*:[[:space:]]*$1([^0-9]|\$)"
}
cbm_close_wezterm() {  # $1=handle
  [ -n "$1" ] || return 0
  wezterm cli kill-pane --pane-id "$1" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Backend: iTerm2  (macOS). Handle = an iTerm session id. AppleScript is moved
# here verbatim from the old cbm_open_pane / close-pane.sh / cbm_pane_alive, so
# iTerm behavior is byte-for-byte unchanged.
# ---------------------------------------------------------------------------
cbm_open_iterm() {  # $1=cmd  $2=split  -> prints iTerm session id
  # Single-quote each arg for the shell, then escape the whole string for the
  # AppleScript double-quoted literal (\ and ").
  local osa_cmd
  osa_cmd="$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  /usr/bin/osascript 2>/dev/null <<OSA
tell application "iTerm2"
  tell current session of current window
    set newSession to (split $2 with same profile command "$osa_cmd")
  end tell
  id of newSession
end tell
OSA
}
cbm_alive_iterm() {  # $1=handle
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
cbm_close_iterm() {  # $1=handle
  [ -n "$1" ] || return 0
  /usr/bin/osascript >/dev/null 2>&1 <<OSA
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "$1" then close s
      end repeat
    end repeat
  end repeat
end tell
OSA
}

# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

# Portable change-signature for a file: "<mtime>-<size>". GNU stat FIRST —
# on Linux, BSD `stat -f '%m-%z'` does NOT fail cleanly: `-f` means filesystem
# info, so it prints a churning free-block table to stdout (exit 1), which would
# make the signature differ on nearly every poll and re-render constantly. On
# macOS `stat -c` is an invalid flag (empty, nonzero) so it falls through to -f.
cbm_stat_sig() {  # $1=path
  stat -c '%Y-%s' "$1" 2>/dev/null && return   # GNU/Linux
  stat -f '%m-%z' "$1" 2>/dev/null && return   # BSD/macOS
  return 1
}

# Resolve a usable login/interactive shell. $SHELL if set, else bash, else sh.
cbm_login_shell() {
  if [ -n "$SHELL" ]; then printf '%s' "$SHELL"; return; fi
  command -v bash >/dev/null 2>&1 && { command -v bash; return; }
  printf '/bin/sh'
}
# Drop the pane into an interactive shell. Use -il only for bash/zsh; plain
# /bin/sh (dash) rejects -l and would kill the pane.
cbm_exec_shell() {
  local sh
  sh="$(cbm_login_shell)"
  case "$(basename "$sh")" in
    bash|zsh) exec "$sh" -il ;;
    *)        exec "$sh" -i  ;;
  esac
}

# Read one top-level string field from hook JSON supplied on stdin.
# Pure shell (no python3) so the hooks work even without python3 installed —
# the rich panel still degrades to plain text in that case, as documented.
cbm_json_field() {
  sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
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
