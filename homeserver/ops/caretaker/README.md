# caretaker

## What it does

A no-LLM health loop. Every run it:

1. lists agents (`GET /api/ops/fleet/status`) and finds those on your `should_run` list that are not running;
2. lists executions with status `running` (`GET /api/executions`) and flags any older than `stuck_minutes`;
3. lists schedules (`GET /api/ops/schedules`) and flags enabled ones whose last execution failed;
4. starts a stopped agent (`POST /api/agents/{name}/start`), at most `max_restarts_per_hour` per rolling hour. This is the only action it takes;
5. writes everything else as a finding to `/var/lib/homeserver/caretaker/state.json` (first seen, last seen, detail). Findings that stop being true are removed.

With `"notify": true` and `"act": true`, each new finding is also raised once as a Trinity
notification (`POST /api/notifications`, type `alert`).

Not possible with the API as it is: an operator-side "create operator-queue item" call does
not exist (an agent raises its own asks, `routers/operator_queue.py:663`), and there is no
endpoint to cancel a running execution. So stuck executions are reported, never killed, and
findings go to notifications rather than the operator queue. If you add such an endpoint,
replace the `/api/notifications` call in `run()`.

## Why

An agent that stopped at 03:00 should be back by 03:05 without anyone being paged, and one
that keeps stopping should be seen by a person, not restarted forever. The hourly cap and
the allowlist are what make an automatic restart safe.

## How to enable

1. Install the script: `sudo install -m 0755 hs-caretaker /usr/local/sbin/hs-caretaker`
2. Create the config directory and copy the example:
   `sudo install -d /etc/homeserver/caretaker && sudo install -m 0644 config.example.json /etc/homeserver/caretaker/config.json`
3. Edit `should_run` to the agent names that must always run.
4. Store an API key (admin, or a key allowed to start agents and read ops data), readable by root only:
   `sudo install -m 0600 /dev/stdin /etc/homeserver/caretaker/token <<< "PASTE_KEY_HERE"`
5. Install and start the timer:
   `sudo install -m 0644 hs-caretaker.service hs-caretaker.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now hs-caretaker.timer`
6. Read the dry-run output for a day: `journalctl -u hs-caretaker -n 50` and `cat /var/lib/homeserver/caretaker/state.json`.
7. When the findings look right, set `"act": true` in the config. No restart is needed; the next run picks it up.

## Configuration

| Key (config.json) | Default | Meaning |
|---|---|---|
| `act` | `false` | `false` = dry-run, log only |
| `should_run` | `[]` | Agents that must be running. Only these are ever started |
| `ignore` | `[]` | Agents never reported or touched |
| `max_restarts_per_hour` | `3` | Rolling-hour cap across all agents |
| `stuck_minutes` | `120` | Age after which a running execution is stuck |
| `notify` | `false` | Raise a notification per new finding (needs `act`) |
| `notify_priority` | `high` | `low`, `normal`, `high` or `urgent` |

Environment: `TRINITY_URL`, `TRINITY_TOKEN_FILE`, `HS_CARETAKER_DIR`, `HS_CARETAKER_STATE_DIR`, `HS_CARETAKER_KILL`.

## Undo

- Stop it: `sudo touch /etc/homeserver/caretaker.disable` (remove the file to resume), or `sudo systemctl disable --now hs-caretaker.timer`.
- Remove it: delete the two unit files and `/usr/local/sbin/hs-caretaker`, then `sudo systemctl daemon-reload`.
- Its one action, starting an agent, is undone with `POST /api/agents/{name}/stop`. State lives in `/var/lib/homeserver/caretaker/` and can be deleted.
