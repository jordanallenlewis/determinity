"""Offline tests for hs-release: GitHub, docker and compose are stubbed on PATH.

Run: python3 -m unittest homeserver/release/test_hs_release.py
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "hs-release")
SERVICES = ["backend", "frontend", "scheduler", "mcp-server"]
API = "https://api.example.test"


def digest(tag, svc):
    return "sha256:" + format(abs(hash((tag, svc))) % (16 ** 64), "064x")


CURL = r"""#!/usr/bin/env bash
# Stub: serves $FAKE_WEB/<sanitised url>. The probe URLs answer from env knobs:
# health fails while the release in release.env is $BAD_TAG; frontend/mcp/setup
# fail when BAD_FRONTEND/BAD_MCP/BAD_SETUP are set. A POST (-d/--data-binary)
# is an alert: logged, fails when ALERT_FAIL is set.
url="${@: -1}"
out=""
post=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  [ "${args[$i]}" = -o ] && out="${args[$((i+1))]}"
  case "${args[$i]}" in -d|--data-binary) post="${args[$((i+1))]}" ;; esac
done
if [ -n "$post" ]; then
  printf 'curl-post %s %s\n' "$url" "$post" >> "$FAKE_LOG"
  [ -n "$ALERT_FAIL" ] && exit 22
  exit 0
fi
rel=$(sed -n 's/^DETERMINITY_RELEASE=//p' "$TRINITY_DIR/release.env" 2>/dev/null)
if [ "$url" = "$HEALTH_URL" ]; then
  [ -n "$BAD_TAG" ] && [ "$rel" = "$BAD_TAG" ] && exit 22
  exit 0
fi
[ "$url" = "$FRONTEND_URL" ] && { [ -n "$BAD_FRONTEND" ] && exit 22; exit 0; }
[ "$url" = "$MCP_URL" ] && { [ -n "$BAD_MCP" ] && exit 22; exit 0; }
if [ "$url" = "$SETUP_URL" ]; then
  [ -n "$BAD_SETUP" ] && { echo '{}'; exit 0; }
  echo '{"setup_completed": true}'; exit 0
fi
f="$FAKE_WEB/$(printf '%s' "$url" | tr '/:?=' '____')"
[ -f "$f" ] || exit 22
if [ -n "$out" ]; then cp "$f" "$out"; else cat "$f"; fi
"""

DOCKER = r"""#!/usr/bin/env bash
# Stub: a container is named after its service; it runs whatever release.env
# (or, before any release, the upstream image) says. Restart count of a
# service grows on every inspect while CRASH_SVC names it (and, if CRASH_TAG is
# set, only while that release is the one in release.env).
echo "docker $*" >> "$FAKE_LOG"
img_for() {
  local v="DETERMINITY_$(echo "$1" | tr 'a-z-' 'A-Z_')_IMAGE"
  local r; r=$(sed -n "s/^$v=//p" "$TRINITY_DIR/release.env" 2>/dev/null)
  [ -n "$r" ] && echo "$r" || echo "ghcr.io/abilityai/trinity-$1:v1.0.5"
}
case "$1" in
  pull) exit 0 ;;
  inspect)
    fmt="$3"; id="$4"
    case "$fmt" in
      '{{.Config.Image}}') img_for "$id" ;;
      '{{.State.Running}} {{.RestartCount}}')
        rel=$(sed -n 's/^DETERMINITY_RELEASE=//p' "$TRINITY_DIR/release.env" 2>/dev/null)
        c="$FAKE_DIR/rc_$id"; [ -f "$c" ] || echo 0 > "$c"
        if [ "$id" = "$CRASH_SVC" ] && { [ -z "$CRASH_TAG" ] || [ "$rel" = "$CRASH_TAG" ]; }; then
          echo $(( $(cat "$c") + 1 )) > "$c"
        fi
        echo "true $(cat "$c")" ;;
      *) echo "id:$(img_for "$id")" ;;
    esac ;;
  image)
    if [ "$5" = trinity-agent-base:latest ]; then echo "id:agent-old"; else echo "id:$5"; fi ;;
  *) exit 0 ;;
