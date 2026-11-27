#!/usr/bin/env bash
# Run the node-npm image with NO network and prove it is what images/node-npm.env says it is.
#
#   scripts/smoke-node-npm.sh node-npm <image.tar | image-ref>
#
# Every container gets --network none. Checks: the image config is upstream's (read from the registry by digest,
# nothing pulled or run: same entrypoint and command, no user, same environment); the labels name the build; node is
# the upstream's release; npm is NPM_VERSION and the global modules are npm alone (no corepack); every brace-expansion
# bundled in that npm is at least BRACE_EXPANSION_MIN; yarn still runs; apk leaves no package cache; and npm and node
# run a small offline job as an unprivileged user on a read-only root.
# Exit 0 only if every check passes.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ $# -eq 2 ] || { echo "usage: $0 <app> <image.tar | image-ref>" >&2; exit 2; }
app=$1 target=$2
[ -f "$ROOT/images/$app.env" ] || { echo "no image definition images/$app.env" >&2; exit 2; }
# shellcheck source=/dev/null
. "$ROOT/images/$app.env"

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
# A job as the deployments run it: unprivileged, read-only root, no capabilities, no network. HOME and /tmp are writable.
job() { docker run --rm --network none --user 1000:1000 --read-only --cap-drop ALL --security-opt no-new-privileges --memory 512m --tmpfs /tmp -e HOME=/tmp "$img" "$@"; }

# The image config is upstream's: a deployment written for upstream's image runs this one unchanged.
ours=$(docker image inspect --format '{{json .Config}}' "$img" | jq -cS "$normalize")
theirs=$(docker buildx imagetools inspect "$UPSTREAM_IMAGE" --format '{{json .Image}}' | jq -cS '."linux/amd64".config | '"$normalize")
check config "ours ${ours:-none}" test -n "$ours" -a "$ours" = "$theirs"
check config "entrypoint $(jq -c .Entrypoint <<<"$ours"), command $(jq -c .Cmd <<<"$ours"), user '$(jq -r .User <<<"$ours")'" \
  test "$(jq -c '[.Entrypoint, .Cmd, .User]' <<<"$ours")" = '[["docker-entrypoint.sh"],["node"],""]'

while read -r key want; do
  got=$(label "$key")
  check labels "$key = '${got}'" test "$got" = "$want"
done <<EOF
org.opencontainers.image.version $UPSTREAM_TAG-$FC_REVISION
org.opencontainers.image.base.name $UPSTREAM_IMAGE
com.figurecollecting.upstream.tag $UPSTREAM_TAG
com.figurecollecting.npm.version $NPM_VERSION
com.figurecollecting.brace-expansion.min $BRACE_EXPANSION_MIN
EOF

node_said=$(job node --version 2>&1)
check version "node --version -> '$node_said'" test "$node_said" = "$UPSTREAM_TAG"
npm_said=$(job npm --version 2>&1)
check version "npm --version -> '$npm_said'" test "$npm_said" = "$NPM_VERSION"
yarn_said=$(job yarn --version 2>&1)
check version "yarn --version -> '$yarn_said'" test "$yarn_said" = "1.22.22"

# corepack is gone: no command, no module; npm is the only global module.
modules=$(job ls /usr/local/lib/node_modules 2>&1 | tr '\n' ' ')
check corepack "global modules: '${modules}'" test "$modules" = "npm "
check corepack "no corepack command" test -z "$(job sh -c 'command -v corepack' 2>/dev/null)"

# Every brace-expansion bundled in npm is at least the floor (sort -V puts the smaller first).
versions=$(job sh -c 'for f in $(find /usr/local/lib/node_modules/npm -path "*/brace-expansion/package.json"); do node -p "require(\"$f\").version"; done' 2>&1)
check brace-expansion "versions in npm: '$(tr '\n' ' ' <<<"$versions")'" test -n "$versions"
for v in $versions; do
  check brace-expansion "$v >= $BRACE_EXPANSION_MIN" test "$(printf '%s\n%s\n' "$BRACE_EXPANSION_MIN" "$v" | sort -V | head -n 1)" = "$BRACE_EXPANSION_MIN"
done

# apk's records are there and its download cache is not.
packages=$(job apk info -v 2>&1 | grep -c .)
check packages "apk lists $packages packages" test "$packages" -gt 10
check packages "no apk download cache: '$(job sh -c 'ls /var/cache/apk 2>/dev/null | wc -l' 2>&1)'" test "$(job sh -c 'ls /var/cache/apk 2>/dev/null | wc -l' 2>&1)" = 0

# A small offline job: create a package, read a field, write and run a script, pack it.
out=$(job sh -c 'mkdir /tmp/job && cd /tmp/job && npm init -y >/dev/null 2>&1 && npm pkg set scripts.hello="node -p 6*7" >/dev/null && npm run -s hello && npm pack --dry-run --json 2>/dev/null | node -p "JSON.parse(require(\"fs\").readFileSync(0))[0].files.length"' 2>&1 | tr '\n' ' ')
check job "npm init, pkg set, run and pack offline -> '${out}'" test "$out" = "42 1 "

echo "SMOKE $([ "$failures" = 0 ] && echo PASS || echo "FAIL ($failures)") $app $img"
[ "$failures" = 0 ]
