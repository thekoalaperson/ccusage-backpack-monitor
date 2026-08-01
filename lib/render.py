#!/usr/bin/env python3
"""Rich, colored, one-screen usage panel for a single Claude Code session.

Usage: render.py <session-id> [transcript-path]

Reads the session's transcripts directly rather than shelling out to
`ccusage session`, because that call rescans every transcript on the machine
(~16 CPU-seconds) on every turn, prices anything newer than the installed
ccusage at $0.00, and cannot see subagent spend at all.

The layout adapts to the pane width: fields are dropped in order of importance
until each row fits, so a 24-column pane degrades instead of wrapping.

Env knobs:
  CBM_BLOCKS       "0" hides the 5h burn-rate section
  CBM_GRAPH        "0" hides the sparkline
  CBM_AGENTS       "0" hides the subagent breakdown
  CBM_CONTEXT      "0" hides the context-window gauge
  CBM_BG           256-color index for an opaque background card (e.g. 234);
                   unset = transparent-friendly (no background fill)
  CBM_NO_NETWORK   "1" never refresh pricing over the network
  NO_COLOR         set (any value) to disable ANSI color entirely
Exit 0 always; prints a "waiting" line if data isn't ready yet.
"""
import sys, os, re, glob, shutil, time, unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pricing import Pricing            # noqa: E402
from usage import Scanner              # noqa: E402

sid = sys.argv[1] if len(sys.argv) > 1 else ""
transcript = sys.argv[2] if len(sys.argv) > 2 else ""
SHOW_BLOCKS = os.environ.get("CBM_BLOCKS", "1") != "0"
SHOW_GRAPH = os.environ.get("CBM_GRAPH", "1") != "0"
SHOW_AGENTS = os.environ.get("CBM_AGENTS", "1") != "0"
SHOW_CONTEXT = os.environ.get("CBM_CONTEXT", "1") != "0"
BG = os.environ.get("CBM_BG", "").strip()

# Respect NO_COLOR (no-color.org) so the panel is readable in plain terminals,
# piped output, and logs. BG implies a styled card, so it forces color back on.
NO_COLOR = ("NO_COLOR" in os.environ) and not BG

W = shutil.get_terminal_size((48, 24)).columns
# Clamp only the UPPER bound. A lower floor would make the panel render wider
# than the pane and wrap, which is the bug this replaced.
W = max(8, min(W, 72))
TIGHT = W < 34          # drop bars and token counts
NARROW = W < 46         # drop secondary labels


# ---- color (solid attributes; no DIM, which washes out on transparency) ---
def a(code): return "" if NO_COLOR else "\033[%sm" % code
RESET, BOLD = a(0), a(1)
GREEN, YELLOW, RED, CYAN, MAG, BLUE, WHITE, GREY = (
    a(32), a(33), a(31), a(36), a(35), a(94), a(97), a(90))


def model_color(name):
    if "fable" in name or "mythos" in name: return YELLOW
    if "opus" in name: return MAG
    if "sonnet" in name: return CYAN
    if "haiku" in name: return GREEN
    return WHITE


ANSI = re.compile(r"\033\[[0-9;]*m")


def dw(s):
    """Display width: CJK and most emoji occupy two terminal cells."""
    s = ANSI.sub("", s)
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def clip(s, width):
    """Hard-truncate to `width` display columns, keeping ANSI codes intact.

    The last line of defence: `fit` drops optional fields, but a long enough
    required field (a five-figure cost, a wide model name) could still overrun.
    Wrapping corrupts the whole panel, so clipping is always preferable.
    """
    if dw(s) <= width:
        return s
    out_chars = []
    used = 0
    i = 0
    while i < len(s):
        m = ANSI.match(s, i)
        if m:                       # zero-width, always keep
            out_chars.append(m.group(0))
            i = m.end()
            continue
        c = s[i]
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if used + cw > width:
            break
        out_chars.append(c)
        used += cw
        i += 1
    return "".join(out_chars) + RESET


def line(s=""):
    s = clip(s, W)
    if BG:
        s = s + " " * max(0, W - dw(s))
        return "\033[48;5;%sm%s%s" % (BG, s, RESET)
    return s


