"""Offline tests for hs-capguard bookkeeping. Run: python3 test_capguard.py"""
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest

here = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("capguard", os.path.join(here, "hs-capguard"))
spec = importlib.util.spec_from_loader("capguard", loader)
cg = importlib.util.module_from_spec(spec)
loader.exec_module(cg)

CFG = dict(cg.DEFAULTS, weekly_budget_usd=100.0, never_pause=["agent-a"])


class FakeApi:
    """cost is the fleet total; schedules mutate on enable/disable calls."""

    def __init__(self, cost, scheds):
        self.cost, self.scheds, self.posts = cost, scheds, []

    def call(self, method, path, params=None, body=None):
        if method == "POST":
            self.posts.append(path)
            sid, verb = path.split("/")[-2:][0], path.split("/")[-1]
            for s in self.scheds:
                if s["id"] == sid:
                    s["enabled"] = verb == "enable"
            return {}
        if path == "/api/executions/stats":
            return {"total_cost": self.cost}
        if path == "/api/ops/fleet/status":
            return {"agents": [{"name": "agent-a"}, {"name": "agent-b"}]}
        if path == "/api/ops/schedules":
            return {"schedules": self.scheds}
        raise AssertionError(path)


def scheds():
    return [{"id": "s1", "agent_name": "agent-a", "name": "keep", "enabled": True},
            {"id": "s2", "agent_name": "agent-b", "name": "nightly", "enabled": True},
            {"id": "s3", "agent_name": "agent-b", "name": "already-off", "enabled": False}]


class Tests(unittest.TestCase):
    def test_decide_hysteresis(self):
        self.assertEqual(cg.decide(95, False, CFG), "pause")
        self.assertEqual(cg.decide(80, True, CFG), "hold")
        self.assertEqual(cg.decide(70, True, CFG), "restore")
        self.assertEqual(cg.decide(70, False, CFG), "hold")

    def test_pause_then_restore_only_ours(self):
        with tempfile.TemporaryDirectory() as d:
            st = os.path.join(d, "s.json")
            api = FakeApi(95.0, scheds())
            self.assertEqual(cg.run(dict(CFG, act=True), api, st), "pause")
            self.assertEqual(len(api.posts), 1)            # only s2: s1 allowlisted, s3 already off
            self.assertTrue(api.posts[0].endswith("/agent-b/schedules/s2/disable"))
            api.cost = 50.0
            self.assertEqual(cg.run(dict(CFG, act=True), api, st), "restore")
            self.assertEqual(len(api.posts), 2)
            self.assertTrue(api.posts[1].endswith("/s2/enable"))
            self.assertFalse([s for s in api.scheds if s["id"] == "s3"][0]["enabled"])  # untouched
            self.assertEqual(cg.load_state(st)["paused"], {})

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            st = os.path.join(d, "s.json")
            api = FakeApi(95.0, scheds())
            cg.run(CFG, api, st)
            self.assertEqual(api.posts, [])
            self.assertEqual(cg.load_state(st)["paused"], {})

    def test_restore_skips_what_someone_else_changed(self):
        ledger = {"s2": {"agent": "agent-b", "name": "n"}, "s9": {"agent": "x", "name": "gone"}}
        enable, forget = cg.plan_restore(ledger, [{"id": "s2", "enabled": True, "agent_name": "agent-b"}])
        self.assertEqual((enable, sorted(forget)), ([], ["s2", "s9"]))

    def test_failed_pause_not_recorded(self):
        import urllib.error

        class Boom(FakeApi):
            def call(self, method, path, params=None, body=None):
                if method == "POST":
                    raise urllib.error.HTTPError(path, 500, "x", {}, None)
                return super().call(method, path, params, body)
        with tempfile.TemporaryDirectory() as d:
            st = os.path.join(d, "s.json")
            cg.run(dict(CFG, act=True), Boom(95.0, scheds()), st)
            self.assertEqual(cg.load_state(st)["paused"], {})

    def test_bad_config_fails_closed(self):
        with self.assertRaises(ValueError):
            cg.validate(dict(CFG, weekly_budget_usd=0))
        with self.assertRaises(ValueError):
            cg.validate(dict(CFG, restore_pct=95))


if __name__ == "__main__":
    unittest.main()
