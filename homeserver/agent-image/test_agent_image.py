#!/usr/bin/env python3
"""Offline tests for the agent-image overlay: sudoers rules, the startup.sh rewrite and hs-root-hook."""
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load_hook():
    loader = importlib.machinery.SourceFileLoader("hs_root_hook", os.path.join(HERE, "hs-root-hook"))
    spec = importlib.util.spec_from_loader("hs_root_hook", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def rules():
    with open(os.path.join(HERE, "sudoers-agent"), encoding="utf-8") as fh:
        return [l.strip() for l in fh if l.strip() and not l.lstrip().startswith("#")]


class SudoersTest(unittest.TestCase):
    def test_no_setenv_tag(self):
        # SETENV lets the caller set LD_PRELOAD for a root command: never allowed.
        for line in rules():
            self.assertNotIn("SETENV:", line.replace("NOSETENV:", ""), line)

    def test_no_env_keep(self):
        for line in rules():
            self.assertNotIn("env_keep", line, line)

    def test_setenv_disabled_for_developer(self):
        self.assertIn("Defaults:developer env_reset, !setenv", rules())

    def test_only_exact_commands(self):
        cmds = [l.split("NOPASSWD:", 1)[1].strip() for l in rules() if "NOPASSWD:" in l]
        self.assertEqual(cmds, [
            "/usr/local/sbin/hs-root-hook runtime-config",
            "/usr/local/sbin/hs-root-hook routing-env",
            "/usr/bin/mkdir -p /var/run/sshd",
            "/usr/bin/chmod 0755 /var/run/sshd",
            "/usr/sbin/sshd -D",
        ])
        for c in cmds:
            self.assertNotIn("ALL", c)
            self.assertNotIn("*", c)


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "Dockerfile"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_startup_calls_rewritten_without_sudo_E(self):
        repl = re.findall(r"sed -i 's#([^#]*)#([^#]*)#' /app/startup.sh", self.text)
        targets = [new for _, new in repl]
        self.assertIn("sudo -n /usr/local/sbin/hs-root-hook runtime-config", targets)
        self.assertIn("sudo -n /usr/local/sbin/hs-root-hook routing-env", targets)
        for new in targets:
            self.assertNotIn("sudo -E", new)
        self.assertIn("! grep -q 'sudo -E' /app/startup.sh", self.text)

    def test_app_root_owned(self):
        self.assertIn("chown -R root:root /app", self.text)


def self_test_body():
    with open(os.path.join(HERE, "harden.sh"), encoding="utf-8") as fh:
        return fh.read().split("-c '", 1)[1].split("'; then", 1)[0]


class SelfTestScriptTest(unittest.TestCase):
    def run_helper(self, call):
        """Run the self-test's own helper definitions (everything before the first check) with one call."""
        body = self_test_body()
        defs = body.split('  NOPW=', 1)[0]
        return subprocess.run(["sh", "-c", defs + "\n" + call], capture_output=True, text=True).returncode

    def test_refuse_requires_the_refusal_message(self):
        """A command that fails for another reason (a hook that ran and then crashed) is not a refusal."""
        self.assertNotEqual(self.run_helper('refuse "password is required" sh -c "echo hook crashed >&2; exit 1"'), 0)
        self.assertEqual(self.run_helper('refuse "password is required" sh -c "echo sudo: a password is required >&2; exit 1"'), 0)
        self.assertNotEqual(self.run_helper('refuse "password is required" true'), 0)  # allowed = failure

    def test_every_refusal_names_its_expected_message(self):
        for line in self_test_body().splitlines():
            words = line.split()
            if words[:1] == ["refuse"]:
                self.assertTrue(words[1].startswith('"'), line)  # a quoted pattern comes first


    def test_ld_preload_refusal_is_checked(self):
        with open(os.path.join(HERE, "harden.sh"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('refuse "not allowed to set the following environment variables" sudo -n LD_PRELOAD=/nonexistent.so '
                      '/usr/local/sbin/hs-root-hook routing-env', text)
        # `! cmd` never trips `set -e`; a negative check written that way checks nothing.
        body = text.split("-c '", 1)[1].split("'; then", 1)[0]
        for line in body.splitlines():
            self.assertFalse(line.strip().startswith("! "), line)


class RootHookTest(unittest.TestCase):
    def setUp(self):
        self.h = load_hook()
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = os.path.join(self.tmp.name, "proc")
        os.makedirs(os.path.join(self.proc, "1"))
        os.makedirs(os.path.join(self.proc, "sys", "kernel", "random"))
        with open(os.path.join(self.proc, "sys", "kernel", "random", "boot_id"), "w") as fh:
            fh.write("boot-a\n")
        self.set_start(1000)
        env = {"AGENT_GUARDRAILS": '{"max_turns_chat": 5}', "HTTPS_PROXY": "http://proxy.example:3128",
               "LD_PRELOAD": "/tmp/evil.so", "PYTHONPATH": "/tmp", "BASH_ENV": "/tmp/x", "HOME": "/home/developer"}
        with open(os.path.join(self.proc, "1", "environ"), "wb") as fh:
            fh.write(b"\0".join(("%s=%s" % kv).encode() for kv in env.items()) + b"\0")
        self.state = os.path.join(self.tmp.name, "run")
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def set_start(self, ticks):
        rest = ["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 10
        with open(os.path.join(self.proc, "1", "stat"), "w") as fh:
            fh.write("1 (start up.sh) " + " ".join(rest) + "\n")

    def run_hook(self, name, euid=0):
        return self.h.main(["hs-root-hook", name], proc_root=self.proc, state_dir=self.state,
                           execve=lambda p, a, e: self.calls.append((p, a, e)), euid=euid)

    def test_env_comes_from_pid1_not_caller(self):
        os.environ["AGENT_GUARDRAILS"] = '{"disallowed_tools": []}'
        try:
            self.run_hook("runtime-config")
        finally:
            del os.environ["AGENT_GUARDRAILS"]
        path, argv, env = self.calls[0]
        self.assertEqual(argv, ["/usr/bin/python3", "-E", "-s", "/opt/trinity/hooks/write-runtime-config.py"])
        self.assertEqual(env["AGENT_GUARDRAILS"], '{"max_turns_chat": 5}')
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:3128")

    def test_loader_and_interpreter_vars_dropped(self):
        self.run_hook("routing-env")
        env = self.calls[0][2]
        for name in ("LD_PRELOAD", "PYTHONPATH", "BASH_ENV"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], self.h.SAFE_PATH)

    def test_once_per_container_start(self):
        self.assertEqual(self.run_hook("routing-env"), 1)  # stub execve returns; real exec never does
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.run_hook("routing-env"), 1)
        self.assertEqual(len(self.calls), 1)  # second run refused before exec
        self.set_start(2000)  # container restarted: PID 1 has a new start time
        self.run_hook("routing-env")
        self.assertEqual(len(self.calls), 2)

    def test_hooks_claimed_separately(self):
        self.run_hook("runtime-config")
        self.run_hook("routing-env")
        self.assertEqual(len(self.calls), 2)

    def test_unknown_name_and_non_root_refused(self):
        self.assertEqual(self.run_hook("anything"), 2)
        self.assertEqual(self.run_hook("routing-env", euid=1000), 1)
        self.assertEqual(self.calls, [])

    def test_missing_pid1_environ_fails_closed(self):
        os.remove(os.path.join(self.proc, "1", "environ"))
        self.assertEqual(self.run_hook("routing-env"), 1)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
