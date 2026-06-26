#!/usr/bin/env bash
# On-demand opener for the /ccusage-monitor slash command. Unlike the
# SessionStart hook, this can run mid-session. It figures out the current
# session itself, so it doesn't rely on any env var being present.

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"

if ! cbm_is_supported; then
  echo "ccusage-backpack-monitor: no supported terminal (tmux/WezTerm/iTerm2) detected — nothing to open."
  exit 0
fi

# Identify the current session + transcript:
#  1) $CLAUDE_SESSION_ID if Claude Code exported it,
#  2) else the most-recently-active transcript in this project's dir,
#  3) else the newest transcript anywhere.
sid="${CLAUDE_SESSION_ID:-}"
trans=""
if [ -n "$sid" ]; then
  trans="$(find "$HOME/.claude/projects" -name "$sid.jsonl" 2>/dev/null | head -1)"
else
  proj="$HOME/.claude/projects/$(printf '%s' "$PWD" | sed 's#[/.]#-#g')"
  newest="$(ls -t "$proj"/*.jsonl 2>/dev/null | head -1)"
  [ -z "$newest" ] && newest="$(ls -t "$HOME"/.claude/projects/*/*.jsonl 2>/dev/null | head -1)"
  trans="$newest"
  [ -n "$newest" ] && sid="$(basename "$newest" .jsonl)"
fi

if [ -z "$sid" ]; then
  echo "ccusage-backpack-monitor: couldn't determine the current session."
  exit 0
fi

# cbm_open_pane is idempotent and reports the outcome via its exit code, so we
# need no separate pane-alive queries here.
cbm_open_pane "$sid" "$trans"
case $? in
  0) echo "✅ Opened ccusage monitor for session ${sid:0:8}." ;;
  2) echo "ccusage monitor already open for session ${sid:0:8}." ;;
  *) echo "Could not open the pane (backend: $(cbm_backend)). On iTerm2, check Automation permission." ;;
esac
exit 0
