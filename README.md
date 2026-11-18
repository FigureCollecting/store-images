# store-images

Stopgap rebuilds of the VictoriaLogs and VictoriaTraces container images used by FigureCollecting.
Each image is built from an upstream release tag with exactly one change: the Go toolchain is moved
to the patch release that upstream has already adopted on its main branch. Nothing else is changed.

The images are published as `ghcr.io/figurecollecting/victoria-logs` and
`ghcr.io/figurecollecting/victoria-traces`, tagged `<upstream tag>-fc<N>`.

This repository will be archived once upstream publishes releases built with the fixed Go version
and it holds no other image we use. At that point, consumers pin upstream's own images again.

It also holds the recipe for our Grafana image, `ghcr.io/figurecollecting/grafana` (see
[Grafana](#grafana)). The recipe is public; the image is internal and its package stays private.

## What is rebuilt

| Image | Upstream tag (commit) | Upstream Go | Ours | Upstream's Go fix on master |
|---|---|---|---|---|
| victoria-logs | v1.53.0 (915d91904b) | 1.27.1 | 1.27.3 | VictoriaLogs 06d56702 |
| victoria-traces | v0.12.1 (7b13fef2f) | 1.27.2 | 1.27.3 | VictoriaTraces 8586f7c8 |

## Grafana

`Dockerfile.grafana` starts from upstream's Grafana OSS image, pinned by digest, and changes six things
(the sixth, the server rebuild, is described under the table):

- `victoriametrics-logs-datasource` is baked in from its release zip (pinned version, sha256-verified)
  into `/usr/share/grafana/plugins`.
- Every bundled data source we do not use is removed. Jaeger stays, because it reads VictoriaTraces'
  `/select/jaeger`. The whole file system is one layer, so the removed files are in no layer (a second
  layer, from `WORKDIR`, is empty).
- The backends of the two data sources are rebuilt from their release tags with Go 1.27.4 and the
  dependency fixes upstream has on main but has not released (`patches/grafana-jaeger.patch`,
  `patches/grafana-vlogs.patch`), with the flags of upstream's own binaries. A rebuilt backend no
  longer matches its plugin's signature manifest, so the manifest is removed and these two plugins,
  and no others, are allowed to load unsigned. Because nothing then verifies their files at start,
  both plugin trees (`/usr/share/grafana/plugins` and `data/plugins-bundled`, where Jaeger lives) are
  root-owned and the Grafana user cannot write, rename or add anything in them.
- Alpine's `zlib` is moved to the fixed package (a pinned, sha256-verified file).
- Defaults, set as environment variables so that a mounted `grafana.ini` does not undo them: no usage
  reporting, no update checks for Grafana or plugins, no news feed, no plugin catalog, no preinstalled
  or run-time plugin installs, no gravatar, no external snapshots. Every grafana.com address the server
  reads (`[grafana_com] url` and `api_url`, `[grafana_net] url`) points at a closed loopback port, so
  the grafana.com proxy that any signed-in user can call (`/api/gnet/*`) cannot reach it. With these
  defaults the server made no DNS query and no outbound connection in a 4-minute probe that also
  called the avatar, grafana.com proxy, plugin install and external snapshot endpoints. A deployment
  that overrides these variables can turn the calls back on.

| Component | Upstream | Ours |
|---|---|---|
| Grafana server | v13.3.1 (5db6ba8876): Go 1.27.3, thrift v0.24.0 | Go 1.27.4, thrift v0.25.0 (upstream f9eeaa2a on release-13.3.2); frontend and the rest unchanged, non-root as upstream (uid 472) |
| victoriametrics-logs-datasource | v0.34.0 (a5450dee43): Go 1.27.2, x/net v0.60.0, compress v1.19.2 | Go 1.27.4, x/net v0.62.0, compress v1.20.4 (upstream 2d682142, 12c4778f on main) |
| Jaeger data source | v13.0.8 (7bf6c164d5): Go 1.27.3, x/net v0.61.0 | Go 1.27.4, x/net v0.62.0 (upstream cb0d47d1 on main) |
| zlib (Alpine 3.24) | 1.3.2-r1 | 1.4-r0 |

Grafana's own server binary is rebuilt too. Upstream's v13.3.1 binary carries
`github.com/apache/thrift` v0.24.0, which the scan gate reports. The server is built from the public
v13.3.1 tag with one source change, `github.com/apache/thrift` v0.25.0 (`patches/grafana-server.patch`),
with upstream's build settings and Go 1.27.4, and replaces the binary in upstream's image. The build
fails unless the result is built by that Go, from the same modules, at the same versions, as upstream's
binary apart from that one.
`grafana server -v` reports `Version 13.3.1 (commit: <the tag's commit>, branch: v13.3.1-fc-thrift-v0.25.0)`.
Upstream's published binary reports a commit that is not in the public repository
(`branch: release-13.3.1#patched`). The public tag is the same source: built unpatched with Go 1.27.3
and upstream's own version stamps, it is byte-identical to upstream's binary (checked 2026-11-18), and
its release commit names the security patches it applies.

Drop the server rebuild when the recipe moves to a Grafana release, 13.3.2 or later, that ships thrift
v0.25.0 or later: delete the `server` stage and `patches/grafana-server.patch`. The weekly
`upstream-watch` reads the new release's `go.mod` and says whether to drop or keep it. It also reports
Grafana's out-of-band security releases (`v13.3.1+security-01`) as newer than their base.

Go back to a plugin's own signed backend when its upstream releases one with the fixes.

Two clean builds of one commit give the same image ID: the build stamps are fixed, layer timestamps
are the commit time, and `apk` is told not to write its time-stamped log line.

Known limits:

- The `zlib` pin is a file on Alpine's CDN, which keeps only the current package version. When Alpine
  supersedes 1.4-r0 the URL returns 404 and the Grafana build fails until it is re-pinned. Nothing
  watches for that.
- A clean Grafana build left about 17.7 GB of BuildKit state in one local measurement, and the
  scanners need about 5 GB of databases. GitHub documents 14 GB of disk for the public
  `ubuntu-24.04` runner, so the `grafana` leg may not fit; CI has not run it yet.
- The upstream watch reads only the `require` lines of the new release's root `go.mod`. A `replace`
  of thrift back to an older version would still read as "drop".
- A `+security-NN` release cannot be the image tag as written, because `+` is not allowed in one.
- The Prometheus, Loki, Tempo and other bundled data sources are absent. Grafana logs two errors at
  start about the missing Prometheus plugin.

## Layout

- `images/<app>.env`: the definition (upstream repo, tag, commit, Go version, our `FC_REVISION`).
  A definition may name its own `DOCKERFILE` and `SMOKE` test.
- `patches/<app>.patch`: the only source change, the `go` line in `go.mod` and `GO_BUILDER_IMAGE` in
  `deployment/docker/Makefile`, as in upstream's fix commit. `patches/grafana-*.patch` change only
  module versions in `go.mod` and their `go.sum` lines.
- `Dockerfile`: upstream's linux/amd64 build (`-trimpath`, static, `netgo osusergo`, CGO on as upstream
  does for amd64, same distroless runtime pin, same binary path), with the builder and runtime pinned by
  digest. The build checks the upstream commit, the patch and the resulting Go version.
- `scripts/build.sh` builds; `scripts/scan.sh` runs trivy and grype; `scripts/gate.py` fails on any
  HIGH or CRITICAL finding that has a fix, from either scanner, with no ignore list; `scripts/smoke.sh`
  runs the image and checks `/health`, the version string and `go version -m`;
  `scripts/smoke-grafana.sh` runs Grafana with no network and checks health, the plugins, the defaults
  and that nothing tried to reach the outside; `scripts/check_pins.py` fails on a base image without a
  digest or a download without a checksum; `scripts/upstream_watch.py` reports a newer upstream release.

## Local use

    docker buildx create --name store-images --driver docker-container --use
    scripts/build.sh victoria-logs archive ~/tmp/store-images/victoria-logs.tar
    scripts/scan.sh victoria-logs ~/tmp/store-images/victoria-logs.tar ~/tmp/store-images/scan-vl
    scripts/smoke.sh victoria-logs ~/tmp/store-images/victoria-logs.tar
    python3 -m unittest discover -s tests

## CI

- Pull requests: unit tests and the pin check, then build, scan gate and smoke for every image. No
  push, no secrets.
- Push to `main`: the same, then publish. The image is pushed by digest with SBOM and provenance
  attestations, its linux/amd64 manifest is checked against the one that was scanned, and only then is
  it tagged. An existing tag is never overwritten; bump `FC_REVISION` to publish a new build.
- Weekly `upstream-watch`: fails if upstream has a stable release newer than our base tag. That is the
  signal to re-pin consumers to upstream's image (check that its Go is fixed) and archive this repo.
  For Grafana it also watches the two data sources, and a newer release means re-pin and rebuild.

Consumers pin by digest. The VictoriaLogs and VictoriaTraces packages must be public for anonymous
pulls; the Grafana package is private and is pulled with a pull secret.
