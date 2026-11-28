"""Offline tests for hs-release: GitHub, docker and compose are stubbed on PATH.

Run: python3 -m unittest homeserver/release/test_hs_release.py
"""
import calendar
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("HS_RELEASE_SCRIPT") or os.path.join(HERE, "hs-release")
ALERT = os.environ.get("HS_RELEASE_ALERT_SCRIPT") or os.path.join(HERE, "hs-release-alert")
SERVICES = ["backend", "frontend", "scheduler", "mcp-server"]
API = "https://api.example.test"


def digest(tag, svc):
    return "sha256:" + format(abs(hash((tag, svc))) % (16 ** 64), "064x")


CURL = r"""#!/usr/bin/env bash
# Stub: serves $FAKE_WEB/<sanitised url>. The probe URLs answer from env knobs:
# health fails while the release in release.env is one of $BAD_TAG (space separated); frontend/mcp/setup
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
  case " $BAD_TAG " in *" $rel "*) [ -n "$BAD_TAG" ] && exit 22 ;; esac
  exit 0
fi
# BAD_FRONTEND / BAD_MCP / BAD_SETUP: "1" = always, or the release tag(s) it applies to.
bad() { [ -n "$1" ] && { [ "$1" = 1 ] || case " $1 " in *" $rel "*) true ;; *) false ;; esac; }; }
[ "$url" = "$FRONTEND_URL" ] && { bad "$BAD_FRONTEND" && exit 22; exit 0; }
[ "$url" = "$MCP_URL" ] && { bad "$BAD_MCP" && exit 22; exit 0; }
if [ "$url" = "$SETUP_URL" ]; then
  bad "$BAD_SETUP" && { echo '{}'; exit 0; }
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
  tag)
    # Was the in-flight note written before the box was changed?
    if [ -f "$HS_RELEASE_STATE/inflight" ]; then echo "inflight-at-tag" >> "$FAKE_LOG"; else echo "no-inflight-at-tag" >> "$FAKE_LOG"; fi
    exit 0 ;;
  pull)
    # PULL_FAIL: a substring of the ref whose pull fails.
    if [ -n "$PULL_FAIL" ]; then case "$*" in *"$PULL_FAIL"*) exit 1 ;; esac; fi
    exit 0 ;;
  inspect)
    fmt="$3"; id="$4"
    case "$fmt" in
      '{{.Config.Image}}') img_for "$id" ;;
      '{{.State.Running}} {{.RestartCount}}')
        [ "$id" = "$RECREATE_SVC" ] && touch "$FAKE_DIR/recreate_now"
        rel=$(sed -n 's/^DETERMINITY_RELEASE=//p' "$TRINITY_DIR/release.env" 2>/dev/null)
        c="$FAKE_DIR/rc_$id"; [ -f "$c" ] || echo 0 > "$c"
        if [ "$id" = "$CRASH_SVC" ] && { [ -z "$CRASH_TAG" ] || [ "$rel" = "$CRASH_TAG" ]; }; then
          echo $(( $(cat "$c") + 1 )) > "$c"
        fi
        echo "true $(cat "$c")" ;;
      *) if [ "$id" = "$STALE_SVC" ]; then echo "id:stale-image"; else echo "id:$(img_for "$id")"; fi ;;
    esac ;;
  image)
    if [ "$4" = '{{json .RepoDigests}}' ]; then
      # A decoy digest from another repo comes first; NO_DIGEST: a local-only image.
      [ -n "$NO_DIGEST" ] && { echo null; exit 0; }
      name="${5#id:}"; repo="${name%%@*}"; [[ "${repo##*/}" == *:* ]] && repo="${repo%:*}"
      echo "[\"mirror.test/other/x@sha256:$(printf %064d 1)\",\"$repo@sha256:$(printf %s "$repo" | sha256sum | cut -c1-64)\"]"
      exit 0
    fi
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
  config)
    # `config --format json`: the backend's effective DATABASE_URL. NO_PG = empty
    # (SQLite), DB_URL = whatever is set there, else the bundled default.
    [ -n "$CONFIG_FAIL" ] && exit 1
    # URL_MODE: empty / null / missing (key absent) / nobackend (no backend service).
    if [ -n "$NO_PG" ]; then u="sqlite:////data/trinity.db"; elif [ -n "$DB_URL" ]; then u="$DB_URL"; else u="postgresql://trinity@postgres:5432/trinity"; fi
    pgdb="${PG_DBNAME:-trinity}"; [ -n "$PG_DBEMPTY" ] && pgdb=""
    case "$URL_MODE" in
      empty) be='{"environment":{"DATABASE_URL":""}}' ;;
      null) be='{"environment":{"DATABASE_URL":null}}' ;;
      missing) be='{"environment":{"OTHER":"x"}}' ;;
      nobackend) be="" ;;
      *) be="{\"environment\":{\"DATABASE_URL\":\"$u\"}}" ;;
    esac
    if [ -n "$be" ]; then be="\"backend\":$be,"; fi
    printf '{"services":{%s"postgres":{"environment":{"POSTGRES_DB":"%s","POSTGRES_USER":"trinity"}}}}\n' "$be" "$pgdb" ;;
  ps)
    [ "$3" = postgres ] && [ -n "$PG_DOWN" ] && exit 0
    # RECREATE_SVC: once a State inspect has run (the soak snapshot), this service answers with a new container id.
    if [ "$3" = "$RECREATE_SVC" ] && [ -f "$FAKE_DIR/recreate_now" ]; then echo "$3-new"; exit 0; fi
    echo "$3" ;;
  up)
    # Was the in-flight note written before the services were started?
    if [ -f "$HS_RELEASE_STATE/inflight" ]; then echo "inflight-at-up" >> "$FAKE_LOG"; else echo "no-inflight-at-up" >> "$FAKE_LOG"; fi
    # What the blocked file says while the services are being started.
    [ -f "$HS_RELEASE_STATE/blocked" ] && echo "blocked-at-up $(sed -n 's/^services_stopped=//p' "$HS_RELEASE_STATE/blocked")" >> "$FAKE_LOG"
    exit "${UP_FAIL:-0}" ;;
  stop) [ -n "$STOP_FAIL" ] && exit 1; [ -n "$STOP_FAIL_EXACT" ] && [ "$*" = "stop $STOP_FAIL_EXACT" ] && exit 1; exit 0 ;;
  exec)
    script="$*"
    case "$script" in
      *pg_dump*) [ -n "$DUMP_FAIL" ] && exit 1; [ -n "$DUMP_EMPTY" ] && exit 0; [ -n "$DUMP_BAD" ] && { echo "NOTADUMP rev=$(rev)"; exit 0; }; echo "PGDMP rev=$(rev)" ;;
      *alembic_version*)
        [ -n "$REV_FAIL" ] && exit 1
        if [ -n "$REV_FAIL_TAG" ] && [ ! -f "$FAKE_DIR/rev_override" ]; then
          rel=$(sed -n 's/^DETERMINITY_RELEASE=//p' "$TRINITY_DIR/release.env" 2>/dev/null)
          [ "$rel" = "$REV_FAIL_TAG" ] && exit 1
        fi
        [ -n "$REV_ROWS" ] && { echo rev1; echo rev2; exit 0; }
        rev ;;
      *"DROP SCHEMA"*) echo dropped >> "$FAKE_LOG" ;;
      *pg_restore*) [ -n "$RESTORE_FAIL" ] && exit 1; [ -n "$RESTORE_WRONG_REV" ] && { echo revX > "$FAKE_DIR/rev_override"; exit 0; }; sed -n 's/^PGDMP rev=//p' > "$FAKE_DIR/rev_override" ;;
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
            HS_RELEASE_DEFAULTS=os.path.join(self.tmp, "no-such-defaults"),   # never the host's /etc/default/hs-release
            HS_RELEASE_ALERT_URL="http://alert.test/default",   # tests of the unset case pass ""
            **{k: "" for k in ("BAD_TAG", "BAD_FRONTEND", "BAD_MCP", "BAD_SETUP", "CRASH_SVC", "CRASH_TAG",
                               "MIGRATES_TAG", "NO_PG", "DUMP_FAIL", "DUMP_EMPTY", "REV_FAIL", "ALERT_FAIL",
                               "PULL_FAIL", "NO_DIGEST", "RESTORE_FAIL", "DB_URL", "CONFIG_FAIL",
                               "REV_FAIL_TAG", "REV_ROWS", "HS_RELEASE_EXTERNAL_DB", "URL_MODE", "PG_DBNAME",
                               "PG_DOWN", "STOP_FAIL", "STOP_FAIL_EXACT", "UP_FAIL", "PG_DBEMPTY", "RECREATE_SVC", "STALE_SVC", "DUMP_BAD", "RESTORE_WRONG_REV",
                               )},
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def serve(self, url, body):
        name = url.replace("/", "_").replace(":", "_").replace("?", "_").replace("=", "_")
        with open(os.path.join(self.web, name), "w") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def publish(self, tag, latest=True, prerelease=False, draft=False, contract=None,
                author="github-actions[bot]", uploader="github-actions[bot]", agent=False, refs=None, asset=None,
                published_at="2020-01-01T00:00:00Z", homeserver_files=None):
        asset = asset or f"https://github.com/owner/determinity/releases/download/{tag}/release.json"
        images = {s: {"ref": f"ghcr.io/owner/determinity-{s}@{digest(tag, s)}"} for s in SERVICES}
        if agent:
            images["agent"] = {"ref": f"ghcr.io/owner/determinity-agent@{digest(tag, 'agent')}"}
        images.update(refs or {})
        rel = contract or {
            "schema": "determinity-release/1", "tag": tag, "version": tag[1:],
            "channel": "stable", "images": images,
        }
        if homeserver_files is not None and contract is None:
            rel["homeserver_files"] = homeserver_files
        meta = {"tag_name": tag, "draft": draft, "prerelease": prerelease, "published_at": published_at,
                "author": {"login": author},
                "assets": [{"name": "release.json", "browser_download_url": asset,
                            "uploader": {"login": uploader}}]}
        self.serve(asset, rel)
        self.serve(f"{API}/repos/owner/determinity/releases/tags/{tag}", meta)
        if latest:
            self.serve(f"{API}/repos/owner/determinity/releases/latest", meta)

    def run_cmd(self, *args, **env):
        e = {k: v for k, v in dict(self.env, **env).items() if v is not None}   # None removes a variable
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
        # By digest, from the image's own repository (not the decoy from another registry).
        want = "ghcr.io/abilityai/trinity-backend@sha256:" + hashlib.sha256(b"ghcr.io/abilityai/trinity-backend").hexdigest()
        self.assertIn("DETERMINITY_BACKEND_IMAGE=" + want, prev)
        self.assertNotIn("mirror.test", prev)

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

    # ---- review round: database mode, restore, rollback point

    def blocked_by_migration(self, **env):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        return self.run_cmd("apply", BAD_TAG=self.T2, MIGRATES_TAG=self.T2, **env)

    def test_m6_restore_db_works_when_the_revision_is_unreadable_after_a_failed_gate(self):
        r = self.blocked_by_migration(REV_FAIL_TAG=self.T2)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("schema_now=unreadable", self.state_file("blocked"))
        self.assertEqual(self.run_cmd("apply").returncode, 3)
        r = self.run_cmd("rollback", "--restore-db", MIGRATES_TAG=self.T2, REV_FAIL_TAG=self.T2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("restoring anyway", r.stderr)
        self.assertEqual(self.state_file("../rev_override").strip(), "rev1")
        self.assertFalse(os.path.exists(os.path.join(self.state, "blocked")))
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        self.assertNotEqual(self.run_cmd("apply").returncode, 3)  # the timer is no longer blocked

    def test_m7_sqlite_url_takes_the_sqlite_path_even_with_a_postgres_service(self):
        self.publish(self.T1)
        for url in ("sqlite:////data/trinity.db", "sqlite:///x.db"):
            shutil.rmtree(self.state, ignore_errors=True)
            r = self.run_cmd("apply", DB_URL=url)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("not PostgreSQL", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.state, "backups")))
            self.assertNotIn("pg_dump", self.calls())
            self.run_cmd("rollback", DB_URL=url)

    def test_m7_bundled_postgres_url_takes_the_pg_path(self):
        self.publish(self.T1)
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("database backed up", r.stderr)
        self.assertNotIn("s3cret", r.stderr)

    def test_m7_external_postgres_is_refused_without_the_opt_in(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", DB_URL="postgresql://u@db.example.test:5432/trinity")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("external database", r.stderr)
        self.assertIn("HS_RELEASE_EXTERNAL_DB", r.stderr)
        self.assertNotIn("pw@", r.stderr)
        self.assertNotIn("compose up", self.calls())

    def test_m7_external_postgres_with_opt_in_blocks_after_a_failed_gate(self):
        env = dict(DB_URL="postgresql://u@db.example.test:5432/trinity",
                   HS_RELEASE_EXTERNAL_DB="own-backups-no-rollback-across-migration")
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply", **env).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "backups")))
        self.publish(self.T2)
        r = self.run_cmd("apply", BAD_TAG=self.T2, **env)   # no migration at all: still blocked
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T2)
        self.assertTrue(os.path.exists(os.path.join(self.state, "blocked")))
        self.assertEqual(self.run_cmd("apply", **env).returncode, 3)
        r = self.run_cmd("rollback", **env)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T2)
        r = self.run_cmd("rollback", "--restore-db", **env)
        self.assertEqual(r.returncode, 1)
        r = self.run_cmd("rollback", "--db-restored", **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        self.assertFalse(os.path.exists(os.path.join(self.state, "blocked")))

    def test_m7_unreadable_compose_config_is_not_guessed(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", CONFIG_FAIL="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("compose up", self.calls())

    def test_l6_apply_while_blocked_keeps_the_rollback_point_and_dump(self):
        self.assertEqual(self.blocked_by_migration().returncode, 3)
        prev, meta = self.state_file("previous.env"), self.state_file("previous.meta")
        blocked = self.state_file("blocked")
        dump = [l for l in blocked.split("\n") if l.startswith("backup=")][0][7:]
        self.publish(self.T3)
        r = self.run_cmd("apply", self.T3, BAD_TAG=self.T3, MIGRATES_TAG=self.T3)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.state_file("previous.env"), prev)
        self.assertEqual(self.state_file("previous.meta"), meta)
        self.assertIn("backup=" + dump, self.state_file("blocked"))
        self.assertTrue(os.path.exists(dump))
        # The attempt that does not migrate rolls back to what ran, not to the old point.
        self.publish("v1.0.5-determinity.4")
        r = self.run_cmd("apply", "v1.0.5-determinity.4", BAD_TAG="v1.0.5-determinity.4", MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.state_file("previous.env"), prev)
        self.assertTrue(os.path.exists(os.path.join(self.state, "blocked")))
        r = self.run_cmd("rollback", "--restore-db", MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state_file("../rev_override").strip(), "rev1")
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)

    def test_l6_pruning_never_removes_the_dump_blocked_names(self):
        self.assertEqual(self.blocked_by_migration().returncode, 3)
        dump = [l for l in self.state_file("blocked").split("\n") if l.startswith("backup=")][0][7:]
        os.utime(dump, (1, 1))
        for n, t in (("v1.0.5-determinity.3", 3), ("v1.0.5-determinity.4", 4)):
            self.publish(n)
            self.run_cmd("apply", n, BAD_TAG=n, MIGRATES_TAG=self.T2, HS_RELEASE_KEEP_BACKUPS="1")
        self.assertTrue(os.path.exists(dump))

    def test_l7_services_are_stopped_before_the_post_gate_revision_is_read(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.publish(self.T2)
        self.run_cmd("apply", BAD_TAG=self.T2, MIGRATES_TAG=self.T2)
        calls = self.calls()
        i = calls.rindex("compose stop backend scheduler")
        self.assertGreater(calls.index("alembic_version", i), i)
        # and the revision read that follows is not the one taken before the switch
        self.assertGreater(calls.count("alembic_version"), 1)

    def test_l7_a_failed_stop_counts_as_unreadable_and_blocks(self):
        self.publish(self.T1)
        self.run_cmd("apply")
        self.publish(self.T2)
        shim = os.path.join(self.bin, "fake-compose2")
        with open(shim, "w") as f:
            f.write('#!/usr/bin/env bash\n[ "$1" = stop ] && { echo "compose $*" >> "$FAKE_LOG"; exit 1; }\nexec fake-compose "$@"\n')
        os.chmod(shim, 0o755)
        r = self.run_cmd("apply", BAD_TAG=self.T2, COMPOSE="fake-compose2")
        self.assertEqual(r.returncode, 4, r.stderr)   # a failed stop is broken, as on the re-gate path
        self.assertIn("schema_now=unreadable", self.state_file("blocked"))

    def test_l8_printed_manual_steps_use_the_install_wrapper_and_stop_on_error(self):
        r = self.blocked_by_migration()
        self.assertEqual(r.returncode, 3)
        self.assertIn("hs-trinity stop backend scheduler mcp-server", r.stderr)
        self.assertIn("hs-trinity exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1", r.stderr)
        self.assertNotRegex(r.stderr, r"(?m)^\s+trinity ")

    def test_nit_more_than_one_alembic_row_is_unreadable(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", REV_ROWS="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cannot read the Alembic revision", r.stderr)
        self.assertNotIn("compose up", self.calls())

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
            r = self.run_cmd("apply", self.T2, **{knob: self.T2})   # only the new release is bad
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

    # ---- round 3 helpers
    def pin(self):
        return self.release_env()["DETERMINITY_RELEASE"]

    def assert_current_is_pinned(self):
        """current.env (last good state) must never disagree with release.env."""
        self.assertEqual(self.current()["DETERMINITY_RELEASE"], self.pin())

    def count(self, needle):
        return self.calls().count(needle) if os.path.exists(self.log) else 0

    def applied(self, *tags):
        for t in tags:
            self.publish(t)
            r = self.run_cmd("apply")
            self.assertEqual(r.returncode, 0, r.stderr)

    @staticmethod
    def epoch(iso):
        return str(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))

    # ---- M2: quiet after failure (s2, s6)
    def test_s2_double_failure_stays_loud_until_a_person_clears_it(self):
        self.applied(self.T1)
        self.publish(self.T2)
        bad = f"{self.T1} {self.T2}"                       # the rollback target is unhealthy too
        r = self.run_cmd("apply", BAD_TAG=bad)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("BROKEN (double-failure)", r.stderr)
        marker = self.state_file("broken")
        self.assertIn("kind=double-failure", marker)
        self.assertRegex(marker, r"since=\d{4}-\d\d-\d\dT")
        self.assertIn("reason=", marker)
        self.assert_current_is_pinned()
        ups = self.count("compose up")
        # The next timer runs: still non-zero, still doing nothing, even with a healthy platform.
        self.publish(self.T3)
        for _ in range(2):
            r = self.run_cmd("apply")
            self.assertEqual(r.returncode, 4, r.stderr)
            self.assertIn("NOT applying", r.stderr)
        # A person naming a tag does not get around it either.
        self.assertEqual(self.run_cmd("apply", self.T3).returncode, 4)
        self.assertEqual(self.count("compose up"), ups)
        self.assertEqual(self.pin(), self.T1)
        self.assertEqual(self.run_cmd("check").returncode, 4)
        out = self.run_cmd("status").stdout
        self.assertTrue(out.startswith("BROKEN since"), out)
        self.assertIn("hs-release clear-broken", out)
        self.assertNotIn("running:", out)                   # it does not claim a verified state
        # clear-broken checks the pinned stack first.
        r = self.run_cmd("clear-broken", BAD_TAG=bad)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.state, "broken")))
        r = self.run_cmd("clear-broken")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "broken")))
        self.assertTrue(self.run_cmd("status").stdout.startswith("running:"))
        r = self.run_cmd("apply")                            # .3 is not held: it goes
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pin(), self.T3)
        self.assertEqual(self.run_cmd("apply", self.T2).returncode, 0)  # the held failure is still named by hand only

    def test_s2_clear_broken_force_and_nothing_to_clear(self):
        self.assertIn("nothing to clear", self.run_cmd("clear-broken").stdout)
        self.applied(self.T1)
        self.publish(self.T2)
        bad = f"{self.T1} {self.T2}"
        self.assertEqual(self.run_cmd("apply", BAD_TAG=bad).returncode, 4)
        self.assertEqual(self.run_cmd("clear-broken", "--force", BAD_TAG=bad).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "broken")))

    def test_s6_failed_manual_rollback_is_recorded_and_status_is_truthful(self):
        self.applied(self.T1, self.T2)
        r = self.run_cmd("rollback", BAD_TAG=self.T1)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("BROKEN (rollback-failed)", r.stderr)
        self.assertEqual(self.pin(), self.T1)
        self.assert_current_is_pinned()                      # was "R2" while release.env pinned R1
        out = self.run_cmd("status").stdout
        self.assertTrue(out.startswith("BROKEN since"), out)
        self.assertIn("pinned in release.env: " + self.T1, out)
        self.assertNotIn("running: " + self.T2, out)
        r = self.run_cmd("apply", self.T2)                  # "already on R2" would be the lie
        self.assertEqual(r.returncode, 4)
        self.assertNotIn("already on", r.stderr)
        self.assertEqual(self.run_cmd("apply").returncode, 4)

    def test_m2_failed_restore_is_broken(self):
        self.applied(self.T1)
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply", MIGRATES_TAG=self.T2).returncode, 0)
        r = self.run_cmd("rollback", "--restore-db", MIGRATES_TAG=self.T2, RESTORE_FAIL="1")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("kind=restore-failed", self.state_file("broken"))
        self.assertIn("pg_restore failed", self.state_file("broken"))
        self.assertEqual(self.run_cmd("apply").returncode, 4)
        self.assertTrue(self.run_cmd("status").stdout.startswith("BROKEN since"))

    def test_m2_a_run_killed_mid_switch_is_found_and_stops_the_timer(self):
        self.applied(self.T1)
        with open(os.path.join(self.state, "inflight"), "w") as f:
            f.write(f"op=apply\ntag={self.T2}\nstarted=2026-01-01T00:00:00Z\n")
        self.assertIn("INTERRUPTED", self.run_cmd("status").stdout)
        self.publish(self.T2)
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("kind=interrupted", self.state_file("broken"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))

    def test_m2_blocked_and_broken_are_consistent(self):
        # blocked (exit 3): the timer waits; a person can fix forward or restore.
        self.applied(self.T1)
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply", BAD_TAG=self.T2, MIGRATES_TAG=self.T2).returncode, 3)
        self.assert_current_is_pinned()
        self.publish(self.T3)
        self.assertEqual(self.run_cmd("apply").returncode, 3)
        self.assertTrue(self.run_cmd("status").stdout.startswith("BLOCKED"))
        r = self.run_cmd("apply", self.T3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "blocked")))
        self.assert_current_is_pinned()

    def test_m2_state_always_matches_the_pin(self):
        self.applied(self.T1, self.T2)
        self.assert_current_is_pinned()
        self.publish(self.T3)
        self.assertEqual(self.run_cmd("apply", BAD_TAG=self.T3).returncode, 2)
        self.assert_current_is_pinned()
        self.assertEqual(self.run_cmd("rollback").returncode, 0)
        self.assert_current_is_pinned()

    # ---- L2 (s9): after an automatic rollback `previous` is still a rollback target
    def test_s9_automatic_rollback_keeps_the_previous_rollback_point(self):
        self.applied(self.T1, self.T2)
        self.publish(self.T3)
        self.assertEqual(self.run_cmd("apply", BAD_TAG=self.T3).returncode, 2)
        self.assertEqual(self.pin(), self.T2)
        self.assertIn("rollback: " + self.T1, self.run_cmd("status").stdout)   # not "rollback: T2"
        r = self.run_cmd("rollback")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pin(), self.T1)

    def test_s9_first_switch_failure_leaves_no_bogus_rollback_point(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply", BAD_TAG=self.T1).returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "previous.env")))
        self.assertIn("no previous release", self.run_cmd("rollback").stderr)

    # ---- L3: the first rollback point is a digest
    def test_l3_no_digest_for_the_running_image_refuses_the_first_switch(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", NO_DIGEST="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no rollback point by digest", r.stderr)
        self.assertIn("no repo digest", r.stderr)
        self.assertNotIn("compose up", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.state, "current.env")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "previous.env")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "backups")))   # refused before the dump

    # ---- M5: minimum release age on the timer path
    def test_m5_young_release_waits_on_the_timer_and_a_person_can_name_it(self):
        self.publish(self.T1, published_at="2026-11-27T00:00:00Z")
        r = self.run_cmd("apply", HS_RELEASE_NOW=self.epoch("2026-11-27T10:00:00Z"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("waiting until 2026-11-28T00:00:00Z", r.stderr)
        self.assertNotIn("docker pull", self.calls() if os.path.exists(self.log) else "")
        self.assertNotIn("compose up", self.calls() if os.path.exists(self.log) else "")
        self.assertFalse(os.path.exists(os.path.join(self.state, "current.env")))
        # One second short of 24 h: still waiting. Exactly 24 h: goes.
        r = self.run_cmd("apply", HS_RELEASE_NOW=self.epoch("2026-11-27T23:59:59Z"))
        self.assertIn("waiting until", r.stderr)
        r = self.run_cmd("apply", HS_RELEASE_NOW=self.epoch("2026-11-28T00:00:00Z"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pin(), self.T1)

    def test_m5_explicit_tag_bypasses_the_age(self):
        self.publish(self.T1, published_at="2026-11-27T00:00:00Z")
        r = self.run_cmd("apply", self.T1, HS_RELEASE_NOW=self.epoch("2026-11-27T00:30:00Z"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pin(), self.T1)

    def test_m5_age_is_configurable_and_zero_turns_it_off(self):
        self.publish(self.T1, published_at="2026-11-27T00:00:00Z")
        now = self.epoch("2026-11-27T03:00:00Z")
        self.assertIn("waiting until 2026-11-27T06:00:00Z",
                      self.run_cmd("apply", HS_RELEASE_NOW=now, HS_RELEASE_MIN_AGE_HOURS="6").stderr)
        self.assertEqual(self.run_cmd("apply", HS_RELEASE_NOW=now, HS_RELEASE_MIN_AGE_HOURS="0").returncode, 0)
        self.assertEqual(self.pin(), self.T1)
        self.assertEqual(self.run_cmd("apply", HS_RELEASE_MIN_AGE_HOURS="soon").returncode, 1)

    def test_m5_unreadable_published_at_does_not_move_the_box(self):
        self.publish(self.T1, published_at="")
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cannot read published_at", r.stderr)
        self.assertNotIn("compose up", self.calls() if os.path.exists(self.log) else "")

    # ---- L1: the timer only moves forward
    def test_l1_timer_does_not_downgrade_to_a_withdrawn_latest(self):
        self.applied(self.T1, self.T2)
        self.publish(self.T1)                                # latest points back at .1
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not downgrading", r.stderr)
        self.assertEqual(self.pin(), self.T2)
        # A person may go to it by name.
        self.assertEqual(self.run_cmd("apply", self.T1).returncode, 0)
        self.assertEqual(self.pin(), self.T1)

    def test_l1_versions_compare_by_field_not_as_text(self):
        a, b = "v1.0.10-determinity.1", "v1.0.9-determinity.5"
        self.applied(a)
        self.publish(b)
        self.assertIn("not downgrading", self.run_cmd("apply").stderr)
        self.assertEqual(self.pin(), a)
        self.applied("v1.0.10-determinity.2", "v1.1.0-determinity.1", "v2.0.0-determinity.1")
        self.assertEqual(self.pin(), "v2.0.0-determinity.1")

    def test_l1_an_unparseable_running_tag_is_not_moved_from(self):
        os.makedirs(self.trinity, exist_ok=True)
        with open(os.path.join(self.trinity, "release.env"), "w") as f:
            f.write("DETERMINITY_RELEASE=local-build\n")
        self.publish(self.T1)
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cannot compare", r.stderr)
        self.assertEqual(self.pin(), "local-build")
        self.assertNotIn("compose up", self.calls() if os.path.exists(self.log) else "")

    # ---- M3a: homeserver/ files do not follow a release
    def sha(self, text):
        return hashlib.sha256(text.encode()).hexdigest()

    def install_files(self, **contents):
        """Installed copies, mapped through HS_RELEASE_FILES; returns the env knob."""
        pairs = []
        for name, text in contents.items():
            path = os.path.join(self.tmp, "installed-" + name)
            with open(path, "w") as f:
                f.write(text)
            pairs.append(f"homeserver/release/{name}={path}")
        return " ".join(pairs)

    def test_m3a_drift_warns_in_apply_and_status_without_blocking(self):
        files = self.install_files(**{"hs-release": "old script\n"})
        self.publish(self.T1, homeserver_files={
            "homeserver/release/hs-release": self.sha("new script\n"),
            "homeserver/release/hs-release-alert": self.sha("x"),   # no installed location mapped: not compared
        })
        r = self.run_cmd("apply", HS_RELEASE_FILES=files)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"homeserver files differ from release {self.T1}: homeserver/release/hs-release; "
                      f"re-run install from tag {self.T1}", r.stderr)
        self.assertNotIn("hs-release-alert;", r.stderr)
        self.assertEqual(self.pin(), self.T1)                # a warning, not a block
        # It repeats on the no-op runs, and status shows it.
        self.assertIn("homeserver files differ", self.run_cmd("apply", HS_RELEASE_FILES=files).stderr)
        out = self.run_cmd("status", HS_RELEASE_FILES=files).stdout
        self.assertIn("WARNING: homeserver files differ from release " + self.T1, out)
        self.assertEqual(self.run_cmd("status", HS_RELEASE_FILES=files).returncode, 0)
        self.assertEqual(self.run_cmd("check", HS_RELEASE_FILES=files).returncode, 0)

    def test_m3a_matching_files_and_a_missing_field_are_quiet(self):
        files = self.install_files(**{"hs-release": "same\n"})
        self.publish(self.T1, homeserver_files={"homeserver/release/hs-release": self.sha("same\n")})
        r = self.run_cmd("apply", HS_RELEASE_FILES=files)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("homeserver files differ", r.stderr)
        self.publish(self.T2)                                  # no homeserver_files at all
        r = self.run_cmd("apply", HS_RELEASE_FILES=files)
        self.assertNotIn("homeserver files differ", r.stderr)
        self.assertNotIn("WARNING", self.run_cmd("status", HS_RELEASE_FILES=files).stdout)

    def test_m3a_default_mapping_covers_the_install_paths_and_a_missing_file_differs(self):
        with open(os.path.join(self.trinity, "docker-compose.box.yml"), "w") as f:
            f.write("services: {}\n")
        self.publish(self.T1, homeserver_files={
            "homeserver/compose/docker-compose.box.yml": self.sha("services: {}\n"),
            "homeserver/release/hs-release.timer": self.sha("t"),
        })
        r = self.run_cmd("apply")      # the real /etc/systemd/system/hs-release.timer is not there
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("differ from release", r.stderr)
        self.assertIn("homeserver/release/hs-release.timer", r.stderr)
        self.assertNotIn("docker-compose.box.yml;", r.stderr)   # matched, so not listed
        with open(SCRIPT) as f:
            text = f.read()
        for repo_path, installed in (("homeserver/release/hs-release", "/usr/local/sbin/hs-release"),
                                     ("homeserver/release/hs-release-alert", "/usr/local/sbin/hs-release-alert"),
                                     ("homeserver/release/hs-release.service", "/etc/systemd/system/hs-release.service"),
                                     ("homeserver/release/hs-release-alert.service", "/etc/systemd/system/hs-release-alert.service"),
                                     ("homeserver/release/hs-release.timer", "/etc/systemd/system/hs-release.timer"),
                                     ("homeserver/compose/docker-compose.box.yml", "$TRINITY_DIR/docker-compose.box.yml")):
            self.assertIn(f"{repo_path}={installed}", text)

    def test_m3a_malformed_entries_are_ignored(self):
        files = self.install_files(**{"hs-release": "x"})
        self.publish(self.T1, homeserver_files={"homeserver/release/hs-release": "not-a-sha"})
        r = self.run_cmd("apply", HS_RELEASE_FILES=files)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("homeserver files differ", r.stderr)

    # ---- M3b: the agent base image follows the release by default
    def test_m3b_agent_image_opt_out(self):
        self.applied(self.T1)
        self.publish(self.T2, agent=True)
        r = self.run_cmd("apply", HS_RELEASE_AGENT_IMAGE="no")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("determinity-agent", self.calls())
        self.assertNotIn("trinity-agent-base", self.calls())
        self.assertNotIn("AGENT_ID=id", self.state_file("previous.meta"))

    def test_m3b_agent_image_follows_by_default_and_is_in_the_rollback_point(self):
        self.applied(self.T1)
        self.publish(self.T2, agent=True)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.assertIn("AGENT_ID=id:agent-old", self.state_file("previous.meta"))
        self.assertNotIn("determinity-agent", self.state_file("previous.env"))  # tags, not env pins
        # Agent pull failure: nothing switched.
        self.publish(self.T3, agent=True)
        ups = self.count("compose up")
        r = self.run_cmd("apply", PULL_FAIL="determinity-agent")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("pull failed for the agent image", r.stderr)
        self.assertEqual(self.count("compose up"), ups)

    # ---- L6: tests aimed at mutants that used to survive
    def sourced(self, script, **extra):
        env = dict(self.env, HS_RELEASE_SOURCED="1", **extra)
        return subprocess.run(["bash", "-c", f'. "{SCRIPT}"; {script}'], env=env, capture_output=True, text=True, timeout=30)

    def test_l6a_valid_ref_accepts_only_the_whole_pinned_string(self):
        d = "a" * 64
        good = f"ghcr.io/owner/determinity-backend@sha256:{d}"
        self.assertEqual(self.sourced(f'valid_ref backend "{good}"').returncode, 0)
        bad = [
            f"evil.test/{good}",                                   # prefix
            f"{good}x", f"{good}0",                               # suffix
            f"docker.io/x/determinity-backend@sha256:{d}",        # other registry
            f"ghcr.io/other/determinity-backend@sha256:{d}",      # other owner
            f"ghcr.io/owner/determinity-frontend@sha256:{d}",     # other service
            "ghcr.io/owner/determinity-backend:latest",           # tag, not digest
            f"ghcr.io/owner/determinity-backend@sha256:{'a' * 63}",
            f"ghcr.io/owner/determinity-backend@sha256:{'G' * 64}",
        ]
        for ref in bad:
            self.assertEqual(self.sourced(f'valid_ref backend "{ref}"').returncode, 1, ref)
        for ref in (good + "\n", good + "\nX=1", "\n" + good, good + "\r"):
            self.assertEqual(self.sourced('valid_ref backend "$REF"', REF=ref).returncode, 1, repr(ref))

    def test_l6a_release_env_is_not_written_from_a_bad_ref_even_if_the_contract_check_passed(self):
        d = "b" * 64
        rel = {"tag": self.T1, "images": {s: {"ref": f"ghcr.io/owner/determinity-{s}@sha256:{d}"} for s in SERVICES}}
        r = self.sourced(f"env_from_release <<'J'\n{json.dumps(rel)}\nJ")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DETERMINITY_BACKEND_IMAGE=ghcr.io/owner/determinity-backend@sha256:" + d, r.stdout)
        rel["images"]["mcp-server"]["ref"] = f"docker.io/x/determinity-mcp-server@sha256:{d}"
        r = self.sourced(f"env_from_release <<'J'\n{json.dumps(rel)}\nJ")
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad mcp-server image ref", r.stderr)
        rel["images"]["mcp-server"]["ref"] = f"ghcr.io/owner/determinity-mcp-server@sha256:{d}\nCOMPOSE_PROFILES=otel"
        self.assertEqual(self.sourced(f"env_from_release <<'J'\n{json.dumps(rel)}\nJ").returncode, 1)
        rel["images"]["agent"] = {"ref": "ghcr.io/owner/determinity-agent:latest"}
        r = self.sourced(f"agent_ref_from_release <<'J'\n{json.dumps(rel)}\nJ")
        self.assertEqual(r.returncode, 1)

    def test_l6b_pull_failure_leaves_the_running_release_untouched(self):
        self.applied(self.T1)
        with open(os.path.join(self.trinity, "release.env")) as f:
            before_env = f.read()
        before_state = os.listdir(self.state)
        ups, dumps = self.count("compose up"), len(os.listdir(os.path.join(self.state, "backups")))
        self.publish(self.T2)
        for svc in SERVICES:
            r = self.run_cmd("apply", PULL_FAIL=f"determinity-{svc}@")
            self.assertEqual(r.returncode, 1, (svc, r.stderr))
            self.assertIn(f"pull failed for {svc}", r.stderr)
            with open(os.path.join(self.trinity, "release.env")) as f:
                self.assertEqual(f.read(), before_env)
            self.assertEqual(self.count("compose up"), ups)
            self.assertEqual(self.count("compose stop"), 0)
            self.assertEqual(len(os.listdir(os.path.join(self.state, "backups"))), dumps)
        self.assertEqual(self.current()["DETERMINITY_RELEASE"], self.T1)
        self.assertNotIn("broken", set(os.listdir(self.state)) - set(before_state))

    def test_l6b_every_pull_happens_before_the_backup_and_the_switch(self):
        self.applied(self.T1)
        self.publish(self.T2, agent=True)
        mark = len(self.calls())
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        calls = self.calls()[mark:]
        last_pull = calls.rindex("docker pull")
        self.assertEqual(calls.count("docker pull"), 5)
        self.assertLess(last_pull, calls.index("pg_dump"))
        self.assertLess(last_pull, calls.index("compose up"))
        self.assertLess(last_pull, calls.index("docker tag"))

    def test_l6c_a_second_apply_while_one_runs_is_refused(self):
        self.publish(self.T1)
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for args in (("apply",), ("apply", self.T1), ("rollback",), ("clear-broken",)):
                r = self.run_cmd(*args)
                self.assertEqual(r.returncode, 1, (args, r.stderr))
                self.assertIn("another hs-release is running", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.trinity, "release.env")))
            self.assertNotIn("docker", self.calls() if os.path.exists(self.log) else "")
        self.assertEqual(self.run_cmd("apply").returncode, 0)   # released: works

    # ---- L4: unit files
    def test_l4_timer_does_not_catch_up_at_boot_and_service_has_a_timeout(self):
        with open(os.path.join(HERE, "hs-release.timer")) as f:
            timer = f.read()
        self.assertIn("Persistent=false", timer)
        self.assertNotIn("Persistent=true", timer)
        with open(os.path.join(HERE, "hs-release.service")) as f:
            unit = f.read()
        self.assertIn("TimeoutStartSec=45min", unit)

    # ---- nits
    def test_status_says_unknown_when_the_api_read_fails(self):
        r = self.run_cmd("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("latest:  unknown (API read failed)", r.stdout)

    # ---- M4: alerting
    def run_alert(self, **env):
        alert = ALERT
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "log"), "w") as f:
            f.write("2026-01-01T00:00:00Z hs-release: ROLLBACK ALSO UNHEALTHY\n")
        e = {k: v for k, v in dict(self.env, **env).items() if v is not None}
        return subprocess.run([alert], env=e, capture_output=True, text=True, timeout=30)

    def test_m4_alert_posts_to_the_configured_url(self):
        r = self.run_alert(HS_RELEASE_ALERT_URL="http://alert.test/topic")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("curl-post http://alert.test/topic", self.calls())
        self.assertIn("ROLLBACK ALSO UNHEALTHY", self.calls())

    def test_m4_alert_without_a_url_writes_the_journal_at_err(self):
        r = self.run_alert(HS_RELEASE_ALERT_URL="")
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


    # ---- round 4
    def r4_blocked(self, **env):
        r = self.blocked_by_migration(**env)
        self.assertEqual(r.returncode, 3, r.stderr)
        return r

    def pinned_size(self):  # nothing was pinned: release.env is absent or empty
        p = os.path.join(self.trinity, "release.env")
        return os.path.getsize(p) if os.path.exists(p) else 0

    def r4_dump(self):
        return [l for l in self.state_file("blocked").split("\n") if l.startswith("backup=")][0][7:]

    def test_r4_blocked_says_the_services_are_stopped(self):
        r = self.r4_blocked()
        self.assertIn("services_stopped=backend scheduler", self.state_file("blocked"))
        self.assertIn("services_stopped=backend scheduler", r.stderr)
        self.assertIn("NOT serving", r.stderr)
        self.assertNotIn("compose up -d --no-build backend", self.calls().split("compose stop backend scheduler")[-1])
        st = self.run_cmd("status")
        self.assertIn("BLOCKED", st.stdout)
        self.assertIn("services stopped: backend scheduler", st.stdout)
        alert = self.run_alert(HS_RELEASE_ALERT_URL="http://alert.test/topic")
        self.assertIn("stopped on purpose", self.calls())

    def test_r4_apply_the_pinned_tag_while_blocked_reruns_the_gate(self):
        self.r4_blocked()
        # Still failing: stays blocked, exit 3, services stopped again.
        stops = self.calls().count("compose stop backend scheduler")
        r = self.run_cmd("apply", self.T2, BAD_TAG=self.T2, MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.calls().count("compose stop backend scheduler"), stops + 1)   # the stop after the failed re-gate
        self.assertIn("inflight-at-up", self.calls())   # the in-flight note is written before the services start
        self.assertNotIn("no-inflight-at-up", self.calls())
        self.assertIn("services_stopped=backend scheduler", self.state_file("blocked"))
        self.assertNotIn("already on", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.state, "blocked")))
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))
        # Healthy now: services started, gate passes, blocked cleared.
        n = self.calls().count("compose up -d")
        r = self.run_cmd("apply", self.T2, MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater(self.calls().count("compose up -d"), n)
        self.assertFalse(os.path.exists(os.path.join(self.state, "blocked")))
        self.assertEqual(self.current()["DETERMINITY_RELEASE"], self.T2)

    def test_r4_clear_broken_while_blocked_points_at_the_right_command(self):
        self.r4_blocked()
        r = self.run_cmd("clear-broken")
        self.assertEqual(r.returncode, 3)
        self.assertNotIn("nothing to clear", r.stdout)
        self.assertIn("rollback --restore-db", r.stdout)
        self.assertIn("apply " + self.T2, r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.state, "blocked")))

    def test_r4_plain_rollback_that_passes_the_gate_clears_blocked(self):
        self.r4_blocked()
        with open(os.path.join(self.tmp, "rev_override"), "w") as f:
            f.write("rev1\n")   # the database is back at the schema T1 ran on
        r = self.run_cmd("rollback")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        self.assertFalse(os.path.exists(os.path.join(self.state, "blocked")))
        self.assertNotEqual(self.run_cmd("apply").returncode, 3)

    def test_r4_person_applies_an_older_tag_and_the_timer_stays(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T3)  # latest
        r = self.run_cmd("apply", self.T1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        self.assertIn("hs-release apply " + self.T3, r.stderr)
        r = self.run_cmd("apply")   # the timer
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("refused/held here", r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        # A person can still go forward, which lifts the hold.
        self.assertEqual(self.run_cmd("apply", self.T3).returncode, 0)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T3)

    def test_r4_person_applying_the_latest_adds_no_hold(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply", self.T1).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "refused")) and self.state_file("refused").strip())

    def test_r4_db_url_parsing(self):
        bundled = ["postgresql://u:p~postgres:5432/trinity", "postgresql+psycopg2://u~postgres/trinity",
                   "postgres://u:p~postgres:5432/trinity"]
        external = ["postgresql://u:p~postgres:5432/otherdb",
                    "postgresql://u:p~db.example.com/x?host=~postgres/",
                    "postgresql://u:p~db.example.com/trinity?host=postgres",
                    "postgresql://u:a~postgres:b~db.example.com:5432/trinity",
                    "postgresql://u:p~postgres:5433/trinity",
                    "postgresql://u~postgres:5432/trinity?sslmode=disable",   # a query string: not the bundled one
                    "postgresql://u~x:p~postgres:5432/trinity",   # a second @ in the user part
                    "postgresql://u?x~postgres/trinity", "postgresql://u#x~postgres/trinity",
                    "postgresql://u:p~postgresx/trinity"]
        # written with ~ for @ so the fixtures do not read as credentials to scanners
        bundled = [u.replace("~", "@") for u in bundled]
        external = [u.replace("~", "@") for u in external]
        self.publish(self.T1)
        for url in bundled:
            shutil.rmtree(self.state, ignore_errors=True)
            r = self.run_cmd("apply", DB_URL=url)
            self.assertEqual(r.returncode, 0, (url, r.stderr))
            self.assertIn("database backed up", r.stderr, url)
            os.remove(os.path.join(self.trinity, "release.env"))
        for url in external:
            shutil.rmtree(self.state, ignore_errors=True)
            open(self.log, "w").close()
            r = self.run_cmd("apply", DB_URL=url)
            self.assertEqual(r.returncode, 1, (url, r.stderr))
            self.assertIn("external database", r.stderr, url)
            self.assertNotIn("compose up", self.calls(), url)
            self.assertEqual(self.pinned_size(), 0, url)
        r = self.run_cmd("apply", DB_URL="postgresql://u@postgres:5432/other", PG_DBNAME="other")
        self.assertEqual(r.returncode, 0, r.stderr)   # the bundled name is read from compose
        # A query string on the database name is not a name that matches, even when the service's name has it.
        shutil.rmtree(self.state, ignore_errors=True)
        os.remove(os.path.join(self.trinity, "release.env"))
        r = self.run_cmd("apply", DB_URL="postgresql://u@postgres:5432/q?sslmode=disable", PG_DBNAME="q?sslmode=disable")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("external database", r.stderr)
        # An empty POSTGRES_DB is never "the bundled database", even for a URL with an empty name.
        r = self.run_cmd("apply", DB_URL="postgresql://u@postgres:5432/", PG_DBEMPTY="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("external database", r.stderr)
        self.assertEqual(self.pinned_size(), 0)

    def test_r4_empty_database_url_is_sqlite(self):
        # upstream's documented switch: DATABASE_URL set but empty keeps SQLite
        self.publish(self.T1)
        r = self.run_cmd("apply", URL_MODE="empty")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("pg_dump", self.calls())

    def test_r4_missing_or_null_database_url_is_refused(self):
        self.publish(self.T1)
        for mode in ("null", "missing", "nobackend"):
            shutil.rmtree(self.state, ignore_errors=True)
            open(self.log, "w").close()
            r = self.run_cmd("apply", URL_MODE=mode)
            self.assertEqual(r.returncode, 1, (mode, r.stderr))
            self.assertIn("no DATABASE_URL", r.stderr, mode)
            self.assertNotIn("compose up", self.calls(), mode)
            self.assertEqual(self.pinned_size(), 0, mode)

    def test_r4_external_rollback_needs_db_restored(self):
        env = dict(DB_URL="postgresql://u@db.example.test:5432/trinity",
                   HS_RELEASE_EXTERNAL_DB="own-backups-no-rollback-across-migration")
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply", **env).returncode, 0)
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply", **env).returncode, 0)
        r = self.run_cmd("rollback", **env)   # healthy, not blocked: still refused
        self.assertEqual(r.returncode, 1)
        self.assertIn("--db-restored", r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T2)
        r = self.run_cmd("rollback", "--db-restored", **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)

    # mutation tests: each fails when its check is deleted or inverted
    def test_r4_gate_refuses_a_service_not_running_the_pinned_image(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", STALE_SVC="frontend")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("health: frontend runs", r.stderr)

    def test_r4_gate_reads_the_alembic_revision(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        r = self.run_cmd("apply", REV_FAIL_TAG=self.T2)   # /health and the rest answer; only the read fails
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("health: cannot read alembic_version", r.stderr)

    def test_r4_soak_catches_a_recreated_container(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", RECREATE_SVC="scheduler")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("scheduler was recreated during", r.stderr)

    def test_r4_restore_checks_the_restored_revision(self):
        self.r4_blocked()
        r = self.run_cmd("rollback", "--restore-db", MIGRATES_TAG=self.T2, RESTORE_WRONG_REV="1")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("not at schema rev1", self.state_file("broken"))
        self.assertTrue(os.path.exists(os.path.join(self.state, "broken")))

    def test_r4_dump_without_the_custom_format_header_is_refused(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", DUMP_BAD="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("database backup failed", r.stderr)
        self.assertEqual(self.pinned_size(), 0)
        self.assertEqual([f for f in os.listdir(os.path.join(self.state, "backups"))], [])

    def test_r4_bundled_postgres_not_running_is_refused(self):
        self.publish(self.T1)
        r = self.run_cmd("apply", PG_DOWN="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("bundled postgres service is not running", r.stderr)
        self.assertNotIn("compose up", self.calls())

    # ---- alert URL
    def test_r4_status_and_loud_exits_warn_when_no_alert_url(self):
        self.assertIn("HS_RELEASE_ALERT_URL is not set", self.run_cmd("status", HS_RELEASE_ALERT_URL="").stdout)
        self.assertNotIn("HS_RELEASE_ALERT_URL is not set", self.run_cmd("status", HS_RELEASE_ALERT_URL="http://a.test/t").stdout)
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        r = self.run_cmd("apply", BAD_TAG=self.T2, MIGRATES_TAG=self.T2, HS_RELEASE_ALERT_URL="")   # exit 3
        self.assertEqual(r.returncode, 3)
        self.assertIn("HS_RELEASE_ALERT_URL is not set", r.stderr)
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 3)
        self.assertNotIn("HS_RELEASE_ALERT_URL is not set", r.stderr)
        r = self.run_cmd("check", HS_RELEASE_ALERT_URL="")   # exit 0 / 10 are not loud
        self.assertNotIn("HS_RELEASE_ALERT_URL is not set", r.stderr)

    def test_r4_alert_test_mode(self):
        alert = ALERT
        r = subprocess.run([alert, "--test"], env=dict(self.env, HS_RELEASE_ALERT_URL=""), capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not set", r.stderr)
        self.assertFalse(os.path.exists(self.log) and "curl-post" in self.calls())
        r = subprocess.run([alert, "--test"], env=dict(self.env, HS_RELEASE_ALERT_URL="http://alert.test/topic"),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("curl-post http://alert.test/topic", self.calls())
        r = subprocess.run([alert, "--test"], env=dict(self.env, HS_RELEASE_ALERT_URL="http://alert.test/topic", ALERT_FAIL="1"),
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)

    # ---- restore record, in-flight order, uploader
    def test_r4_restore_record_names_the_backup_restored(self):
        self.r4_blocked()
        dump = self.r4_dump()
        r = self.run_cmd("rollback", "--restore-db", MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RESTORED_FROM=" + dump, self.state_file("previous.meta"))
        self.assertIn("database restored from " + dump, r.stderr)

    def test_r4_inflight_note_is_written_before_the_agent_retag(self):
        self.publish(self.T1, agent=True)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        c = self.calls()
        self.assertIn("inflight-at-tag", c)
        self.assertNotIn("no-inflight-at-tag", c)

    def test_r4_release_json_uploaded_by_someone_else_is_refused(self):
        self.publish(self.T1, uploader="someone-else")
        r = self.run_cmd("apply")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("uploaded by 'someone-else'", r.stderr)
        self.assertEqual(self.pinned_size(), 0)

    # ---- round 5
    def test_r5_a1_external_database_regate_never_stops_services(self):
        ext = dict(DB_URL="postgresql://u~db.example.com/trinity".replace("~", "@"), HS_RELEASE_EXTERNAL_DB="own-backups-no-rollback-across-migration")
        self.r4_blocked(**ext)
        self.assertIn("services_stopped=none", self.state_file("blocked"))
        r = self.run_cmd("apply", self.T2, BAD_TAG=self.T2, **ext)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn("compose stop", self.calls())
        self.assertIn("services_stopped=none", self.state_file("blocked"))
        self.assertNotIn("stopped again", r.stderr)
        self.assertIn("nothing was stopped", r.stderr)

    def test_r5_a2_failed_stop_on_regate_is_loud_and_recorded(self):
        self.r4_blocked()
        r = self.run_cmd("apply", self.T2, BAD_TAG=self.T2, MIGRATES_TAG=self.T2, STOP_FAIL="1")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("services_stopped=FAILED", self.state_file("blocked"))
        self.assertNotIn("services_stopped=backend scheduler", self.state_file("blocked"))
        self.assertIn("may be serving", r.stderr)
        self.assertNotIn("stopped again", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.state, "broken")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))
        st = self.run_cmd("status").stdout
        self.assertIn("services stopped: FAILED", st)
        self.assertNotIn("running:", st)

    def test_r5_a3_regate_refusals_happen_before_anything_starts(self):
        self.r4_blocked()
        ups = self.calls().count("compose up")
        for env in ({"CONFIG_FAIL": "1"}, {"PG_DOWN": "1"}):
            r = self.run_cmd("apply", self.T2, MIGRATES_TAG=self.T2, **env)
            self.assertEqual(r.returncode, 1, (env, r.stderr))
            self.assertEqual(self.calls().count("compose up"), ups, env)
            self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")), env)
            self.assertIn("services_stopped=backend scheduler", self.state_file("blocked"), env)
            self.assertTrue(os.path.exists(os.path.join(self.state, "blocked")), env)

    def test_r5_a4_clear_broken_after_a_killed_regate_keeps_blocked(self):
        self.r4_blocked()
        with open(os.path.join(self.state, "inflight"), "w") as f:
            f.write("op=apply\ntag=%s\nstarted=2026-01-01T00:00:00Z\n" % self.T2)   # what a killed re-gate leaves
        r = self.run_cmd("clear-broken")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.state, "blocked")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))
        self.assertIn("BLOCKED state is still set", r.stderr)

    def test_r5_b1_older_apply_warns_when_latest_cannot_be_read_and_hold_works(self):
        self.publish(self.T1, latest=False)
        self.publish(self.T2, latest=False)
        self.assertEqual(self.run_cmd("apply", self.T2).returncode, 0)
        r = self.run_cmd("apply", self.T1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("could not read the latest release, so no hold was set", r.stderr)
        self.assertIn("hs-release hold <tag>", r.stderr)
        self.assertIn("could not read the latest release", self.state_file("log"))
        r = self.run_cmd("hold", "not-a-tag")
        self.assertEqual(r.returncode, 64)
        r = self.run_cmd("hold", self.T2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.T2, self.state_file("refused").split())
        self.assertIn("held by hand", self.state_file("log"))
        self.publish(self.T2)   # now latest
        r = self.run_cmd("apply")
        self.assertIn("refused/held here", r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)

    def test_r6_b1_unread_latest_warning_only_on_a_move_back_and_after_the_switch(self):
        warn = "could not read the latest release, so no hold was set"
        self.publish(self.T1, latest=False)
        self.publish(self.T2, latest=False)
        # First switch: nothing pinned, so a move back cannot be ruled out: warn.
        r = self.run_cmd("apply", self.T1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(warn, r.stderr)
        # Forward (T1 -> T2): the timer cannot undo it, no warning.
        r = self.run_cmd("apply", self.T2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(warn, r.stderr)
        # Back (T2 -> T1): warning, printed after the switch result.
        r = self.run_cmd("apply", self.T1)
        self.assertEqual(r.returncode, 0, r.stderr)
        err = r.stderr
        self.assertIn(warn, err)
        self.assertLess(err.index("on %s (healthy)" % self.T1), err.index(warn))
        self.assertLess(err.index("switching %s -> %s" % (self.T2, self.T1)), err.index(warn))

    def test_r6_b1_no_unread_latest_warning_when_the_older_apply_fails(self):
        warn = "could not read the latest release, so no hold was set"
        self.publish(self.T1, latest=False)
        self.publish(self.T2, latest=False)
        self.assertEqual(self.run_cmd("apply", self.T2).returncode, 0)
        r = self.run_cmd("apply", self.T1, BAD_TAG=self.T1)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(warn, r.stderr)

    def test_r5_b2_b3_hold_messages_name_every_reason_and_what_lifts_it(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T3)
        r = self.run_cmd("apply", self.T1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lifted only by hs-release apply " + self.T3, r.stderr)
        self.assertIn("deleting its line", r.stderr)
        self.assertNotIn("until a newer release", r.stderr)
        for out in (self.run_cmd("apply").stderr, self.run_cmd("check").stdout):
            self.assertIn("failed the health gate here", out)
            self.assertIn("was rolled back from", out)
            self.assertIn("a person applied an older release", out)

    def test_r5_b3_hold_is_not_set_before_the_gate_result(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T3)   # latest
        r = self.run_cmd("apply", self.T1, BAD_TAG=self.T1)   # the older tag fails: rolled back to T2
        self.assertEqual(r.returncode, 2, r.stderr)
        held = self.state_file("refused").split()
        self.assertIn(self.T1, held)
        self.assertNotIn(self.T3, held)

    def test_r5_b4_status_while_blocked_says_pinned_not_running(self):
        self.r4_blocked()
        out = self.run_cmd("status").stdout
        self.assertNotIn("running:", out)
        self.assertIn("pinned in release.env: " + self.T2, out)
        self.assertIn("services stopped: backend scheduler", out)

    def test_r5_c_invalid_alert_urls(self):
        bad = ["https://x.test/t   # REQUIRED: your topic", "https://x.test/t#c", "ftp://x.test/t", "x.test/t", "http://", "https://x.test/a b"]
        for url in bad:
            st = self.run_cmd("status", HS_RELEASE_ALERT_URL=url).stdout
            self.assertIn("set but invalid", st, url)
            self.assertNotIn("is not set", st, url)
            r = subprocess.run([ALERT, "--test"], env=dict(self.env, HS_RELEASE_ALERT_URL=url), capture_output=True, text=True, timeout=30)
            self.assertNotEqual(r.returncode, 0, url)
            self.assertIn("invalid", r.stderr, url)
            self.assertNotIn("curl-post", self.calls() if os.path.exists(self.log) else "", url)
        for url in ("http://alert.test/topic", "https://ntfy.example.com/my-topic?auth=1"):
            self.assertNotIn("invalid", self.run_cmd("status", HS_RELEASE_ALERT_URL=url).stdout, url)
        # A loud exit warns too.
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        r = self.run_cmd("apply", BAD_TAG=self.T2, MIGRATES_TAG=self.T2, HS_RELEASE_ALERT_URL="https://x.test/t # c")
        self.assertEqual(r.returncode, 3)
        self.assertIn("set but invalid", r.stderr)
        # The alert script itself: an invalid URL is not posted to, the journal gets the message and the reason.
        r = self.run_alert(HS_RELEASE_ALERT_URL="https://x.test/t   # note")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("curl-post", self.calls())
        self.assertIn("logger -p daemon.err -t hs-release-alert", self.calls())
        self.assertIn("HS_RELEASE_ALERT_URL is invalid", self.calls())

    # ---- round 6
    EXT_OK = "own-backups-no-rollback-across-migration"

    def defaults_file(self, text):
        p = os.path.join(self.tmp, "defaults")
        with open(p, "w") as f:
            f.write(text)
        return p

    def ext_env(self):
        return dict(DB_URL="postgresql://u@db.example.test:5432/trinity")

    def test_r6_r5_2_a_by_hand_external_database_reads_the_defaults_file(self):
        d = self.defaults_file("# the install's file\n\nHS_RELEASE_EXTERNAL_DB=%s\nHEALTH_URL=http://nowhere.invalid/health\n" % self.EXT_OK)
        by_hand = dict(self.ext_env(), HS_RELEASE_DEFAULTS=d, HS_RELEASE_EXTERNAL_DB=None, HEALTH_URL="http://health.test/health")
        self.publish(self.T1)
        r = self.run_cmd("apply", **by_hand)   # no variable in the environment: only the file says it
        self.assertEqual(r.returncode, 0, r.stderr)   # (HEAD: refused, rc 1)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        self.publish(self.T2)
        self.assertEqual(self.run_cmd("apply", **by_hand).returncode, 0)
        r = self.run_cmd("rollback", "--db-restored", **by_hand)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release_env()["DETERMINITY_RELEASE"], self.T1)
        # A variable already in the environment wins over the file (HEALTH_URL above would never answer).
        # Without the file's opt-in the install is still refused.
        r = self.run_cmd("apply", self.T2, **dict(by_hand, HS_RELEASE_DEFAULTS=os.path.join(self.tmp, "none")))
        self.assertEqual(r.returncode, 1)
        self.assertIn("HS_RELEASE_EXTERNAL_DB", r.stderr)

    def test_r6_r5_2_b_environment_beats_the_defaults_file(self):
        d = self.defaults_file("HS_RELEASE_EXTERNAL_DB=%s\n" % self.EXT_OK)
        self.publish(self.T1)
        r = self.run_cmd("apply", HS_RELEASE_DEFAULTS=d, HS_RELEASE_EXTERNAL_DB="something-else", **self.ext_env())
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("external database", r.stderr)
        # and the file alone would have been enough
        r = self.run_cmd("apply", HS_RELEASE_DEFAULTS=d, HS_RELEASE_EXTERNAL_DB=None, **self.ext_env())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_r6_r5_2_c_defaults_file_is_data_never_executed(self):
        mark = os.path.join(self.tmp, "ran")
        d = self.defaults_file('# c\nHS_RELEASE_ALERT_URL=$(touch %s)\nHS_RELEASE_KEEP_BACKUPS=`touch %s`\nNOT A LINE\n=x\n1BAD=y\n' % (mark, mark))
        r = self.run_cmd("status", HS_RELEASE_DEFAULTS=d, HS_RELEASE_ALERT_URL=None)
        self.assertFalse(os.path.exists(mark))
        self.assertIn("set but invalid", r.stdout)   # taken literally (HEAD: "is not set")
        r = self.run_alert(HS_RELEASE_DEFAULTS=d, HS_RELEASE_ALERT_URL=None)
        self.assertFalse(os.path.exists(mark))
        self.assertIn("invalid", r.stderr)

    def test_r6_r5_2_d_defaults_file_quotes_and_the_alert_script(self):
        d = self.defaults_file("  # indented comment\nHS_RELEASE_ALERT_URL=\"http://alert.test/quoted\"\nHS_RELEASE_MIN_AGE_HOURS='0'\n")
        r = self.run_alert(HS_RELEASE_DEFAULTS=d, HS_RELEASE_ALERT_URL=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("curl-post http://alert.test/quoted", self.calls())   # the alert reads the file itself
        st = self.run_cmd("status", HS_RELEASE_DEFAULTS=d, HS_RELEASE_ALERT_URL=None)
        self.assertNotIn("HS_RELEASE_ALERT_URL", st.stdout)
        # a trailing comment on the value stays in it, as under systemd: invalid
        d = self.defaults_file("HS_RELEASE_ALERT_URL=http://alert.test/x   # note\n")
        self.assertIn("set but invalid", self.run_cmd("status", HS_RELEASE_DEFAULTS=d, HS_RELEASE_ALERT_URL=None).stdout)

    # L9 / R5-4: the alert and status say what the blocked file says
    def write_blocked(self, value, tag=None):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "blocked"), "w") as f:
            f.write("tag=%s\nschema_before=rev1\nschema_now=rev2\nbackup=\nservices_stopped=%s\n" % (tag or self.T2, value))
        with open(os.path.join(self.trinity, "release.env"), "w") as f:
            f.write("DETERMINITY_RELEASE=%s\n" % (tag or self.T2))

    def alert_text(self, value):
        self.write_blocked(value)
        self.run_alert(HS_RELEASE_ALERT_URL="http://alert.test/topic")
        return self.calls()

    def test_r6_l9_alert_backend_scheduler_keeps_the_stopped_on_purpose_text(self):
        c = self.alert_text("backend scheduler")
        self.assertIn("stopped on purpose", c)
        self.assertIn("the app is down", c)

    def test_r6_l9_alert_none_says_nothing_was_stopped(self):
        c = self.alert_text("none")
        self.assertIn("nothing was stopped", c)
        self.assertIn("NOT been verified healthy", c)
        self.assertNotIn("stopped on purpose", c)
        self.assertNotIn("the app is down", c)

    def test_r6_l9_alert_unknown_says_check_docker_ps(self):
        c = self.alert_text("unknown (a re-gate is running: backend and scheduler were started; if this note stays, check docker ps)")
        self.assertIn("may be serving", c)
        self.assertIn("docker ps", c)
        self.assertNotIn("stopped on purpose", c)
        self.assertNotIn("the app is down", c)

    def test_r6_l9_alert_failed_says_check_docker_ps(self):
        c = self.alert_text("FAILED (the stop failed: the failed release may be serving on the moved schema; check docker ps)")
        self.assertIn("did not finish or failed", c)
        self.assertIn("may be serving", c)
        self.assertNotIn("stopped on purpose", c)
        self.assertNotIn("the app is down", c)

    def status_text(self, value):
        self.write_blocked(value)
        return self.run_cmd("status").stdout

    def test_r6_l9_status_backend_scheduler_is_not_a_running_release(self):
        out = self.status_text("backend scheduler")
        self.assertIn("NOT a running release; services stopped: backend scheduler", out)

    def test_r6_l9_status_none_says_it_is_running_unverified(self):
        out = self.status_text("none")
        self.assertIn("nothing was stopped", out)
        self.assertIn("NOT verified healthy; services stopped: none", out)
        self.assertNotIn("NOT a running release", out)

    def test_r6_l9_status_unknown_says_it_may_be_serving(self):
        out = self.status_text("unknown (a re-gate is running: x)")
        self.assertIn("may be serving: check docker ps", out)
        self.assertNotIn("NOT a running release", out)

    def test_r6_l9_status_failed_says_it_may_be_serving(self):
        out = self.status_text("FAILED (the stop failed)")
        self.assertIn("may be serving: check docker ps", out)
        self.assertNotIn("NOT a running release", out)

    # R5-4 rest
    def test_r6_r5_4a_first_failure_whose_stop_fails_is_broken_exit_4(self):
        self.publish(self.T1)
        self.assertEqual(self.run_cmd("apply").returncode, 0)
        self.publish(self.T2)
        r = self.run_cmd("apply", BAD_TAG=self.T2, MIGRATES_TAG=self.T2, STOP_FAIL_EXACT="backend scheduler")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.state, "broken")))
        self.assertIn("kind=apply-stop-failed", self.state_file("broken"))
        self.assertIn("services_stopped=FAILED", self.state_file("blocked"))
        self.assertNotIn("NOT serving", r.stderr)
        self.assertIn("may be serving", r.stderr)
        self.assertIn(self.T2, self.state_file("refused"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))
        self.assertEqual(self.run_cmd("apply").returncode, 4)   # nothing applies until clear-broken
        self.assertNotIn("running:", self.run_cmd("status").stdout)

    def test_r6_r5_4b_rollback_that_fails_the_gate_while_blocked_stops_the_services_again(self):
        self.r4_blocked()
        before = [l for l in self.calls().split("\n") if l == "compose stop backend scheduler"]
        r = self.run_cmd("rollback", "--restore-db", BAD_TAG=self.T1)
        self.assertEqual(r.returncode, 4, r.stderr)
        after = [l for l in self.calls().split("\n") if l == "compose stop backend scheduler"]
        self.assertEqual(len(after), len(before) + 1)   # the services the rollback started are stopped again
        self.assertIn("services_stopped=backend scheduler", self.state_file("blocked"))
        self.assertTrue(os.path.exists(os.path.join(self.state, "broken")))

    def test_r6_r5_4b_rollback_that_fails_the_gate_and_the_stop_says_failed(self):
        self.r4_blocked()
        r = self.run_cmd("rollback", "--restore-db", BAD_TAG=self.T1, STOP_FAIL_EXACT="backend scheduler")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("services_stopped=FAILED", self.state_file("blocked"))
        self.assertNotIn("services_stopped=backend scheduler", self.state_file("blocked"))
        self.assertIn("could NOT be stopped", self.state_file("broken"))

    def test_r6_r5_4b_external_rollback_that_fails_the_gate_while_blocked_says_none(self):
        ext = dict(self.ext_env(), HS_RELEASE_EXTERNAL_DB=self.EXT_OK)
        self.r4_blocked(**ext)
        r = self.run_cmd("rollback", "--db-restored", BAD_TAG=self.T1, **ext)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("services_stopped=none", self.state_file("blocked"))
        self.assertNotIn("compose stop", self.calls())

    def test_r6_r5_4c_clear_broken_drops_the_regate_running_note(self):
        self.r4_blocked()
        p = os.path.join(self.state, "blocked")
        with open(p) as f:
            text = f.read().replace("services_stopped=backend scheduler",
                                    "services_stopped=unknown (a re-gate is running: backend and scheduler were started; if this note stays, check docker ps)")
        with open(p, "w") as f:
            f.write(text)
        with open(os.path.join(self.state, "inflight"), "w") as f:
            f.write("op=apply\ntag=%s\nstarted=2026-01-01T00:00:00Z\n" % self.T2)
        r = self.run_cmd("clear-broken")
        self.assertEqual(r.returncode, 3, r.stderr)
        blocked = self.state_file("blocked")
        self.assertNotIn("re-gate is running", blocked)
        self.assertIn("services_stopped=unknown", blocked)
        self.assertTrue(os.path.exists(p))

    # R5-5: hold when the file has no trailing newline
    def test_r6_r5_5_hold_after_a_last_line_without_a_newline(self):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "refused"), "w") as f:
            f.write(self.T1)   # no trailing newline
        r = self.run_cmd("hold", self.T3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state_file("refused").split("\n"), [self.T1, self.T3, ""])
        self.assertFalse(os.path.exists(os.path.join(self.state, "refused.new")))
        # the timer now leaves it alone
        self.publish(self.T3)
        r = self.run_cmd("apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("refused/held here", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.trinity, "release.env")))

    def test_r6_r5_5_hold_that_cannot_be_recorded_fails_loudly(self):
        os.makedirs(os.path.join(self.state, "refused.new"))   # the temp file cannot be written
        r = self.run_cmd("hold", self.T2)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("NOT held", r.stderr)

    # R5-7: mutants that survived
    def test_r6_r5_7_regate_note_and_inflight_note_are_written(self):
        self.r4_blocked()
        ups = self.calls().split("\n").count("inflight-at-up")
        r = self.run_cmd("apply", self.T2, BAD_TAG=self.T2, MIGRATES_TAG=self.T2)
        self.assertEqual(r.returncode, 3, r.stderr)
        c = self.calls()
        self.assertEqual(len([l for l in c.split("\n") if l.startswith("blocked-at-up") and "re-gate is running" in l]), 1)
        self.assertEqual(c.split("\n").count("inflight-at-up"), ups + 1)   # written before the re-gate started the services
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))
        self.assertIn("services_stopped=backend scheduler", self.state_file("blocked"))

    def test_r6_r5_7_failed_gate_on_another_database_clears_the_note_and_the_inflight_file(self):
        ext = dict(self.ext_env(), HS_RELEASE_EXTERNAL_DB=self.EXT_OK)
        self.r4_blocked(**ext)
        ups = self.calls().split("\n").count("inflight-at-up")
        r = self.run_cmd("apply", self.T2, BAD_TAG=self.T2, **ext)
        self.assertEqual(r.returncode, 3, r.stderr)
        c = self.calls()
        self.assertEqual(c.split("\n").count("inflight-at-up"), ups + 1)   # written before the services were started
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))
        self.assertNotIn("re-gate is running", c)   # no note on a database where nothing is ever stopped
        self.assertIn("blocked-at-up none", c)

    # N8
    def test_r6_n8_failed_compose_up_on_regate_says_only_that_nothing_was_stopped(self):
        ext = dict(self.ext_env(), HS_RELEASE_EXTERNAL_DB=self.EXT_OK)
        self.r4_blocked(**ext)
        r = self.run_cmd("apply", self.T2, UP_FAIL="1", **ext)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("Nothing was stopped.", r.stderr)
        self.assertNotIn("still running", r.stderr)
        self.assertIn("services_stopped=none", self.state_file("blocked"))

    # N9: Ctrl-C / TERM during the re-gate
    def signalled_regate_with(self, base, sig, **env):
        import signal
        e = {k: v for k, v in dict(self.env, HEALTH_SOAK="60", HEALTH_SOAK_STEP="1", **env).items() if v is not None}
        # A job started in the background has SIGINT ignored, and an ignored signal cannot be trapped: reset it.
        p = subprocess.Popen([SCRIPT, "apply", self.T2], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        try:
            soaking = lambda: self.calls().count("{{.State.Running}}") > base
            deadline = time.time() + 30
            while not soaking():
                self.assertLess(time.time(), deadline, "the re-gate never reached the soak")
                self.assertIsNone(p.poll())
                time.sleep(0.2)
            os.kill(p.pid, getattr(signal, sig))
            out, err = p.communicate(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
        return p.returncode, err

    def prepare_signal(self, **env):
        self.r4_blocked(**env)
        return self.calls().count("{{.State.Running}}")

    def test_r6_n9_term_during_the_regate_stops_the_services_and_settles_the_file(self):
        base = self.prepare_signal()
        stops = self.calls().split("\n").count("compose stop backend scheduler")
        rc, err = self.signalled_regate_with(base, "SIGTERM")
        self.assertEqual(rc, 143, err)
        self.assertEqual(self.calls().split("\n").count("compose stop backend scheduler"), stops + 1)
        self.assertIn("services_stopped=backend scheduler", self.state_file("blocked"))
        self.assertNotIn("re-gate is running", self.state_file("blocked"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "broken")))

    def test_r6_n9_int_during_the_regate_exits_130(self):
        base = self.prepare_signal()
        rc, err = self.signalled_regate_with(base, "SIGINT")
        self.assertEqual(rc, 130, err)
        self.assertIn("services_stopped=backend scheduler", self.state_file("blocked"))

    def test_r6_n9_term_on_another_database_writes_none_and_stops_nothing(self):
        ext = dict(self.ext_env(), HS_RELEASE_EXTERNAL_DB=self.EXT_OK)
        base = self.prepare_signal(**ext)
        rc, err = self.signalled_regate_with(base, "SIGTERM", **ext)
        self.assertEqual(rc, 143, err)
        self.assertIn("services_stopped=none", self.state_file("blocked"))
        self.assertNotIn("compose stop", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))

    def test_r6_n9_term_with_a_failed_stop_is_failed_and_broken(self):
        base = self.prepare_signal()
        rc, err = self.signalled_regate_with(base, "SIGTERM", STOP_FAIL_EXACT="backend scheduler")
        self.assertEqual(rc, 143, err)
        self.assertIn("services_stopped=FAILED", self.state_file("blocked"))
        self.assertTrue(os.path.exists(os.path.join(self.state, "broken")))
        self.assertIn("kind=regate-stop-failed", self.state_file("broken"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "inflight")))


class DeterminityVersionTest(unittest.TestCase):
    """determinity-version in a throwaway git repo: numbers are never reused."""
    SCRIPT = os.path.join(HERE, "determinity-version")

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.git("init", "-q", "-b", "hq")
        self.git("config", "user.email", "t@example.test")
        self.git("config", "user.name", "t")
        with open(os.path.join(self.repo, "VERSION"), "w") as f:
            f.write("1.0.5\n")
        self.git("add", "VERSION")
        self.git("commit", "-q", "-m", "upstream 1.0.5")
        self.git("update-ref", "refs/upstream-tags/v1.0.5", "HEAD")
        self.commit("local work")

    def tearDown(self):
        shutil.rmtree(self.repo)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True).stdout

    def commit(self, msg):
        self.git("commit", "-q", "--allow-empty", "-m", msg)

    def version(self, **env):
        r = subprocess.run([self.SCRIPT], cwd=self.repo, env=dict(os.environ, **env), capture_output=True, text=True)
        out = dict(l.split("=", 1) for l in r.stdout.splitlines())
        return r, out

    def test_first_release_is_number_one_and_the_output_contract_is_unchanged(self):
        r, out = self.version()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([l.split("=")[0] for l in r.stdout.splitlines()],
                         ["tag", "version", "upstream_tag", "upstream_commit", "revision", "short_sha", "prev_tag"])
        self.assertEqual(out["tag"], "v1.0.5-determinity.1")
        self.assertEqual(out["version"], "1.0.5-determinity.1")
        self.assertEqual(out["prev_tag"], "v1.0.5")

    def test_the_next_number_follows_the_highest_tag_even_after_a_release_was_deleted(self):
        # Deleting a GitHub Release leaves its tag: .2 was released and deleted, .1 and .3 are gone from view.
        for n in (1, 2):
            self.commit(f"release {n}")
            self.git("tag", f"v1.0.5-determinity.{n}")
        self.commit("more")
        r, out = self.version()
        self.assertEqual(out["tag"], "v1.0.5-determinity.3", r.stderr)
        self.assertEqual(out["prev_tag"], "v1.0.5-determinity.2")
        # A gap in the tags (one was deleted outright) is not filled: the highest wins.
        self.git("tag", "-d", "v1.0.5-determinity.1")
        self.assertEqual(self.version()[1]["tag"], "v1.0.5-determinity.3")

    def test_a_number_whose_tag_is_gone_is_not_reused_when_the_release_list_is_given(self):
        self.commit("release 1")
        self.git("tag", "v1.0.5-determinity.1")
        self.commit("release 2 (tag deleted afterwards)")
        self.commit("more")
        self.assertEqual(self.version()[1]["tag"], "v1.0.5-determinity.2")   # the limit RELEASES.md states
        r, out = self.version(DETERMINITY_KNOWN_TAGS="v1.0.5-determinity.2  v1.0.5-determinity.1 v1.0.4-determinity.9 junk")
        self.assertEqual(out["tag"], "v1.0.5-determinity.3", r.stderr)

    def test_a_released_head_is_refused(self):
        self.git("tag", "v1.0.5-determinity.1")
        r, _ = self.version()
        self.assertEqual(r.returncode, 1)
        self.assertIn("already released", r.stderr)


if __name__ == "__main__":
    unittest.main()
