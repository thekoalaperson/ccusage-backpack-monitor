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


def pick(width, *cands):
    """The first candidate that fits `width` columns, longest phrasing first.

    `fit` drops fields; this rewords one. A reset time can be "Fable · resets
    Wed 10:30 · 2d left" or "Wed 10:30" depending on the room available, and
    dropping the words would leave a bare clock with nothing saying what it is
    — which is exactly the reading this panel got wrong before.
    """
    for c in cands:
        if c and dw(c) <= width:
            return c
    return ""


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


def meter_row(label, frac, pct_text, details, col, out, indent=""):
    """One aligned gauge row: label, bar, percent, then a fitted detail.

    All three limits share this shape on purpose -- ctx, the 5h window and the
    weekly cap are the same question ("how full, and when does it reset"), so
    they should be one vertical scan rather than three different-looking rows.

    `details` is a list of phrasings for the tail, longest first.
    """
    # Below ~40 columns the bar is decoration and the sentence beside it is
    # not: a bare "Wed 10:30" with no "resets" is the exact misreading this
    # panel is being fixed for, so the meter yields the columns first.
    mw = 6 if W < 40 else (8 if NARROW else 10)
    spent = len(indent) + 4 + 1 + mw + 5 + 2
    detail = pick(max(0, W - spent), *details) if details else ""
    parts = [("%s%s%-4s%s " % (indent, BOLD, label[:4], RESET), True),
             ("%s%s%s" % (col, meter(frac, mw), RESET), True),
             ("%s%5s%s" % (col, pct_text, RESET), True)]
    if detail:                       # no empty tail, no trailing whitespace
        parts.append(("  %s%s%s" % (GREY, detail, RESET), False))
    out.append(line(fit(parts)))


def kv(key, val, out, col=None, kw=None, indent="  "):
    """A labelled value: dim key in a fixed column, bright value beside it.

    Every bare number on this panel used to require remembering what it was.
    Aligning the labels costs a few columns and removes that entirely.
    """
    # Never below 7: the keys here ("credits", "sessions") are words, and a
    # column that renders "credi" costs more clarity than it saves space.
    kw = kw if kw is not None else 7
    out.append(line(fit([
        ("%s%s%-*s%s " % (indent, GREY, kw, key[:kw], RESET), True),
        ("%s%s%s" % (col or WHITE, val, RESET), True)])))


def cells(pairs, out, indent="  "):
    """Small key/value cells, two per row when there's room, else one.

    The cell is sized to its contents, not to half the pane: splitting the
    width evenly pushes each value miles from its own label and turns a
    four-number block into something you have to trace with a finger.
    """
    kw = 12 if not NARROW else 11
    cw = kw + 10
    per = 2 if W >= len(indent) + 2 * cw else 1
    for i in range(0, len(pairs), per):
        s = indent
        for k, v in pairs[i:i + per]:
            cell = "%s%-*s%s%s%s" % (GREY, kw, k[:kw], WHITE, v, RESET)
            s += cell + " " * max(0, cw - dw(cell))
        out.append(line(s.rstrip()))


def bar_row(label, frac, value, tail, col, out, indent="  ", lw=6):
    """label · bar · value · optional tail, degrading bar-then-tail.

    Used wherever a share is being compared (models, agents, daily history), so
    the same shape means the same thing everywhere on the panel.
    """
    bw = max(6, min(20, W - (len(indent) + lw + 12)))
    parts = [("%s%s%-*s%s " % (indent, WHITE, lw, label[:lw], RESET), True),
             ("%s%s%s " % (col, meter(frac, bw), RESET), False),
             ("%s%7s%s" % (GREEN if "$" in str(value) else WHITE, value, RESET), True)]
    if tail:
        parts.append(("  %s%s%s" % (GREY, tail, RESET), False))
    out.append(line(fit(parts)))


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


