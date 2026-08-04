#!/usr/bin/env python3
"""Regression tests for pricing, transcript scanning, and the panel.

Fixture-driven: every test builds its own transcripts in a temp directory, so
the suite never reads the developer's real usage data and gives the same answer
on any machine.

Run:  python3 tests/test_usage.py
"""
import json, os, re, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..", "lib")
sys.path.insert(0, LIB)

os.environ["CBM_NO_NETWORK"] = "1"   # tests must never hit the network

from pricing import Pricing          # noqa: E402
import usage as usage_mod            # noqa: E402
from usage import Scanner            # noqa: E402


def msg(model, msg_id, inp=0, out=0, cache_read=0, c5=0, c1h=0, ts=None, req=None):
    """One assistant transcript line."""
    u = {"input_tokens": inp, "output_tokens": out,
         "cache_read_input_tokens": cache_read,
         "cache_creation_input_tokens": c5 + c1h,
         "cache_creation": {"ephemeral_5m_input_tokens": c5,
                            "ephemeral_1h_input_tokens": c1h}}
    return {"type": "assistant", "requestId": req or ("req_" + msg_id),
            "timestamp": ts or "2026-08-01T12:00:00.000Z",
            "message": {"id": msg_id, "model": model, "usage": u}}


def write(path, rows):
    """Write a transcript the way Claude Code does: one compact JSON line each."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cbm-test-")
        # Redirect all state (scan cache, pricing snapshot) into the sandbox.
        os.environ["XDG_STATE_HOME"] = os.path.join(self.tmp, "state")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        # HOME too: the panel now reads account facts from ~/.claude.json, and a
        # suite that picked up the developer's real plan and email would both
        # leak them into test output and give a different answer per machine.
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = os.path.join(self.tmp, "home")
        os.makedirs(os.environ["HOME"], exist_ok=True)
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects, exist_ok=True)
        self.pricing = Pricing(allow_refresh=False)
        self.scan = Scanner(self.pricing)

    def tearDown(self):
        if self._home is not None:
            os.environ["HOME"] = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- account fixtures ---------------------------------------------------
    def write_account(self, limits=None, oauth=None, fetched_ms=None, stats=None):
        """Plant a ~/.claude.json (and optionally stats-cache.json) in the sandbox."""
        conf = {}
        conf["oauthAccount"] = oauth if oauth is not None else {
            "displayName": "Testy", "emailAddress": "testy@example.com",
            "organizationName": "Testy's Org", "organizationType": "claude_max",
            "organizationRateLimitTier": "default_claude_max_5x",
            "billingType": "stripe_subscription", "organizationRole": "admin",
            "subscriptionCreatedAt": "2026-07-24T19:23:02.350742Z"}
        if limits is not None:
            ms = fetched_ms if fetched_ms is not None else int(time.time() * 1000)
            conf["cachedUsageUtilization"] = {
                "fetchedAtMs": ms,
                "utilization": {"limits": limits, "extra_usage": {"is_enabled": False}}}
        with open(os.path.join(os.environ["HOME"], ".claude.json"), "w") as f:
            json.dump(conf, f)
        if stats is not None:
            d = os.path.join(os.environ["HOME"], ".claude")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "stats-cache.json"), "w") as f:
                json.dump(stats, f)

    @staticmethod
    def limit_row(kind, group, percent, resets_in=3600, scope=None, severity="normal"):
        iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                            time.gmtime(time.time() + resets_in))
        row = {"kind": kind, "group": group, "percent": percent,
               "severity": severity, "resets_at": iso, "is_active": True}
        if scope:
            row["scope"] = {"model": {"display_name": scope}}
        return row


class TestPricing(Base):
    def test_new_models_are_priced(self):
        """The reported bug: Opus 5 and Sonnet 5 costing $0.00."""
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5",
                      "claude-opus-4-8", "claude-haiku-4-5"):
            r = self.pricing.rates(model)
            self.assertIsNotNone(r, "%s must be priceable" % model)
            self.assertGreater(r[0], 0, "%s input rate must be > 0" % model)
            self.assertTrue(self.pricing.is_exact(model))

    def test_dated_suffix_resolves(self):
        a = self.pricing.rates("claude-haiku-4-5-20251001")
        b = self.pricing.rates("claude-haiku-4-5")
        self.assertEqual(a[:5], b[:5])

    def test_unknown_future_model_falls_back_to_family(self):
        """A model shipped after this release must not silently cost $0."""
        r = self.pricing.rates("claude-opus-7-20990101")
        self.assertIsNotNone(r)
        self.assertGreater(r[0], 0)
        self.assertFalse(self.pricing.is_exact("claude-opus-7-20990101"),
                         "guessed rates must be flagged as estimated")

    def test_unpriceable_is_none_not_zero(self):
        self.assertIsNone(self.pricing.rates("<synthetic>"))
        self.assertFalse(self.pricing.is_priceable("<synthetic>"))

    def test_cache_ttl_rates(self):
        """1h cache writes cost 2x input; 5m cost 1.25x."""
        inp, out, cread, c5, c1h, _ = self.pricing.rates("claude-opus-5")
        self.assertAlmostEqual(cread, inp * 0.10, places=6)
        self.assertAlmostEqual(c5, inp * 1.25, places=6)
        self.assertAlmostEqual(c1h, inp * 2.00, places=6)

    def test_cost_math(self):
        """Hand-computed: Opus 5 at $5/$25 per Mtok."""
        u = msg("claude-opus-5", "m1", inp=1_000_000, out=1_000_000,
                cache_read=1_000_000, c5=1_000_000, c1h=1_000_000)["message"]["usage"]
        # 5 + 25 + 0.5 + 6.25 + 10
        self.assertAlmostEqual(self.pricing.cost("claude-opus-5", u), 46.75, places=6)

    def test_1h_cache_not_collapsed_into_5m(self):
        cheap = self.pricing.cost("claude-opus-5", msg(
            "claude-opus-5", "a", c5=1_000_000)["message"]["usage"])
        dear = self.pricing.cost("claude-opus-5", msg(
            "claude-opus-5", "b", c1h=1_000_000)["message"]["usage"])
        self.assertAlmostEqual(cheap, 6.25, places=6)
        self.assertAlmostEqual(dear, 10.0, places=6)

    def test_fast_mode_premium(self):
        u = msg("claude-opus-5", "m", out=1_000_000)["message"]["usage"]
        base = self.pricing.cost("claude-opus-5", u)
        u["speed"] = "fast"
        self.assertAlmostEqual(self.pricing.cost("claude-opus-5", u), base * 2, places=6)

    def test_context_windows(self):
        self.assertEqual(self.pricing.context_window("claude-opus-5"), 1000000)
        self.assertEqual(self.pricing.context_window("claude-haiku-4-5"), 200000)
        self.assertEqual(self.pricing.context_window("claude-haiku-4-5-20251001"), 200000)

    def test_unknown_context_window_errs_small(self):
        """An unknown model should read as fuller, not emptier, than reality."""
        self.assertEqual(self.pricing.context_window("claude-brandnew-9"), 200000)

    def test_user_override_wins(self):
        cfg = os.path.join(self.tmp, "config", "ccusage-backpack-monitor")
        os.makedirs(cfg, exist_ok=True)
        with open(os.path.join(cfg, "pricing.json"), "w") as f:
            json.dump({"claude-opus-5": {"input": 99.0, "output": 1.0}}, f)
        p = Pricing(allow_refresh=False)
        self.assertEqual(p.rates("claude-opus-5")[0], 99.0)


class TestScanner(Base):
    def _session(self, sid, rows, project="proj"):
        path = os.path.join(self.projects, project, sid + ".jsonl")
        write(path, rows)
        return path

    def test_dedup_multi_block_message(self):
        """One API response is written once per content block; bill it once."""
        rows = [msg("claude-opus-5", "msg_1", out=1_000_000) for _ in range(13)]
        path = self._session("s1", rows)
        d = self.scan.scan_file(path)
        self.assertEqual(d["models"]["claude-opus-5"]["turns"], 1)
        self.assertAlmostEqual(d["models"]["claude-opus-5"]["cost"], 25.0, places=6)

    def test_distinct_messages_all_counted(self):
        rows = [msg("claude-opus-5", "msg_%d" % i, out=1_000_000) for i in range(4)]
        d = self.scan.scan_file(self._session("s2", rows))
        self.assertEqual(d["models"]["claude-opus-5"]["turns"], 4)
        self.assertAlmostEqual(d["models"]["claude-opus-5"]["cost"], 100.0, places=6)

    def test_subagents_attributed_to_parent(self):
        """The reported bug: subagent models missing from the panel."""
        sid = "abc12345-0000-0000-0000-000000000000"
        main = self._session(sid, [msg("claude-fable-5", "m1", out=1_000_000)])
        proj = os.path.dirname(main)
        write(os.path.join(proj, sid, "subagents", "agent-aaa.jsonl"),
              [{"type": "agent-setting", "agentName": "researcher",
                "agentSetting": "general-purpose"},
               msg("claude-sonnet-5", "s1", out=1_000_000)])
        write(os.path.join(proj, sid, "subagents", "workflows", "wf_1", "agent-bbb.jsonl"),
              [msg("claude-opus-4-8", "o1", out=1_000_000)])

        res = self.scan.session(sid, main, cross_project=False)
        self.assertAlmostEqual(res["self_cost"], 50.0, places=6)     # fable  $50
        self.assertAlmostEqual(res["agent_cost"], 35.0, places=6)    # 10 + 25
        self.assertAlmostEqual(res["cost"], 85.0, places=6)
        self.assertEqual(len(res["agents"]), 2)
        self.assertIn("claude-sonnet-5", res["models"])
        self.assertIn("claude-opus-4-8", res["models"])
        names = {a["name"] for a in res["agents"]}
        self.assertIn("researcher", names)

    def test_no_subagents_is_clean(self):
        sid = "def67890-0000-0000-0000-000000000000"
        main = self._session(sid, [msg("claude-opus-5", "m1", out=1_000_000)])
        res = self.scan.session(sid, main, cross_project=False)
        self.assertEqual(res["agents"], [])
        self.assertAlmostEqual(res["cost"], 25.0, places=6)
        self.assertEqual(res["agent_cost"], 0.0)

    def test_sibling_session_not_absorbed(self):
        """Another top-level session must not leak into this one's total."""
        sid = "aaa11111-0000-0000-0000-000000000000"
        main = self._session(sid, [msg("claude-opus-5", "m1", out=1_000_000)])
        self._session("bbb22222-0000-0000-0000-000000000000",
                      [msg("claude-opus-5", "x1", out=1_000_000)])
        res = self.scan.session(sid, main, cross_project=False)
        self.assertAlmostEqual(res["cost"], 25.0, places=6)

    def test_cache_reuses_result_until_file_changes(self):
        path = self._session("s3", [msg("claude-opus-5", "m1", out=1_000_000)])
        first = self.scan.scan_file(path)
        self.assertAlmostEqual(first["models"]["claude-opus-5"]["cost"], 25.0, places=6)
        # Append a new message; mtime/size change must invalidate the cache.
        time.sleep(0.01)
        with open(path, "a") as f:
            f.write(json.dumps(msg("claude-opus-5", "m2", out=1_000_000)) + "\n")
        second = self.scan.scan_file(path)
        self.assertAlmostEqual(second["models"]["claude-opus-5"]["cost"], 50.0, places=6)

    def test_malformed_lines_are_skipped(self):
        path = os.path.join(self.projects, "proj", "s4.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("not json at all\n")
            f.write('{"type":"assistant","message":{"usage":\n')     # truncated
            f.write(json.dumps(msg("claude-opus-5", "m1", out=1_000_000)) + "\n")
            f.write('{"type":"user","message":{"content":"hi"}}\n')
        d = self.scan.scan_file(path)
        self.assertAlmostEqual(d["models"]["claude-opus-5"]["cost"], 25.0, places=6)

    def test_missing_file_returns_none(self):
        self.assertIsNone(self.scan.scan_file(os.path.join(self.tmp, "nope.jsonl")))

    def test_unpriceable_model_contributes_zero(self):
        d = self.scan.scan_file(self._session(
            "s5", [msg("<synthetic>", "m1", out=1_000_000)]))
        self.assertAlmostEqual(d["models"]["<synthetic>"]["cost"], 0.0, places=6)

    def test_context_is_last_turn_input_side(self):
        """Context = what the model read (fresh + cached), not what it wrote."""
        rows = [msg("claude-opus-5", "m1", inp=10, cache_read=10, out=999),
                msg("claude-opus-5", "m2", inp=1000, cache_read=200_000,
                    c1h=5_000, out=999)]
        d = self.scan.scan_file(self._session("s6", rows))
        model, ctx = d["last_ctx"]
        self.assertEqual(model, "claude-opus-5")
        self.assertEqual(ctx, 1000 + 200_000 + 5_000)   # output excluded

    def test_scan_cache_is_bounded(self):
        import usage as u
        original = u.SCAN_CACHE_MAX
        u.SCAN_CACHE_MAX = 5
        try:
            for i in range(12):
                self.scan.scan_file(self._session(
                    "bulk%d" % i, [msg("claude-opus-5", "m%d" % i, out=1000)]))
            self.scan.save()
            with open(os.path.join(self.tmp, "state",
                                   "ccusage-backpack-monitor", "scan-cache.json")) as f:
                saved = json.load(f)
            self.assertLessEqual(len(saved["files"]), 5)
        finally:
            u.SCAN_CACHE_MAX = original


class TestActiveBlock(Base):
    def _at(self, hours_ago):
        t = time.time() - hours_ago * 3600
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))

    def test_window_excludes_old_activity(self):
        """A long session's older hours must not inflate the 5h number."""
        rows = [msg("claude-opus-5", "old", out=1_000_000, ts=self._at(30)),
                msg("claude-opus-5", "new", out=1_000_000, ts=self._at(1))]
        write(os.path.join(self.projects, "proj", "s.jsonl"), rows)
        blk = self.scan.active_block(root=self.projects)
        self.assertIsNotNone(blk)
        self.assertAlmostEqual(blk["cost"], 25.0, places=6)

    def test_idle_returns_none(self):
        write(os.path.join(self.projects, "proj", "s.jsonl"),
              [msg("claude-opus-5", "old", out=1_000_000, ts=self._at(40))])
        self.assertIsNone(self.scan.active_block(root=self.projects))

    def test_block_sums_across_projects(self):
        write(os.path.join(self.projects, "p1", "a.jsonl"),
              [msg("claude-opus-5", "a", out=1_000_000, ts=self._at(1))])
        write(os.path.join(self.projects, "p2", "b.jsonl"),
              [msg("claude-opus-5", "b", out=1_000_000, ts=self._at(1))])
        blk = self.scan.active_block(root=self.projects)
        self.assertAlmostEqual(blk["cost"], 50.0, places=6)

    def test_block_includes_nested_subagents(self):
        write(os.path.join(self.projects, "p1", "a.jsonl"),
              [msg("claude-opus-5", "a", out=1_000_000, ts=self._at(1))])
        write(os.path.join(self.projects, "p1", "a", "subagents", "agent-x.jsonl"),
              [msg("claude-sonnet-5", "b", out=1_000_000, ts=self._at(1))])
        blk = self.scan.active_block(root=self.projects)
        self.assertAlmostEqual(blk["cost"], 35.0, places=6)


