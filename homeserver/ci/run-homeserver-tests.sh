#!/usr/bin/env bash
# Runs every homeserver/ test suite (test_*.py), each from its own folder so it
# imports the module beside it. Fails if any suite fails or none is found.
# Used by .github/workflows/hq-homeserver-tests.yml and the release workflow.
set -euo pipefail
cd "$(dirname "$0")/.."
mapfile -t suites < <(find . -name 'test_*.py' -not -path '*/__pycache__/*' | sort)
[ "${#suites[@]}" -gt 0 ] || { echo "no homeserver test suites found" >&2; exit 1; }
failed=()
for t in "${suites[@]}"; do
  echo "== ${t#./}"
  if ! (cd "$(dirname "$t")" && PYTHONDONTWRITEBYTECODE=1 python3 "$(basename "$t")"); then
    failed+=("${t#./}")
  fi
done
if [ "${#failed[@]}" -gt 0 ]; then
  printf 'FAILED: %s\n' "${failed[@]}" >&2
  exit 1
fi
echo "all ${#suites[@]} homeserver suites passed"
