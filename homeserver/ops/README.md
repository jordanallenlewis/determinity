# ops: boring babysitters for a fleet of agents

Three small, deterministic scripts that watch a Trinity instance through its REST API.
They are concept scripts: short enough to read in one sitting, generic, and meant as a
starting point you adapt. Python 3.11+ standard library only.

| Piece | Folder | Job | Timer |
|---|---|---|---|
| caretaker | `caretaker/` | Health loop: stopped agents, stuck executions, failed schedules | 5 min |
| ops-desk | `ops-desk/` | Triage of the operator queue by simple rules | 15 min |
| capguard | `capguard/` | Pauses non-essential schedules when usage nears a weekly budget | 10 min |

## Why

A fleet of agents produces routine trouble all day: an agent that stopped, a run that hung,
an alert nobody needs, a bill creeping toward a limit. Asking a model to babysit that costs
money, gives different answers on different days, and can fail in the same way as the
thing it watches. These scripts use no model. The same input gives the same output, every
action is on a short allowlist and is reversible, and every decision is written down.
Humans then see only what needs them: findings the scripts cannot safely handle, and
queue items that need a person's judgement.

## Shared rules

- Dry-run is the default. Nothing changes until the config file says `"act": true`.
  `--dry-run` on the command line forces it off again.
- Fail closed. A missing token, an unreachable API, or a bad config exits non-zero and
  changes nothing.
- Kill switch. `touch /etc/homeserver/<piece>.disable` makes a run a no-op, in both the
  systemd unit (`ConditionPathExists=!`) and the script.
- The token is read from a file (`TRINITY_TOKEN_FILE`), never from the command line. Under
  systemd it is a root-only file passed in with `LoadCredential=`.
- Only real endpoints are used; each script cites the router file and line. Where Trinity
  has no endpoint for something, the README of that piece says so.
- Each piece logs to stdout (journald) and keeps its state under
  `/var/lib/homeserver/<piece>/`.

## Common settings

| Variable | Default | Meaning |
|---|---|---|
| `TRINITY_URL` | `http://127.0.0.1:8000` | Base URL of the Trinity backend |
| `TRINITY_TOKEN_FILE` | `$CREDENTIALS_DIRECTORY/trinity-token`, else `/etc/homeserver/<piece>/token` | File holding the API key |

## Tests

Each folder has an offline `test_*.py` (no network, no Trinity needed):

    python3 caretaker/test_caretaker.py
    python3 ops-desk/test_ops_desk.py
    python3 capguard/test_capguard.py

## Status

Written against the Trinity source tree in `src/backend/routers/`; not yet run against a
live instance. Run each in dry-run first and read the log.
