# github-broker

## What it does

`ghbroker.py` is a small HTTP proxy (Python stdlib only) that runs on the host. It is the only way agent containers reach GitHub.

- `/gh/<REST path>` is forwarded to the GitHub REST API.
- `/git/OWNER/REPO.git/...` is forwarded as git smart-HTTP (`info/refs`, `git-upload-pack`, `git-receive-pack`).
- Each agent has its own bearer token. The broker stores only the sha256 of each token, never the token.
- A policy file maps each agent to repos with `read` or `write`.
- It refuses writes to protected branches (`main`, `master` plus any patterns you add), on both the REST API and git pushes (the push command lines are read before anything is forwarded). It refuses direct `PUT /pulls/N/merge` (also when the path is percent-encoded) on repos the merge gate owns. On every repo it refuses `PUT /pulls/N/update-branch` (GitHub merges the base into the head branch, which may be protected; merge the base locally and push instead) and `POST /pulls` whose `head` is a protected branch (`owner:branch` included). If a repo's default branch is not `main` or `master`, add it to `protected_branches`.
- Every REST path is canonicalised before any decision, and the canonical path (each segment re-encoded) is what gets forwarded, never the raw one. Percent-escapes are decoded (`%6derge` is `merge`, so it meets the same rules). Refused with 400: encoded dots, slashes, backslashes and percent signs (`%2e`, `%2f`, `%5c`, `%25`, any case, which also rules out double encoding), a literal backslash, malformed escapes, invalid UTF-8 or raw non-ASCII, control characters and NUL, `;` parameters, `.`, `..` and empty segments, and a literal `?` or `#` inside the path. One trailing slash is dropped. Query strings are passed through on `GET` and `HEAD` only; on any write a query string is refused with 400, because decisions read only the path and body.
- Repository administration is refused for any write, matched on whole path segments after `/repos/OWNER/REPO` (so `contents/hooks.md` is fine, `hooks/1` is not): `PATCH` or `DELETE` of the repo itself (settings, visibility, default branch, deletion); `actions/permissions`, `actions/secrets`, `actions/variables`, `actions/runners`, `actions/oidc`; `environments`, `secrets`, `dependabot/secrets`, `codespaces/secrets`; `pages`; `visibility`; `merge-upstream`; `autolinks`; `deployment-protection-rules`; `branches/<branch>/protection`; `rules`, `rulesets`; `hooks`; `keys`; `collaborators`; `invitations`; `transfer`; `vulnerability-alerts`; `automated-security-fixes`; `private-vulnerability-reporting`; `interaction-limits`; `teams`. Topics stay allowed. Reads are not affected.
- On every write, the issue, pull request, comment, review and reaction numbers in the path must be plain digits
  (`[1-9][0-9]*`): GitHub reads only the leading digits, so `pulls/7abc/merge`, `pulls/7%20/merge`, `pulls/7.json/merge`
  or `pulls/07/merge` would be the merge of PR 7 and slip past any rule that names a number. Such a write is refused
  with 400. Reads are not affected. `POST /pulls` needs a plain `head` (`BRANCH` or `OWNER:BRANCH`, no space, no second
  colon), so the protected-branch check reads the name GitHub reads. Route names on writes are matched exactly: the
  first segment, the one after `issues/N` or `pulls/N`, after `actions/`, after `actions/runs/<id>` and after
  `commits/<sha>` must be lower-case letters, `_` and `-` only, so `pulls/7/merge.json` or `labels.json` is refused
  with 400 (file paths, branch, tag and label names are values, not route names, and are not affected).
- On every repo it refuses approving a run's deployments or a fork's run (`actions/runs/<id>/pending_deployments`,
  `approve`, `deployment_protection_rule`), since the broker's account may be an environment's required reviewer.
