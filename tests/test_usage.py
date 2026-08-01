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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


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


class TestToggle(Base):
    """The slash command's open/close/restart state machine (lib/common.sh).

    Runs against a mocked backend so no real terminal panes are created.
    """

    # No `set -e`: cbm_toggle_pane signals outcomes through non-zero exit codes
    # (3 = closed, 4 = restarted), which are successes, not failures.
    HARNESS = r'''
. "%(lib)s/common.sh"
cbm_backend()     { printf mock; }
cbm_dispatch_ok() { return 0; }
cbm_open_mock()   { printf 'HANDLE-NEW'; }
cbm_alive_mock()  { [ -n "$1" ]; }
cbm_close_mock()  { return 0; }
cbm_pane_alive()  { cbm_alive_mock "$1"; }
cbm_pane_close()  { cbm_close_mock "$1"; }
sid=testsess; trans=/tmp/x.jsonl
f="$(cbm_state_dir)/$sid.pane"
%(body)s
'''

    def _sh(self, body):
        script = self.HARNESS % {"lib": os.path.abspath(LIB), "body": body}
        e = dict(os.environ)
        e["XDG_STATE_HOME"] = os.path.join(self.tmp, "state")
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
printf 'mock\\nHANDLE-OLD\\n/some/old/plugin/0.7.0\\n' > "$f"
cbm_toggle_pane "$sid" "$trans"; echo "rc=$?"
echo "root=$(cbm_state_root "$f")"
''')
        self.assertIn("rc=4", out, r.stderr)
        self.assertNotIn("/some/old/plugin/0.7.0", out)

    def test_legacy_two_line_state_treated_as_stale(self):
        """State files written before version stamping have no root -> refresh."""
        out, r = self._sh('''
printf 'mock\\nHANDLE-LEGACY\\n' > "$f"
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

    def test_dead_pane_reopens_rather_than_toggling_off(self):
        """If the recorded pane is gone (crash, manual close), open a fresh one."""
        out, r = self._sh('''
cbm_alive_mock() { return 1; }
printf 'mock\\nHANDLE-DEAD\\n%s\\n' "$(cbm_plugin_root)" > "$f"
cbm_toggle_pane "$sid" "$trans"; echo "rc=$?"
''')
        self.assertIn("rc=0", out, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
