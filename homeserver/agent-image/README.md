# agent-image: hardened overlay for the agent base image

## Known issue

The self-test in `harden.sh` fails on a GitHub runner and would fail in a Trinity agent container. `hs-root-hook` runs as root and reads `/proc/1/environ`, but PID 1 runs as the agent user, and reading another user's process environment needs `CAP_SYS_PTRACE`, which Trinity does not grant. The hook refuses (as designed: "cannot read the container environment") and `harden.sh` rolls the tag back. Releases do not ship this image until that is fixed.

## What it does

`Dockerfile` builds on top of Trinity's agent base image (upstream's `docker/base-image/Dockerfile` is not modified) and:

- bakes in packages that agents would otherwise install with sudo at every boot. The list is an example (`ripgrep jq poppler-utils pandoc dnsutils`); edit it for your agents;
- removes blanket passwordless sudo and the `developer` user's `sudo` group membership;
- installs `sudoers-agent`, which allows only the exact privileged commands upstream `startup.sh` runs, with no `SETENV` tag and no `env_keep` (`Defaults:developer env_reset, !setenv`), then runs `visudo -c`. A variable on the sudo command line (`sudo LD_PRELOAD=... cmd`) and `sudo -E` are refused;
- rewrites the two `sudo -E <python> <hook>` lines in `/app/startup.sh` to `sudo -n /usr/local/sbin/hs-root-hook runtime-config` and `... routing-env`. `hs-root-hook` (root-owned) ignores the caller's environment, rebuilds the hooks' input (`AGENT_GUARDRAILS` and the endpoint, proxy and TLS-trust names) from `/proc/1/environ`, the environment the container was created with, drops `LD_*`, `PYTHON*` and shell start-up variables, and runs each hook at most once per container start. An agent can neither pass variables to a root process nor re-run a hook later to rewrite its root-owned output;
- makes `/app` root-owned. `startup.sh` is PID 1, and upstream leaves `/app` writable by `developer`, which would let an agent choose PID 1's environment at the next container start. Nothing writes under `/app` at run time;
- labels the image `homeserver.hardened`.

Each `sed` is guarded by `grep -q`, so the build fails if upstream changes those lines.

`harden.sh TAG` keeps the upstream build as `<name>:upstream`, builds the overlay (under `nice` and `ionice`), runs a self-test container and rolls the tag back on failure. It does nothing if `TAG` already carries the label.

## Why

An agent that can use sudo can disable every in-container guard (hooks, managed settings, root-owned policy files). With the overlay, the only root actions left are Trinity's own startup hooks, with fixed arguments.

## How to enable

1. Build the upstream base image as usual: `./scripts/deploy/build-base-image.sh`
2. `bash harden.sh trinity-agent-base:latest` (`DRY_RUN=1` prints the steps only). Expected last line: `harden: trinity-agent-base:latest hardened (<id>)`.
3. Recreate the agents so they start from the new image (stop and start each agent from the Trinity UI or API).

Re-run `harden.sh` after every rebuild of the upstream base image: a rebuild replaces the tag with an unhardened image. If upstream changed the `startup.sh` lines, the build fails and the tag stays unhardened; update the Dockerfile and `sudoers-agent` to match.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BASE_IMAGE_NAME` | the part of `TAG` before the colon | repository used for the `:upstream` tag |
| `DRY_RUN` | `0` | `1` prints the commands without running them |

## Undo

```
docker tag trinity-agent-base:upstream trinity-agent-base:latest
```
then recreate the agents.

## Validation status

`bash -n harden.sh` passes, and `--help` and `DRY_RUN=1` run. Release dry run 2 (run 46103227165) built the image on a GitHub runner: the Dockerfile and `visudo -c` passed, and the self-test failed as described under "Known issue", so `harden.sh` rolled the tag back. The image has not passed its self-test anywhere yet. Note that the packages in the self-test (`rg jq pdftotext pandoc dig`) must match the package list in the Dockerfile if you change it.
