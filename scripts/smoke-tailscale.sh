#!/usr/bin/env bash
# Run the Tailscale image with NO network and prove it is what images/tailscale.env says it is.
#
#   scripts/smoke-tailscale.sh tailscale <image.tar | image-ref>
#
# Every container gets --network none and no key, so nothing can reach a control server or join a tailnet.
# Checks: the image config is upstream's (same command, no entrypoint, no user, same PATH); the labels name
# the build; `tailscaled --version` and `tailscale version` print what upstream's image prints, with -dirty
# appended to the commit (the tree the build came from has a changed go.mod and go.sum); the
# three binaries are built by the pinned Go from the tag's commit with upstream's settings and carry the two
# moved modules at the pinned versions; zlib is the fixed package; against upstream's file system only the
# three binaries, zlib's library and apk's records differ; and containerboot starts tailscaled as the
# deployments run it (uid 10001, read-only root, no capabilities, userspace networking) and the daemon answers
# on its socket that it needs a login. containerboot has no --help: it reads its settings from TS_* variables.
# Needs upstream's image (UPSTREAM_IMAGE, pulled by digest) for the comparisons.
# Exit 0 only if every check passes.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GO_IMAGE=$(sed -n 's/^ARG GO_IMAGE=//p' "$ROOT/Dockerfile.tailscale")
ZLIB_APK=$(sed -n 's/^ARG ZLIB_APK=//p' "$ROOT/Dockerfile.tailscale")
[ $# -eq 2 ] || { echo "usage: $0 <app> <image.tar | image-ref>" >&2; exit 2; }
app=$1 target=$2
[ -f "$ROOT/images/$app.env" ] || { echo "no image definition images/$app.env" >&2; exit 2; }
# shellcheck source=/dev/null
. "$ROOT/images/$app.env"
name="${SMOKE_NAME:-${SMOKE_PREFIX:-store-images-smoke}-$app}"
work=$(mktemp -d)
cleanup() {
  docker rm -f "$name" "$name-ours" "$name-upstream" >/dev/null 2>&1
  chmod -R u+w "${work:?}" 2>/dev/null
  rm -rf "${work:?}"
}
trap cleanup EXIT

if [ -f "$target" ]; then
  img=$(docker load -i "$target" | sed -n 's/^Loaded image: //p' | tail -1)
  [ -n "$img" ] || { echo "FAIL  docker load of $target named no image"; exit 1; }
else
  img=$target
fi
docker image inspect "$UPSTREAM_IMAGE" >/dev/null 2>&1 || docker pull -q "$UPSTREAM_IMAGE" >/dev/null \
  || { echo "FAIL  cannot pull upstream's image $UPSTREAM_IMAGE"; exit 1; }

failures=0
check() {  # check <name> <detail> <command...>: PASS if the command succeeds
  local label=$1 detail=$2; shift 2
  if "$@" >/dev/null 2>&1; then echo "PASS  $label: $detail"; else echo "FAIL  $label: $detail"; failures=$((failures + 1)); fi
}
config() { docker image inspect --format '{{json .Config}}' "$1" | jq -cS '{Cmd, Entrypoint, User, WorkingDir, Env, ExposedPorts, Volumes}'; }
label() { docker image inspect --format "{{index .Config.Labels \"$1\"}}" "$img" 2>/dev/null; }

# The image config is upstream's: a deployment written for upstream's image runs this one unchanged.
ours=$(config "$img") theirs=$(config "$UPSTREAM_IMAGE")
check config "ours ${ours:-none}" test -n "$ours" -a "$ours" = "$theirs"
check config "command $(jq -c .Cmd <<<"$ours"), entrypoint $(jq -c .Entrypoint <<<"$ours"), user '$(jq -r '.User // ""' <<<"$ours")'" \
  test "$(jq -c '[.Cmd, .Entrypoint, (.User // "")]' <<<"$ours")" = '[["/usr/local/bin/containerboot"],null,""]'

while read -r key want; do
  got=$(label "$key")
  check labels "$key = '${got}'" test "$got" = "$want"
done <<EOF
org.opencontainers.image.version $UPSTREAM_TAG-$FC_REVISION
org.opencontainers.image.base.name $UPSTREAM_IMAGE
com.figurecollecting.upstream.tag $UPSTREAM_TAG
com.figurecollecting.upstream.commit $UPSTREAM_COMMIT
com.figurecollecting.tailscale-go.rev $TS_GO_REV
com.figurecollecting.x-net.version $XNET_VERSION
com.figurecollecting.compress.version $COMPRESS_VERSION
com.figurecollecting.zlib.package $ZLIB_APK
EOF

# The version stamps are upstream's: both tools print what upstream's image prints, with one mark. tailscale
# appends -dirty to the commit when the build's tree was modified, as go.mod and go.sum are.
for tool in "tailscaled --version" "tailscale version"; do
  # shellcheck disable=SC2086  # the tool and its one argument
  said=$(docker run --rm --network none "$img" $tool 2>&1)
  # shellcheck disable=SC2086
  upstream_said=$(docker run --rm --network none "$UPSTREAM_IMAGE" $tool 2>&1)
  want_said=${upstream_said/"  tailscale commit: $UPSTREAM_COMMIT"$'\n'/"  tailscale commit: $UPSTREAM_COMMIT-dirty"$'\n'}
  check version "$tool -> '$(tr -s ' \n' ' ' <<<"$said")'" test -n "$said" -a "$said" = "$want_said" -a "$said" != "$upstream_said"
  check version "$tool names ${UPSTREAM_TAG#v}, commit $UPSTREAM_COMMIT-dirty and tailscale/go ${TS_GO_REV:0:10}" \
    grep -qzP "(?s)^\Q${UPSTREAM_TAG#v}\E\n.*tailscale commit: $UPSTREAM_COMMIT-dirty\n.*go version: go\Q$GO_VERSION\E \(tailscale/go ${TS_GO_REV:0:10}\)" <<<"$said"
done

zlib=$(docker run --rm --network none "$img" apk info -v 2>/dev/null | grep '^zlib-')
check zlib "installed package '${zlib}'" test "$zlib" = "$(basename "$ZLIB_APK" .apk)"

# The three binaries are the rebuilt ones: upstream's Go, commit and settings, and the two moved modules.
docker create --network none --name "$name-ours" "$img" >/dev/null
docker create --network none --name "$name-upstream" "$UPSTREAM_IMAGE" >/dev/null
mkdir "$work/bin"
for b in tailscale tailscaled containerboot; do docker cp -q "$name-ours:/usr/local/bin/$b" "$work/bin/$b" 2>/dev/null; done
chmod -R a+rX "$work"
docker run --rm --network none -v "$work/bin:/b:ro" "$GO_IMAGE" go version -m /b/tailscale /b/tailscaled /b/containerboot >"$work/buildinfo" 2>&1
buildinfo() {  # buildinfo <binary> <pattern>: the pattern matches a line of that binary's section
  awk -v bin="/b/$1:" '$1 == bin { on = 1; print; next } /^\/b\// { on = 0 } on' "$work/buildinfo" >"$work/section"
  grep -qE "$2" "$work/section"  # from a file: grep -q on a pipe can end awk with SIGPIPE and fail the check
}
for b in tailscale tailscaled containerboot; do
  while read -r want; do
    check buildinfo "$b $want" buildinfo "$b" "$want"
  done <<EOF
^/b/$b: go$GO_VERSION\$
path[[:space:]]+tailscale.com/cmd/$b\$
mod[[:space:]]+tailscale.com[[:space:]]+${UPSTREAM_TAG}\+dirty[[:space:]]
dep[[:space:]]+golang.org/x/net[[:space:]]+${XNET_VERSION}[[:space:]]
build[[:space:]]+-tags=ts_kube,ts_package_container\$
build[[:space:]]+-trimpath=true\$
build[[:space:]]+CGO_ENABLED=0\$
build[[:space:]]+vcs.revision=$UPSTREAM_COMMIT\$
build[[:space:]]+vcs.modified=true\$
EOF
done
# containerboot does not link klauspost/compress; the two that do carry the pinned version and no other.
for b in tailscale tailscaled; do
  want="dep[[:space:]]+github.com/klauspost/compress[[:space:]]+${COMPRESS_VERSION}[[:space:]]"
  check buildinfo "$b $want" buildinfo "$b" "$want"
done
others=$(grep -E 'dep[[:space:]]+(golang.org/x/net|github.com/klauspost/compress)[[:space:]]' "$work/buildinfo" \
  | grep -cvE "(golang.org/x/net[[:space:]]+${XNET_VERSION}|github.com/klauspost/compress[[:space:]]+${COMPRESS_VERSION})[[:space:]]")
check buildinfo "$others lines carry either module at another version" test "$others" = 0

# Against upstream's file system, only the three binaries, zlib's library and apk's records may differ.
docker export "$name-ours" >"$work/ours.tar" 2>/dev/null
docker export "$name-upstream" >"$work/upstream.tar" 2>/dev/null
python3 "$ROOT/scripts/rootfs_diff.py" "$work/upstream.tar" "$work/ours.tar" >"$work/rootfs" 2>&1
rootfs_rc=$?
expected='^changed usr/local/bin/(tailscale|tailscaled|containerboot)$|^(added|removed|changed) (usr/)?lib/libz\.so\.[0-9.]+$|^changed (lib/apk/db/(installed|scripts\.tar|triggers)|etc/apk/world)$'
unexpected=$(grep -cvE "$expected" "$work/rootfs")
binaries=$(grep -cE '^changed usr/local/bin/(tailscale|tailscaled|containerboot)$' "$work/rootfs")
check rootfs "$(grep -c . "$work/rootfs") paths differ from upstream's image, $unexpected unexpected: $(tr '\n' ';' <"$work/rootfs" | cut -c1-600)" \
  test "$rootfs_rc" = 0 -a "$unexpected" = 0 -a "$binaries" = 3
check rootfs "zlib's library changed: $(grep -cE ' (usr/)?lib/libz\.so\.' "$work/rootfs") lines" grep -qE ' (usr/)?lib/libz\.so\.' "$work/rootfs"

# As the deployments run it: containerboot starts tailscaled, which answers on its socket and has no login.
docker run -d --name "$name" --network none --user 10001:10001 --read-only --cap-drop ALL --security-opt no-new-privileges --memory 512m --tmpfs /tmp --tmpfs /var/lib/tailscale:uid=10001,gid=10001,mode=0700 --tmpfs /var/run/tailscale:uid=10001,gid=10001,mode=0700 -e TS_USERSPACE=true -e TS_KUBE_SECRET= -e TS_SOCKET=/var/run/tailscale/tailscaled.sock -e TS_STATE_DIR=/var/lib/tailscale/state -e TS_AUTH_ONCE=true -e TS_HOSTNAME=smoke -e TS_NO_LOGS_NO_SUPPORT=true "$img" >/dev/null
status=""
for _ in $(seq 1 30); do
  status=$(docker exec "$name" tailscale --socket /var/run/tailscale/tailscaled.sock status --json 2>/dev/null) \
    && [ "$(jq -r .BackendState <<<"$status" 2>/dev/null)" = NeedsLogin ] && break
  sleep 1
done
state=$(jq -r '[.BackendState, .Version, (.TUN | tostring), (.Self.Online | tostring)] | join(" ")' <<<"$status" 2>/dev/null)
check boot "tailscale status: state, version, TUN, online = '${state:-no answer}'" \
  test "$state" = "NeedsLogin ${UPSTREAM_TAG#v}-t${UPSTREAM_COMMIT:0:9} false false"
pid1=$(docker exec "$name" sh -c 'tr "\0" " " </proc/1/cmdline; id -u' 2>/dev/null | tr '\n' ' ')
check boot "process 1 and its uid: '${pid1}'" test "$pid1" = "/usr/local/bin/containerboot 10001 "
docker logs "$name" >"$work/log" 2>&1
check boot "containerboot started tailscaled in userspace mode" grep -qE 'Program starting: v.*--tun=userspace-networking' "$work/log"
check boot "$(grep -cE '^panic:|fatal error:' "$work/log") panics or fatal errors logged" test "$(grep -cE '^panic:|fatal error:' "$work/log")" = 0
running=$(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null)
check boot "the container is still running: '${running}'" test "$running" = true

[ "$failures" = 0 ] || { echo "--- container log (tail)"; tail -15 "$work/log" 2>/dev/null | cut -c1-300; }
echo "SMOKE $([ "$failures" = 0 ] && echo PASS || echo "FAIL ($failures)") $app $img"
[ "$failures" = 0 ]