esac
"""

COMPOSE = r"""#!/usr/bin/env bash
# Stub compose with a fake PostgreSQL: schema revision rev1, or rev2 while the
# release in release.env is $MIGRATES_TAG, or whatever a pg_restore put back.
echo "compose $*" >> "$FAKE_LOG"
rev() {
  [ -f "$FAKE_DIR/rev_override" ] && { cat "$FAKE_DIR/rev_override"; return; }
  rel=$(sed -n 's/^DETERMINITY_RELEASE=//p' "$TRINITY_DIR/release.env" 2>/dev/null)
  if [ -n "$MIGRATES_TAG" ] && [ "$rel" = "$MIGRATES_TAG" ]; then echo rev2; else echo rev1; fi
}
case "$1" in
  config) [ -z "$NO_PG" ] && printf 'backend\nfrontend\nscheduler\nmcp-server\npostgres\n' || printf 'backend\nfrontend\n' ;;
  ps) [ "$3" = postgres ] && [ -n "$NO_PG" ] && exit 0; echo "$3" ;;
  up) exit "${UP_FAIL:-0}" ;;
  exec)
    script="$*"
    case "$script" in
      *pg_dump*) [ -n "$DUMP_FAIL" ] && exit 1; [ -n "$DUMP_EMPTY" ] && exit 0; echo "PGDMP rev=$(rev)" ;;
      *alembic_version*) [ -n "$REV_FAIL" ] && exit 1; rev ;;
      *"DROP SCHEMA"*) echo dropped >> "$FAKE_LOG" ;;
      *pg_restore*) sed -n 's/^PGDMP rev=//p' > "$FAKE_DIR/rev_override" ;;
    esac ;;
