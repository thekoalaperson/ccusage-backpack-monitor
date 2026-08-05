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

# Close first, unlink only once the pane is actually gone. A SessionEnd hook is
# not a guarantee — it runs while the session is tearing down and can be cut
# short — and forgetting the pane before closing it turns any such failure into
# a pane nothing can ever find again. Keeping the record means the next toggle,
# the next session's sweep, or the watcher itself can still finish the job.
cbm_close_recorded_pane "$f"
exit 0
