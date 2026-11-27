#!/usr/bin/env bash
# Run the valkey image with NO network and prove it is what images/valkey.env says it is.
#
#   scripts/smoke-valkey.sh valkey <image.tar | image-ref>
#
# Every container gets --network none. Checks: the image config is upstream's (read from the registry by digest,
# nothing pulled or run); the labels name the build; valkey-server and valkey-cli are UPSTREAM_TAG; apk leaves no
# package cache; and a throwaway server, started and removed by its recorded container id, answers PING with PONG,
# stores and reads back a key, and logs no crash.
# Exit 0 only if every check passes.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ $# -eq 2 ] || { echo "usage: $0 <app> <image.tar | image-ref>" >&2; exit 2; }
app=$1 target=$2
[ -f "$ROOT/images/$app.env" ] || { echo "no image definition images/$app.env" >&2; exit 2; }
# shellcheck source=/dev/null
. "$ROOT/images/$app.env"
cid=""
cleanup() { [ -z "$cid" ] || docker rm -f "$cid" >/dev/null 2>&1; }
trap cleanup EXIT

if [ -f "$target" ]; then
  img=$(docker load -i "$target" | sed -n 's/^Loaded image: //p' | tail -1)
  [ -n "$img" ] || { echo "FAIL  docker load of $target named no image"; exit 1; }
else
  img=$target
fi

failures=0
check() {  # check <name> <detail> <command...>: PASS if the command succeeds
  local label=$1 detail=$2; shift 2
  if "$@" >/dev/null 2>&1; then echo "PASS  $label: $detail"; else echo "FAIL  $label: $detail"; failures=$((failures + 1)); fi
}
normalize='{Cmd, Entrypoint, User: (.User // ""), WorkingDir: (.WorkingDir // ""), Env, ExposedPorts, Volumes}'
label() { docker image inspect --format "{{index .Config.Labels \"$1\"}}" "$img" 2>/dev/null; }
tool() { docker run --rm --network none --entrypoint "$1" "$img" "${@:2}"; }  # one tool, no server, no network

ours=$(docker image inspect --format '{{json .Config}}' "$img" | jq -cS "$normalize")
theirs=$(docker buildx imagetools inspect "$UPSTREAM_IMAGE" --format '{{json .Image}}' | jq -cS '."linux/amd64".config | '"$normalize")
check config "ours ${ours:-none}" test -n "$ours" -a "$ours" = "$theirs"
check config "entrypoint $(jq -c .Entrypoint <<<"$ours"), command $(jq -c .Cmd <<<"$ours"), port $(jq -c .ExposedPorts <<<"$ours")" \
  test "$(jq -c '[.Entrypoint, .Cmd, .ExposedPorts, .WorkingDir]' <<<"$ours")" = '[["tini","--","docker-entrypoint.sh"],["valkey-server"],{"6379/tcp":{}},"/data"]'

while read -r key want; do
  got=$(label "$key")
  check labels "$key = '${got}'" test "$got" = "$want"
done <<EOF
org.opencontainers.image.version $UPSTREAM_TAG-$FC_REVISION
org.opencontainers.image.base.name $UPSTREAM_IMAGE
com.figurecollecting.upstream.tag $UPSTREAM_TAG
EOF

said=$(tool valkey-server --version 2>&1)
check version "valkey-server --version -> '$said'" grep -qE "^Valkey server v=$UPSTREAM_TAG " <<<"$said"
said=$(tool valkey-cli --version 2>&1)
check version "valkey-cli --version -> '$said'" grep -qE "^valkey-cli $UPSTREAM_TAG\$" <<<"$said"
cache=$(tool sh -c 'ls /var/cache/apk 2>/dev/null | wc -l' 2>&1)
check packages "no apk download cache: '$cache'" test "$cache" = 0

# A throwaway server on no network, removed by the id recorded here.
cid=$(docker run -d --network none --memory 256m "$img")
check boot "container started: ${cid:0:12}" test -n "$cid"
pong=""
for _ in $(seq 1 30); do
  pong=$(docker exec "$cid" valkey-cli ping 2>/dev/null) && [ "$pong" = PONG ] && break
  sleep 1
done
check boot "valkey-cli ping -> '${pong}'" test "$pong" = PONG
check boot "set and get a key: '$(docker exec "$cid" sh -c 'valkey-cli set smoke ok >/dev/null && valkey-cli get smoke' 2>&1)'" \
  test "$(docker exec "$cid" sh -c 'valkey-cli set smoke ok >/dev/null && valkey-cli get smoke' 2>&1)" = ok
info=$(docker exec "$cid" valkey-cli info server 2>&1)
check boot "server reports valkey_version:$UPSTREAM_TAG" grep -q "^valkey_version:$UPSTREAM_TAG" <<<"$info"
pid1=$(docker exec "$cid" sh -c 'tr "\0" " " </proc/1/cmdline' 2>/dev/null)
check boot "process 1: '${pid1}'" grep -q '^tini ' <<<"$pid1"
crashes=$(docker logs "$cid" 2>&1 | grep -ciE 'panic|crashed|bug report|segmentation')
check boot "$crashes crash lines logged" test "$crashes" = 0
running=$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null)
check boot "the container is still running: '${running}'" test "$running" = true

echo "SMOKE $([ "$failures" = 0 ] && echo PASS || echo "FAIL ($failures)") $app $img"
[ "$failures" = 0 ]
