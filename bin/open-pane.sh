#!/usr/bin/env bash
# SessionStart hook: open a side pane with a live ccusage readout scoped to THIS
# Claude Code session (tmux, WezTerm, or iTerm2), and remember it so we can close
# it on exit.
#
# Config (env vars, all optional):
#   CBM_POLL   seconds between cheap file-change checks (default 3)
#   CBM_SPLIT  "vertically" (side-by-side) or "horizontally" (default vertically)
#
# Notes:
#  - On iTerm2, the first run triggers a one-time macOS Automation prompt.
#  - Prints to stdout only when ccusage is missing, and then only a structured
#    SessionStart JSON object (additionalContext) -- never raw text.

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"

input="$(cat)"
cbm_is_supported || exit 0   # no-op unless tmux/WezTerm/iTerm2 is active
cbm_fix_path                 # so ccusage/runtime detection here matches the pane's

sid="$(printf '%s' "$input"   | cbm_json_field session_id)"
trans="$(printf '%s' "$input" | cbm_json_field transcript_path)"

# Open the pane. When ccusage can't run, the pane itself shows the fix and turns
# into a shell, so this is still useful.
cbm_open_pane "$sid" "$trans"

# Backstop: if ccusage genuinely can't run, also surface it *through Claude* so
# the failure isn't silent when the user isn't looking at the pane. Emitted ONLY
# when actually broken (normal sessions print nothing -> zero context cost), via
# SessionStart's additionalContext so the model can explain it on request.
if ! cbm_ccusage >/dev/null 2>&1; then
  note="$(cbm_no_ccusage_msg oneline) After installing, start a fresh claude or run /ccusage-backpack-monitor:ccusage-monitor."
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' "$(cbm_json_quote "$note")"
fi
exit 0
