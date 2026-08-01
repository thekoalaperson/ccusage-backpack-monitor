"""Model pricing for ccusage-backpack-monitor.

Why this exists: `ccusage --offline` prices from a snapshot baked in at release
time, so any model newer than the installed ccusage silently costs $0.00. That
is how Opus 5 and Sonnet 5 ended up showing $0 next to millions of tokens.

Resolution order (first hit wins):
  1. user overrides   ~/.config/ccusage-backpack-monitor/pricing.json
  2. refreshed cache  LiteLLM snapshot, refetched at most once a day
  3. bundled table    below — correct at release, no network needed
  4. family fallback  infer from the model name (opus/sonnet/haiku/fable)

Rates are USD per million tokens: (input, output).
Cache rates follow Anthropic's fixed ratios, so a new model only ever needs
those two numbers:
    cache read      = 0.10 x input
    cache write 5m  = 1.25 x input
    cache write 1h  = 2.00 x input
An explicit rate from the refreshed snapshot always wins over the ratio.
"""
import json, os, time

# --- bundled rates: USD per 1M tokens, (input, output) ---------------------
BUNDLED = {
    "claude-opus-5":     (5.0,  25.0),
    "claude-opus-4-8":   (5.0,  25.0),
    "claude-opus-4-7":   (5.0,  25.0),
    "claude-opus-4-6":   (5.0,  25.0),
    "claude-opus-4-5":   (5.0,  25.0),
    "claude-opus-4-1":   (15.0, 75.0),
    "claude-opus-4":     (15.0, 75.0),
    "claude-fable-5":    (10.0, 50.0),
    "claude-mythos-5":   (10.0, 50.0),
    "claude-sonnet-5":   (2.0,  10.0),   # intro rate through 2026-08-31 ($3/$15 after)
    "claude-sonnet-4-6": (3.0,  15.0),
    "claude-sonnet-4-5": (3.0,  15.0),
    "claude-sonnet-4":   (3.0,  15.0),
    "claude-haiku-4-5":  (1.0,   5.0),
    "claude-3-5-haiku":  (0.8,   4.0),
    "claude-3-haiku":    (0.25,  1.25),
}

# Context window (max input tokens) per model, for the usage gauge. Same
# resolution order as rates; the refreshed snapshot carries `max_input_tokens`.
CONTEXT = {
    "claude-opus-5":     1000000,
    "claude-opus-4-8":   1000000,
    "claude-opus-4-7":   1000000,
    "claude-opus-4-6":   1000000,
    "claude-opus-4-5":    200000,
    "claude-opus-4-1":    200000,
    "claude-opus-4":      200000,
    "claude-fable-5":    1000000,
    "claude-mythos-5":   1000000,
    "claude-sonnet-5":   1000000,
    "claude-sonnet-4-6": 1000000,
    "claude-sonnet-4-5":  200000,
    "claude-sonnet-4":    200000,
    "claude-haiku-4-5":   200000,
    "claude-3-5-haiku":   200000,
    "claude-3-haiku":     200000,
}
DEFAULT_CONTEXT = 200000

# Family fallback for a model we have never heard of. Better than $0.00: the
# panel marks these estimated so the number is never silently trusted.
FAMILY = (
    ("fable",  (10.0, 50.0)),
    ("mythos", (10.0, 50.0)),
    ("opus",   (5.0,  25.0)),
    ("sonnet", (3.0,  15.0)),
    ("haiku",  (1.0,   5.0)),
)

LITELLM_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
               "model_prices_and_context_window.json")
REFRESH_TTL = 86400  # once a day is plenty; pricing changes rarely


def _state_dir():
    d = os.path.join(os.environ.get("XDG_STATE_HOME",
                                    os.path.expanduser("~/.local/state")),
                     "ccusage-backpack-monitor")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _config_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                       os.path.expanduser("~/.config")),
                        "ccusage-backpack-monitor")


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _refresh_if_stale(path):
    """Refetch the LiteLLM snapshot at most once a day.

    Never raises and never blocks for long: a slow or absent network just
    leaves the bundled table in charge. Set CBM_NO_NETWORK=1 to disable.
    """
    if os.environ.get("CBM_NO_NETWORK") == "1":
        return
    try:
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < REFRESH_TTL:
            return
    except OSError:
        pass
    try:
        import urllib.request
        with urllib.request.urlopen(LITELLM_URL, timeout=5) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except Exception:
        # Touch the file so a hard-down network does not retry every render.
        try:
            if os.path.exists(path):
                os.utime(path, None)
        except OSError:
            pass
        return

    slim = {}
    for name, e in raw.items():
        if not isinstance(e, dict) or "claude" not in name:
            continue
        if e.get("litellm_provider") != "anthropic":
            continue
        inp = e.get("input_cost_per_token")
        out = e.get("output_cost_per_token")
        if not inp or not out:
            continue
        rec = {"input": inp * 1e6, "output": out * 1e6}
        for src, dst in (("cache_read_input_token_cost", "cache_read"),
                         ("cache_creation_input_token_cost", "cache_5m"),
                         ("cache_creation_input_token_cost_above_1hr", "cache_1h")):
            if e.get(src):
                rec[dst] = e[src] * 1e6
        if e.get("max_input_tokens"):
            rec["context"] = e["max_input_tokens"]
        slim[name] = rec
    if not slim:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(slim, f)
        os.replace(tmp, path)
    except OSError:
        pass