- On repos listed in `merge_gate_repos` the inputs of the merge gate are also off limits to agents. Refused: any write whose path has a `statuses`, `check-runs` or `check-suites` segment at any depth (so `statuses/<sha>` and its alias `commits/<sha>/statuses`, rerequests included); a `PATCH` of a pull request that changes its `base`; `DELETE` under `actions/` (workflow runs, logs, artifacts) and switching a workflow off or on; adding, removing, replacing or clearing the merge label or the hold label (`POST` with a body naming them, `DELETE issues/N/labels/<name>`, any `PUT` or whole-list `DELETE` on `issues/N/labels`, a `labels` key in a `PATCH` of an issue or pull request, `labels` naming them when creating an issue); and any write to `labels/<name>` (rename, recolour, delete). Label names are matched case-insensitively. Other labels can still be added and removed. On repos that are not merge-gate repos these writes are allowed.
- On git pushes it reads the ref updates and refuses any push to a protected branch, so a protected branch's history cannot be rewritten through the broker.
- REST write bodies are checked against the commit message policy: the regexes you list in `extra_message_patterns` (case-insensitive, multi-line; none by default). A match is a 403 with a reason.
- Each agent has an hourly call budget. Over it you get 429 with `Retry-After`.
- Every call is logged as one line: agent, method, path (no query string), status. No bodies, no tokens.
- `hs-gh` is the client wrapper agents run inside their container.

Known limits: decisions use the request path and JSON body only, so other routes that influence a merge gate (a pull request review, a workflow an agent can trigger) are not covered. Pushed packs are not read: a push can add a workflow file, and on a repo whose CI is GitHub Actions that workflow can post check runs under any name. The token is one control (step 2): without the Workflows permission GitHub itself refuses such a push. It is not the whole stop: code an existing workflow runs from the branch (a test script, a Makefile, a local action) can post a required check name wherever the job token can write checks, so the stop is a signed lead review of the head plus job tokens without `checks: write` (or `check_app_ids` pinned to an app that is not GitHub Actions) (see `../merge-gate`). On merge-gate repos the `statuses` / `check-runs` / `check-suites` rule matches any segment, so a file path or branch with such a segment (`contents/statuses/x.md`) cannot be written through REST there; push it with git instead. Administration is a deny-list of named routes, not an allow-list. The commit message policy covers REST JSON bodies only; it does not read pushed commits (add a GitHub-side check if you need that). A REST merge on a repo that is not a merge-gate repo lands on the PR's base branch without the broker looking it up, so keep GitHub branch protection on as the backstop. GraphQL is not proxied. Pushes need a `Content-Length` (git sends one when `http.postBuffer` is large enough; `hs-gh git-setup` sets it).

## Why

- Agents never see a GitHub token. A compromised or confused container can lose its broker token (revoke one line) but cannot leak the host's credential.
- One place enforces repo scope, protected branches and budget, instead of trusting every agent prompt to do it.
- Fit with Trinity: agent containers sit on the agent bridge (default `172.28.0.0/16`) and reach the host at `172.28.0.1`. The broker listens on that address and port. The firewall example in `../firewall` allows containers to reach only this port on the host, so the broker is the only outbound path to GitHub.

## How to enable

1. Install the files:
   ```
   sudo install -m 0755 ghbroker.py /usr/local/sbin/hs-ghbroker
   sudo install -m 0755 hs-gh /usr/local/sbin/hs-gh
   sudo install -d -m 0755 /etc/homeserver/ghbroker
   sudo install -m 0644 policy.example.json /etc/homeserver/ghbroker/policy.json
   sudo install -m 0644 ghbroker.service /etc/systemd/system/ghbroker.service
   ```
2. Put the host's GitHub token in a root-only file (a fine-grained token limited to the repos in your policy). Give it
   no Workflows permission (fine-grained) and no `workflow` scope (classic): then GitHub refuses any push made through
   the broker that adds or changes `.github/workflows/`, so an agent cannot add its own workflow on a merge-gate repo.
   That is not the whole stop: code an existing workflow runs from the branch (a test script, a Makefile, a local action) can post a required check name wherever the job token can write checks, so the stop is a signed lead review of the head plus job tokens without `checks: write` (or `check_app_ids` pinned to an app that is not GitHub Actions). The token's account must not be the review signer's account: the signer needs a dedicated
   account (see `../review-signer`). Put this token's login in the merge gate's `broker_login`.
   ```
   sudo install -m 0600 -o root -g root /dev/null /etc/homeserver/ghbroker/github-token
   sudoedit /etc/homeserver/ghbroker/github-token
   ```
