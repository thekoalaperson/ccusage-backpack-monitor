"""Read Claude Code transcripts and total up usage.

Replaces the per-render `ccusage session` subprocess, which cost ~16 CPU-seconds
per turn because it rescans every transcript on the machine. Scanning just the
session's own files costs ~0.2s even for a 270 MB transcript.

Three things this gets right that the subprocess did not:

1. Dedup. One assistant response is written once per content block, so a
   13-block message appears 13 times with the same message id and the same
   cumulative usage. You were billed once. Counting each line inflates cost
   several-fold on tool-heavy sessions.
2. Subagents. Agents spawned by a session write to their own transcript files,
   tagged `teamName: session-<parent-prefix>`. Attributing them back to the
   parent is the only way their models show up at all.
3. Cache TTL. 1-hour cache writes cost 2x input, not 1.25x.
"""
import json, os, glob, time

SCAN_CACHE_VERSION = 3


def _state_dir():
    d = os.path.join(os.environ.get("XDG_STATE_HOME",
                                    os.path.expanduser("~/.local/state")),
                     "ccusage-backpack-monitor")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def team_of(sid):
    """The teamName subagents of session `sid` are tagged with."""
    return "session-" + (sid or "")[:8]


class Scanner:
    """Scans transcripts, memoising per-file results by (size, mtime).

    Subagent transcripts stop changing once their agent finishes, so on a
    typical render only the active file is actually re-read.
    """

    def __init__(self, pricing):
        self.pricing = pricing
        self._path = os.path.join(_state_dir(), "scan-cache.json")
        self._cache = self._load()
        self._dirty = False

    def _load(self):
        try:
            with open(self._path) as f:
                c = json.load(f)
            if c.get("version") != SCAN_CACHE_VERSION:
                return {"version": SCAN_CACHE_VERSION, "files": {}}
            return c
        except Exception:
            return {"version": SCAN_CACHE_VERSION, "files": {}}

    def save(self):
        if not self._dirty:
            return
        files = self._cache.get("files", {})
        # Drop entries for files that no longer exist so the cache cannot grow
        # without bound across months of sessions.
        if len(files) > 400:
            for p in [p for p in files if not os.path.exists(p)]:
                files.pop(p, None)
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._cache, f)
            os.replace(tmp, self._path)
        except OSError:
            pass

    # -- single file --------------------------------------------------------
    def scan_file(self, path):
        """-> {'team':..,'agent':..,'models':{name:{cost,input,output,cache_read,
        cache_creation,turns}}, 'outputs':[...], 'last':epoch}"""
        try:
            st = os.stat(path)
        except OSError:
            return None
        sig = "%d-%d" % (st.st_mtime_ns, st.st_size)
        ent = self._cache["files"].get(path)
        if ent and ent.get("sig") == sig:
            return ent["data"]

        models = {}
        seen = set()
        outputs = []
        hours = {}      # epoch-hour -> cost, so a rolling window can sum slices
        team = agent = None
        last = 0.0
        first = 0.0
        try:
            with open(path, "r", errors="ignore") as f:
                for line in f:
                    # Cheap prefilter: most lines are user turns / tool results.
                    if '"usage"' not in line:
                        if ('"teamName"' in line or '"agentName"' in line
                                or '"agentSetting"' in line):
                            try:
                                o = json.loads(line)
                            except Exception:
                                continue
                            team = o.get("teamName") or team
                            agent = o.get("agentName") or agent or o.get("agentSetting")
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    team = o.get("teamName") or team
                    agent = o.get("agentName") or agent
                    if o.get("type") != "assistant":
                        continue
                    msg = o.get("message")
                    if not isinstance(msg, dict):
                        continue
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    # One API response, many transcript lines -> bill once.
                    key = msg.get("id") or o.get("requestId")
                    if key is not None:
                        if key in seen:
                            continue
                        seen.add(key)
                    name = msg.get("model") or "unknown"
                    m = models.setdefault(name, {"cost": 0.0, "input": 0, "output": 0,
                                                 "cache_read": 0, "cache_creation": 0,
                                                 "turns": 0})
                    c = self.pricing.cost(name, u)
                    m["cost"] += c
                    m["input"] += u.get("input_tokens") or 0
                    m["output"] += u.get("output_tokens") or 0
                    m["cache_read"] += u.get("cache_read_input_tokens") or 0
                    m["cache_creation"] += u.get("cache_creation_input_tokens") or 0
                    m["turns"] += 1
                    outputs.append(u.get("output_tokens") or 0)
                    ts = o.get("timestamp")
                    if ts:
                        e = _epoch(ts)
                        if e > last:
                            last = e
                        if e and (not first or e < first):
                            first = e
                        if e:
                            hb = str(int(e // 3600))
                            hours[hb] = hours.get(hb, 0.0) + c
        except OSError:
            return None

        data = {"team": team, "agent": agent, "models": models,
                "outputs": outputs[-256:], "hours": hours,
                "first": first, "last": last}
        self._cache["files"][path] = {"sig": sig, "data": data}
        self._dirty = True
        return data

    # -- one session, including its subagents --------------------------------
    def subagent_files(self, sid, transcript):
        """Transcripts of every agent this session spawned.

        Claude Code nests them under a directory named for the parent session:

            <project>/<sid>.jsonl                                  the session
            <project>/<sid>/subagents/agent-<id>.jsonl             Task agents
            <project>/<sid>/subagents/workflows/<wf>/agent-*.jsonl workflow fan-out

        That directory name is the parent link, so no content scan is needed to
        decide membership. A few agents also run with a different cwd and land
        in another project directory; those are matched on `teamName` instead,
        but only among files touched since this session started.
        """
        if not (sid and transcript):
            return []
        proj = os.path.dirname(transcript)
        found = set()
        base = os.path.join(proj, sid, "subagents")
        if os.path.isdir(base):
            for root, _dirs, names in os.walk(base):
                for n in names:
                    if n.endswith(".jsonl"):
                        found.add(os.path.join(root, n))
        return sorted(found)

    def session(self, sid, transcript, cross_project=True):
        """Total a session: its own transcript plus every subagent it spawned."""
        result = {"models": {}, "agents": [], "outputs": [], "cost": 0.0,
                  "tokens": 0, "self_cost": 0.0, "agent_cost": 0.0}
        if not transcript or not os.path.exists(transcript):
            return result

        own = self.scan_file(transcript)
        started = 0.0
        if own:
            _merge(result["models"], own["models"])
            result["outputs"] = own["outputs"]
            result["self_cost"] = sum(m["cost"] for m in own["models"].values())
            started = own.get("first") or 0.0

        paths = list(self.subagent_files(sid, transcript))

        # Agents launched with a different cwd land in a sibling project dir.
        # Bound that search to files touched since this session began.
        if cross_project and started:
            team = team_of(sid)
            root = os.path.dirname(os.path.dirname(transcript))
            seen = {os.path.abspath(p) for p in paths}
            for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
                ap = os.path.abspath(path)
                if ap in seen or ap == os.path.abspath(transcript):
                    continue
                try:
                    if os.path.getmtime(path) < started:
                        continue
                except OSError:
                    continue
                d = self.scan_file(path)
                if d and d.get("team") == team:
                    paths.append(path)

        for path in paths:
            if os.path.abspath(path) == os.path.abspath(transcript):
                continue
            d = self.scan_file(path)
            if not d or not d["models"]:
                continue
            cost = sum(m["cost"] for m in d["models"].values())
            _merge(result["models"], d["models"])
            result["agent_cost"] += cost
            result["agents"].append({
                "name": d.get("agent") or _agent_label(path),
                "cost": cost,
                "models": sorted(d["models"], key=lambda k: -d["models"][k]["cost"]),
                "last": d.get("last", 0.0),
            })

        result["agents"].sort(key=lambda a: -a["cost"])
        result["cost"] = sum(m["cost"] for m in result["models"].values())
        result["tokens"] = sum(m["input"] + m["output"] + m["cache_read"]
                               + m["cache_creation"] for m in result["models"].values())
        return result

    # -- account-wide 5h billing block --------------------------------------
    def active_block(self, hours=5.0, root=None, now=None):
        """The current rolling usage block, account-wide.

        Same shape as `ccusage blocks --active` but computed locally, so it is
        priced with the same table as the session panel — otherwise the two
        numbers on screen disagree, which is what made the old panel show
        $0.00 for the session and $143 for the window at the same moment.

        A block starts at the first activity after a gap of >= `hours`, floored
        to the hour, and runs for `hours`. Returns None when nothing is active.
        """
        root = root or os.path.expanduser("~/.claude/projects")
        now = now or time.time()
        span = hours * 3600
        cutoff = now - span * 2  # only files that could touch the current block

        buckets = {}
        for path in _walk_jsonl(root):
            try:
                if os.path.getmtime(path) < cutoff:
                    continue
            except OSError:
                continue
            d = self.scan_file(path)
            if not d:
                continue
            for hb, c in (d.get("hours") or {}).items():
                h = int(hb)
                if h * 3600 < cutoff:
                    continue
                buckets[h] = buckets.get(h, 0.0) + c
        if not buckets:
            return None

        # Walk hours forward, breaking a block on a >= span gap or on span elapsed.
        start = None
        prev = None
        total = 0.0
        for h in sorted(buckets):
            t = h * 3600
            if (start is None or t - start >= span
                    or (prev is not None and t - prev >= span)):
                start, total = t, 0.0
            total += buckets[h]
            prev = t

        if start is None or now - start >= span:
            return None
        end = start + span
        elapsed = max(now - start, 60.0)
        rate = total / (elapsed / 3600.0)
        return {"cost": total, "start": start, "end": end,
                "rate": rate, "projected": rate * hours}


def _walk_jsonl(root):
    """Every transcript under `root`, including nested subagent directories."""
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.endswith(".jsonl"):
                yield os.path.join(dirpath, n)


def _agent_label(path):
    """Readable name for an agent transcript that carries no agentName."""
    name = os.path.basename(path)[:-6]
    if name.startswith("agent-"):
        name = name[6:]
    parent = os.path.basename(os.path.dirname(path))
    if parent.startswith("wf_"):
        return "workflow " + name[:8]
    return name[:12]


def _merge(dst, src):
    for name, m in src.items():
        d = dst.setdefault(name, {"cost": 0.0, "input": 0, "output": 0,
                                  "cache_read": 0, "cache_creation": 0, "turns": 0})
        for k in d:
            d[k] += m.get(k, 0)


def _epoch(ts):
    """Parse an ISO-8601 timestamp without pulling in a dependency."""
    try:
        import datetime
        s = ts.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0
