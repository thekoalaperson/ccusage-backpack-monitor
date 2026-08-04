"""Account, plan and rate-limit facts, read from Claude Code's own caches.

Everything here is read-only, best-effort, and *never* fetched over the network.
Two files, both written by Claude Code itself:

  ~/.claude.json              oauthAccount (who/what plan) and
                              cachedUsageUtilization (server-side limit state)
  ~/.claude/stats-cache.json  account-wide daily history, recomputed once a day

Why this is worth reading at all: on a subscription the dollar figures the panel
computes are notional — you do not pay them. What actually stops you mid-task is
the *percentage of your rate limit*, and the weekly window in particular is
invisible to anything that only reads transcripts.

Three rules this module holds to:

1. **Never fetch.** `cachedUsageUtilization` is a cache the client refreshes on
   its own schedule. We read whatever is there and report how old it is; we do
   not go and get a fresher one. Polling Anthropic from a status pane would be
   both rude and a betrayal of the idle-cost promise.
2. **Report the age, always.** A stale percentage presented as current is
   exactly the "never claim a number you inferred" failure in CLAUDE.md. Callers
   get `age` in seconds and decide how to mark it.
3. **Guard every key.** These are private client internals with no compatibility
   promise; any Claude Code release may rename or drop them. Every access is
   defensive and a missing key hides a row rather than raising.

Privacy: this pane is on screen during screen-shares and recordings, so the
default identity is a first name only. `CBM_ACCOUNT=full` opts into the email
and organisation name; `CBM_ACCOUNT=0` turns the whole thing off.
"""
import json, os, re, time

# The percentage bands the panel colours on. `severity` from the server wins
# when present; these are the fallback for an older client that omits it.
WARN_AT = 60
HOT_AT = 85

# Past this, the cached utilisation is marked `~` rather than shown bare.
#
# Tuned against observed behaviour, not guessed: Claude Code refreshes this on
# its own cadence, and mid-session gaps of half an hour are normal. A tighter
# threshold would stamp `~` on every render, and a marker that is always on is a
# marker nobody reads. 45 minutes is 15% of the 5-hour window -- old enough that
# the number really might have moved. The limits tab always prints the exact
# age regardless, so the precise figure is never hidden behind this.
STALE_AFTER = 45 * 60


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _epoch(ts):
    """ISO-8601 -> epoch seconds, 0.0 on anything unparseable."""
    if not ts:
        return 0.0
    try:
        import datetime
        return datetime.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _num(v, default=None):
    """A number, or `default` — `None` and "" are common in these caches."""
    if isinstance(v, bool) or v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def plan_label(oauth):
    """A short plan badge: 'Max 5x', 'Pro', 'Team', 'API'.

    Derived from the rate-limit tier ('default_claude_max_5x') with the
    organisation type as backup, because the tier string is the only place the
    5x/20x multiplier appears and that multiplier is the part worth knowing.
    """
    if not isinstance(oauth, dict):
        return ""
    tier = str(oauth.get("organizationRateLimitTier")
               or oauth.get("userRateLimitTier") or "").lower()
    otype = str(oauth.get("organizationType") or "").lower()
    hay = tier + " " + otype

    mult = ""
    m = re.search(r"max_?(\d+)x", hay)
    if m:
        mult = " %sx" % m.group(1)

    # Most specific first: 'enterprise' and 'team' must not be eaten by 'pro'.
    for key, label in (("enterprise", "Ent"), ("team", "Team"),
                       ("max", "Max"), ("pro", "Pro"), ("free", "Free")):
        if key in hay:
            return label + (mult if key == "max" else "")
    if "api" in str(oauth.get("billingType") or "").lower():
        return "API"
    return ""


