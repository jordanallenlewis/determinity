#!/usr/bin/env python3
"""Offline tests for gateway.py. Trinity and the upstream are mocked. Run: python3 test_gateway.py"""
import hashlib
import json
import os
import tempfile
import unittest

import gateway

TOKEN_A, TOKEN_B = "token-for-agent-a", "token-for-agent-b"
def put(path, text):
    with open(path, "w") as fh:
        fh.write(text if isinstance(text, str) else json.dumps(text))


H = lambda t: hashlib.sha256(t.encode()).hexdigest()
ACTIONS = {
    "weather.read": {"kind": "read", "method": "GET", "url": "https://api.example.com/v1/forecast?lat={lat}&lon={lon}",
                     "credential": {"ref": "weather-key", "header": "X-Api-Key", "prefix": ""},
                     "params": {"lat": {"pattern": "-?[0-9.]{1,8}"}, "lon": {"pattern": "-?[0-9.]{1,8}"}}},
    "lights.set": {"kind": "write", "method": "POST", "url": "http://192.0.2.20/api/lights/{id}",
                   "credential": {"ref": "lights-token"}, "max_per_hour": 2,
                   "params": {"id": {"pattern": "[a-z0-9-]{1,32}"}, "state": {"pattern": "on|off"}}},
    "door.unlock": {"kind": "write", "method": "POST", "url": "http://198.51.100.30/api/{door}", "sensitive": True,
                    "params": {"door": {"pattern": "front|garage"}, "code": {"pattern": "[0-9]{4}", "secret": True}}},
}
ACL = {"agent-a": ["weather.read", "lights.set", "door.unlock"], "agent-b": ["weather.read"]}



