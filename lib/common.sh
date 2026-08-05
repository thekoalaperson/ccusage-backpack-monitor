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

# Locate python3, which is what the panel actually needs. Prefer an absolute
# path so a broken pyenv/asdf shim on PATH can't take the pane down; fall back
# to PATH resolution for hosts where python lives elsewhere (Linux, nix).
cbm_python() {
  local d
  for d in /usr/bin /opt/homebrew/bin /usr/local/bin /usr/local/opt/python/libexec/bin; do
    if [ -x "$d/python3" ]; then printf '%s/python3' "$d"; return 0; fi
  done
  if command -v python3 >/dev/null 2>&1; then command -v python3; return 0; fi
  return 1
}

# Human-facing explanation shown when the panel has no way to render at all.
# Centralized here so the in-pane watcher and the SessionStart hook surface the
# SAME fix. OS-aware, since Linux is supported via tmux/WezTerm.
# $1 = "plain" (default, multi-line for the pane) or "oneline" (for hook output).
cbm_no_source_msg() {
  local install hint
  if cbm_is_macos; then
    install='brew install python3'
    hint='macOS also ships one with the Xcode Command Line Tools:
      xcode-select --install'
  else
    install='sudo apt install python3      # or: dnf install python3, pacman -S python'
    hint='Any python3 on PATH works — no pip packages are needed.'
  fi
  if [ "$1" = "oneline" ]; then
    printf 'ccusage-backpack-monitor: no python3 found (and no ccusage to fall back on). Install it with: %s' "$install"
    return
  fi
  cat <<MSG
ccusage-backpack-monitor

  The panel reads Claude Code's own transcripts, so all it needs is python3 —
  but none was found, and neither was ccusage as a fallback.

  Fix it:

      $install

  $hint

  Then start a fresh claude, or run  /ccusage-backpack-monitor:ccusage-monitor
MSG
}

