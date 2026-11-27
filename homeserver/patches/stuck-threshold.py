#!/usr/bin/env python3
"""Make Trinity's business health check honour MonitoringConfig.stuck_execution_threshold.

Upstream (src/backend/services/monitoring_service.py) flags an execution as stuck
after a hardcoded 30 minutes and never reads the configured threshold. This script
patches the file in place when the backend container starts.

Rules:
  - all or nothing: three snippets are replaced, each must occur exactly once,
    otherwise nothing is written;
  - idempotent: a second run reports "already applied";
  - safe: if upstream changed, it prints a message and exits 0 without touching the file;
  - the patched source must compile before it is written.

Environment:
  STUCK_PATCH_TARGET   file to patch (default /app/services/monitoring_service.py)
"""
import os
import sys

TAG = "stuck-threshold patch:"
TARGET = os.environ.get("STUCK_PATCH_TARGET", "/app/services/monitoring_service.py")

# (old, new) pairs. Each `old` must occur exactly once in the target.
REPLACEMENTS = [
    # 1. new parameter on the check function
    ("    timeout: float = 10.0\n) -> BusinessHealthCheck:",
     "    timeout: float = 10.0,\n    stuck_threshold_s: int = 1800\n) -> BusinessHealthCheck:"),
    # 2. use it instead of the literal 30 minutes
    ("stuck_threshold = datetime.utcnow() - timedelta(minutes=30)",
     "stuck_threshold = datetime.utcnow() - timedelta(seconds=stuck_threshold_s)"),
    # 3. pass the configured value at the call site
    ("check_business_health(agent_name, config.http_timeout)",
     "check_business_health(agent_name, config.http_timeout, "
     "stuck_threshold_s=config.stuck_execution_threshold)"),
]


def main() -> int:
    if len(sys.argv) > 1:
        print(__doc__)
        return 0
    try:
        with open(TARGET, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        print(f"{TAG} cannot read {TARGET}: {exc}; skipped")
        return 0
    if "stuck_threshold_s" in src:
        print(f"{TAG} already applied")
        return 0
    bad = [old.splitlines()[0] for old, _ in REPLACEMENTS if src.count(old) != 1]
    if bad:
        print(f"{TAG} upstream code changed, NOT applied (no match for: {bad[0]!r}); "
              "check whether upstream fixed it and remove this patch")
        return 0
    for old, new in REPLACEMENTS:
        src = src.replace(old, new)
    try:
        compile(src, TARGET, "exec")
    except SyntaxError as exc:
        print(f"{TAG} patched source does not compile ({exc}), NOT applied")
        return 0
    with open(TARGET, "w", encoding="utf-8") as fh:
        fh.write(src)
    print(f"{TAG} applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
