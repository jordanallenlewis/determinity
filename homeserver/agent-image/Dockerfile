# Hardening overlay for Trinity's agent base image. Upstream's Dockerfile stays untouched.
# Built by harden.sh, which passes BASE=<name>:upstream (the unmodified upstream build).
#
#   1. bakes in packages agents would otherwise install with sudo at every boot;
#   2. removes blanket passwordless sudo and the sudo group membership, and allows
#      only the commands Trinity's startup.sh runs as root.
#
# The build FAILS if upstream changes the startup.sh lines or the sudo setup this relies on.
ARG BASE=trinity-agent-base:upstream
FROM ${BASE}
USER root
ENV DEBIAN_FRONTEND=noninteractive

# Example package list. Add the ones your agents need.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ripgrep jq poppler-utils pandoc dnsutils \
 && rm -rf /var/lib/apt/lists/*

# startup.sh runs two hooks through `sudo -E`. -E would let the caller's environment
# (LD_PRELOAD, AGENT_GUARDRAILS, proxy and endpoint names) reach a root process, so both
# calls are rewritten to the root-owned wrapper hs-root-hook, which takes the hooks' input
# from the container's own environment and runs each hook once per container start.
# Each grep -q fails the build when upstream's line is not what we expect.
COPY hs-root-hook /usr/local/sbin/hs-root-hook
RUN chown root:root /usr/local/sbin/hs-root-hook \
 && chmod 0755 /usr/local/sbin/hs-root-hook \
 && grep -q 'sudo -E /usr/bin/python3 /opt/trinity/hooks/write-runtime-config.py' /app/startup.sh \
 && grep -q 'sudo -E /usr/local/bin/python3 -I -S /opt/trinity/hooks/write-routing-env.py' /app/startup.sh \
 && sed -i 's#sudo -E /usr/bin/python3 /opt/trinity/hooks/write-runtime-config.py#sudo -n /usr/local/sbin/hs-root-hook runtime-config#' /app/startup.sh \
 && sed -i 's#sudo -E /usr/local/bin/python3 -I -S /opt/trinity/hooks/write-routing-env.py#sudo -n /usr/local/sbin/hs-root-hook routing-env#' /app/startup.sh \
 && grep -q 'sudo -n /usr/local/sbin/hs-root-hook runtime-config' /app/startup.sh \
 && grep -q 'sudo -n /usr/local/sbin/hs-root-hook routing-env' /app/startup.sh \
 && ! grep -q 'sudo -E' /app/startup.sh \
 && grep -q 'sudo mkdir -p /var/run/sshd' /app/startup.sh \
 && grep -q 'sudo chmod 0755 /var/run/sshd' /app/startup.sh \
 && grep -q 'sudo /usr/sbin/sshd -D' /app/startup.sh \
 && grep -q '^developer ALL=(ALL) NOPASSWD:ALL$' /etc/sudoers \
 && sed -i '/^developer ALL=(ALL) NOPASSWD:ALL$/d' /etc/sudoers \
 && ! grep -rq 'NOPASSWD:ALL' /etc/sudoers /etc/sudoers.d \
 && gpasswd -d developer sudo

# /app (startup.sh = PID 1, and the agent server) is developer-owned upstream. An agent that
# can rewrite startup.sh controls PID 1's environment at the next container start, which is
# what hs-root-hook trusts. Nothing writes under /app at run time, so make it root-owned.
RUN chown -R root:root /app \
 && chmod -R go-w /app \
 && [ "$(stat -c %U /app/startup.sh)" = root ]

COPY sudoers-agent /etc/sudoers.d/agent-image
RUN chown root:root /etc/sudoers.d/agent-image \
 && chmod 0440 /etc/sudoers.d/agent-image \
 && visudo -c

LABEL homeserver.hardened="1"
USER developer
