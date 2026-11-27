# capguard

## What it does

Compares the fleet's spend over a rolling 7 days with a weekly budget you configure.

- At or above `pause_pct`: disables every enabled schedule (`POST /api/agents/{name}/schedules/{id}/disable`) of every agent not in `never_pause`, and records each one in `/var/lib/homeserver/capguard/state.json`.
- At or below `restore_pct`: re-enables only the schedules in that record.
- Between the two: nothing, so it does not flap.

It never re-enables a schedule it did not pause. If a person already re-enabled one, or it
was deleted, the entry is dropped. A pause or restore call that fails is not recorded as
done and is retried next run.

Where the numbers come from: Trinity stores a `cost` on each execution. The guard reads
`GET /api/executions/stats?hours=168` (fleet total) and the same per agent
(`routers/executions.py:64`) and logs the top five agents. `168` is the longest "week" the
endpoint accepts, so this is a rolling 7 days, not a calendar week. `GET /api/ops/costs`
needs an OpenTelemetry collector and gives a daily figure, so it is not used. If your
install records no cost, readings are 0 and nothing is ever paused. Trinity also reports
subscription rate-limit pressure (`GET /api/agents/subscription-pressure`,
`routers/agents.py:335`); this concept does not use it, but it is a natural second input.

## Why

A runaway schedule can spend a week's budget in a day. Pausing the optional schedules
early, keeping the essential agents running, and putting everything back by itself when
usage falls turns a surprise into a log line.

## How to enable

1. `sudo install -m 0755 hs-capguard /usr/local/sbin/hs-capguard`
2. `sudo install -d /etc/homeserver/capguard && sudo install -m 0644 config.example.json /etc/homeserver/capguard/config.json`
3. Set `weekly_budget_usd` and list the agents that must never be paused in `never_pause`.
4. `sudo install -m 0600 /dev/stdin /etc/homeserver/capguard/token <<< "PASTE_ADMIN_KEY_HERE"` (pause and resume need an owner or admin key; reading needs only read access).
5. `sudo install -m 0644 hs-capguard.service hs-capguard.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now hs-capguard.timer`
6. Read `journalctl -u hs-capguard -n 30`. Lines starting `DRY-RUN would pause` show what would happen.
7. Set `"act": true` when satisfied.

## Configuration

| Key (config.json) | Default | Meaning |
|---|---|---|
| `act` | `false` | `false` = dry-run |
| `weekly_budget_usd` | `0` | Required, must be > 0 |
| `warn_pct` | `70` | Logged only |
| `pause_pct` | `90` | At or above: pause |
| `restore_pct` | `75` | At or below: restore; must be below `pause_pct` |
| `never_pause` | `[]` | Agents whose schedules are never touched |

Environment: `TRINITY_URL`, `TRINITY_TOKEN_FILE`, `HS_CAPGUARD_DIR`, `HS_CAPGUARD_STATE_DIR`, `HS_CAPGUARD_KILL`.

## Undo

- Stop the guard: `sudo touch /etc/homeserver/capguard.disable`, or `sudo systemctl disable --now hs-capguard.timer`.
- Put paused schedules back by hand: the `paused` map in `/var/lib/homeserver/capguard/state.json` lists each schedule id and agent; call `POST /api/agents/{agent}/schedules/{id}/enable` for each, then delete the file. (If the guard stays enabled and usage is under `restore_pct`, it does this itself.)