def fit(parts):
    """Join `parts` (text, optional) dropping optional tail items until it fits.

    Each part is (text, required). Optional parts are dropped from the right,
    which is where the least important fields live.
    """
    chosen = [p for p in parts]
    while True:
        s = "".join(t for t, _ in chosen)
        if dw(s) <= W or not any(not req for _, req in chosen):
            return s
        for i in range(len(chosen) - 1, -1, -1):
            if not chosen[i][1]:
                chosen.pop(i)
                break


def human(n):
    n = float(n or 0)
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return str(int(n))


def money(v):
    """Keep the headline readable at any magnitude in a narrow pane."""
    v = float(v or 0)
    if v >= 10000:
        return "$%.1fk" % (v / 1000.0)
    if v >= 100:
        return "$%.0f" % v
    return "$%.2f" % v


def short_model(m):
    m = (m or "?").replace("claude-", "")
    return re.sub(r"-20\d{6}$", "", m)


def hm(epoch):
    try:
        return time.strftime("%H:%M", time.localtime(epoch))
    except Exception:
        return "?"


BARS = "▁▂▃▄▅▆▇█"


def spark(vals, width):
    vals = vals[-width:]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return BARS[3] * len(vals)
    return "".join(BARS[int((v - lo) / (hi - lo) * (len(BARS) - 1))] for v in vals)


def meter(frac, width):
    frac = max(0.0, min(1.0, frac))
    fill = int(round(frac * width))
    if frac > 0 and fill == 0:
        fill = 1
    return "█" * fill + "·" * (width - fill)


def burn_color(cph):
    if cph is None: return GREY
    if cph < 3: return GREEN
    if cph < 10: return YELLOW
    return RED


def context_color(frac):
    if frac < 0.60: return GREEN
    if frac < 0.85: return YELLOW
    return RED


def locate(sid, transcript):
    if transcript and os.path.exists(transcript):
        return transcript
    hits = glob.glob(os.path.expanduser("~/.claude/projects/*/%s.jsonl" % sid))
    return hits[0] if hits else ""


# ---- gather ---------------------------------------------------------------
transcript = locate(sid, transcript)
if not sid or not transcript:
    print(line("%swaiting for session %s data...%s" % (GREY, sid[:8], RESET)))
    sys.exit(0)

pricing = Pricing()
scan = Scanner(pricing)
sess = scan.session(sid, transcript)

models = sorted(((n, m) for n, m in sess["models"].items()
                 if m["cost"] > 0 or m["input"] + m["output"] > 0),
                key=lambda kv: -kv[1]["cost"])
# Models we cannot price at all (e.g. "<synthetic>") carry no signal.
models = [(n, m) for n, m in models if pricing.is_priceable(n)]

tot_cost = sess["cost"]
tot_tok = sess["tokens"]
estimated = [n for n, _ in models if not pricing.is_exact(n)]

out = []

# ---- header ---------------------------------------------------------------
out.append(line(fit([("%s%sccusage%s" % (BOLD, CYAN, RESET), True),
                     ("  %s%s%s" % (WHITE, sid[:8], RESET), False)])))
out.append(line(fit([("%s%s%s%s" % (BOLD, GREEN, money(tot_cost), RESET), True),
                     ("  %s%s%s" % (BOLD, human(tot_tok), RESET), False),
                     ("%s tokens%s" % (BOLD, RESET), False)])))
if sess["agents"]:
    n = len(sess["agents"])
    noun = "ag" if TIGHT else ("agent" if n == 1 else "agents")
    out.append(line(fit([
        ("%s  you %s" % (GREY, money(sess["self_cost"])), True),
        (" · %d %s %s" % (n, noun, money(sess["agent_cost"])), False),
        ("%s" % RESET, True)])))

# ---- context window -------------------------------------------------------
if SHOW_CONTEXT and sess.get("context"):
    cmodel, ctok = sess["context"]
    cwin = pricing.context_window(cmodel)
    # Only claim a percentage when the window is actually known for this model.
    # For an unrecognised model we fall back to the smallest common window, and
    # a session larger than that would render as ">100% full" — alarming and
    # false. Show the raw count instead and say nothing we can't stand behind.
    known = pricing.is_exact(cmodel) and ctok <= cwin
    out.append(line())
    if known:
        frac = (ctok / float(cwin)) if cwin else 0.0
        col = context_color(frac)
        mw = 6 if TIGHT else (8 if NARROW else 10)
        out.append(line(fit([
            ("%sctx%s " % (BOLD, RESET), True),
            ("%s%s%s " % (col, meter(frac, mw), RESET), True),
            ("%s%d%%%s" % (col, round(frac * 100), RESET), True),
            ("  %s%s/%s%s" % (GREY, human(ctok), human(cwin), RESET), False)])))
    else:
        out.append(line(fit([
            ("%sctx%s " % (BOLD, RESET), True),
            ("%s%s%s" % (WHITE, human(ctok), RESET), True),
            (" %stokens%s" % (GREY, RESET), False)])))

