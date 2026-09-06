# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); this
project uses [Semantic Versioning](https://semver.org/).

## [0.12.0] - 2026-09-06

### Added
- **[herdr](https://herdr.dev) is a supported terminal.** herdr is a
  tmux-like multiplexer for coding agents, and it runs *inside* a GUI terminal —
  which the plugin had no way to know. Detection fell straight through to
  iTerm2, so the monitor was opened by splitting the **iTerm2 window** itself.

  That pane was never part of herdr's tab tree. It sat over the whole window,
  stayed on screen when the user switched herdr tabs or workspaces, and went on
  reporting the session they had just navigated away from — a cost readout
  physically disconnected from the session it belonged to, and no way to move
  it.

  There is now a `herdr` backend, and it is detected **first**: `$HERDR_PANE_ID`
  is injected per pane, so it is authoritative, and whatever owns the layout of
  the pane Claude runs in is what may split it. The monitor opens as a herdr
  pane beside the session, moves and closes with its tab, and is labelled
  `ccusage <session>` in the sidebar so it is identifiable at a glance.
  `CBM_SIZE` is honoured (the split ratio is converted to the share herdr
  keeps), and `CBM_SPLIT=horizontally` stacks it below.

  `$TMUX` outranks herdr when both are set. herdr's variables are ordinary
  environment variables, so a tmux server first started from a herdr pane hands
  them to every window opened from it afterwards, even ones attached from a
  plain terminal — and splitting on that stale pane id would drop the monitor
  into an unrelated pane. When both are set, the tmux pane is where Claude
  actually is. herdr running *inside* tmux is the nesting this gets wrong;
  `CBM_BACKEND=herdr` forces it.

  Detection order is now **herdr → tmux → WezTerm → iTerm2**.

- Pane state files record `herdr` as a backend. Without that the tag would have
  been read as a pre-0.6 single-line iTerm record, and the SessionEnd hook would
  have gone looking for an iTerm session called `herdr`.

## [0.11.0] - 2026-08-05

### Fixed
- **A pane could outlive the session it was watching.** Closing the pane is the
  `SessionEnd` hook's job, but a hook is not a guarantee — it runs while the
  session is tearing down and can be cut short. When that happened the pane
  stayed on screen after the session ended, and worse: the hook unlinked the
  pane's state file *before* closing it, so a failed close left a pane that
  nothing could ever find again. No toggle, no sweep and no later hook can
  reach a pane with no record, and the only way out was closing it by hand.

  The state file now outlives a failed close — the pane is closed, verified,
  retried once, and only forgotten once it is really gone — so a failure
  becomes a retry rather than an orphan.

  And the pane no longer depends on the hook at all. It is the one thing still
  running when a session ends, so it checks: if Claude Code's live-session
  registry no longer lists the session it follows, it sees itself out. Three
  consecutive misses are required, and a machine with no registry (older Claude
  Code) yields *unknown* rather than *ended*, so a pane is never retired on an
  absence of evidence. Costs one `grep` a minute.

- **`/ccusage-monitor` could close another session's pane.** The slash command
  runs without `$CLAUDE_SESSION_ID`, so it worked out which session it belonged
  to by taking the most recently written transcript recorded against `$PWD`.
  With two Claude sessions running in the same directory that is a coin toss,
  and losing it meant resolving to the *neighbouring* session — then toggling
  its monitor pane shut, in a window the user was not even looking at.

  The session id is no longer inferred. Claude Code registers every live session
  as `~/.claude/sessions/<pid>.json`, so the command walks up its own process
  ancestry to the `claude` that spawned it and reads the id straight out. Exact,
  and indifferent to how many sessions share a directory.

  Because a wrong answer here is so costly, the guarantee no longer depends on
  getting it right: pane state files now record the pid of the session that
  opened them, and **a pane owned by a different live Claude session is never
  closed, never reopened, never swept by the stale-state prune, and never has
  its state file deleted** — whatever session id was resolved. Where that would
  once have closed a pane, the command now leaves it alone and says so. Panes
  whose owning session has exited are still reclaimed, so nothing is stranded.
- **The ghost panel in scrollback.** Scrolling up in the monitor pane found the
  panel again, showing older numbers. A terminal banks a line into scrollback
  when that line leaves the screen — on a scroll, or when an application hands
  over a whole frame to erase — and a panel that redraws on every change was
  doing the latter every few seconds. macOS `clear(1)` makes it exact: it emits
  `ESC[3J ESC[H ESC[2J`, dropping the scrollback and *then* banking the frame it
  just erased, which is why precisely one ghost survived each redraw.

  The panel therefore **erases nothing**. Each row is addressed absolutely,
  overwritten, and erased only to its own end; rows below the panel are blanked
  the same way, autowrap is off for the duration, and no newline is emitted at
  all. The cursor is never advanced off a row, so the screen cannot scroll and
  no line can leave it — on any terminal, in either screen buffer.

  Two things had to be true for this to keep holding, and neither was:

  - `$LINES`/`$COLUMNS` are now ignored outright. A pane inherits them from the
    window it was split out of, so they describe a *larger* screen than the pane
    owns — and everything downstream believes them, `tput` and Python's
    `shutil.get_terminal_size()` included. Painting a pane to its window's
    height is a scroll. The size comes from the tty itself now, and resizes
    with it.
  - The watcher **re-execs itself when its own file changes**. `bash` reads a
    script from an open descriptor by offset as it runs, so a pane open for days
    keeps executing the build it started with no matter what is installed. Two
    earlier attempts at this bug looked like failures for exactly that reason.
    The successor is syntax-checked before being `exec`'d, so an update caught
    mid-write can never close the pane.

  The panel also runs on the **alternate screen buffer** — the one `vim`, `less`
  and `htop` use — so the pane's previous contents come back when it exits. That
  is now presentation, not the fix: iTerm2 saves alternate-screen lines to
  scrollback by default, so an erase there banked a copy per redraw with nothing
  to drop the pile. Every exit path hands the screen back, including `q` and
  `Ctrl-C`, which `exec` a shell rather than returning. `CBM_ALTSCREEN=0` opts
  out of the buffer; that path clears scrollback *after* erasing.

### Changed
- **Every number now says what it is.** The panel had accumulated bare figures
  whose meaning you had to remember: a headline dollar amount with no stated
  scope, `~$121` beside `ends 15:30`, and a weekly limit rendered `Fable Wed
  10:30` — a scope name butted against a clock, with nothing marking it as a
  reset. Reset times are sentences now (`Fable · resets Wed 10:30 · 21h left`),
  the headline reads `session $25.36`, and the burn block is four labelled rows
  (`rate` / `so far` / `on track` / `window`).

  Phrasings are *reworded* to fit rather than truncated: a heading clipped
  mid-word ("5h rolling block · all sessi") is worse than no note at all, and
  below ~40 columns the meter gives up cells so the sentence beside it survives.

- **Tabs advertise their own shortcut.** The strip is numbered (`1 live
  2 limits …`), so the `1`–`6` jump keys are discoverable instead of being
  documented only under `?`. The footer names the keys it expects.

- **Each tab reworked around aligned key/value rows and comparable bars.**
  `models` lays its token counts out as labelled cells sized to their contents
  rather than to half the pane; `agents` gives every agent a share bar and ends
  with the you-vs-agents split; `trend` draws a real bar per day with `today`
  and `yest` named, and separates the lifetime totals under their own `ALL TIME`
  heading — they sat under `7 DAYS` reading as the week's; `limits` states in
  plain words that the percentages are account-wide and that they, not the
  dollars, are what stop you; `account` labels every field.

- `?` explains the `~` marker, which appears on any figure the panel will not
  vouch for as current or exact.

## [0.10.0] - 2026-08-04

### Added
- **Rate-limit meters — the numbers that actually stop you.** On a subscription
  the dollar figures are notional; what blocks you mid-task is the percentage of
  your limit. The panel now shows the **5-hour** and **weekly** windows as
  meters beside the context gauge, each with its real reset time. The weekly cap
  is invisible to anything that only reads transcripts, and it is usually the
  one that bites.

  These are read from Claude Code's own cache of the server's limit state
  (`~/.claude.json`), **read-only and never fetched** — polling Anthropic from a
  status pane would break the idle-cost promise. The age of the cached figure is
  reported instead, and a percentage is marked `~` once it is old enough to have
  moved. Every field is read defensively: these are private client internals, so
  a future rename makes a row disappear rather than breaking the panel.
- **Tabs, navigable with the arrow keys.** The header (spend + all three meters)
  is pinned to every tab, so the numbers worth watching never need a keystroke.
  Six views: `live`, `limits`, `models`, `agents`, `trend`, `account`. Also
  `Tab`, `1`–`6` to jump, `r` to redraw, `?` for the key list, `q` for a shell.

  This costs nothing when idle: the keypress read **replaces** the `sleep` at the
  end of the watch loop rather than adding to it, so the poll interval is
  unchanged. Verified at ~0s CPU over a 4-second idle window. With no terminal on
  stdin it falls back to `sleep` — without that guard `read` would fail instantly
  and turn the poll into a busy loop. `CBM_TABS=0` restores the static panel.
- **Plan and account identity.** Your plan (`Max 5x`, `Pro`, `Team`, …) and name
  appear in the footer. Identity defaults to a **first name only** — this pane
  ends up in screen-shares and recordings. `CBM_ACCOUNT=full` opts into email and
  organisation; `CBM_ACCOUNT=0` turns it off.
- **Detail the panel never had room for.** Cache hit rate (the single biggest
  lever on session cost) now rides on an existing line; the `models` tab adds
  per-model in/out, cache read/write, the 1h-vs-5m write split and $/turn; the
  `trend` tab surfaces the 7-day account history from `stats-cache.json`; the
  `account` tab shows the session's title, branch, effort and permission mode.
- Terminal resize (`SIGWINCH`) now redraws, instead of leaving the panel laid out
  for the old pane width.

### Changed
- Layout reordered live → reference: spend, then the three meters as one aligned
  block (ordered by how soon each one hurts: ctx fills in minutes, 5h in hours,
  the week in days), then models and agents, then a dimmed identity footer below
  a rule. Colour is now reserved for things that can go wrong.
- The local 5-hour dollar total is dropped from the burn line **when** the
  server's own 5h percentage is available, since the meter covers that window. It
  returns automatically when the cache is absent.

### Internal
- Arrow keys are parsed for both normal (`ESC [ C`) and **application cursor
  mode** (`ESC O C`); tmux and some terminals send the latter. macOS ships bash
  3.2, which rejects a fractional `read -t`, so the tail of an escape sequence is
  read with an integer timeout — those bytes arrive in the same burst, so it
  returns immediately.
- Session identity (title, branch, version, effort, permission mode) is scanned
  only from lines already parsed or from line types that appear a handful of
  times per session. Adding `gitBranch` to the prefilter would have parsed every
  line — the exact cost this scanner exists to avoid.
- The test suite now sandboxes `HOME`. Without it the panel tests read the
  developer's real account file, leaking their plan and email into test output
  and giving a different answer per machine. 31 new tests (61 → 92), including
  pty-driven coverage of the key loop and a guard that the watcher does not spin
  when stdin is not a terminal.
- Scan-cache version bumped to 5 (per-model cache-TTL split, session identity).

## [0.9.3] - 2026-08-04

### Fixed
- **A monitor pane opened for every teammate.** Teammates are not subagents —
  they are *full* Claude Code sessions, with their own session id, their own
  top-level transcript, and their own `SessionStart`. The hook opened a pane for
  whatever session id it was handed, so a session that spawned three teammates
  ended up with four monitor panes, three of them showing an agent's spend as if
  it were a session's. Their cost was already attributed to the parent session's
  panel, which is where it belongs.

  `SessionStart` now declines to open a pane for an agent transcript, detected
  from the `agent-setting` line Claude Code writes first and the `teamName` that
  follows within a few lines, or from a `subagents/` path.

  The check only reports "agent" when it can actually tell. A brand-new human
  session has an empty transcript at `SessionStart` too, so treating "cannot
  tell" as "agent" would have stopped the monitor opening for anyone — there is
  an explicit regression test for exactly that.

  Because an empty transcript is genuinely ambiguous at hook time, a pane opened
  automatically also re-checks once the file has content and closes itself if it
  turns out to be following an agent. Panes opened deliberately with
  `/ccusage-monitor` are never closed this way and keep their existing behaviour
  of naming the agent and its parent session.
## [0.9.2] - 2026-08-02

### Fixed
- **Opening the monitor could leave you with two panes.** Panes are tracked per
  session id, so anything that changes which id gets resolved — the resolver fix
  in 0.9.1, a resumed session — orphaned the existing pane: the "is one already
  open?" check looked under the *new* id, found nothing, and opened a second one.
  The old pane had to be closed by hand.

  The toggle now acts on every live pane **belonging to this session**, which
  includes a pane that was following one of the session's own agents. Panes
  belonging to *other* sessions are never touched — several Claude sessions
  commonly run side by side, each with its own monitor.
- **Stale pane state now heals itself.** State files whose pane is gone (closed
  by hand, crashed, rebooted) are pruned whenever the state directory is read,
  instead of lingering until the one-day sweep.
- **`find` follows a symlinked `~/.claude`.** Transcript lookups used `find`
  without `-L`, which does not descend a symlinked starting path — so a user who
  keeps `~/.claude` on another volume would silently find no transcripts.
- The whitespace between JSON keys and values is no longer assumed when matching
  `teamName`, so the check does not depend on how Claude Code serialises.

### Internal
- The toggle tests were mocking a backend named `mock`, which is not a
  recognised backend id, so `cbm_state_backend` / `cbm_state_handle` quietly fell
  back to legacy single-line parsing and returned the backend string as the pane
  handle. The tests passed without exercising the code they claimed to. They now
  mock a real backend id and parse state files for real, which is what surfaced
  the two bugs above.
- Test fixtures now write compact JSON, matching the real transcript format.

## [0.9.1] - 2026-08-02

### Fixed
- **The pane could follow a subagent instead of your session**, reporting one
  agent's spend as the whole session's. Observed live: a session that had cost
  **$941** across fable, opus, sonnet and haiku displayed as **$1.84, sonnet
  only** — the transcript of a single `pixverse-v6-research` agent.

  Slash commands run without `CLAUDE_SESSION_ID`, so the command had to guess,
  and its guess was "newest transcript in the directory derived from `$PWD`".
  When an agent runs with a different working directory it leaves its transcript
  at the top level of *that* project directory, where it is indistinguishable
  from a session by filename alone. A wrong number that looks entirely plausible
  is the worst kind, so session resolution is now explicit:

  - transcripts carrying `teamName`, or living under a `subagents/` directory,
    are agents and are **never** chosen as a session;
  - matching is done on the `cwd` recorded *inside* transcripts rather than on a
    path-to-directory-name transform, which is undocumented and would break on
    paths containing spaces or other unusual characters;
  - if only agents have run in a directory, the resolver follows one **home to
    its session** — deterministically, rather than falling back to whichever
    unrelated session happened to be written last;
  - failing that, it uses the session started in the nearest **ancestor**
    directory, since Claude records where a session began, not where you have
    since navigated.

  Agents spawn agents (a workflow coordinator is itself an agent), so the walk
  up the chain is recursive, cycle-guarded, and depth-capped. If the chain is
  broken — the parent transcript deleted — it reports no session rather than
  returning an agent. Verified across every working directory recorded on a real
  machine: **0 of 10 now resolve to an agent** (previously 1, plus 5 that landed
  on an unrelated session by mtime luck).
- **If an agent transcript is rendered anyway, the panel now says so**
  (`agent pixverse-v6-research of session d6ac3aa3`) instead of presenting it as
  the session.

### Added
- `bin/resolve-session.py`, with a shell fallback that still skips `subagents/`
  paths on hosts without python3.
- 17 more tests (58 total) covering session resolution: agents never chosen,
  stray agents followed home, agent-of-agent chains, broken chains, cycles,
  ancestor-directory matching, concurrent sessions, paths with spaces and
  unicode, `.`/`..`/trailing-slash normalisation, and a missing projects root.

## [0.9.0] - 2026-08-01

Follow-up to 0.8.0, driven by using it: the slash command didn't behave the way
anyone expects, and the panel broke at the pane size the default config produces.

### Added
- **`/ccusage-monitor` is now a toggle.** Run it once to open, again to close.
  Previously it only ever opened, and a second run printed "already open" and did
  nothing — which was also the *only* feedback you got when the open pane was
  running a **older** version of the plugin after an upgrade.
- **Version-aware restart.** The pane's state file now records the plugin root
  that launched it. If you toggle a pane that predates an upgrade, it is replaced
  rather than closed, and the message says so. State files written before this
  release have no version recorded and are treated as stale, so the first toggle
  after upgrading refreshes the pane.
- **Context-window gauge.** `ctx ██████···· 58%  577K/1.0M` from the last turn's
  input side (fresh + cached tokens). The percentage is shown **only** when the
  model's window is actually known — for an unrecognised model the raw token
  count is shown instead, rather than a percentage against a guessed window.
- **`NO_COLOR` support** (no-color.org) for plain terminals, piped output, and
  logs. `CBM_BG` still forces color, since it implies a styled card.
- `CBM_CONTEXT=0` hides the context gauge.

### Fixed
- **The panel no longer wraps in a narrow pane.** Width had a hard floor of 40
  columns, so anything narrower wrapped every model row — and the default
  `CBM_SIZE=25` produces a 30-column pane on a 120-column terminal, so this hit
  default configs, not edge cases. The layout now drops fields in order of
  importance and hard-clips as a last resort; verified overflow-free from 8 to
  120 columns with deliberately hostile content (five-figure costs, 50-character
  model and agent names).
- Large costs are formatted compactly in model and agent rows (`$21.9k`), so they
  can no longer be truncated mid-number.
- **The scan cache is bounded** (600 entries, least-recently-used evicted). It
  previously only pruned deleted files and had reached 571 entries / 343 KB.

## [0.8.0] - 2026-08-01

The panel now reads Claude Code's transcripts directly instead of shelling out
to `ccusage session`. That one change fixes three wrong numbers and makes the
pane roughly 200x cheaper to render.

### Fixed
- **Newer models no longer cost $0.00.** The panel passed `--offline`, which
  prices from a snapshot baked into whichever ccusage you installed. Anything
  released since then priced at zero — Opus 5 and Sonnet 5 showed `$0.00` next
  to millions of tokens. Pricing now resolves against a bundled table, refreshed
  daily from LiteLLM, with per-family estimates as a last resort so a model that
  ships tomorrow is never silently free. Estimated rates are marked `~`.
  (Upgrading ccusage alone did not fix this: 20.0.19 still zeroes Opus 5.)
- **Subagent spend is no longer invisible.** Agents write to their own
  transcripts under `<project>/<session-id>/subagents/`, which the old data
  source largely missed — on one measured session that hid $523 across 171
  agents, and two entire models (Opus 4.8 and Sonnet 5) never appeared. The
  panel now attributes them to the parent session and breaks them out.
- **Multi-block messages are no longer counted several times.** One assistant
  response is written to the transcript once per content block, each line
  carrying the same cumulative usage; a 13-block reply was billed 13x. Usage is
  now deduplicated per API response.
- **1-hour cache writes are priced correctly** at 2x input instead of 1.25x,
  which had been under-reporting cache-heavy sessions by roughly 15%.
- **The session total and the 5h figure now agree** — both come from the same
  pricing path, so the panel can no longer show `$0.00` for the session and
  `$143` for the window at the same moment.

### Changed
- Rendering costs ~0.07s instead of ~2s wall and ~16 CPU-seconds, because it
  reads this session's files rather than rescanning every transcript on the
  machine on every turn. Restores the plugin's own "idle-cheap" promise.
- **`ccusage` is no longer required.** `python3` is enough; ccusage remains a
  fallback when python3 is absent.
- The watcher now also notices subagent activity, so the pane stays live during
  a workflow fan-out when the main transcript is momentarily still.
- Panel additions: a `you / agents` cost split, an `AGENTS` section, and a
  distinct color for the Fable/Mythos family.

### Added
- `tests/test_usage.py` — 28 fixture-based regression tests covering pricing,
  deduplication, subagent attribution, block math, and panel rendering. They
  build their own transcripts in a temp directory and never touch real usage
  data or the network.
- Optional pricing overrides at
  `~/.config/ccusage-backpack-monitor/pricing.json`.
- `CBM_AGENTS=0` to hide the subagent section; `CBM_NO_NETWORK=1` to pin
  pricing to the bundled table.

## [0.7.0] - 2026-07-13

### Added
- **`CBM_SIZE` — the monitor pane now opens at a fraction of the screen instead
  of half of it.** New env var sets the percent of the terminal the pane takes
  (clamped to a sane 5–90); the main Claude session keeps the rest. It applies to
  **every** backend — tmux, WezTerm, and iTerm2 — so the split is consistent no
  matter which terminal you're in.

### Changed
- **Default split is now 3:1 in the Claude session's favor (pane = 25%).**
  Previously the pane grabbed half the screen (tmux/iTerm2) or 40% (WezTerm).
  - **tmux** now passes `-l <pct>%` to `split-window`. On tmux older than 3.1
    (which lacks `-l N%`) it falls back to a plain even split, so the monitor
    still opens rather than failing.
  - **iTerm2** has no size option on its AppleScript `split` (it always halves
    the pane), so it now records the pre-split size and shrinks the new pane to
    `<pct>%` afterward, wrapped in `try` — if iTerm won't honor the resize the
    50/50 pane is kept rather than failing the open.
- **`CBM_WEZTERM_PERCENT` now defaults to `CBM_SIZE`** (was a fixed `40`). Set it
  explicitly to keep a WezTerm-only override; unset, WezTerm follows `CBM_SIZE`.

## [0.6.1] - 2026-07-06

### Fixed
- **tmux and WezTerm were reported as "unsupported" and fell back to iTerm-only
  behavior.** Terminal detection required the CLI on `PATH` (`command -v tmux` /
  `command -v wezterm`), but the `SessionStart` hook and the `/ccusage-monitor`
  command run with a stripped `PATH` that usually lacks Homebrew — so the probe
  failed and the plugin decided no supported terminal was present. iTerm2, which
  is detected purely from env vars, kept working, which is why only iTerm worked.
  - Detection now keys off env vars only (`$TMUX`, `$WEZTERM_PANE`,
    `$TERM_PROGRAM`), which are authoritative for "which terminal am I in." CLI
    availability is handled at open time instead (PATH is fixed first, then a
    clean failure if the split command can't run).
  - `cbm_fix_path` now runs **before** the support check in `bin/open-pane.sh`,
    and is now called in `bin/open-now.sh` (it was missing entirely).
  - `cbm_fix_path` also covers MacPorts (`/opt/local/bin`) and the WezTerm.app
    bundled CLI (`/Applications/WezTerm.app/Contents/MacOS`).

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

[0.6.1]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.6.1
[0.6.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.6.0
[0.5.2]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.5.2
[0.5.1]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.5.1
[0.5.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.5.0
[0.4.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.4.0
[0.3.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.3.0
[0.2.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.2.0
[0.1.0]: https://github.com/thekoalaperson/ccusage-backpack-monitor/releases/tag/v0.1.0
