#!/usr/bin/env python3
"""Offline tests for merge-gate: no network, no token. Run: python3 test_merge_gate.py"""
import importlib.machinery, importlib.util, os, unittest

here = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("merge_gate", os.path.join(here, "merge-gate"))
spec = importlib.util.spec_from_loader("merge_gate", loader)
mg = importlib.util.module_from_spec(spec)
loader.exec_module(mg)

OWNER = "OWNER@users.noreply.github.com"
SIGNER = "review-bot-account"
CFG = {**mg.DEFAULTS, "prefixes": ["agent-a/", "docs/"], "owner_emails": [OWNER], "base": "main",
       "signer_login": SIGNER, "broker_login": "owner-account"}
PCFG = {**CFG, "required_checks": ["test"]}  # process_pr tests
# Neutral placeholder pattern for the commit message policy tests (the default list is empty).
PATTERN = r"^signed-off-by-bot:"
MARKER = "Signed-off-by-bot: example"


def pr(labels=("automerge",), draft=False, ref="agent-a/fix", base="main", title="Fix a bug", body="Details."):
    return {"draft": draft, "labels": [{"name": n} for n in labels], "head": {"ref": ref, "sha": "a" * 40},
            "base": {"ref": base}, "title": title, "body": body}


def commit(msg="fix", author=OWNER, committer=OWNER):
    return {"commit": {"message": msg, "author": {"email": author}, "committer": {"email": committer}}}


class Tiers(unittest.TestCase):
    def test_high(self):
        for p in [".github/workflows/ci.yml", "db/migrations/001.sql", "CODEOWNERS", "svc/auth/login.py", "x/CODEOWNERS"]:
            self.assertEqual(mg.tier_for(CFG, ["README.md", p]), "high", p)

    def test_low_and_medium(self):
        self.assertEqual(mg.tier_for(CFG, ["docs/a/b.md", "README.md"]), "low")
        self.assertEqual(mg.tier_for(CFG, ["docs/a.md", "src/app.py"]), "medium")
        self.assertEqual(mg.tier_for(CFG, ["src/author.py"]), "medium")  # 'auth' must be a whole directory name


class Reviews(unittest.TestCase):
    def test_low_medium_need_lead(self):
        for tier in ("low", "medium"):
            self.assertIsNotNone(mg.review_reason(CFG, tier, 10, {}))
            self.assertIsNone(mg.review_reason(CFG, tier, 10, {"review/lead": "success"}))
            self.assertIsNotNone(mg.review_reason(CFG, tier, 10, {"review/lead": "pending"}))

    def test_high_needs_both(self):
        self.assertIsNotNone(mg.review_reason(CFG, "high", 10, {"review/lead": "success"}))
        self.assertIsNone(mg.review_reason(CFG, "high", 10, {"review/lead": "success", "review/principal": "success"}))

    def test_medium_line_cap(self):
        ok = {"review/lead": "success"}
        self.assertIsNone(mg.review_reason(CFG, "medium", 400, ok))
        self.assertIn("too large", mg.review_reason(CFG, "medium", 401, ok))
        self.assertIsNone(mg.review_reason(CFG, "low", 5000, ok))


class Checks(unittest.TestCase):
    def test_required_list(self):
        cfg = {**CFG, "required_checks": ["test"]}
        self.assertIsNone(mg.checks_reason(cfg, {"test": "success", "other": "failure"}))
        self.assertIsNotNone(mg.checks_reason(cfg, {"test": "pending"}))
        self.assertIsNotNone(mg.checks_reason(cfg, {}))

    def test_all_reported(self):
        self.assertIsNotNone(mg.checks_reason(CFG, {}))                       # none present
        self.assertIsNotNone(mg.checks_reason(CFG, {"review/lead": "success"}))  # reviews do not count as CI
        self.assertIsNone(mg.checks_reason(CFG, {"a": "success", "b": "skipped"}))
        self.assertIsNotNone(mg.checks_reason(CFG, {"a": "success", "b": "failure"}))
        self.assertIsNotNone(mg.checks_reason(CFG, {"a": "success", "b": "pending"}))
        self.assertIsNone(mg.checks_reason(CFG, {"a": "success", "merge-gate": "failure"}))  # own status ignored


