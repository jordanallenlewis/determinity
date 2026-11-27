# patches: stuck-threshold

## What it does

`stuck-threshold.py` patches `services/monitoring_service.py` inside the backend container when it starts, so the business health check uses `MonitoringConfig.stuck_execution_threshold` instead of a fixed 30 minutes.

Three replacements, each of which must match exactly once (line numbers are from `src/backend/services/monitoring_service.py` in this checkout):

| Line | Snippet | Change |
|---|---|---|
| 428-431 | `timeout: float = 10.0` + `) -> BusinessHealthCheck:` in `check_business_health` | adds a `stuck_threshold_s: int = 1800` parameter |
| 496 | `stuck_threshold = datetime.utcnow() - timedelta(minutes=30)` | uses `timedelta(seconds=stuck_threshold_s)` |
| 678 | `check_business_health(agent_name, config.http_timeout)` | passes `stuck_threshold_s=config.stuck_execution_threshold` |

The short `timeout: float = 10.0` text also occurs at line 230, so the first snippet includes the following `) -> BusinessHealthCheck:` line; the full text occurs once. The config field is declared at `src/backend/db_models.py:1374`.

The patch is all or nothing, checks that the result compiles, and is idempotent. If upstream code changed, it prints "upstream code changed, NOT applied" and leaves the file alone.

## Why

Long agent runs (for example a 60-minute execution timeout) are flagged "stuck" at 30 minutes, even when the operator raised `stuck_execution_threshold`.

## How to enable

1. Copy `stuck-threshold.py` to `$TRINITY_DIR/patches/`.
2. Use `../compose/docker-compose.box.yml`, which mounts that directory read-only at `/patches` and runs the patch before the backend command.
3. `hs-trinity up -d backend`, then `hs-trinity logs backend | grep "stuck-threshold patch"`. Expect `applied`.

The patched file lives in the container layer, so the patch runs again on every container recreate; a second run in the same container prints `already applied`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `STUCK_PATCH_TARGET` | `/app/services/monitoring_service.py` | file to patch (set for offline tests) |

## Undo

Once upstream honours the setting (the log says "upstream code changed, NOT applied" and the code at the lines above reads the config), delete the `volumes` and `entrypoint` lines for the backend in `docker-compose.box.yml`, remove `patches/stuck-threshold.py`, and run `hs-trinity up -d backend`. Recreating the container also reverts a patch that was applied.

## Test

Run against a copy of the upstream file, not the live one:
```
T=$(mktemp -d); cp src/backend/services/monitoring_service.py $T/m.py
STUCK_PATCH_TARGET=$T/m.py python3 stuck-threshold.py   # applied
STUCK_PATCH_TARGET=$T/m.py python3 stuck-threshold.py   # already applied
python3 -m py_compile $T/m.py
```
