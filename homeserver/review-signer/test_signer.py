#!/usr/bin/env python3
"""Offline tests for review-signer: role checks, neutral wording, request validation, mocked HTTP call."""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

here = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("review_signer", os.path.join(here, "review-signer"))
spec = importlib.util.spec_from_loader("review_signer", loader)
rs = importlib.util.module_from_spec(spec)
loader.exec_module(rs)

ROLES = json.load(open(os.path.join(here, "roles.example.json")))
SHA = "a" * 40


LEAD, PRINCIPAL = "reviewer-lead", "reviewer-principal"  # local user names in roles.example.json


def req(**kw):
    base = {"repo": "OWNER/REPO", "pr": 7, "sha": SHA, "role": "lead",
            "verdict": "findings", "findings_high": 0, "findings_total": 2}
    base.update(kw)
    return json.dumps({k: v for k, v in base.items() if v is not DROP})


DROP = object()


class Signer(unittest.TestCase):
    def setUp(self):
        fh = tempfile.NamedTemporaryFile("w", delete=False)
        fh.write("fake-token\n")
        fh.close()
        self.tok = fh.name
        self.c = {"api_base": "https://api.example.invalid", "token_file": self.tok}
        self.calls = []

    def tearDown(self):
        os.unlink(self.tok)

    def poster(self, base, path, payload, token):
        self.calls.append((base, path, payload, token))
        return 201

    def run_req(self, line, caller=LEAD, **kw):
        return rs.handle(line, self.c, ROLES, caller, poster=self.poster, **kw)

    def test_posts_neutral_status(self):
        out = self.run_req(req())
        self.assertTrue(out["ok"], out)
        base, path, payload, token = self.calls[0]
        self.assertEqual(path, "/repos/OWNER/REPO/statuses/" + SHA)
        self.assertEqual(payload, {"state": "success", "context": "review/lead", "description": "2 findings, none blocking"})
        self.assertEqual(token, "fake-token")

    def test_blocking_and_error_never_success(self):
        self.run_req(req(findings_high=1))
        self.assertEqual((self.calls[-1][2]["state"], self.calls[-1][2]["description"]), ("failure", "2 findings, 1 blocking"))
        self.run_req(req(verdict="error"))
        self.assertEqual(self.calls[-1][2]["state"], "error")
        self.run_req(req(verdict="clean"))
        self.assertEqual(self.calls[-1][2]["description"], "no findings")

    def test_description_is_neutral(self):
        for v in ("clean", "findings", "error"):
            self.run_req(req(verdict=v))
        for call in self.calls:
            text = call[2]["description"].lower()
            for word in ("agent", "bot", "reviewer", " ai"):
                self.assertNotIn(word, text)

    def test_role_rules(self):
        for line, caller in ((req(), "stranger"), (req(role="principal"), LEAD), (req(role="review"), LEAD),
                             (req(role="lead"), PRINCIPAL), (req(verdict="pass"), LEAD), (req(role="verify"), PRINCIPAL)):
            self.assertFalse(self.run_req(line, caller=caller)["ok"], (line, caller))
        self.assertEqual(self.calls, [])
        self.assertTrue(self.run_req(req(role="principal"), caller=PRINCIPAL)["ok"])
        self.assertEqual(self.calls[-1][2]["context"], "review/principal")

    def test_claimed_caller_cannot_override_the_socket_peer(self):
        # Peer is the lead account; the request claims to be the principal and asks for review/principal.
        out = self.run_req(req(caller=PRINCIPAL, role="principal"), caller=LEAD)
        self.assertFalse(out["ok"], out)
        self.assertEqual(self.calls, [])
        # The same request without the claim is refused by the role table, not by the claim check.
        self.assertFalse(self.run_req(req(role="principal"), caller=LEAD)["ok"])
        # A claim that matches the peer is harmless; a claim naming another caller never widens or narrows anything.
        self.assertTrue(self.run_req(req(caller=LEAD), caller=LEAD)["ok"])
        for bad in (None, "", 7, ["reviewer-lead"]):
            self.assertFalse(self.run_req(req(caller=bad), caller=LEAD)["ok"], bad)
        self.assertFalse(self.run_req(req(), caller=None)["ok"])
        self.assertFalse(self.run_req(req(), caller="")["ok"])

    def test_peer_identity_comes_from_so_peercred_lookup(self):
        seen = []
        uid_of = lambda fd: seen.append(fd) or 4242
        self.assertEqual(rs.caller_from_peer(7, uid_of=uid_of, name_of={4242: LEAD}.__getitem__), LEAD)
        self.assertEqual(seen, [7])
        with mock.patch.object(rs, "peer_uid", return_value=4242), \
                mock.patch.object(rs.pwd, "getpwuid", return_value=mock.Mock(pw_name=LEAD)):
            caller = rs.caller_from_peer()
        self.assertEqual(caller, LEAD)
        # End to end with the mocked peer: a lead peer claiming to be the principal cannot sign review/principal.
        out = self.run_req(req(caller=PRINCIPAL, role="principal"), caller=caller)
        self.assertFalse(out["ok"])
        self.assertEqual(self.calls, [])
        # An unknown uid raises (the service then fails closed with "cannot identify the caller").
        with mock.patch.object(rs, "peer_uid", return_value=4242), \
                mock.patch.object(rs.pwd, "getpwuid", side_effect=KeyError("uid not found")):
            self.assertRaises(KeyError, rs.caller_from_peer)

    def test_validation(self):
        for line in (req(sha="abc"), req(repo="../x"), req(pr=0), req(pr="7"), req(findings_high=-1), req(findings_high=True),
                     "not json", "[]", req(role=["lead"])):
            self.assertFalse(self.run_req(line)["ok"], line)
        self.assertEqual(self.calls, [])

    def test_dry_run_and_failures(self):
        out = self.run_req(req(), dry_run=True)
        self.assertTrue(out["ok"])
        self.assertIn("POST https://api.example.invalid/repos/OWNER/REPO/statuses/", out["msg"])
        self.assertNotIn("fake-token", out["msg"])
        self.assertEqual(self.calls, [])
        self.c["token_file"] = "/nonexistent/token"
        self.assertFalse(self.run_req(req())["ok"])
        self.c["token_file"] = self.tok

        def boom(*a):
            raise OSError("network down")
        self.assertFalse(rs.handle(req(), self.c, ROLES, LEAD, poster=boom)["ok"])


