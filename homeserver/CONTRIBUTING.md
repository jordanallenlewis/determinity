# Contributing to Determinity

Determinity follows Trinity's own [CONTRIBUTING.md](../CONTRIBUTING.md) for code style, tests and pull requests.
This page adds the rules that are specific to the fork.

## A problem on a running server is fixed here, then released

1. **Open an issue on this repository** for every platform problem hit on a server running Determinity: what
   broke, the version (`hs-release status`), and how to see it.
2. **Fix it in this repository:** in `homeserver/`, or as a carried patch on upstream files that is shaped so it
   could be offered upstream (small, with a test, no fork-only assumptions). The pull request says `Fixes #<issue>`.
3. **It reaches the server through a release.** Merge to `hq` and run the Determinity release workflow. What happens
   next depends on what you changed:
   - **Images** (backend, frontend, scheduler, MCP server, agent): the server's updater (or `hs-release apply`) picks
     them up by itself, pinned by digest.
   - **Files under `homeserver/`** (the updater and its units, the compose override, the wrapper, patches): these live
     on the server, and the updater does not replace itself. They arrive when someone re-runs the install step from the
     release tag ([RELEASES.md](RELEASES.md), "Running a release on a home server"). Say in the pull request that the
     install has to be re-run; the release notes carry it. `hs-release status` warns when installed files differ from
     the release.
   Nothing else puts code on a server except the exception in the next point.
4. **A fix made only on the server is allowed to stop harm, and only for that.** Write it down in the issue at once
   (what was changed, where, how to undo it), and land the same change here as a pull request. The issue stays open
   until the server runs a release that contains it and the server-only change is removed.

If the problem is in upstream Trinity, also report it upstream, and drop the carried patch once an upstream release
contains the fix.

## Upstream files

Files that come from upstream stay as upstream wrote them, except the ones the README lists. A change to any other
upstream file is a carried patch: list it in the README's "What's different" table in the same pull request.

## Releases

How releases are named, built and followed: [RELEASES.md](RELEASES.md). Never push a release tag by hand; the
Determinity release workflow creates it.
