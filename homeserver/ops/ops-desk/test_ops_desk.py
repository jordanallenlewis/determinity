"""Offline tests for hs-ops-desk classification and safety. Run: python3 test_ops_desk.py"""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

here = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("opsdesk", os.path.join(here, "hs-ops-desk"))
spec = importlib.util.spec_from_loader("opsdesk", loader)
od = importlib.util.module_from_spec(spec)
loader.exec_module(od)

NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
RULES = json.load(open(os.path.join(here, "rules.example.json")))["rules"]
ALLOW = {"auto-resolve"}


def item(**kw):
    base = {"id": "i1", "agent_name": "agent-a", "type": "alert", "priority": "low",
            "title": "x", "created_at": (NOW - timedelta(days=2)).isoformat()}
    base.update(kw)
    return base


class FakeApi:
    def __init__(self, items):
        self.items, self.posts = items, []

    def call(self, method, path, params=None, body=None):
        if method == "GET":
            return {"items": self.items, "has_more": False}
        self.posts.append((path, body))
        return {}


class Tests(unittest.TestCase):
    def test_example_rules_valid(self):
        od.validate_rules(RULES)

    def test_old_low_alert_auto_resolves(self):
        v, rule, _ = od.classify(item(), RULES, ALLOW, NOW)
        self.assertEqual((v, rule["name"]), ("auto-resolve", "old-low-alerts"))

    def test_young_alert_kept(self):
        v, _, why = od.classify(item(created_at=NOW.isoformat()), RULES, ALLOW, NOW)
        self.assertEqual((v, why), ("keep-for-human", "no rule matched"))

    def test_route_blocked_unless_allowed(self):
        it = item(type="credential", priority="normal", created_at=NOW.isoformat())
        self.assertEqual(od.classify(it, RULES, ALLOW, NOW)[0], "keep-for-human")
        self.assertEqual(od.classify(it, RULES, {"route"}, NOW)[0], "route")

    def test_approval_never_touched_even_with_catch_all_rule(self):
        catch_all = [{"name": "all", "match": {}, "verdict": "auto-resolve", "action": "cancel"}]
        for it in (item(type="approval"), item(gate={"state": "pending"}), item(proposal={"x": 1})):
            self.assertEqual(od.classify(it, catch_all, ALLOW, NOW)[0], "keep-for-human")

    def test_respond_requires_offered_option(self):
        rules = [{"name": "r", "match": {}, "verdict": "auto-resolve", "action": "respond", "response": "ok"}]
        self.assertEqual(od.classify(item(options=["ok", "no"]), rules, ALLOW, NOW)[0], "auto-resolve")
        self.assertEqual(od.classify(item(options=["no"]), rules, ALLOW, NOW)[0], "keep-for-human")

    def test_dry_run_posts_nothing_and_act_posts_reason(self):
        cfg = dict(od.DEFAULTS, rules=RULES)
        with tempfile.TemporaryDirectory() as d:
            api = FakeApi([item(), item(id="i2", type="approval")])
            od.run(cfg, api, os.path.join(d, "dec.jsonl"), now=NOW)
            self.assertEqual(api.posts, [])
            api = FakeApi([item(), item(id="i2", type="approval")])
            od.run(dict(cfg, act=True), api, os.path.join(d, "dec.jsonl"), now=NOW)
            self.assertEqual(len(api.posts), 1)
            self.assertTrue(api.posts[0][0].endswith("/i1/cancel"))
            self.assertTrue(api.posts[0][1]["reason"])
            self.assertEqual(len(open(os.path.join(d, "dec.jsonl")).readlines()), 4)

    def test_action_cap(self):
        cfg = dict(od.DEFAULTS, rules=RULES, act=True, max_actions_per_run=1)
        with tempfile.TemporaryDirectory() as d:
            api = FakeApi([item(id="a"), item(id="b")])
            od.run(cfg, api, os.path.join(d, "dec.jsonl"), now=NOW)
        self.assertEqual(len(api.posts), 1)


if __name__ == "__main__":
    unittest.main()