class ExamplesAgree(unittest.TestCase):
    def test_example_roles_produce_exactly_the_contexts_the_example_gate_requires(self):
        with open(os.path.join(here, "..", "merge-gate", "config.example.json")) as fh:
            gate = json.load(fh)
        required = {gate["review_lead"], gate["review_principal"]}
        posted, calls = {}, []
        tok = tempfile.NamedTemporaryFile("w", delete=False)
        tok.write("fake-token\n")
        tok.close()
        try:
            c = {"api_base": "https://api.example.invalid", "token_file": tok.name}
            for caller, roles in ROLES["callers"].items():
                for role, verdicts in roles.items():
                    for verdict in verdicts:
                        out = rs.handle(req(role=role, verdict=verdict), c, ROLES, caller,
                                        poster=lambda b, p, pl, t: calls.append(pl) or 201)
                        self.assertTrue(out["ok"], out)
                        posted.setdefault(caller, set()).add(calls[-1]["context"])
        finally:
            os.unlink(tok.name)
        self.assertEqual(set().union(*posted.values()), required)
        self.assertEqual(posted[LEAD], {gate["review_lead"]})          # the lead account cannot post the principal context
        self.assertEqual(posted[PRINCIPAL], {gate["review_principal"]})
        self.assertTrue(gate["signer_login"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