def limit_phrasings(row, now, scope_as="", extra=0):
    """How to say "21%, resets Wed 10:30, scoped to Fable" at any width.

    The old panel emitted "Fable Wed 10:30", which reads as one unidentifiable
    blob: no verb, no separator, and nothing saying the clock is a reset. Every
    phrasing here keeps the word `resets` for as long as the room lasts.
    """
    scope = scope_as or (row.get("scope") or "")
    at = when(row.get("resets"), now)
    left = (row.get("resets") or 0) - now
    left_s = dur(left) if (row.get("resets") and left > 0) else ""
    base = "resets %s" % at if at else ""
    long = base + (" · %s left" % left_s if base and left_s else "")
    plus = " · +%d more" % extra if extra > 0 else ""
    cands = []
    if scope and base:
        cands += [scope + " · " + long + plus, scope + " · " + long,
                  scope + " · " + base]
    elif scope:
        cands += [scope]
    if base:
        cands += [long + plus, long, base, at]
    return [c for c in cands if c] or [scope or ""]


# ---- pinned header --------------------------------------------------------
def draw_header():
    if agent_of:
        out.append(line(fit([
            ("%sagent %s" % (YELLOW, own.get("agent") or "?"), True),
            ("%s of session %s%s" % (GREY, agent_of, RESET), False),
            ("%s" % RESET, True)])))
    # `session` is the most important word on the panel: without it the top
    # line is a dollar figure with no stated scope, and readers assumed it was
    # the 5h window or the whole account.
    head = [("%ssession%s " % (GREY, RESET), True),
            ("%s%s%s%s" % (BOLD, GREEN, money(tot_cost), RESET), True),
            ("  %s%s tok%s" % (WHITE, human(tot_tok), RESET), False),
            ("  %s%d turns%s" % (GREY, turns, RESET), False)]
    if sess.get("started"):
        head.append(("  %sup %s%s" % (GREY, dur(time.time() - sess["started"]),
                                      RESET), False))
    out.append(line(fit(head)))
    if sess["agents"]:
        n = len(sess["agents"])
        noun = "ag" if TIGHT else ("agent" if n == 1 else "agents")
        out.append(line(fit([
            ("%s  you %s%s%s" % (GREY, GREEN, money(sess["self_cost"]), RESET), True),
            ("  %s%d %s %s%s%s" % (GREY, n, noun, GREEN,
                                   money(sess["agent_cost"]), RESET), False)])))

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
                      ["%s of %s window" % (human(ctok), human(cwin)),
                       "%s of %s" % (human(ctok), human(cwin)),
                       "%s/%s" % (human(ctok), human(cwin))],
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
        now = time.time()
        extra = len(limits.get("weekly") or []) - 1
        for label, row in (("5h", limits.get("session")),
                           ("week", (limits.get("weekly") or [None])[0])):
            if not row:
                continue
            if not rows:
                out.append(line())
            rows += 1
            pct = row["percent"]
            meter_row(label, pct / 100.0, "%s%d%%" % (mark, round(pct)),
                      limit_phrasings(row, now,
                                      extra=extra if label == "week" else 0),
                      level_color(acct.severity_rank(row)), out)
    return rows


# ---- tab strip ------------------------------------------------------------
def draw_tabs():
    if not SHOW_TABS:
        return
    out.append(line())
    # Numbered, because `1`-`6` is the fastest way to move and nothing else on
    # screen advertises it. The number and the name are one unit, so the active
    # highlight covers both.
    for idx, sep in ((1, "  "), (1, " "), (2, " ")):
        seg = []
        for i, t in enumerate(TABS):
            name = "%d %s" % (i + 1, t[idx])
            if i == TAB:
                seg.append("[%s]" % name if NO_COLOR
                           else "%s %s %s" % (REV, name, RESET))
            else:
                seg.append("%s%s%s" % (GREY, name, RESET))
        s = sep.join(seg)
        if dw(s) <= W:
            out.append(line(s))
            return
    # Too narrow for any strip: say where you are, not what else exists.
    out.append(line("%s%d/%d%s %s%s%s" % (
        GREY, TAB + 1, len(TABS), RESET, BOLD, TABS[TAB][1], RESET)))


