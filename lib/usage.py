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

SCAN_CACHE_VERSION = 4
SCAN_CACHE_MAX = 600      # entries; oldest-touched evicted beyond this


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
        # Bound the cache. Deleted files go first; if that isn't enough, evict
        # the least recently touched. Without this it grows for every agent
        # transcript ever seen (571 entries / 343 KB after a few weeks).
        if len(files) > SCAN_CACHE_MAX:
            for p in [p for p in files if not os.path.exists(p)]:
                files.pop(p, None)
        if len(files) > SCAN_CACHE_MAX:
            ordered = sorted(files.items(), key=lambda kv: kv[1].get("seen", 0))
            for p, _ in ordered[:len(files) - SCAN_CACHE_MAX]:
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
            ent["seen"] = int(time.time())   # LRU stamp for eviction
            self._dirty = True
            return ent["data"]

        models = {}
        seen = set()
        outputs = []
        hours = {}      # epoch-hour -> cost, so a rolling window can sum slices
        team = agent = cwd = None
        last = 0.0
        first = 0.0
        last_ctx = None
        try:
            with open(path, "r", errors="ignore") as f:
                for line in f:
                    # Cheap prefilter: most lines are user turns / tool results.
                    if '"usage"' not in line:
                        if ('"teamName"' in line or '"agentName"' in line
                                or '"agentSetting"' in line or ('"cwd"' in line and not cwd)):
                            try:
                                o = json.loads(line)
                            except Exception:
                                continue
                            team = o.get("teamName") or team
                            agent = o.get("agentName") or agent or o.get("agentSetting")
                            cwd = cwd or o.get("cwd")
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
                    # Context in play on the most recent turn: everything the
                    # model read this request (fresh + cached), which is what
                    # fills the window. Output is not part of the input window.
                    ctx = ((u.get("input_tokens") or 0)
                           + (u.get("cache_read_input_tokens") or 0)
                           + (u.get("cache_creation_input_tokens") or 0))
                    last_ctx = (name, ctx)
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

        data = {"team": team, "agent": agent, "cwd": cwd, "models": models,
                "outputs": outputs[-256:], "hours": hours,
                "first": first, "last": last, "last_ctx": last_ctx}
        self._cache["files"][path] = {"sig": sig, "data": data,
                                      "seen": int(time.time())}
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
                  "tokens": 0, "self_cost": 0.0, "agent_cost": 0.0,
                  "context": None}
        if not transcript or not os.path.exists(transcript):
            return result

        own = self.scan_file(transcript)
        started = 0.0
        if own:
            _merge(result["models"], own["models"])
            result["outputs"] = own["outputs"]
            result["context"] = own.get("last_ctx")
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


def is_agent_path(path):
    """True if `path` sits under a `subagents/` directory."""
    parts = os.path.normpath(path).split(os.sep)
    return "subagents" in parts


