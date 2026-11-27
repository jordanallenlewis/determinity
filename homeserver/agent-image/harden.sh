#!/bin/bash
# harden.sh IMAGE:TAG
# Rebuilds IMAGE:TAG as the hardening overlay on top of itself. The unmodified
# upstream build is kept as IMAGE:upstream. A self-test runs before the result is
# kept; on failure the tag is rolled back. Already-hardened tags are left alone.
#
#   BASE_IMAGE_NAME  repository name used for the :upstream tag (default: from TAG)
#   DRY_RUN=1        print the steps, change nothing
#
# Undo: docker tag IMAGE:upstream IMAGE:latest, then recreate the agents.
set -euo pipefail

case "${1:-}" in
  -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") echo "usage: harden.sh IMAGE:TAG (see --help)" >&2; exit 2 ;;
esac

TAG=$1
IMG=${BASE_IMAGE_NAME:-${TAG%:*}}
DIR=$(cd "$(dirname "$0")" && pwd)
run() { if [ "${DRY_RUN:-0}" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

if [ "$(docker image inspect -f '{{index .Config.Labels "homeserver.hardened"}}' "$TAG" 2>/dev/null || true)" != "" ]; then
  echo "harden: $TAG already hardened"; exit 0
fi
if [ "${DRY_RUN:-0}" != 1 ]; then
  docker image inspect "$TAG" >/dev/null 2>&1 || { echo "harden: image $TAG not found" >&2; exit 1; }
fi

run docker image tag "$TAG" "$IMG:upstream"
if ! run nice -n 19 ionice -c3 docker build -q --build-arg "BASE=$IMG:upstream" -t "$TAG" "$DIR" >/dev/null; then
  docker image tag "$IMG:upstream" "$TAG"
  echo "harden: build FAILED, $TAG left as upstream" >&2
  exit 1
fi
[ "${DRY_RUN:-0}" = 1 ] && exit 0

# Self-test: no blanket sudo, startup hooks still allowed, no SETENV (a variable on the sudo
# command line, LD_PRELOAD included, is refused), no sudo -E left, /app root-owned,
# not in the sudo group, packages present.
if ! docker run --rm --entrypoint sh "$TAG" -c '
  set -e
  # `! cmd` does not trip set -e, so every "must be refused" check uses refuse(): the command must fail AND
  # say why in words matching the pattern, so a hook that ran and then failed does not pass as refused.
  # absent() is for "must not match" greps, which fail silently.
  refuse() { re=$1; shift; if out=$("$@" 2>&1); then echo "self-test: allowed but must be refused: $*"; exit 1; fi
    printf "%s\n" "$out" | grep -Eqi -- "$re" || { echo "self-test: failed for another reason than a refusal: $*: $out"; exit 1; }; }
  absent() { if "$@" >/dev/null 2>&1; then echo "self-test: must not match: $*"; exit 1; fi; }
  NOPW="a password is required|not allowed to (execute|run)"
  refuse "$NOPW" sudo -n true
  refuse "$NOPW" sudo -n apt-get --version
  sudo -n -l | grep -q "hs-root-hook runtime-config"
  sudo -n -l | grep -q "hs-root-hook routing-env"
  sudo -n -l | grep -q "/usr/sbin/sshd -D"
  absent sh -c "sudo -n -l | grep -q SETENV"
  refuse "not allowed to set the following environment variables" sudo -n LD_PRELOAD=/nonexistent.so /usr/local/sbin/hs-root-hook routing-env
  refuse "not allowed to set the following environment variables" sudo -n AGENT_GUARDRAILS={} /usr/local/sbin/hs-root-hook runtime-config
  refuse "not allowed to preserve the environment" sudo -n -E /usr/local/sbin/hs-root-hook routing-env
  absent grep -q "sudo -E" /app/startup.sh
  [ "$(stat -c %U /app/startup.sh)" = root ]
  sudo -n /usr/local/sbin/hs-root-hook runtime-config
  refuse "already ran for this container start" sudo -n /usr/local/sbin/hs-root-hook runtime-config
  absent sh -c "id -nG | grep -qw sudo"
  for b in rg jq pdftotext pandoc dig; do command -v "$b" >/dev/null || { echo "missing $b"; exit 1; }; done
'; then
  docker image tag "$IMG:upstream" "$TAG"
  echo "harden: self-test FAILED, $TAG rolled back to upstream" >&2
  exit 1
fi
echo "harden: $TAG hardened ($(docker image inspect -f '{{.Id}}' "$TAG" | cut -c8-19))"