class Rules(unittest.TestCase):
    def test_ok(self): self.assertIsNone(mg.gate_reason(CFG, pr()))
    def test_draft(self): self.assertEqual(mg.gate_reason(CFG, pr(draft=True)), "draft")
    def test_no_label(self): self.assertIn("label", mg.gate_reason(CFG, pr(labels=())))
    def test_hold(self): self.assertIn("hold", mg.gate_reason(CFG, pr(labels=("automerge", "hold"))))
    def test_prefix(self):
        self.assertIsNone(mg.gate_reason(CFG, pr(ref="docs/readme")))
        self.assertIn("prefix", mg.gate_reason(CFG, pr(ref="feature/x")))
    def test_base(self): self.assertIn("base", mg.gate_reason(CFG, pr(base="dev")))
    def test_custom_label(self):
        self.assertIsNone(mg.gate_reason({**CFG, "merge_label": "ship"}, pr(labels=("ship",))))


class MessagePolicy(unittest.TestCase):
    CFG_P = {**CFG, "extra_message_patterns": [PATTERN]}

    def check(self, p=None, commits=None, cfg=None):
        return mg.message_policy_problems(cfg or self.CFG_P, p or pr(), commits or [commit()])

    def test_clean(self): self.assertEqual(self.check(), (0, False))

    def test_marker_in_commit(self):
        self.assertEqual(self.check(commits=[commit("fix\n\n" + MARKER), commit()])[0], 1)
        self.assertEqual(self.check(commits=[commit("fix\n\n" + MARKER.upper())])[0], 1)  # case-insensitive

    def test_marker_in_pr_text(self):
        self.assertTrue(self.check(p=pr(body="x\n" + MARKER))[1])
        self.assertTrue(self.check(p=pr(title=MARKER))[1])
        self.assertFalse(self.check(p=pr(body="x " + MARKER))[1])  # pattern is anchored to a line start

    def test_foreign_email(self):
        self.assertEqual(self.check(commits=[commit(author="x@example.com")])[0], 1)
        self.assertEqual(self.check(commits=[commit(committer="noreply@github.com")])[0], 1)
        self.assertEqual(self.check(commits=[commit(author=OWNER.upper())])[0], 0)  # case-insensitive

    def test_empty_default_flags_nothing(self):
        self.assertEqual(mg.DEFAULTS["extra_message_patterns"], [])
        self.assertEqual(mg.message_policy_problems(CFG, pr(title=MARKER, body=MARKER), [commit(MARKER)]), (0, False))
        self.assertEqual(mg.message_policy_problems(CFG, pr(), [commit(author="x@example.com")])[0], 1)  # identity still checked

    def test_several_patterns(self):
        cfg = {**CFG, "extra_message_patterns": [PATTERN, r"\bexample-marker\b"]}
        self.assertEqual(mg.message_policy_problems(cfg, pr(), [commit("Example-Marker refactor")])[0], 1)
        self.assertEqual(mg.message_policy_problems(cfg, pr(body=MARKER), [commit()]), (0, True))

    def test_unmatched_text_passes(self):
        p = pr(title="Document the bot that signs off releases", body="The README now explains sign-off.")
        c = commit("docs: Signed-off-by: Alice <alice@example.com>")
        self.assertEqual(self.check(p=p, commits=[c]), (0, False))

    def test_description(self):
        self.assertEqual(mg.message_policy_description(2, False), "commit message policy failed: 2 commits")
        self.assertEqual(mg.message_policy_description(1, True), "commit message policy failed: 1 commit, PR text")
        self.assertEqual(mg.message_policy_description(0, True), "commit message policy failed: PR text")


class Config(unittest.TestCase):
    def test_example_loads(self):
        cfg = mg.load_config(os.path.join(here, "config.example.json"))
        self.assertEqual(cfg["merge_method"], "squash")

    def test_fail_closed(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"repos": ["OWNER/REPO"], "prefixes": ["a/"]}, f)
        try: self.assertRaises(ValueError, mg.load_config, f.name)  # no owner_emails
        finally: os.unlink(f.name)

    def test_bad_message_patterns_refused(self):
        import json, tempfile
        base = json.load(open(os.path.join(here, "config.example.json")))
        self.assertEqual(mg.load_config(os.path.join(here, "config.example.json"))["extra_message_patterns"], [PATTERN])
        for bad in (["("], [""], [3], "^x"):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump({**base, "extra_message_patterns": bad}, f)
            try: self.assertRaises(ValueError, mg.load_config, f.name)
            finally: os.unlink(f.name)