class Pricing:
    """Resolves a model id to per-million-token rates."""

    def __init__(self, allow_refresh=True):
        self._cache = {}
        self.overrides = _load_json(os.path.join(_config_dir(), "pricing.json")) or {}
        snap_path = os.path.join(_state_dir(), "pricing.json")
        if allow_refresh:
            _refresh_if_stale(snap_path)
        self.snapshot = _load_json(snap_path) or {}

    def _lookup(self, model):
        """-> (rates dict, exact) where exact is False for a guessed family."""
        for table in (self.overrides, self.snapshot):
            if model in table:
                return dict(table[model]), True
        if model in BUNDLED:
            i, o = BUNDLED[model]
            return {"input": i, "output": o}, True

        # Strip a trailing -YYYYMMDD date suffix, then retry.
        base = model
        parts = model.rsplit("-", 1)
        if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
            base = parts[0]
            for table in (self.overrides, self.snapshot):
                if base in table:
                    return dict(table[base]), True
            if base in BUNDLED:
                i, o = BUNDLED[base]
                return {"input": i, "output": o}, True

        # Longest known id that prefixes this one (handles new date suffixes).
        best = None
        for known in BUNDLED:
            if base.startswith(known) and (best is None or len(known) > len(best)):
                best = known
        if best:
            i, o = BUNDLED[best]
            return {"input": i, "output": o}, True

        for key, (i, o) in FAMILY:
            if key in base:
                return {"input": i, "output": o}, False
        return None, False

    def rates(self, model):
        """-> (input, output, cache_read, cache_5m, cache_1h, exact) per 1M tokens.

        Returns None for a model we cannot price at all (e.g. '<synthetic>').
        """
        if model in self._cache:
            return self._cache[model]
        r, exact = self._lookup(model or "")
        if r is None:
            self._cache[model] = None
            return None
        inp = r["input"]
        out = r["output"]
        val = (inp, out,
               r.get("cache_read", inp * 0.10),
               r.get("cache_5m",   inp * 1.25),
               r.get("cache_1h",   inp * 2.00),
               exact)
        self._cache[model] = val
        return val

    def cost(self, model, usage):
        """USD for one assistant message's usage block.

        Splits cache writes by TTL: a 1-hour write costs 2x input while a
        5-minute write costs 1.25x, so collapsing them under-reports by
        roughly 15% on cache-heavy sessions.
        """
        r = self.rates(model)
        if r is None:
            return 0.0
        inp, out, cread, c5r, c1r, _ = r
        c5 = c1h = 0
        cc = usage.get("cache_creation")
        if isinstance(cc, dict):
            c5 = cc.get("ephemeral_5m_input_tokens") or 0
            c1h = cc.get("ephemeral_1h_input_tokens") or 0
        if not (c5 or c1h):
            c5 = usage.get("cache_creation_input_tokens") or 0
        total = ((usage.get("input_tokens") or 0) * inp
                 + (usage.get("output_tokens") or 0) * out
                 + (usage.get("cache_read_input_tokens") or 0) * cread
                 + c5 * c5r
                 + c1h * c1r)
        # Fast mode bills at a premium (Opus 5: $10/$50 vs $5/$25).
        if usage.get("speed") == "fast":
            total *= 2.0
        return total / 1e6

    def context_window(self, model):
        """Max input tokens for `model`, for the context gauge.

        Falls back to the smallest common window rather than a large one, so a
        model we don't know reads as *fuller* than it is — erring toward warning
        the user early rather than reassuring them wrongly.
        """
        model = model or ""
        for table in (self.overrides, self.snapshot):
            v = table.get(model)
            if isinstance(v, dict) and v.get("context"):
                return int(v["context"])
        if model in CONTEXT:
            return CONTEXT[model]
        base = model
        parts = model.rsplit("-", 1)
        if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
            base = parts[0]
            if base in CONTEXT:
                return CONTEXT[base]
        best = None
        for known in CONTEXT:
            if base.startswith(known) and (best is None or len(known) > len(best)):
                best = known
        return CONTEXT[best] if best else DEFAULT_CONTEXT

    def is_exact(self, model):
        r = self.rates(model)
        return bool(r and r[5])

    def is_priceable(self, model):
        return self.rates(model) is not None
