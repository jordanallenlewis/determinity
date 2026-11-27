# compose: Trinity on a single home server

## What it does

`docker-compose.box.yml` is an override file for Trinity's pull-only compose file. It:

- runs Determinity's images for the backend, frontend, scheduler and MCP server, each set by a variable (`DETERMINITY_BACKEND_IMAGE` and so on) that `../release/hs-release` pins by digest in `release.env` (see `../RELEASES.md`); without `release.env` they follow `latest`;
- binds the backend, MCP server and Vector ports to `127.0.0.1` only (`!override`);
- sets example memory limits (backend 4g, postgres 2g, the rest 256-512m) and `cpu_shares` so the platform wins CPU contention against agent workloads;
- makes the backend and scheduler wait for a healthy postgres (`depends_on` with `service_healthy`);
- puts the OpenTelemetry collector behind an `otel` profile, and binds its ports to loopback;
- mounts `../patches` and runs `patches/stuck-threshold.py` before the backend starts (`python /patches/stuck-threshold.py || true; exec "$@"`).

`trinity` is a small wrapper that runs `docker compose -p trinity -f docker-compose.hosted.yml -f docker-compose.box.yml "$@"` from `$TRINITY_DIR`, adding `--env-file .env --env-file release.env` when `release.env` exists. A compose command run without `release.env` uses the `:latest` images, not the pinned ones: use the wrapper.

Base file: `docker-compose.hosted.yml`. It is the upstream file that uses published images, so nothing is built on the server; the override swaps in Determinity's. If you build images yourself, use `docker-compose.prod.yml` as the base instead. Do not stack the box file on `docker-compose.yml` (the dev file). Every service overridden here (`backend`, `frontend`, `redis`, `scheduler`, `mcp-server`, `vector`, `otel-collector`, `postgres`) exists in `docker-compose.hosted.yml`.

## Why

Upstream's quick start publishes the backend on `0.0.0.0:8000`, MCP on `8080`, Vector on `8686` and the collector on `4317/4318/8889/13133`. On a home server those ports often collide with other services, and the backend is exposed to the whole LAN. This override keeps everything on loopback, with a reverse proxy in front (see `../caddy`).

The frontend needs no override: upstream already honours `FRONTEND_BIND_ADDRESS`. Set it to `127.0.0.1` in `.env`.

## How to enable

1. Copy the files next to upstream's compose file (Docker Compose 2.24 or newer is needed for `!override`):
   ```
   export TRINITY_DIR=/srv/trinity
   cp docker-compose.box.yml $TRINITY_DIR/     # release.env (pinned images) is written next to it by hs-release
   mkdir -p $TRINITY_DIR/patches && cp ../patches/stuck-threshold.py $TRINITY_DIR/patches/
   install -m 0755 trinity /usr/local/sbin/hs-trinity
   ```
2. In `$TRINITY_DIR/.env` set `FRONTEND_BIND_ADDRESS=127.0.0.1`, plus the secrets upstream's `.env.example` asks for.
3. Check the merged file: `hs-trinity config -q`
4. Start: `hs-trinity up -d` (add `--profile otel` before `up` for the collector).
5. Point the reverse proxy at `127.0.0.1:8000` (API), `127.0.0.1:18080` (MCP) and the frontend port.

Do not run upstream's `start.sh`, `install.sh` or a bare `docker compose up` on this host: they use the upstream ports.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TRINITY_DIR` | `/srv/trinity` | directory with the compose files and `.env` (used by the wrapper) |
| `BOX_BACKEND_PORT` | `8000` | host loopback port for the backend |
| `BOX_MCP_PORT` | `18080` | host loopback port for the MCP server |
| `BOX_VECTOR_PORT` | `8686` | host loopback port for Vector |
| `FRONTEND_PORT` | `80` | host port of the frontend (`hs-release` probes it; override `FRONTEND_URL` if it differs) |
| `BOX_PATCHES_DIR` | `./patches` | host directory mounted at `/patches` |

Memory limits are examples. Edit them in the file for your machine.

## Undo

Run `hs-trinity` with the base file only: `docker compose -p trinity -f docker-compose.hosted.yml up -d`. To drop just the patch hook, delete the backend `volumes` and `entrypoint` keys and run `hs-trinity up -d backend`.

## Validation

`docker compose config -q` was not run when this was written: Docker Compose is not installed in the authoring environment. Run step 3 above before first use.