esac
exit 0
"""

LOGGER = r"""#!/usr/bin/env bash
echo "logger $*" >> "$FAKE_LOG"
cat >> "$FAKE_LOG"
"""


class HsReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.web = os.path.join(self.tmp, "web")
        self.trinity = os.path.join(self.tmp, "trinity")
        self.state = os.path.join(self.tmp, "state")
        for d in (self.bin, self.web, self.trinity):
            os.makedirs(d)
        for name, body in (("curl", CURL), ("docker", DOCKER), ("fake-compose", COMPOSE), ("logger", LOGGER)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.log = os.path.join(self.tmp, "calls.log")
        self.env = dict(
            os.environ,
            PATH=self.bin + ":" + os.environ["PATH"],
            GITHUB_API=API,
            DETERMINITY_REPO="owner/determinity",
            TRINITY_DIR=self.trinity,
            HS_RELEASE_STATE=self.state,
            HEALTH_URL="http://health.test/health",
            SETUP_URL="http://setup.test/api/setup/status",
            FRONTEND_URL="http://frontend.test/",
            MCP_URL="http://mcp.test/health",
            HEALTH_TIMEOUT="1",
            HEALTH_SOAK="1",
            HEALTH_POLL="1",
            HEALTH_SOAK_STEP="1",
            FAKE_DIR=self.tmp,
            COMPOSE="fake-compose",
            FAKE_WEB=self.web,
            FAKE_LOG=self.log,
            **{k: "" for k in ("BAD_TAG", "BAD_FRONTEND", "BAD_MCP", "BAD_SETUP", "CRASH_SVC", "CRASH_TAG",
                               "MIGRATES_TAG", "NO_PG", "DUMP_FAIL", "DUMP_EMPTY", "REV_FAIL", "ALERT_FAIL")},
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def serve(self, url, body):
        name = url.replace("/", "_").replace(":", "_").replace("?", "_").replace("=", "_")
        with open(os.path.join(self.web, name), "w") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def publish(self, tag, latest=True, prerelease=False, draft=False, contract=None,
                author="github-actions[bot]", agent=False, refs=None, asset=None):
        asset = asset or f"https://github.com/owner/determinity/releases/download/{tag}/release.json"
        images = {s: {"ref": f"ghcr.io/owner/determinity-{s}@{digest(tag, s)}"} for s in SERVICES}
        if agent:
            images["agent"] = {"ref": f"ghcr.io/owner/determinity-agent@{digest(tag, 'agent')}"}
        images.update(refs or {})
        rel = contract or {
            "schema": "determinity-release/1", "tag": tag, "version": tag[1:],
            "channel": "stable", "images": images,
        }
        meta = {"tag_name": tag, "draft": draft, "prerelease": prerelease,
                "author": {"login": author},
                "assets": [{"name": "release.json", "browser_download_url": asset}]}
        self.serve(asset, rel)
        self.serve(f"{API}/repos/owner/determinity/releases/tags/{tag}", meta)
        if latest:
            self.serve(f"{API}/repos/owner/determinity/releases/latest", meta)

    def run_cmd(self, *args, **env):
        e = dict(self.env, **env)
        return subprocess.run([SCRIPT, *args], env=e, capture_output=True, text=True, timeout=60)

    def release_env(self):
        with open(os.path.join(self.trinity, "release.env")) as f:
            return dict(line.split("=", 1) for line in f.read().split())

    def current(self):
        with open(os.path.join(self.state, "current.env")) as f:
            return dict(line.split("=", 1) for line in f.read().split())

    def test_first_apply_pins_digests_and_records_upstream_as_rollback(self):
        self.publish("v1.0.5-determinity.1")
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        env = self.release_env()
        self.assertEqual(env["DETERMINITY_RELEASE"], "v1.0.5-determinity.1")
        self.assertTrue(env["DETERMINITY_MCP_SERVER_IMAGE"].endswith(digest("v1.0.5-determinity.1", "mcp-server")))
        with open(os.path.join(self.state, "previous.env")) as f:
            prev = f.read()
        self.assertIn("DETERMINITY_BACKEND_IMAGE=ghcr.io/abilityai/trinity-backend:v1.0.5", prev)

    def test_apply_is_a_no_op_when_current(self):
        self.publish("v1.0.5-determinity.1")
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 0)
        self.assertIn("already on v1.0.5-determinity.1", r.stderr)

    def test_unhealthy_release_rolls_back_and_is_not_retried_by_the_timer(self):
        self.publish("v1.0.5-determinity.1")
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish("v1.0.5-determinity.2")
        r = self.run_cmd("apply", BAD_TAG="v1.0.5-determinity.2")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], "v1.0.5-determinity.1")
        self.assertEqual(self.current()["DETERMINITY_RELEASE"], "v1.0.5-determinity.1")
        r = self.run_cmd("apply", BAD_TAG="v1.0.5-determinity.2")
        self.assertEqual(r.returncode, 0)
        self.assertIn("refused/held here", r.stderr)
        # Naming it explicitly tries again (and it now passes).
        r = self.run_cmd("apply", "v1.0.5-determinity.2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current()["DETERMINITY_RELEASE"], "v1.0.5-determinity.2")

    def test_prerelease_and_draft_are_refused(self):
        self.publish("v1.0.5-determinity.1", prerelease=True)
        r = self.run_cmd("apply")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pre-release", r.stderr)
        self.publish("v1.0.5-determinity.1", draft=True)
        self.assertIn("draft", self.run_cmd("apply").stderr)
        self.assertFalse(os.path.exists(os.path.join(self.trinity, "release.env")))

    def test_tag_outside_the_scheme_is_refused(self):
        self.publish("v1.0.6")
        r = self.run_cmd("apply")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a Determinity release tag", r.stderr)

    def test_contract_without_digest_pins_is_refused(self):
        tag = "v1.0.5-determinity.1"
        bad = {"schema": "determinity-release/1", "tag": tag, "channel": "stable",
               "images": {s: {"ref": f"ghcr.io/owner/determinity-{s}:latest"} for s in SERVICES}}
        self.publish(tag, contract=bad)
        r = self.run_cmd("apply")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match the contract", r.stderr)

    def test_rollback_swaps_current_and_previous(self):
        self.publish("v1.0.5-determinity.1")
        self.run_cmd("apply")
        self.publish("v1.0.5-determinity.2")
        self.run_cmd("apply")
        r = self.run_cmd("rollback")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], "v1.0.5-determinity.1")
        r = self.run_cmd("rollback")
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], "v1.0.5-determinity.2")

    def test_check_exit_codes(self):
        self.publish("v1.0.5-determinity.1")
        self.assertEqual(self.run_cmd("check").returncode, 10)
        self.run_cmd("apply")
        self.assertEqual(self.run_cmd("check").returncode, 0)


    # ---- helpers
    def calls(self):
        with open(self.log) as f:
            return f.read()

    def assert_refused_untouched(self, r, needle=None):
        self.assertNotEqual(r.returncode, 0, r.stderr)
        if needle:
            self.assertIn(needle, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.trinity, "release.env")))
        self.assertNotIn("docker pull", self.calls() if os.path.exists(self.log) else "")

    def state_file(self, name):
        with open(os.path.join(self.state, name)) as f:
            return f.read()

    T1, T2, T3 = "v1.0.5-determinity.1", "v1.0.5-determinity.2", "v1.0.5-determinity.3"

    # ---- H1: what may reach release.env
    def test_h1_foreign_registry_ref_is_refused(self):
        self.publish(self.T1, refs={"backend": {"ref": "docker.io/attacker/backdoor@" + digest("x", "y")}})
        self.assert_refused_untouched(self.run_cmd("apply"), "does not match the contract")

    def test_h1_embedded_newline_ref_is_refused(self):
        evil = f"ghcr.io/owner/determinity-backend@{digest('a', 'b')}\nCOMPOSE_PROFILES=otel\nX=" + digest("a", "b")
        self.publish(self.T1, refs={"backend": {"ref": evil}})
        self.assert_refused_untouched(self.run_cmd("apply"), "does not match the contract")

    def test_h1_trailing_newline_ref_is_refused(self):
        self.publish(self.T1, refs={"frontend": {"ref": f"ghcr.io/owner/determinity-frontend@{digest('a', 'b')}\n"}})
        self.assert_refused_untouched(self.run_cmd("apply"), "does not match the contract")

    def test_h1_wrong_owner_and_wrong_service_are_refused(self):
        self.publish(self.T1, refs={"scheduler": {"ref": f"ghcr.io/someoneelse/determinity-scheduler@{digest('a', 'b')}"}})
        self.assert_refused_untouched(self.run_cmd("apply"), "does not match the contract")
        self.publish(self.T1, refs={"scheduler": {"ref": f"ghcr.io/owner/determinity-backend@{digest('a', 'b')}"}})
        self.assert_refused_untouched(self.run_cmd("apply"), "does not match the contract")

    def test_h1_prefix_only_match_is_refused(self):
        self.publish(self.T1, refs={"mcp-server": {"ref": f"evil.test/ghcr.io/owner/determinity-mcp-server@{digest('a', 'b')}"}})
        self.assert_refused_untouched(self.run_cmd("apply"), "does not match the contract")

    def test_h1_non_bot_author_is_refused(self):
        self.publish(self.T1, author="a-maintainer")
        self.assert_refused_untouched(self.run_cmd("apply"), "not github-actions[bot]")

    def test_h1_release_json_hosted_elsewhere_is_refused(self):
        self.publish(self.T1, asset="https://attacker.test/release.json")
        self.assert_refused_untouched(self.run_cmd("apply"), "not hosted under")

    def test_h1_owner_is_lowercased(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", DETERMINITY_REPO="Owner/determinity")
        # Metadata for Owner/determinity is not served by the fake web, so the
        # read fails; the point is the repo name is accepted, not rejected as bad.
        self.assertNotIn("bad DETERMINITY_REPO", r.stderr)
        r = self.run_cmd("apply", DETERMINITY_REPO="bad owner/determinity")
        self.assertIn("bad DETERMINITY_REPO", r.stderr)

    # ---- H2: backup, schema revision, no code-only rollback after a migration
    def test_h2_apply_backs_up_the_database_and_records_the_revision(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        dumps = os.listdir(os.path.join(self.state, "backups"))
        self.assertEqual(len(dumps), 1, dumps)
        self.assertTrue(dumps[0].startswith("none-"), dumps)
        with open(os.path.join(self.state, "backups", dumps[0])) as f:
            self.assertTrue(f.read().startswith("PGDMP"))
        meta = self.state_file("previous.meta")
        self.assertIn("SCHEMA_REV=rev1", meta)
        self.assertIn("BACKUP=" + os.path.join(self.state, "backups", dumps[0]), meta)

    def test_h2_failed_or_empty_backup_blocks_the_switch(self):
        self.publish(self.T1)
        for knob in ("DUMP_FAIL", "DUMP_EMPTY"):
            r = self.run_cmd("apply", **{knob: "1"})
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("backup failed or is empty", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.trinity, "release.env.new")))
            self.assertEqual(os.path.getsize(os.path.join(self.trinity, "release.env")), 0)  # touched, never written
            self.assertFalse(os.path.exists(os.path.join(self.state, "current.env")))
            self.assertNotIn("compose up", self.calls())
            self.assertEqual(os.listdir(os.path.join(self.state, "backups")), [])

    def test_h2_unreadable_revision_blocks_the_switch(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", REV_FAIL="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("compose up", self.calls())

    def test_h2_only_the_newest_backups_are_kept(self):
        d = os.path.join(self.state, "backups")
        os.makedirs(d)
        for n in range(4):
            p = os.path.join(d, f"old{n}-20200101T00000{n}Z.dump")
            with open(p, "w") as f:
                f.write("PGDMP")
            os.utime(p, (1000 + n, 1000 + n))
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply", HS_RELEASE_KEEP_BACKUPS="2").returncode, 0)
        left = sorted(os.listdir(d))
        self.assertEqual(len(left), 2, left)
        self.assertTrue(any(n.startswith("none-") for n in left))
        self.assertIn("old3-20200101T000003Z.dump", left)

    def test_h2_failed_gate_after_a_migration_does_not_roll_back_the_images(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        r = self.run_cmd("apply", BAD_TAG=self.T2, MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("SCHEMA MOVED (rev1 -> rev2)", r.stderr)
        self.assertIn("hs-release rollback --restore-db", r.stderr)
        backup = [n for n in os.listdir(os.path.join(self.state, "backups")) if n.startswith(self.T1)]
        self.assertEqual(len(backup), 1)
        self.assertIn(os.path.join(self.state, "backups", backup[0]), r.stderr)
        # No code-only rollback: the failed release is still what is pinned, and recorded as such.
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T2)
        self.assertEqual(self.current()["DETERMINITY_RELEASE"], self.T2)
        self.assertIn(self.T2, self.state_file("refused"))
        self.assertTrue(os.path.exists(os.path.join(self.state, "blocked")))
        self.assertEqual(self.calls().count("compose up"), 2)  # .1 and .2 only
        # The timer stays out of it until a person acts, and keeps reporting failure.
        self.publish(self.T3)
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 3)
        self.assertIn("NOT applying", r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T2)
        self.assertIn("BLOCKED", self.run_cmd("status").stdout)

    def test_h2_failed_gate_with_unchanged_schema_still_rolls_back(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.publish(self.T2)
        r = self.run_cmd("apply", BAD_TAG=self.T2)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        self.assertFalse(os.path.exists(os.path.join(self.state, "blocked")))

    def test_h2_manual_rollback_is_refused_when_the_schema_moved(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply", MIGRATES_TAG=self.T2).returncode, 0)
        r = self.run_cmd("rollback", MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("rollback --restore-db", r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T2)
        self.assertEqual(self.calls().count("compose up"), 2)

    def test_h2_rollback_restore_db_restores_the_backup_then_the_images(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply", MIGRATES_TAG=self.T2).returncode, 0)
        r = self.run_cmd("rollback", "--restore-db", MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertIn("dropped", calls)
        self.assertIn("compose stop backend scheduler mcp-server", calls)
        self.assertLess(calls.index("compose stop"), calls.index("dropped"))
        self.assertEqual(self.state_file("../rev_override").strip(), "rev1")  # pg_restore got the pre-migration dump
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        self.assertEqual(self.current()["DETERMINITY_RELEASE"], self.T1)
        # The database as it was before the restore is kept too.
        self.assertTrue(any("prerestore" in n for n in os.listdir(os.path.join(self.state, "backups"))))

    def test_h2_restore_after_a_blocked_apply_unblocks_the_timer(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply", BAD_TAG=self.T2, MIGRATES_TAG=self.T2).returncode, 3)
        r = self.run_cmd("rollback", "--restore-db", MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "blocked")))
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        self.assertIn("held", self.run_cmd("apply").stderr)  # .2 stays refused

    def test_h2_restore_without_a_usable_backup_touches_nothing(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.publish(self.T2)
        self.run_cmd("apply", MIGRATES_TAG=self.T2)
        for n in os.listdir(os.path.join(self.state, "backups")):
            os.remove(os.path.join(self.state, "backups", n))
        r = self.run_cmd("rollback", "--restore-db", MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("dropped", self.calls())
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T2)

    def test_h2_sqlite_keeps_the_old_behaviour_and_says_so(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", NO_PG="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not PostgreSQL", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "backups")))
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply", NO_PG="1").returncode, 0)
        r = self.run_cmd("rollback", NO_PG="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("code-only", r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)

    # ---- M2: the gate
    def test_m2_soak_catches_a_crash_looping_service(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.publish(self.T2)
        r = self.run_cmd("apply", CRASH_SVC="scheduler", CRASH_TAG=self.T2, HEALTH_SOAK="2")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("scheduler stopped or restarted during the 2s soak", r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)

    def test_m2_soak_passes_a_steady_release(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", HEALTH_SOAK="2")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_m2_frontend_mcp_and_database_read_are_probed(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        for knob, what in (("BAD_FRONTEND", "frontend"), ("BAD_MCP", "mcp-server"), ("BAD_SETUP", "database read")):
            self.publish(self.T2)
            self.state_clear_held()
            r = self.run_cmd("apply", self.T2, **{knob: "1"})
            self.assertEqual(r.returncode, 2, (knob, r.stderr))
            self.assertIn(what, r.stderr)
            self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)

    def state_clear_held(self):
        p = os.path.join(self.state, "refused")
        if os.path.exists(p):
            os.remove(p)

    # ---- M3: a manual rollback sticks
    def test_m3_manual_rollback_is_not_undone_by_the_timer(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.assertEqual(self.run_cmd("rollback").returncode, 0)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        r = self.run_cmd("apply")                     # the timer
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("held", r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        self.assertIn("refused/held", self.run_cmd("check").stdout)
        self.assertEqual(self.run_cmd("check").returncode, 0)
        # A person names it: the hold is cleared and it goes on.
        r = self.run_cmd("apply", self.T2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T2)
        self.assertNotIn(self.T2, self.state_file("refused"))
        self.assertIn("already on", self.run_cmd("apply").stderr)

    # ---- agent image
    def test_agent_image_is_pulled_and_tagged_and_rolled_back(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.assertNotIn("trinity-agent-base", self.calls())   # a release without it leaves the tag alone
        self.publish(self.T2, agent=True)
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        ref = f"ghcr.io/owner/determinity-agent@{digest(self.T2, 'agent')}"
        calls = self.calls()
        self.assertIn(f"docker pull -q {ref}", calls)
        self.assertIn(f"docker tag {ref} trinity-agent-base:latest", calls)
        self.assertIn("AGENT_ID=id:agent-old", self.state_file("previous.meta"))
        with open(os.path.join(self.trinity, "release.env")) as f:
            self.assertNotIn("DETERMINITY_AGENT", f.read())
        r = self.run_cmd("rollback")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("docker tag id:agent-old trinity-agent-base:latest", self.calls())

    def test_agent_image_is_retagged_when_the_gate_fails(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.publish(self.T2, agent=True)
        r = self.run_cmd("apply", BAD_TAG=self.T2)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("docker tag id:agent-old trinity-agent-base:latest", self.calls())

    def test_agent_ref_is_validated_like_the_others(self):
        self.publish(self.T1, agent=True, refs={"agent": {"ref": f"docker.io/x/determinity-agent@{digest('a', 'b')}"}})
        self.assert_refused_untouched(self.run_cmd("apply"), "does not match the contract")

    # ---- nits
    def test_status_says_unknown_when_the_api_read_fails(self):
        r = self.run_cmd("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("latest:  unknown (API read failed)", r.stdout)

    # ---- M4: alerting
    def run_alert(self, **env):
        alert = os.path.join(HERE, "hs-release-alert")
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "log"), "w") as f:
            f.write("2026-01-01T00:00:00Z hs-release: ROLLBACK ALSO UNHEALTHY\n")
        return subprocess.run([alert], env=dict(self.env, **env), capture_output=True, text=True, timeout=30)

    def test_m4_alert_posts_to_the_configured_url(self):
        r = self.run_alert(HS_RELEASE_ALERT_URL="http://alert.test/topic")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("curl-post http://alert.test/topic", self.calls())
        self.assertIn("ROLLBACK ALSO UNHEALTHY", self.calls())

    def test_m4_alert_without_a_url_writes_the_journal_at_err(self):
        r = self.run_alert()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("logger -p daemon.err -t hs-release-alert", self.calls())
        self.assertNotIn("curl-post", self.calls())

    def test_m4_failed_post_falls_back_to_the_journal(self):
        r = self.run_alert(HS_RELEASE_ALERT_URL="http://alert.test/topic", ALERT_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("logger -p daemon.err", self.calls())

    def test_m4_units_alert_on_every_nonzero_exit(self):
        with open(os.path.join(HERE, "hs-release.service")) as f:
            unit = f.read()
        self.assertIn("OnFailure=hs-release-alert.service", unit)
        self.assertNotIn("SuccessExitStatus", unit)


if __name__ == "__main__":
    unittest.main()
