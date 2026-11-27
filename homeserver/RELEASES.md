# Determinity releases: running, upgrading, rolling back

## Versions

A Determinity release is named after the Trinity release it is built on:

```
v<trinity version>-determinity.<n>        v1.0.5-determinity.1, v1.0.5-determinity.2, v1.0.6-determinity.1
```

`<n>` starts at 1 for each Trinity release and goes up by one for every Determinity release on the same base.
A new Trinity release that the daily sync merges into `hq` starts a new release on its own. A Determinity-only change
is released by running the **Determinity release** workflow on `hq` from the Actions tab.

Every release has:

- a git tag and a GitHub Release (notes: the Trinity release it is based on, and the Determinity changes since the
  previous release);
- four images, `ghcr.io/jordanallenlewis/determinity-{backend,frontend,scheduler,mcp-server}`, tagged
  `1.0.5-determinity.1`, `v1.0.5-determinity.1` and `sha-<commit>`. `latest` moves to a release only after its
  GitHub Release exists;
- OCI labels: `org.opencontainers.image.{title,description,source,version,revision,created,licenses}` plus
  `io.github.jordanallenlewis.determinity.upstream.{source,version,revision}` (the Trinity release it is based on);
- `release.json` attached to the Release (below).

The agent base image is normally not rebuilt: Determinity does not change `docker/base-image/`, so agents keep using
upstream's `trinity-agent-base` for the same Trinity version. A release that does change it carries a fifth,
optional image, `ghcr.io/jordanallenlewis/determinity-agent`, listed as `images.agent` in `release.json`.

## The contract a machine can follow

1. **Which release is current:** `GET https://api.github.com/repos/jordanallenlewis/determinity/releases/latest`.
   GitHub never returns a draft or a pre-release there. Only a tag that matches
   `^v[0-9]+\.[0-9]+\.[0-9]+-determinity\.[0-9]+$` is a release. Anything with a further suffix
   (`v1.0.6-determinity.1-rc.1`) is a pre-release, is marked as one, and is never followed.
   Do not compare versions as semver: `1.0.5-determinity.1` is a semver *pre-release* of 1.0.5, so semver tools
   sort and filter it wrongly. The `latest` release is the answer.
2. **What to run:** the `release.json` asset of that release:

   ```json
   {
     "schema": "determinity-release/1",
     "tag": "v1.0.5-determinity.1",
     "version": "1.0.5-determinity.1",
     "channel": "stable",
     "repo": "jordanallenlewis/determinity",
     "revision": "<commit>",
     "upstream": {"repo": "abilityai/trinity", "tag": "v1.0.5", "revision": "<commit>"},
     "images": {
       "backend":    {"name": "ghcr.io/jordanallenlewis/determinity-backend",    "tag": "1.0.5-determinity.1",
                      "digest": "sha256:...", "ref": "ghcr.io/jordanallenlewis/determinity-backend@sha256:..."},
       "frontend":   {"...": "..."},
       "scheduler":  {"...": "..."},
       "mcp-server": {"...": "..."}
     }
   }
   ```

   `images.agent` (same shape, optional) may be present. Pull and run each `ref` (pinned by digest). Image names and the schema field are stable; a breaking change to
   the file gets a new `schema` value.
3. **Tags are never moved or reused.** A bad release is fixed by a new one, never by re-tagging.

## Running a release on a home server

The compose override (`compose/docker-compose.box.yml`) runs Determinity's images for the four services, each set
by a variable: `DETERMINITY_BACKEND_IMAGE`, `DETERMINITY_FRONTEND_IMAGE`, `DETERMINITY_SCHEDULER_IMAGE`,
`DETERMINITY_MCP_SERVER_IMAGE`. `release/hs-release` writes them, pinned by digest, to `release.env` next to `.env`;
the `compose/trinity` wrapper reads both.

A machine follower must also check that the release was published by `github-actions[bot]` (`author.login` in the
releases API) and that every `ref` is exactly `ghcr.io/<owner of this repo>/determinity-<service>@sha256:<64 hex>`;
`hs-release` refuses anything else before it writes a file or pulls an image.

Install (as root, from the `homeserver/` folder):

```
install -m 0755 release/hs-release release/hs-release-alert /usr/local/sbin/
install -m 0644 release/hs-release.service release/hs-release-alert.service release/hs-release.timer /etc/systemd/system/
install -m 0644 compose/docker-compose.box.yml /srv/trinity/        # next to docker-compose.hosted.yml and .env
install -m 0755 compose/trinity /usr/local/sbin/hs-trinity           # the compose wrapper; it reads release.env too
cat > /etc/default/hs-release <<'CONF'
TRINITY_DIR=/srv/trinity
# BOX_BACKEND_PORT=8000  BOX_MCP_PORT=18080  FRONTEND_PORT=80   (only if not the defaults)
# HS_RELEASE_ALERT_URL=https://ntfy.example.com/hs-release       (see "Alerts")
CONF
hs-release status
hs-release apply v1.0.5-determinity.1                             # the first switch, by hand, watched
systemctl daemon-reload && systemctl enable --now hs-release.timer
```

Always run compose through `hs-trinity` (or with `--env-file .env --env-file release.env`): without `release.env` the
four images fall back to the moving `:latest` tag instead of the pinned digests.

