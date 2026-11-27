# ops-desk

## What it does

Triage of the operator queue without a model. Each run it pages through pending items
(`GET /api/operator-queue?status=pending`) and gives each one a verdict from the first
matching rule in `rules.json`:

| Verdict | Effect |
|---|---|
| `auto-resolve` | `action: "cancel"` (default) cancels the item with a reason (`POST .../cancel`), or `action: "respond"` answers with one option the agent itself offered (`POST .../respond`) |
| `route` | Moves the item with the admin readdress call (`POST .../readdress`, `to: "rule"` or `"admin"`) |
| `keep-for-human` | Nothing. The item stays |

Rules match on `type`, `priority`, `agent`, `title_regex` and `min_age_minutes`. No match
means `keep-for-human`. In code, and not only in config: any item of type `approval`, or
carrying a gate or proposal, is always `keep-for-human`, and a verdict is applied only if it
is in `allowed_verdicts` (default: only `auto-resolve`).

The one-line reason is sent to Trinity where the API takes one (cancel, readdress). Trinity
has no call to attach a note to an item, so for every item, kept ones included, the reason
is also appended to `/var/lib/homeserver/ops-desk/decisions.jsonl`.

Limit of the API: `respond`, `cancel` and `readdress` are refused for machine keys
(`reject_non_person_principal`, `routers/operator_queue.py:410` and `:538`), and `readdress`
needs an admin person (`:597`). The token therefore has to be a person's own API key.
Listing works with any key that can see the queue. Use a dedicated account for this and
keep the rules narrow.

## Why

Most of a queue is noise: expired low-priority alerts, routine notices, requests that always
go to the same place. A person should spend their attention on approvals and real decisions,
and see the rest only as a log line.

## How to enable

1. `sudo install -m 0755 hs-ops-desk /usr/local/sbin/hs-ops-desk`
2. `sudo install -d /etc/homeserver/ops-desk && sudo install -m 0644 config.example.json /etc/homeserver/ops-desk/config.json && sudo install -m 0644 rules.example.json /etc/homeserver/ops-desk/rules.json`
3. Edit `rules.json` for your queue. Start with one rule.
4. `sudo install -m 0600 /dev/stdin /etc/homeserver/ops-desk/token <<< "PASTE_PERSON_KEY_HERE"`
5. `sudo install -m 0644 hs-ops-desk.service hs-ops-desk.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now hs-ops-desk.timer`
6. Check `journalctl -u hs-ops-desk -n 50` and `/var/lib/homeserver/ops-desk/decisions.jsonl` (lines say `dry-run: would ...`).
7. Set `"act": true` in `config.json` when the decisions are what you would have done by hand.

## Configuration

| Key (config.json) | Default | Meaning |
|---|---|---|
| `act` | `false` | `false` = dry-run |
| `allowed_verdicts` | `["auto-resolve"]` | Subset of `auto-resolve`, `route` that may be applied |
| `max_actions_per_run` | `20` | Cap on changes per run |

Rules file: see `rules.example.json` (`name`, `match`, `verdict`, `action`, `to`, `response`, `reason`).
Environment: `TRINITY_URL`, `TRINITY_TOKEN_FILE`, `HS_OPSDESK_DIR`, `HS_OPSDESK_STATE_DIR`, `HS_OPSDESK_KILL`.

## Undo

- Stop: `sudo touch /etc/homeserver/ops-desk.disable`, or `sudo systemctl disable --now hs-ops-desk.timer`.
- A cancelled item is not reopened by Trinity; the agent receives the reason and can raise it again. `decisions.jsonl` lists every item that was touched.
- Remove: delete the unit files, `/usr/local/sbin/hs-ops-desk` and `/etc/homeserver/ops-desk/`.
