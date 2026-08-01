#!/usr/bin/env python3
"""Rich, colored, one-screen usage panel for a single Claude Code session.

Usage: render.py <session-id> [transcript-path]

Reads the session's transcripts directly rather than shelling out to
`ccusage session`, because that call rescans every transcript on the machine
(~16 CPU-seconds) on every turn, prices anything newer than the installed
ccusage at $0.00, and cannot see subagent spend at all.

Env knobs:
  CBM_BLOCKS       "0" hides the 5h burn-rate section
  CBM_GRAPH        "0" hides the sparkline
  CBM_AGENTS       "0" hides the subagent breakdown
  CBM_BG           256-color index for an opaque background card (e.g. 234);
                   unset = transparent-friendly (no background fill)
  CBM_NO_NETWORK   "1" never refresh pricing over the network
Exit 0 always; prints a "waiting" line if data isn't ready yet.
"""
import sys, os, re, glob, shutil, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pricing import Pricing            # noqa: E402
from usage import Scanner              # noqa: E402

sid = sys.argv[1] if len(sys.argv) > 1 else ""
transcript = sys.argv[2] if len(sys.argv) > 2 else ""
SHOW_BLOCKS = os.environ.get("CBM_BLOCKS", "1") != "0"
SHOW_GRAPH = os.environ.get("CBM_GRAPH", "1") != "0"
SHOW_AGENTS = os.environ.get("CBM_AGENTS", "1") != "0"
BG = os.environ.get("CBM_BG", "").strip()

W = max(40, min(shutil.get_terminal_size((48, 24)).columns, 64))

# ---- color (solid attributes; no DIM, which washes out on transparency) ---
def a(code): return "\033[%sm" % code
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
def vlen(s): return len(ANSI.sub("", s))


def line(s=""):
    if BG:
        s = s + " " * max(0, W - vlen(s))
        return "\033[48;5;%sm%s%s" % (BG, s, RESET)
    return s


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


def burn_color(cph):
    if cph is None: return GREY
    if cph < 3: return GREEN
    if cph < 10: return YELLOW
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
out.append(line("%s%sccusage%s  %s%s%s" % (BOLD, CYAN, RESET, WHITE, sid[:8], RESET)))
out.append(line("%s%s%s%s  %s%s tokens%s"
                % (BOLD, GREEN, money(tot_cost), RESET, BOLD, human(tot_tok), RESET)))
if sess["agents"]:
    out.append(line("%s  you %s · %d agent%s %s%s"
                    % (GREY, money(sess["self_cost"]), len(sess["agents"]),
                       "" if len(sess["agents"]) == 1 else "s",
                       money(sess["agent_cost"]), RESET)))
out.append(line())

# ---- models ---------------------------------------------------------------
out.append(line("%sMODELS%s" % (BOLD, RESET)))
if not models:
    out.append(line("  %sno priced usage yet%s" % (GREY, RESET)))
for n, m in models[:6]:
    name = short_model(n)
    col = model_color(name)
    mtok = m["input"] + m["output"] + m["cache_read"] + m["cache_creation"]
    share = (m["cost"] / tot_cost) if tot_cost else 0
    barlen = 8
    fill = int(round(share * barlen))
    # Any model with real spend should show at least one block, so a small
    # contributor never renders as an empty row.
    if m["cost"] > 0 and fill == 0:
        fill = 1
    bar = "█" * fill + "·" * (barlen - fill)
    mark = "~" if not pricing.is_exact(n) else " "
    out.append(line("  %s●%s %s%-11s%s %s%s%s%6s%s %s%s%s %s%5s%s"
                    % (col, RESET, col, name[:11], RESET,
                       GREY, mark, GREEN, "$%.2f" % m["cost"], RESET,
                       col, bar, RESET, WHITE, human(mtok), RESET)))

# ---- subagents ------------------------------------------------------------
if SHOW_AGENTS and sess["agents"]:
    top = sess["agents"][:3]
    out.append(line())
    head = "%sAGENTS%s" % (BOLD, RESET)
    if len(sess["agents"]) > len(top):
        head += "  %s(top %d of %d)%s" % (GREY, len(top), len(sess["agents"]), RESET)
    out.append(line(head))
    for ag in top:
        named = [x for x in ag["models"] if pricing.is_priceable(x)]
        mods = "+".join(short_model(x).split("-")[0] for x in named[:2])
        out.append(line("  %s%-16s%s %s%7s%s %s%s%s"
                        % (WHITE, ag["name"][:16], RESET,
                           GREEN, "$%.2f" % ag["cost"], RESET,
                           GREY, mods[:14], RESET)))

# ---- 5h block -------------------------------------------------------------
if SHOW_BLOCKS:
    try:
        blk = scan.active_block()
    except Exception:
        blk = None
    if blk:
        col = burn_color(blk["rate"])
        out.append(line())
        out.append(line("%s5h%s %s%s%s  %s🔥 $%.1f/hr%s  %sends %s%s  %s~%s%s"
                        % (BOLD, RESET, YELLOW, money(blk["cost"]), RESET,
                           col, blk["rate"], RESET,
                           GREY, hm(blk["end"]), RESET,
                           WHITE, money(blk["projected"]), RESET)))

# ---- sparkline ------------------------------------------------------------
if SHOW_GRAPH and sess["outputs"]:
    width = W - 4
    outs = sess["outputs"]
    out.append(line())
    out.append(line("%sout/turn%s %s(last %d)%s"
                    % (BOLD, RESET, GREY, min(len(outs), width), RESET)))
    out.append(line("%s%s%s" % (BLUE, spark(outs, width), RESET)))

if estimated:
    out.append(line())
    out.append(line("%s~ estimated rate for %s%s"
                    % (GREY, ", ".join(short_model(x) for x in estimated[:2]), RESET)))

out.append(line())
out.append(line("%slive · updates on change · Ctrl-C to stop%s" % (GREY, RESET)))
print("\n".join(out))

try:
    scan.save()
except Exception:
    pass