# ---- tab bodies -----------------------------------------------------------
def sect(title, *notes):
    """Section heading, with a note that is reworded rather than truncated.

    A heading clipped mid-word ("5h rolling block · all sessi") is worse than
    no note at all, so the shortest candidate is always the empty string.
    """
    out.append(line())
    s = "%s%s%s" % (BOLD, title, RESET)
    note = pick(max(0, W - dw(title) - 2), *(list(notes) + [""])) if notes else ""
    if note:
        s += "  %s%s%s" % (GREY, note, RESET)
    out.append(line(s))


def draw_models_brief():
    sect("MODELS", "share of session spend", "share of spend", "by spend")
    if not models:
        out.append(line("  %sno priced usage yet%s" % (GREY, RESET)))
    for n, m in models[:6]:
        name = short_model(n)
        mtok = m["input"] + m["output"] + m["cache_read"] + m["cache_creation"]
        share = (m["cost"] / tot_cost) if tot_cost else 0
        mark = "~" if not pricing.is_exact(n) else ""
        bar_row(name, share, mark + money(m["cost"]),
                "%s tok" % human(mtok), model_color(name), out,
                lw=max(6, min(12, W - 24)))
    # Cache hit rate is the single biggest lever on what a session costs, and
    # it's already summed -- so it rides along on a line that exists anyway.
    reads = sum(m["cache_read"] for _n, m in models)
    fresh = sum(m["input"] + m["cache_creation"] for _n, m in models)
    if reads + fresh:
        hit = 100.0 * reads / (reads + fresh)
        per = (tot_cost / turns) if turns else 0.0
        out.append(line(fit([
            ("  %scache hits %s%d%%%s" % (GREY, GREEN, round(hit), RESET), True),
            (" %s· %s per turn%s" % (GREY, money(per), RESET), False)])))


def draw_agents_brief():
    if not (SHOW_AGENTS and sess["agents"]):
        return
    top = sess["agents"][:3]
    share = (100.0 * sess["agent_cost"] / tot_cost) if tot_cost else 0.0
    note = ("top %d of %d · %d%% of spend"
            % (len(top), len(sess["agents"]), round(share))
            if len(sess["agents"]) > len(top)
            else "%d · %d%% of spend" % (len(top), round(share)))
    sect("AGENTS", note, "%d%% of spend" % round(share))
    for ag in top:
        named = [x for x in ag["models"] if pricing.is_priceable(x)]
        mods = "+".join(short_model(x).split("-")[0] for x in named[:2])
        share = (ag["cost"] / tot_cost) if tot_cost else 0
        bar_row(ag["name"], share, money(ag["cost"]), mods[:12], CYAN, out,
                lw=max(6, min(12, W - 24)))


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
    # Previously "$24.2/hr  ~$121  ends 15:30": a rate, an unexplained dollar
    # figure and an unexplained clock. Each number now says what it is.
    sect("BURN", "5h rolling block · all sessions", "5h rolling block",
         "5h block")
    kw = 8 if not TIGHT else 5
    keys = ("rate", "so far", "on track", "window") if not TIGHT \
        else ("rate", "spent", "eta", "ends")
    kv(keys[0], "%s$%.1f/hr" % ("" if NARROW else "🔥 ", blk["rate"]),
       out, col=col, kw=kw)
    kv(keys[1], money(blk["cost"]), out, col=GREEN, kw=kw)
    kv(keys[2], pick(max(0, W - kw - 4),
                     "~%s by end of block" % money(blk["projected"]),
                     "~%s this block" % money(blk["projected"]),
                     "~%s" % money(blk["projected"])), out, col=YELLOW, kw=kw)
    left = blk["end"] - time.time()
    kv(keys[3], pick(max(0, W - kw - 4),
                     "ends %s · %s left" % (hm(blk["end"]), dur(left)),
                     "ends %s" % hm(blk["end"]),
                     hm(blk["end"])), out, kw=kw)


