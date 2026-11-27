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

The agent base image is not rebuilt: Determinity does not change `docker/base-image/`, so agents keep using
upstream's `trinity-agent-base` for the same Trinity version.

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

   Pull and run each `ref` (pinned by digest). Image names and the schema field are stable; a breaking change to
   the file gets a new `schema` value.
3. **Tags are never moved or reused.** A bad release is fixed by a new one, never by re-tagging.

## Running a release on a home server

The compose override (`compose/docker-compose.box.yml`) runs Determinity's images for the four services, each set
by a variable: `DETERMINITY_BACKEND_IMAGE`, `DETERMINITY_FRONTEND_IMAGE`, `DETERMINITY_SCHEDULER_IMAGE`,
`DETERMINITY_MCP_SERVER_IMAGE`. `release/hs-release` writes them, pinned by digest, to `release.env` next to `.env`;
the `compose/trinity` wrapper reads both.

Install (as root):

```
install -m 0755 release/hs-release /usr/local/sbin/hs-release
install -m 0644 release/hs-release.service release/hs-release.timer /etc/systemd/system/
printf 'TRINITY_DIR=/srv/trinity\n' > /etc/default/hs-release     # plus BOX_BACKEND_PORT if not 8000
hs-release status
hs-release apply v1.0.5-determinity.1                             # the first switch, by hand, watched
systemctl daemon-reload && systemctl enable --now hs-release.timer
```

The first `apply` records what runs now (for example upstream's `ghcr.io/abilityai/trinity-*:v1.0.5`) as the rollback
point.

## Upgrading

The timer runs `hs-release apply` once a day off-peak (04:15 Pacific). By hand: `hs-release check` (exit 10 means a
newer release exists), then `hs-release apply` or `hs-release apply <tag>`.

`apply` refuses drafts, pre-releases, tags outside the scheme and a `release.json` without digest pins. It pulls all
four images before touching anything, then switches and runs the health gate:

- every one of the four containers runs exactly the pinned image, and
- the backend's `/health` answers 200 within `HEALTH_TIMEOUT` (300 s).

## Rolling back

- **Automatic:** a release that fails the health gate is rolled back to the previous state at once (exit code 2,
  logged in `/var/lib/hs-release/log`). The timer does not try that release again; `hs-release apply <tag>` does.
- **By hand:** `hs-release rollback` switches to the previous state and back again if run twice.
- **Database:** an image rollback does not undo a database migration. When the new release was based on a newer
  Trinity version, check its release notes for migrations; Trinity keeps a pre-migration backup under
  `~/trinity-data/backups/` (`pre-migration-*`). Restore it before running the older images.

## Issues found while running it

Every platform problem found on a server running Determinity becomes an issue on this repository, and the fix lands
here and reaches the server through a release. See [CONTRIBUTING.md](CONTRIBUTING.md).