def _all_strings(obj):
    """Every string inside a decoded JSON-like value (keys too), unescaped."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _all_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_strings(v)

class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        for name, obj in (("actions.json", ACTIONS), ("acl.json", ACL)):
            put(os.path.join(d, name), obj)
        put(os.path.join(d, "tokens"), "%s  agent-a\n%s  agent-b\n" % (H(TOKEN_A), H(TOKEN_B)))
        os.mkdir(os.path.join(d, "creds"))
        put(os.path.join(d, "creds", "weather-key"), "SECRET-WEATHER\n")
        put(os.path.join(d, "creds", "lights-token"), "SECRET-LIGHTS\n")
        self.cfg = {"tokens": d + "/tokens", "actions": d + "/actions.json", "acl": d + "/acl.json", "creds": d + "/creds",
                    "disable": d + "/gateway.disable", "ledger": d + "/used.json", "plans": d + "/plans.json", "trinity_url": "http://127.0.0.1:1",
                    "trinity_token_file": d + "/tt", "timeout": 5, "dry_run": False, "plan_ttl": 3600, "plan_max_per_hour": 20}
        self.gw = gateway.Gateway(self.cfg)
        self.sent, self.items, self.trinity_down, self.raised = [], {}, False, []
        self.gw.send = lambda m, u, h, b: (self.sent.append((m, u, h, b)) or (200, "application/json", b'{"ok":1}'))
        self.gw.trinity_get = self.fake_trinity
        self.gw.trinity_raise = self.fake_raise

    def tearDown(self):
        self.tmp.cleanup()

    def fake_trinity(self, aid):
        if self.trinity_down:
            raise OSError("connection refused")
        if aid not in self.items:
            raise OSError("404")
        return self.items[aid]

    def fake_raise(self, agent, ask):
        """Stands in for POST /api/agents/{agent}/operator-queue: stores the row the way Trinity would."""
        if self.trinity_down:
            raise OSError("connection refused")
        self.raised.append((agent, ask))
        iid = "item-%d" % (len(self.items) + 1)
        self.items[iid] = {"id": iid, "agent_name": agent, "request_id": ask["request_id"], "type": ask["type"],
                           "status": "pending", "response": None, "title": ask["title"],
                           "question": ask["question"], "options": ask["options"], "context": ask["context"]}
        return {"status": "created", "id": iid, "request_id": ask["request_id"]}

    def answer(self, iid, response="Approve", status="responded"):
        self.items[iid].update(status=status, response=response)

    def plan(self, action="lights.set", params=None, token=TOKEN_A):
        st, data, _ = self.call(token, action, {"params": params or {"id": "porch", "state": "on"}}, route="plan")
        self.assertEqual(st, 200, data)
        return json.loads(data)

    def approved_plan(self, action="lights.set", params=None, response="Approve", status="responded", token=TOKEN_A):
        p = self.plan(action, params, token)
        self.answer(p["queue_item_id"], response, status)
        return p["plan_id"]

    def read_plans(self):
        with open(self.cfg["plans"]) as fh:
            return json.load(fh)

    def call(self, token, action, body, route="act"):
        st, _, data, info = self.gw.process(route, token, action, json.dumps(body).encode())
        return st, data, info

    LIGHT = {"params": {"id": "porch", "state": "on"}}

    def light(self, pid="x", **kw):
        return self.call(TOKEN_A, "lights.set", dict(self.LIGHT, plan_id=pid, **kw))

    # -- reads and the basic checks
    def test_read_ok_injects_credential(self):
        st, _, info = self.call(TOKEN_B, "weather.read", {"params": {"lat": "1.5", "lon": "-2"}})
        self.assertEqual(st, 200)
        m, url, headers, _ = self.sent[0]
        self.assertEqual((m, url), ("GET", "https://api.example.com/v1/forecast?lat=1.5&lon=-2"))
        self.assertEqual(headers["X-Api-Key"], "SECRET-WEATHER")

    def test_unknown_token(self):
        self.assertEqual(self.call("nope", "weather.read", {"params": {}})[0], 401)
        self.assertEqual(self.sent, [])

    def test_acl_deny(self):
        self.assertEqual(self.call(TOKEN_B, "lights.set", dict(self.LIGHT, approval_id="x"))[0], 403)
        self.assertEqual(self.call(TOKEN_B, "no.such.action", {})[0], 403)
        self.assertEqual(self.sent, [])

    def test_param_regex_deny(self):
        for bad in ({"lat": "1;rm", "lon": "2"}, {"lat": "1", "lon": "2", "extra": "x"}, {"lat": "1"}, {"lat": ["1"], "lon": "2"}):
            self.assertEqual(self.call(TOKEN_A, "weather.read", {"params": bad})[0], 400, bad)
        self.assertEqual(self.sent, [])

    def test_url_injection_is_encoded(self):
        a = dict(ACTIONS["weather.read"]); a["params"] = {"lat": {"pattern": ".*"}, "lon": {"pattern": ".*"}}
        self.gw.actions["weather.read"] = a
        self.call(TOKEN_A, "weather.read", {"params": {"lat": "1&x=2", "lon": "../a"}})
        self.assertIn("lat=1%26x%3D2&lon=..%2Fa", self.sent[0][1])

    # -- writes and approvals
    def test_write_without_plan_id(self):
        st, data, _ = self.call(TOKEN_A, "lights.set", self.LIGHT)
        self.assertEqual(st, 403)
        self.assertIn(b"plan_id", data)
        self.assertEqual(self.sent, [])

    def test_plan_then_approve_then_call_works_once(self):
        pid = self.approved_plan()
        self.assertEqual(self.light(pid)[0], 200)
        self.assertEqual(json.loads(self.sent[0][3]), {"state": "on"})
        st, data, _ = self.light(pid)
        self.assertEqual(st, 403)
        self.assertIn(b"already used", data)
        self.assertEqual(len(self.sent), 1)

    def test_plan_ask_is_written_by_the_gateway(self):
        p = self.plan("door.unlock", {"door": "garage", "code": "1234"})
        agent, ask = self.raised[0]
        self.assertEqual(agent, "agent-a")
        self.assertEqual(ask["type"], "approval")
        self.assertIn("garage", ask["question"])
        self.assertIn('door = "garage"', ask["question"])
        self.assertEqual(ask["context"]["gateway"]["params"], {"door": "garage", "code": "(secret, not shown)"})
        self.assertEqual(ask["context"]["gateway"]["action"], "door.unlock")
        self.assertEqual(p["title"], ask["title"])
        # text supplied by the agent in the plan body is never used
        self.call(TOKEN_A, "lights.set", {"params": self.LIGHT["params"], "title": "Unlock the FRONT door",
                                          "question": "harmless", "context": {"x": 1}}, route="plan")
        self.assertNotIn("FRONT", self.raised[1][1]["title"] + self.raised[1][1]["question"])
        self.assertNotIn("harmless", self.raised[1][1]["question"])

    def test_plan_for_read_action_raises_nothing(self):
        st, data, _ = self.call(TOKEN_B, "weather.read", {"params": {"lat": "1", "lon": "2"}}, route="plan")
        self.assertEqual(st, 200)
        self.assertFalse(json.loads(data)["needs_approval"])
        self.assertEqual(self.raised, [])

    def test_plan_checks_acl_and_params(self):
        self.assertEqual(self.call(TOKEN_B, "lights.set", self.LIGHT, route="plan")[0], 403)
        self.assertEqual(self.call(TOKEN_A, "lights.set", {"params": {"id": "porch", "state": "dim"}}, route="plan")[0], 400)
        self.assertEqual(self.raised, [])

    def test_front_door_approval_cannot_unlock_garage(self):
        """The person approved a request about the FRONT door; the agent's own ask carries the old
        marker for the garage params. Neither path may unlock the garage."""
        garage = {"door": "garage", "code": "1234"}
        canon = json.dumps({k: str(v) for k, v in garage.items()}, sort_keys=True, separators=(",", ":"))
        legacy_marker = "gw:door.unlock:" + hashlib.sha256(canon.encode()).hexdigest()
        self.items["agent-ask"] = {"id": "agent-ask", "agent_name": "agent-a", "request_id": "agent-ask", "status": "responded",
                                   "response": "Approve", "title": "Unlock the FRONT door",
                                   "question": "Please approve. " + legacy_marker, "context": {"door": "front"}}
        for field in ("approval_id", "plan_id"):
            st, _, _ = self.call(TOKEN_A, "door.unlock", {"params": garage, field: "agent-ask"})
            self.assertEqual(st, 403, field)
        self.assertEqual(self.sent, [])

    def test_approved_front_plan_cannot_be_spent_on_garage(self):
        pid = self.approved_plan("door.unlock", {"door": "front", "code": "1234"})
        self.assertEqual(self.call(TOKEN_A, "door.unlock", {"params": {"door": "garage", "code": "1234"}, "plan_id": pid})[0], 403)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.call(TOKEN_A, "door.unlock", {"params": {"door": "front", "code": "1234"}, "plan_id": pid})[0], 200)

    def test_legacy_approval_id_is_not_accepted(self):
        p = self.plan()
        self.answer(p["queue_item_id"])
        for ident in (p["plan_id"], p["queue_item_id"]):
            self.assertEqual(self.call(TOKEN_A, "lights.set", dict(self.LIGHT, approval_id=ident))[0], 403)
        self.assertEqual(self.sent, [])

    def test_queue_item_text_changed_after_planning(self):
        p = self.plan("door.unlock", {"door": "front", "code": "1234"})
        self.items[p["queue_item_id"]]["title"] = "Unlock the garage door"
        self.answer(p["queue_item_id"])
        st, data, _ = self.call(TOKEN_A, "door.unlock", {"params": {"door": "front", "code": "1234"}, "plan_id": p["plan_id"]})
        self.assertEqual(st, 403)
        self.assertIn(b"differs", data)
        self.assertEqual(self.sent, [])

    def test_plan_pointing_at_a_different_queue_item(self):
        p = self.plan()
        other = self.plan(params={"id": "porch", "state": "off"})
        self.answer(other["queue_item_id"])
        plans = self.read_plans()
        plans[p["plan_id"]]["item_id"] = other["queue_item_id"]  # a record that points at the wrong row
        put(self.cfg["plans"], plans)
        self.assertEqual(self.light(p["plan_id"])[0], 403)
        self.assertEqual(self.sent, [])

    def test_plan_of_other_agent(self):
        pid = self.approved_plan()
        self.assertEqual(self.call(TOKEN_B, "lights.set", dict(self.LIGHT, plan_id=pid))[0], 403)  # ACL
        self.gw.acl["agent-b"].append("lights.set")
        self.assertEqual(self.call(TOKEN_B, "lights.set", dict(self.LIGHT, plan_id=pid))[0], 403)  # not b's plan
        self.assertEqual(self.sent, [])

    def test_item_belonging_to_other_agent(self):
        pid = self.approved_plan()
        for it in self.items.values():
            it["agent_name"] = "agent-b"
        self.assertEqual(self.light(pid)[0], 403)
        self.assertEqual(self.sent, [])

    def test_approval_for_other_action(self):
        pid = self.approved_plan("door.unlock", {"door": "front", "code": "1234"})
        self.assertEqual(self.light(pid)[0], 403)
        self.assertEqual(self.sent, [])

    def test_approval_for_other_params(self):
        pid = self.approved_plan(params={"id": "porch", "state": "off"})
        self.assertEqual(self.light(pid)[0], 403)  # the call asks for state=on
        self.assertEqual(self.sent, [])

    def test_approval_not_approved(self):
        deny = self.approved_plan(response="Deny")
        pending = self.plan()["plan_id"]
        something_else = self.approved_plan(response="(something else)")
        self.assertEqual(self.light(deny)[0], 403)
        self.assertEqual(self.light(pending)[0], 403)
        self.assertEqual(self.light(something_else)[0], 403)
        self.assertEqual(self.light("gwp-" + "0" * 32)[0], 403)  # a plan this gateway never made
        self.assertEqual(self.sent, [])

    def test_numeric_plan_id_replay_is_refused(self):
        self.gw.new_plan_id = lambda: "12"
        pid = self.approved_plan()
        self.assertEqual(pid, "12")
        self.assertEqual(self.light("012")[0], 200)  # first spend, any spelling
        for again in ("12", 12, "012", " 12 ", "0012", "000012"):
            st, data, _ = self.light(again)
            self.assertEqual(st, 403, again)
            self.assertIn(b"already used", data)
        for bad in (12.0, True, ["12"], {"a": 1}, None):
            self.assertEqual(self.light(bad)[0], 403, bad)
        self.assertEqual(len(self.sent), 1)

    def test_canon_id(self):
        self.assertEqual({gateway.canon_id(v) for v in ("12", 12, "012", " 12 ")}, {"12"})
        self.assertEqual(gateway.canon_id("0"), gateway.canon_id("000"))
        for bad in (True, 1.5, None, "", "a b", "x" * 300, ["1"]):
            self.assertIsNone(gateway.canon_id(bad))

    def test_used_ids_survive_restart(self):
        pid = self.approved_plan()
        self.assertEqual(self.light(pid)[0], 200)
        gw2 = gateway.Gateway(dict(self.cfg))
        gw2.send, gw2.trinity_get, gw2.trinity_raise = self.gw.send, self.fake_trinity, self.fake_raise
        st, _, data, _ = gw2.process("act", TOKEN_A, "lights.set", json.dumps(dict(self.LIGHT, plan_id=pid)).encode())
        self.assertEqual(st, 403)
        self.assertIn(b"already used", data)
        self.assertEqual(len(self.sent), 1)

    def test_one_queue_item_buys_one_call_even_with_a_second_plan_record(self):
        pid = self.approved_plan()
        plans = self.read_plans()
        plans["gwp-copy"] = dict(plans[pid])
        put(self.cfg["plans"], plans)
        self.assertEqual(self.light(pid)[0], 200)
        self.items["item-1"]["request_id"] = "gwp-copy"
        self.assertEqual(self.light("gwp-copy")[0], 403)
        self.assertEqual(len(self.sent), 1)

    def test_plan_expires(self):
        pid = self.approved_plan()
        self.gw.cfg["plan_ttl"] = 0
        self.assertEqual(self.light(pid)[0], 403)
        self.assertEqual(self.sent, [])

    def test_trinity_unreachable_fails_closed(self):
        self.trinity_down = True
        st, data, _ = self.call(TOKEN_A, "lights.set", self.LIGHT, route="plan")
        self.assertEqual(st, 503)  # no ask, no plan
        self.assertFalse(os.path.exists(self.cfg["plans"]))
        self.trinity_down = False
        pid = self.approved_plan()
        self.trinity_down = True
        self.assertEqual(self.light(pid)[0], 503)
        self.assertEqual(self.sent, [])
        self.trinity_down = False
        self.assertEqual(self.light(pid)[0], 200)  # nothing was burned by the failure

    def test_bad_receipt_fails_closed(self):
        self.gw.trinity_raise = lambda agent, ask: {"status": "created", "id": "x", "request_id": "not-ours"}
        self.assertEqual(self.call(TOKEN_A, "lights.set", self.LIGHT, route="plan")[0], 503)
        self.gw.trinity_raise = lambda agent, ask: {"status": "replayed", "id": "x", "request_id": ask["request_id"]}
        self.assertEqual(self.call(TOKEN_A, "lights.set", self.LIGHT, route="plan")[0], 503)
        self.assertFalse(os.path.exists(self.cfg["plans"]))

    def test_plans_file_unreadable_fails_closed(self):
        pid = self.approved_plan()
        put(self.cfg["plans"], "{not json")
        self.assertEqual(self.light(pid)[0], 503)
        self.assertEqual(self.sent, [])

    def test_plan_rate_limit(self):
        self.gw.cfg["plan_max_per_hour"] = 2
        self.plan(); self.plan()
        self.assertEqual(self.call(TOKEN_A, "lights.set", self.LIGHT, route="plan")[0], 429)
        self.assertEqual(len(self.raised), 2)

    def test_rendering_is_escaped_and_bounded(self):
        a = dict(ACTIONS["lights.set"]); a["params"] = {"id": {"pattern": "[\\s\\S]*"}, "state": {"pattern": "on|off"}}
        a["describe"] = "Set light {id} {state}"
        self.gw.actions["lights.set"] = a
        self.plan(params={"id": "a\u202eb\nstate = \"off\"", "state": "on"})
        q = self.raised[0][1]["question"]
        self.assertNotIn("\u202e", q)
        self.assertEqual(len([l for l in q.splitlines() if l.startswith("  state = ")]), 1)
        self.assertLessEqual(len(self.raised[0][1]["title"]), gateway.TITLE_MAX)
        self.assertEqual(self.call(TOKEN_A, "lights.set", {"params": {"id": "x" * 5000, "state": "on"}}, route="plan")[0], 413)

    def test_plan_hash_is_stable_and_binds_action(self):
        self.assertEqual(gateway.plan_hash("lights.set", {"b": 1, "a": "2"}), gateway.plan_hash("lights.set", {"a": "2", "b": "1"}))
        self.assertNotEqual(gateway.plan_hash("lights.set", {"a": "2"}), gateway.plan_hash("door.unlock", {"a": "2"}))

    def test_hourly_cap_does_not_burn_approval(self):
        pids = [self.approved_plan() for _ in range(3)]
        self.assertEqual(self.light(pids[0])[0], 200)
        self.assertEqual(self.light(pids[1])[0], 200)
        self.assertEqual(self.light(pids[2])[0], 429)
        self.assertFalse(self.gw.used(pids[2]))

    # -- kill switch, dry run, log hygiene
    def test_kill_switch_blocks_writes_only(self):
        put(self.cfg["disable"], "")
        self.assertEqual(self.call(TOKEN_A, "lights.set", self.LIGHT, route="plan")[0], 503)
        os.remove(self.cfg["disable"])
        pid = self.approved_plan()
        put(self.cfg["disable"], "")
        self.assertEqual(self.light(pid)[0], 503)
        self.assertEqual(self.call(TOKEN_B, "weather.read", {"params": {"lat": "1", "lon": "2"}})[0], 200)
        os.remove(self.cfg["disable"])
        self.assertEqual(self.light(pid)[0], 200)

    def test_dry_run_sends_nothing_and_keeps_approval(self):
        pid = self.approved_plan()
        self.gw.cfg["dry_run"] = True
        st, data, _ = self.light(pid)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(data)["dry_run"])
        self.assertNotIn(b"SECRET", data)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.gw.used(pid))
        n = len(self.raised)
        self.assertTrue(json.loads(self.call(TOKEN_A, "lights.set", self.LIGHT, route="plan")[1])["dry_run"])
        self.assertEqual(len(self.raised), n)

    def test_sensitive_params_not_in_log_info(self):
        params = {"door": "front", "code": "1234"}
        pid = self.approved_plan("door.unlock", params)
        st, _, info = self.call(TOKEN_A, "door.unlock", {"params": params, "plan_id": pid})
        self.assertEqual(st, 200)
        self.assertNotIn("1234", json.dumps(info))
        self.assertNotIn("SECRET", json.dumps(info))

    # -- secret params, plan ownership and request ids
    def test_secret_param_values_never_reach_trinity(self):
        """A door code must not be stored in the operator-queue row or sent to its channels."""
        self.gw.actions["door.unlock"]["params"]["code"]["secret"] = True
        p = self.plan("door.unlock", {"door": "front", "code": "4821"})
        agent, ask = self.raised[0]
        everything = json.dumps(ask)
        self.assertNotIn("4821", everything)
        self.assertNotIn(gateway.plan_hash("door.unlock", {"door": "front", "code": "4821"}), everything)
        self.assertIn('door = "front"', ask["question"])  # what is being unlocked stays readable
        self.assertIn("code = \"(secret, not shown)\"", ask["question"])
        self.assertEqual(ask["context"]["gateway"]["params"], {"door": "front", "code": "(secret, not shown)"})
        self.assertNotIn("4821", json.dumps(p))
        # the approval still binds the real value
        self.answer(p["queue_item_id"])
        self.assertEqual(self.call(TOKEN_A, "door.unlock", {"params": {"door": "front", "code": "1111"}, "plan_id": p["plan_id"]})[0], 403)
        self.assertEqual(self.call(TOKEN_A, "door.unlock", {"params": {"door": "front", "code": "4821"}, "plan_id": p["plan_id"]})[0], 200)

    def test_sensitive_action_without_marked_params_hides_every_param(self):
        del self.gw.actions["door.unlock"]["params"]["code"]["secret"]
        self.plan("door.unlock", {"door": "garage", "code": "4821"})
        ask = self.raised[0][1]
        self.assertNotIn("4821", json.dumps(ask))
        self.assertNotIn("garage", json.dumps(ask))

    def test_secret_param_on_a_plain_action_is_not_logged(self):
        self.gw.actions["lights.set"]["params"]["state"]["secret"] = True
        pid = self.approved_plan(params={"id": "porch", "state": "off"})
        st, _, info = self.call(TOKEN_A, "lights.set", {"params": {"id": "porch", "state": "off"}, "plan_id": pid})
        self.assertEqual(st, 200)
        self.assertEqual(info["detail"], 'params={"id": "porch", "state": "(secret, not shown)"}')
        ask_text = "\n".join(_all_strings(self.raised[0][1]))
        self.assertIn("porch", ask_text)  # the ask is readable here ...
        self.assertNotIn("off", ask_text)  # ... and the secret value is not in it

    def test_secret_flag_must_be_boolean(self):
        bad = json.loads(json.dumps(ACTIONS))
        bad["door.unlock"]["params"]["code"]["secret"] = "yes"
        put(self.cfg["actions"], bad)
        self.assertRaises(ValueError, gateway.load_actions, self.cfg["actions"])

    def test_load_plan_refuses_another_agents_plan_record(self):
        """load_plan on its own refuses another agent's plan record, without relying on the later item check."""
        pid = self.approved_plan()
        with self.assertRaises(gateway.Refuse) as cm:
            self.gw.load_plan("agent-b", "lights.set", self.LIGHT["params"], pid)
        self.assertEqual((cm.exception.status, cm.exception.reason), (403, "unknown plan"))
        self.assertEqual(self.gw.load_plan("agent-a", "lights.set", self.LIGHT["params"], pid)[0], pid)

    def test_queue_item_with_a_different_request_id_is_refused(self):
        """Same item id, agent, text and answer, but another request id: not the ask this gateway raised."""
        p = self.plan()
        self.answer(p["queue_item_id"])
        self.items[p["queue_item_id"]]["request_id"] = "gwp-" + "1" * 32
        st, data, _ = self.light(p["plan_id"])
        self.assertEqual(st, 403)
        self.assertIn(b"not the request this gateway raised", data)
        self.assertEqual(self.sent, [])

    def test_missing_credential_refused(self):
        os.remove(self.tmp.name + "/creds/lights-token")
        pid = self.approved_plan()
        self.assertEqual(self.light(pid)[0], 503)
        self.assertEqual(self.sent, [])

    def test_ledger_unwritable_fails_closed(self):
        pid = self.approved_plan()
        self.gw.cfg["ledger"] = self.tmp.name + "/nodir/used.json"
        self.assertEqual(self.light(pid)[0], 503)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
