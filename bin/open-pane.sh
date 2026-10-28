#!/usr/bin/env bash
# SessionStart hook: open a side pane with a live usage readout scoped to THIS
# Claude Code session (herdr, tmux, Orca, WezTerm, or iTerm2), and remember it so
# we can close it on exit.
#
# Config (env vars, all optional):
#   PANE_POLL   seconds between cheap file-change checks (default 3)
#   PANE_SPLIT  "vertically" (side-by-side) or "horizontally" (default vertically)
#   PANE_SIZE   percent of the terminal the pane takes, 5-90 (default 25) -- the
#              main Claude session keeps the rest (a 3:1 split by default).
#              Not honoured on Orca, whose split is always 50/50 (no size flag).
#
# Notes:
#  - On iTerm2, the first run triggers a one-time macOS Automation prompt.
#  - Prints to stdout only when ccusage is missing, and then only a structured
#    SessionStart JSON object (additionalContext) -- never raw text.

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"

input="$(cat)"
pane_fix_path                 # put Homebrew/npm (tmux, wezterm, ccusage) on PATH FIRST
pane_is_supported || exit 0   # no-op unless herdr/tmux/Orca/WezTerm/iTerm2 is active

sid="$(printf '%s' "$input"   | pane_json_field session_id)"
trans="$(printf '%s' "$input" | pane_json_field transcript_path)"

# Never open a pane for an agent. Teammates are full sessions — own session id,
# own transcript, own SessionStart — so without this every teammate a session
# spawns gets its own monitor pane, on top of the one the human already has.
# Their spend is already attributed to the parent session's panel, which is
# where the user actually wants to see it.
pane_is_agent_session "$trans" && exit 0

# Open the pane. When the panel can't run, the pane itself shows the fix and
# turns into a shell, so this is still useful.
pane_open "$sid" "$trans" auto

# Backstop: if the panel genuinely can't run, also surface it *through Claude* so
# the failure isn't silent when the user isn't looking at the pane. Emitted ONLY
# when actually broken (normal sessions print nothing -> zero context cost), via
# SessionStart's additionalContext so the model can explain it on request.
if ! pane_python >/dev/null 2>&1 && ! pane_ccusage >/dev/null 2>&1; then
  note="$(pane_no_source_msg oneline) After installing, start a fresh claude or run /pane:toggle."
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' "$(pane_json_quote "$note")"
fi
exit 0
