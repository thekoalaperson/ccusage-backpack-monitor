#!/usr/bin/env python3
"""Rich, colored, one-screen usage panel for a single Claude Code session.

Usage: render.py <session-id> [transcript-path]

Designed to stay cheap: the watcher only calls this when the transcript actually
changes, and within a render we keep the work bounded:
  - `ccusage session -i <id>`  : the one per-change call (this session's cost)
  - `ccusage blocks --active`  : account-wide 5h burn rate -> CACHED with a TTL
                                 so frequent turns don't re-trigger the big scan
  - sparkline                  : reads only the TAIL of the transcript, so cost
                                 does not grow with session length

Env knobs:
  CBM_CCUSAGE      how to invoke ccusage (may include spaces)
  CBM_BLOCKS       "0" hides the 5h burn-rate section (skips that call entirely)
  CBM_BLOCKS_TTL   seconds to cache the blocks call (default 30)
  CBM_GRAPH        "0" hides the sparkline
Exit 0 always; prints a "waiting" line if data isn't available yet.
"""
import sys, os, json, subprocess, time, tempfile

sid = sys.argv[1] if len(sys.argv) > 1 else ""
transcript = sys.argv[2] if len(sys.argv) > 2 else ""
CCU = os.environ.get("CBM_CCUSAGE", "ccusage")
SHOW_BLOCKS = os.environ.get("CBM_BLOCKS", "1") != "0"
BLOCKS_TTL = float(os.environ.get("CBM_BLOCKS_TTL", "30"))
SHOW_GRAPH = os.environ.get("CBM_GRAPH", "1") != "0"
TAIL_BYTES = 262144  # 256 KB is plenty for the last few dozen turns

def a(code): return f"\033[{code}m"
RESET, DIM, BOLD = a(0), a(2), a(1)
GREEN, YELLOW, RED, CYAN, MAG, GREY, BLUE = (a(32), a(33), a(31), a(36), a(35), a(90), a(34))

def run_json(args):
    try:
        p = subprocess.run(CCU.split() + args, capture_output=True, text=True, timeout=40)
        return json.loads(p.stdout) if p.stdout.strip() else None
    except Exception:
        return None

def cached_blocks(ttl):
    """Account-wide; one cache shared across all sessions is correct here."""
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
            if sz > max_bytes:
                f.seek(sz - max_bytes)
            return f.read().decode("utf-8", "ignore").splitlines()
    except Exception:
        return []

def human(n):
    n = float(n or 0)
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n/div:.1f}{unit}"
    return str(int(n))

def hm(iso):
    try: return iso[11:16]
    except Exception: return "?"

BARS = "▁▂▃▄▅▆▇█"
def spark(vals, width=34):
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

# ---- gather (the one mandatory per-change call) ---------------------------
sess = run_json(["session", "-i", sid, "--json", "--offline"])
entries = (sess or {}).get("entries") or []
if not sess or not entries:
    print(f"{GREY}waiting for session {sid[:8]} data...{RESET}")
    sys.exit(0)

tot_cost = sess.get("totalCost", 0)
tot_tok = sess.get("totalTokens", 0)
tin = sum(e.get("inputTokens", 0) for e in entries)
tout = sum(e.get("outputTokens", 0) for e in entries)
tcache = sum(e.get("cacheReadTokens", 0) + e.get("cacheCreationTokens", 0) for e in entries)
models = []
for e in entries:
    m = (e.get("model") or "").replace("claude-", "").replace("-20251001", "")
    if m and m not in models: models.append(m)
model_label = (models[0] if models else "?") + (f" +{len(models)-1}" if len(models) > 1 else "")

# ---- render ---------------------------------------------------------------
L = [f"{BOLD}{CYAN}ccusage{RESET}{DIM}  session {sid[:8]}{RESET}   {MAG}● {model_label}{RESET}",
     "",
     f"  {DIM}COST{RESET}  {BOLD}{GREEN}${tot_cost:,.2f}{RESET}     {DIM}TOKENS{RESET}  {BOLD}{human(tot_tok)}{RESET}",
     f"  {DIM}in {human(tin)} · out {human(tout)} · cache {human(tcache)}{RESET}"]

if SHOW_BLOCKS:
    b = ((cached_blocks(BLOCKS_TTL) or {}).get("blocks") or [None])[0]
    if b:
        br = (b.get("burnRate") or {}).get("costPerHour")
        col = burn_color(br)
        rate = f"{col}🔥 ${br:,.2f}/hr{RESET}" if br is not None else ""
        L += ["", f"  {DIM}5h BLOCK{RESET}  {YELLOW}${b.get('costUSD',0):,.2f}{RESET}   {rate}   {DIM}ends {hm(b.get('endTime',''))}{RESET}"]
        proj = (b.get("projection") or {}).get("totalCost")
        if proj is not None:
            L.append(f"  {DIM}projected ~${proj:,.0f} this 5h window{RESET}")

if SHOW_GRAPH and transcript and os.path.exists(transcript):
    outs = []
    for line in tail_lines(transcript):
        try: o = json.loads(line)
        except Exception: continue
        msg = o.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if u and o.get("type") == "assistant":
            outs.append(u.get("output_tokens", 0))
    if outs:
        L += ["", f"  {DIM}out tokens / turn  (last {min(len(outs),34)}){RESET}",
              f"  {BLUE}{spark(outs)}{RESET}"]

L += ["", f"  {GREY}live · updates on change · Ctrl-C to stop{RESET}"]
print("\n".join(L))
