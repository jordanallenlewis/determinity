# Determinity releases: running, upgrading, rolling back

## Versions

A Determinity release is named after the Trinity release it is built on:

```
v<trinity version>-determinity.<n>        v1.0.5-determinity.1, v1.0.5-determinity.2, v1.0.6-determinity.1
```

`<n>` starts at 1 for each Trinity release and goes up by one for every Determinity release on the same base.
Numbers are never reused: the next `<n>` is one more than the highest tag `v<trinity version>-determinity.<n>` that
exists in the repository, and tags stay when a GitHub Release is deleted. If someone deletes a release **and** its tag,
the number is only protected when the release workflow passes the names of the existing releases in
`DETERMINITY_KNOWN_TAGS` (`homeserver/release/determinity-version` reads it); otherwise it could be handed out again,
and a box that already ran the old one would see two different releases under one name. Do not delete tags. The
repository's tag rules enforce this: no tag can be moved or deleted, and only the release workflow's deploy key can create a
`v*-determinity.*` tag (the release job pushes it over SSH; never push one by hand). So a wrong release tag stays where it
is: release again from a new commit on `hq`, and that number is skipped.
The release job checks again with its write token, drafts included (deleting a published Release's tag turns it into a
draft): a Release under the new tag whose `release.json` names another revision, or that has no `release.json`, stops
the release.

