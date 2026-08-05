#!/usr/bin/env bash
# On-demand opener for the /ccusage-monitor slash command. Unlike the
# SessionStart hook, this can run mid-session. It figures out the current
# session itself, so it doesn't rely on any env var being present.

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
cbm_fix_path   # slash commands run with a stripped PATH; make tmux/wezterm/ccusage findable

if ! cbm_is_supported; then
  echo "ccusage-backpack-monitor: this terminal can't be scripted to open a side pane"
  echo "  (supported: tmux, WezTerm, iTerm2). Terminal.app / VS Code / Ghostty have no"
  echo "  split-pane API, so there's nothing to open here."
  echo "  Tip: run Claude inside tmux — 'tmux', then 'claude' — and the monitor opens"
  echo "  as a tmux split in ANY terminal."
  exit 0
fi

# Identify the current session + transcript. Slash commands run without
# CLAUDE_SESSION_ID, so this has to be worked out — and working it out badly is
# worse than failing: a subagent transcript rendered as a session shows one
# agent's spend as the whole session's and looks entirely plausible, and a
# neighbouring session resolved as this one gets its monitor pane closed out
# from under it.
#
# Claude Code tracks its own live sessions by pid, so ask it: cbm_owner_sid
# walks up to the claude process that spawned this command and reads the id it
# registered. That is exact, and in particular it does not care that two
# sessions are running in the same directory, which is what defeated the
# newest-transcript-for-this-cwd fallback below.
sid="${CLAUDE_SESSION_ID:-}"
[ -z "$sid" ] && sid="$(cbm_owner_sid 2>/dev/null)"
trans=""
resolver="$here/resolve-session.py"
py="$(cbm_python)"
if [ -n "$py" ] && [ -f "$resolver" ]; then
  line="$("$py" "$resolver" "$PWD" "$sid" 2>/dev/null)"
  if [ -n "$line" ]; then
    sid="${line%%	*}"
    trans="${line#*	}"
  fi
fi

# Shell-only fallback for hosts without python3. Still avoids the worst case by
# skipping transcripts that live under a subagents/ directory.
if [ -z "$trans" ]; then
  if [ -n "$sid" ]; then
    trans="$(find "$HOME/.claude/projects" -name "$sid.jsonl" 2>/dev/null | head -1)"
  else
    proj="$HOME/.claude/projects/$(printf '%s' "$PWD" | sed 's#[/.]#-#g')"
    newest="$(ls -t "$proj"/*.jsonl 2>/dev/null | head -1)"
    [ -z "$newest" ] && newest="$(ls -t "$HOME"/.claude/projects/*/*.jsonl 2>/dev/null | head -1)"
    trans="$newest"
    [ -n "$newest" ] && sid="$(basename "$newest" .jsonl)"
  fi
fi

if [ -z "$sid" ]; then
  echo "ccusage-backpack-monitor: couldn't determine the current session."
  exit 0
fi

# Toggle: run it once to open, again to close. cbm_toggle_pane reports the
# outcome via its exit code, so no separate pane-alive queries are needed here.
cbm_toggle_pane "$sid" "$trans"
case $? in
  0) echo "✅ Opened ccusage monitor for session ${sid:0:8}." ;;
  3) echo "◻️  Closed ccusage monitor for session ${sid:0:8}. Run it again to reopen." ;;
  5) echo "⏭️  Left session ${sid:0:8}'s monitor alone — that session belongs to another running Claude process, not this one." ;;
  4) echo "🔄 Restarted ccusage monitor for session ${sid:0:8} on v$(cbm_plugin_version) (replaced a pane that was stale or duplicated)." ;;
  *) echo "Could not open the pane (backend: $(cbm_backend)). On iTerm2, check Automation permission." ;;
esac
exit 0
