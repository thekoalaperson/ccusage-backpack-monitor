# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); this
project uses [Semantic Versioning](https://semver.org/).

## [0.6.0] - 2026-06-26

### Added
- **Multi-terminal support via a pluggable backend abstraction.** The plugin no
  longer hard-codes iTerm2. Each terminal is a "backend" implementing four small
  operations (detect / open / alive / close), and a cached `cbm_backend()`
  resolves the active one in priority order (**tmux → WezTerm → iTerm2**).
- **tmux backend** — opens the monitor in a `tmux split-window`. This unlocks
  **Linux** (and tmux-on-macOS), which were previously unsupported. tmux is
  detected first so it wins even when running inside iTerm2/WezTerm (a GUI split
  there would live outside the multiplexer's pane tree).
- **WezTerm backend** — opens via `wezterm cli split-pane`; cross-platform.
  New `CBM_WEZTERM_PERCENT` (default `40`) sets the split size.
- `CBM_BACKEND` env var to force/override the detected backend (handy for tests).

### Changed
- **Backend-tagged state file.** The per-session `.pane` file is now two lines
  (`backend` + `handle`) so the `SessionEnd` hook — a fresh process whose
  environment may differ from open time — closes via the *recorded* backend
  instead of re-detecting. Legacy single-line files are still read (as iTerm2).
- `plugin.json`/marketplace description and keywords updated for tmux/WezTerm/Linux.

### Fixed
- **Portability for non-macOS hosts** (required for tmux/WezTerm on Linux):
  - The change-signature `stat` now tries GNU `stat -c` first, then BSD `stat -f`.
    (BSD `-f` on Linux means *filesystem info* and prints a churning table, which
    would have re-rendered the panel on nearly every poll.)
  - `python3` is resolved via `PATH` instead of the hard-coded `/usr/bin/python3`,
    so the rich panel works where Python lives elsewhere.
  - The Ctrl-C / fallback shell uses `-il` only for bash/zsh and `-i` for
    `/bin/sh` (dash rejects `-l`), with a sane default when `$SHELL` is unset.
- The "install ccusage" guidance is now OS-aware (`npm i -g ccusage` on Linux,
  `brew install ccusage` on macOS), and `PATH` augmentation includes Linux
  npm/local bin dirs.
- iTerm2 behavior is unchanged — its AppleScript moved verbatim into the new
  backend functions, and detection is identical on macOS + iTerm2.

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

[0.6.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.6.0
[0.5.2]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.5.2
[0.5.1]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.5.1
[0.5.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.5.0
[0.4.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.4.0
[0.3.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.3.0
[0.2.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.2.0
[0.1.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.1.0
