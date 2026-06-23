# ccusage-backpack-monitor

A Claude Code plugin that pops open an **iTerm2 side pane** showing a **live,
change-driven [ccusage](https://github.com/ryoppippi/ccusage) readout** for the
session you just started — and closes that pane automatically when the session
ends. Think of it as a usage "backpack" your session carries while it runs.

> **Status:** v0.4.0 — macOS + iTerm2 only. On any other terminal the hooks
> no-op silently, so it's safe to install anywhere.

## Why

`/usage` is a one-shot, machine-local snapshot. This gives you an always-on,
per-session readout in a pane beside your work — without polling on a timer.
It watches only *this* session's transcript file for changes and runs `ccusage`
only when something actually changed, so it's near-live yet idle-cheap.

## Requirements

- **macOS** with **iTerm2** (the pane is opened via iTerm2's AppleScript).
- [`ccusage`](https://github.com/ryoppippi/ccusage) — `brew install ccusage`
  (recommended), or it falls back to `npx -y ccusage@latest` if Node is present.
- **`python3`** powers the *rich* colored panel + sparkline (on macOS it's at
  `/usr/bin/python3`, which may prompt to install the Xcode Command Line Tools
  the first time). It is **not required** for the plugin to work: the hooks parse
  their input in pure shell, so the pane still opens without python3 and simply
  shows plain `ccusage session` text instead of the rich panel — no error.
- One-time: allow the macOS Automation prompt ("iTerm wants to control iTerm").

### Where it works (and where it no-ops)

| Scenario | Behaviour |
|---|---|
| macOS + iTerm2 + ccusage + python3 | Full rich panel (cost, burn rate, sparkline) |
| macOS + iTerm2, no python3        | Plain `ccusage` text fallback, still live    |
| Any non-iTerm terminal            | Hooks **no-op silently** (safe to leave installed) |
| Linux / Windows                   | No-op (no iTerm2 AppleScript); not yet supported |

No `watch`, `jq`, or charting libraries are required — the only hard runtime
dependencies are `ccusage` and (for the rich view) the stock `python3`.

## Install

Inside Claude Code:

```text
/plugin marketplace add thekoalaperson/ccusage-backpack-monitor
/plugin install ccusage-backpack-monitor@ccusage-backpack-monitor
```

Then start a fresh `claude` in iTerm2 — a side pane opens automatically.

<details>
<summary>Local development install</summary>

Point the marketplace at a local checkout instead of GitHub:

```text
/plugin marketplace add /path/to/ccusage-backpack-monitor
/plugin install ccusage-backpack-monitor@ccusage-backpack-monitor
```
</details>

## Configuration

Set these as environment variables before launching `claude`:

| Var              | Default       | Meaning                                              |
|------------------|---------------|------------------------------------------------------|
| `CBM_POLL`       | `3`           | Seconds between cheap file-change checks              |
| `CBM_SPLIT`      | `vertically`  | `vertically` (side-by-side) or `horizontally`        |
| `CBM_BLOCKS`     | `1`           | `0` hides the 5h burn-rate/projection section (skips that account-wide call entirely) |
| `CBM_BLOCKS_TTL` | `30`          | Seconds to cache the burn-rate call so frequent turns don't re-trigger the scan |
| `CBM_GRAPH`      | `1`           | `0` hides the sparkline                              |
| `CBM_BG`         | _(unset)_     | A 256-color index (e.g. `234`) paints an opaque background card behind the panel — useful in **transparent terminals**. Unset = solid high-contrast text, no fill. |

The panel shows **every model used in the session** with its own cost, a cost-share
bar, and token count (sessions that switched models show each one, not a `+N`).

## Performance

The watcher is **change-driven, not interval-driven**. When idle it only does a
cheap `stat` on this session's transcript every `CBM_POLL` seconds — no `ccusage`
process, no parsing. It renders only when the transcript actually changes, so
cost is proportional to real activity, not wall-clock time. The account-wide
burn-rate call is cached (`CBM_BLOCKS_TTL`), and the sparkline reads only the tail
of the transcript, so a render stays ~0.1s regardless of session length.

## How it works

| Phase  | File                | Hook           | Action                                          |
|--------|---------------------|----------------|-------------------------------------------------|
| open   | `bin/open-pane.sh`  | `SessionStart` | split pane, run watcher, stash the pane id      |
| render | `bin/watch.sh`      | —              | stat transcript; on change, draw the panel      |
| panel  | `lib/render.py`     | —              | rich colored cost/burn-rate/sparkline view      |
| close  | `bin/close-pane.sh` | `SessionEnd`   | look up the pane id, close that exact pane       |

State (one tiny `<session-id>.pane` file per session) lives in the plugin's
data dir and self-prunes after a day.

## Roadmap

- Support for more terminals (tmux, WezTerm, kitty, Ghostty)
- A companion skill/command for on-demand usage summaries
- Optional context-window % and per-model cost split in the panel

## License

MIT