# Ensure common bin dirs are on PATH. Needed because the terminal execs the
# watcher with a bare, non-login shell that lacks Homebrew/npm paths. Covers
# both macOS (Homebrew) and Linux (npm-global / local) install locations.
cbm_fix_path() {
  export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/opt/local/bin:$HOME/.bun/bin:$HOME/.deno/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/node_modules/.bin:/Applications/WezTerm.app/Contents/MacOS:$PATH"
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

# Every live pane that belongs to session $1, one per line as
# "<sid>\t<backend>\t<handle>\t<plugin-root>". Prunes state files whose pane is
# gone, so crashes and manually-closed panes heal themselves.
#
# "Belongs to" is deliberately wider than "keyed by this session id": panes are
# tracked per session id, so anything that changes which id we resolve to — a
# fixed resolver, a resumed session — orphans the old pane and the next open
# leaves the user with two. A pane following one of this session's *agents*
# counts as this session's too.
cbm_session_panes() {  # $1=sid
  local state f sid be h root tr t8
  state="$(cbm_state_dir)"
  t8="$(printf '%s' "$1" | cut -c1-8)"
  for f in "$state"/*.pane; do
    [ -e "$f" ] || continue
    sid="$(basename "$f" .pane)"
    be="$(cbm_state_backend "$f")"
    h="$(cbm_state_handle "$f")"
    root="$(cbm_state_root "$f")"
    # Checked before anything else, including the liveness prune: a pane owned
    # by another running session is not ours to close, to reopen, or to forget
    # the state file of. This is the backstop for a session id resolved wrongly
    # -- which has happened, and cost somebody else their monitor.
    if cbm_pane_is_foreign "$f"; then
      continue
    fi
    if [ -z "$h" ] || ! cbm_pane_alive "$h" "$be"; then
      rm -f "$f"                        # self-healing: drop dead entries
      continue
    fi
    if [ "$sid" = "$1" ]; then
      printf '%s\t%s\t%s\t%s\n' "$sid" "$be" "$h" "$root"
      continue
    fi
    tr="$(find -L "$HOME/.claude/projects" -name "$sid.jsonl" 2>/dev/null | head -1)"
    if [ -n "$tr" ] && grep -qEm1 "\"teamName\"[[:space:]]*:[[:space:]]*\"session-$t8\"" "$tr" 2>/dev/null; then
      printf '%s\t%s\t%s\t%s\n' "$sid" "$be" "$h" "$root"
    fi
  done
}

# Toggle the monitor pane for a session, which is what a user pressing the same
# command twice actually expects. Exit codes let the caller report precisely:
#   0 opened     3 closed     4 restarted (stale version, or duplicates cleaned)
#   1 could not open        5 belongs to another live session -> left alone
# Panes belonging to OTHER sessions are never touched — several Claude sessions
# commonly run side by side, each with its own monitor.
cbm_toggle_pane() {  # $1=sid  $2=trans
  local sid="$1" trans="$2"
  [ -z "$sid" ] && return 1

  local state cur panes count clean psid pbe ph proot
  state="$(cbm_state_dir)"
  cur="$(cbm_plugin_root)"

  # This session id resolved to somebody else's live Claude process. Their pane
  # is not ours to close, and a pane we opened for their session would report
  # their spend as ours. Both are worse than doing nothing.
  cbm_pane_is_foreign "$state/$sid.pane" && return 5

  panes="$(cbm_session_panes "$sid")"

  if [ -n "$panes" ]; then
    count=0
    clean=1
    while IFS="$(printf '\t')" read -r psid pbe ph proot; do
      [ -n "$ph" ] || continue
      count=$((count + 1))
      cbm_pane_close "$ph" "$pbe" 2>/dev/null
      rm -f "$state/$psid.pane"
      # Anything not on the current version, or keyed under a different id, means
      # the user wants a working monitor rather than no monitor.
      if [ "$proot" != "$cur" ] || [ "$psid" != "$sid" ]; then clean=0; fi
    done <<EOF
$panes
EOF
    if [ "$count" -eq 1 ] && [ "$clean" -eq 1 ]; then
      return 3                          # a single current pane -> close it
    fi
    cbm_open_pane "$sid" "$trans" || return 1
    return 4                            # stale and/or duplicates -> fresh one
  fi

  cbm_open_pane "$sid" "$trans" || return 1
  return 0
}

# ---------------------------------------------------------------------------
# Backend resolution + dispatch
# ---------------------------------------------------------------------------

# Detectors: return 0 when that terminal is the active one. These key off ENV
# VARS only — never `command -v <cli>` — because the env var is authoritative
# ($TMUX/$WEZTERM_PANE are set by a live tmux/WezTerm) AND because hooks and
# slash commands frequently run with a stripped PATH that lacks Homebrew, so a
# `command -v` probe would spuriously report "unsupported" for a terminal we're
# clearly inside. Whether the CLI is actually runnable is handled at open time
# (cbm_fix_path first, then a graceful return 1 if the split command fails).
# Vars are read with ${VAR:-} so the detectors are safe even under `set -u`.
cbm_detect_tmux()    { [ -n "${TMUX:-}" ]; }
cbm_detect_wezterm() { [ -n "${WEZTERM_PANE:-}" ] || [ "${TERM_PROGRAM:-}" = "WezTerm" ]; }
cbm_detect_iterm()   { [ "${TERM_PROGRAM:-}" = "iTerm.app" ] || [ -n "${ITERM_SESSION_ID:-}" ]; }

# Back-compat alias: older call sites / external scripts may still ask this.
cbm_is_iterm() { cbm_detect_iterm; }

# Resolve the active backend, in priority order. Detection is deterministic
# within a process and cheap (a couple of `command -v` + env checks), so the
# few callers that re-resolve via $(cbm_backend) simply re-run it; the hot path
# (cbm_open_pane) resolves once into a local and threads it through explicitly.
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
cbm_state_write() {  # $1=path $2=backend $3=handle [$4=plugin root] [$5=owner pid]
  printf '%s\n%s\n%s\n%s\n' "$2" "$3" "${4:-$(cbm_plugin_root)}" \
    "${5-$(cbm_owner_pid)}" > "$1"
}

# ---------------------------------------------------------------------------
# Which Claude session owns this process
#
# Slash commands run without $CLAUDE_SESSION_ID, and the fallback -- the newest
# transcript recorded against this cwd -- is a coin toss the moment two Claude
# sessions run in the same directory. It lost that toss and closed the OTHER
# session's monitor pane. Nothing this tool does is worth reaching into another
# session, so it does not guess any more.
#
# Claude Code keeps ~/.claude/sessions/<pid>.json for every live session, which
# makes the answer exact: walk our own ancestry to the claude process that
# spawned us and read its id. Absent (older Claude Code), callers fall back to
# the heuristics as before -- but with cbm_pane_is_foreign still standing over
# them, so a wrong guess can cost a pane of ours and never one of somebody
# else's.
# ---------------------------------------------------------------------------

# The session id owned by $1, or nothing if $1 is not a live Claude session.
# The file outlives the process it describes, so liveness is checked too: a pid
# whose number has since been recycled must never be mistaken for the session
# that used to hold it.
cbm_session_of_pid() {  # $1=pid -> session id | empty
  local pid="$1" f
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  f="$HOME/.claude/sessions/$pid.json"
  [ -f "$f" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  case "$(ps -o comm= -p "$pid" 2>/dev/null)" in
    *claude*) ;;
    *) return 1 ;;                      # recycled pid wearing a dead session's file
  esac
  cbm_json_field sessionId < "$f"
}

# The pid of the Claude Code process we are running under, walking up from here.
cbm_owner_pid() {
  local pid=$$ depth=0
  while [ "$depth" -lt 12 ]; do
    case "$pid" in ''|0|1|*[!0-9]*) return 1 ;; esac
    if cbm_session_of_pid "$pid" >/dev/null 2>&1; then printf '%s' "$pid"; return 0; fi
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    depth=$((depth + 1))
  done
  return 1
}

# The session id of the Claude Code process we are running under.
cbm_owner_sid() {
  local pid
  pid="$(cbm_owner_pid)" || return 1
  cbm_session_of_pid "$pid"
}

# True when this pane belongs to a DIFFERENT session that is still running.
# Such a pane is untouchable: somebody is watching it, and it is not us. A pane
# whose owner has exited is an orphan and may be cleaned up, and a pane written
# before owners were recorded is unknowable -- neither is anyone else's to lose.
cbm_pane_is_foreign() {  # $1=state file
  local owner mine
  owner="$(cbm_state_owner "$1")"
  [ -n "$owner" ] || return 1
  cbm_session_of_pid "$owner" >/dev/null 2>&1 || return 1
  mine="$(cbm_owner_pid 2>/dev/null)"
  [ -n "$mine" ] && [ "$mine" = "$owner" ] && return 1
  return 0
}

# Absolute path of the plugin directory that owns this common.sh. Under the
# marketplace cache this path contains the version, so comparing it to the
# recorded one tells us whether a running pane predates an upgrade.
cbm_plugin_root() {
  (cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)
}

# Version string for user-facing messages. Read from plugin.json without needing
# python or jq, so it works in the same stripped environments the hooks run in.
cbm_plugin_version() {
  sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$(cbm_plugin_root)/.claude-plugin/plugin.json" 2>/dev/null | head -1
}

# Third line of the state file: the plugin root the pane was launched from.
# Empty for state files written before v0.9.0, which we treat as "unknown" and
# therefore stale, so the first toggle after upgrading refreshes the pane.
cbm_state_root() {  # $1=path -> plugin root or empty
  sed -n 3p "$1" 2>/dev/null
}
cbm_state_owner() {  # $1=path -> pid of the Claude session that opened it
  sed -n 4p "$1" 2>/dev/null
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
#
# $3 is 'auto' when the SessionStart hook opened it and 'manual' (the default,
# used by the toggle command) when the user asked for it by name. Only an auto
# pane is allowed to close itself on discovering it follows an agent.
cbm_open_pane() {  # $1=sid  $2=trans  $3=auto|manual
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
  # Never a live session's, however old the file: forgetting a pane somebody is
  # watching leaves them a monitor they can no longer toggle off.
  find "$state" -name '*.pane' -mtime +1 2>/dev/null | while IFS= read -r old; do
    cbm_pane_is_foreign "$old" || rm -f "$old"
  done

  # Idempotency: if a live pane already exists for this session, don't reopen.
  # Check the RECORDED backend's handle, not the ambient one.
  local prev="$state/$sid.pane" pb ph
  if [ -f "$prev" ]; then
    pb="$(cbm_state_backend "$prev")"
    ph="$(cbm_state_handle "$prev")"
    if [ -n "$ph" ] && cbm_pane_alive "$ph" "$pb"; then return 2; fi
  fi

  local poll="${CBM_POLL:-3}" split="${CBM_SPLIT:-vertically}" size="${CBM_SIZE:-25}"
  case "$poll" in ''|*[!0-9]*) poll=3 ;; esac
  [ "$poll" -lt 1 ] && poll=1
  case "$split" in vertically|horizontally) ;; *) split=vertically ;; esac
  # Share of the terminal the monitor pane takes; the main Claude session keeps
  # the rest. Default 25% -> a 3:1 split favoring Claude (three columns to it, one
  # to the pane). Clamp to a sane band so a typo can't produce a 0%/sliver pane or
  # one that swallows the screen.
  case "$size" in ''|*[!0-9]*) size=25 ;; esac
  [ "$size" -lt 5 ]  && size=5
  [ "$size" -gt 90 ] && size=90

  # Build the watcher invocation as a self-contained shell program: each arg is
  # single-quoted (cbm_shq) so a hostile sid/transcript can't break out. Each
  # backend then runs this string through exactly ONE more parse layer (sh -c,
  # or iTerm's shell) — never re-interpreting the user data in its own language.
  # The absolute watcher path is baked in so the new pane finds it regardless
  # of cwd/OS.
  # `mode` reaches the watcher so it knows whether it was opened automatically by
  # the SessionStart hook or deliberately by the user. Only an auto-opened pane
  # may retire itself on discovering it is following an agent.
  local cmd handle mode="${3:-manual}"
  cmd="$watcher $(cbm_shq "$sid") $(cbm_shq "$trans") $(cbm_shq "$poll") $(cbm_shq "$mode")"
  handle="$("cbm_open_$backend" "$cmd" "$split" "$size")" || return 1
  [ -z "$handle" ] && return 1
  # If we can't record the pane, close it again rather than leaking an orphan
  # that SessionEnd (which keys off the state file) could never find.
  if ! cbm_state_write "$prev" "$backend" "$handle"; then   # overwrites any stale file
    "cbm_close_$backend" "$handle" 2>/dev/null
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Backend: tmux  (Linux + macOS). Handle = a tmux pane id like %3.
# ---------------------------------------------------------------------------
# Split map preserves the iTerm semantics (vertically = side-by-side): tmux's -h
# splits left/right, -v splits top/bottom (the classic tmux naming inversion).
cbm_open_tmux() {  # $1=cmd  $2=split  $3=size%  -> prints %N
  local cmd="$1" dir=-h pct="${3:-25}" id
  [ "$2" = horizontally ] && dir=-v
  # Build argv so -t <pane> is only added when $TMUX_PANE is set. Everything
  # after `--` is forwarded verbatim to exec as [sh, -c, <cmd>]; tmux does NO
  # word-splitting there, so sh -c is the sole parser of cmd's inner quoting.
  set -- split-window "$dir" -d -P -F '#{pane_id}'
  [ -n "$TMUX_PANE" ] && set -- "$@" -t "$TMUX_PANE"
  # Size the new pane to <pct>% of the split axis (width for -h, height for -v).
  # `-l N%` needs tmux >= 3.1; if that tmux rejects it the split creates no pane,
  # so fall back to an even (default) split rather than leaving no monitor at all.
  if id="$(tmux "$@" -l "${pct}%" -- /bin/sh -c "$cmd" 2>/dev/null)" && [ -n "$id" ]; then
    printf '%s\n' "$id"; return 0
  fi
  tmux "$@" -- /bin/sh -c "$cmd" 2>/dev/null
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
cbm_open_wezterm() {  # $1=cmd  $2=split  $3=size%  -> prints <int>
  # CBM_WEZTERM_PERCENT stays as an explicit per-backend override; unset, it
  # inherits the unified size ($3, default 25).
  local cmd="$1" dir=--right pct="${CBM_WEZTERM_PERCENT:-${3:-25}}"
  [ "$2" = horizontally ] && dir=--bottom
  case "$pct" in ''|*[!0-9]*) pct=25 ;; esac
  # Only pass --pane-id when WEZTERM_PANE is set (never emit --pane-id ''). The
  # post-`--` argv goes straight to exec — same injection-safe model as tmux.
  set -- cli split-pane "$dir" --percent "$pct"
  [ -n "$WEZTERM_PANE" ] && set -- "$@" --pane-id "$WEZTERM_PANE"
  set -- "$@" -- /bin/sh -c "$cmd"
  wezterm "$@" 2>/dev/null
}
cbm_alive_wezterm() {  # $1=handle
  [ -n "$1" ] || return 1
  case "$1" in ''|*[!0-9]*) return 1 ;; esac   # handle must be a bare integer
  # (so a corrupt state file can't inject regex metacharacters below).
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
# Backend: iTerm2  (macOS). Handle = an iTerm session id. The alive/close
# AppleScript is unchanged from the original single-terminal implementation; only
# open now resizes the new pane (see cbm_open_iterm) to honor CBM_SIZE.
# ---------------------------------------------------------------------------
cbm_open_iterm() {  # $1=cmd  $2=split  $3=size%  -> prints iTerm session id
  # Single-quote each arg for the shell, then escape the whole string for the
  # AppleScript double-quoted literal (\ and ").
  local osa_cmd pct="${3:-25}" dim=columns
  # A vertical (side-by-side) split shares WIDTH -> size the new pane by columns;
  # a horizontal (stacked) split shares HEIGHT -> size it by rows.
  [ "$2" = horizontally ] && dim=rows
  osa_cmd="$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  # iTerm's `split` has no size option — it always halves the pane. So record the
  # pre-split dimension, split, then shrink the new pane to <pct>% of it. The
  # resize is wrapped in `try`: if iTerm won't honor it we keep the 50/50 pane
  # rather than failing the whole open.
  /usr/bin/osascript 2>/dev/null <<OSA
tell application "iTerm2"
  tell current session of current window
    set parentDim to $dim
    set newSession to (split $2 with same profile command "$osa_cmd")
  end tell
  try
    set $dim of newSession to (parentDim * $pct) div 100
  end try
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

# Is this session an *agent* rather than a person's session?
#
# Teammates are full Claude Code sessions: their own session id, their own
# transcript at the top level of a project directory, their own SessionStart.
# So without this check the monitor opens a second pane for every teammate a
# session spawns, which is the bug this exists to prevent. Task subagents are
# easier — they live under a `subagents/` directory.
#
# Only the first few lines are read. An agent transcript announces itself
# immediately: line 1 is `agent-setting`, and `teamName` has appeared by line 4.
# Reading further would risk matching those key names inside ordinary
# conversation text and suppressing a legitimate monitor.
#
# Returns 0 (an agent) ONLY when it can actually tell. A brand-new human session
# has an empty transcript at SessionStart too, so "can't tell yet" must mean
# "not an agent" — the opposite default would stop the pane ever opening.
cbm_is_agent_session() {  # $1=transcript path
  local t="$1"
  case "$t" in
    */subagents/*) return 0 ;;
  esac
  [ -n "$t" ] && [ -f "$t" ] || return 1
  head -n 10 "$t" 2>/dev/null | grep -q '"teamName"\|"agentSetting"'
}

# Close the pane recorded for $1 and forget it. Shared by the SessionEnd hook and
# by a watcher that has worked out it should never have been opened.
cbm_close_own_pane() {  # $1=sid
  local f
  f="$(cbm_state_dir)/$1.pane"
  [ -f "$f" ] || return 0
  cbm_close_recorded_pane "$f"
}

# Close the pane a state file describes, and only then forget it.
#
# The order is the whole point. Removing the state file first -- which this did
# -- means any failure to close leaves a pane on screen that NOTHING can ever
# find again: no state file, so no toggle, no sweep and no hook can reach it,
# and the user is left closing it by hand. Closing first, verifying, and
# unlinking only once the pane is really gone makes a failed close a retry
# instead of an orphan.
cbm_close_recorded_pane() {  # $1=state file -> 0 when the pane is gone
  local f="$1" backend handle
  [ -f "$f" ] || return 0
  backend="$(cbm_state_backend "$f")"
  handle="$(cbm_state_handle "$f")"
  if [ -z "$handle" ]; then
    rm -f "$f"                          # nothing to close; the record is junk
    return 0
  fi
  cbm_pane_close "$handle" "$backend"
  # One retry: at session teardown the terminal can be mid-quit and refuse the
  # first attempt, which is precisely when nobody is left to try again.
  if cbm_pane_alive "$handle" "$backend"; then
    cbm_pane_close "$handle" "$backend"
  fi
  if cbm_pane_alive "$handle" "$backend"; then
    return 1                            # still there: keep the record for later
  fi
  rm -f "$f"
  return 0
}

# Is Claude Code still running the session $1?
#   0 = live   1 = ended   2 = unknown (no registry to consult)
#
# "Unknown" is a distinct answer on purpose: an older Claude Code keeps no
# ~/.claude/sessions registry, and reading "no entry" as "ended" there would
# retire every pane on the machine. Only an entry's ABSENCE FROM A REGISTRY
# THAT IS OTHERWISE IN USE means the session is over.
cbm_session_is_live() {  # $1=sid
  local want="$1" dir found pid
  dir="$HOME/.claude/sessions"
  [ -d "$dir" ] || return 2
  ls "$dir"/*.json >/dev/null 2>&1 || return 2
  found="$(grep -lE "\"sessionId\"[[:space:]]*:[[:space:]]*\"$want\"" \
           "$dir"/*.json 2>/dev/null | head -1)"
  [ -n "$found" ] || return 1
  pid="$(basename "$found" .json)"
  cbm_session_of_pid "$pid" >/dev/null 2>&1 && return 0
  return 1
}

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
# Drop the pane into an interactive login shell (-il), matching the historical
# behavior for bash/zsh/fish/ksh. Only POSIX sh/dash get -i, since they reject
# -l and would otherwise kill the pane.
cbm_exec_shell() {
  local sh
  sh="$(cbm_login_shell)"
  case "$(basename "$sh")" in
    sh|dash) exec "$sh" -i  ;;
    *)       exec "$sh" -il ;;
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
