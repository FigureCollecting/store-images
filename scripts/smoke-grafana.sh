#!/usr/bin/env bash
# Run the Grafana image with NO network and prove it is what images/grafana.env says it is.
#
#   scripts/smoke-grafana.sh grafana <image.tar | image-ref>
#
# The container gets --network none, so every probe runs inside it (docker exec) and any attempt to
# reach the outside shows up in its log as a failed dial. Checks: it runs as the non-root upstream
# user; /api/health is OK at the pinned version; the baked-in plugins are listed, the only external
# ones, and their backends answer and are the rebuilt binaries (`go version -m`); telemetry, update
# checks, the news feed, the plugin catalog and runtime plugin installs are off; and the log holds
# no outbound attempt.
# Exit 0 only if every check passes.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GO_IMAGE=$(sed -n 's/^ARG GO_IMAGE=//p' "$ROOT/Dockerfile.grafana")
[ $# -eq 2 ] || { echo "usage: $0 <app> <image.tar | image-ref>" >&2; exit 2; }
app=$1 target=$2
[ -f "$ROOT/images/$app.env" ] || { echo "no image definition images/$app.env" >&2; exit 2; }
# shellcheck source=/dev/null
. "$ROOT/images/$app.env"
name="${SMOKE_NAME:-store-images-smoke-$app}"
settle="${SMOKE_SETTLE:-20}"
work=$(mktemp -d)
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1
  rm -rf "${work:?}"
}
trap cleanup EXIT

if [ -f "$target" ]; then
  img=$(docker load -i "$target" | sed -n 's/^Loaded image: //p' | tail -1)
  [ -n "$img" ] || { echo "FAIL  docker load of $target named no image"; exit 1; }
else
  img=$target
fi

failures=0
check() {  # check <name> <detail> <command...>: PASS if the command succeeds
  local name=$1 detail=$2; shift 2
  if "$@" >/dev/null 2>&1; then echo "PASS  $name: $detail"; else echo "FAIL  $name: $detail"; failures=$((failures + 1)); fi
}
SMOKE_PW=$(tr -d - </proc/sys/kernel/random/uuid)  # a throwaway admin password, never printed
export SMOKE_PW
api() {  # api <path> [curl args...]: an admin request from inside the container
  local path=$1; shift
  # shellcheck disable=SC2016  # expanded by the shell inside the container
  docker exec -e SMOKE_PW "$name" sh -c 'p=$1; shift; curl -sS -m 10 -u "admin:$SMOKE_PW" "$@" "http://127.0.0.1:$HTTP_PORT$p"' \
    sh "$path" "$@" 2>/dev/null
}
is() { [ "$(jq -r "$2" <<<"$1" 2>/dev/null)" = "$3" ]; }  # is <json> <jq filter> <expected>

docker run -d --name "$name" --network none --memory 2g -e "HTTP_PORT=$HTTP_PORT" \
  -e GF_SECURITY_ADMIN_PASSWORD="$SMOKE_PW" "$img" >/dev/null

health=""
for _ in $(seq 1 90); do
  health=$(docker exec "$name" wget -qO- -T 2 "http://127.0.0.1:$HTTP_PORT/api/health" 2>/dev/null) \
    && is "$health" .database ok && break
  sleep 2
done
check health "GET :$HTTP_PORT/api/health -> $(tr -d ' \n' <<<"${health:-no answer}")" is "$health" .database ok
check version "/api/health version is ${UPSTREAM_TAG#v}" is "$health" .version "${UPSTREAM_TAG#v}"

user=$(docker image inspect --format '{{.Config.User}}' "$img" 2>/dev/null)
uid=$(docker exec "$name" id -u 2>/dev/null)
check non-root "image user '${user}', running uid '${uid}'" test "$user" = 472 -a "$uid" = 472

sleep "$settle"  # the update checker and the plugin installer run right after start
docker logs "$name" >"$work/log" 2>&1
outbound=$(grep -cE 'dial (tcp|udp)|lookup |no such host|network is unreachable|grafana\.(com|net)|raintank|gravatar' "$work/log")
check offline "${outbound} outbound attempts logged in the first ${settle}s with no network" test "$outbound" = 0

plugins=$(api /api/plugins)
external=$(jq -r '[.[] | select(.signature != "internal") | .id] | sort | join(" ")' <<<"$plugins" 2>/dev/null)
check plugins "external plugins: '${external}'" test "$external" = "canvas jaeger victoriametrics-logs-datasource"
vlogs=$(jq -c '.[] | select(.id == "victoriametrics-logs-datasource") | {type, version: .info.version, signature, enabled}' <<<"$plugins" 2>/dev/null)
check plugin-vlogs "victoriametrics-logs-datasource ${vlogs:-not listed by /api/plugins}" \
  is "$vlogs" '[.type, .version, .signature, .enabled] | join(" ")' "datasource ${VLOGS_DS_TAG#v} unsigned true"