class Resolver:
    """Work out which session a pane should follow.

    Getting this wrong is worse than showing nothing: a subagent transcript
    rendered as a session reports one agent's spend as the whole session's, and
    the number looks plausible enough to be believed. That is exactly what
    happened when an agent ran with a different cwd and left its transcript at
    the top level of another project directory.

    Two rules do most of the work:
      * a transcript carrying `teamName` is an agent, never a session;
      * match on the `cwd` recorded *inside* transcripts rather than on a
        path-to-directory-name transform, which is undocumented and would break
        on paths containing spaces or other unusual characters.
    """

    def __init__(self, scanner, root=None):
        self.scan = scanner
        self.root = root or os.path.expanduser("~/.claude/projects")

    def _all(self):
        return list(_walk_jsonl(self.root))

    def _info(self, path):
        d = self.scan.scan_file(path)
        if d is None:
            return None
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        return {"path": path, "sid": os.path.basename(path)[:-6],
                "team": d.get("team"), "cwd": d.get("cwd"),
                "agent": bool(d.get("team")) or is_agent_path(path),
                "mtime": mtime}

    def find_by_sid(self, sid):
        for p in self._all():
            if os.path.basename(p) == sid + ".jsonl":
                return p
        return None

    def parent_of(self, info):
        """Given an agent transcript, the session that spawned it."""
        team = info.get("team") or ""
        if team.startswith("session-"):
            prefix = team[len("session-"):]
            best = None
            for p in self._all():
                base = os.path.basename(p)[:-6]
                if base.startswith(prefix) and not is_agent_path(p):
                    i = self._info(p)
                    if i and not i["team"]:
                        if best is None or i["mtime"] > best["mtime"]:
                            best = i
            return best
        # Nested layout: <project>/<parent-sid>/subagents/...
        parts = os.path.normpath(info["path"]).split(os.sep)
        if "subagents" in parts:
            idx = parts.index("subagents")
            if idx >= 1:
                cand = os.sep.join(parts[:idx]) + ".jsonl"
                if os.path.exists(cand):
                    return self._info(cand)
        return None

    def session_ancestor(self, info, max_depth=8):
        """Walk up from an agent to the real session at the top of its chain.

        Agents spawn agents (a workflow's coordinator is itself an agent), so a
        single hop can land on another agent. And a chain can be broken — an
        agent whose parent transcript has been deleted has no session, and
        saying so is better than returning the nearest agent as if it were one.
        """
        seen = set()
        cur = info
        for _ in range(max_depth):
            parent = self.parent_of(cur)
            if not parent:
                return None
            if parent["path"] in seen:      # defensive: never loop
                return None
            seen.add(parent["path"])
            if not parent["agent"]:
                return parent
            cur = parent
        return None

    def resolve(self, pwd=None, sid=None):
        """-> (sid, transcript_path) or (None, None).

        Preference order: an explicitly supplied session id; a real session
        whose recorded cwd matches `pwd`; the most recent real session anywhere;
        finally, the parent of the most recent agent transcript.
        """
        if sid:
            p = self.find_by_sid(sid)
            if p:
                return sid, p

        infos = [i for i in (self._info(p) for p in self._all()) if i]
        sessions = [i for i in infos if not i["agent"]]

        agents = [i for i in infos if i["agent"]]

        if pwd:
            pwd = os.path.normpath(pwd)

            # 1. A real session started in exactly this directory.
            exact = [i for i in sessions
                     if i["cwd"] and os.path.normpath(i["cwd"]) == pwd]
            if exact:
                best = max(exact, key=lambda i: i["mtime"])
                return best["sid"], best["path"]

            # 2. No session here, but an agent ran here -> follow it home. This
            #    is deterministic; falling through to "newest session anywhere"
            #    would answer with whichever unrelated session was written last.
            agent_here = [i for i in agents
                          if i["cwd"] and os.path.normpath(i["cwd"]) == pwd]
            for cand in sorted(agent_here, key=lambda i: -i["mtime"]):
                parent = self.session_ancestor(cand)
                if parent:
                    return parent["sid"], parent["path"]

            # 3. A session started in the nearest ancestor directory. Claude
            #    records the directory it started in, so working deeper in the
            #    tree still belongs to that session.
            anc = []
            for i in sessions:
                if not i["cwd"]:
                    continue
                c = os.path.normpath(i["cwd"])
                if pwd.startswith(c + os.sep):
                    anc.append((len(c), i["mtime"], i))
            if anc:
                anc.sort(key=lambda t: (t[0], t[1]), reverse=True)
                best = anc[0][2]
                return best["sid"], best["path"]

        # 4. Last resort: the most recently active session anywhere.
        if sessions:
            best = max(sessions, key=lambda i: i["mtime"])
            return best["sid"], best["path"]

        for cand in sorted(agents, key=lambda i: -i["mtime"]):
            parent = self.session_ancestor(cand)
            if parent:
                return parent["sid"], parent["path"]
        return None, None


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