3. Make one token per agent and record only its hash:
   ```
   T=$(openssl rand -hex 32)
   echo "$(printf %s "$T" | sha256sum | cut -d' ' -f1)  agent-a" | sudo tee -a /etc/homeserver/ghbroker/tokens
   sudo chmod 0644 /etc/homeserver/ghbroker/tokens
   ```
   Give `$T` to that agent as its token (file `/etc/homeserver/hs-gh/token` in its container, or env `HS_GH_TOKEN`). See `tokens.example`. The file holds hashes only; the unit runs as a dynamic user, so it must be readable.
4. Edit `/etc/homeserver/ghbroker/policy.json` (see `policy.example.json`): agents, repos, `merge_gate_repos`, `merge_label`, `hold_label`, `protected_branches`, `hourly_budget`, `extra_message_patterns` (a list of regexes; empty or absent means no message check; a bad regex stops the broker from starting). `merge_gate_repos` is required (write `[]` when no repo is merge-gated); the broker refuses to start when it is missing, misspelled, has an entry that is not `OWNER/REPO` or names a repo no agent has, or when the policy has any unknown key. `merge_label` (default `automerge`) and `hold_label` (default `hold`) name the two labels the merge gate reads; set them to whatever your gate uses.
5. Check, then start:
   ```
   GHBROKER_TOKENS=/etc/homeserver/ghbroker/tokens GHBROKER_POLICY=/etc/homeserver/ghbroker/policy.json /usr/local/sbin/hs-ghbroker --check --gate-config /etc/homeserver/merge-gate/config.json
   sudo systemctl daemon-reload && sudo systemctl enable --now ghbroker
   curl -s http://172.28.0.1:8790/healthz
   ```
   `/healthz` answers 503 (`"ok": false`) while no policy is in force (a policy edit that does not load).
   To rehearse first, run `hs-ghbroker --dry-run` by hand: it decides and logs but never calls GitHub.
6. In a container: `hs-gh api GET /repos/OWNER/REPO/pulls`, or `hs-gh git-setup OWNER/REPO` to clone through the broker. The clone's own `.git/config` holds the redirect and the broker token; nothing global changes.
7. Run `python3 test_ghbroker.py` after any change to the code.

The policy file is re-read when it changes. If the new file does not load (bad JSON, a key the broker refuses, a
`merge_gate_repos` entry no agent has), the broker does not keep the old policy, which may be looser than the edit:
it answers every call with 503 and logs the full error once, until a valid file loads. To take away an agent's last
access to a merge-gate repo, remove the repo from `merge_gate_repos` in the same edit (or give it to another agent).
Run `--check` on the edited file first. Token changes need `systemctl restart ghbroker`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GHBROKER_BIND` | `127.0.0.1` | Listen address (the unit sets `172.28.0.1`) |
| `GHBROKER_PORT` | `8790` | Listen port |
| `GHBROKER_TOKENS` | `/etc/homeserver/ghbroker/tokens` | `<sha256>  <agent>` lines |
| `GHBROKER_POLICY` | `/etc/homeserver/ghbroker/policy.json` | Policy JSON |
| `GHBROKER_TOKEN_FILE` | `/etc/homeserver/ghbroker/github-token` | Host GitHub token (root-only; the unit passes it as a systemd credential) |
| `GHBROKER_DISABLE` | `/etc/homeserver/ghbroker.disable` | If this file exists the broker answers 503 and the unit will not start |
| `GHBROKER_API_BASE` / `GHBROKER_GIT_BASE` | `https://api.github.com` / `https://github.com` | Upstreams (change for tests or GitHub Enterprise) |
| `GHBROKER_MAX_BODY` | 268435456 | Largest request body in bytes |

Client (`hs-gh`): `HS_GH_BROKER` (default `http://172.28.0.1:8790`), `HS_GH_TOKEN` or `HS_GH_TOKEN_FILE`, `HS_GH_DRY_RUN=1` or `--dry-run`.

## Undo

```
sudo systemctl disable --now ghbroker
sudo rm /etc/systemd/system/ghbroker.service /usr/local/sbin/hs-ghbroker /usr/local/sbin/hs-gh
sudo rm -r /etc/homeserver/ghbroker
```
To pause without removing: `sudo touch /etc/homeserver/ghbroker.disable` (remove the file and restart to resume). To cut one agent off, delete its line in the tokens file and restart. Also remove the firewall rule that allows containers to reach the broker port.
