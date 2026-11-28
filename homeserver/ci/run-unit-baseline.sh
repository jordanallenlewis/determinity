#!/usr/bin/env bash
# Runs the backend unit suite and checks its failures against upstream's
# tests/unit_failure_baseline.txt plus Determinity's own
# homeserver/ci/unit-failure-baseline.txt. Exit 0 = no failure outside the two
# files. Used by the release workflow's `unit` job and by hq-unit-baseline.yml
# on pull requests into `hq`, so both run the same suite the same way.
#
# Usage: run-unit-baseline.sh [summary-file]   (summary is appended, e.g. $GITHUB_STEP_SUMMARY)
# Needs the packages in tests/requirements-test.txt plus pytest-randomly and pytest-xdist.
set -euo pipefail
cd "$(dirname "$0")/../.."
summary=${1:-}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

python scripts/ci/check-failure-baseline.py --self-test

# Same selection and flags as the `absolute` job in backend-unit-test.yml. A
# failing suite is not an error here: the comparison below decides.
(cd tests && python -m pytest unit/ -m "not slow" \
  --timeout=300 --timeout-method=signal \
  -n logical --dist loadfile \
  -p randomly --randomly-seed=12345 \
  --junit-xml="$work/junit.xml" \
  --tb=no -q) || true

# echo between them: a last line without a newline must not run into the next file.
{ cat tests/unit_failure_baseline.txt; echo; cat homeserver/ci/unit-failure-baseline.txt; } > "$work/baseline.txt"
args=(--xml "$work/junit.xml" --baseline "$work/baseline.txt")
if [ -n "$summary" ]; then args+=(--out "$summary"); fi
python scripts/ci/check-failure-baseline.py "${args[@]}"
