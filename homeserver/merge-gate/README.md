# merge-gate

## What it does

A host-side merger, run by a systemd timer every minute. For each configured repo it merges an open PR only when all of
these hold on the PR's current head SHA:

- the PR is not a draft, has the merge label (default `automerge`), has no `hold` label, targets the configured base,
  and its head branch starts with an allowed prefix (for example `agent-a/`, `docs/`);
- every commit has author and committer email in an allowlist (the owner's noreply address), and every commit
  message, the PR title and the PR body pass the commit message policy (the regexes you list in `extra_message_patterns`;
  none by default). Any miss means no merge and one commit status `merge-gate` = failure with a description such as
  `commit message policy failed: 2 commits`;
- the required checks are green (an empty list means every reported check is green and at least one exists). A check
  is green only through a check run: a commit status can be posted by any token with write access, so a status can make
  a check worse (a failing or pending status blocks) but can never make it green or stand in for a missing run. CI must
  therefore report through check runs (GitHub Actions does). When a check run and a status share a name, the worse of
  the two counts. Worst to best: failure, error, cancelled, timed out, action required, stale
  or anything unknown; then pending, queued, in progress or missing; then skipped or neutral; then success. A required
  check must be literally `success`, so a skipped required check blocks the merge. `check_app_ids` pins a check name
  to the GitHub App that must have produced it. That helps only when CI is an app agents cannot run code as. When CI is
  GitHub Actions, a workflow an agent pushes on any branch runs as the same app and, with `checks: write`, can post a
  passing check run under a required name on the PR's head. The broker's GitHub token must not have the Workflows
  permission (fine-grained) or the `workflow` scope (classic), so GitHub refuses any push made through the broker that
  adds or changes a file under `.github/workflows/`. That is not the whole stop: code an existing workflow runs from the branch (a test script, a Makefile, a local action) can post a required check name wherever the job token can write checks, so the stop is a signed lead review of the head plus job tokens without `checks: write` (or `check_app_ids` pinned to an app that is not GitHub Actions). A forged check
  cannot stand in for the signed review;
- review statuses for the risk tier are `success` on that SHA, and were posted by the signer's account (`signer_login`).
  The tier comes from path globs: `high_paths` is high, a change touching only `low_paths` is low, anything else is
  medium. Low and medium need `review/lead`; high needs `review/lead` and `review/principal`; medium also has a
  changed-lines cap, above which a human merges. A `review/*` status from any other account is ignored, as if it had
  not been posted, and so is a check run with a review name. The newest status from the signer account for each
  context is the one that counts. If `signer_login` is not configured, or is the gate's own account or `broker_login`,
  or `broker_login` is empty, the gate merges nothing and posts a `merge-gate` failure status that says so.

It then merges with the configured method (default squash), using the PR title as the commit title and the PR body as the
message, pinned to the checked SHA so a push after the check cannot slip in. State is kept so a merged or refused SHA is
never retried in a loop. Nothing in it calls a model.

## Why

- Green PRs merge on policy, not on a reviewer's whim or availability. The gate does the merging; a review result only
  counts when the signer's account posted it.
- The gate is deterministic and small enough to read in one sitting. Every refusal has a logged reason.
- Author identity is checked on every commit of a pull request the gate merges.

## How to enable

1. Install the script: `sudo install -m 0755 merge-gate /usr/local/sbin/hs-merge-gate`
2. Install the units: `sudo install -m 0644 merge-gate.service merge-gate.timer /etc/systemd/system/`
3. Write the config: `sudo install -d /etc/homeserver/merge-gate && sudo install -m 0644 config.example.json /etc/homeserver/merge-gate/config.json`, then edit repos, prefixes, owner_emails and the check names.
4. Put the gate's token in a root-only file: the owner's token, or a fine-grained token with PR, commit status and
   contents write on the listed repos, so merges are made as the owner. It must NOT belong to the signer's account (the
   gate reads its own login with `GET /user` at start, refuses to run if it cannot, and refuses every merge if it
   equals `signer_login`). A GitHub App installation token cannot call `GET /user`, so use a user's token:
   `sudo install -m 0600 /dev/null /etc/homeserver/merge-gate/github-token` and write the token into it.
5. Try it without side effects: `sudo MERGE_GATE_TOKEN_FILE=/etc/homeserver/merge-gate/github-token hs-merge-gate --dry-run`
6. Enable: `sudo systemctl daemon-reload && sudo systemctl enable --now merge-gate.timer`
7. Run the offline tests any time: `python3 test_merge_gate.py`

The review statuses (`review/lead`, `review/principal`) are posted by the review signer (see `../review-signer`). The
signer needs its OWN GitHub account, a dedicated one that no other token on the host belongs to: not the owner's
account, not the account of the broker's token (the one agents' writes are made as), not the gate's. Put that account's
login in `signer_login` and the broker token's login in `broker_login`; the gate refuses to start when they are equal.
`broker_login` is required: left empty, the gate merges nothing and posts a `merge-gate` failure status that names it.
Write `-` when no broker runs on this host.
On a one-account setup every status an agent posts through the broker carries the owner's login, so a `signer_login`
equal to the owner's would count an agent's own status as a review. The CI checks come from CI as check runs. This
gate only reads them. Keep branch protection on for the default branch, and require the `merge-gate` status there too, so
a failed author or commit message check also blocks a manual merge.