def draw_graph():
    if not (SHOW_GRAPH and sess["outputs"]):
        return
    width = max(8, W - 2)
    outs = sess["outputs"]
    shown = outs[-width:]
    sect("ACTIVITY", "output tokens per turn", "output per turn", "out/turn")
    out.append(line("%s%s%s" % (BLUE, spark(outs, width), RESET)))
    # A sparkline with no scale is decoration. Three numbers make it a chart.
    if shown and not TIGHT:
        avg = sum(shown) / float(len(shown))
        out.append(line(fit([
            ("  %slast %d turns%s" % (GREY, len(shown), RESET), True),
            ("  %speak %s%s" % (GREY, human(max(shown)), RESET), False),
            ("  %savg %s%s" % (GREY, human(avg), RESET), False)])))


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
    # Rate before totals: the header already carries the totals, so what this
    # tab adds is the direction things are moving in.
    draw_burn()
    draw_models_brief()
    draw_agents_brief()
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
    rows = [("5h", limits.get("session"), "")]
    # An unscoped weekly covers everything, which is worth saying out loud when
    # a scoped one sits right beside it claiming a different number.
    rows += [("week", r, r.get("scope") or "all models")
             for r in (limits.get("weekly") or [])]
    drew = False
    for label, row, scope in rows:
        if not row:
            continue
        drew = True
        meter_row(label, row["percent"] / 100.0, "%d%%" % round(row["percent"]),
                  limit_phrasings(row, now, scope_as=scope),
                  level_color(acct.severity_rank(row)), out)
    if not drew:
        out.append(line("  %sno active limits reported%s" % (GREY, RESET)))

    out.append(line())
    cred = limits.get("credits")
    if cred:
        col = RED if cred.get("capped") else YELLOW
        kv("credits", "%d%% · %s used" % (round(cred["percent"] or 0),
                                          money(cred["used"])), out, col=col)
    else:
        kv("credits", "off", out, col=GREY)
    if ident and ident.get("plan"):
        kv("plan", ident["plan"], out, col=YELLOW)

    out.append(line())
    # The panel's own dollar figures are computed from token counts, which is
    # not what a subscription actually meters. Saying so here is the difference
    # between "$25 spent" reading as a bill and reading as a workload estimate.
    for txt in (pick(W, "Account-wide: every machine, every session.",
                     "Account-wide: every machine,",
                     "account-wide"),
                pick(W, "Claude Code caches these; we never fetch them.",
                     "Claude Code caches these.", ""),
                "",
                pick(W, "On a plan the % is what stops you --",
                     "the % is what stops you --", "the % stops you;"),
                pick(W, "the $ above is a workload estimate.",
                     "$ is an estimate.")):
        out.append(line("%s%s%s" % (GREY, txt, RESET)) if txt else line())
    if LIM_STALE:
        out.append(line("%s%s%s" % (YELLOW, pick(
            W, "~ cached figure, may have moved", "~ cached, may have moved",
            "~ may have moved", "~ stale"), RESET)))


def tab_models():
    sect("MODELS", "%d turns · %s" % (turns, money(tot_cost)),
         "%d turns" % turns)
    if not models:
        out.append(line("  %sno priced usage yet%s" % (GREY, RESET)))
        return
    for n, m in models[:4]:
        name = short_model(n)
        col = model_color(name)
        mark = "" if pricing.is_exact(n) else "~"
        share = (100.0 * m["cost"] / tot_cost) if tot_cost else 0.0
        out.append(line())
        out.append(line(fit([
            ("%s%s%s%s" % (BOLD, col, name[:max(8, W - 22)], RESET), True),
            ("  %s%s%s%s" % (mark, GREEN, money(m["cost"]), RESET), True),
            (" %s· %d%% of session%s" % (GREY, round(share), RESET), False),
            (" %s· %d turns%s" % (GREY, m["turns"], RESET), False)])))
        cells([("input", human(m["input"])), ("output", human(m["output"])),
               ("cache read", human(m["cache_read"])),
               ("cache write", human(m["cache_creation"]))], out)
        if m["cache_1h"] or m["cache_5m"]:
            # Why a cache-heavy session costs what it does: a 1h write is 2x
            # input, a 5m write 1.25x. Collapsing them hides ~15%.
            out.append(line("  %s  of which 1h %s (2x) · 5m %s%s" % (
                GREY, human(m["cache_1h"]), human(m["cache_5m"]), RESET)))
        seen = m["cache_read"] + m["input"] + m["cache_creation"]
        if seen:
            hit = 100.0 * m["cache_read"] / seen
            per = m["cost"] / m["turns"] if m["turns"] else 0.0
            out.append(line(fit([
                ("  %scache hits %s%d%%%s" % (GREY, GREEN, round(hit), RESET), True),
                (" %s· %s per turn%s" % (GREY, money(per), RESET), False)])))
    if estimated:
        out.append(line())
        out.append(line("%s~ estimated rate, not a published one%s" % (GREY, RESET)))


