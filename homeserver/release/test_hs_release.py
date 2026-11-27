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
# Stub: serves $FAKE_WEB/<sanitised url>; the health URL fails while the
# release in release.env is $BAD_TAG.
url="${@: -1}"
out=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do [ "${args[$i]}" = -o ] && out="${args[$((i+1))]}"; done
if [ "$url" = "$HEALTH_URL" ]; then
  rel=$(sed -n 's/^DETERMINITY_RELEASE=//p' "$TRINITY_DIR/release.env" 2>/dev/null)
  [ -n "$BAD_TAG" ] && [ "$rel" = "$BAD_TAG" ] && exit 22
  exit 0
fi
f="$FAKE_WEB/$(printf '%s' "$url" | tr '/:?=' '____')"
[ -f "$f" ] || exit 22
if [ -n "$out" ]; then cp "$f" "$out"; else cat "$f"; fi
"""

DOCKER = r"""#!/usr/bin/env bash
# Stub: a container is named after its service; it runs whatever release.env
# (or, before any release, the upstream image) says.
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
    if [ "$fmt" = '{{.Config.Image}}' ]; then img_for "$id"; else echo "id:$(img_for "$id")"; fi ;;
  image) echo "id:$5" ;;
  *) exit 0 ;;
esac
"""

COMPOSE = r"""#!/usr/bin/env bash
echo "compose $*" >> "$FAKE_LOG"
case "$1" in
  ps) echo "$3" ;;
  up) exit "${UP_FAIL:-0}" ;;
esac
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
        for name, body in (("curl", CURL), ("docker", DOCKER), ("fake-compose", COMPOSE)):
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
            HEALTH_TIMEOUT="1",
            COMPOSE="fake-compose",
            FAKE_WEB=self.web,
            FAKE_LOG=self.log,
            BAD_TAG="",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def serve(self, url, body):
        name = url.replace("/", "_").replace(":", "_").replace("?", "_").replace("=", "_")
        with open(os.path.join(self.web, name), "w") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def publish(self, tag, latest=True, prerelease=False, draft=False, contract=None):
        asset = f"https://dl.example.test/{tag}/release.json"
        rel = contract or {
            "schema": "determinity-release/1", "tag": tag, "version": tag[1:],
            "channel": "stable",
            "images": {s: {"ref": f"ghcr.io/owner/determinity-{s}@{digest(tag, s)}"} for s in SERVICES},
        }
        meta = {"tag_name": tag, "draft": draft, "prerelease": prerelease,
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
        self.assertIn("failed the health gate here before", r.stderr)
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


if __name__ == "__main__":
    unittest.main()
