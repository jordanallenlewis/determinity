# review-signer

## What it does

A tiny root-owned program behind a systemd socket. A local user (one account per reviewer role) connects and sends one JSON line: `{repo, pr, sha, role, verdict, findings_high}` (optional `findings_total`). The signer works out who is calling from the socket itself, checks that this caller may sign this role with this verdict (`roles.example.json`), then posts a GitHub commit status with context `review/<role>` on the sha. The description is neutral and counts only, for example "2 findings, none blocking". It never names who reviewed. An `error` verdict posts state `error`, never success.

## Why

The reviewers run unprivileged and must not hold a GitHub token. The signer is the only process that holds one for this purpose. The caller's identity comes only from the socket peer: the signer reads the connecting process's uid with `SO_PEERCRED` and maps it to a user name with the password database. `roles.json` is keyed by those user names, so a caller can sign only the roles listed for its own account. A `caller` field in the request is not used: if one is present and differs from the socket peer the request is refused. The socket's `0660` mode and group limit who can connect at all. A merge gate can then require `review/<role>` statuses posted by the signer's GitHub account (its `signer_login` setting) without trusting free text.

The context names line up with the merge gate: role `lead` posts `review/lead` and role `principal` posts `review/principal`, which are the gate's `review_lead` and `review_principal` defaults. `test_signer.py` checks that the example roles file produces exactly the contexts the example gate config requires.

What this does not protect against: root, and anyone who can read the token file, can post any status; a local user listed in `roles.json` can sign whatever its entry allows, and anyone who can run code as that user is that caller.

## How to enable

1. Create the config and install the files (each reviewer role runs as its own local user; `reviewer-lead` and `reviewer-principal` here, and a group `review-signer-clients` holds them):
   ```
   sudo install -m 0755 review-signer /usr/local/sbin/hs-review-signer
   sudo install -d -m 0755 /etc/homeserver/review-signer
   sudo install -m 0644 roles.example.json /etc/homeserver/review-signer/roles.json
   sudo install -m 0600 -o root -g root /dev/null /etc/homeserver/review-signer/github-token
   sudoedit /etc/homeserver/review-signer/github-token
   sudo install -m 0644 review-signer.socket review-signer@.service /etc/systemd/system/
   ```
   The token needs permission to write commit statuses on the repos concerned. It must belong to a dedicated GitHub account that no other token on the host belongs to: not the owner's, not the broker's (the account agents' writes are made as), not the merge gate's. The merge gate counts a `review/*` status by the login that posted it, so a shared account would let an agent's own status count as a review.
2. Edit `roles.json`: the keys are local user names, each mapped to the roles (`lead`, `principal`) it may sign and the verdicts allowed. Create the group named in `SocketGroup=` in `review-signer.socket` (`review-signer-clients`) and add those users to it. Put the GitHub login that owns the token in the merge gate's `signer_login`.
3. Check the logic offline with `python3 test_signer.py`. To rehearse under systemd, add `--dry-run` to `ExecStart` in `review-signer@.service`, send a request, read the printed API call in the reply, then remove the flag. (Run by hand there is no socket peer, so the signer answers "cannot identify the caller" and fails closed.) Dry-run still uses the real socket peer.
4. Start it:
   ```
   sudo systemctl daemon-reload && sudo systemctl enable --now review-signer.socket
   ```
5. As one of the listed users, connect to `/run/review-signer.sock`, write one JSON line, read one JSON line back.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SIGNER_ROLES` | `/etc/homeserver/review-signer/roles.json` | local user name -> role -> allowed verdicts |
| `SIGNER_TOKEN_FILE` | `/etc/homeserver/review-signer/github-token` | Root-only GitHub token |
| `SIGNER_API_BASE` | `https://api.github.com` | API base |
| `SIGNER_DISABLE` | `/etc/homeserver/review-signer.disable` | If present, signing is refused and the socket will not start |

Verdicts: `clean`, `findings`, `error` (review roles), and `pass`, `fail` if you add a verification role. A request is one line of at most 4 KB.

## Undo

```
sudo systemctl disable --now review-signer.socket
sudo rm /etc/systemd/system/review-signer.socket /etc/systemd/system/review-signer@.service /usr/local/sbin/hs-review-signer
sudo rm -r /etc/homeserver/review-signer
```
To pause: `sudo touch /etc/homeserver/review-signer.disable`. Statuses already posted stay on GitHub; a later status for the same context replaces them.
