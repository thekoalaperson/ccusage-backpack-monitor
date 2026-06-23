#!/usr/bin/env python3
"""Rich, colored, one-screen usage panel for a single Claude Code session.

Usage: render.py <session-id> [transcript-path]

Stays cheap (the watcher only calls this when the transcript changes):
  - `ccusage session --json`  : ONE call -> totals + true per-model breakdown
  - `ccusage blocks --active` : account-wide 5h burn rate -> CACHED with a TTL
  - sparkline                 : reads only the TAIL of the transcript

Env knobs:
  CBM_CCUSAGE      how to invoke ccusage (may include spaces)
  CBM_BLOCKS       "0" hides the 5h burn-rate section (skips that call)
  CBM_BLOCKS_TTL   seconds to cache the blocks call (default 30)
  CBM_GRAPH        "0" hides the sparkline
  CBM_BG           256-color index for an opaque background card (e.g. 234);
                   unset = transparent-friendly (no background fill)
Exit 0 always; prints a "waiting" line if data isn't ready yet.
"""
import sys, os, re, json, subprocess, time, tempfile, shutil

sid = sys.argv[1] if len(sys.argv) > 1 else ""
transcript = sys.argv[2] if len(sys.argv) > 2 else ""
CCU = os.environ.get("CBM_CCUSAGE", "ccusage")
SHOW_BLOCKS = os.environ.get("CBM_BLOCKS", "1") != "0"
BLOCKS_TTL = float(os.environ.get("CBM_BLOCKS_TTL", "30"))
SHOW_GRAPH = os.environ.get("CBM_GRAPH", "1") != "0"
BG = os.environ.get("CBM_BG", "").strip()
TAIL_BYTES = 262144

W = max(40, min(shutil.get_terminal_size((48, 24)).columns, 64))

# ---- color (solid attributes; no DIM, which washes out on transparency) ---
def a(code): return f"\033[{code}m"
RESET, BOLD = a(0), a(1)
GREEN, YELLOW, RED, CYAN, MAG, BLUE, WHITE, GREY = (
    a(32), a(33), a(31), a(36), a(35), a(94), a(97), a(90))

def model_color(name):
    if "opus" in name: return MAG
    if "sonnet" in name: return CYAN
    if "haiku" in name: return GREEN
    return WHITE

ANSI = re.compile(r"\033\[[0-9;]*m")
def vlen(s): return len(ANSI.sub("", s))

def line(s=""):
    if BG:
        s = s + " " * max(0, W - vlen(s))
        return f"\033[48;5;{BG}m{s}{RESET}"
    return s

def run_json(args):
    try:
        p = subprocess.run(CCU.split() + args, capture_output=True, text=True, timeout=40)
        return json.loads(p.stdout) if p.stdout.strip() else None
    except Exception:
        return None

def cached_blocks(ttl):
    cache = os.path.join(tempfile.gettempdir(), "cbm-blocks-active.json")
    try:
        if os.path.exists(cache) and (time.time() - os.path.getmtime(cache)) < ttl:
            return json.load(open(cache))
    except Exception:
        pass
    data = run_json(["blocks", "--active", "--json", "--offline"])
    if data is not None:
        try: json.dump(data, open(cache, "w"))
        except Exception: pass
    return data

def tail_lines(path, max_bytes=TAIL_BYTES):
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            if sz > max_bytes: f.seek(sz - max_bytes)
            return f.read().decode("utf-8", "ignore").splitlines()
    except Exception:
        return []

def human(n):
    n = float(n or 0)
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if n >= div: return f"{n/div:.1f}{unit}"
    return str(int(n))

def short_model(m):
    return (m or "?").replace("claude-", "").replace("-20251001", "")

def hm(iso):
    try: return iso[11:16]
    except Exception: return "?"

BARS = "▁▂▃▄▅▆▇█"
def spark(vals, width=W-4):
    vals = vals[-width:]
    if not vals: return ""
    lo, hi = min(vals), max(vals)
    if hi == lo: return BARS[3] * len(vals)
    return "".join(BARS[int((v - lo) / (hi - lo) * (len(BARS) - 1))] for v in vals)

def burn_color(cph):
    if cph is None: return GREY
    if cph < 3: return GREEN
    if cph < 10: return YELLOW
    return RED

# ---- find this session in the one unfiltered call --------------------------
data = run_json(["session", "--json", "--offline"])
sess = None
for s in (data or {}).get("session", []):
    if s.get("period") == sid:
        sess = s
        break
if not sess:
    print(line(f"{GREY}waiting for session {sid[:8]} data...{RESET}"))
    sys.exit(0)

tot_cost = sess.get("totalCost", 0)
tot_tok = sess.get("totalTokens", 0)
breakdowns = sorted(sess.get("modelBreakdowns", []),
                    key=lambda b: b.get("cost", 0), reverse=True)

# ---- render ---------------------------------------------------------------
out = []
out.append(line(f"{BOLD}{CYAN}ccusage{RESET}  {WHITE}{sid[:8]}{RESET}"))
out.append(line(f"{BOLD}{GREEN}${tot_cost:,.2f}{RESET}  {BOLD}{human(tot_tok)} tokens{RESET}"))
out.append(line())

out.append(line(f"{BOLD}MODELS{RESET}"))
for b in breakdowns:
    name = short_model(b.get("modelName"))
    col = model_color(name)
    cost = b.get("cost", 0)
    mtok = (b.get("inputTokens", 0) + b.get("outputTokens", 0)
            + b.get("cacheReadTokens", 0) + b.get("cacheCreationTokens", 0))
    share = (cost / tot_cost) if tot_cost else 0
    barlen = 8
    fill = int(round(share * barlen))
    bar = "█" * fill + "·" * (barlen - fill)
    out.append(line(f"  {col}●{RESET} {col}{name:<11}{RESET} {GREEN}${cost:>5.2f}{RESET} "
                    f"{col}{bar}{RESET} {WHITE}{human(mtok):>5}{RESET}"))

if SHOW_BLOCKS:
    b = ((cached_blocks(BLOCKS_TTL) or {}).get("blocks") or [None])[0]
    if b:
        br = (b.get("burnRate") or {}).get("costPerHour")
        col = burn_color(br)
        rate = f"{col}🔥 ${br:,.1f}/hr{RESET}" if br is not None else ""
        proj = (b.get("projection") or {}).get("totalCost")
        ptxt = f"  {WHITE}~${proj:,.0f}{RESET}" if proj is not None else ""
        out.append(line())
        out.append(line(f"{BOLD}5h{RESET} {YELLOW}${b.get('costUSD',0):,.2f}{RESET}  {rate}"
                        f"  {GREY}ends {hm(b.get('endTime',''))}{RESET}{ptxt}"))

if SHOW_GRAPH and transcript and os.path.exists(transcript):
    outs = []
    for ln in tail_lines(transcript):
        try: o = json.loads(ln)
        except Exception: continue
        msg = o.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if u and o.get("type") == "assistant":
            outs.append(u.get("output_tokens", 0))
    if outs:
        out.append(line())
        out.append(line(f"{BOLD}out/turn{RESET} {GREY}(last {min(len(outs), W-4)}){RESET}"))
        out.append(line(f"{BLUE}{spark(outs)}{RESET}"))

out.append(line())
out.append(line(f"{GREY}live · updates on change · Ctrl-C to stop{RESET}"))
print("\n".join(out))
