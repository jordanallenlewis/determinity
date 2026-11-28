#!/usr/bin/env python3
"""Checks of homeserver/ci/run-unit-baseline.sh and homeserver/ci/unit-failure-baseline.txt.

The script is run for real with a stand-in `python` first on PATH: the stand-in writes a prepared JUnit file
where pytest would, and passes every other call to the real interpreter, so the upstream checker runs as in CI.
"""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(HERE, "run-unit-baseline.sh")
FORK_BASELINE = os.path.join(HERE, "unit-failure-baseline.txt")
UPSTREAM_BASELINE = os.path.join(ROOT, "tests", "unit_failure_baseline.txt")

ID_RE = re.compile(r"^(test_\w+)::(test_\w+)(\[[^\]]+\])?$")

STAND_IN = """#!/usr/bin/env bash
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  printf '%s\\n' "$PWD" "$@" > "$FAKE_ARGS"
  for a in "$@"; do case $a in --junit-xml=*) out=${a#--junit-xml=};; esac; done
  cp "$FAKE_JUNIT" "$out"
  exit 1
fi
exec "$REAL_PYTHON" "$@"
"""


def ids(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def junit(failing, passing=()):
    cases = []
    for test_id in failing:
        cls, name = test_id.split("::", 1)
        cases.append(f'<testcase classname="{cls}" name="{name}"><failure message="x"/></testcase>')
    for test_id in passing:
        cls, name = test_id.split("::", 1)
        cases.append(f'<testcase classname="{cls}" name="{name}"/>')
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'


class BaselineFile(unittest.TestCase):
    def test_every_entry_names_a_real_test(self):
        entries = ids(FORK_BASELINE)
        self.assertTrue(entries)
        for entry in entries:
            m = ID_RE.match(entry)
            self.assertIsNotNone(m, entry)
            module = os.path.join(ROOT, "tests", "unit", m.group(1) + ".py")
            self.assertTrue(os.path.isfile(module), entry)
            with open(module, encoding="utf-8") as f:
                self.assertIn(f"def {m.group(2)}(", f.read(), entry)

    def test_every_entry_has_a_reason_above_it(self):
        with open(FORK_BASELINE, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f]
        seen_reason = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                seen_reason = False
            elif stripped.startswith("#"):
                seen_reason = True
            else:
                self.assertTrue(seen_reason, f"no comment line above {stripped!r}")

    def test_no_entry_repeats_upstream_baseline(self):
        self.assertFalse(set(ids(FORK_BASELINE)) & set(ids(UPSTREAM_BASELINE)))


class Script(unittest.TestCase):
    def run_script(self, failing, passing=()):
        with tempfile.TemporaryDirectory() as tmp:
            stand_in = os.path.join(tmp, "python")
            with open(stand_in, "w") as f:
                f.write(STAND_IN)
            os.chmod(stand_in, os.stat(stand_in).st_mode | stat.S_IXUSR)
            fake = os.path.join(tmp, "junit.xml")
            with open(fake, "w") as f:
                f.write(junit(failing, passing))
            args_file = os.path.join(tmp, "args")
            summary = os.path.join(tmp, "summary.md")
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ.get("PATH", ""),
                       FAKE_JUNIT=fake, FAKE_ARGS=args_file, REAL_PYTHON=sys.executable)
            proc = subprocess.run(["bash", SCRIPT, summary], env=env, capture_output=True, text=True)
            with open(args_file) as f:
                pytest_args = f.read().splitlines()
            with open(summary) as f:
                summary_text = f.read()
        return proc, pytest_args, summary_text

    def test_passes_when_only_baselined_tests_fail(self):
        proc, _, summary = self.run_script(ids(FORK_BASELINE), passing=["test_x::test_ok"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("baselined: " + str(len(ids(FORK_BASELINE))), summary)

    def test_fails_on_a_failure_outside_both_files(self):
        proc, _, summary = self.run_script(ids(FORK_BASELINE) + ["test_x::test_new_failure"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("test_x::test_new_failure", summary)

    def test_fails_when_no_test_ran(self):
        proc, _, _ = self.run_script([])
        self.assertNotEqual(proc.returncode, 0)

    def test_runs_the_suite_the_release_gate_runs(self):
        _, args, _ = self.run_script([], passing=["test_x::test_ok"])
        self.assertEqual(args[0], os.path.join(ROOT, "tests"))
        for expected in ("unit/", "not slow", "--timeout=300", "logical", "loadfile", "--randomly-seed=12345"):
            self.assertIn(expected, args[1:])


class Workflows(unittest.TestCase):
    def test_release_and_pull_request_jobs_call_the_script(self):
        for name in ("determinity-release.yml", "hq-unit-baseline.yml"):
            with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn('bash homeserver/ci/run-unit-baseline.sh "$GITHUB_STEP_SUMMARY"', text, name)
            self.assertNotIn("check-failure-baseline.py", text, name)

    def test_pull_request_job_has_no_path_filter(self):
        with open(os.path.join(ROOT, ".github", "workflows", "hq-unit-baseline.yml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("branches: [hq]", text)
        self.assertNotRegex(text, r"(?m)^\s+paths(-ignore)?:")


class ImageWorkflows(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as f:
            return f.read()

    def test_release_has_no_agent_image_job(self):
        text = self.read("determinity-release.yml")
        self.assertNotRegex(text, r"(?m)^  agent-image:")
        self.assertNotIn("digest-agent", text)
        self.assertRegex(text, r"(?m)^    needs: \[plan, images\]$")
        self.assertNotIn("agent-image", text.split("\n  release:\n", 1)[1])
        self.assertIn("for img in backend frontend scheduler mcp-server; do", text)

    def test_images_check_runs_only_on_pull_requests_into_hq(self):
        text = self.read("hq-images-check.yml")
        on = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual([l.strip() for l in on.strip().split("\n")], ["pull_request:", "branches: [hq]"])
        self.assertRegex(text, r"(?m)^permissions:\n  contents: read\n")
        self.assertRegex(text, r"(?m)^  cancel-in-progress: true$")

    def test_images_check_never_pushes_or_writes_cache(self):
        text = self.read("hq-images-check.yml")
        self.assertIn("push: false", text)
        self.assertNotRegex(text, r"(?m)^\s*push:\s*true")
        self.assertNotIn("cache-to", text)
        self.assertNotIn("docker/login-action", text)

    def test_images_check_builds_the_images_hq_images_publishes(self):
        def matrix(text):
            return re.findall(r"- image: (\S+)\n\s+context: (\S+)\n\s+dockerfile: (\S+)\n\s+build_args: (\S+)", text)
        published = matrix(self.read("hq-images.yml"))
        self.assertEqual(len(published), 4)
        self.assertEqual(matrix(self.read("hq-images-check.yml")), published)


if __name__ == "__main__":
    unittest.main()