class TestRender(Base):
    def _run(self, sid, transcript, env=None):
        e = dict(os.environ)
        e["CBM_NO_NETWORK"] = "1"
        e.update(env or {})
        r = subprocess.run([sys.executable, os.path.join(LIB, "render.py"), sid, transcript],
                           capture_output=True, text=True, env=e)
        return r

    def test_panel_shows_cost_for_new_model(self):
        sid = "abc12345-0000-0000-0000-000000000000"
        path = os.path.join(self.projects, "proj", sid + ".jsonl")
        write(path, [msg("claude-opus-5", "m1", out=1_000_000)])
        r = self._run(sid, path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("$25.00", r.stdout)
        self.assertIn("opus-5", r.stdout)
        self.assertNotIn("$0.00", r.stdout)

    def test_panel_lists_subagents(self):
        sid = "abc12345-0000-0000-0000-000000000000"
        path = os.path.join(self.projects, "proj", sid + ".jsonl")
        write(path, [msg("claude-fable-5", "m1", out=1_000_000)])
        write(os.path.join(self.projects, "proj", sid, "subagents", "agent-a.jsonl"),
              [{"type": "agent-setting", "agentName": "scout"},
               msg("claude-sonnet-5", "s1", out=1_000_000)])
        r = self._run(sid, path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("AGENTS", r.stdout)
        self.assertIn("scout", r.stdout)
        self.assertIn("sonnet-5", r.stdout)

    def test_panel_survives_missing_transcript(self):
        r = self._run("deadbeef-0000-0000-0000-000000000000", "/nonexistent/x.jsonl")
        self.assertEqual(r.returncode, 0)
        self.assertIn("waiting", r.stdout)

    def test_panel_survives_empty_transcript(self):
        sid = "abc12345-0000-0000-0000-000000000000"
        path = os.path.join(self.projects, "proj", sid + ".jsonl")
        write(path, [])
        r = self._run(sid, path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_estimated_models_are_flagged(self):
        sid = "abc12345-0000-0000-0000-000000000000"
        path = os.path.join(self.projects, "proj", sid + ".jsonl")
        write(path, [msg("claude-opus-9-20990101", "m1", out=1_000_000)])
        r = self._run(sid, path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("estimated", r.stdout)

    def test_context_gauge_rendered(self):
        sid = "abc12345-0000-0000-0000-000000000000"
        path = os.path.join(self.projects, "proj", sid + ".jsonl")
        # 500K of a 1M window -> 50%
        write(path, [msg("claude-opus-5", "m1", cache_read=500_000, out=100)])
        r = self._run(sid, path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ctx", r.stdout)
        self.assertIn("50%", r.stdout)

    def test_no_color_emits_no_ansi(self):
        sid = "abc12345-0000-0000-0000-000000000000"
        path = os.path.join(self.projects, "proj", sid + ".jsonl")
        write(path, [msg("claude-opus-5", "m1", out=1_000_000)])
        r = self._run(sid, path, {"NO_COLOR": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("\033[", r.stdout)
        self.assertIn("$25.00", r.stdout)      # content still present

    def test_no_line_overflows_pane_width(self):
        """A narrow pane must degrade, not wrap. CBM_SIZE=25 makes this common."""
        import unicodedata
        sid = "abc12345-0000-0000-0000-000000000000"
        path = os.path.join(self.projects, "proj", sid + ".jsonl")
        write(path, [msg("claude-fable-5", "m1", inp=50_000, out=9_000,
                         cache_read=400_000, c1h=60_000)])
        write(os.path.join(self.projects, "proj", sid, "subagents", "agent-a.jsonl"),
              [{"type": "agent-setting", "agentName": "a-very-long-agent-name"},
               msg("claude-sonnet-5", "s1", out=1_000_000)])
        ansi = re.compile(r"\033\[[0-9;]*m")

        def width(s):
            s = ansi.sub("", s)
            return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

        for cols in (20, 24, 30, 34, 40, 46, 60, 72):
            r = self._run(sid, path, {"COLUMNS": str(cols)})
            self.assertEqual(r.returncode, 0, r.stderr)
            for ln in r.stdout.splitlines():
                self.assertLessEqual(
                    width(ln), cols,
                    "line overflows at COLUMNS=%d: %r" % (cols, ansi.sub("", ln)))

    def test_toggles_hide_sections(self):
        sid = "abc12345-0000-0000-0000-000000000000"
        path = os.path.join(self.projects, "proj", sid + ".jsonl")
        write(path, [msg("claude-fable-5", "m1", out=1_000_000)])
        write(os.path.join(self.projects, "proj", sid, "subagents", "agent-a.jsonl"),
              [msg("claude-sonnet-5", "s1", out=1_000_000)])
        r = self._run(sid, path, {"CBM_AGENTS": "0", "CBM_GRAPH": "0", "CBM_BLOCKS": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("AGENTS", r.stdout)
        self.assertNotIn("out/turn", r.stdout)


class TestResolver(Base):
    """Which session does a pane follow?

    Every case here is a way to show a confidently wrong number. A subagent
    transcript rendered as a session reports one agent's spend as the whole
    session's, which is plausible enough to be believed and quietly wrong.
    """

    def setUp(self):
        super(TestResolver, self).setUp()
        from usage import Resolver
        self.res = Resolver(self.scan, root=self.projects)

    def _session(self, proj, sid, cwd, rows=None, mtime=None):
        p = os.path.join(self.projects, proj, sid + ".jsonl")
        rows = rows or [msg("claude-opus-5", "m-" + sid, out=1000)]
        write(p, [{"type": "user", "cwd": cwd, "sessionId": sid}] + rows)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def _agent(self, proj, parent_sid, name, cwd, nested=True, mtime=None):
        if nested:
            p = os.path.join(self.projects, proj, parent_sid, "subagents",
                             "agent-%s.jsonl" % name)
        else:                       # ran with a different cwd -> sibling project
            p = os.path.join(self.projects, proj, "ag%s-0000-0000" % name + ".jsonl")
        write(p, [{"type": "agent-setting", "agentName": name, "cwd": cwd,
                   "teamName": "session-" + parent_sid[:8]},
                  msg("claude-sonnet-5", "a-" + name, out=500)])
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_normal_single_session(self):
        self._session("p1", "aaaa1111-0000-0000-0000-000000000000", "/work/one")
        sid, path = self.res.resolve(pwd="/work/one")
        self.assertTrue(sid.startswith("aaaa1111"))

    def test_agent_in_same_dir_is_not_chosen(self):
        """An agent transcript must never win over a real session."""
        now = time.time()
        self._session("p1", "aaaa1111-0000-0000-0000-000000000000", "/work/one",
                      mtime=now - 100)
        self._agent("p1", "aaaa1111-0000-0000-0000-000000000000", "helper",
                    "/work/one", nested=False, mtime=now)   # newer!
        sid, _ = self.res.resolve(pwd="/work/one")
        self.assertTrue(sid.startswith("aaaa1111"), "picked the agent: %s" % sid)

    def test_only_agent_present_resolves_to_parent(self):
        """The reported bug: agent ran with a different cwd, alone in that dir."""
        parent = "bbbb2222-0000-0000-0000-000000000000"
        self._session("home", parent, "/work/home")
        self._agent("elsewhere", parent, "researcher", "/work/elsewhere",
                    nested=False)
        sid, path = self.res.resolve(pwd="/work/elsewhere")
        self.assertTrue(sid.startswith("bbbb2222"),
                        "should follow the agent home to its session, got %s" % sid)
        self.assertNotIn("subagents", path)

    def test_nested_subagents_never_chosen(self):
        parent = "cccc3333-0000-0000-0000-000000000000"
        self._session("p1", parent, "/work/one")
        self._agent("p1", parent, "nested", "/work/one", nested=True,
                    mtime=time.time() + 50)
        sid, path = self.res.resolve(pwd="/work/one")
        self.assertTrue(sid.startswith("cccc3333"))
        self.assertNotIn("subagents", path)

    def test_cwd_match_beats_newer_session_elsewhere(self):
        now = time.time()
        self._session("p1", "aaaa1111-0000-0000-0000-000000000000", "/work/one",
                      mtime=now - 500)
        self._session("p2", "dddd4444-0000-0000-0000-000000000000", "/work/two",
                      mtime=now)          # newer, but wrong directory
        sid, _ = self.res.resolve(pwd="/work/one")
        self.assertTrue(sid.startswith("aaaa1111"))

    def test_two_sessions_same_cwd_picks_most_recent(self):
        now = time.time()
        self._session("p1", "aaaa1111-0000-0000-0000-000000000000", "/work/one",
                      mtime=now - 500)
        self._session("p1", "eeee5555-0000-0000-0000-000000000000", "/work/one",
                      mtime=now)
        sid, _ = self.res.resolve(pwd="/work/one")
        self.assertTrue(sid.startswith("eeee5555"))

    def test_unknown_cwd_falls_back_to_newest_session(self):
        now = time.time()
        self._session("p1", "aaaa1111-0000-0000-0000-000000000000", "/work/one",
                      mtime=now - 500)
        self._session("p2", "dddd4444-0000-0000-0000-000000000000", "/work/two",
                      mtime=now)
        sid, _ = self.res.resolve(pwd="/somewhere/never/seen")
        self.assertTrue(sid.startswith("dddd4444"))

    def test_no_transcripts_at_all(self):
        sid, path = self.res.resolve(pwd="/work/one")
        self.assertIsNone(sid)
        self.assertIsNone(path)

    def test_explicit_session_id_wins(self):
        self._session("p1", "aaaa1111-0000-0000-0000-000000000000", "/work/one")
        self._session("p2", "dddd4444-0000-0000-0000-000000000000", "/work/two")
        sid, _ = self.res.resolve(pwd="/work/one",
                                  sid="dddd4444-0000-0000-0000-000000000000")
        self.assertTrue(sid.startswith("dddd4444"))

    def test_cwd_with_spaces_and_unicode(self):
        """Path-to-directory-name transforms are undocumented; cwd matching isn't."""
        odd = "/Users/x/Idea Chest/proj (v2)/café"
        self._session("weird", "ffff6666-0000-0000-0000-000000000000", odd)
        sid, _ = self.res.resolve(pwd=odd)
        self.assertTrue(sid.startswith("ffff6666"))

    def test_trailing_slash_and_dotsegments_normalise(self):
        self._session("p1", "aaaa1111-0000-0000-0000-000000000000", "/work/one")
        for variant in ("/work/one/", "/work/./one", "/work/two/../one"):
            sid, _ = self.res.resolve(pwd=variant)
            self.assertTrue(sid and sid.startswith("aaaa1111"),
                            "failed to normalise %r" % variant)

    def test_orphan_agent_does_not_crash(self):
        """Agent whose parent transcript is gone: degrade, don't explode."""
        self._agent("p1", "99999999-0000-0000-0000-000000000000", "orphan",
                    "/work/one", nested=False)
        sid, path = self.res.resolve(pwd="/work/one")
        self.assertIsNone(sid)      # no session to point at, and that's honest

    def test_nested_orphan_resolves_by_path(self):
        """No teamName, but the directory layout still names the parent."""
        parent = "cccc3333-0000-0000-0000-000000000000"
        self._session("p1", parent, "/work/one")
        p = os.path.join(self.projects, "p1", parent, "subagents", "agent-x.jsonl")
        write(p, [msg("claude-sonnet-5", "nx", out=500)])   # no teamName at all
        info = self.res._info(p)
        self.assertTrue(info["agent"], "path under subagents/ must count as an agent")
        parent_info = self.res.parent_of(info)
        self.assertIsNotNone(parent_info)
        self.assertTrue(parent_info["sid"].startswith("cccc3333"))

    def test_agent_of_agent_walks_up_to_the_real_session(self):
        """A workflow coordinator is itself an agent, so one hop isn't enough."""
        session = "aaaa1111-0000-0000-0000-000000000000"
        mid = "bbbb2222-0000-0000-0000-000000000000"
        self._session("p1", session, "/work/top")
        # mid is an agent OF the session, and has its own children
        write(os.path.join(self.projects, "p2", mid + ".jsonl"),
              [{"type": "agent-setting", "agentName": "coordinator",
                "cwd": "/work/deep", "teamName": "session-" + session[:8]},
               msg("claude-opus-5", "mid1", out=1000)])
        write(os.path.join(self.projects, "p2", mid, "subagents", "agent-leaf.jsonl"),
              [{"type": "agent-setting", "agentName": "leaf", "cwd": "/work/deep"},
               msg("claude-sonnet-5", "leaf1", out=1000)])
        sid, path = self.res.resolve(pwd="/work/deep")
        self.assertTrue(sid.startswith("aaaa1111"),
                        "should climb past the intermediate agent, got %s" % sid)
        self.assertFalse(self.res._info(path)["agent"])

    def test_broken_parent_chain_never_returns_an_agent(self):
        """Parent transcript deleted: prefer an honest fallback over an agent."""
        orphan = "bbbb2222-0000-0000-0000-000000000000"
        # session at an ancestor directory, so there IS a sane answer
        self._session("p1", "aaaa1111-0000-0000-0000-000000000000", "/work")
        write(os.path.join(self.projects, "p2", orphan + ".jsonl"),
              [{"type": "agent-setting", "agentName": "stranded", "cwd": "/work/deep",
                "teamName": "session-deadbeef"},          # parent does not exist
               msg("claude-opus-5", "o1", out=1000)])
        write(os.path.join(self.projects, "p2", orphan, "subagents", "agent-c.jsonl"),
              [{"type": "agent-setting", "agentName": "child", "cwd": "/work/deep"},
               msg("claude-sonnet-5", "c1", out=1000)])
        sid, path = self.res.resolve(pwd="/work/deep")
        self.assertIsNotNone(sid)
        self.assertFalse(self.res._info(path)["agent"],
                         "resolved to an agent: %s" % sid)
        self.assertTrue(sid.startswith("aaaa1111"))

    def test_parent_chain_cycle_terminates(self):
        """Two agents naming each other must not hang the resolver."""
        a = "aaaa1111-0000-0000-0000-000000000000"
        b = "bbbb2222-0000-0000-0000-000000000000"
        write(os.path.join(self.projects, "p1", a + ".jsonl"),
              [{"type": "agent-setting", "agentName": "a", "cwd": "/w",
                "teamName": "session-" + b[:8]}, msg("claude-opus-5", "a1", out=10)])
        write(os.path.join(self.projects, "p1", b + ".jsonl"),
              [{"type": "agent-setting", "agentName": "b", "cwd": "/w",
                "teamName": "session-" + a[:8]}, msg("claude-opus-5", "b1", out=10)])
        sid, path = self.res.resolve(pwd="/w")     # must return, not spin
        self.assertIsNone(sid)

    def test_missing_projects_root(self):
        from usage import Resolver
        r = Resolver(self.scan, root=os.path.join(self.tmp, "does-not-exist"))
        sid, path = r.resolve(pwd="/work/one")
        self.assertIsNone(sid)


class TestAgentLabelling(Base):
    def test_agent_transcript_is_labelled_not_disguised(self):
        """If an agent is ever rendered, it must not read as the session."""
        parent = "bbbb2222-0000-0000-0000-000000000000"
        p = os.path.join(self.projects, "p1", "agent-strays.jsonl")
        write(p, [{"type": "agent-setting", "agentName": "pixverse-research",
                   "teamName": "session-" + parent[:8]},
                  msg("claude-sonnet-5", "a1", out=1_000_000)])
        e = dict(os.environ)
        e["CBM_NO_NETWORK"] = "1"
        e["COLUMNS"] = "60"
        r = subprocess.run([sys.executable, os.path.join(LIB, "render.py"),
                            "agent-strays", p], capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("agent", r.stdout)
        self.assertIn("pixverse-research", r.stdout)
        self.assertIn(parent[:8], r.stdout)


class TestToggle(Base):
    """The slash command's open/close/restart state machine (lib/common.sh).

    Runs against a mocked backend so no real terminal panes are created.
    """

    # No `set -e`: cbm_toggle_pane signals outcomes through non-zero exit codes
    # (3 = closed, 4 = restarted), which are successes, not failures.
    HARNESS = r'''
. "%(lib)s/common.sh"
# `iterm` (a real backend id) so cbm_state_backend/cbm_state_handle parse the
# state file for real; an unrecognised id degrades to legacy single-line mode.
cbm_backend()     { printf iterm; }
cbm_dispatch_ok() { return 0; }
cbm_open_iterm()  { printf 'HANDLE-NEW'; }
cbm_alive_iterm() { [ -n "$1" ]; }
cbm_close_iterm() { return 0; }
cbm_pane_alive()  { cbm_alive_iterm "$1"; }
cbm_pane_close()  { return 0; }
sid=testsess; trans=/tmp/x.jsonl
f="$(cbm_state_dir)/$sid.pane"
%(body)s
'''

    def _sh(self, body, home=None):
        script = self.HARNESS % {"lib": os.path.abspath(LIB), "body": body}
        e = dict(os.environ)
        e["XDG_STATE_HOME"] = os.path.join(self.tmp, "state")
        if home:
            # so the agent-transcript lookup searches the fixture tree
            e["HOME"] = home
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)
        return r.stdout.strip(), r

    def test_open_then_close_then_open(self):
        out, r = self._sh('''
cbm_toggle_pane "$sid" "$trans"; echo "rc1=$?"
cbm_toggle_pane "$sid" "$trans"; echo "rc2=$?"
cbm_toggle_pane "$sid" "$trans"; echo "rc3=$?"
''')
        self.assertIn("rc1=0", out, r.stderr)   # opened
        self.assertIn("rc2=3", out, r.stderr)   # closed
        self.assertIn("rc3=0", out, r.stderr)   # opened again

    def test_stale_version_restarts_instead_of_closing(self):
        """A pane launched by an older plugin version must be replaced, not closed."""
        out, r = self._sh('''
printf 'iterm\\nHANDLE-OLD\\n/some/old/plugin/0.7.0\\n' > "$f"
cbm_toggle_pane "$sid" "$trans"; echo "rc=$?"
echo "root=$(cbm_state_root "$f")"
''')
        self.assertIn("rc=4", out, r.stderr)
        self.assertNotIn("/some/old/plugin/0.7.0", out)

    def test_legacy_two_line_state_treated_as_stale(self):
        """State files written before version stamping have no root -> refresh."""
        out, r = self._sh('''
printf 'iterm\\nHANDLE-LEGACY\\n' > "$f"
cbm_toggle_pane "$sid" "$trans"; echo "rc=$?"
''')
        self.assertIn("rc=4", out, r.stderr)

    def test_state_file_records_plugin_root(self):
        out, r = self._sh('''
cbm_toggle_pane "$sid" "$trans" >/dev/null
echo "lines=$(wc -l < "$f" | tr -d ' ')"
echo "root=$(cbm_state_root "$f")"
''')
        self.assertIn("lines=3", out, r.stderr)
        self.assertIn("root=%s" % os.path.abspath(os.path.join(LIB, "..")), out)

    def test_pane_keyed_under_an_agent_id_is_closed_too(self):
        """The duplicate-pane report: resolution changed, orphaning the old pane.

        Panes are keyed by session id, so a pane opened for an *agent* of this
        session must still be recognised as this session's — otherwise opening
        leaves the user with two panes to close by hand.
        """
        proj = os.path.join(self.tmp, ".claude", "projects", "p1")
        os.makedirs(proj, exist_ok=True)
        agent_sid = "6c245910-0000-0000-0000-000000000000"
        write(os.path.join(proj, agent_sid + ".jsonl"),
              [{"type": "agent-setting", "agentName": "stray",
                "teamName": "session-testsess"},
               msg("claude-sonnet-5", "a1", out=1000)])
        out, r = self._sh('''
printf 'iterm\\nHANDLE-ORPHAN\\n/old/0.8.0\\n' > "$(cbm_state_dir)/%s.pane"
cbm_toggle_pane "$sid" "$trans"; echo "rc=$?"
ls "$(cbm_state_dir)" | grep -c pane | sed 's/^/panes=/'
''' % agent_sid, home=self.tmp)
        self.assertIn("rc=4", out, r.stderr)     # cleaned up + reopened
        self.assertIn("panes=1", out, r.stderr)  # exactly one pane remains

    def test_other_sessions_panes_are_left_alone(self):
        """Several Claude sessions run side by side; don't close their monitors."""
        out, r = self._sh('''
printf 'iterm\\nHANDLE-OTHER\\n%s\\n' "$(cbm_plugin_root)" > "$(cbm_state_dir)/other-session.pane"
cbm_toggle_pane "$sid" "$trans"; echo "rc=$?"
[ -f "$(cbm_state_dir)/other-session.pane" ] && echo "other=kept" || echo "other=CLOSED"
''')
        self.assertIn("rc=0", out, r.stderr)     # opened ours
        self.assertIn("other=kept", out, r.stderr)

    def test_dead_state_files_are_pruned(self):
        out, r = self._sh('''
cbm_alive_iterm() { case "$1" in HANDLE-DEAD*) return 1 ;; *) [ -n "$1" ] ;; esac; }
printf 'iterm\\nHANDLE-DEAD-1\\n%s\\n' "$(cbm_plugin_root)" > "$(cbm_state_dir)/gone-one.pane"
printf 'iterm\\nHANDLE-DEAD-2\\n%s\\n' "$(cbm_plugin_root)" > "$(cbm_state_dir)/gone-two.pane"
cbm_session_panes "$sid" >/dev/null
ls "$(cbm_state_dir)" 2>/dev/null | grep -c pane | sed 's/^/left=/'
''')
        self.assertIn("left=0", out, r.stderr)

    def test_dead_pane_reopens_rather_than_toggling_off(self):
        """If the recorded pane is gone (crash, manual close), open a fresh one."""
        out, r = self._sh('''
cbm_alive_iterm() { return 1; }
printf 'iterm\\nHANDLE-DEAD\\n%s\\n' "$(cbm_plugin_root)" > "$f"
cbm_toggle_pane "$sid" "$trans"; echo "rc=$?"
''')
        self.assertIn("rc=0", out, r.stderr)


class TestAgentSessionsGetNoPane(Base):
    """Teammates are FULL Claude Code sessions -- their own session id, their own
    transcript, their own SessionStart. Without a guard the monitor opens a pane
    for every teammate a session spawns, on top of the human's own pane.

    The hard part is that the check must stay silent when it cannot tell: a
    brand-new human session also has an empty transcript at SessionStart, and
    defaulting to "agent" there would stop the monitor ever opening.
    """

    def _transcript(self, name, rows):
        path = os.path.join(self.projects, "proj", name + ".jsonl")
        write(path, rows)
        return path

    def _is_agent(self, path):
        script = ('. "%s/common.sh"\ncbm_is_agent_session %s && echo YES || echo NO'
                  % (os.path.abspath(LIB), "'" + path + "'"))
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env=dict(os.environ,
                                    XDG_STATE_HOME=os.path.join(self.tmp, "state")))
        return r.stdout.strip() == "YES"

    # -- detection ----------------------------------------------------------
    def test_teammate_transcript_is_detected(self):
        """Exactly the shape Claude Code writes: `agent-setting` first, then a
        user line carrying teamName."""
        p = self._transcript("8d640dfc-0551-431f-bc45-f70835e642c0", [
            {"type": "agent-setting", "agentSetting": "general-purpose"},
            {"type": "mode", "mode": "normal"},
            {"type": "permission-mode", "permissionMode": "auto"},
            {"type": "user", "teamName": "session-ce7fc201",
             "agentName": "ball-track-research", "cwd": "/x"},
        ])
        self.assertTrue(self._is_agent(p))

    def test_subagent_path_is_detected_without_reading(self):
        p = os.path.join(self.projects, "proj", "sid", "subagents", "agent-a.jsonl")
        write(p, [msg("claude-opus-5", "m1", out=10)])
        self.assertTrue(self._is_agent(p))
        # Even if the file has since been removed, the path alone is conclusive.
        os.remove(p)
        self.assertTrue(self._is_agent(p))

    def test_ordinary_session_is_not_an_agent(self):
        p = self._transcript("abc12345-0000-0000-0000-000000000000", [
            {"type": "mode", "mode": "normal"},
            {"type": "user", "cwd": "/x", "gitBranch": "main"},
            msg("claude-opus-5", "m1", out=1000),
        ])
        self.assertFalse(self._is_agent(p))

    def test_empty_and_missing_transcripts_are_not_agents(self):
        """The critical non-regression: a fresh session's transcript is empty at
        SessionStart. Guessing "agent" here would break the whole plugin."""
        empty = self._transcript("fresh0000-0000-0000-0000-000000000000", [])
        self.assertFalse(self._is_agent(empty))
        self.assertFalse(self._is_agent(
            os.path.join(self.projects, "proj", "nope.jsonl")))
        self.assertFalse(self._is_agent(""))

    def test_agent_words_deep_in_conversation_do_not_false_positive(self):
        """A session that merely *talks* about teamName must still get a pane."""
        rows = [{"type": "user", "cwd": "/x"}] * 12
        rows.append({"type": "user", "message": {"role": "user",
                     "content": 'what does "teamName" mean in "agentSetting"?'}})
        p = self._transcript("chatty000-0000-0000-0000-000000000000", rows)
        self.assertFalse(self._is_agent(p))

    # -- the hook itself -----------------------------------------------------
    def _hook(self, sid, transcript):
        """Run the real SessionStart hook against a faked backend, and report
        which panes it recorded.

        The hook is copied into the sandbox and the *copied* `common.sh` gets the
        fake backend appended to it. That is not fussiness: exporting mock shell
        functions into the environment does NOT work here, because open-pane.sh
        sources common.sh, which redefines them and silently restores the real
        AppleScript. An earlier version of this test did exactly that and opened
        real iTerm panes on the developer's desktop. Patching the file the hook
        actually sources makes touching a real terminal impossible.
        """
        sandbox = os.path.join(self.tmp, "plugin")
        if not os.path.isdir(sandbox):
            src = os.path.abspath(os.path.join(HERE, ".."))
            os.makedirs(sandbox, exist_ok=True)
            for sub in ("bin", "lib"):
                shutil.copytree(os.path.join(src, sub), os.path.join(sandbox, sub))
            with open(os.path.join(sandbox, "lib", "common.sh"), "a") as f:
                f.write("\n# ---- test backend: never touches a real terminal ----\n"
                        "cbm_backend()     { printf iterm; }\n"
                        "cbm_dispatch_ok() { return 0; }\n"
                        "cbm_open_iterm()  { printf 'HANDLE-FAKE'; }\n"
                        "cbm_alive_iterm() { return 1; }\n"
                        "cbm_close_iterm() { return 0; }\n"
                        "cbm_pane_alive()  { return 1; }\n"
                        "cbm_pane_close()  { return 0; }\n")
        payload = json.dumps({"session_id": sid, "transcript_path": transcript,
                              "source": "startup", "cwd": "/x"})
        state = os.path.join(self.tmp, "state")
        subprocess.run(["bash", os.path.join(sandbox, "bin", "open-pane.sh")],
                       input=payload, capture_output=True, text=True,
                       env=dict(os.environ, XDG_STATE_HOME=state))
        panes = os.path.join(state, "ccusage-backpack-monitor")
        if not os.path.isdir(panes):
            return []
        return sorted(f for f in os.listdir(panes) if f.endswith(".pane"))

    def test_the_hook_harness_cannot_reach_a_real_terminal(self):
        """Guard the guard: prove the sandboxed hook uses the fake backend.

        If this ever fails, the suite is capable of opening panes on a real
        desktop again -- which it once did.
        """
        sid = "abc12345-0000-0000-0000-000000000000"
        p = self._transcript(sid, [{"type": "user", "cwd": "/x"}])
        self._hook(sid, p)
        state = os.path.join(self.tmp, "state", "ccusage-backpack-monitor")
        with open(os.path.join(state, sid + ".pane")) as f:
            self.assertIn("HANDLE-FAKE", f.read(),
                          "the hook used a REAL backend, not the test double")

    def test_hook_opens_nothing_for_a_teammate(self):
        sid = "8d640dfc-0551-431f-bc45-f70835e642c0"
        p = self._transcript(sid, [
            {"type": "agent-setting", "agentSetting": "general-purpose"},
            {"type": "user", "teamName": "session-ce7fc201",
             "agentName": "ball-track-research"},
        ])
        self.assertEqual(self._hook(sid, p), [],
                         "a teammate session must not get its own monitor pane")

    def test_hook_still_opens_for_a_real_session(self):
        sid = "abc12345-0000-0000-0000-000000000000"
        p = self._transcript(sid, [{"type": "user", "cwd": "/x"}])
        self.assertEqual(self._hook(sid, p), [sid + ".pane"])

    def test_hook_still_opens_for_a_brand_new_empty_session(self):
        sid = "fresh0000-0000-0000-0000-000000000000"
        p = self._transcript(sid, [])
        self.assertEqual(self._hook(sid, p), [sid + ".pane"])
class TestAccount(Base):
    """The account/limit caches are private client internals with no
    compatibility promise, so the contract is: read what's there, mark how old
    it is, and never raise."""

    def _acct(self, **kw):
        import account
        self.write_account(**kw)
        return account.Account(home=os.environ["HOME"])

    def test_plan_label_carries_the_multiplier(self):
        import account
        cases = [
            ({"organizationRateLimitTier": "default_claude_max_5x",
              "organizationType": "claude_max"}, "Max 5x"),
            ({"organizationRateLimitTier": "default_claude_max_20x"}, "Max 20x"),
            ({"organizationType": "claude_pro"}, "Pro"),
            ({"organizationType": "claude_team"}, "Team"),
            ({"organizationType": "claude_enterprise"}, "Ent"),
            ({"billingType": "api"}, "API"),
            ({}, ""),
        ]
        for oauth, want in cases:
            self.assertEqual(account.plan_label(oauth), want, oauth)

    def test_missing_file_is_not_an_error(self):
        import account
        a = account.Account(home=os.path.join(self.tmp, "nope"))
        self.assertIsNone(a.identity())
        self.assertIsNone(a.limits())
        self.assertIsNone(a.history())

    def test_corrupt_file_is_not_an_error(self):
        import account
        with open(os.path.join(os.environ["HOME"], ".claude.json"), "w") as f:
            f.write("{not json at all")
        a = account.Account(home=os.environ["HOME"])
        self.assertIsNone(a.identity())
        self.assertIsNone(a.limits())

    def test_unknown_shape_hides_rows_rather_than_raising(self):
        """A future Claude Code renaming these keys must degrade, not crash."""
        import account
        with open(os.path.join(os.environ["HOME"], ".claude.json"), "w") as f:
            json.dump({"oauthAccount": "not-a-dict",
                       "cachedUsageUtilization": {"utilization": [1, 2, 3]}}, f)
        a = account.Account(home=os.environ["HOME"])
        self.assertIsNone(a.identity())
        self.assertIsNone(a.limits())

    def test_limits_are_parsed_and_sorted_hottest_first(self):
        a = self._acct(limits=[
            self.limit_row("session", "session", 12),
            self.limit_row("weekly_scoped", "weekly", 21, scope="Fable"),
            self.limit_row("weekly_scoped", "weekly", 63, scope="Opus"),
        ])
        lim = a.limits()
        self.assertEqual(round(lim["session"]["percent"]), 12)
        self.assertEqual([round(r["percent"]) for r in lim["weekly"]], [63, 21])
        self.assertEqual(lim["weekly"][0]["scope"], "Opus")
        self.assertLess(lim["age"], 60)

    def test_absent_percentage_is_not_reported_as_zero(self):
        """A limit the server didn't quantify must not render as an empty
        meter -- that reads as 'you have used none of it', which is a claim."""
        a = self._acct(limits=[{"kind": "weekly_scoped", "group": "weekly",
                                "percent": None, "resets_at": None}])
        lim = a.limits()
        self.assertEqual(lim["weekly"], [])
        self.assertIsNone(lim["session"])

    def test_stale_cache_is_reported_by_age(self):
        import account
        old = int((time.time() - 3600) * 1000)
        a = self._acct(limits=[self.limit_row("session", "session", 5)],
                       fetched_ms=old)
        self.assertGreater(a.limits()["age"], account.STALE_AFTER)

    def test_legacy_five_hour_block_still_read(self):
        """An older client wrote only `five_hour`; keep reading it."""
        import account
        conf = {"cachedUsageUtilization": {
            "fetchedAtMs": int(time.time() * 1000),
            "utilization": {"five_hour": {"utilization": 42,
                                          "resets_at": "2026-08-04T10:20:00+00:00"}}}}
        with open(os.path.join(os.environ["HOME"], ".claude.json"), "w") as f:
            json.dump(conf, f)
        lim = account.Account(home=os.environ["HOME"]).limits()
        self.assertEqual(round(lim["session"]["percent"]), 42)

    def test_email_and_org_are_withheld_by_default(self):
        """This pane gets screen-shared; identity is opt-in."""
        a = self._acct(limits=[])
        os.environ.pop("CBM_ACCOUNT", None)
        ident = a.identity()
        self.assertEqual(ident["name"], "Testy")
        self.assertEqual(ident["email"], "")
        self.assertEqual(ident["org"], "")
        self.assertEqual(ident["plan"], "Max 5x")
        try:
            os.environ["CBM_ACCOUNT"] = "full"
            ident = a.identity()
            self.assertEqual(ident["email"], "testy@example.com")
            self.assertEqual(ident["org"], "Testy's Org")
        finally:
            os.environ.pop("CBM_ACCOUNT", None)

    def test_severity_from_server_outranks_the_percentage(self):
        import account
        self.assertEqual(account.severity_rank({"percent": 3, "severity": "warning"}), 1)
        self.assertEqual(account.severity_rank({"percent": 3, "severity": "critical"}), 2)
        self.assertEqual(account.severity_rank({"percent": 95, "severity": ""}), 2)
        self.assertEqual(account.severity_rank({"percent": 10, "severity": ""}), 0)

    def test_history_totals_and_staleness(self):
        import account
        a = self._acct(limits=[], stats={
            "lastComputedDate": "1999-01-01",
            "totalSessions": 145, "totalMessages": 44164,
            "firstSessionDate": "2026-06-24T14:58:31.893Z",
            "dailyModelTokens": [
                {"date": "2026-08-02", "tokensByModel": {"claude-opus-5": 10}},
                {"date": "2026-08-03", "tokensByModel": {"claude-fable-5": 90,
                                                         "claude-opus-5": 10}}]})
        h = a.history()
        self.assertEqual(h["sessions"], 145)
        self.assertEqual(h["days"][-1]["tokens"], 100)
        self.assertEqual(h["days"][-1]["models"][0], "claude-fable-5")
        self.assertTrue(h["stale"])


class TestTabs(Base):
    """Every tab must render, fit the pane, and survive missing data."""

    TABS = ["live", "limits", "models", "agents", "trend", "account"]

    def _session(self, sid="abc12345-0000-0000-0000-000000000000"):
        # Under the sandbox HOME, because the account-wide 5h block scans
        # ~/.claude/projects -- a fixture parked anywhere else is invisible to it.
        root = os.path.join(os.environ["HOME"], ".claude", "projects")
        path = os.path.join(root, "proj", sid + ".jsonl")
        rows = [{"type": "ai-title", "aiTitle": "a test session"},
                {"type": "permission-mode", "permissionMode": "auto"}]
        # "Now", so the rolling 5h block is actually active -- a fixture dated
        # in the past silently skips every burn-rate assertion below.
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        m = msg("claude-opus-5", "m1", inp=50_000, out=9_000,
                cache_read=400_000, c1h=60_000, ts=now_iso)
        m["gitBranch"] = "main"
        m["version"] = "2.1.221"
        m["effort"] = "xhigh"
        m["cwd"] = "/Users/x/projects/thing"
        rows.append(m)
        write(path, rows)
        write(os.path.join(root, "proj", sid, "subagents", "agent-a.jsonl"),
              [{"type": "agent-setting", "agentName": "scout"},
               msg("claude-sonnet-5", "s1", out=1_000_000, ts=now_iso)])
        return sid, path

    def _run(self, sid, path, tab="", env=None):
        e = dict(os.environ)
        e["CBM_NO_NETWORK"] = "1"
        e.update(env or {})
        return subprocess.run(
            [sys.executable, os.path.join(LIB, "render.py"), sid, path, str(tab)],
            capture_output=True, text=True, env=e)

    def test_every_tab_renders(self):
        sid, path = self._session()
        self.write_account(limits=[
            self.limit_row("session", "session", 12),
            self.limit_row("weekly_scoped", "weekly", 21, scope="Fable")],
            stats={"lastComputedDate": "2026-08-03", "totalSessions": 3,
                   "totalMessages": 10, "firstSessionDate": "2026-06-24",
                   "dailyModelTokens": [{"date": "2026-08-03",
                                         "tokensByModel": {"claude-opus-5": 5}}]})
        expect = {"live": "MODELS", "limits": "RATE LIMITS", "models": "MODELS",
                  "agents": "AGENTS", "trend": "7 DAYS", "account": "ACCOUNT"}
        for tab in self.TABS:
            r = self._run(sid, path, tab)
            self.assertEqual(r.returncode, 0, "%s: %s" % (tab, r.stderr))
            self.assertIn(expect[tab], r.stdout, tab)
            # The pinned header is on every tab -- that's the whole point.
            self.assertIn("ctx", r.stdout, tab)
            self.assertIn("$", r.stdout, tab)

    def test_tab_accepts_index_or_name_and_never_fails(self):
        sid, path = self._session()
        for arg in ("1", "limits", "99", "-3", "bogus", ""):
            r = self._run(sid, path, arg)
            self.assertEqual(r.returncode, 0, "%r: %s" % (arg, r.stderr))

    def test_no_tab_line_overflows_pane_width(self):
        """Same guarantee as the base panel, now across all six views."""
        import unicodedata
        sid, path = self._session()
        self.write_account(limits=[
            self.limit_row("session", "session", 100),
            self.limit_row("weekly_scoped", "weekly", 21, scope="Fable"),
            self.limit_row("weekly_scoped", "weekly", 99, scope="Opus 4.8 Long Name")],
            stats={"lastComputedDate": "2026-08-03", "totalSessions": 145,
                   "totalMessages": 44164, "firstSessionDate": "2026-06-24",
                   "dailyModelTokens": [{"date": "2026-08-0%d" % i,
                                         "tokensByModel": {"claude-opus-5": i * 1e6}}
                                        for i in range(1, 8)]})
        ansi = re.compile(r"\033\[[0-9;]*m")

        def width(s):
            s = ansi.sub("", s)
            return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

        for cols in (20, 24, 30, 34, 40, 46, 60, 72):
            for tab in self.TABS:
                r = self._run(sid, path, tab, {"COLUMNS": str(cols)})
                self.assertEqual(r.returncode, 0, r.stderr)
                for ln in r.stdout.splitlines():
                    self.assertLessEqual(
                        width(ln), cols,
                        "tab %s overflows at COLUMNS=%d: %r"
                        % (tab, cols, ansi.sub("", ln)))

    def test_no_row_ends_in_dead_space(self):
        """A dropped tail must take its separator with it.

        Trailing padding is invisible until you select the pane to copy a
        number out of it, and then every line has a ragged tail.
        """
        sid, path = self._session()
        self.write_account(limits=[self.limit_row("session", "session", 12)])
        for cols in (20, 28, 34, 60, 72):
            for tab in self.TABS:
                r = self._run(sid, path, tab, {"COLUMNS": str(cols),
                                               "NO_COLOR": "1"})
                for ln in r.stdout.splitlines():
                    self.assertEqual(ln, ln.rstrip(),
                                     "tab %s @%d: %r" % (tab, cols, ln))

    def test_no_color_across_tabs(self):
        sid, path = self._session()
        self.write_account(limits=[self.limit_row("session", "session", 12)])
        for tab in self.TABS:
            r = self._run(sid, path, tab, {"NO_COLOR": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("\033[", r.stdout, tab)

    def test_active_tab_is_marked_without_color(self):
        sid, path = self._session()
        r = self._run(sid, path, "models", {"NO_COLOR": "1", "COLUMNS": "60"})
        self.assertIn("[3 models]", r.stdout)
        self.assertIn("2 limits", r.stdout)          # inactive: no brackets
        self.assertNotIn("[2 limits]", r.stdout)

    def test_tab_numbers_are_visible(self):
        """The 1-6 shortcut is only useful if the strip advertises it."""
        sid, path = self._session()
        r = self._run(sid, path, "live", {"NO_COLOR": "1", "COLUMNS": "60"})
        strip = [ln for ln in r.stdout.splitlines() if "account" in ln][0]
        for i, name in enumerate(self.TABS, start=1):
            self.assertIn("%d %s" % (i, name), strip)

    def test_tabs_off_restores_the_static_panel(self):
        sid, path = self._session()
        r = self._run(sid, path, "3", {"CBM_TABS": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MODELS", r.stdout)          # forced back to the live view
        self.assertNotIn("live limits", r.stdout)  # no strip
        self.assertIn("Ctrl-C", r.stdout)

    def test_limits_absent_degrades_quietly(self):
        """No cache (fresh machine, or a rename upstream) must not break it."""
        sid, path = self._session()
        for tab in self.TABS:
            r = self._run(sid, path, tab)
            self.assertEqual(r.returncode, 0, "%s: %s" % (tab, r.stderr))
        r = self._run(sid, path, "live")
        self.assertNotIn("week", r.stdout)
        # Without the server's 5h percentage, the local dollar figure is the
        # only 5h number there is, so it must come back.
        self.assertIn("5h", r.stdout)

    def test_stale_limits_are_marked(self):
        sid, path = self._session()
        self.write_account(limits=[self.limit_row("session", "session", 12)],
                           fetched_ms=int((time.time() - 7200) * 1000))
        r = self._run(sid, path, "live", {"COLUMNS": "60"})
        self.assertIn("~12%", r.stdout)

    def test_account_identity_is_not_leaked_by_default(self):
        sid, path = self._session()
        self.write_account(limits=[])
        r = self._run(sid, path, "account", {"COLUMNS": "60"})
        self.assertIn("Testy", r.stdout)
        self.assertNotIn("testy@example.com", r.stdout)
        r = self._run(sid, path, "account", {"COLUMNS": "60", "CBM_ACCOUNT": "full"})
        self.assertIn("testy@example.com", r.stdout)

    def test_account_off_hides_it_everywhere(self):
        sid, path = self._session()
        self.write_account(limits=[])
        for tab in self.TABS:
            r = self._run(sid, path, tab, {"CBM_ACCOUNT": "0", "COLUMNS": "60"})
            self.assertNotIn("Testy", r.stdout, tab)

    def test_session_identity_reaches_the_panel(self):
        sid, path = self._session()
        r = self._run(sid, path, "account", {"COLUMNS": "60"})
        for want in ("a test session", "main", "xhigh", "auto", "2.1.221"):
            self.assertIn(want, r.stdout, want)

    def test_cache_hit_rate_is_shown(self):
        sid, path = self._session()
        r = self._run(sid, path, "live", {"COLUMNS": "60"})
        self.assertIn("cache", r.stdout)

    def test_a_clock_is_never_shown_without_saying_it_is_a_reset(self):
        """The reported misread: `week ~21% Fable Wed 10:30`.

        A scope name butted against a bare clock parses as one meaningless
        blob. Wherever there is room, the reset time is a sentence.
        """
        sid, path = self._session()
        self.write_account(limits=[
            self.limit_row("session", "session", 12),
            self.limit_row("weekly_scoped", "weekly", 21, scope="Fable")])
        for tab in ("live", "limits"):
            r = self._run(sid, path, tab, {"COLUMNS": "60", "NO_COLOR": "1"})
            wk = [ln for ln in r.stdout.splitlines() if ln.startswith("week")]
            self.assertTrue(wk, "no weekly row on %s" % tab)
            self.assertIn("Fable · resets", wk[0], tab)

    def test_headline_says_what_it_is_the_cost_of(self):
        """A bare dollar figure got read as the 5h window and as the account."""
        sid, path = self._session()
        r = self._run(sid, path, "live", {"COLUMNS": "60", "NO_COLOR": "1"})
        self.assertRegex(r.stdout, r"^session\s+\$")

    def test_burn_numbers_are_labelled(self):
        sid, path = self._session()
        r = self._run(sid, path, "live", {"COLUMNS": "60", "NO_COLOR": "1"})
        for want in ("BURN", "rate", "on track", "window"):
            self.assertIn(want, r.stdout, want)


def _cpu_seconds(pid):
    """CPU seconds a process has used, via `ps`. None if it can't be read."""
    try:
        out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return None
    if ":" not in out:
        return None
    try:
        head, ss = out.rsplit(":", 1)          # [dd-]hh:mm:ss.ss or mm:ss.ss
        mins = int(head.split(":")[-1] or 0)
        return mins * 60 + float(ss)
    except ValueError:
        return None


class TestWatchKeys(unittest.TestCase):
    """The interactive loop, driven through a real pty.

    Two properties matter more than the key mapping itself: it must not poll
    faster than before, and it must not spin when there is no terminal.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cbm-keys-")
        self.home = os.path.join(self.tmp, "home")
        self.projects = os.path.join(self.home, ".claude", "projects", "proj")
        os.makedirs(self.projects, exist_ok=True)
        self.sid = "abc12345-0000-0000-0000-000000000000"
        self.path = os.path.join(self.projects, self.sid + ".jsonl")
        write(self.path, [msg("claude-opus-5", "m1", out=1_000_000)])
        self.watch = os.path.join(HERE, "..", "bin", "watch.sh")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self):
        e = dict(os.environ)
        e.update({"HOME": self.home, "COLUMNS": "60", "LINES": "40",
                  "CBM_NO_NETWORK": "1", "TERM": "xterm-256color",
                  "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                  "XDG_CONFIG_HOME": os.path.join(self.tmp, "config")})
        return e

    def _drive(self, keys, settle=1.2, env=None):
        """Run the watcher on a pty, send `keys`, return everything it drew."""
        import pty, select, signal
        pid, fd = pty.fork()
        if pid == 0:                                   # child: the watcher
            os.environ.update(self._env())
            os.environ.update(env or {})
            try:
                os.execv("/bin/sh", ["sh", self.watch, self.sid, self.path, "1"])
            finally:
                os._exit(1)
        buf = b""

        def pump(seconds):
            nonlocal buf
            end = time.time() + seconds
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    try:
                        buf += os.read(fd, 65536)
                    except OSError:
                        return
        try:
            pump(settle)
            for k in keys:
                os.write(fd, k)
                pump(0.8)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
            os.close(fd)
        return buf.decode(errors="ignore")

    def test_right_arrow_switches_tab(self):
        out = self._drive([b"\033[C"])
        self.assertIn("RATE LIMITS", out)

    def test_application_cursor_mode_arrow_also_works(self):
        """tmux and some terminals send ESC O C instead of ESC [ C."""
        out = self._drive([b"\033OC"])
        self.assertIn("RATE LIMITS", out)

    def test_number_key_jumps(self):
        # Tab 4 is `agents`, and this fixture has none -- so that body's text
        # appears nowhere else, which makes it an unambiguous signal.
        out = self._drive([b"4"])
        self.assertIn("none in this session", out)

    def test_left_arrow_wraps_to_the_last_tab(self):
        out = self._drive([b"\033[D"])
        self.assertIn("ACCOUNT", out)

    def test_help_key_shows_and_then_clears(self):
        out = self._drive([b"?", b"3"])
        self.assertIn("jump to tab", out)

    def test_unknown_key_is_ignored(self):
        out = self._drive([b"Z"])
        self.assertIn("MODELS", out)
        self.assertNotIn("RATE LIMITS", out)

    def test_redraws_do_not_pile_up_in_scrollback(self):
        """The ghost-frame report: scroll up, find the panel again, older.

        Erasing the primary buffer scrolls the erased frame into scrollback, so
        a redraw-in-place panel silently archives every frame it ever drew. The
        fix is to own the alternate buffer, which has no scrollback at all --
        and never to clear the primary one while we're on it.
        """
        out = self._drive([b"\033[C", b"\033[C"])
        self.assertIn("\033[?1049h", out, "never entered the alternate buffer")
        self.assertIn("MODELS", out)
        # ED2 on the primary buffer is precisely what banks the ghost frame.
        body = out.split("\033[?1049h", 1)[1]
        self.assertNotIn("\033[2J", body, "cleared the primary buffer while on alt")

    def test_dropping_to_a_shell_hands_the_screen_back(self):
        """Leaving the panel must restore the buffer and the cursor, or the
        shell it execs into inherits an invisible cursor on a screen the user
        cannot scroll."""
        out = self._drive([b"q"], env={"SHELL": "/bin/sh"})
        self.assertIn("\033[?1049l", out)
        self.assertIn("\033[?25h", out)

    def test_altscreen_opt_out_still_clears_scrollback(self):
        """Terminals without the alternate buffer must not ghost either.

        Order is the whole fix. `clear(1)` emits ESC[3J ESC[H ESC[2J -- it drops
        the scrollback and *then* banks the frame it just erased, which is how
        exactly one ghost copy survives every redraw. The scrollback wipe has to
        come last.
        """
        out = self._drive([b"\033[C"], env={"CBM_ALTSCREEN": "0"})
        self.assertNotIn("\033[?1049h", out)
        self.assertIn("\033[3J", out, "left the ghost frame in scrollback")
        self.assertGreater(out.rfind("\033[3J"), out.rfind("\033[2J"),
                           "erased the screen after dropping scrollback, "
                           "which re-banks the frame it just erased")

    def test_no_tty_does_not_spin(self):
        """Without a terminal, `read` fails instantly -- if that became the
        loop's only pause it would burn a core. It must fall back to sleep."""
        e = self._env()
        # stdout to /dev/null, not a pipe: an undrained pipe would fill and block
        # the watcher, which would make "it used no CPU" true for the wrong reason.
        p = subprocess.Popen(["/bin/sh", self.watch, self.sid, self.path, "1"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=e)
        try:
            time.sleep(3.0)
            cpu = _cpu_seconds(p.pid)
            if cpu is None:
                self.skipTest("ps unavailable")
            self.assertLess(cpu, 1.0, "watcher spun without a tty (%.2fs CPU)" % cpu)
        finally:
            p.kill()
            p.wait()

    def test_idle_with_a_tty_costs_nothing(self):
        """The read replaces the sleep; it must not poll any harder.

        The pty is drained throughout. Without that the watcher would block on a
        full buffer, use no CPU for the obvious wrong reason, and the assertion
        would pass while proving nothing.
        """
        import pty, select, signal
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(self._env())
            try:
                os.execv("/bin/sh", ["sh", self.watch, self.sid, self.path, "1"])
            finally:
                os._exit(1)
        try:
            end = time.time() + 4.0
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        os.read(fd, 65536)
                    except OSError:
                        break
            cpu = _cpu_seconds(pid)
            if cpu is None:
                self.skipTest("ps unavailable")
            # Four seconds of wall clock, four poll intervals. A busy loop would
            # be seconds of CPU; a correct one is milliseconds.
            self.assertLess(cpu, 1.0, "idle watcher burned %.2fs CPU" % cpu)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
            os.close(fd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