def tab_agents():
    agents = sess["agents"]
    if not agents:
        sect("AGENTS")
        out.append(line("  %snone in this session%s" % (GREY, RESET)))
        out.append(line("  %sTask agents and teammates would%s" % (GREY, RESET)))
        out.append(line("  %sbe listed here with their spend.%s" % (GREY, RESET)))
        return
    share = (100.0 * sess["agent_cost"] / tot_cost) if tot_cost else 0.0
    sect("AGENTS", "%d · %d%% of session spend" % (len(agents), round(share)),
         "%d · %d%% of spend" % (len(agents), round(share)),
         "%d agents" % len(agents))
    lw = max(6, min(16, W - 26))
    now = time.time()
    for ag in agents[:8]:
        named = [x for x in ag["models"] if pricing.is_priceable(x)]
        mods = "+".join(short_model(x).split("-")[0] for x in named[:2])
        frac = (ag["cost"] / tot_cost) if tot_cost else 0
        # "8s" alone needed a column header to mean anything; "8s ago" does not.
        last = "%s ago" % dur(now - ag["last"]) if ag.get("last") else ""
        tail = " · ".join(x for x in (mods[:12], last) if x)
        bar_row(ag["name"], frac, money(ag["cost"]), tail, CYAN, out, lw=lw)
    if len(agents) > 8:
        out.append(line("  %s+%d more%s" % (GREY, len(agents) - 8, RESET)))
    out.append(line())
    # Who actually spent the money: the split is the reason this tab exists.
    whole = tot_cost or 1.0
    bar_row("you", sess["self_cost"] / whole, money(sess["self_cost"]),
            "%d%%" % round(100.0 * sess["self_cost"] / whole), GREEN, out, lw=lw)
    bar_row("agents", sess["agent_cost"] / whole, money(sess["agent_cost"]),
            "%d%%" % round(share), CYAN, out, lw=lw)


def tab_trend():
    try:
        hist = account.history()
    except Exception:
        hist = None
    if not hist:
        sect("TREND")
        out.append(line("  %sno history cache yet%s" % (GREY, RESET)))
        return
    sect("7 DAYS", "tokens · account-wide", "account-wide", "tokens")
    days = hist["days"]
    peak = max((d["tokens"] for d in days), default=0) or 1
    today = time.strftime("%Y-%m-%d", time.localtime())
    yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    for d in days:
        # A named row beats a date: "today" is the one people look for first.
        if d["date"] == today:
            label = "today"
        elif d["date"] == yday:
            label = "yest"
        else:
            label = d["date"][5:] if len(d["date"]) >= 10 else d["date"]
        bar_row(label, d["tokens"] / float(peak), human(d["tokens"]),
                short_model(d["models"][0]).split("-")[0] if d["models"] else "",
                BLUE, out, lw=5)
    # These are lifetime totals, not the seven days above them -- under a
    # "7 DAYS" heading they read as the week's, which is wrong by two orders
    # of magnitude. Their own heading is the cheapest possible fix.
    sect("ALL TIME")
    kv("sessions", "%d" % hist["sessions"], out, kw=8)
    kv("messages", human(hist["messages"]), out, kw=8)
    if hist.get("since"):
        kv("since", hist["since"], out, kw=8)
    if hist.get("stale"):
        # Rebuilt once a day by Claude Code, so today is usually missing.
        out.append(line())
        one = pick(W, "~ Claude Code rebuilds this once a day, "
                      "so today may be missing")
        if one:
            out.append(line("%s%s%s" % (GREY, one, RESET)))
        else:
            out.append(line("%s~ rebuilt once a day, so%s" % (GREY, RESET)))
            out.append(line("%s  today may be missing%s" % (GREY, RESET)))


