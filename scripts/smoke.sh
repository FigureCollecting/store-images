#!/usr/bin/env bash
# Run one image on its own docker network and prove it is what images/<app>.env says it is.
#
#   scripts/smoke.sh <app> <image.tar | image-ref>
#
# Checks: the server starts and /health answers OK; `-version` and /metrics report
# <app>-<date>-<UPSTREAM_DESCRIBE>-<FC_REVISION>; `go version -m` on the extracted binary reports
# go<GO_VERSION> with upstream's build settings. Exit 0 only if every check passes.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE_IMAGE=docker.io/library/busybox:1.37.0-musl@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092
GO_IMAGE=$(sed -n 's/^ARG GO_IMAGE=//p' "$ROOT/Dockerfile")
[ $# -eq 2 ] || { echo "usage: $0 <app> <image.tar | image-ref>" >&2; exit 2; }
app=$1 target=$2
[ -f "$ROOT/images/$app.env" ] || { echo "no image definition images/$app.env" >&2; exit 2; }
# shellcheck source=/dev/null
. "$ROOT/images/$app.env"
name="store-images-smoke-$app"
work=$(mktemp -d)
cleanup() {
  docker rm -f "$name" "$name-bin" >/dev/null 2>&1
  docker network rm "$name" >/dev/null 2>&1
  rm -rf "$work"
}
trap cleanup EXIT

if [ -f "$target" ]; then
  img=$(docker load -i "$target" | sed -n 's/^Loaded image: //p' | tail -1)
  [ -n "$img" ] || { echo "FAIL  docker load of $target named no image"; exit 1; }
else
  img=$target
fi

failures=0
check() {  # check <name> <detail> <command...>: PASS if the command succeeds (stdin passes through)
  local name=$1 detail=$2; shift 2
  if "$@" >/dev/null 2>&1; then echo "PASS  $name: $detail"; else echo "FAIL  $name: $detail"; failures=$((failures + 1)); fi
}
metric_ok() { grep -qF "version=\"$version\"" <<<"$metric" && grep -qF "short_version=\"$UPSTREAM_TAG\"" <<<"$metric"; }
probe() { docker run --rm --network "$name" "$PROBE_IMAGE" wget -qO- -T 2 "http://$name:$HTTP_PORT$1" 2>/dev/null; }

docker network create "$name" >/dev/null
docker run -d --name "$name" --network "$name" "$img" >/dev/null

health=""
for _ in $(seq 1 30); do
  health=$(probe /health) && [ "$health" = OK ] && break
  sleep 1
done
check health "GET :$HTTP_PORT/health -> '${health}'" test "$health" = OK

want="^${APP_NAME}-[0-9]{8}-[0-9]{6}-${UPSTREAM_DESCRIBE}-${FC_REVISION}\$"
version=$(docker run --rm --network none "$img" -version 2>&1)
check version "-version -> '$version'" grep -qE "$want" <<<"$version"

metric=$(probe /metrics | grep '^vm_app_version{')
check metrics "${metric:-no vm_app_version}" metric_ok

docker create --name "$name-bin" "$img" >/dev/null
docker cp -q "$name-bin:/$APP_NAME-prod" "$work/" 2>/dev/null
docker run --rm --network none -v "$work:/b:ro" "$GO_IMAGE" go version -m "/b/$APP_NAME-prod" >"$work/buildinfo" 2>&1
grep -vE '^[[:space:]]+dep[[:space:]]' "$work/buildinfo" | sed 's/^/      /'
for want in "^/b/$APP_NAME-prod: go$GO_VERSION\$" "path[[:space:]]+.*/app/$APP_NAME\$" "build[[:space:]]+CGO_ENABLED=1\$" \
            "build[[:space:]]+-tags=netgo,osusergo\$" "build[[:space:]]+-trimpath=true\$" "build[[:space:]]+GOARCH=amd64\$"; do
  check buildinfo "$want" grep -qE "$want" "$work/buildinfo"
done

[ "$failures" = 0 ] || { echo "--- container log (tail)"; docker logs "$name" 2>&1 | tail -15; }
echo "SMOKE $([ "$failures" = 0 ] && echo PASS || echo "FAIL ($failures)") $app $img"
[ "$failures" = 0 ]
