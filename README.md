# store-images

Stopgap rebuild of the VictoriaTraces container image used by FigureCollecting. The image is built from
an upstream release tag with two changes: the Go toolchain is moved to the current patch release, and one
module is moved to its fixed version (go.mod and go.sum only). Nothing else is changed.

The image is published as `ghcr.io/figurecollecting/victoria-traces`, tagged `<upstream tag>-fc<N>`.

This repository will be archived once upstream publishes releases built with the fixed versions.
At that point, consumers pin upstream's own images again.

## What is rebuilt

| Image | Upstream tag (commit) | Upstream Go | Ours | Module moved | Fixes |
|---|---|---|---|---|---|
| victoria-traces | v0.13.0 (f0295d1f7) | 1.27.3 | 1.27.4 | klauspost/compress v1.20.1 -> v1.20.4 | CVE-2026-97054 (Go), CVE-2026-116591 (compress) |

Upstream's master had neither change on 2026-11-20. v1.20.4 is the current compress release and the one
VictoriaLogs v1.54.0 ships.

VictoriaLogs is no longer rebuilt here. Upstream's v1.54.0 image is built with Go 1.27.4 and
klauspost/compress v1.20.4 and passed the scan gate on 2026-11-20, so consumers pin upstream's image.
The last recipe (v1.53.0-fc1) is in this repository's history; the published package stays.
`watch/victoria-logs.env` keeps it on the weekly upstream-watch (no build), so a newer upstream release is
still flagged.

The binary is built from the clean tag. Upstream's own v0.13.0 binary reports a `-dirty` version suffix
(its release was built from a modified tree); `-help`, the image config, the Go buildinfo and the embedded
UI assets match it.

## Layout

- `images/<app>.env`: the definition (upstream repo, tag, commit and image, upstream's Go, our Go, the
  module and version the patch moves, our `FC_REVISION`).
- `watch/<app>.env`: an image we only watch (upstream repo and the tag the store runs); never built.
- `patches/<app>.patch`: the only source change, the module's line in `go.mod` and its two `go.sum` lines.
- `Dockerfile`: upstream's linux/amd64 build (`-trimpath`, static, `netgo osusergo`, CGO on as upstream
  does for amd64, same distroless runtime pin, same binary path), with the builder, the runtime and
  upstream's image pinned by digest. The build checks the upstream commit, the patch footprint and the Go
  version, builds from the go.sum-verified module cache with no network, and fails unless the binary's
  `go version -m` matches the binary in upstream's image except for the Go version and the moved module.
  `-trimpath` hides the linker flags from `go version -m`, so the unit tests read the Dockerfile as
  BuildKit does (keywords in any case, indentation, continuations, no parser directives, heredocs or
  ONBUILD) and pin its sequence of FROM lines and every instruction of the build stage. The build also
  fails unless the binary's ELF section names and its program-header types and flags match upstream's
  binary (a stripped binary or an executable stack fails), and on any file the patch adds, at a
  git-ignored path or not. Threat model: these checks catch patch drift and accidental flag changes; they
  do not defend against a hostile Dockerfile edit, which code review must catch.
- `scripts/build.sh` builds; `scripts/scan.sh` runs trivy and grype; `scripts/gate.py` fails on any
  HIGH or CRITICAL finding that has a fix, from either scanner, with no ignore list; `scripts/smoke.sh`
  runs the image, checks `/health`, writes a gzip-compressed OTLP span and reads it back through the
  Jaeger API, and checks the version string and `go version -m`;
  `scripts/upstream_watch.py` reports a newer upstream release.

## Local use

    docker buildx create --name store-images --driver docker-container --use
    scripts/build.sh victoria-traces archive ~/tmp/store-images/victoria-traces.tar
    scripts/scan.sh victoria-traces ~/tmp/store-images/victoria-traces.tar ~/tmp/store-images/scan-vt
    scripts/smoke.sh victoria-traces ~/tmp/store-images/victoria-traces.tar
    python3 -m unittest discover -s tests

## CI

- Pull requests: unit tests, then build, scan gate and smoke for every image. No push, no secrets.
- Push to `main`: the same, then publish. The image is pushed by digest with SBOM and provenance
  attestations, its linux/amd64 manifest is checked against the one that was scanned, and only then is
  it tagged. An existing tag is never overwritten; bump `FC_REVISION` to publish a new build.
- Weekly `upstream-watch`: fails if upstream has a stable release newer than our base tag (`images/` and `watch/`). For
  an `images/` entry that is the signal to check the new release's Go and modules, then either re-pin consumers
  to upstream's image and archive this repo, or move the recipe to the new tag. For a `watch/` entry, check its
  image's scan, re-pin the store and bump the tag in `watch/`; the rebuilds in `images/` stay.

Consumers pin by digest. The packages must be public for anonymous pulls.