def tab_account():
    # The SESSION half comes from the transcript, which we always have, so a
    # missing account cache must not take it down with it.
    sect("ACCOUNT")
    if not ident:
        out.append(line("  %shidden or unavailable%s" % (GREY, RESET)))
    if ident:
        if ident.get("name"):
            kv("name", clip(ident["name"], max(0, W - 11)), out, kw=8)
        if ident.get("email"):
            kv("email", clip(ident["email"], max(0, W - 11)), out, kw=8, col=GREY)
        if ident.get("org"):
            kv("org", clip(ident["org"], max(0, W - 11)), out, kw=8, col=GREY)
        if ident.get("plan"):
            kv("plan", ident["plan"], out, kw=8, col=YELLOW)
        if ident.get("billing"):
            kv("billing", ident["billing"], out, kw=8, col=GREY)
        if ident.get("role"):
            kv("role", ident["role"], out, kw=8, col=GREY)
        if ident.get("since"):
            kv("since", time.strftime("%d %b %Y", time.localtime(ident["since"])),
               out, kw=8, col=GREY)
        if not ident.get("email"):
            out.append(line("  %sCBM_ACCOUNT=full adds email%s" % (GREY, RESET)))

    sect("SESSION")
    if meta.get("title"):
        out.append(line("  %s%s%s" % (WHITE, clip(meta["title"], max(0, W - 2)), RESET)))
        out.append(line())
    kv("id", sid[:8], out, kw=8, col=GREY)
    proj = os.path.basename((meta.get("cwd") or "").rstrip("/"))
    if proj:
        kv("folder", proj, out, kw=8, col=GREY)
    br = meta.get("branch")
    if br and br != "HEAD":
        kv("branch", br, out, kw=8, col=GREY)
    ef = [x for x in (meta.get("effort"), meta.get("pmode")) if x]
    if ef:
        kv("mode", " · ".join(ef), out, kw=8, col=GREY)
    if meta.get("version"):
        kv("client", "claude code %s" % meta["version"], out, kw=8, col=GREY)
    if sess.get("started"):
        kv("started", pick(max(0, W - 12),
                           "%s · up %s" % (hm(sess["started"]),
                                           dur(time.time() - sess["started"])),
                           dur(time.time() - sess["started"])), out, kw=8, col=GREY)


# ---- compose --------------------------------------------------------------
draw_header()
draw_tabs()
(tab_live, tab_limits, tab_models, tab_agents, tab_trend, tab_account)[TAB]()

out.append(line())
if SHOW_TABS and os.environ.get("CBM_HELP") == "1":
    out.append(line("%sKEYS%s" % (BOLD, RESET)))
    for k, what in (("<- ->", "previous / next tab"),
                    ("Tab", "next tab"),
                    ("1-6", "jump to tab"),
                    ("r", "redraw now"),
                    ("?", "this list"),
                    ("q", "drop to a shell"),
                    ("Ctrl-C", "drop to a shell")):
        out.append(line("  %s%-7s%s%s%s" % (WHITE, k, GREY, what, RESET)))
    out.append(line())
    out.append(line("%s~ marks a figure we can't vouch%s" % (GREY, RESET)))
    out.append(line("%s  for as current or exact%s" % (GREY, RESET)))
    out.append(line())
if SHOW_TABS:
    out.append(line(fit([("%supdates live%s" % (GREY, ""), True),
                         (" · arrows or 1-6", False),
                         (" · ? keys", False),
                         (" · q shell", False),
                         ("%s" % RESET, True)])))
else:
    out.append(line(fit([("%supdates live%s" % (GREY, ""), True),
                         (" · on change", False),
                         (" · Ctrl-C to stop", False),
                         ("%s" % RESET, True)])))
print("\n".join(out))

try:
    scan.save()
except Exception:
    pass
