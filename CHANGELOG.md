# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); this
project uses [Semantic Versioning](https://semver.org/).

## [0.5.2] - 2026-06-23

### Fixed
- When `ccusage` is not installed **and** no JS runtime (`node`/`npx`/`bun`/`deno`)
  exists to run it, the pane no longer hangs on "waiting for … data…" or dies
  with a cryptic `npx: command not found`. `cbm_ccusage()` now returns empty
  instead of an unrunnable command, and the watcher detects this, prints the
  exact fix (`brew install ccusage`), and drops into a shell so you can run it
  in place.

### Added
- Single source of truth for the "install ccusage" guidance (`cbm_no_ccusage_msg`),
  shared by the pane and the hook so the message can't drift.
- The `SessionStart` hook now also surfaces the missing-ccusage notice *through
  Claude* (via `additionalContext`) so the failure isn't silent if you're not
  looking at the pane. Emitted **only** when ccusage genuinely can't run, so
  normal sessions add zero context.
- Recognize `bunx` and `deno` as additional ways to run ccusage.

## [0.5.1] - 2026-06-23

### Fixed
- Panes opened by the `/ccusage-monitor` command were not closed on session
  exit. The state dir was derived from `$CLAUDE_PLUGIN_DATA`, which Claude Code
  sets in hook context but not in slash-command (`!`-bash) context, so the open
  and close sides used different directories. The state dir is now a fixed,
  context-independent path (`~/.local/state/ccusage-backpack-monitor`).

## [0.5.0] - 2026-06-23

### Added
- `/ccusage-backpack-monitor:ccusage-monitor` command to open the monitor pane
  for the current session on demand (the hook only fires at session start).
- `bin/open-now.sh`, which identifies the current session itself
  (`$CLAUDE_SESSION_ID`, else the newest transcript in the cwd, else globally).

### Changed
- Pane-open logic lifted into a shared `cbm_open_pane()` in `lib/common.sh`,
  used by both the SessionStart hook and the command. It is idempotent and
  reports its outcome via exit code (0 opened / 2 already open / 1 not opened),
  so callers need no extra liveness queries.

## [0.4.0] - 2026-06-23

### Changed
- Hooks now parse their JSON input in pure shell, so the plugin works without
  `python3` (the rich panel still degrades to plain text).
- The pane now also opens on session **resume** (`SessionStart` matcher
  `startup|resume`), with an idempotency guard against duplicate panes.

### Fixed
- Safely quote arguments into the AppleScript command (spaced/quoted transcript
  paths, latent injection).
- Validate `CBM_POLL`/`CBM_SPLIT`; strip model date suffixes via regex; write the
  burn-rate cache atomically.

## [0.3.0] - 2026-06-23

### Added
- Full per-model breakdown in the panel — every model used in the session with
  its own cost, a cost-share bar, and token count (no more `+N`).
- `CBM_BG` option: an opaque background card for transparent terminals.

### Changed
- Dropped the ANSI dim attribute for solid high-contrast colors so the panel is
  legible on transparent backgrounds.

## [0.2.0] - 2026-06-23

### Added
- Rich colored panel: cost + token breakdown, 5h-block burn rate and projection,
  and a Unicode sparkline of output-tokens-per-turn.

### Changed
- Kept it idle-cheap: the burn-rate call is cached with a TTL and the sparkline
  reads only the tail of the transcript; rendering still fires only on change.

## [0.1.0] - 2026-06-23

### Added
- Initial release: a live iTerm2 side pane showing `ccusage` for the current
  session, opened on `SessionStart` and closed on `SessionEnd`.
- Change-driven watcher — idle cost is a single `stat`; `ccusage` runs only when
  the transcript changes. No-op on non-iTerm terminals.

[0.5.2]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.5.2
[0.5.1]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.5.1
[0.5.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.5.0
[0.4.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.4.0
[0.3.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.3.0
[0.2.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.2.0
[0.1.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.1.0