jaeger=$(jq -c '.[] | select(.id == "jaeger") | {type, version: .info.version, signature, enabled}' <<<"$plugins" 2>/dev/null)
check plugin-jaeger "jaeger ${jaeger:-not listed by /api/plugins}" \
  is "$jaeger" '[.type, .version, .signature, .enabled] | join(" ")' "datasource ${JAEGER_DS_TAG#v} unsigned true"

# A data source pointed at a closed local port: the refusal must come from the plugin's backend process.
for type in victoriametrics-logs-datasource jaeger; do
  api /api/datasources -X POST -H 'Content-Type: application/json' \
    -d "{\"name\":\"smoke-$type\",\"uid\":\"smoke-$type\",\"type\":\"$type\",\"access\":\"proxy\",\"url\":\"http://127.0.0.1:9\"}" >/dev/null
  answer=$(api "/api/datasources/uid/smoke-$type/health")
  check "backend-$type" "health via the backend -> $(cut -c1-140 <<<"${answer:-no answer}")" \
    grep -qE '127\.0\.0\.1:9.*connection refused' <<<"$answer"
done

settings=$(api /api/admin/settings)
while read -r key want; do
  got=$(jq -r "$key" <<<"$settings" 2>/dev/null)
  check setting "${key#.} = '${got}' (want '${want}')" test "$got" = "$want"
done <<'EOF'
.analytics.reporting_enabled false
.analytics.check_for_updates false
.analytics.check_for_plugin_updates false
.analytics.feedback_links_enabled false
.news.news_feed_enabled false
.plugins.preinstall_disabled true
.plugins.plugin_admin_enabled false
.plugins.public_key_retrieval_disabled true
.security.disable_gravatar true
.snapshots.external_enabled false
.grafana_com.url http://127.0.0.1:9
.grafana_com.api_url http://127.0.0.1:9/api
.grafana_net.url http://127.0.0.1:9
EOF

# The grafana.com proxy, which any signed-in user can call, must not try to reach grafana.com either.
before=$(docker logs "$name" 2>&1 | wc -l)
gnet=$(api /api/gnet/plugins -o /dev/null -w '%{http_code}')
sleep 2
docker logs "$name" 2>&1 | tail -n "+$((before + 1))" >"$work/gnet.log"
tried=$(grep -cE 'lookup |no such host|network is unreachable|grafana\.(com|net)' "$work/gnet.log")
check gnet "GET /api/gnet/plugins -> HTTP ${gnet:-none}, ${tried} attempts to reach grafana.com logged" \
  test "$tried" = 0 -a -n "$gnet" -a "$gnet" != 200
frontend=$(api /api/frontend/settings)
got=$(jq -c '{hasUpdate: .buildInfo.hasUpdate, latestVersion: .buildInfo.latestVersion, newsFeedEnabled, pluginAdminEnabled, preinstalled: (.pluginCatalogPreinstalledPlugins | length)}' <<<"$frontend" 2>/dev/null)
check frontend "/api/frontend/settings ${got:-no answer}" \
  test "$got" = '{"hasUpdate":false,"latestVersion":"","newsFeedEnabled":false,"pluginAdminEnabled":false,"preinstalled":0}'

# Nothing can be installed at run time: the plugin directory is the baked-in one and is read-only for the user.
# shellcheck disable=SC2016  # expanded by the shell inside the container
dir=$(docker exec "$name" sh -c 'if [ -w "$GF_PATHS_PLUGINS" ]; then echo "writable $GF_PATHS_PLUGINS"; else echo "read-only $GF_PATHS_PLUGINS: $(ls "$GF_PATHS_PLUGINS" | tr "\n" " ")"; fi' 2>/dev/null)
check plugin-dir "${dir:-no answer}" test "$dir" = "read-only /usr/share/grafana/plugins: victoriametrics-logs-datasource "
# The plugins load unsigned, so nothing verifies their files at start: the running user must not be able to
# change any of them, in either plugin tree (the bundled one holds Jaeger).
trees=(/usr/share/grafana/plugins /usr/share/grafana/data/plugins-bundled)
# shellcheck disable=SC2016  # expanded by the shell inside the container
writable=$(docker exec "$name" sh -c 'find "$@" ! -type l \( \( -user "$(id -u)" -perm -0200 \) -o \( -group "$(id -g)" -perm -0020 \) -o -perm -0002 \) | wc -l' sh "${trees[@]}" 2>/dev/null)
files=$(docker exec "$name" sh -c 'find "$@" | wc -l' sh "${trees[@]}" 2>/dev/null)
check plugin-files "${writable:-?} of ${files:-?} plugin files and directories are writable by the running user" \
  test "${writable:-1}" = 0 -a "${files:-0}" -gt 50
