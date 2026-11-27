#!/usr/bin/env python3
"""Offline checks of agents.nft.example's structure (no nft needed; `nft -c -f` is still step 2 of the README).

In an `inet` table an `ip saddr`/`ip daddr` match never matches an IPv6 packet, so `ip saddr != NET return`
does not return IPv6 and a later catch-all drop would drop it. Each chain must return non-IPv4 first.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def chains(text):
    """{chain name: [rule lines]} for the homeserver_agents table, comments and blank lines removed."""
    out, current, depth = {}, None, 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"chain (\w+) \{$", line)
        if m:
            current = m.group(1)
            out[current] = []
            continue
        if line == "}" and current:
            current = None
            continue
        if current:
            out[current].append(line)
    return out


class RulesetTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "agents.nft.example"), encoding="utf-8") as fh:
            self.text = fh.read()
        self.chains = chains(self.text)

    def test_table_is_inet(self):
        self.assertIn("table inet homeserver_agents {", self.text)

    def test_both_chains_present(self):
        self.assertEqual(sorted(self.chains), ["forward", "input"])

    def test_non_ipv4_returns_first_in_every_chain(self):
        for name, rules in self.chains.items():
            body = [r for r in rules if not r.startswith("type ")]
            self.assertEqual(body[0], "meta nfproto != ipv4 return", name)

    def test_no_drop_or_accept_before_the_ipv4_guard(self):
        for name, rules in self.chains.items():
            body = [r for r in rules if not r.startswith("type ")]
            guard = body.index("meta nfproto != ipv4 return")
            for r in body[:guard]:
                self.assertNotRegex(r, r"\b(drop|accept|reject)\b", name)

    def test_catch_all_drop_only_after_agent_source_return(self):
        rules = [r for r in self.chains["input"] if not r.startswith("type ")]
        src = rules.index("ip saddr != $AGENT_NET return")
        drops = [i for i, r in enumerate(rules) if r.endswith("drop")]
        self.assertTrue(drops)
        self.assertTrue(all(i > src for i in drops))

    def test_policies_accept(self):
        for name, rules in self.chains.items():
            self.assertRegex(rules[0], r"policy accept;$", name)


if __name__ == "__main__":
    unittest.main()
