# store-images

Stopgap rebuilds of the VictoriaLogs and VictoriaTraces container images used by FigureCollecting.
Each image is built from an upstream release tag with exactly one change: the Go toolchain is moved
to the patch release that upstream has already adopted on its main branch. Nothing else is changed.

The images are published as `ghcr.io/figurecollecting/victoria-logs` and
`ghcr.io/figurecollecting/victoria-traces`, tagged `<upstream tag>-fc<N>`.

This repository will be archived once upstream publishes releases built with the fixed Go version.
At that point, consumers pin upstream's own images again.

## What is rebuilt

| Image | Upstream tag (commit) | Upstream Go | Ours | Upstream's Go fix on master |
|---|---|---|---|---|
| victoria-logs | v1.53.0 (915d91904b) | 1.27.1 | 1.27.3 | VictoriaLogs 06d56702 |
| victoria-traces | v0.12.1 (7b13fef2f) | 1.27.2 | 1.27.3 | VictoriaTraces 8586f7c8 |

## Layout

- `images/<app>.env`: the definition (upstream repo, tag, commit, Go version, our `FC_REVISION`).
- `patches/<app>.patch`: the only source change, the `go` line in `go.mod` and `GO_BUILDER_IMAGE` in
  `deployment/docker/Makefile`, as in upstream's fix commit.
- `Dockerfile`: upstream's linux/amd64 build (`-trimpath`, static, `netgo osusergo`, CGO on as upstream
  does for amd64, same distroless runtime pin, same binary path), with the builder and runtime pinned by
  digest. The build checks the upstream commit, the patch and the resulting Go version.
- `scripts/build.sh` builds; `scripts/scan.sh` runs trivy and grype; `scripts/gate.py` fails on any
  HIGH or CRITICAL finding that has a fix, from either scanner, with no ignore list; `scripts/smoke.sh`
  runs the image and checks `/health`, the version string and `go version -m`;
  `scripts/upstream_watch.py` reports a newer upstream release.

## Local use

    docker buildx create --name store-images --driver docker-container --use
    scripts/build.sh victoria-logs archive ~/tmp/store-images/victoria-logs.tar
    scripts/scan.sh victoria-logs ~/tmp/store-images/victoria-logs.tar ~/tmp/store-images/scan-vl
    scripts/smoke.sh victoria-logs ~/tmp/store-images/victoria-logs.tar
    python3 -m unittest discover -s tests

## CI

- Pull requests: unit tests, then build, scan gate and smoke for both images. No push, no secrets.
- Push to `main`: the same, then publish. The image is pushed by digest with SBOM and provenance
  attestations, its linux/amd64 manifest is checked against the one that was scanned, and only then is
  it tagged. An existing tag is never overwritten; bump `FC_REVISION` to publish a new build.
- Weekly `upstream-watch`: fails if upstream has a stable release newer than our base tag. That is the
  signal to re-pin consumers to upstream's image (check that its Go is fixed) and archive this repo.

Consumers pin by digest. The packages must be public for anonymous pulls.