# ---- models ---------------------------------------------------------------
out.append(line())
out.append(line("%sMODELS%s" % (BOLD, RESET)))
if not models:
    out.append(line("  %sno priced usage yet%s" % (GREY, RESET)))
# Leave room for the bullet, cost, and padding; clamp so names stay readable.
namew = max(6, min(11, W - 13))
for n, m in models[:6]:
    name = short_model(n)
    col = model_color(name)
    mtok = m["input"] + m["output"] + m["cache_read"] + m["cache_creation"]
    share = (m["cost"] / tot_cost) if tot_cost else 0
    mark = "~" if not pricing.is_exact(n) else " "
    out.append(line(fit([
        ("  %s●%s " % (col, RESET), True),
        ("%s%-*s%s" % (col, namew, name[:namew], RESET), True),
        (" %s%s%s%s%s" % (GREY, mark, GREEN, money(m["cost"]), RESET), True),
        (" %s%s%s" % (col, meter(share, 8), RESET), False),
        (" %s%5s%s" % (WHITE, human(mtok), RESET), False)])))

# ---- subagents ------------------------------------------------------------
if SHOW_AGENTS and sess["agents"]:
    top = sess["agents"][:3]
    out.append(line())
    head = "%sAGENTS%s" % (BOLD, RESET)
    if len(sess["agents"]) > len(top) and not TIGHT:
        head += "  %s(top %d of %d)%s" % (GREY, len(top), len(sess["agents"]), RESET)
    out.append(line(head))
    aw = 10 if TIGHT else 16
    for ag in top:
        named = [x for x in ag["models"] if pricing.is_priceable(x)]
        mods = "+".join(short_model(x).split("-")[0] for x in named[:2])
        out.append(line(fit([
            ("  %s%-*s%s" % (WHITE, aw, ag["name"][:aw], RESET), True),
            (" %s%7s%s" % (GREEN, money(ag["cost"]), RESET), True),
            (" %s%s%s" % (GREY, mods[:14], RESET), False)])))

# ---- 5h block -------------------------------------------------------------
if SHOW_BLOCKS:
    try:
        blk = scan.active_block()
    except Exception:
        blk = None
    if blk:
        col = burn_color(blk["rate"])
        flame = "" if NARROW else "🔥 "
        out.append(line())
        out.append(line(fit([
            ("%s5h%s %s%s%s" % (BOLD, RESET, YELLOW, money(blk["cost"]), RESET), True),
            ("  %s%s$%.1f/hr%s" % (col, flame, blk["rate"], RESET), True),
            ("  %s~%s%s" % (WHITE, money(blk["projected"]), RESET), False),
            ("  %sends %s%s" % (GREY, hm(blk["end"]), RESET), False)])))

# ---- sparkline ------------------------------------------------------------
if SHOW_GRAPH and sess["outputs"]:
    width = max(8, W - 2)
    outs = sess["outputs"]
    out.append(line())
    out.append(line(fit([
        ("%sout/turn%s" % (BOLD, RESET), True),
        (" %s(last %d)%s" % (GREY, min(len(outs), width), RESET), False)])))
    out.append(line("%s%s%s" % (BLUE, spark(outs, width), RESET)))

if estimated:
    out.append(line())
    out.append(line(fit([
        ("%s~ estimated rate" % GREY, True),
        (" for %s" % ", ".join(short_model(x) for x in estimated[:2]), False),
        ("%s" % RESET, True)])))

out.append(line())
out.append(line(fit([("%slive%s" % (GREY, ""), True),
                     (" · updates on change", False),
                     (" · Ctrl-C to stop", False),
                     ("%s" % RESET, True)])))
print("\n".join(out))

try:
    scan.save()
except Exception:
    pass
