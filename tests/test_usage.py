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
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects, exist_ok=True)
        self.pricing = Pricing(allow_refresh=False)
        self.scan = Scanner(self.pricing)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


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
        """Run the SessionStart hook against a mocked backend; report whether it
        opened a pane."""
        hook = os.path.join(HERE, "..", "bin", "open-pane.sh")
        payload = json.dumps({"session_id": sid, "transcript_path": transcript,
                              "source": "startup", "cwd": "/x"})
        # Mock the backend by pre-loading overrides, then exec the real hook.
        script = (
            'cbm_backend() { printf iterm; }\n'
            'cbm_dispatch_ok() { return 0; }\n'
            'cbm_open_iterm() { printf HANDLE-X; }\n'
            'cbm_alive_iterm() { return 1; }\n'
            'cbm_pane_alive() { return 1; }\n'
            'cbm_pane_close() { return 0; }\n'
            'export -f cbm_backend cbm_dispatch_ok cbm_open_iterm '
            'cbm_alive_iterm cbm_pane_alive cbm_pane_close\n'
            'printf %%s %s | bash %s\n' % (json.dumps(payload).replace("'", "'\\''"),
                                           os.path.abspath(hook)))
        state = os.path.join(self.tmp, "state")
        subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=dict(os.environ, XDG_STATE_HOME=state,
                                BASH_ENV="", CBM_BACKEND="iterm"))
        panes = os.path.join(state, "ccusage-backpack-monitor")
        if not os.path.isdir(panes):
            return []
        return [f for f in os.listdir(panes) if f.endswith(".pane")]

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