Context names must match end to end: the signer posts `review/<role>` for the roles in its `roles.json`, and the gate
requires `review_lead` and `review_principal` (defaults `review/lead` and `review/principal`). The example files in
both folders use the same names; a test in `../review-signer` checks that.

## Configuration

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `MERGE_GATE_CONFIG` | `/etc/homeserver/merge-gate/config.json` | config file |
| `MERGE_GATE_TOKEN_FILE` | `/etc/homeserver/merge-gate/github-token` | root-only file holding the token |
| `MERGE_GATE_STATE` | `/var/lib/homeserver/merge-gate/state.json` | merged/refused SHA memory |
| `MERGE_GATE_DISABLE_FILE` | `/etc/homeserver/merge-gate.disable` | kill switch path |
| `MERGE_GATE_API` | `https://api.github.com` | API base (GitHub Enterprise, tests) |

Config keys (see `config.example.json`): `repos`, `base`, `merge_label`, `hold_label`, `prefixes`, `merge_method`
(`squash`, `merge` or `rebase`), `required_checks`, `ok_conclusions` (accepted results when `required_checks` is empty),
`high_paths`, `low_paths`, `medium_max_lines`, `review_lead`, `review_principal`, `signer_login` (required, a dedicated account), `broker_login` (required: the broker token's login, or `-` when no broker runs on this host), `check_app_ids` (`{check name: app id}`; recommended in production), `owner_emails`, `extra_message_patterns` (regexes, case-insensitive and multi-line; empty means only the email check runs; a bad regex is a config error),
`interval` (only for a non-`--once` loop). Empty `repos`, `prefixes` or `owner_emails` is a config error and the gate
does nothing. Globs: `**` crosses directories, `*` does not, and a pattern without `/` (such as `CODEOWNERS`) matches
that name at any depth.

Flags: `--once` single pass (what the timer runs), `--dry-run` print decisions and change nothing, `--config PATH`.

## What the guarantees are, and are not

What the gate enforces: a `review/*` status counts only from `signer_login`, which must be a dedicated account; any
other check is green only through a check run, and a status can only make it worse;
the merge call is pinned to the checked SHA; and anything missing, unknown or erroring means no merge.

Defence in depth, outside this program: the GitHub broker in front of agent credentials refuses commit-status and
check writes at any path (`statuses/<sha>` and `commits/<sha>/statuses` alike), label writes (merge label, hold label),
base changes, `update-branch`, and deleting workflow runs on repos the gate covers. Run `hs-ghbroker --check
--gate-config <this config>` so the two agree on repos and labels. The gate does not rely on that, but it
narrows who can mislead it.

What it does not guard against:

- Anyone holding the signer's token, or the gate's own token, can post a status or merge as that account. Keep both
  token files root-only.
- When CI is GitHub Actions, a workflow file pushed on any branch can post check runs under any name (see
  `check_app_ids` above). The gate cannot tell those from real CI. The broker token's missing Workflows permission keeps
  agents from pushing a workflow file, but code an existing workflow runs from the branch (a test script, a Makefile, a local action) can post a required check name wherever the job token can write checks, so the stop is a signed lead review of the head plus job tokens without `checks: write` (or `check_app_ids` pinned to an app that is not GitHub Actions). Anyone who can push workflows another way (the owner, an admin) can do it.
- GitHub repository admins and owners can post statuses as themselves, merge in the web UI and edit branch protection.
  If branch protection is off, a web-UI merge bypasses the gate completely. Keep it on.
- Commit author and committer emails are self-declared strings in the commit object, checked against an allowlist. That
  is a consistency check, not a signature check: anyone can write any email into a commit. Require signed commits in
  branch protection if you need proof of who authored a change.
- It cannot tell whether a review was any good; it only reads the signer's verdict.
- The label that requests an automerge is set by whoever can label PRs; restrict that in the repo's permissions.

Notes: the commit message policy reads commit messages, PR titles and PR bodies only, never file contents. Web-UI merge commits are committed by GitHub's own address; add it to
`owner_emails` only if you accept that. Changing a PR title or body re-runs the check even on the same SHA.

## Undo

- Pause: `sudo touch /etc/homeserver/merge-gate.disable` (remove it to resume).
- Remove: `sudo systemctl disable --now merge-gate.timer`, then delete the two units, `/usr/local/sbin/hs-merge-gate`,
  `/etc/homeserver/merge-gate/` and `/var/lib/homeserver/merge-gate/`.
- A merge is a normal git merge; revert it with a PR like any other. A stuck `merge-gate` failure status clears on the
  next push to the PR branch (new SHA).
