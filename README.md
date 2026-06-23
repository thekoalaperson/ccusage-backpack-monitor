# ccusage-backpack-monitor

A Claude Code plugin that pops open an **iTerm2 side pane** showing a **live,
change-driven [ccusage](https://github.com/ryoppippi/ccusage) readout** for the
session you just started — and closes that pane automatically when the session
ends. Think of it as a usage "backpack" your session carries while it runs.

> **Status:** v0.1.0 — macOS + iTerm2 only. On any other terminal the hooks
> no-op silently, so it's safe to install anywhere.

## Why

`/usage` is a one-shot, machine-local snapshot. This gives you an always-on,
per-session readout in a pane beside your work — without polling on a timer.
It watches only *this* session's transcript file for changes and runs `ccusage`
only when something actually changed, so it's near-live yet idle-cheap.

## Requirements

- macOS with **iTerm2**
- [`ccusage`](https://github.com/ryoppippi/ccusage) (`brew install ccusage`, or it
  falls back to `npx -y ccusage@latest`)
- One-time: allow the macOS Automation prompt ("iTerm wants to control iTerm").

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

| Var         | Default       | Meaning                                            |
|-------------|---------------|----------------------------------------------------|
| `CBM_POLL`  | `3`           | Seconds between cheap file-change checks            |
| `CBM_SPLIT` | `vertically`  | `vertically` (side-by-side) or `horizontally`      |

## How it works

| Phase  | File               | Hook           | Action                                          |
|--------|--------------------|----------------|-------------------------------------------------|
| open   | `bin/open-pane.sh`  | `SessionStart` | split pane, run watcher, stash the pane id      |
| render | `bin/watch.sh`      | —              | stat transcript; run ccusage only on change     |
| close  | `bin/close-pane.sh` | `SessionEnd`   | look up the pane id, close that exact pane       |

State (one tiny `<session-id>.pane` file per session) lives in the plugin's
data dir and self-prunes after a day.

## Roadmap

- Lightweight in-pane temporal visualizations (Unicode sparklines, no deps)
- Support for more terminals (tmux, WezTerm, kitty, Ghostty)
- A companion skill/command for on-demand usage summaries

## License

MIT
