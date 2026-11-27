"""Offline tests for hs-caretaker. Run: python3 test_caretaker.py"""
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

here = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("caretaker", os.path.join(here, "hs-caretaker"))
spec = importlib.util.spec_from_loader("caretaker", loader)
ct = importlib.util.module_from_spec(spec)
loader.exec_module(ct)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeApi:
    def __init__(self, agents):
        self.agents, self.posts = agents, []

    def call(self, method, path, params=None, body=None):
        if method == "POST":
            self.posts.append(path)
            return {}
        if path == "/api/ops/fleet/status":
            return {"agents": self.agents}
        if path == "/api/executions":
            old = (NOW - timedelta(hours=5)).isoformat()
            new = (NOW - timedelta(minutes=5)).isoformat()
            return [{"id": "e1", "agent_name": "agent-a", "started_at": old},
                    {"id": "e2", "agent_name": "agent-a", "started_at": new}]
        if path == "/api/ops/schedules":
            return {"schedules": [
                {"id": "s1", "agent_name": "agent-a", "name": "x", "enabled": True,
                 "last_execution": {"status": "failed"}},
                {"id": "s2", "agent_name": "agent-b", "name": "y", "enabled": False,
                 "last_execution": {"status": "failed"}}]}
        raise AssertionError(path)


def cfg(**kw):
    c = dict(ct.DEFAULTS, should_run=["agent-a", "agent-b", "agent-c"], **kw)
    return c


class Tests(unittest.TestCase):
    def test_checks(self):
        self.assertEqual(ct.find_stopped([{"name": "a", "status": "stopped"}, {"name": "b", "status": "running"}],
                                         {"a", "b"}, set()), ["a"])
        stuck = ct.find_stuck(FakeApi([]).call("GET", "/api/executions"), NOW, 120, set())
        self.assertEqual([s["id"] for s in stuck], ["e1"])

    def test_budget(self):
        left, recent = ct.restart_budget([NOW.timestamp() - 7200, NOW.timestamp() - 60], NOW.timestamp(), 2)
        self.assertEqual((left, len(recent)), (1, 1))

    def test_dry_run_does_not_post(self):
        api = FakeApi([{"name": "agent-a", "status": "stopped"}])
        with tempfile.TemporaryDirectory() as d:
            f = ct.run(cfg(), api, os.path.join(d, "s.json"), now=NOW)
        self.assertEqual(api.posts, [])
        self.assertIn("stopped:agent-a", f)
        self.assertIn("stuck:e1", f)
        self.assertIn("schedule:s1", f)
        self.assertNotIn("schedule:s2", f)  # disabled schedule is not reported

    def test_act_respects_hourly_cap(self):
        agents = [{"name": n, "status": "stopped"} for n in ("agent-a", "agent-b", "agent-c")]
        api = FakeApi(agents)
        with tempfile.TemporaryDirectory() as d:
            f = ct.run(cfg(act=True, max_restarts_per_hour=2), api, os.path.join(d, "s.json"), now=NOW)
        self.assertEqual(len(api.posts), 2)
        self.assertIn("restart cap reached", f["stopped:agent-c"])

    def test_unlisted_agent_never_started(self):
        api = FakeApi([{"name": "other", "status": "stopped"}])
        with tempfile.TemporaryDirectory() as d:
            ct.run(cfg(act=True), api, os.path.join(d, "s.json"), now=NOW)
        self.assertEqual(api.posts, [])

    def test_unknown_config_key_fails(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"acts": true}')
        with self.assertRaises(ValueError):
            ct.load_config(fh.name)
        os.unlink(fh.name)


if __name__ == "__main__":
    unittest.main()
