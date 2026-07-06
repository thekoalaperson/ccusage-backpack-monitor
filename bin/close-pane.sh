#!/usr/bin/env bash
# SessionEnd hook: close the pane that open-pane.sh created for this session,
# using the backend + handle stashed at open time.
#
# We do NOT gate on the ambient terminal here: at SessionEnd the environment may
# no longer match open time (e.g. a tmux server detached, or the hook shell lacks
# $TERM_PROGRAM). We trust the RECORDED backend in the state file instead, and
# cbm_pane_close no-ops safely if that backend's CLI is gone or the tag is bad.

here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"

input="$(cat)"

sid="$(printf '%s' "$input" | cbm_json_field session_id)"
[ -z "$sid" ] && exit 0

state="$(cbm_state_dir)"
f="$state/$sid.pane"
[ -f "$f" ] || exit 0

backend="$(cbm_state_backend "$f")"   # 'iterm' for legacy single-line files
handle="$(cbm_state_handle "$f")"
rm -f "$f"
[ -z "$handle" ] && exit 0

cbm_pane_close "$handle" "$backend"
exit 0