# shellcheck disable=SC2016  # expanded by the shell inside the container
tamper=$(docker exec "$name" sh -c 'd=/usr/share/grafana/data/plugins-bundled/jaeger
  ( echo "// x" >>"$d/module.js" ) 2>/dev/null && echo "module.js WRITTEN"
  ( rm -f "$d/plugin.json" ) 2>/dev/null; [ -e "$d/plugin.json" ] || echo "plugin.json REMOVED"
  ( mv "$d" "$d.x" ) 2>/dev/null && echo "directory RENAMED"
  ( touch /usr/share/grafana/data/plugins-bundled/new ) 2>/dev/null && echo "sibling CREATED"
  echo done' 2>/dev/null | tr '\n' ' ')
check plugin-tamper "as the running user, change the Jaeger plugin: '${tamper:-no answer}'" test "$tamper" = "done "
backends=$(docker exec "$name" find / -xdev -type f \( -name 'gpx_*' -o -name '*_backend_plugin_*' \) 2>/dev/null | sort | tr '\n' ' ')
jaeger_bin=/usr/share/grafana/data/plugins-bundled/jaeger/gpx_grafana-jaeger-datasource_linux_amd64
vlogs_bin=/usr/share/grafana/plugins/victoriametrics-logs-datasource/victoriametrics_logs_backend_plugin_linux_amd64
check backends "plugin executables in the image: ${backends:-none}" test "$backends" = "$jaeger_bin $vlogs_bin "

# The two backends are the rebuilt ones: the fixed Go and dependencies, upstream's commit and build settings.
docker cp -q "$name:$jaeger_bin" "$work/jaeger" 2>/dev/null
docker cp -q "$name:$vlogs_bin" "$work/vlogs" 2>/dev/null
docker cp -q "$name:/usr/share/grafana/bin/grafana" "$work/server" 2>/dev/null
chmod -R a+rX "$work"
docker run --rm --network none -v "$work:/b:ro" "$GO_IMAGE" go version -m /b/jaeger /b/vlogs /b/server >"$work/buildinfo" 2>&1
buildinfo() {  # buildinfo <binary> <pattern>: the pattern matches a line of that binary's section
  awk -v bin="/b/$1:" '$1 == bin { on = 1; print; next } /^\/b\// { on = 0 } on' "$work/buildinfo" >"$work/section"
  grep -qE "$2" "$work/section"  # from a file: grep -q on a pipe can end awk with SIGPIPE and fail the check
}
while read -r bin want; do
  check buildinfo "$bin $want" buildinfo "$bin" "$want"
done <<EOF
jaeger ^/b/jaeger: go$GO_VERSION\$
jaeger dep[[:space:]]+golang.org/x/net[[:space:]]+${XNET_VERSION}[[:space:]]
jaeger build[[:space:]]+vcs.revision=$JAEGER_DS_COMMIT\$
jaeger build[[:space:]]+CGO_ENABLED=0\$
jaeger pluginID.{1,4}jaeger.{1,20}version.{1,4}${JAEGER_DS_TAG#v}[^0-9]
vlogs ^/b/vlogs: go$GO_VERSION\$
vlogs dep[[:space:]]+golang.org/x/net[[:space:]]+${XNET_VERSION}[[:space:]]
vlogs dep[[:space:]]+github.com/klauspost/compress[[:space:]]+${COMPRESS_VERSION}[[:space:]]
vlogs build[[:space:]]+vcs.revision=$VLOGS_DS_COMMIT\$
vlogs build[[:space:]]+CGO_ENABLED=0\$
vlogs pluginID.{1,4}victoriametrics-logs-datasource.{1,20}version.{1,4}${VLOGS_DS_TAG#v}[^0-9]
server ^/b/server: go$GO_VERSION\$
server path[[:space:]]+github.com/grafana/grafana/pkg/cmd/grafana\$
server dep[[:space:]]+${UPSTREAM_PATCH_MODULE}[[:space:]]+${UPSTREAM_PATCH_VERSION}[[:space:]]
server build[[:space:]]+CGO_ENABLED=0\$
server build[[:space:]]+-trimpath=true\$
EOF

# The server is the rebuilt one and says so: upstream's version, the public tag's commit, our patch in the branch.
branch="${UPSTREAM_TAG}-fc-${UPSTREAM_PATCH_MODULE##*/}-${UPSTREAM_PATCH_VERSION}"
said=$(docker run --rm --network none --entrypoint grafana "$img" server -v 2>&1 | head -1)
check server-version "grafana server -v -> '${said}'" \
  test "$said" = "Version ${UPSTREAM_TAG#v} (commit: ${UPSTREAM_COMMIT}, branch: ${branch})"
check server-commit "/api/health commit is ${UPSTREAM_COMMIT}" is "$health" .commit "$UPSTREAM_COMMIT"

[ "$failures" = 0 ] || { echo "--- container log (warnings and errors, tail)"; docker logs "$name" 2>&1 | grep -E 'level=(warn|error)' | tail -15 | cut -c1-300; }
echo "SMOKE $([ "$failures" = 0 ] && echo PASS || echo "FAIL ($failures)") $app $img"
[ "$failures" = 0 ]
