# ccusage-backpack-monitor

A Claude Code plugin that pops open a **side pane** showing a **live,
change-driven cost readout** for the session you just started — and closes that
pane automatically when the session ends. Think of it as a usage "backpack" your
session carries while it runs.

It reads Claude Code's own transcripts, so it prices **every model** (including
ones released after this plugin was), counts **subagent and workflow spend**
against the session that spawned it, and renders in ~0.07s.

It works in **tmux**, **WezTerm**, and **iTerm2**, on **macOS and Linux**.

<p align="center">
  <img src="assets/hero.png" width="820"
       alt="A live ccusage cost pane — spend, per-model breakdown, burn rate, and a tokens-per-turn sparkline — in a side pane beside a Claude Code session">
</p>

> **Status:** v0.9.3 — supports tmux (macOS/Linux), WezTerm (macOS/Linux), and
> iTerm2 (macOS). In any other terminal the hooks no-op silently, so it's safe
> to install anywhere.

## Why

`/usage` is a one-shot, machine-local snapshot. This gives you an always-on,
per-session readout in a pane beside your work — without polling on a timer. It
watches only *this* session's files and redraws only when something actually
changed, so it's near-live yet idle-cheap.

Three things it gets right that a generic `ccusage session` call does not:

- **New models are priced.** Tools that price from a snapshot baked in at
  release time show `$0.00` for anything newer — that is why Opus 5 and Sonnet 5
  read as free next to millions of tokens. Pricing here refreshes daily and
  falls back to per-family estimates (marked `~`) rather than to zero.
- **Subagents are counted.** Task agents and workflow fan-out write to their own
  transcripts; their spend is attributed back to the session that spawned it.
  On one measured session that was $523 across 171 agents, and two models that
  otherwise never appeared at all.
- **Messages are counted once.** A single assistant reply is written to the
  transcript once per content block, each line repeating the same cumulative
  usage — counting the lines bills a 13-block reply thirteen times.

## Requirements

- **A supported terminal**, any one of:
  - **tmux** (macOS or Linux) — the pane is a `tmux split-window`.
  - **WezTerm** (macOS or Linux) — the pane is a `wezterm cli split-pane`.
  - **iTerm2** (macOS) — the pane is opened via iTerm2's AppleScript.

  Detection is automatic and ordered **tmux → WezTerm → iTerm2**, so if you run
  tmux inside iTerm2 or WezTerm, the tmux split is used (the multiplexer owns the
  layout). You can force a choice with `CBM_BACKEND` (see Configuration).
- **`python3`** — the only real dependency, resolved via your `PATH` (macOS
  ships one at `/usr/bin/python3`; it may prompt to install the Xcode Command
  Line Tools the first time). No pip packages; the standard library is enough.