class Account:
    """Lazy, defensive reader over the two cache files.

    Nothing is read until asked for, and every accessor returns None rather than
    raising, so a caller can treat the whole module as optional.
    """

    def __init__(self, home=None, now=None):
        self.home = home or os.path.expanduser("~")
        self._now = now
        self._conf = None
        self._stats = None
        self._loaded_conf = False
        self._loaded_stats = False

    def now(self):
        return self._now if self._now is not None else time.time()

    # -- raw files ----------------------------------------------------------
    def _config(self):
        if not self._loaded_conf:
            self._loaded_conf = True
            self._conf = _read_json(os.path.join(self.home, ".claude.json"))
        return self._conf if isinstance(self._conf, dict) else None

    def _stats_file(self):
        if not self._loaded_stats:
            self._loaded_stats = True
            self._stats = _read_json(
                os.path.join(self.home, ".claude", "stats-cache.json"))
        return self._stats if isinstance(self._stats, dict) else None

    # -- identity -----------------------------------------------------------
    def identity(self):
        """-> {name, email, org, plan, billing, role, since} or None.

        `email` and `org` are only populated when CBM_ACCOUNT=full, so the
        default panel leaks nothing beyond a first name.
        """
        conf = self._config()
        if not conf:
            return None
        oauth = conf.get("oauthAccount")
        if not isinstance(oauth, dict):
            return None
        full = os.environ.get("CBM_ACCOUNT", "").lower() == "full"
        name = oauth.get("displayName") or ""
        email = oauth.get("emailAddress") or ""
        if not name and email:
            name = email.split("@")[0]
        return {
            "name": str(name),
            "email": str(email) if full else "",
            "org": str(oauth.get("organizationName") or "") if full else "",
            "plan": plan_label(oauth),
            "billing": str(oauth.get("billingType") or "").replace("_", " "),
            "role": str(oauth.get("organizationRole") or ""),
            "since": _epoch(oauth.get("subscriptionCreatedAt")
                            or oauth.get("accountCreatedAt")),
            "extra_usage": bool(oauth.get("hasExtraUsageEnabled")),
        }

    # -- rate limits --------------------------------------------------------
    def _utilization(self):
        conf = self._config()
        if not conf:
            return None, None
        cached = conf.get("cachedUsageUtilization")
        if not isinstance(cached, dict):
            return None, None
        util = cached.get("utilization")
        if not isinstance(util, dict):
            return None, None
        fetched = _num(cached.get("fetchedAtMs"))
        age = None
        if fetched:
            age = max(0.0, self.now() - fetched / 1000.0)
        return util, age

    def limits(self):
        """-> {'age': seconds|None, 'session': row|None, 'weekly': [row...],
                'credits': {...}|None} or None.

        A row is {label, percent, resets, severity, scope, kind}. `resets` is an
        epoch, 0 when the server didn't say. Rows are only included when the
        server actually gave a percentage — an absent limit is not a limit at 0%,
        and rendering it as an empty meter would be a claim we can't support.
        """
        util, age = self._utilization()
        if util is None:
            return None

        session = None
        weekly = []
        raw = util.get("limits")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                pct = _num(item.get("percent"))
                if pct is None:
                    continue
                row = {
                    "kind": str(item.get("kind") or ""),
                    "group": str(item.get("group") or ""),
                    "percent": pct,
                    "resets": _epoch(item.get("resets_at")),
                    "severity": str(item.get("severity") or ""),
                    "scope": self._scope_label(item.get("scope")),
                    "active": bool(item.get("is_active", True)),
                }
                if row["group"] == "session" or row["kind"] == "session":
                    # Keep the hottest if a client ever reports more than one.
                    if session is None or pct > session["percent"]:
                        session = row
                elif "week" in row["group"] or "week" in row["kind"]:
                    weekly.append(row)

        # Older clients carry only the flat five_hour block. Same shape out.
        if session is None:
            fh = util.get("five_hour")
            if isinstance(fh, dict):
                pct = _num(fh.get("utilization"))
                if pct is not None:
                    session = {"kind": "session", "group": "session",
                               "percent": pct, "resets": _epoch(fh.get("resets_at")),
                               "severity": "", "scope": "", "active": True}

        # Hottest weekly first: that's the one that decides when you're blocked.
        weekly.sort(key=lambda r: -r["percent"])
        return {"age": age, "session": session, "weekly": weekly,
                "credits": self._credits(util)}

    def _scope_label(self, scope):
        """'Fable' from {'model': {'display_name': 'Fable'}}, else ''."""
        if not isinstance(scope, dict):
            return ""
        model = scope.get("model")
        if isinstance(model, dict):
            name = model.get("display_name") or model.get("id")
            if name:
                return str(name)
        surface = scope.get("surface")
        if isinstance(surface, dict):
            name = surface.get("display_name") or surface.get("id")
            if name:
                return str(name)
        return ""

    def _credits(self, util):
        """Extra-usage credits, only when actually enabled."""
        extra = util.get("extra_usage")
        if not isinstance(extra, dict) or not extra.get("is_enabled"):
            return None
        return {"percent": _num(extra.get("utilization"), 0.0) or 0.0,
                "used": _num(extra.get("used_credits"), 0.0) or 0.0,
                "limit": _num(extra.get("monthly_limit")),
                "currency": str(extra.get("currency") or "USD"),
                "capped": bool(extra.get("spend_limit_reached"))}

    # -- history ------------------------------------------------------------
    def history(self, days=7):
        """-> {'days': [{date, tokens, models}], 'sessions', 'messages', 'since',
                'stale'} or None.

        `stale` is True when the cache wasn't recomputed today — it's rebuilt
        once a day, so today's row is usually missing entirely.
        """
        stats = self._stats_file()
        if not stats:
            return None
        rows = []
        daily = stats.get("dailyModelTokens")
        if isinstance(daily, list):
            for d in daily[-days:]:
                if not isinstance(d, dict):
                    continue
                by = d.get("tokensByModel")
                if not isinstance(by, dict):
                    continue
                total = sum(v for v in by.values() if isinstance(v, (int, float)))
                rows.append({"date": str(d.get("date") or ""),
                             "tokens": total,
                             "models": sorted(by, key=lambda k: -by[k])})
        if not rows:
            return None
        return {"days": rows,
                "sessions": _num(stats.get("totalSessions"), 0) or 0,
                "messages": _num(stats.get("totalMessages"), 0) or 0,
                "since": str(stats.get("firstSessionDate") or "")[:10],
                "computed": str(stats.get("lastComputedDate") or ""),
                "stale": str(stats.get("lastComputedDate") or "")
                         != time.strftime("%Y-%m-%d", time.localtime(self.now()))}


def severity_rank(row, warn=WARN_AT, hot=HOT_AT):
    """0 normal / 1 warn / 2 hot, trusting the server's own word first."""
    sev = (row.get("severity") or "").lower()
    if sev in ("critical", "exceeded", "blocked", "severe"):
        return 2
    if sev in ("warning", "warn", "elevated"):
        return 1
    pct = row.get("percent") or 0
    if pct >= hot:
        return 2
    if pct >= warn:
        return 1
    return 0