SHA = "a" * 40
LEAD, PRINCIPAL = "review/lead", "review/principal"


class FakeGh:
    """Stands in for Gh: serves canned lists by URL fragment and records every write."""
    def __init__(self, statuses=(), runs=(), files=None, commits=None):
        self.statuses, self.runs = list(statuses), list(runs)
        self.files = files if files is not None else [{"filename": "src/app.py", "additions": 5, "deletions": 1}]
        self.commits = commits if commits is not None else [commit()]
        self.posts, self.puts = [], []

    def pages(self, path, key=None):
        if path.endswith("/check-runs"): return list(self.runs)
        if path.endswith("/statuses"): return list(self.statuses)
        if path.endswith("/commits"): return list(self.commits)
        if path.endswith("/files"): return list(self.files)
        raise AssertionError("unexpected pages() " + path)

    def get(self, path): raise AssertionError("unexpected get() " + path)

    def post(self, path, body):
        self.posts.append((path, body))
        return 201, {}

    def put(self, path, body):
        self.puts.append((path, body))
        return 200, {}


def status(ctx, state, login=SIGNER, sid=1):
    return {"id": sid, "context": ctx, "state": state, "created_at": "2026-01-01T00:00:%02dZ" % sid,
            "creator": {"login": login}}


def run(name, conclusion="success", app=None, completed=True):
    return {"name": name, "status": "completed" if completed else "in_progress", "conclusion": conclusion if completed else None,
            "app": {"id": app} if app is not None else None}


def reviews(login=SIGNER, base=10):
    return [status(LEAD, "success", login, base), status(PRINCIPAL, "success", login, base + 1)]


def gate(gh, cfg=PCFG, pr_=None, dry=False):
    st = {"done": {}, "attempts": {}, "logged": {}}
    p = pr_ or {**pr(), "number": 7}
    mg.process_pr(gh, cfg, "OWNER/REPO", p, st, dry)
    return st


HIGH_FILES = [{"filename": ".github/workflows/ci.yml", "additions": 2, "deletions": 0}]


class HeadStates(unittest.TestCase):
    def states(self, gh, cfg=PCFG): return mg.head_states(gh, "OWNER/REPO", SHA, cfg)

    def test_review_status_from_non_signer_is_ignored(self):
        s = self.states(FakeGh(statuses=reviews("someone-else")))
        self.assertNotIn(LEAD, s)
        self.assertNotIn(PRINCIPAL, s)

    def test_review_status_from_signer_counts_case_insensitive(self):
        s = self.states(FakeGh(statuses=reviews(SIGNER.upper())))
        self.assertEqual((s[LEAD], s[PRINCIPAL]), ("success", "success"))

    def test_newest_signer_status_wins_and_forged_one_cannot_shadow_it(self):
        gh = FakeGh(statuses=[status(LEAD, "success", "someone-else", 5), status(LEAD, "failure", SIGNER, 4),
                              status(LEAD, "success", SIGNER, 2)])
        self.assertEqual(self.states(gh)[LEAD], "failure")
        gh = FakeGh(statuses=[status(LEAD, "pending", "someone-else", 5), status(LEAD, "success", SIGNER, 2)])
        self.assertEqual(self.states(gh)[LEAD], "success")

    def test_check_run_cannot_stand_in_for_a_review(self):
        self.assertNotIn(LEAD, self.states(FakeGh(runs=[run(LEAD)])))

    def test_no_signer_login_counts_no_review(self):
        s = self.states(FakeGh(statuses=reviews()), {**PCFG, "signer_login": ""})
        self.assertNotIn(LEAD, s)

    def test_worse_of_status_and_check_run(self):
        self.assertEqual(self.states(FakeGh(statuses=[status("test", "success")], runs=[run("test", "failure")]))["test"], "failure")
        self.assertEqual(self.states(FakeGh(statuses=[status("test", "failure")], runs=[run("test", "success")]))["test"], "failure")
        self.assertEqual(self.states(FakeGh(statuses=[status("test", "success")], runs=[run("test", completed=False)]))["test"], "pending")
        self.assertEqual(self.states(FakeGh(statuses=[status("test", "pending")], runs=[run("test")]))["test"], "pending")
        self.assertEqual(self.states(FakeGh(statuses=[status("test", "success")], runs=[run("test", "cancelled")]))["test"], "cancelled")
        self.assertEqual(self.states(FakeGh(statuses=[status("test", "success")], runs=[run("test", "skipped")]))["test"], "skipped")
        self.assertEqual(self.states(FakeGh(statuses=[status("test", "success")], runs=[run("test")]))["test"], "success")

    def test_two_runs_same_name_keep_worse(self):
        self.assertEqual(self.states(FakeGh(runs=[run("test", app=1), run("test", "timed_out", app=2)]))["test"], "timed_out")

    def test_latest_status_per_context_is_used(self):
        gh = FakeGh(runs=[run("test")], statuses=[status("test", "success", sid=9), status("test", "failure", sid=3)])
        self.assertEqual(self.states(gh)["test"], "success")
        gh = FakeGh(runs=[run("test")], statuses=[status("test", "failure", sid=9), status("test", "success", sid=3)])
        self.assertEqual(self.states(gh)["test"], "failure")

    def test_check_app_ids(self):
        cfg = {**PCFG, "check_app_ids": {"test": 15368}}
        self.assertEqual(self.states(FakeGh(runs=[run("test", app=15368)]), cfg)["test"], "success")
        self.assertEqual(self.states(FakeGh(runs=[run("test", app=999)]), cfg)["test"], "missing")  # wrong app ignored
        self.assertEqual(self.states(FakeGh(statuses=[status("test", "success")]), cfg)["test"], "missing")  # status cannot supply it
        self.assertEqual(self.states(FakeGh(runs=[run("test", app=15368)], statuses=[status("test", "failure")]), cfg)["test"], "failure")
        self.assertEqual(self.states(FakeGh(runs=[run("test", "failure", app=999), run("test", app=15368)]), cfg)["test"], "success")