- [`ccusage`](https://github.com/ryoppippi/ccusage) is **optional** — used only
  as a fallback renderer if `python3` is missing.
- On iTerm2 only, a one-time macOS Automation prompt ("iTerm wants to control
  iTerm") must be allowed.

### Where it works (and where it no-ops)

| Scenario | Behaviour |
|---|---|
| tmux / WezTerm / iTerm2 + python3 | Full rich panel (cost, agents, burn rate, sparkline) |
| Supported terminal, no python3, ccusage present | Plain `ccusage` text fallback, still live |
| Supported terminal, **neither** | Pane shows the exact, OS-aware fix and becomes a shell so you can run it |
| tmux or WezTerm on **Linux** | Full support |
| Any other terminal (plain Terminal.app, kitty, Ghostty, …) | Hooks **no-op silently** (safe to leave installed) |

> **In an unsupported terminal (Terminal.app, VS Code, Ghostty, …)?** Those apps
> have no scriptable split-pane, so the plugin can't draw the monitor there. The
> universal workaround is **tmux**: run `tmux`, then `claude` inside it, and the
> monitor opens as a tmux split — in *any* terminal.

No `watch`, `jq`, `node`, or charting libraries are required — the only hard
runtime dependencies are a supported terminal and the stock `python3`.

## Install

Inside Claude Code:

```text
/plugin marketplace add thekoalaperson/ccusage-backpack-monitor
/plugin install ccusage-backpack-monitor@ccusage-backpack-monitor
```

Then start a fresh `claude` in tmux, WezTerm, or iTerm2 — a side pane opens
automatically.

<details>
<summary>Local development install</summary>

Point the marketplace at a local checkout instead of GitHub:

```text
/plugin marketplace add /path/to/ccusage-backpack-monitor
/plugin install ccusage-backpack-monitor@ccusage-backpack-monitor
```
</details>

## Commands

`/ccusage-backpack-monitor:ccusage-monitor` — **toggles** the monitor pane for
the **current** session: run it once to open, again to close. Plugin commands are
namespaced, so type `/ccusage` and let autocomplete finish it.

If the open pane was started by an older version of the plugin (i.e. you upgraded
mid-session), or a stray duplicate is open, toggling **replaces** them with a
single current pane instead of closing, and says so. Panes belonging to other
Claude sessions are left alone. A running pane is a long-lived process, so `/plugin` + `/reload-plugins`
alone can't update it — this is how you pick up a new version without restarting
Claude.

## Configuration

Set these as environment variables before launching `claude`:

| Var                  | Default       | Meaning                                              |
|----------------------|---------------|------------------------------------------------------|
| `CBM_POLL`           | `3`           | Seconds between cheap file-change checks              |
| `CBM_SPLIT`          | `vertically`  | `vertically` (side-by-side) or `horizontally`        |
| `CBM_SIZE`           | `25`          | Percent of the terminal the monitor pane takes (clamped to 5–90). The main Claude session keeps the rest, so the default is a 3:1 split in Claude's favor. Applies to **all** backends (tmux, WezTerm, iTerm2). |
| `CBM_BACKEND`        | _(auto)_      | Force a terminal backend: `tmux`, `wezterm`, or `iterm`. Unset = auto-detect (tmux → WezTerm → iTerm2). |
| `CBM_WEZTERM_PERCENT`| `CBM_SIZE`    | WezTerm-only override for the split size (percent of the source pane). Unset, it inherits `CBM_SIZE`. |
| `CBM_BLOCKS`         | `1`           | `0` hides the 5h burn-rate/projection section        |
| `CBM_AGENTS`         | `1`           | `0` hides the subagent breakdown                     |
| `CBM_GRAPH`          | `1`           | `0` hides the sparkline                              |
| `CBM_CONTEXT`        | `1`           | `0` hides the context-window gauge                   |
| `NO_COLOR`           | _(unset)_     | Set (any value) to disable ANSI color entirely ([no-color.org](https://no-color.org)). `CBM_BG` overrides it. |
| `CBM_NO_NETWORK`     | _(unset)_     | `1` pins pricing to the bundled table (no daily refresh) |
| `CBM_BG`             | _(unset)_     | A 256-color index (e.g. `234`) paints an opaque background card behind the panel — useful in **transparent terminals**. Unset = solid high-contrast text, no fill. |

The panel shows **every model used in the session** — including those used by
subagents — with its own cost, a cost-share bar, and token count. When a session
spawned agents, the header splits the total into what you spent directly versus
what your agents spent, and an `AGENTS` section lists the biggest ones.

A **context gauge** (`ctx ██████···· 58%  577K/1.0M`) shows how full the window is,
taken from the last turn's input side. The percentage appears only when the
model's context window is actually known; for an unrecognised model the raw token
count is shown instead of a percentage against a guessed window.

The layout **adapts to the pane width** — fields are dropped in order of
importance, so a 24-column pane degrades gracefully instead of wrapping.

### Pricing

Rates come from a bundled table, refreshed at most once a day from
[LiteLLM](https://github.com/BerriAI/litellm) into
`~/.local/state/ccusage-backpack-monitor/pricing.json`. A model missing from
both falls back to a per-family estimate and is marked `~` in the panel, so a
newly released model is never silently free. To pin a rate yourself, create
`~/.config/ccusage-backpack-monitor/pricing.json`:

```json
{ "claude-opus-5": { "input": 5.0, "output": 25.0 } }
```

Values are USD per million tokens; cache rates are derived (read 0.1x input,
5-minute write 1.25x, 1-hour write 2x) unless given explicitly.

## Performance

The watcher is **change-driven, not interval-driven**. When idle it only does a
cheap `stat` on this session's transcript and its subagent directory every
`CBM_POLL` seconds — no subprocess, no parsing. It renders only when something
actually changed, so cost is proportional to real activity, not wall-clock time.

A render is **~0.07s**. Earlier versions shelled out to `ccusage session`, which
rescans every transcript on the machine — ~2s wall and ~16 CPU-seconds, on every
turn. Reading only this session's files removed that. Per-file results are
memoised by size and mtime in
`~/.local/state/ccusage-backpack-monitor/scan-cache.json`, so finished agents are
never re-read.

## How it works

| Phase  | File                | Hook           | Action                                          |
|--------|---------------------|----------------|-------------------------------------------------|
| open   | `bin/open-pane.sh`  | `SessionStart` | split pane, run watcher, stash the backend + pane handle |
| render | `bin/watch.sh`      | —              | stat transcript + agents; on change, draw       |
| panel  | `lib/render.py`     | —              | rich colored cost/agents/burn-rate view         |
| cost   | `lib/usage.py`      | —              | transcript scan, dedup, subagent attribution    |
| pick   | `bin/resolve-session.py` | —         | works out which session a pane should follow    |
| rates  | `lib/pricing.py`    | —              | model pricing with daily refresh + fallbacks    |
| close  | `bin/close-pane.sh` | `SessionEnd`   | look up the handle, close that exact pane via its backend |

**Backends.** All terminal-specific logic lives in `lib/common.sh` as small
per-backend functions — `cbm_{detect,open,alive,close}_<backend>`. A cached
`cbm_backend()` picks the active one (tmux → WezTerm → iTerm2). Adding a new
terminal (kitty, Ghostty, …) is four functions plus one word in that list.

State (one tiny `<session-id>.pane` file per session) lives in
`~/.local/state/ccusage-backpack-monitor` and self-prunes after a day. It records
the **backend** and the **pane handle** (two lines) so the `SessionEnd` hook — a
fresh process whose environment may differ — closes via the recorded backend
rather than re-detecting. (A fixed path on purpose: the hooks and the slash
command must agree on it, so the close hook can find a pane the command opened.)

## Tests

```sh
python3 tests/test_usage.py
```

61 fixture-based regression tests covering pricing resolution, deduplication,
subagent attribution, 5h block math, context-window handling, session
resolution (agents are never mistaken for sessions), the open/close/restart
toggle, and panel rendering (including an overflow check at every pane width). They build their own transcripts in a temp directory — they never read
your real usage data and never touch the network.

## Roadmap

- More terminals: kitty, Ghostty (tmux, WezTerm, and iTerm2 are supported)
- A companion skill/command for on-demand usage summaries
- Optional context-window % in the panel

## License

MIT
