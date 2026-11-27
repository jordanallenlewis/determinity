#!/usr/bin/env python3
"""Offline tests for ghbroker: policy decisions, path canonicalisation, merge-gate forgery, admin list,
protected branches, commit message policy, tokens, budget."""
import hashlib
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghbroker as g  # noqa: E402

POLICY = g.Policy(json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.example.json"))))


POLICY_FREE = g.Policy({
    "merge_gate_repos": ["OWNER/REPO"],
    "agents": {"agent-a": {"OWNER/REPO": "write", "OWNER/free": "write"}},
})
POLICY_CUSTOM = g.Policy({
    "merge_gate_repos": ["OWNER/REPO"], "merge_label": "Ship-It", "hold_label": "wait",
    "agents": {"agent-a": {"OWNER/REPO": "write"}},
})


def pkt(s):
    return ("%04x" % (len(s) + 4)).encode() + s


def push(ref):
    return pkt(b"0" * 40 + b" " + b"1" * 40 + b" " + ref.encode() + b"\0report-status\n") + b"0000PACK"


class Tokens(unittest.TestCase):
    def test_lookup(self):
        raw = "s3cret-token"
        with tempfile.NamedTemporaryFile("w", suffix=".tok", delete=False) as fh:
            fh.write("# c\n%s  agent-a\nnot-a-hash  evil\n" % hashlib.sha256(raw.encode()).hexdigest())
        t = g.load_tokens(fh.name)
        os.unlink(fh.name)
        self.assertEqual(g.lookup_agent(t, raw), "agent-a")
        self.assertIsNone(g.lookup_agent(t, "wrong"))
        self.assertIsNone(g.lookup_agent(t, ""))
        self.assertEqual(len(t), 1)


class Rest(unittest.TestCase):
    def d(self, agent, method, path, body=None):
        return g.decide_rest(POLICY, agent, method, path, json.dumps(body).encode() if body is not None else b"")

    def test_scope(self):
        self.assertIsNone(self.d("agent-a", "GET", "/repos/OWNER/REPO/pulls"))
        self.assertIsNone(self.d("agent-a", "GET", "/repos/owner/repo/pulls"))  # case-insensitive
        self.assertEqual(self.d("agent-a", "GET", "/repos/OWNER/other/pulls")[0], 403)
        self.assertEqual(self.d("nobody", "GET", "/repos/OWNER/REPO")[0], 403)
        self.assertEqual(self.d("agent-a", "POST", "/repos/OWNER/docs/issues", {"title": "x"})[0], 403)  # read only
        self.assertEqual(self.d("agent-b", "POST", "/repos/OWNER/REPO/issues", {"title": "x"})[0], 403)
        self.assertEqual(self.d("agent-a", "POST", "/graphql", {})[0], 403)
        self.assertEqual(self.d("agent-a", "GET", "/user")[0], 403)
        self.assertIsNone(self.d("agent-a", "GET", "/rate_limit"))

    def test_writes_allowed(self):
        self.assertIsNone(self.d("agent-a", "POST", "/repos/OWNER/REPO/issues/3/comments", {"body": "2 findings"}))
        self.assertIsNone(self.d("agent-a", "POST", "/repos/OWNER/REPO/git/refs", {"ref": "refs/heads/feature/x", "sha": "a"}))

    def test_protected_and_merge(self):
        for ref in ("refs/heads/main", "refs/heads/master", "refs/heads/release/1.0"):
            self.assertEqual(self.d("agent-a", "POST", "/repos/OWNER/REPO/git/refs", {"ref": ref, "sha": "a"})[0], 403, ref)
        self.assertEqual(self.d("agent-a", "PATCH", "/repos/OWNER/REPO/git/refs/heads/main", {"sha": "a"})[0], 403)
        self.assertEqual(self.d("agent-a", "DELETE", "/repos/OWNER/REPO/git/refs/heads/main")[0], 403)
        self.assertEqual(self.d("agent-a", "POST", "/repos/OWNER/REPO/merges", {"base": "main", "head": "x"})[0], 403)
        self.assertEqual(self.d("agent-a", "PUT", "/repos/OWNER/REPO/contents/a.txt", {"message": "m", "content": "e"})[0], 403)
        self.assertEqual(self.d("agent-a", "PUT", "/repos/OWNER/REPO/contents/a.txt", {"branch": "main"})[0], 403)
        self.assertIsNone(self.d("agent-a", "PUT", "/repos/OWNER/REPO/contents/a.txt", {"branch": "topic"}))
        self.assertEqual(self.d("agent-a", "PUT", "/repos/OWNER/REPO/pulls/5/merge", {})[0], 403)  # merge-gate repo
        self.assertEqual(self.d("agent-a", "PUT", "/repos/OWNER/REPO/branches/main/protection", {})[0], 403)
        self.assertEqual(self.d("agent-a", "POST", "/repos/OWNER/REPO/hooks", {})[0], 403)
        self.assertEqual(self.d("agent-a", "DELETE", "/repos/OWNER/REPO")[0], 403)

    def test_commit_message_policy(self):
        # policy.example.json configures one neutral placeholder pattern; the default list is empty.
        marker = "Signed-off-by-bot: example"
        for text in (marker, marker.upper()):
            r = self.d("agent-a", "POST", "/repos/OWNER/REPO/issues/3/comments", {"body": "fine\n\n" + text})
            self.assertEqual(r[0], 403, text)
            self.assertEqual(r[1], "commit message policy: refused (pattern 0)")
        for text in ("Signed-off-by: Alice Smith <alice@example.com>", "the bot signed off", "x " + marker):
            self.assertIsNone(self.d("agent-a", "POST", "/repos/OWNER/REPO/issues/3/comments", {"body": text}), text)
        self.assertEqual(self.d("agent-a", "POST", "/repos/OWNER/REPO/issues", {"title": "x", "labels": [marker]})[0], 403)
        self.assertEqual(self.d("agent-a", "POST", "/repos/OWNER/REPO/issues", {"title": "x", marker: 1})[0], 403)
        self.assertEqual(g.screen_message_policy(b"not json\n" + marker.encode(), POLICY.message_patterns)[0:21],
                         "commit message policy")

    def test_commit_message_policy_empty_default(self):
        marker = "Signed-off-by-bot: example"
        self.assertEqual(POLICY_FREE.message_patterns, [])
        self.assertIsNone(g.decide_rest(POLICY_FREE, "agent-a", "POST", "/repos/OWNER/free/issues/3/comments",
                                        json.dumps({"body": marker}).encode()))
        self.assertIsNone(g.screen_message_policy(marker.encode()))

    def test_commit_message_policy_bad_config_refused(self):
        for bad in (["("], [""], [3], "^x"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                g.Policy({"merge_gate_repos": [], "agents": {}, "extra_message_patterns": bad})

    def test_path_tricks(self):
        for p in ("/repos/OWNER/REPO/../other/issues", "/repos/OWNER/REPO/%2e%2e/x", "/repos/OWNER/REPO%2Fx", "/repos/OWNER/./REPO"):
            self.assertEqual(self.d("agent-a", "GET", p)[0], 400, p)

    def test_bad_json(self):
        self.assertEqual(g.decide_rest(POLICY, "agent-a", "POST", "/repos/OWNER/REPO/issues", b"{oops")[0], 400)


class Decide(unittest.TestCase):
    policy = POLICY

    def d(self, method, path, body=None, agent="agent-a"):
        raw = json.dumps(body).encode() if body is not None else b""
        return g.decide_rest(self.policy, agent, method, path, raw)

    def refused(self, method, path, body=None, codes=(400, 403)):
        r = self.d(method, path, body)
        self.assertIsNotNone(r, "%s %s was allowed" % (method, path))
        self.assertIn(r[0], codes, "%s %s -> %r" % (method, path, r))
        return r


class PctEscape(Decide):
    def test_pct_escape_protected_ref_patch(self):
        self.refused("PATCH", "/repos/OWNER/REPO/git/refs/heads/%6dain", {"sha": "a"})
        self.refused("PATCH", "/repos/OWNER/REPO/git/refs/heads/mai%6e", {"sha": "a"})
        self.refused("DELETE", "/repos/OWNER/REPO/git/refs/heads/%6Dain")
        self.refused("PATCH", "/repos/OWNER/REPO/branches/%6dain/rename", {"new_name": "x"})
        self.refused("POST", "/repos/OWNER/REPO/branches/release/1/rename", {"new_name": "x"})

    def test_pct_escape_merge_refused_on_gate_repo(self):
        self.refused("PUT", "/repos/OWNER/REPO/pulls/1/%6derge", {})
        self.refused("PUT", "/repos/OWNER/REPO/%70ulls/1/merge", {})
        self.refused("PUT", "/repos/OWNER/REPO/pulls/1/MERGE", {})  # route words match case-insensitively

    def test_pct_escape_admin_words(self):
        self.refused("PUT", "/repos/OWNER/REPO/branches/main/%70rotection", {})
        self.refused("POST", "/repos/OWNER/REPO/%68ooks", {})
        self.refused("PUT", "/repos/OWNER/REPO/actions/%73ecrets/X", {})
        self.refused("PUT", "/repos/OWNER/REPO/%63ollaborators/u", {})
        self.refused("POST", "/repos/OWNER/REPO/%6beys", {})

    def test_pct_escape_double_encoding_refused(self):
        self.refused("PATCH", "/repos/OWNER/REPO/git/refs/heads/%256dain", {"sha": "a"}, codes=(400,))
        self.refused("GET", "/repos/OWNER/REPO/issues/%25", codes=(400,))
        self.refused("GET", "/repos/OWNER/REPO/issues/%2525", codes=(400,))

    def test_pct_escape_slash_dot_backslash_refused(self):
        for p in ("/repos/OWNER/REPO/git/refs/heads%2Fmain", "/repos/OWNER/REPO/git/refs/heads%2fmain",
                  "/repos/OWNER/REPO/%2e%2e/x", "/repos/OWNER/REPO/%2E/x", "/repos/OWNER/REPO/a%5cb",
                  "/repos/OWNER/REPO/a%5Cb", "/repos/OWNER/REPO/a\\b", "/repos/OWNER/REPO/%00",
                  "/repos/OWNER/REPO/a%00b"):
            self.refused("GET", p, codes=(400,))

    def test_pct_escape_control_chars_and_bad_encodings_refused(self):
        for p in ("/repos/OWNER/REPO/a%0ab", "/repos/OWNER/REPO/a%0Db", "/repos/OWNER/REPO/a%1fb",
                  "/repos/OWNER/REPO/a%7fb", "/repos/OWNER/REPO/a\nb", "/repos/OWNER/REPO/a%zzb",
                  "/repos/OWNER/REPO/a%", "/repos/OWNER/REPO/a%c3", "/repos/OWNER/REPO/caf\u00e9",
                  "/repos/OWNER/REPO/%%36d"):
            self.refused("GET", p, codes=(400,))

    def test_pct_escape_params_and_empty_segments_refused(self):
        for p in ("/repos/OWNER/REPO/pulls;x=1", "/repos/OWNER/REPO/pulls/1;x/merge", "/repos/OWNER/REPO/a%3bb",
                  "/repos/OWNER//REPO/pulls", "//repos/OWNER/REPO", "/repos/OWNER/REPO/pulls//1",
                  "/repos/OWNER/REPO/./x", "/repos/OWNER/REPO/../x"):
            self.refused("GET", p, codes=(400,))

    def test_pct_escape_unreserved_decoded_and_trailing_slash(self):
        self.assertEqual(g.canon_rest_path("/repos/OWNER/REPO/%6derge"), ("/repos/OWNER/REPO/merge", None))
        self.assertEqual(g.canon_rest_path("/repos/OWNER/REPO/pulls/"), ("/repos/OWNER/REPO/pulls", None))
        self.assertEqual(g.canon_rest_path("/"), ("/", None))
        self.assertEqual(g.canon_rest_path("/rate_limit"), ("/rate_limit", None))
        self.assertIsNone(self.d("GET", "/repos/OWNER/REPO/pulls/"))
        self.assertIsNone(self.d("GET", "/repos/OWNER/REPO/contents/dir%20name/a%2Bb.txt"))

    def test_pct_escape_wire_path_is_canonical(self):
        for raw, wire in (
            ("/repos/OWNER/REPO/%6derge", "/repos/OWNER/REPO/merge"),
            ("/repos/OWNER/REPO/git/refs/heads/mai%6e", "/repos/OWNER/REPO/git/refs/heads/main"),
            ("/repos/OWNER/REPO/contents/dir%20name/a%2Bb.txt", "/repos/OWNER/REPO/contents/dir%20name/a+b.txt"),
            ("/repos/OWNER/REPO/contents/%C3%A9.txt", "/repos/OWNER/REPO/contents/%C3%A9.txt"),
            ("/repos/OWNER/REPO/contents/a%3Fb%23c", "/repos/OWNER/REPO/contents/a%3Fb%23c"),
            ("/repos/OWNER/REPO/pulls/", "/repos/OWNER/REPO/pulls"),
        ):
            canon, why = g.canon_rest_path(raw)
            self.assertIsNone(why, raw)
            self.assertEqual(g.wire_path(canon), wire, raw)
            # the wire form is itself a fixed point: canonicalising it again changes nothing
            self.assertEqual(g.canon_rest_path(g.wire_path(canon)), (canon, None), raw)
        self.assertEqual(g.wire_path("/"), "/")

    def test_pct_escape_handler_forwards_canonical_path(self):
        """End to end in dry-run mode: the call that would be made uses the canonical path."""
        with tempfile.TemporaryDirectory() as tmp:
            tokens, pol = os.path.join(tmp, "tokens"), os.path.join(tmp, "policy.json")
            with open(tokens, "w") as fh:
                fh.write("%s  agent-a\n" % hashlib.sha256(b"tok").hexdigest())
            with open(pol, "w") as fh:
                json.dump({"merge_gate_repos": [], "agents": {"agent-a": {"OWNER/free": "write"}}}, fh)
            cfg = {"tokens": tokens, "policy": pol, "token_file": os.path.join(tmp, "none"),
                   "disable": os.path.join(tmp, "off"), "api_base": "http://upstream.invalid",
                   "git_base": "http://upstream.invalid", "dry_run": True}
            srv = g.Broker(("localhost", 0), cfg)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                def call(path, method="GET"):
                    req = urllib.request.Request("http://localhost:%d%s" % (srv.server_address[1], path),
                                                 method=method, headers={"Authorization": "Bearer tok"})
                    with contextlib.redirect_stdout(io.StringIO()):  # the handler logs one line per call
                        try:
                            with urllib.request.urlopen(req) as r:
                                return r.status, json.loads(r.read())
                        except urllib.error.HTTPError as e:
                            return e.code, json.loads(e.read())
                code, out = call("/gh/repos/OWNER/free/%69ssues/%34?per_page=5")
                self.assertEqual(code, 200)
                self.assertEqual(out["would_call"], "GET http://upstream.invalid/repos/OWNER/free/issues/4?per_page=5")
                self.assertEqual(call("/gh/repos/OWNER/free/%69ssues/%254")[0], 400)
                self.assertEqual(call("/gh/repos/OWNER/free/issues/4;x")[0], 400)
            finally:
                srv.shutdown()
                srv.server_close()


class Forgery(Decide):
    policy = POLICY_FREE
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def test_forgery_status_write_refused_on_gate_repo(self):
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            self.refused(m, "/repos/OWNER/REPO/statuses/" + self.SHA, {"state": "success", "context": "ci/required"}, codes=(403,))
        self.refused("POST", "/repos/OWNER/REPO/%73tatuses/" + self.SHA, {"state": "success"}, codes=(403,))

    def test_forgery_status_write_allowed_on_non_gate_repo(self):
        self.assertIsNone(self.d("POST", "/repos/OWNER/free/statuses/" + self.SHA, {"state": "success", "context": "ci"}))

    def test_forgery_status_read_allowed_on_gate_repo(self):
        self.assertIsNone(self.d("GET", "/repos/OWNER/REPO/commits/%s/status" % self.SHA))
        self.assertIsNone(self.d("GET", "/repos/OWNER/REPO/statuses/" + self.SHA))

    def test_forgery_check_writes_refused_on_gate_repo(self):
        for m, p in (("POST", "/repos/OWNER/REPO/check-runs"), ("PATCH", "/repos/OWNER/REPO/check-runs/7"),
                     ("DELETE", "/repos/OWNER/REPO/check-runs/7"), ("POST", "/repos/OWNER/REPO/check-suites"),
                     ("PATCH", "/repos/OWNER/REPO/check-suites/preferences"), ("POST", "/repos/OWNER/REPO/check-suites/7/rerequest")):
            self.refused(m, p, {"name": "x", "conclusion": "success"}, codes=(403,))
        self.assertIsNone(self.d("POST", "/repos/OWNER/free/check-runs", {"name": "x", "head_sha": self.SHA}))

    def test_forgery_label_add_refused_on_gate_repo(self):
        self.refused("POST", "/repos/OWNER/REPO/issues/3/labels", {"labels": ["automerge"]}, codes=(403,))
        self.refused("POST", "/repos/OWNER/REPO/issues/3/labels", {"labels": ["AutoMerge"]}, codes=(403,))
        self.refused("POST", "/repos/OWNER/REPO/issues/3/labels", {"labels": [{"name": "hold"}]}, codes=(403,))
        self.refused("POST", "/repos/OWNER/REPO/issues/3/labels", ["automerge"], codes=(403,))  # legacy array body
        self.refused("POST", "/repos/OWNER/REPO/%69ssues/3/labels", {"labels": ["automerge"]}, codes=(403,))
        self.refused("POST", "/repos/OWNER/REPO/issues", {"title": "t", "labels": ["automerge"]}, codes=(403,))

    def test_forgery_label_remove_and_replace_refused_on_gate_repo(self):
        self.refused("DELETE", "/repos/OWNER/REPO/issues/3/labels/hold", codes=(403,))
        self.refused("DELETE", "/repos/OWNER/REPO/issues/3/labels/Hold", codes=(403,))
        self.refused("DELETE", "/repos/OWNER/REPO/issues/3/labels/%68old", codes=(403,))
        self.refused("DELETE", "/repos/OWNER/REPO/issues/3/labels", codes=(403,))  # clears every label
        self.refused("PUT", "/repos/OWNER/REPO/issues/3/labels", {"labels": ["bug"]}, codes=(403,))
        self.refused("PATCH", "/repos/OWNER/REPO/issues/3", {"labels": ["automerge"]}, codes=(403,))
        self.refused("PATCH", "/repos/OWNER/REPO/pulls/3", {"labels": []}, codes=(403,))
        self.refused("PATCH", "/repos/OWNER/REPO/labels/bug", {"new_name": "automerge"}, codes=(403,))
        self.refused("DELETE", "/repos/OWNER/REPO/labels/automerge", codes=(403,))

    def test_forgery_other_label_work_still_allowed(self):
        self.assertIsNone(self.d("POST", "/repos/OWNER/REPO/issues/3/labels", {"labels": ["bug", "docs"]}))
        self.assertIsNone(self.d("DELETE", "/repos/OWNER/REPO/issues/3/labels/bug"))
        self.assertIsNone(self.d("PATCH", "/repos/OWNER/REPO/issues/3", {"title": "new title"}))
        self.assertIsNone(self.d("POST", "/repos/OWNER/REPO/issues", {"title": "t", "labels": ["bug"]}))
        self.assertIsNone(self.d("POST", "/repos/OWNER/REPO/labels", {"name": "newlabel", "color": "ffffff"}))
        self.assertIsNone(self.d("GET", "/repos/OWNER/REPO/issues/3/labels"))

    def test_forgery_labels_free_on_non_gate_repo(self):
        self.assertIsNone(self.d("POST", "/repos/OWNER/free/issues/3/labels", {"labels": ["automerge"]}))
        self.assertIsNone(self.d("PUT", "/repos/OWNER/free/issues/3/labels", {"labels": ["automerge"]}))
        self.assertIsNone(self.d("PATCH", "/repos/OWNER/free/issues/3", {"labels": ["hold"]}))

    def test_forgery_custom_label_names_from_policy(self):
        r = g.decide_rest(POLICY_CUSTOM, "agent-a", "POST", "/repos/OWNER/REPO/issues/3/labels", b'{"labels":["ship-it"]}')
        self.assertEqual(r[0], 403)
        r = g.decide_rest(POLICY_CUSTOM, "agent-a", "DELETE", "/repos/OWNER/REPO/issues/3/labels/wait", b"")
        self.assertEqual(r[0], 403)
        self.assertIsNone(g.decide_rest(POLICY_CUSTOM, "agent-a", "POST", "/repos/OWNER/REPO/issues/3/labels", b'{"labels":["automerge"]}'))
        self.assertEqual((POLICY.merge_label, POLICY.hold_label), ("automerge", "hold"))


@contextlib.contextmanager
def dry_broker(policy):
    """A broker in dry-run mode on a free port; yields call(path, method, body) -> (status, json)."""
    with tempfile.TemporaryDirectory() as tmp:
        tokens, pol = os.path.join(tmp, "tokens"), os.path.join(tmp, "policy.json")
        with open(tokens, "w") as fh:
            fh.write("%s  agent-a\n" % hashlib.sha256(b"tok").hexdigest())
        with open(pol, "w") as fh:
            json.dump(policy, fh)
        cfg = {"tokens": tokens, "policy": pol, "token_file": os.path.join(tmp, "none"),
               "disable": os.path.join(tmp, "off"), "api_base": "http://upstream.invalid",
               "git_base": "http://upstream.invalid", "dry_run": True}
        srv = g.Broker(("localhost", 0), cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def call(path, method="GET", body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request("http://localhost:%d%s" % (srv.server_address[1], path), data=data,
                                         method=method, headers={"Authorization": "Bearer tok",
                                                                 "Content-Type": "application/json"})
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
        call.server = srv
        try:
            yield call
        finally:
            srv.shutdown()
            srv.server_close()


class GateHardening(Decide):
    """Status aliases, update-branch, protected heads, base changes, run deletion, write queries, policy checks."""
    policy = POLICY_FREE
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def test_status_alias_commits_sha_statuses_refused_on_gate_repo(self):
        for ref in (self.SHA, "main", "agent-a/x"):
            self.refused("POST", "/repos/OWNER/REPO/commits/%s/statuses" % ref,
                         {"state": "success", "context": "review/principal"}, codes=(403,))
        self.refused("POST", "/repos/OWNER/REPO/commits/%s/%%73tatuses" % self.SHA, {"state": "success"}, codes=(403,))
        self.assertIsNone(self.d("GET", "/repos/OWNER/REPO/commits/%s/statuses" % self.SHA))
        self.assertIsNone(self.d("POST", "/repos/OWNER/free/commits/%s/statuses" % self.SHA, {"state": "success"}))

    def test_gate_segments_refused_below_the_first_segment(self):
        for path in ("/commits/%s/check-runs" % self.SHA, "/commits/%s/check-suites" % self.SHA,
                     "/check-suites/7/check-runs", "/git/refs/statuses", "/x/y/z/statuses/1"):
            self.refused("POST", "/repos/OWNER/REPO" + path, {"name": "test", "conclusion": "success"}, codes=(403,))

    def test_update_branch_refused(self):
        for repo in ("REPO", "free"):
            self.refused("PUT", "/repos/OWNER/%s/pulls/7/update-branch" % repo, {}, codes=(403,))
            self.refused("PUT", "/repos/OWNER/%s/pulls/7/%%75pdate-branch" % repo, {}, codes=(403,))

    def test_pull_request_from_a_protected_head_refused(self):
        for head in ("main", "master", "OWNER:main", "refs/heads/main"):
            self.refused("POST", "/repos/OWNER/REPO/pulls", {"head": head, "base": "agent-a/x", "title": "t"}, codes=(403,))
            self.refused("POST", "/repos/OWNER/free/pulls", {"head": head, "base": "agent-a/x", "title": "t"}, codes=(403,))
        self.refused("POST", "/repos/OWNER/REPO/pulls", {"head": ["main"], "base": "x", "title": "t"}, codes=(403,))
        self.assertIsNone(self.d("POST", "/repos/OWNER/REPO/pulls", {"head": "agent-a/x", "base": "main", "title": "t"}))
        self.assertIsNone(self.d("POST", "/repos/OWNER/REPO/pulls", {"head": "OWNER:agent-a/x", "base": "main", "title": "t"}))

    def test_pull_request_base_change_refused_on_gate_repo(self):
        self.refused("PATCH", "/repos/OWNER/REPO/pulls/7", {"base": "main"}, codes=(403,))
        self.refused("PATCH", "/repos/OWNER/REPO/pulls/7", {"title": "t", "BASE": "main"}, codes=(403,))
        self.assertIsNone(self.d("PATCH", "/repos/OWNER/REPO/pulls/7", {"title": "new"}))
        self.assertIsNone(self.d("PATCH", "/repos/OWNER/free/pulls/7", {"base": "main"}))

    def test_run_delete_and_workflow_switch_refused_on_gate_repo(self):
        for m, p in (("DELETE", "/actions/runs/42"), ("DELETE", "/actions/runs/42/logs"), ("DELETE", "/actions/artifacts/9"),
                     ("PUT", "/actions/workflows/ci.yml/disable"), ("PUT", "/actions/workflows/123/enable")):
            self.refused(m, "/repos/OWNER/REPO" + p, codes=(403,))
        self.assertIsNone(self.d("POST", "/repos/OWNER/REPO/actions/runs/42/rerun", {}))
        self.assertIsNone(self.d("DELETE", "/repos/OWNER/free/actions/runs/42"))

    def test_query_string_refused_on_writes(self):
        with dry_broker({"merge_gate_repos": ["OWNER/REPO"], "agents": {"agent-a": {"OWNER/REPO": "write"}}}) as call:
            code, out = call("/gh/repos/OWNER/REPO/issues/1/labels?labels[]=automerge", "POST", {"labels": ["bug"]})
            self.assertEqual(code, 400, out)
            self.assertIn("query", out["reason"])
            code, out = call("/gh/repos/OWNER/REPO/contents/f.txt?branch=main", "PUT",
                             {"message": "m", "content": "eA==", "branch": "agent-a/x"})
            self.assertEqual(code, 400, out)
            code, out = call("/gh/repos/OWNER/REPO/issues/1/labels", "POST", {"labels": ["bug"]})
            self.assertEqual((code, out.get("would_call")), (200, "POST http://upstream.invalid/repos/OWNER/REPO/issues/1/labels"))
            code, out = call("/gh/repos/OWNER/REPO/pulls?state=open")
            self.assertEqual((code, out.get("would_call")), (200, "GET http://upstream.invalid/repos/OWNER/REPO/pulls?state=open"))

    def test_missing_or_misspelled_merge_gate_repos_fails_closed(self):
        agents = {"agent-a": {"OWNER/REPO": "write"}}
        for bad in ({"agents": agents},                                           # key missing
                    {"merge_gate_repo": ["OWNER/REPO"], "agents": agents},        # key misspelled
                    {"merge_gate_repos": "OWNER/REPO", "agents": agents},         # not a list
                    {"merge_gate_repos": ["OWNER/REPO "], "agents": agents},      # trailing space
                    {"merge_gate_repos": ["OWNER/REOP"], "agents": agents},       # names no agent repo
                    {"merge_gate_repos": ["OWNER/REPO"], "agents": agents, "merge_lable": "x"}):
            with self.assertRaises(ValueError, msg=json.dumps(bad)):
                g.Policy(bad)
        self.assertEqual(g.Policy({"merge_gate_repos": [], "agents": agents}).merge_gate_repos, set())
        self.assertEqual(g.Policy({"merge_gate_repos": ["owner/repo"], "agents": agents}).merge_gate_repos, {"owner/repo"})
        with self.assertRaises(ValueError):  # the broker refuses to start on such a policy
            with dry_broker({"agents": agents}):
                pass

    def test_gate_config_cross_check(self):
        pol = g.Policy({"merge_gate_repos": ["OWNER/REPO"], "agents": {"agent-a": {"OWNER/REPO": "write", "OWNER/b": "write"}}})
        self.assertEqual(g.gate_config_mismatch(pol, {"repos": ["OWNER/REPO"], "merge_label": "AutoMerge"}), [])
        problems = g.gate_config_mismatch(pol, {"repos": ["OWNER/REPO", "OWNER/b"], "hold_label": "wait"})
        self.assertEqual(len(problems), 2, problems)


class NumberSegments(Decide):
    """GitHub reads only the leading digits of an issue or pull request number; writes must spell it plainly."""
    policy = POLICY_FREE
    LOOSE = ("7abc", "7%20", "7.json", "7.0", "07", "+7", "7%2B", "0")

    def loose_writes(self, n, repo="REPO"):
        r = "/repos/OWNER/%s" % repo
        return (("PUT", r + "/pulls/%s/merge" % n, {"merge_method": "squash"}),
                ("POST", r + "/issues/%s/labels" % n, {"labels": ["automerge"]}),
                ("POST", r + "/issues/%s/labels" % n, {"labels": ["bug"]}),
                ("DELETE", r + "/issues/%s/labels/hold" % n, None),
                ("PUT", r + "/issues/%s/labels" % n, {"labels": ["bug"]}),
                ("DELETE", r + "/issues/%s/labels" % n, None),
                ("PATCH", r + "/issues/%s" % n, {"labels": ["automerge"]}),
                ("PATCH", r + "/pulls/%s" % n, {"labels": ["automerge"]}),
                ("PUT", r + "/pulls/%s/update-branch" % n, {}),
                ("PATCH", r + "/pulls/%s" % n, {"base": "main"}),
                ("POST", r + "/issues/%s/comments" % n, {"body": "x"}))

    def test_loose_number_segments_refused_on_every_write(self):
        for n in self.LOOSE:
            for repo in ("REPO", "free"):
                for method, path, body in self.loose_writes(n, repo):
                    r = self.refused(method, path, body, codes=(400,))
                    if "%2B" not in n:
                        self.assertIn("plain number", r[1], (method, path))

    def test_loose_comment_review_and_reaction_ids_refused(self):
        for path in ("/issues/comments/5abc", "/pulls/comments/05", "/issues/comments/5/reactions/1x",
                     "/pulls/7/reviews/3.0/events", "/pulls/7/comments/3abc/replies", "/issues/7/reactions/9%20"):
            self.refused("POST", "/repos/OWNER/free" + path, {"body": "x", "event": "COMMENT"}, codes=(400,))

    def test_plain_numbers_keep_their_answers(self):
        r = "/repos/OWNER/REPO"
        for method, path, body in self.loose_writes("7"):
            got = self.d(method, path, body)
            if path.endswith("/comments") or body == {"labels": ["bug"]} and method == "POST":
                self.assertIsNone(got, (method, path))
            else:
                self.assertEqual(got[0], 403, (method, path, got))
        for method, path, body in self.loose_writes("7", "free"):
            got = self.d(method, path, body)
            if path.endswith("update-branch"):
                self.assertEqual(got[0], 403, (method, path, got))
            else:
                self.assertIsNone(got, (method, path, got))
        for method, path in (("PATCH", r + "/issues/comments/5"), ("DELETE", r + "/pulls/comments/5"),
                             ("POST", r + "/issues/comments/5/reactions"), ("DELETE", r + "/issues/7/reactions/12"),
                             ("POST", r + "/pulls/7/reviews/3/events"), ("POST", r + "/pulls/7/comments/3/replies"),
                             ("POST", r + "/pulls/7/requested_reviewers"), ("PUT", r + "/issues/7/lock"),
                             ("DELETE", r + "/issues/7/labels/bug"), ("POST", r + "/issues"), ("POST", r + "/pulls")):
            body = {"body": "x", "head": "agent-a/x", "base": "main", "title": "t"}
            self.assertIsNone(self.d(method, path, body), (method, path))
        self.assertIsNone(self.d("GET", r + "/pulls/7abc"))  # reads are not screened

    def test_loose_number_refused_by_the_running_broker(self):
        with dry_broker({"merge_gate_repos": ["OWNER/REPO"], "agents": {"agent-a": {"OWNER/REPO": "write"}}}) as call:
            for n in ("7abc", "7%20", "7.json"):
                code, out = call("/gh/repos/OWNER/REPO/pulls/%s/merge" % n, "PUT", {"merge_method": "squash"})
                self.assertEqual(code, 400, out)
            code, out = call("/gh/repos/OWNER/REPO/pulls/7/merge", "PUT", {"merge_method": "squash"})
            self.assertEqual(code, 403, out)

    def test_run_and_deployment_approvals_refused_on_every_repo(self):
        # The broker's account may be a required reviewer of an environment on any repo, gated or not.
        for repo in ("REPO", "free"):
            for route in ("pending_deployments", "approve", "deployment_protection_rule"):
                r = self.refused("POST", "/repos/OWNER/%s/actions/runs/42/%s" % (repo, route),
                                 {"environment_ids": [1], "state": "approved", "comment": "x"}, codes=(403,))
                self.assertIn("approvals are refused", r[1])
            self.assertIsNone(self.d("POST", "/repos/OWNER/%s/actions/runs/42/rerun" % repo, {}))
            self.assertIsNone(self.d("POST", "/repos/OWNER/%s/actions/runs/42/cancel" % repo, {}))

    def test_pull_request_head_must_be_a_plain_branch_name(self):
        for head in (" main", "main ", "a:b:main", "OWNER:main ", "OWNER: main", "", "ma\tin"):
            self.refused("POST", "/repos/OWNER/free/pulls", {"head": head, "base": "x", "title": "t"}, codes=(400,))
        self.assertIsNone(self.d("POST", "/repos/OWNER/free/pulls", {"head": "OWNER:agent-a/x", "base": "main", "title": "t"}))


class RouteNames(Decide):
    """Route names on writes are matched exactly: a suffix or odd character on one is refused, on every repo."""
    policy = POLICY_FREE

    def test_route_name_with_a_suffix_or_odd_character_refused_on_every_write(self):
        body = {"merge_method": "squash", "labels": ["bug"], "state": "approved", "environment_ids": [1],
                "sha": "a" * 40, "context": "x", "name": "x", "head_sha": "a" * 40}
        for repo in ("REPO", "free"):
            r = "/repos/OWNER/" + repo
            for method, path in (("PUT", "/pulls/7/merge.json"), ("PUT", "/pulls/7/merge."), ("PUT", "/pulls/7/merge;x"),
                                 ("PUT", "/pulls/7/merge.JSON"), ("POST", "/issues/7/labels.json"),
                                 ("PUT", "/issues/7/labels.json"), ("DELETE", "/issues/7/labels.json"),
                                 ("PUT", "/pulls/7/update-branch.json"), ("POST", "/issues/comments/5/reactions.json"),
                                 ("POST", "/actions/runs/42/approve.json"),
                                 ("POST", "/actions/runs/42/pending_deployments.json"),
                                 ("POST", "/actions/runs/42/deployment_protection_rule.json"),
                                 ("POST", "/actions/workflows.json/1/dispatches"),
                                 ("POST", "/labels.json"), ("POST", "/statuses.json/" + "a" * 40),
                                 ("POST", "/check-runs.json"), ("POST", "/commits/abc/statuses.json"),
                                 ("POST", "/commits/abc/check-runs.json"), ("POST", "/pulls/7/merge%2Ejson")):
                got = self.refused(method, r + path, body, codes=(400,))
                if ";" not in path and "%" not in path:  # refused earlier: path parameter, encoded dot
                    self.assertIn("route name", got[1], (method, path))

    def test_update_branch_with_a_trailing_segment_refused(self):
        for repo in ("REPO", "free"):
            self.refused("PUT", "/repos/OWNER/%s/pulls/7/update-branch/x" % repo, {}, codes=(403,))

    def test_values_with_dots_keep_their_answers(self):
        r = "/repos/OWNER/free"
        for method, path, body in (("PUT", r + "/contents/docs/merge.json", {"branch": "agent-a/x", "message": "m", "content": ""}),
                                   ("PUT", r + "/contents/labels.json", {"branch": "agent-a/x", "message": "m", "content": ""}),
                                   ("DELETE", r + "/issues/7/labels/v1.2", None),
                                   ("PATCH", r + "/labels/v1.2", {"color": "fff"}),
                                   ("PATCH", r + "/git/refs/heads/release-1.2", {"sha": "a" * 40}),
                                   ("POST", r + "/actions/workflows/ci.yml/dispatches", {"ref": "agent-a/x"}),
                                   ("POST", r + "/pulls/7/requested_reviewers", {"reviewers": ["x"]}),
                                   ("POST", r + "/actions/runs/42/rerun-failed-jobs", {})):
            self.assertIsNone(self.d(method, path, body), (method, path))


class PolicyReload(unittest.TestCase):
    """A policy edit that does not load must never leave the older (possibly looser) policy in force."""

    def test_failed_reload_refuses_every_call_until_a_valid_policy_loads(self):
        good = {"merge_gate_repos": ["OWNER/REPO"], "agents": {"agent-a": {"OWNER/REPO": "write"}}}
        with tempfile.TemporaryDirectory() as tmp:
            tokens, pol = os.path.join(tmp, "tokens"), os.path.join(tmp, "policy.json")
            with open(tokens, "w") as fh:
                fh.write("%s  agent-a\n" % hashlib.sha256(b"tok").hexdigest())

            def write(doc, stamp):
                with open(pol, "w") as fh:
                    fh.write(doc if isinstance(doc, str) else json.dumps(doc))
                os.utime(pol, ns=(stamp, stamp))
            write(good, 1_000_000_000)
            srv = g.Broker(("localhost", 0), {"tokens": tokens, "policy": pol, "token_file": os.path.join(tmp, "none"),
                                              "disable": os.path.join(tmp, "off"), "api_base": "http://x.invalid",
                                              "git_base": "http://x.invalid", "dry_run": True})
            try:
                self.assertIsNotNone(srv.policy_now())
                # The operator takes agent-a's only access away and leaves the repo gated: Policy() refuses it.
                write({"merge_gate_repos": ["OWNER/REPO"], "agents": {}}, 2_000_000_000)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertIsNone(srv.policy_now())
                    self.assertIsNone(srv.policy_now())
                log = out.getvalue()
                self.assertIn("ValueError: merge_gate_repos entry 'OWNER/REPO' matches no repo in agents", log)
                self.assertEqual(log.count("policy reload failed"), 1, log)
                write("{not json", 3_000_000_000)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(srv.policy_now())
                os.unlink(pol)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(srv.policy_now())
                write({"merge_gate_repos": [], "agents": {"agent-b": {"OWNER/REPO": "write"}}}, 4_000_000_000)
                self.assertEqual(srv.policy_now().access("agent-a", "OWNER/REPO"), None)
            finally:
                srv.server_close()

    def test_running_broker_answers_503_while_the_policy_does_not_load(self):
        policy = {"merge_gate_repos": ["OWNER/REPO"], "agents": {"agent-a": {"OWNER/REPO": "write"}}}
        with dry_broker(policy) as call:
            self.assertEqual(call("/gh/repos/OWNER/REPO/pulls")[0], 200)
            srv = call.server
            with open(srv.cfg["policy"], "w") as fh:
                json.dump({"merge_gate_repos": ["OWNER/REPO"], "agents": {}}, fh)
            os.utime(srv.cfg["policy"], ns=(9_000_000_000, 9_000_000_000))
            code, out = call("/gh/repos/OWNER/REPO/pulls")
            self.assertEqual(code, 503, out)
            code, out = call("/gh/repos/OWNER/REPO/issues/1/comments", "POST", {"body": "x"})
            self.assertEqual(code, 503, out)

    def test_healthz_is_not_ok_while_no_policy_is_in_force(self):
        policy = {"merge_gate_repos": ["OWNER/REPO"], "agents": {"agent-a": {"OWNER/REPO": "write"}}}
        with dry_broker(policy) as call:
            self.assertEqual(call("/healthz"), (200, {"ok": True}))
            srv = call.server
            with open(srv.cfg["policy"], "w") as fh:
                fh.write("{not json")
            os.utime(srv.cfg["policy"], ns=(9_000_000_000, 9_000_000_000))
            code, out = call("/healthz")
            self.assertEqual(code, 503, out)
            self.assertIs(out.get("ok"), False, out)
            with open(srv.cfg["policy"], "w") as fh:
                json.dump(policy, fh)
            os.utime(srv.cfg["policy"], ns=(9_500_000_000, 9_500_000_000))
            self.assertEqual(call("/healthz"), (200, {"ok": True}))


class Admin(Decide):
    policy = POLICY_FREE

    def test_admin_repo_settings_refused(self):
        self.refused("PATCH", "/repos/OWNER/free", {"private": False}, codes=(403,))
        self.refused("PATCH", "/repos/OWNER/free/", {"default_branch": "x"}, codes=(403,))
        self.refused("PATCH", "/repos/OWNER/REPO", {"private": False}, codes=(403,))
        self.refused("DELETE", "/repos/OWNER/free", codes=(403,))

    def test_admin_listed_paths_refused(self):
        paths = [
            ("PUT", "/actions/permissions"), ("PUT", "/actions/permissions/workflow"), ("PUT", "/actions/secrets/X"),
            ("PUT", "/actions/variables/X"), ("POST", "/actions/variables"), ("PUT", "/environments/prod"),
            ("PUT", "/environments/prod/secrets/X"), ("PUT", "/secrets/X"), ("PUT", "/dependabot/secrets/X"),
            ("PUT", "/codespaces/secrets/X"), ("POST", "/pages"), ("PUT", "/pages"), ("POST", "/visibility"),
            ("POST", "/merge-upstream"), ("POST", "/autolinks"), ("POST", "/deployment-protection-rules"),
            ("PUT", "/branches/main/protection"), ("PUT", "/branches/feature/x/protection/required_status_checks"),
            ("POST", "/rules"), ("POST", "/rulesets"), ("PUT", "/rulesets/4"), ("POST", "/hooks"), ("PATCH", "/hooks/3"),
            ("POST", "/keys"), ("PUT", "/collaborators/u"), ("PATCH", "/invitations/3"), ("POST", "/transfer"),
            ("POST", "/actions/runners/registration-token"), ("PUT", "/actions/oidc/customization/sub"),
            ("PUT", "/vulnerability-alerts"), ("PUT", "/automated-security-fixes"),
            ("PUT", "/private-vulnerability-reporting"), ("PUT", "/interaction-limits"), ("PUT", "/teams/t"),
        ]
        for m, p in paths:
            for repo in ("free", "REPO"):
                self.refused(m, "/repos/OWNER/%s%s" % (repo, p), {"x": 1}, codes=(403,))

    def test_admin_match_is_on_segments_not_substrings(self):
        for p in ("/contents/hooks.md", "/contents/docs/keys.txt", "/contents/hooks/run.sh", "/contents/secrets-howto.md",
                  "/git/refs/heads/hooks", "/issues/3/comments"):
            body = {"message": "m", "content": "e", "branch": "topic", "ref": "refs/heads/x", "sha": "a", "body": "b"}
            self.assertIsNone(self.d("PUT" if p.startswith("/contents") else "POST", "/repos/OWNER/free" + p, body), p)

    def test_admin_contents_hooks_md_allowed_on_non_protected_branch(self):
        self.assertIsNone(self.d("PUT", "/repos/OWNER/free/contents/hooks.md", {"message": "m", "content": "e", "branch": "topic"}))
        self.assertEqual(self.d("PUT", "/repos/OWNER/free/contents/hooks.md", {"message": "m", "content": "e", "branch": "main"})[0], 403)

    def test_admin_topics_allowed_and_reads_unaffected(self):
        self.assertIsNone(self.d("PUT", "/repos/OWNER/free/topics", {"names": ["a"]}))
        self.assertIsNone(self.d("GET", "/repos/OWNER/free/hooks"))
        self.assertIsNone(self.d("GET", "/repos/OWNER/free/branches/main/protection"))
        self.assertIsNone(self.d("GET", "/repos/OWNER/free"))


class Git(unittest.TestCase):
    def d(self, agent, method, sub, query="", body=b""):
        return g.decide_git(POLICY, agent, method, "OWNER/REPO", sub, query, body)

    def test_flow(self):
        self.assertIsNone(self.d("agent-a", "GET", "info/refs", "service=git-upload-pack"))
        self.assertIsNone(self.d("agent-b", "GET", "info/refs", "service=git-upload-pack"))
        self.assertEqual(self.d("agent-b", "GET", "info/refs", "service=git-receive-pack")[0], 403)
        self.assertEqual(self.d("agent-a", "GET", "info/refs", "service=other")[0], 400)
        self.assertIsNone(self.d("agent-a", "POST", "git-receive-pack", body=push("refs/heads/topic")))
        self.assertEqual(self.d("agent-a", "POST", "git-receive-pack", body=push("refs/heads/main"))[0], 403)
        self.assertEqual(self.d("agent-a", "POST", "git-receive-pack", body=push("refs/heads/release/2"))[0], 403)
        self.assertEqual(self.d("agent-a", "POST", "git-receive-pack", body=b"garbage")[0], 400)
        self.assertEqual(self.d("agent-b", "POST", "git-receive-pack", body=push("refs/heads/topic"))[0], 403)
        self.assertEqual(self.d("agent-a", "POST", "git-receive-pack", body=pkt(b"push-cert\0x") + b"0000")[0], 400)


class Budget(unittest.TestCase):
    def test_window(self):
        b = g.Budget()
        self.assertEqual([b.take("a", 2, now=100), b.take("a", 2, now=101)], [0, 0])
        wait = b.take("a", 2, now=102)
        self.assertGreater(wait, 0)
        self.assertEqual(b.take("a", 2, now=100 + 3601), 0)
        self.assertEqual(b.take("other", 2, now=102), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
