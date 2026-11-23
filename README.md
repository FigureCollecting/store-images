# store-images

Stopgap rebuild of the VictoriaTraces container image used by FigureCollecting. The image is built from
an upstream release tag with two changes: the Go toolchain is moved to the current patch release, and one
module is moved to its fixed version (go.mod and go.sum only). Nothing else is changed.

The image is published as `ghcr.io/figurecollecting/victoria-traces`, tagged `<upstream tag>-fc<N>`.

This repository will be archived once upstream publishes releases built with the fixed versions
and it holds no other image we use. At that point, consumers pin upstream's own images again.

It also holds the recipe for our Grafana image, `ghcr.io/figurecollecting/grafana` (see
[Grafana](#grafana)). The recipe is public; the image is internal today; see [CI](#ci), last paragraph.

A third recipe rebuilds Tailscale's container image, `ghcr.io/figurecollecting/tailscale` (see
[Tailscale](#tailscale)), until an upstream release passes the scan gate.

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

## Grafana

`Dockerfile.grafana` starts from upstream's Grafana OSS image, pinned by digest, and changes seven things
(the seventh, the server rebuild, is described under the table):

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
- Alpine's `zlib`, `libcrypto3` and `libssl3` are moved to the fixed packages (pinned, sha256-verified files).
- Defaults, set as environment variables so that a mounted `grafana.ini` does not undo them: no usage
  reporting, no update checks for Grafana or plugins, no news feed, no plugin catalog, no preinstalled
  or run-time plugin installs, no gravatar, no external snapshots, no Grafana Cloud Migration, and the
  three grafana.com addresses at a closed loopback port. [Outbound settings](#outbound-settings) lists
  each one. A deployment that overrides these variables can turn the calls back on.
- The entrypoint `/run.sh`, which upstream ships writable by group 0 (the Grafana user's group), is
  mode 0755, and `/etc/grafana/provisioning`, `/home/grafana` and `/usr/share/grafana/.aws` are
  root-owned. Outside `/var/lib/grafana`, `/var/log/grafana`, `/tmp` and `/var/tmp`, the Grafana user
  owns nothing and can write nothing; the build and the smoke test both fail otherwise. Without this,
  code running as the Grafana user could rewrite the entrypoint and load a plugin of its own at the
  next start. The deployment must still set `readOnlyRootFilesystem: true`, with volumes for those
  four paths, as a second barrier (the image starts and both plugins work that way).

| Component | Upstream | Ours |
|---|---|---|
| Grafana server | v13.3.1 (5db6ba8876): Go 1.27.3, thrift v0.24.0 | Go 1.27.4, thrift v0.25.0 (upstream f9eeaa2a on release-13.3.2); frontend and the rest unchanged, non-root as upstream (uid 472) |
| victoriametrics-logs-datasource | v0.34.0 (a5450dee43): Go 1.27.2, x/net v0.60.0, compress v1.19.2 | Go 1.27.4, x/net v0.62.0, compress v1.20.4 (upstream 2d682142, 12c4778f on main) |
| Jaeger data source | v13.0.8 (7bf6c164d5): Go 1.27.3, x/net v0.61.0 | Go 1.27.4, x/net v0.62.0 (upstream cb0d47d1 on main) |
| zlib (Alpine 3.24) | 1.3.2-r1 | 1.4-r0 |
| libcrypto3 (Alpine 3.24) | 3.5.9-r0 | 3.5.9-r1 |
| libssl3 (Alpine 3.24) | 3.5.9-r0 | 3.5.9-r1 |

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

Two clean builds of one commit give the same image ID: the build stamps are fixed, file times later
than the commit time are set to it (earlier ones are kept), and `apk` is told not to write its
time-stamped log line.

### Outbound settings

Every setting in Grafana 13.3's `conf/defaults.ini` that can make the server call out by itself or on
a user's request, and its value in this image. "Ours" rows are set in `Dockerfile.grafana` and read
back by the smoke test.

| Setting | Upstream default | Reaches | This image |
|---|---|---|---|
| `[cloud_migration] enabled` | `true` | `cms-<cluster>.grafana.net`, or any host named in a token a server admin pastes | ours: `GF_CLOUD_MIGRATION_ENABLED=false`; the endpoints return 404 |
| `[provisioning] enabled` | `true` | Git Sync: `github.com`, or any host an admin names in a repository URL | ours: `GF_PROVISIONING_ENABLED=false`; its endpoints return 404. Provisioning files mounted into `/etc/grafana/provisioning` still load |
| `[grafana_com] url` | `https://grafana.com` | grafana.com sign-in and sign-out | ours: `GF_GRAFANA_COM_URL=http://127.0.0.1:9` |
| `[grafana_com] api_url` | `https://grafana.com/api` | the `/api/gnet/*` proxy (any signed-in user), plugin catalog, installs and update checks, signing-key download, Cloud Migration's token calls | ours: `GF_GRAFANA_COM_API_URL=http://127.0.0.1:9/api` |
| `[grafana_net] url` | `https://grafana.com` | older name of `[grafana_com] url` | ours: `GF_GRAFANA_NET_URL=http://127.0.0.1:9` |
| `[analytics] reporting_enabled` | `true` | `stats.grafana.org` (address fixed in the code) | ours: `GF_ANALYTICS_REPORTING_ENABLED=false` |
| `[analytics] check_for_updates` | `true` | `grafana.com/api/grafana/versions` (address fixed in the code) | ours: `GF_ANALYTICS_CHECK_FOR_UPDATES=false` |
| `[analytics] check_for_plugin_updates` | `true` | plugin versions and the Angular pattern list, at `api_url` | ours: `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false` |
| `[analytics] feedback_links_enabled` | `true` | links in the UI only | ours: `GF_ANALYTICS_FEEDBACK_LINKS_ENABLED=false` |
| `[news] news_feed_enabled` | `true` | `grafana.com/blog/news.xml`, fetched by the browser | ours: `GF_NEWS_NEWS_FEED_ENABLED=false` |
| `[plugins] preinstall_disabled` | `false` | installs Grafana's default plugin list from `api_url` at start | ours: `GF_PLUGINS_PREINSTALL_DISABLED=true` |
| `[plugins] plugin_admin_enabled` | `true` | catalog browsing and installs from `api_url` | ours: `GF_PLUGINS_PLUGIN_ADMIN_ENABLED=false` |
| `[plugins] public_key_retrieval_disabled` | `false` | signing-key download from `api_url` | ours: `GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED=true` |
| `[security] disable_gravatar` | `false` | `secure.gravatar.com` (`gravatar_url`), through the server's `/avatar` proxy | ours: `GF_SECURITY_DISABLE_GRAVATAR=true` |
| `[snapshots] external_enabled` | `true` | `snapshots.raintank.io` (`external_snapshot_url`) | ours: `GF_SNAPSHOTS_EXTERNAL_ENABLED=false` |
| `[plugins] plugin_catalog_url` | grafana.com | a link in the UI only | upstream's |
| `[secretscan] enabled` | `false` | `secret-scanning.grafana.net` (`base_url`) | upstream's: off |
| `[auth.grafana_com]`, `[auth.grafananet] enabled` | `false` | grafana.com sign-in | upstream's: off |
| `[feature_toggles.openfeature] provider`, `url` | `static`, empty | a feature-flag service | upstream's: none; the image sets no feature toggle |
| `[server] cdn_url` | empty | a CDN for the frontend files (browser) | upstream's: none |
| `[analytics]` trackers (Google Analytics, Tag Manager, RudderStack, PostHog, Application Insights) | empty | the vendor, from the browser | upstream's: none |
| `[log.frontend] enabled` | `false` | frontend logs, to this server unless pointed elsewhere | upstream's: off |
| `[tracing.*] address`, `[rendering] server_url`, `[external_image_storage] provider`, `[remote.alertmanager] url`, `[smtp] enabled`, `[unified_alerting] ha_peers` | empty or `false` | the host the operator names | upstream's: none |

Two more image defaults, not outbound switches: `GF_PATHS_PLUGINS` is the baked-in read-only directory,
and `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS` names the two rebuilt plugins.

Data sources, alert contact points and sign-in providers reach the hosts an admin gives them; that is
their purpose and no default covers it. Git Sync is the exception, and it is off.

With these defaults the server made no DNS query and no outbound connection in a run that called the
avatar, grafana.com proxy, plugin install, external snapshot, Cloud Migration and Git Sync endpoints as
admin and then opened 54 pages of the UI in a browser (checked 2026-11-19 on the built image).

Known limits:

- The operator's browser still fetches from third parties in at least three cases, all upstream
  behaviour. The VictoriaLogs plugin's page (`/plugins/victoriametrics-logs-datasource`) shows its
  README, whose 20 images load from github.com. Upstream's frontend bundles FingerprintJS, which
  reports to `openfpcdn.io` on some page loads (the code draws a random number; two hits were seen in
  about six 54-page walks). A Geomap panel on its default base layer loads map tiles from
  `basemaps.cartocdn.com`. Other panels may name other hosts. A content security policy that limits
  `img-src` and `connect-src` to `'self'` should stop these; it is not set here and not tested.
- The server-side check ran with a watcher on the container's sockets (any address, any port) as well as a
  DNS and TCP sink; the earlier runs saw only DNS and ports 80 and 443.
- The build guard that finds paths the Grafana user could write skips symlinks, so a root-owned symlink
  in `PATH` that points into the data volume would pass. The image has none.
- `GF_INSTALL_PLUGINS` with `GF_INSTALL_PLUGINS_FORCE` runs `grafana cli plugins install`, whose
  repository address is fixed in the code at `https://grafana.com/api/plugins`, not `[grafana_com]
  api_url`. It needs the operator to set both, it is deprecated, and it fails on the read-only plugin
  directory.
- `GF_AWS_PROFILES` no longer works: the entrypoint cannot write `/usr/share/grafana/.aws/credentials`
  and stops. No bundled data source that reads it is left in the image.
- Provisioning files must be mounted; the Grafana user cannot create them in `/etc/grafana/provisioning`.
- `scripts/check_pins.py` reads a `DOCKERFILE=` line in `images/*.env` only when it starts the line
  (not `export DOCKERFILE=` or an indented one), and skips Dockerfiles under dot directories.
- The `zlib`, `libcrypto3` and `libssl3` pins are files on Alpine's CDN, which keeps only the current package
  version. When Alpine supersedes 1.4-r0 (or 3.5.9-r1) the URL returns 404 and the Grafana build fails until it is re-pinned. Nothing
  watches for that.
- A clean Grafana build left about 17.7 GB of BuildKit state in one local measurement, and the
  scanners need about 5 GB of databases. CI ran the `grafana` leg on the GitHub runner at `a3daa23`
  and it passed (scan gate, smoke 52/52). The leg takes about 10 minutes.
- The upstream watch reads only the `require` lines of the new release's root `go.mod`. A `replace`
  of thrift back to an older version would still read as "drop".
- A `+security-NN` release cannot be the image tag as written, because `+` is not allowed in one.
- The Prometheus, Loki, Tempo and other bundled data sources are absent. Grafana logs two errors at
  start about the missing Prometheus plugin.

## Tailscale

`Dockerfile.tailscale` starts from upstream's `tailscale/tailscale` image of a release tag, pinned by
digest, and changes three things. The image is published as
`ghcr.io/figurecollecting/tailscale:v1.104.7-fc1`.

| Component | Upstream v1.104.7 (cbc74fe48e) | Ours | Fixes |
|---|---|---|---|
| `golang.org/x/net` in `tailscale`, `tailscaled`, `containerboot` | v0.60.0 | `golang.org/x/net` v0.62.0 (upstream d064d751 on main) | CVE-2026-78661 |
| `github.com/klauspost/compress` in `tailscale`, `tailscaled` | v1.20.0 | `github.com/klauspost/compress` v1.20.2 (upstream 46f93879 on main) | CVE-2026-116591 |
| zlib (Alpine 3.22) | 1.3.2-r1 | zlib 1.4-r0 (a pinned, sha256-verified file) | CVE-2026-4738 |

Upstream's v1.104.7 image fails the scan gate on these three (checked 2026-11-23: trivy reports the
first, grype the other two). Both module versions are the ones upstream's own v1.106.0 tag ships;
`release-branch/1.104` had neither on that day.

The three binaries are built from the clean tag plus `patches/tailscale.patch` (the two module lines in
`go.mod` and their `go.sum` lines) with upstream's toolchain, Tailscale's fork of Go at the revision
the tag's `go.toolchain.rev` names (a pinned, sha256-verified download), and with the build tags, CGO
setting, `-trimpath` and version stamps of upstream's container build. Built this way without the
patch, the three binaries are byte-identical to the ones in upstream's image (checked 2026-11-23). The
build fails unless each patched binary differs from upstream's only in the two modules and in the two
marks Go puts on a build from a changed tree: `go version -m` reports the module as `v1.104.7+dirty`
and `vcs.modified=true`, because `go.mod` and `go.sum` are changed.

Everything else is upstream's: the Alpine base and its other packages, the iptables links, the command
(`/usr/local/bin/containerboot`), no entrypoint and no user. Like upstream's image it runs as root
unless the deployment sets a user, so a deployment written for upstream's image needs only the image
line changed. The version stamps are upstream's too: the short and long versions are v1.104.7's
(`1.104.7-tcbc74fe48`), and `tailscale version` prints upstream's commit with `-dirty` appended, which
Tailscale adds by itself to a build from a changed tree. That mark, the label set and `go version -m` on
a binary tell the two images apart. The whole file system is one layer, so the replaced binaries and the
old zlib are in no layer.

The smoke test gives no container a network or a key, so it joins no tailnet. It compares the image
with upstream's (config, version output, and every path of the file system), reads the three
binaries' build information, and starts `containerboot` as our deployments run it (uid 10001,
read-only root, no capabilities, userspace networking) until `tailscale status` answers `NeedsLogin`.
It does not exercise a tunnel, the outbound proxy or `tailscale serve`; those need a tailnet.

Delete the recipe when an upstream release passes the gate: the weekly `upstream-watch` fails on a
newer stable release and asks for `scripts/scan.sh` on upstream's image of it. If it passes, re-pin
consumers to upstream's image and delete `images/tailscale.env`, `Dockerfile.tailscale`,
`patches/tailscale.patch` and `scripts/smoke-tailscale.sh`. Upstream tagged v1.106.0, with both module
changes, on 2026-11-23; its image was not published yet when this recipe was written.

Known limits:

- The zlib pin is a file on Alpine's CDN, which keeps only the current package version. When Alpine
  supersedes 1.4-r0 in v3.22 the URL returns 404 and the build fails until it is re-pinned.
- `apk add` of the file records the package in `/etc/apk/world`, pinned to that file's checksum. A
  later `apk upgrade` inside the image, which nothing here runs, would refuse to move zlib.
- The Tailscale toolchain is a release asset of `github.com/tailscale/go`, verified by checksum only.

## Layout

- `images/<app>.env`: the definition (upstream repo, tag, commit and image, upstream's Go, our Go, the
  module and version the patch moves, our `FC_REVISION`). A definition may name its own `DOCKERFILE`
  and `SMOKE` test.
- `watch/<app>.env`: an image we only watch (upstream repo and the tag the store runs); never built.
- `patches/<app>.patch`: the only source change, the module's line in `go.mod` and its two `go.sum` lines.
  `patches/grafana-*.patch` likewise change only module versions in `go.mod` and their `go.sum` lines.
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
  `scripts/smoke-grafana.sh` runs Grafana with no network and checks health, the plugins, the defaults
  and that nothing tried to reach the outside; `scripts/smoke-tailscale.sh` runs Tailscale with no
  network and compares it with upstream's image, using `scripts/rootfs_diff.py` to name every path
  that differs between the two file systems; `scripts/check_pins.py` fails on a base image without a
  digest or a download without a checksum; `scripts/upstream_watch.py` reports a newer upstream release.

## Local use

    docker buildx create --name store-images --driver docker-container --use
    scripts/build.sh victoria-traces archive ~/tmp/store-images/victoria-traces.tar
    scripts/scan.sh victoria-traces ~/tmp/store-images/victoria-traces.tar ~/tmp/store-images/scan-vt
    scripts/smoke.sh victoria-traces ~/tmp/store-images/victoria-traces.tar
    python3 -m unittest discover -s tests

## CI

- Pull requests: unit tests and the pin check, then build, scan gate and smoke for every image. No
  push, no secrets.
- Push to `main`: the same, then publish. The image is pushed by digest with SBOM and provenance
  attestations, its linux/amd64 manifest is checked against the one that was scanned, and only then is
  it tagged. An existing tag is never overwritten; bump `FC_REVISION` to publish a new build.
- Weekly `upstream-watch`: fails if upstream has a stable release newer than our base tag (`images/` and `watch/`). For
  an `images/` entry that is the signal to check the new release's Go and modules, then either re-pin consumers
  to upstream's image and archive this repo, or move the recipe to the new tag. For a `watch/` entry, check its
  image's scan, re-pin the store and bump the tag in `watch/`; the rebuilds in `images/` stay.
  For Grafana it also watches the two data sources, and a newer release means re-pin and rebuild.
  For Tailscale a newer release means scan upstream's image and, if it passes, delete the recipe.

Consumers pin by digest. The victoria-traces and grafana packages are internal today; the cluster can pull them only
after they are made public or a pull secret is added, which is decided before either image is deployed.
The tailscale package is created by its first publish from `main` and needs the same decision.