A release that failed after its tag was created is finished with **Re-run failed jobs** on the same run (within 30
days); a fresh run stops with "HEAD is already released". A person with write access on the repository clicks the
re-run; automation does not re-run release runs. A re-run resumes only its own tag and Release. Use **Re-run failed
jobs**, not "Re-run all jobs": the image jobs stop whenever the tag already exists (they ran before it was created, and a
re-run of them would push images over a released version's tags). Two runs that both failed before the tag share a number:
re-running the older one releases the older revision under it, so re-run the newer one. If a newer release is already `latest`, an
older one that finishes late is published without moving `latest` on the Release or the images; if `latest` cannot be
read, the run stops before publishing and a re-run finishes it.

After 30 days GitHub no longer offers the re-run. Leave the tag and the draft Release as they are (the number stays
used; servers never read a draft) and release again from a new commit on `hq`: the next run takes the next number.
A draft without `release.json` that an earlier attempt of the same release left behind (cancelled while creating or
uploading; its tag is on that run's revision) stops every re-run; a person deletes that draft Release, not the tag, and re-runs the failed jobs.
A new Trinity release that the daily sync merges into `hq` starts a new release on its own. When the sync opens a
pull request instead, GitHub does not start the `hq` checks on it by itself (it is opened with the workflow token):
before merging it, a person starts them, for example by closing and reopening the pull request or pushing a commit
to its branch, and waits for them to pass. A Determinity-only change
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
No release carries it yet: the hardened agent image is planned for `v1.0.5-determinity.2`
([known issue](agent-image/README.md#known-issue)).

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

   `homeserver_files` (optional object) maps repository paths to the sha256 of that file at the release, for the
   files that are installed on a server by hand rather than shipped as an image: `homeserver/release/hs-release`,
   `hs-release-alert`, `hs-release.service`, `hs-release-alert.service`, `hs-release.timer` and
   `homeserver/compose/docker-compose.box.yml`. A follower may compare them with what it has installed (see
   "Files that do not follow a release"). Absent means no check.
3. **Tags are never moved or reused.** A bad release is fixed by a new one, never by re-tagging.

## Running a release on a home server

What follows a release by itself, and what does not:

- **Follows automatically:** the four images (and `images.agent`, once a release carries it), pinned by digest by
  `hs-release`.
- **Does not follow yet:** the agent base image, `trinity-agent-base`. Agents keep the one the server has. If it is
  older than the Trinity release the server runs, the backend raises `fleet_base_image_stale` and agents stay on the
  old runtime: rebuild it from the release tag with `./scripts/deploy/build-base-image.sh`, then recreate the agents.
- **Does not follow:** the files under `homeserver/` that live on the server itself: `hs-release` and `hs-release-alert`,
  their systemd units, the compose override (`docker-compose.box.yml`), the `trinity` wrapper and the patches. They reach
  the server through the install step below, run from the release tag, and the release notes say when they changed.
  `hs-release` deliberately does not replace itself: the updater that runs as root unattended is not changed by the thing
  it fetches. A fix to any of those files is therefore not live until someone re-runs the install from the tag.

The compose override (`compose/docker-compose.box.yml`) runs Determinity's images for the four services, each set
by a variable: `DETERMINITY_BACKEND_IMAGE`, `DETERMINITY_FRONTEND_IMAGE`, `DETERMINITY_SCHEDULER_IMAGE`,
`DETERMINITY_MCP_SERVER_IMAGE`. `release/hs-release` writes them, pinned by digest, to `release.env` next to `.env`;
the `compose/trinity` wrapper reads both.

A machine follower must also check that the release was published by `github-actions[bot]` (`author.login` in the
releases API), that the `release.json` asset was uploaded by the same login (`assets[].uploader.login`), and that every `ref` is exactly `ghcr.io/<owner of this repo>/determinity-<service>@sha256:<64 hex>`;
`hs-release` refuses anything else before it writes a file or pulls an image.

Install (as root, from the `homeserver/` folder of a checkout of the release tag, `git checkout v1.0.5-determinity.1`;
run it again from the new tag whenever the release notes list a `homeserver/` change):

```
install -m 0755 release/hs-release release/hs-release-alert /usr/local/sbin/
install -m 0644 release/hs-release.service release/hs-release-alert.service release/hs-release.timer /etc/systemd/system/
install -m 0644 compose/docker-compose.box.yml /srv/trinity/        # next to docker-compose.hosted.yml and .env
install -m 0755 compose/trinity /usr/local/sbin/hs-trinity           # the compose wrapper; it reads release.env too
cat > /etc/default/hs-release <<'CONF'
TRINITY_DIR=/srv/trinity
# BOX_BACKEND_PORT=8000  BOX_MCP_PORT=18080  FRONTEND_PORT=80   (only if not the defaults)
# REQUIRED, see "Alerts"; replace with your own topic. Comments go on their own line: systemd reads the rest of a
# line as the value, so a trailing comment would become part of the URL.
HS_RELEASE_ALERT_URL=https://ntfy.example.com/hs-release
# HS_RELEASE_MIN_AGE_HOURS=24   HS_RELEASE_AGENT_IMAGE=yes   (the defaults; see "Upgrading")
# HS_RELEASE_EXTERNAL_DB=own-backups-no-rollback-across-migration   (only for an external PostgreSQL; see "Rolling back")
# HS_RELEASE_FILES="homeserver/release/hs-release=/usr/local/sbin/hs-release ..."   (only if you install elsewhere)
CONF
# Run both through systemd, so they read /etc/default/hs-release exactly as the units do:
systemd-run --wait --pipe --quiet -p EnvironmentFile=/etc/default/hs-release /usr/local/sbin/hs-release-alert --test
                                    # required: must print "test message sent" and the message must reach you
systemd-run --wait --pipe --quiet -p EnvironmentFile=/etc/default/hs-release /usr/local/sbin/hs-release status
                                    # must print no HS_RELEASE_ALERT_URL warning (unset or not a valid URL)
hs-release apply <latest tag>       # the first switch, by hand, watched; <latest tag> is the Release marked
                                    # "Latest" on the Releases page (an older tag puts a hold on latest).
                                    # Run by hand, hs-release and hs-release-alert read /etc/default/hs-release
                                    # themselves, so they see the same settings as the units
systemctl daemon-reload && systemctl enable --now hs-release.timer
```

`hs-release` and `hs-release-alert` read `/etc/default/hs-release` themselves when you run them by hand, the way
systemd's `EnvironmentFile` does for the units: `KEY=VALUE` lines, blank lines and lines starting with `#` skipped,
optional surrounding quotes removed. The file is never run as shell (a `$(...)` in a value is just text), and a variable
already set in your environment wins over the file. So `hs-release apply <tag>` and `hs-release rollback --db-restored`
on an external database see `HS_RELEASE_EXTERNAL_DB` without you exporting it, and a by-hand run probes the ports the
file sets. (`HS_RELEASE_DEFAULTS` points both at another file; it exists for the tests.)

Setting `HS_RELEASE_ALERT_URL` and receiving the test message are **part of the install**, not an option: with it unset
every failure (a rollback, a moved schema, a broken state) is only a journal line and nobody is told. `hs-release status`
and every non-zero exit of `hs-release` print a warning while it is unset.

Always run compose through `hs-trinity` (or with `--env-file .env --env-file release.env`): without `release.env` the
four images fall back to the moving `:latest` tag instead of the pinned digests.

The first `apply` records what runs now as the rollback point, **by digest**: each running image is resolved to a
repository digest (`docker image inspect`, `RepoDigests`, the one of the image's own repository preferred), for example
`ghcr.io/abilityai/trinity-backend@sha256:...` instead of `...:v1.0.5`, because a tag can move before you roll back. If a
service's image has no repo digest (built or loaded locally), or a service is not running, the first switch is refused
before anything is backed up or changed.

### Files that do not follow a release

When `release.json` has `homeserver_files`, `hs-release apply` (every run, including the no-op ones) and
`hs-release status` hash the installed copy of each listed file and, when one differs or is missing, print:

```
homeserver files differ from release v1.0.5-determinity.2: homeserver/release/hs-release; re-run install from tag v1.0.5-determinity.2
```

It is a warning only: the apply goes on and the exit code is unchanged. Where each file is installed is defined once, in
`hs-release` (`HS_RELEASE_FILES`), matching the install commands above:

| repository path | installed at |
|---|---|
| `homeserver/release/hs-release`, `hs-release-alert` | `/usr/local/sbin/` |
| `homeserver/release/hs-release.service`, `hs-release-alert.service`, `hs-release.timer` | `/etc/systemd/system/` |
| `homeserver/compose/docker-compose.box.yml` | `$TRINITY_DIR/docker-compose.box.yml` |

Override it in `/etc/default/hs-release` with space-separated `repo path=installed path` pairs if yours live elsewhere.
Paths in `homeserver_files` that have no entry in the list are not checked. After re-running the install, run
`systemctl daemon-reload` for unit changes.

## Upgrading

The timer runs `hs-release apply` once a day off-peak (04:15 Pacific; `Persistent=false`, so a run missed while the host
was off is not made up at boot). The timer path is deliberately more careful than a person naming a tag:

- **Minimum age:** a latest release younger than `HS_RELEASE_MIN_AGE_HOURS` (24; `0` turns it off) by its
  `published_at` is left alone and logged (`waiting until <time>`, exit 0), so a release cannot be built, published and
  applied overnight with nobody having seen it.
- **Forward only:** it never moves to a release whose version is lower than what runs (upstream version first, then `<n>`,
  compared numerically: `v1.0.10-determinity.1` is newer than `v1.0.9-determinity.5`). A withdrawn or re-pointed
  `latest` therefore does not downgrade the box; a tag it cannot compare is not moved to either. Both are logged.
- **Held releases** (failed the gate here, rolled back from, the latest when a person applied an older release, or held
  with `hs-release hold <tag>`) are skipped.

`hs-release apply <tag>` by a person ignores all three, with one consequence: when the tag is **older than the latest
release**, `hs-release` holds the latest release (it prints how), so the timer does not move forward again the next
night and undo the choice. A hold is lifted only by `hs-release apply <held tag>` (a person naming it), a rollback that
lands on it, or deleting its line from `/var/lib/hs-release/refused`. A release newer than the held one is not held and
the timer follows it once it is old enough. If the latest release cannot be read during that apply, no hold is set; when
the tag is older than what ran before (or there was nothing to compare with), `hs-release` prints a WARNING after the
switch result; hold it by hand: `hs-release hold <latest tag>`. By hand: `hs-release check` (exit 10 means a
newer release exists), then `hs-release apply` or `hs-release apply <tag>`.

`apply` refuses drafts, pre-releases, tags outside the scheme, releases not published by `github-actions[bot]`, a
`release.json` not hosted under `https://github.com/<repo>/releases/download/`, image refs that are not exactly
`ghcr.io/<owner>/determinity-<service>@sha256:<digest>`, and a `release.json` without digest pins. It then:

1. pulls all images (and `images.agent` if present) before touching anything;
2. on the bundled PostgreSQL (decided from the backend's effective `DATABASE_URL`, read with `compose config --format json`: host `postgres` is bundled, empty or `sqlite:` is SQLite, anything else is external, see "Rolling back"), reads the Alembic revision (exactly one `alembic_version` row, else it counts as unreadable) and takes a `pg_dump` (custom format, through the `postgres` container)
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

When the release has `images.agent` it is, by default, pulled and tagged locally as `trinity-agent-base:latest`, the tag the
backend creates new agents from (upstream's `start.sh` does the same with its own image), and the image that tag pointed
at is part of the rollback point (`AGENT_ID` in `previous.meta`), re-tagged on any rollback. A running agent keeps its old
image until it is recreated; new agents pick it up at once. Set `HS_RELEASE_AGENT_IMAGE=no` in `/etc/default/hs-release`
to leave `trinity-agent-base` alone. A release without `images.agent` leaves it alone too.

## Rolling back

Exit codes of `hs-release apply`: `0` fine or nothing to do, `1` error (nothing switched), `2` the release failed the
gate and the images were rolled back (and the rollback is healthy), `3` the database schema moved and a person must
restore (below), `4` broken (below).

Two markers stop the timer from stacking changes on a bad state. Both make every later timer run exit non-zero (so it
alerts every day, not once), and `hs-release status` prints them first:

| marker | exit | meaning | stops | cleared by |
|---|---|---|---|---|
| `/var/lib/hs-release/blocked` | 3 | the schema moved under a release that failed the gate; `release.env` keeps pinning the failed release and, on PostgreSQL, **backend and scheduler are left stopped** (the file says `services_stopped=backend scheduler`; the app is down until a person acts) | the timer path | `hs-release rollback --restore-db`; `hs-release apply <the pinned tag>` (checks the database first, then starts the services and re-runs the gate; healthy clears it; failing leaves it, exit 3, and on the bundled PostgreSQL stops backend and scheduler again; on an external database nothing is stopped and the file says `services_stopped=none`; if that stop fails the state is `broken` too, `services_stopped=FAILED`, exit 4: stop them by hand, then `rollback --restore-db`); or a person fixing forward with `apply <tag>` once that release is healthy. `clear-broken` does not lift it (it exits 3 while `blocked` stays) |
| `/var/lib/hs-release/broken` | 4 | the state is unknown: a release **and** its rollback both failed the gate (`double-failure`), a manual rollback or a restore failed (`rollback-failed`, `restore-failed`), or a run was killed or the host reset between switching and verifying (`interrupted`, found from the `inflight` note), or a failed release's backend and scheduler could not be stopped (`apply-stop-failed` on a first failure, `regate-stop-failed` on the re-gate of a blocked release) | every `apply`, the timer's and `apply <tag>` | `hs-release clear-broken`, only |

`broken` holds a reason and a timestamp. While it exists `apply` does nothing and exits 4; `rollback` and
`rollback --restore-db` still run, because they are the repair tools, but they do not clear it. Fix the platform, then
run `hs-release clear-broken`: it runs the health gate on what `release.env` pins and clears the marker only if that
passes (`--force` clears it without the gate). "Running" in `status`, and `current.env`, always equal what `release.env`
pins; while `broken` is set `status` says "pinned in release.env: X (not verified healthy)", and while `blocked` is set
"pinned in release.env: X (failed the health gate, NOT a running release; services stopped: ...)" (with `none` it says
nothing was stopped, so the release is running and not verified healthy; with `unknown` or `FAILED` that it may be
serving: check `docker ps`), never "running".

Ways out of `broken` (every one ends with the marker gone and `apply` working again):

1. Fix the platform, then `hs-release clear-broken`: it clears the marker if the health gate passes on what `release.env` pins.
2. The gate cannot pass but you accept the state (for example the pinned release is the broken one and a healthy tag
   exists): `hs-release clear-broken --force`, then `hs-release apply <healthy tag>`, which switches to a release that is
   checked.
3. Repair with the tools that run while `broken` is set, `hs-release rollback` (or `rollback --restore-db`, or
   `rollback --db-restored` on an external database), and when the stack is as it should be, `hs-release clear-broken`
   as in route 1: a rollback does not clear the marker by itself.

A failed stop of backend and scheduler is `broken` (exit 4) in every place it can happen: a first failure on a moved schema
(`apply-stop-failed`), a rollback that fails the gate while `blocked` (`rollback-failed`), a re-gate of a blocked release
(`regate-stop-failed`), and a re-gate stopped by Ctrl-C or TERM, which settles the file the same way (`broken` only when its stop fails) and exits 130 or 143.

- **Automatic, schema unchanged:** a release that fails the gate is rolled back at once (exit 2, logged in
  `/var/lib/hs-release/log`; the agent image is re-tagged too). The timer does not try that release again;
  `hs-release apply <tag>` does. The rollback point from before the attempt is put back, so `hs-release rollback`
  afterwards still goes one release further back instead of doing nothing. If the rollback fails the gate too, the state
  is `broken` (exit 4, not 2).
- **Automatic, schema moved (exit 3):** if the Alembic revision after the failed gate differs from the one recorded
  before (or cannot be read), the new backend probably migrated the database and the old images could crash or damage
  it. The images are **not** rolled back. The failed release stays pinned in `release.env` but is **not serving**:
  backend and scheduler were stopped for the revision read and stay stopped (a failed release must not run on a moved
  schema; frontend and mcp-server keep running). The tag is held, and
  `/var/lib/hs-release/blocked` is written (with `services_stopped=`; `status`, the log and the alert say so too); the timer then refuses to apply anything (exit 3 each day, which alerts)
  until a person restores (a restore that itself fails is `broken`):

  ```
  hs-release rollback --restore-db
  ```

  It saves the current database as `<tag>-prerestore-*.dump`, stops backend, scheduler and mcp-server, drops and
  recreates the `public` schema, runs `pg_restore` of the dump taken before that apply, checks the revision is the
  recorded one, starts the previous images, runs the health gate and clears `blocked`. The same steps by hand are
  printed in the log (`hs-trinity stop ...`, `hs-trinity exec -T postgres ... pg_restore ... < <backup>`; `hs-trinity` is the wrapper installed above). Everything
  written since that backup is lost: that is the cost of a release that migrated, so decide before running it. If the
  revision could not be read (the state `schema_now=unreadable` records), the restore runs anyway and the revision
  check afterwards proves the result. Before reading the revision after a failed gate, backend and scheduler are
  stopped, so a migration still in flight cannot move the schema afterwards.
- **While blocked, `apply <tag>`** does not replace the rollback point or the dump `blocked` names (`previous.env`,
  `previous.meta`, the pre-failure dump, which pruning also never deletes): that attempt takes its own dump
  (`<tag>-retry-*`) and, if it fails the gate with the schema unchanged, goes back to the release that was running.
  `rollback --restore-db` keeps restoring the pre-failure dump.
- **`clear-broken` while blocked** does not lift `blocked`; it says so, names the commands above and exits 3.
- **By hand, schema unchanged:** `hs-release rollback` (when its gate passes it also clears `blocked`) switches to the previous state and, run twice, back again. It
  refuses (exit 3) when the current Alembic revision differs from the one recorded for the rollback target, and
  points at `--restore-db`.
- **A manual rollback sticks:** the release you rolled back from is held, so the timer does not put it back the
  next night. `hs-release apply <tag>` clears the hold. `hs-release status` lists held tags and any blocked or broken state. If the manual
  rollback itself fails the gate, the state is `broken`.
- **The database is decided from the URL, strictly.** Bundled means: scheme `postgresql` (or `postgres`, with an
  optional `+driver`), host exactly `postgres`, port empty or `5432`, database name equal to the `postgres` service's
  `POSTGRES_DB`, no query string and a single `@`. Anything else that is not `sqlite:` (another host, another database,
  `?host=...`, a password containing `@postgres:`) is treated as external. A backend with no `DATABASE_URL` (no backend
  service, no key, or null) is refused with a message: nothing is guessed. Set but empty (`DATABASE_URL=`) is
  upstream's SQLite switch (`docker-compose.hosted.yml`, backend `DATABASE_URL` comment) and is read as SQLite.
- **SQLite installs** (`DATABASE_URL` an explicit `sqlite:` URL or set but empty, even though `docker-compose.hosted.yml` always defines a `postgres` service): no backup is taken and the schema is not tracked, so a rollback is
  code-only as before. An image rollback does not undo a migration there either; Trinity keeps its own copies under
  `~/trinity-data/backups/` (`pre-migration-*.db`, `trinity-backup-*.db`). Stop the stack and restore one before running
  older images.
- **External PostgreSQL** (a `DATABASE_URL` that is not the bundled one, above): `apply` refuses, because this updater takes no
  backup of a database it does not run. Setting `HS_RELEASE_EXTERNAL_DB=own-backups-no-rollback-across-migration` in
  `/etc/default/hs-release` states that you take your own backups and accept that a release that fails the gate is never
  rolled back automatically: the schema is treated as moved, the state is `blocked` (exit 3) and the images stay. Restore
  your own backup, then run `hs-release rollback --db-restored`, which runs the previous images and clears `blocked`;
  `--restore-db` is refused there. A plain `hs-release rollback` is refused too, blocked or not: with no schema
  check possible, `--db-restored` is you stating that the database is back at what the previous release expects.
- **A switch interrupts running work.** `apply` and `rollback` recreate the backend, which stops any agent execution
  running at that moment; hs-release does not look for running executions first (Trinity has no endpoint for that, and
  none is assumed). That is why the timer runs at its fixed off-peak hour (04:15 Pacific); for a by-hand `apply` or
  `rollback` pick a quiet time. Open item: a check for running executions before a restart.
- After `rollback --restore-db` the record in `previous.meta` names the backup that was restored (`RESTORED_FROM=`),
  and the log says so.
- On PostgreSQL the `pre-migration-*.db` files do not exist; the dumps in `/var/lib/hs-release/backups/` are the
  recovery points for this updater. They are on the same disk as the database: they protect against a bad release, not
  against losing the disk.

## Alerts

`hs-release.service` has `OnFailure=hs-release-alert.service`, and exits non-zero for an error (1), a rollback (2),
a moved schema (3) and a broken state (4, every day until cleared), so every one of them alerts. The unit has
`TimeoutStartSec=45min` (a normal run takes minutes; if systemd kills a run mid-switch the next run records it as broken). `hs-release-alert` POSTs the last log lines as `text/plain`
(ntfy-style, with `Title` and `Priority` headers) to `HS_RELEASE_ALERT_URL` from `/etc/default/hs-release`. If that is
unset, or the POST fails, it writes them to the journal at priority `err`
(`journalctl -t hs-release-alert -p err`). Unset means nobody is told, so the install steps require it:
`hs-release-alert --test` sends a test message and exits non-zero when the URL is unset or the POST fails. While it is
unset, or set to something that is not a single `http(s)://` URL (a trailing comment in the file becomes part of the
value), `hs-release status` and every non-zero `hs-release` exit print a warning line, and `--test` fails. When a `blocked` marker exists the
alert says what its `services_stopped=` records: `backend scheduler`, that they are stopped on purpose and the app is down
until a person acts; `none` (an external database), that nothing was stopped and the failed release is running and has
not been verified healthy; `unknown` or `FAILED`, that the stop did not finish or failed, the failed release may be
serving on a moved schema, and `docker ps` shows what runs. `homeserver/ops/` has no shared alert channel to reuse
(`hs-caretaker` raises Trinity notifications, which needs its token and a working backend; this alert must work when the
backend is the thing that failed).

## Issues found while running it

Every platform problem found on a server running Determinity becomes an issue on this repository, and the fix lands
here. An image fix reaches the server through a release, by itself; a fix to the `homeserver/` files (the updater,
units or compose override) reaches it when the install step is re-run from the release tag (see "Running a release on a home
server"; `hs-release` warns when the installed files differ). See [CONTRIBUTING.md](CONTRIBUTING.md).