class ProcessPr(unittest.TestCase):
    def ok_gh(self, **kw):
        kw.setdefault("runs", [run("test")])
        kw.setdefault("statuses", reviews())
        return FakeGh(**kw)

    def test_all_green_merges_pinned_to_sha(self):
        gh = self.ok_gh()
        st = gate(gh)
        self.assertEqual(len(gh.puts), 1)
        path, body = gh.puts[0]
        self.assertEqual(path, "/repos/OWNER/REPO/pulls/7/merge")
        self.assertEqual((body["sha"], body["merge_method"]), (SHA, "squash"))
        self.assertEqual(list(st["done"].values()), ["merged"])

    def test_high_tier_forged_reviews_do_not_merge(self):
        gh = self.ok_gh(statuses=reviews("someone-else"), files=HIGH_FILES)
        gate(gh)
        self.assertEqual(gh.puts, [])

    def test_high_tier_signer_reviews_merge(self):
        gh = self.ok_gh(statuses=reviews(SIGNER), files=HIGH_FILES)
        gate(gh)
        self.assertEqual(len(gh.puts), 1)

    def test_forged_lead_only_does_not_merge_medium(self):
        gh = self.ok_gh(statuses=[status(LEAD, "success", "someone-else")])
        gate(gh)
        self.assertEqual(gh.puts, [])

    def test_high_tier_one_forged_one_real_does_not_merge(self):
        gh = self.ok_gh(statuses=[status(LEAD, "success", SIGNER, 1), status(PRINCIPAL, "success", "someone-else", 2)], files=HIGH_FILES)
        gate(gh)
        self.assertEqual(gh.puts, [])

    def test_success_status_does_not_hide_failing_check_run(self):
        gh = self.ok_gh(runs=[run("test", "failure")], statuses=reviews() + [status("test", "success", "anyone", 20)])
        gate(gh)
        self.assertEqual(gh.puts, [])

    def test_success_status_does_not_hide_pending_check_run(self):
        gh = self.ok_gh(runs=[run("test", completed=False)], statuses=reviews() + [status("test", "success", "anyone", 20)])
        gate(gh)
        self.assertEqual(gh.puts, [])

    def test_failing_status_does_not_yield_to_success_check_run(self):
        gh = self.ok_gh(runs=[run("test", "success")], statuses=reviews() + [status("test", "failure", "anyone", 20)])
        gate(gh)
        self.assertEqual(gh.puts, [])

    def test_success_status_alone_does_not_satisfy_required_check(self):
        """An agent posts the CI check names itself as statuses; only check runs make a check green."""
        gh = self.ok_gh(runs=[], statuses=reviews() + [status("test", "success", "ci", 20)])
        gate(gh)
        self.assertEqual(gh.puts, [])
        gh = self.ok_gh(runs=[], statuses=reviews() + [status("test", "success", "agent", 20), status("lint", "success", "agent", 21)])
        gate(gh, {**PCFG, "required_checks": []})  # "every reported check" mode: statuses alone report nothing green
        self.assertEqual(gh.puts, [])

    def test_status_states_never_create_green_but_still_block(self):
        s = mg.head_states(FakeGh(statuses=[status("test", "success", "x", 5), status("lint", "pending", "x", 6),
                                            status("e2e", "failure", "x", 7)]), "OWNER/REPO", SHA, PCFG)
        self.assertEqual(s, {"lint": "pending", "e2e": "failure"})

    def test_signer_shared_with_broker_or_gate_account_fails_closed(self):
        """On a one-account host every status an agent posts through the broker carries the signer's login."""
        for cfg in ({**PCFG, "broker_login": SIGNER.upper()}, {**PCFG, "_gate_login": " " + SIGNER}):
            gh = self.ok_gh(files=HIGH_FILES)
            gate(gh, cfg)
            self.assertEqual(gh.puts, [])
            self.assertEqual(gh.posts[0][1]["state"], "failure")
            self.assertIn("dedicated account", gh.posts[0][1]["description"])
        gh = self.ok_gh(files=HIGH_FILES)
        gate(gh, {**PCFG, "broker_login": "owner-account", "_gate_login": "gate-account"})
        self.assertEqual(len(gh.puts), 1)

    def test_config_refuses_signer_equal_to_broker_login(self):
        import json, tempfile
        with open(os.path.join(here, "config.example.json")) as f: raw = json.load(f)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({**raw, "broker_login": raw["signer_login"].upper()}, f)
        try: self.assertRaises(ValueError, mg.load_config, f.name)
        finally: os.unlink(f.name)

    def test_empty_broker_login_refuses_every_merge_and_says_so(self):
        for cfg in ({**PCFG, "broker_login": ""}, {**PCFG, "broker_login": "  "},
                    {k: v for k, v in PCFG.items() if k != "broker_login"}):
            gh = self.ok_gh(files=HIGH_FILES)
            gate(gh, cfg)
            self.assertEqual(gh.puts, [])
            self.assertEqual((gh.posts[0][1]["state"], gh.posts[0][1]["context"]), ("failure", "merge-gate"))
            self.assertIn("broker_login", gh.posts[0][1]["description"])
        gh = self.ok_gh(files=HIGH_FILES)  # "-" says no broker runs on this host
        gate(gh, {**PCFG, "broker_login": "-", "_gate_login": "gate-account"})
        self.assertEqual(len(gh.puts), 1)

    def test_signer_compared_casefolded_with_every_other_account(self):
        for cfg in ({**PCFG, "signer_login": SIGNER.upper(), "broker_login": SIGNER},
                    {**PCFG, "signer_login": " " + SIGNER.title(), "_gate_login": SIGNER}):
            self.assertEqual(mg.signer_reason(cfg), mg.SHARED_SIGNER)

    def test_main_refuses_to_run_on_an_empty_own_login(self):
        import contextlib, io, tempfile
        with tempfile.TemporaryDirectory() as d:
            tok = os.path.join(d, "token")
            with open(tok, "w") as f: f.write("t")
            old = (mg.TOKEN_FILE, mg.STATE, mg.KILL, mg.Gh, mg.run_pass)
            passes = []
            mg.TOKEN_FILE, mg.STATE, mg.KILL = tok, os.path.join(d, "s", "state.json"), os.path.join(d, "off")
            mg.run_pass = lambda cfg, gh, dry: passes.append(cfg.get("_gate_login"))
            try:
                for answer in ({"login": ""}, {}, None):
                    class G(FakeGh):
                        def __init__(self, token): super().__init__()
                        def get(self, path, answer=answer): return answer
                    mg.Gh = G
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = mg.main(["--once", "--config", os.path.join(here, "config.example.json")])
                    self.assertEqual(rc, 2, answer)
            finally:
                mg.TOKEN_FILE, mg.STATE, mg.KILL, mg.Gh, mg.run_pass = old
        self.assertEqual(passes, [])

    def test_labels_compared_casefolded_like_the_broker(self):
        self.assertIn("hold", mg.gate_reason(CFG, pr(labels=("automerge", "Hold"))))
        self.assertIsNone(mg.gate_reason(CFG, pr(labels=("AutoMerge",))))
        self.assertIsNotNone(mg.gate_reason({**CFG, "hold_label": "Wait "}, pr(labels=("automerge", "wait"))))

    def test_skipped_required_check_does_not_merge(self):
        gh = self.ok_gh(runs=[run("test", "skipped")])
        gate(gh)
        self.assertEqual(gh.puts, [])

    def test_missing_signer_login_fails_closed_and_says_so(self):
        for cfg in ({**PCFG, "signer_login": ""}, {k: v for k, v in PCFG.items() if k != "signer_login"}):
            gh = self.ok_gh()
            st = gate(gh, cfg)
            self.assertEqual(gh.puts, [])
            self.assertEqual(len(gh.posts), 1)
            path, body = gh.posts[0]
            self.assertEqual(path, "/repos/OWNER/REPO/statuses/" + SHA)
            self.assertEqual((body["state"], body["context"]), ("failure", "merge-gate"))
            self.assertIn("signer_login", body["description"])

    def test_missing_signer_login_posts_status_once_per_sha(self):
        gh, cfg = self.ok_gh(), {**PCFG, "signer_login": ""}
        st = {"done": {}, "attempts": {}, "logged": {}}
        for _ in range(3): mg.process_pr(gh, cfg, "OWNER/REPO", {**pr(), "number": 7}, st, False)
        self.assertEqual(len(gh.posts), 1)

    def test_hold_label_does_not_merge(self):
        gh = self.ok_gh()
        gate(gh, pr_={**pr(labels=("automerge", "hold")), "number": 7})
        self.assertEqual(gh.puts, [])

    def test_draft_and_unlabelled_do_not_merge(self):
        for p in (pr(draft=True), pr(labels=())):
            gh = self.ok_gh()
            gate(gh, pr_={**p, "number": 7})
            self.assertEqual(gh.puts, [])

    def test_policy_failure_posts_status_and_does_not_merge(self):
        gh = self.ok_gh(commits=[commit(author="x@example.com")])
        gate(gh)
        self.assertEqual(gh.puts, [])
        self.assertEqual(gh.posts[0][1]["context"], "merge-gate")

    def test_dry_run_neither_merges_nor_posts(self):
        gh = self.ok_gh()
        gate(gh, dry=True)
        self.assertEqual((gh.puts, gh.posts), ([], []))

    def test_main_refuses_to_run_when_its_own_login_is_the_signer_or_unreadable(self):
        import contextlib, io, tempfile
        with tempfile.TemporaryDirectory() as d:
            tok = os.path.join(d, "token")
            with open(tok, "w") as f: f.write("t")
            old = (mg.TOKEN_FILE, mg.STATE, mg.KILL, mg.Gh, mg.run_pass)
            passes = []
            mg.TOKEN_FILE, mg.STATE, mg.KILL = tok, os.path.join(d, "s", "state.json"), os.path.join(d, "off")
            mg.run_pass = lambda cfg, gh, dry: passes.append(cfg.get("_gate_login"))
            try:
                for login, code in ((SIGNER, 0), (None, 2), ("gate-account", 0)):
                    class G(FakeGh):
                        def __init__(self, token): super().__init__()
                        def get(self, path, login=login):
                            if login is None: raise RuntimeError("GET /user -> 401")
                            return {"login": login}
                    mg.Gh = G
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = mg.main(["--once", "--config", os.path.join(here, "config.example.json")])
                    self.assertEqual(rc, code, login)
            finally:
                mg.TOKEN_FILE, mg.STATE, mg.KILL, mg.Gh, mg.run_pass = old
        # the gate's own login reaches every pass (an unreadable login runs no pass at all) ...
        self.assertEqual(passes, [SIGNER, "gate-account"])
        # ... and a pass whose own login is the signer's refuses every PR (see the shared-signer test)
        self.assertEqual(mg.signer_reason({**PCFG, "_gate_login": SIGNER}), mg.SHARED_SIGNER)

    def test_example_config_names_signer(self):
        cfg = mg.load_config(os.path.join(here, "config.example.json"))
        self.assertTrue(cfg["signer_login"])
        self.assertEqual((cfg["review_lead"], cfg["review_principal"]), ("review/lead", "review/principal"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
