#!/usr/bin/env bash
# Run one image on its own docker network and prove it is what images/<app>.env says it is.
#
#   scripts/smoke.sh <app> <image.tar | image-ref>
#
# Checks: the server starts and /health answers OK; it accepts a gzip-compressed OTLP/JSON span on
# /insert/opentelemetry/v1/traces and answers a Jaeger query for that trace; `-version` and /metrics report
# <app>-<date>-<UPSTREAM_DESCRIBE>-<FC_REVISION>; `go version -m` on the extracted binary reports
# go<GO_VERSION> with upstream's build settings and UPSTREAM_PATCH_MODULE at UPSTREAM_PATCH_VERSION.
# Exit 0 only if every check passes. Docker objects are named <SMOKE_PREFIX>-<app>[-bin]
# (default prefix store-images-smoke).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE_IMAGE=docker.io/library/busybox:1.37.0-musl@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092
GO_IMAGE=$(sed -n 's/^ARG GO_IMAGE=//p' "$ROOT/Dockerfile")
[ $# -eq 2 ] || { echo "usage: $0 <app> <image.tar | image-ref>" >&2; exit 2; }
app=$1 target=$2
[ -f "$ROOT/images/$app.env" ] || { echo "no image definition images/$app.env" >&2; exit 2; }
# shellcheck source=/dev/null
. "$ROOT/images/$app.env"
name="${SMOKE_PREFIX:-store-images-smoke}-$app"
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
  local label=$1 detail=$2; shift 2  # not "name": the command may read the global $name
  if "$@" >/dev/null 2>&1; then echo "PASS  $label: $detail"; else echo "FAIL  $label: $detail"; failures=$((failures + 1)); fi
}
metric_ok() { grep -qF "version=\"$version\"" <<<"$metric" && grep -qF "short_version=\"$UPSTREAM_TAG\"" <<<"$metric"; }
probe() { docker run --rm --network "$name" "$PROBE_IMAGE" wget -qO- -T 2 "http://$name:$HTTP_PORT$1" 2>/dev/null; }

docker network create --internal "$name" >/dev/null  # the containers reach each other and nothing else
docker run -d --name "$name" --network "$name" "$img" >/dev/null

health=""
for _ in $(seq 1 30); do
  health=$(probe /health) && [ "$health" = OK ] && break
  sleep 1
done
check health "GET :$HTTP_PORT/health -> '${health}'" test "$health" = OK

# One span, sent gzip-compressed as OTLP exporters do (the request body goes through klauspost/compress),
# then read back by trace ID once the store has flushed it.
trace_id=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
now=$(date +%s%N)
span="{\"resourceSpans\":[{\"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"stringValue\":\"$name\"}}]},\
\"scopeSpans\":[{\"scope\":{\"name\":\"smoke\"},\"spans\":[{\"traceId\":\"$trace_id\",\"spanId\":\"${trace_id:0:16}\",\
\"name\":\"smoke-write\",\"kind\":1,\"startTimeUnixNano\":\"$now\",\"endTimeUnixNano\":\"$((now + 1000000))\"}]}]}]}"
post_gzip() {  # post_gzip <path> <body>; curl, because busybox wget cuts a binary body at its first NUL byte
  docker run --rm --network "$name" "$GO_IMAGE" sh -c 'printf %s "$2" | gzip -c |
    curl -fsS --max-time 5 -H "Content-Type: application/json" -H "Content-Encoding: gzip" --data-binary @- "$1"' \
    _ "http://$name:$HTTP_PORT$1" "$2" >/dev/null 2>&1
}
check write "POST gzip OTLP/JSON span $trace_id to :$HTTP_PORT/insert/opentelemetry/v1/traces" \
  post_gzip /insert/opentelemetry/v1/traces "$span"
found=""
for _ in $(seq 1 30); do
  found=$(probe "/select/jaeger/api/traces/$trace_id") && grep -qF '"operationName":"smoke-write"' <<<"$found" && break
  sleep 1
done
check query "GET :$HTTP_PORT/select/jaeger/api/traces/$trace_id -> '$(cut -c1-120 <<<"$found")'" \
  grep -qF '"operationName":"smoke-write"' <<<"$found"
services=$(probe /select/jaeger/api/services)
check query "GET :$HTTP_PORT/select/jaeger/api/services -> '$services'" grep -qF "\"$name\"" <<<"$services"

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
            "build[[:space:]]+-tags=netgo,osusergo\$" "build[[:space:]]+-trimpath=true\$" "build[[:space:]]+GOARCH=amd64\$" \
            "dep[[:space:]]+${UPSTREAM_PATCH_MODULE}[[:space:]]+${UPSTREAM_PATCH_VERSION}[[:space:]]"; do
  check buildinfo "$want" grep -qE "$want" "$work/buildinfo"
done

[ "$failures" = 0 ] || { echo "--- container log (tail)"; docker logs "$name" 2>&1 | tail -15; }
echo "SMOKE $([ "$failures" = 0 ] && echo PASS || echo "FAIL ($failures)") $app $img"
[ "$failures" = 0 ]
