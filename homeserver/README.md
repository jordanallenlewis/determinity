# Home server layer for Trinity

This folder holds what it takes to run [Trinity](https://github.com/abilityai/trinity) on one always-on machine at
home, with a fleet of agents that work unattended. Upstream Trinity is unchanged. Everything here sits beside it and
can be turned on one piece at a time. Each piece has its own README with the full steps.

All host names, addresses, accounts and paths are placeholders, so replace them with your own. The examples use
`trinity.example.com`, `192.0.2.0/24` for the home network, `OWNER/REPO` on GitHub, `agent-a`, and config under
`/etc/homeserver/`.

## How the pieces fit

```
 browser ──> Caddy ──> sign-in gate ──> Trinity web UI (127.0.0.1 only)
                                         │
 agent containers (172.28.0.0/16) ───────┤ firewall: host ports allowed for agents = gateway + broker only
                                         │
                                         ├─> gateway ──> devices, accounts, APIs (credentials stay on the host;
                                         │               writes need an approval from Trinity's operator queue)
                                         └─> GitHub broker ──> GitHub (the only token lives on the host)
                                                  │
                    review signer ── posts review/* statuses ──> merge gate merges green, reviewed, clean PRs
 caretaker / ops desk / capguard: small timers that keep the fleet healthy and leave people only what needs them
```

## The pieces

### Compose override: [`compose/`](compose/)
**What:** a `docker compose` override that binds every Trinity port to loopback, sets memory and CPU limits, and
mounts the start-time patches.
**Why:** the upstream quick start publishes ports on all interfaces. On a home server that exposes the backend and
collides with other services.
**Enable:** copy it next to the upstream compose file and run Trinity through the `compose/trinity` wrapper.

### Start-time patch: [`patches/`](patches/)
**What:** `stuck-threshold.py` makes the business health check use the configured stuck-execution threshold
instead of a hardcoded 30 minutes.
**Why:** agents with long execution timeouts were flagged as stuck while they were still working normally.
**Enable:** it is mounted and run by the compose override. It does nothing once upstream changes those lines.

### Hardened agent image: [`agent-image/`](agent-image/)
**What:** an overlay built on Trinity's agent base image. It bakes in packages and replaces blanket passwordless
sudo with a rule for Trinity's own startup commands only.
**Why:** an agent that can sudo can switch off every guard inside its container.
**Enable:** `agent-image/harden.sh <base-image-tag>`, then recreate the agents.

### Agent firewall: [`firewall/`](firewall/)
**What:** an nftables table that lets agent containers reach the internet and a short list of host ports, and
drops the home network, the VPN range, link-local and every other host port.
**Why:** an agent with a shell must not be able to browse the home network or the host's admin services.
**Enable:** check the file with `nft -c -f`, then load it.

### Egress gateway: [`gateway/`](gateway/)
**What:** a small host-side service that agents call instead of calling devices, accounts and third-party APIs
directly. It holds the credentials and lets each agent use only the named actions on its allow-list, with checked
parameters. For a write the gateway raises the operator-queue ask itself, from the action's description and the exact
parameters, and the call goes through only after a person approves that ask. Each approval works once.
**Why:** an approval written into an agent's instructions can be skipped. Putting it in the path the call takes
makes it hold.
**Enable:** write `actions.json`, `acl.json` and the token hashes, then install the unit. Use the firewall so agents
can reach only this port and the broker.

### GitHub broker: [`github-broker/`](github-broker/)
**What:** a small host-side proxy and the only route from agents to GitHub. Each agent gets its own bearer
token, and access is checked against a per-repo policy and a list of protected branches. Each agent has an
hourly budget. The broker decides on the canonical, decoded path and forwards that path; on merge-gate repos it
refuses merges, status and check writes at any path depth, merge/hold label changes, base changes and deleting
workflow runs; everywhere it refuses `update-branch`, run and deployment approvals, route names with a suffix
(`merge.json`), pull requests from a protected head, query strings on writes
and repository administration writes. Any push to a protected branch is refused, so a protected branch's history
cannot be rewritten through the broker.
**Why:** agents never hold a GitHub token. Scope, protected branches and budget are enforced in one place.
**Enable:** install the script and unit, write the policy, then point each agent's git at it with `hs-gh git-setup`.

### Review signer: [`review-signer/`](review-signer/)
**What:** a root-owned socket service that posts `review/<role>` commit statuses with neutral wording. The caller
is identified from the socket peer, and only callers who hold that role can trigger it.
**Why:** reviewers sign through one checked path, and the merge gate counts review statuses only from the signer's
account. That account must be a dedicated GitHub account no other token on the host belongs to. Through the
broker, agents cannot post statuses on merge-gate repos (by any path).
**Enable:** install the socket and service units, then write `roles.json`.

### Merge gate: [`merge-gate/`](merge-gate/)
**What:** a one-minute timer that merges a labelled, non-draft PR only when its checks are green and the review
statuses its risk tier needs are on the head commit, posted by the signer's account. A check is green only
through a check run; a status can block a check but never pass one. It refuses to merge when the signer's login is
its own or the broker's. Every commit's author and committer email must be on the owner list.
**Why:** green PRs get merged by policy instead of waiting on a person. Agents can't merge a gated repo's pull requests through the broker.
Keep GitHub branch protection on as the backstop.
**Enable:** write `config.json`, then run `--dry-run` before you enable the timer.

### Ops scripts: [`ops/`](ops/)
**What:** three small, no-LLM timers:
- **caretaker** restarts what should be running and records the rest as findings;
- **ops desk** sorts the operator queue by rules;
- **capguard** pauses non-essential schedules when usage runs high and restores them later.

**Why:** a fleet needs predictable babysitting, so people see only the items that need them.
**Enable:** each one starts in dry-run mode. Set `"act": true` once its output looks right.

### Reverse proxy and sign-in gate: [`caddy/`](caddy/)
**What:** a Caddyfile example that serves the web UI only to the home network and VPN, behind a
password-and-authenticator gate (TinyAuth through `forward_auth`). Token-authenticated API paths bypass the gate.
**Why:** the admin UI can run code on the host, so it gets a second factor.
**Enable:** run `hs-trinity-gate init`, then `hs-trinity-gate on`. The break-glass switch is
`hs-trinity-gate off`.

## Licence

Apache 2.0, as for the rest of the repository. See [`../NOTICE`](../NOTICE) for the credit to Ability.ai.