The first `apply` records what runs now (for example upstream's `ghcr.io/abilityai/trinity-*:v1.0.5`) as the rollback
point.

## Upgrading

The timer runs `hs-release apply` once a day off-peak (04:15 Pacific). By hand: `hs-release check` (exit 10 means a
newer release exists), then `hs-release apply` or `hs-release apply <tag>`.

`apply` refuses drafts, pre-releases, tags outside the scheme, releases not published by `github-actions[bot]`, a
`release.json` not hosted under `https://github.com/<repo>/releases/download/`, image refs that are not exactly
`ghcr.io/<owner>/determinity-<service>@sha256:<digest>`, and a `release.json` without digest pins. It then:

1. pulls all images (and `images.agent` if present) before touching anything;
2. on PostgreSQL, reads the Alembic revision and takes a `pg_dump` (custom format, through the `postgres` container)
   into `/var/lib/hs-release/backups/<from-tag>-<UTC time>.dump`, keeping the newest 5
   (`HS_RELEASE_KEEP_BACKUPS`). A failed or empty backup stops the apply before anything is switched;
3. switches (`compose up -d`) and runs the health gate:
   - every container runs exactly the pinned image;
   - the backend's `/health` answers 200 within `HEALTH_TIMEOUT` (300 s), `/api/setup/status` (an unauthenticated read
     of `system_settings` through the backend's database connection; `/health` does not touch the database on
     PostgreSQL) returns its JSON, and `alembic_version` can be read;
   - the frontend (`FRONTEND_URL`, default `http://127.0.0.1:80/`, set `FRONTEND_PORT` if it differs) and the MCP server
     (`http://127.0.0.1:18080/health`) answer;
   - for `HEALTH_SOAK` seconds (90) every service stays running with the same container and an unchanged restart
     count, so a crash loop that answers 200 between crashes fails the gate.

The optional `images.agent` is pulled and tagged locally as `trinity-agent-base:latest`, the tag the backend creates
new agents from. A running agent keeps its old image until it is recreated; new agents pick it up at once. A release
without `images.agent` leaves `trinity-agent-base` alone.

## Rolling back

Exit codes of `hs-release apply`: `0` fine or nothing to do, `1` error (nothing switched), `2` the release failed the
gate and the images were rolled back, `3` the database schema moved and a person must restore (below).

- **Automatic, schema unchanged:** a release that fails the gate is rolled back at once (exit 2, logged in
  `/var/lib/hs-release/log`; the agent image is re-tagged too). The timer does not try that release again;
  `hs-release apply <tag>` does.
- **Automatic, schema moved (exit 3):** if the Alembic revision after the failed gate differs from the one recorded
  before (or cannot be read), the new backend probably migrated the database and the old images could crash or damage
  it. The images are **not** rolled back. The failed release stays recorded as current, the tag is held, and
  `/var/lib/hs-release/blocked` is written; the timer then refuses to apply anything (exit 3 each day, which alerts)
  until a person restores:

  ```
  hs-release rollback --restore-db
  ```

  It saves the current database as `<tag>-prerestore-*.dump`, stops backend, scheduler and mcp-server, drops and
  recreates the `public` schema, runs `pg_restore` of the dump taken before that apply, checks the revision is the
  recorded one, starts the previous images, runs the health gate and clears `blocked`. The same steps by hand are
  printed in the log (`trinity stop ...`, `trinity exec -T postgres ... pg_restore ... < <backup>`). Everything
  written since that backup is lost: that is the cost of a release that migrated, so decide before running it.
- **By hand, schema unchanged:** `hs-release rollback` switches to the previous state and, run twice, back again. It
  refuses (exit 3) when the current Alembic revision differs from the one recorded for the rollback target, and
  points at `--restore-db`.
- **A manual rollback sticks:** the release you rolled back from is held, so the timer does not put it back the
  next night. `hs-release apply <tag>` clears the hold. `hs-release status` lists held tags and a blocked state.
- **SQLite installs** (no `postgres` service): no backup is taken and the schema is not tracked, so a rollback is
  code-only as before. An image rollback does not undo a migration there either; Trinity keeps its own copies under
  `~/trinity-data/backups/` (`pre-migration-*.db`, `trinity-backup-*.db`). Stop the stack and restore one before running
  older images.
- On PostgreSQL the `pre-migration-*.db` files do not exist; the dumps in `/var/lib/hs-release/backups/` are the
  recovery points for this updater. They are on the same disk as the database: they protect against a bad release, not
  against losing the disk.

## Alerts

`hs-release.service` has `OnFailure=hs-release-alert.service`, and exits non-zero for an error (1), a rollback (2)
and a moved schema (3), so every one of them alerts. `hs-release-alert` POSTs the last log lines as `text/plain`
(ntfy-style, with `Title` and `Priority` headers) to `HS_RELEASE_ALERT_URL` from `/etc/default/hs-release`. If that is
unset, or the POST fails, it writes them to the journal at priority `err`
(`journalctl -t hs-release-alert -p err`). `homeserver/ops/` has no shared alert channel to reuse
(`hs-caretaker` raises Trinity notifications, which needs its token and a working backend; this alert must work when the
backend is the thing that failed).

## Issues found while running it

Every platform problem found on a server running Determinity becomes an issue on this repository, and the fix lands
here and reaches the server through a release. See [CONTRIBUTING.md](CONTRIBUTING.md).
