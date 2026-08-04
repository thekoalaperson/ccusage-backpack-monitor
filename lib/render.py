#!/usr/bin/env python3
"""Rich, colored, one-screen usage panel for a single Claude Code session.

Usage: render.py <session-id> [transcript-path] [tab]

Reads the session's transcripts directly rather than shelling out to
`ccusage session`, because that call rescans every transcript on the machine
(~16 CPU-seconds) on every turn, prices anything newer than the installed
ccusage at $0.00, and cannot see subagent spend at all.

Layout: a pinned header (spend + the three meters that can block you) is drawn
on every tab, so the numbers worth watching never require a keystroke. Below it
sits a tab strip; `tab` selects which detail view is drawn underneath. The
watcher passes the current tab in and re-renders on an arrow key.

The layout adapts to the pane width: fields are dropped in order of importance
until each row fits, so a 24-column pane degrades instead of wrapping.

Env knobs:
  CBM_TABS         "0" hides the tab strip -- a single static panel, as before
  CBM_BLOCKS       "0" hides the burn-rate line
  CBM_GRAPH        "0" hides the sparkline
  CBM_AGENTS       "0" hides the subagent breakdown
  CBM_CONTEXT      "0" hides the context-window gauge
  CBM_LIMITS       "0" hides the account rate-limit meters
  CBM_ACCOUNT      "0" hides account identity entirely; "full" adds email + org
                   (default: first name only -- this pane gets screen-shared)
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
import account as acct                 # noqa: E402

sid = sys.argv[1] if len(sys.argv) > 1 else ""
transcript = sys.argv[2] if len(sys.argv) > 2 else ""
SHOW_BLOCKS = os.environ.get("CBM_BLOCKS", "1") != "0"
SHOW_GRAPH = os.environ.get("CBM_GRAPH", "1") != "0"
SHOW_AGENTS = os.environ.get("CBM_AGENTS", "1") != "0"
SHOW_CONTEXT = os.environ.get("CBM_CONTEXT", "1") != "0"
SHOW_LIMITS = os.environ.get("CBM_LIMITS", "1") != "0"
SHOW_TABS = os.environ.get("CBM_TABS", "1") != "0"
ACCOUNT_MODE = os.environ.get("CBM_ACCOUNT", "").lower()
SHOW_ACCOUNT = ACCOUNT_MODE != "0"
BG = os.environ.get("CBM_BG", "").strip()

# Respect NO_COLOR (no-color.org) so the panel is readable in plain terminals,
# piped output, and logs. BG implies a styled card, so it forces color back on.
NO_COLOR = ("NO_COLOR" in os.environ) and not BG

# (key, full label, short label) -- short labels are used when the pane is too
# narrow for the full strip. Order is the arrow-key order.
TABS = [("live", "live", "live"),
        ("limits", "limits", "lim"),
        ("models", "models", "mod"),
        ("agents", "agents", "agt"),
        ("trend", "trend", "trd"),
        ("account", "account", "acct")]


def _tab_index(arg):
    """Accept an index or a name, and never fail -- a bad tab shows `live`."""
    if not arg:
        return 0
    arg = str(arg).strip().lower()
    for i, (key, _f, _s) in enumerate(TABS):
        if arg == key:
            return i
    try:
        return max(0, min(len(TABS) - 1, int(arg)))
    except ValueError:
        return 0


TAB = _tab_index(sys.argv[3] if len(sys.argv) > 3 else "") if SHOW_TABS else 0

W = shutil.get_terminal_size((48, 24)).columns
# Clamp only the UPPER bound. A lower floor would make the panel render wider
# than the pane and wrap, which is the bug this replaced.
W = max(8, min(W, 72))
TIGHT = W < 34          # drop bars and token counts
NARROW = W < 46         # drop secondary labels


# ---- color (solid attributes; no DIM, which washes out on transparency) ---
def a(code): return "" if NO_COLOR else "\033[%sm" % code
RESET, BOLD, REV = a(0), a(1), a(7)
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


def when(epoch, now=None):
    """A reset time: bare clock if it lands today, weekday + clock otherwise."""
    if not epoch:
        return ""
    now = now if now is not None else time.time()
    try:
        if epoch - now < 12 * 3600 and time.localtime(epoch).tm_yday == time.localtime(now).tm_yday:
            return time.strftime("%H:%M", time.localtime(epoch))
        return time.strftime("%a %H:%M", time.localtime(epoch))
    except Exception:
        return ""


def dur(secs):
    """Compact duration: 45s / 12m / 4h50m / 3d."""
    secs = max(0, int(secs or 0))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        h, m = secs // 3600, (secs % 3600) // 60
        return "%dh%02dm" % (h, m) if m else "%dh" % h
    return "%dd" % (secs // 86400)


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


def level_color(rank):
    return (GREEN, YELLOW, RED)[max(0, min(2, rank))]


def context_color(frac):
    if frac < 0.60: return GREEN
    if frac < 0.85: return YELLOW
    return RED


def meter_row(label, frac, pct_text, detail, col, out):
    """One aligned gauge row: label, bar, percent, then an optional detail.

    All three limits share this shape on purpose -- ctx, the 5h window and the
    weekly cap are the same question ("how full, and when does it reset"), so
    they should be one vertical scan rather than three different-looking rows.
    """
    mw = 6 if TIGHT else (8 if NARROW else 10)
    out.append(line(fit([
        ("%s%-4s%s " % (BOLD, label, RESET), True),
        ("%s%s%s" % (col, meter(frac, mw), RESET), True),
        ("%s%5s%s" % (col, pct_text, RESET), True),
        ("  %s%s%s" % (GREY, detail, RESET), False)])))


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
meta = sess.get("meta") or {}

# Account facts are strictly optional: a missing or renamed cache hides rows
# rather than breaking the panel.
account = acct.Account()
try:
    ident = account.identity() if SHOW_ACCOUNT else None
except Exception:
    ident = None
try:
    limits = account.limits() if SHOW_LIMITS else None
except Exception:
    limits = None

LIM_AGE = (limits or {}).get("age")
LIM_STALE = LIM_AGE is None or LIM_AGE > acct.STALE_AFTER

# If we were pointed at a subagent transcript, say so. Rendering one silently as
# "the session" reports a single agent's spend as the whole session's — a wrong
# number that looks entirely plausible, which is the worst kind.
own = scan.scan_file(transcript) or {}
agent_of = None
_team = own.get("team") or ""
if _team.startswith("session-"):
    agent_of = _team[len("session-"):][:8]

models = sorted(((n, m) for n, m in sess["models"].items()
                 if m["cost"] > 0 or m["input"] + m["output"] > 0),
                key=lambda kv: -kv[1]["cost"])
# Models we cannot price at all (e.g. "<synthetic>") carry no signal.
models = [(n, m) for n, m in models if pricing.is_priceable(n)]

tot_cost = sess["cost"]
tot_tok = sess["tokens"]
turns = sum(m["turns"] for _n, m in models)
estimated = [n for n, _ in models if not pricing.is_exact(n)]

out = []


# ---- pinned header --------------------------------------------------------
def draw_header():
    if agent_of:
        out.append(line(fit([
            ("%sagent %s" % (YELLOW, own.get("agent") or "?"), True),
            ("%s of session %s%s" % (GREY, agent_of, RESET), False),
            ("%s" % RESET, True)])))
    out.append(line(fit([
        ("%s%s%s%s" % (BOLD, GREEN, money(tot_cost), RESET), True),
        ("  %s%s%s" % (BOLD, human(tot_tok), RESET), False),
        ("%s tok%s" % (BOLD, RESET), False),
        (" %s· %d turns%s" % (GREY, turns, RESET), False)])))
    if sess["agents"]:
        n = len(sess["agents"])
        noun = "ag" if TIGHT else ("agent" if n == 1 else "agents")
        out.append(line(fit([
            ("%s  you %s" % (GREY, money(sess["self_cost"])), True),
            (" · %d %s %s" % (n, noun, money(sess["agent_cost"])), False),
            ("%s" % RESET, True)])))

    rows = 0
    # ctx first: of the three, it's the one that fills in minutes.
    if SHOW_CONTEXT and sess.get("context"):
        cmodel, ctok = sess["context"]
        cwin = pricing.context_window(cmodel)
        # Only claim a percentage when the window is actually known for this
        # model. For an unrecognised model we fall back to the smallest common
        # window, and a session larger than that would render as ">100% full" —
        # alarming and false. Show the raw count instead.
        known = pricing.is_exact(cmodel) and ctok <= cwin
        if not rows:
            out.append(line())
        rows += 1
        if known:
            frac = (ctok / float(cwin)) if cwin else 0.0
            meter_row("ctx", frac, "%d%%" % round(frac * 100),
                      "%s/%s" % (human(ctok), human(cwin)),
                      context_color(frac), out)
        else:
            out.append(line(fit([
                ("%sctx%s  " % (BOLD, RESET), True),
                ("%s%s%s" % (WHITE, human(ctok), RESET), True),
                (" %stokens%s" % (GREY, RESET), False)])))

    if limits:
        # A stale cache is marked with the same `~` the panel already uses for
        # an estimated rate: a number we are not willing to vouch for as current.
        mark = "~" if LIM_STALE else ""
        for label, row in (("5h", limits.get("session")),
                           ("week", (limits.get("weekly") or [None])[0])):
            if not row:
                continue
            if not rows:
                out.append(line())
            rows += 1
            pct = row["percent"]
            detail = when(row["resets"])
            if row.get("scope") and not TIGHT:
                detail = "%s %s" % (row["scope"], detail) if detail else row["scope"]
            extra = len(limits.get("weekly") or []) - 1
            if label == "week" and extra > 0 and not NARROW:
                detail += "  +%d" % extra
            meter_row(label, pct / 100.0, "%s%d%%" % (mark, round(pct)),
                      detail, level_color(acct.severity_rank(row)), out)
    return rows


# ---- tab strip ------------------------------------------------------------
def draw_tabs():
    if not SHOW_TABS:
        return
    out.append(line())
    for idx in (1, 2):              # full labels, then short ones
        seg = []
        for i, t in enumerate(TABS):
            name = t[idx]
            if i == TAB:
                seg.append("[%s]" % name if NO_COLOR else "%s%s%s" % (REV, name, RESET))
            else:
                seg.append("%s%s%s" % (GREY, name, RESET))
        s = " ".join(seg)
        if dw(s) <= W:
            out.append(line(s))
            return
    # Too narrow for any strip: say where you are, not what else exists.
    out.append(line("%s%d/%d%s %s%s%s" % (
        GREY, TAB + 1, len(TABS), RESET, BOLD, TABS[TAB][1], RESET)))


# ---- tab bodies -----------------------------------------------------------
def sect(title, note=""):
    out.append(line())
    s = "%s%s%s" % (BOLD, title, RESET)
    if note and not TIGHT:
        s += "  %s%s%s" % (GREY, note, RESET)
    out.append(line(s))


def draw_models_brief():
    sect("MODELS")
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
    # Cache hit rate is the single biggest lever on what a session costs, and
    # it's already summed -- so it rides along on a line that exists anyway.
    reads = sum(m["cache_read"] for _n, m in models)
    fresh = sum(m["input"] + m["cache_creation"] for _n, m in models)
    if reads + fresh:
        hit = 100.0 * reads / (reads + fresh)
        per = (tot_cost / turns) if turns else 0.0
        out.append(line(fit([
            ("    %scache %d%%%s" % (GREY, round(hit), RESET), True),
            (" %s· %s/turn%s" % (GREY, money(per), RESET), False)])))


def draw_agents_brief():
    if not (SHOW_AGENTS and sess["agents"]):
        return
    top = sess["agents"][:3]
    note = ""
    if len(sess["agents"]) > len(top):
        note = "(top %d of %d)" % (len(top), len(sess["agents"]))
    sect("AGENTS", note)
    aw = 10 if TIGHT else 16
    for ag in top:
        named = [x for x in ag["models"] if pricing.is_priceable(x)]
        mods = "+".join(short_model(x).split("-")[0] for x in named[:2])
        out.append(line(fit([
            ("  %s%-*s%s" % (WHITE, aw, ag["name"][:aw], RESET), True),
            (" %s%7s%s" % (GREEN, money(ag["cost"]), RESET), True),
            (" %s%s%s" % (GREY, mods[:14], RESET), False)])))


def draw_burn():
    if not SHOW_BLOCKS:
        return
    try:
        blk = scan.active_block()
    except Exception:
        blk = None
    if not blk:
        return
    col = burn_color(blk["rate"])
    flame = "" if NARROW else "🔥 "
    out.append(line())
    parts = []
    # When the server's own 5h percentage is on screen the local dollar total
    # for that window is redundant; without it, it's the only 5h figure there is.
    if not (limits and limits.get("session")):
        parts.append(("%s5h%s %s%s%s  " % (BOLD, RESET, YELLOW, money(blk["cost"]), RESET), True))
    parts += [("%s%s$%.1f/hr%s" % (col, flame, blk["rate"], RESET), True),
              ("  %s~%s%s" % (WHITE, money(blk["projected"]), RESET), False),
              ("  %sends %s%s" % (GREY, hm(blk["end"]), RESET), False)]
    out.append(line(fit(parts)))


def draw_graph():
    if not (SHOW_GRAPH and sess["outputs"]):
        return
    width = max(8, W - 2)
    outs = sess["outputs"]
    out.append(line())
    out.append(line(fit([
        ("%sout/turn%s" % (BOLD, RESET), True),
        (" %s(last %d)%s" % (GREY, min(len(outs), width), RESET), False)])))
    out.append(line("%s%s%s" % (BLUE, spark(outs, width), RESET)))


def draw_footer_id():
    """The reference block: what this session is, dimmed, below a rule.

    Everything above the rule changes while you work; nothing below it does.
    Two weights and one divider is the whole navigation aid.
    """
    bits = []
    title = (meta.get("title") or "").strip()
    branch = (meta.get("branch") or "").strip()
    proj = os.path.basename((meta.get("cwd") or "").rstrip("/"))
    if title:
        bits.append(title)
    elif proj:
        bits.append(proj)
    line2 = []
    if ident and ident.get("plan"):
        line2.append(ident["plan"])
    if ident and ident.get("name"):
        line2.append(ident["name"])
    if branch and branch != "HEAD":
        line2.append(branch)
    if not bits and not line2:
        return
    out.append(line())
    out.append(line("%s%s%s" % (GREY, "─" * max(0, min(W, 30)), RESET)))
    if bits:
        out.append(line("%s%s%s" % (GREY, clip(bits[0], W), RESET)))
    if line2:
        out.append(line("%s%s%s" % (GREY, " · ".join(line2), RESET)))


def tab_live():
    draw_models_brief()
    draw_agents_brief()
    draw_burn()
    draw_graph()
    if estimated:
        out.append(line())
        out.append(line(fit([
            ("%s~ estimated rate" % GREY, True),
            (" for %s" % ", ".join(short_model(x) for x in estimated[:2]), False),
            ("%s" % RESET, True)])))
    draw_footer_id()


def tab_limits():
    if not limits:
        sect("RATE LIMITS")
        out.append(line("  %sno cached limit data%s" % (GREY, RESET)))
        out.append(line("  %s(Claude Code writes it;%s" % (GREY, RESET)))
        out.append(line("  %s we never fetch it)%s" % (GREY, RESET)))
        return
    age = "as of %s ago" % dur(LIM_AGE) if LIM_AGE is not None else "age unknown"
    sect("RATE LIMITS", age)
    now = time.time()
    rows = [("session", limits.get("session"))]
    rows += [("weekly", r) for r in (limits.get("weekly") or [])]
    drew = False
    for label, row in rows:
        if not row:
            continue
        drew = True
        col = level_color(acct.severity_rank(row))
        name = label if not row.get("scope") else "%s %s" % (label[:4], row["scope"])
        out.append(line(fit([
            ("  %s%-*s%s" % (BOLD, max(7, min(14, W - 12)),
                             name[:max(7, min(14, W - 12))], RESET), True),
            ("%s%4d%%%s" % (col, round(row["percent"]), RESET), True)])))
        if row.get("resets"):
            left = row["resets"] - now
            txt = "resets %s" % when(row["resets"], now)
            if left > 0:
                txt += "  in %s" % dur(left)
            out.append(line("    %s%s%s" % (GREY, clip(txt, max(0, W - 4)), RESET)))
    if not drew:
        out.append(line("  %sno active limits reported%s" % (GREY, RESET)))

    cred = limits.get("credits")
    out.append(line())
    if cred:
        col = RED if cred.get("capped") else YELLOW
        out.append(line(fit([
            ("  %scredits%s " % (BOLD, RESET), True),
            ("%s%d%%%s" % (col, round(cred["percent"] or 0), RESET), True),
            (" %s%s used%s" % (GREY, money(cred["used"]), RESET), False)])))
    else:
        out.append(line("  %scredits  off%s" % (GREY, RESET)))
    if ident and ident.get("plan"):
        out.append(line("  %splan     %s%s%s" % (GREY, YELLOW, ident["plan"], RESET)))
    out.append(line())
    out.append(line("%saccount-wide, all machines%s" % (GREY, RESET)))
    if LIM_STALE:
        out.append(line("%s~ cache is stale%s" % (YELLOW, RESET)))


def tab_models():
    sect("MODELS", "%d turns" % turns)
    if not models:
        out.append(line("  %sno priced usage yet%s" % (GREY, RESET)))
        return
    for n, m in models[:4]:
        name = short_model(n)
        col = model_color(name)
        mark = "" if pricing.is_exact(n) else "~"
        out.append(line(fit([
            ("%s%s%s%s" % (BOLD, col, name[:max(8, W - 16)], RESET), True),
            (" %s%s%s%s" % (mark, GREEN, money(m["cost"]), RESET), True),
            (" %s%d turns%s" % (GREY, m["turns"], RESET), False)])))
        out.append(line(fit([
            ("  %sin %s%-7s%s" % (GREY, WHITE, human(m["input"]), RESET), True),
            ("%sout %s%s%s" % (GREY, WHITE, human(m["output"]), RESET), False)])))
        out.append(line(fit([
            ("  %sread %s%-5s%s" % (GREY, WHITE, human(m["cache_read"]), RESET), True),
            ("%swrite %s%s%s" % (GREY, WHITE, human(m["cache_creation"]), RESET), False)])))
        if m["cache_1h"] or m["cache_5m"]:
            # Why a cache-heavy session costs what it does: a 1h write is 2x
            # input, a 5m write 1.25x. Collapsing them hides ~15%.
            out.append(line("    %s1h %s (2x) · 5m %s%s" % (
                GREY, human(m["cache_1h"]), human(m["cache_5m"]), RESET)))
        seen = m["cache_read"] + m["input"] + m["cache_creation"]
        if seen:
            hit = 100.0 * m["cache_read"] / seen
            per = m["cost"] / m["turns"] if m["turns"] else 0.0
            out.append(line(fit([
                ("  %scache %d%%%s" % (GREEN, round(hit), RESET), True),
                (" %s· %s/turn%s" % (GREY, money(per), RESET), False)])))
    if estimated:
        out.append(line())
        out.append(line("%s~ estimated rate%s" % (GREY, RESET)))


def tab_agents():
    agents = sess["agents"]
    if not agents:
        sect("AGENTS")
        out.append(line("  %snone in this session%s" % (GREY, RESET)))
        return
    share = (100.0 * sess["agent_cost"] / tot_cost) if tot_cost else 0.0
    sect("AGENTS", "%d · %d%%" % (len(agents), round(share)))
    aw = 10 if TIGHT else 14
    now = time.time()
    for ag in agents[:8]:
        named = [x for x in ag["models"] if pricing.is_priceable(x)]
        mods = "+".join(short_model(x).split("-")[0] for x in named[:2])
        out.append(line(fit([
            ("  %s%-*s%s" % (WHITE, aw, ag["name"][:aw], RESET), True),
            (" %s%7s%s" % (GREEN, money(ag["cost"]), RESET), True),
            (" %s%s%s" % (GREY, mods[:12], RESET), False),
            (" %s%s%s" % (GREY, dur(now - ag["last"]) if ag.get("last") else "", RESET), False)])))
    if len(agents) > 8:
        out.append(line("  %s+%d more%s" % (GREY, len(agents) - 8, RESET)))
    out.append(line())
    out.append(line(fit([
        ("  %syou%s %s%s%s" % (GREY, RESET, GREEN, money(sess["self_cost"]), RESET), True),
        ("  %sagents%s %s%s%s" % (GREY, RESET, GREEN, money(sess["agent_cost"]), RESET), False)])))


def tab_trend():
    try:
        hist = account.history()
    except Exception:
        hist = None
    if not hist:
        sect("TREND")
        out.append(line("  %sno history cache yet%s" % (GREY, RESET)))
        return
    sect("7 DAYS", "account-wide")
    days = hist["days"]
    peak = max((d["tokens"] for d in days), default=0) or 1
    for d in days:
        frac = d["tokens"] / float(peak)
        bar = BARS[min(len(BARS) - 1, int(frac * (len(BARS) - 1)))]
        label = d["date"][5:] if len(d["date"]) >= 10 else d["date"]
        out.append(line(fit([
            ("  %s%-5s%s " % (GREY, label, RESET), True),
            ("%s%s%s" % (BLUE, bar, RESET), True),
            (" %s%7s%s" % (WHITE, human(d["tokens"]), RESET), True),
            (" %s%s%s" % (GREY, short_model(d["models"][0]).split("-")[0]
                          if d["models"] else "", RESET), False)])))
    out.append(line())
    out.append(line(fit([
        ("  %s%d sessions%s" % (GREY, hist["sessions"], RESET), True),
        (" %s· %s msgs%s" % (GREY, human(hist["messages"]), RESET), False)])))
    if hist.get("since"):
        out.append(line("  %ssince %s%s" % (GREY, hist["since"], RESET)))
    if hist.get("stale"):
        # Rebuilt once a day by Claude Code, so today is usually missing.
        out.append(line("%s~ rebuilt daily; today may%s" % (GREY, RESET)))
        out.append(line("%s  be missing%s" % (GREY, RESET)))


def tab_account():
    # The SESSION half comes from the transcript, which we always have, so a
    # missing account cache must not take it down with it.
    sect("ACCOUNT")
    if not ident:
        out.append(line("  %shidden or unavailable%s" % (GREY, RESET)))
    if ident and ident.get("name"):
        out.append(line("  %s%s%s" % (WHITE, clip(ident["name"], max(0, W - 2)), RESET)))
    if ident and ident.get("email"):
        out.append(line("  %s%s%s" % (GREY, clip(ident["email"], max(0, W - 2)), RESET)))
    if ident and ident.get("org"):
        out.append(line("  %s%s%s" % (GREY, clip(ident["org"], max(0, W - 2)), RESET)))
    bits = [b for b in ((ident or {}).get("plan"), (ident or {}).get("billing"),
                        (ident or {}).get("role")) if b]
    if bits:
        out.append(line(fit([
            ("  %s%s%s" % (YELLOW, bits[0], RESET), True),
            (" %s· %s%s" % (GREY, " · ".join(bits[1:]), RESET), False)])))
    if ident and ident.get("since"):
        out.append(line("  %ssince %s%s" % (
            GREY, time.strftime("%d %b %Y", time.localtime(ident["since"])), RESET)))
    if ident and not ident.get("email"):
        out.append(line("  %sCBM_ACCOUNT=full for email%s" % (GREY, RESET)))

    sect("SESSION")
    if meta.get("title"):
        out.append(line("  %s%s%s" % (WHITE, clip(meta["title"], max(0, W - 2)), RESET)))
    out.append(line(fit([
        ("  %s%s%s" % (GREY, sid[:8], RESET), True),
        (" %s· %s%s" % (GREY, os.path.basename((meta.get("cwd") or "").rstrip("/")), RESET), False)])))
    br = meta.get("branch")
    if br and br != "HEAD":
        out.append(line("  %sbranch %s%s" % (GREY, br, RESET)))
    ef = [x for x in (meta.get("effort"), meta.get("pmode")) if x]
    if ef:
        out.append(line("  %s%s%s" % (GREY, " · ".join(ef), RESET)))
    if meta.get("version"):
        out.append(line("  %sclaude code %s%s" % (GREY, meta["version"], RESET)))
    if sess.get("started"):
        out.append(line("  %sup %s%s" % (GREY, dur(time.time() - sess["started"]), RESET)))


# ---- compose --------------------------------------------------------------
draw_header()
draw_tabs()
(tab_live, tab_limits, tab_models, tab_agents, tab_trend, tab_account)[TAB]()

out.append(line())
if SHOW_TABS and os.environ.get("CBM_HELP") == "1":
    out.append(line("%s<- ->  or Tab  switch tab%s" % (GREY, RESET)))
    out.append(line("%s1-6        jump to tab%s" % (GREY, RESET)))
    out.append(line("%sr          redraw now%s" % (GREY, RESET)))
    out.append(line("%sq          drop to a shell%s" % (GREY, RESET)))
    out.append(line())
if SHOW_TABS:
    out.append(line(fit([("%slive%s" % (GREY, ""), True),
                         (" · <> tabs", False),
                         (" · ? keys", False),
                         ("%s" % RESET, True)])))
else:
    out.append(line(fit([("%slive%s" % (GREY, ""), True),
                         (" · updates on change", False),
                         (" · Ctrl-C to stop", False),
                         ("%s" % RESET, True)])))
print("\n".join(out))

try:
    scan.save()
except Exception:
    pass
