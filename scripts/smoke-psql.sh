#!/usr/bin/env bash
# Run the psql image with NO network and prove it is what images/psql.env says it is.
#
#   scripts/smoke-psql.sh psql <image.tar | image-ref>
#
# Every container gets --network none and no database. Checks: the image config is Alpine's (read from the registry by
# digest, nothing pulled or run); the labels name the build; psql and pg_isready are PostgreSQL 17 at PGCLIENT_VERSION
# and link libpq with OpenSSL; no server, gosu or su-exec is installed; both clients run as uid 70 (postgres:17-alpine's
# user, which this image does not have) and uid 10001, on a read-only root with no capabilities, and fail as a client
# should against a closed port; /bin/sh is busybox and runs a sample of the Jobs' shell (the partition-runway Job's
# wait loop and retry function: until, case, $(( )), ${var:-default}, a pipeline in $( ), return codes), with the
# external tools such scripts call present.
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
# As the Jobs run it: a numeric user, read-only root, no capabilities, no network. HOME is not set: no .psql_history.
job() {  # job <uid> <command...>
  local uid=$1; shift
  docker run --rm -i --network none --user "$uid:$uid" --read-only --cap-drop ALL --security-opt no-new-privileges --memory 256m --tmpfs /tmp -e PGUSER=smoke "$img" "$@"
}

ours=$(docker image inspect --format '{{json .Config}}' "$img" | jq -cS "$normalize")
theirs=$(docker buildx imagetools inspect "$UPSTREAM_IMAGE" --format '{{json .Image}}' | jq -cS '."linux/amd64".config | '"$normalize")
check config "ours ${ours:-none}" test -n "$ours" -a "$ours" = "$theirs"
check config "command $(jq -c .Cmd <<<"$ours"), entrypoint $(jq -c .Entrypoint <<<"$ours"), user '$(jq -r .User <<<"$ours")'" \
  test "$(jq -c '[.Cmd, .Entrypoint, .User]' <<<"$ours")" = '[["/bin/sh"],null,""]'

while read -r key want; do
  got=$(label "$key")
  check labels "$key = '${got}'" test "$got" = "$want"
done <<EOF
org.opencontainers.image.version $UPSTREAM_TAG-$FC_REVISION
org.opencontainers.image.base.name $UPSTREAM_IMAGE
com.figurecollecting.upstream.tag $UPSTREAM_TAG
com.figurecollecting.postgresql17-client.package $PGCLIENT_VERSION
EOF

packages=$(job 0 apk info -v 2>&1)
check packages "postgresql17-client-$PGCLIENT_VERSION installed" grep -qx "postgresql17-client-$PGCLIENT_VERSION" <<<"$packages"
check packages "no server, gosu or su-exec: $(grep -cE '^(postgresql17-[0-9]|postgresql-[0-9]|gosu|su-exec)' <<<"$packages") found" \
  test "$(grep -cE '^(postgresql17-[0-9]|postgresql-[0-9]|gosu|su-exec)' <<<"$packages")" = 0
check packages "no apk download cache: '$(job 0 sh -c 'ls /var/cache/apk 2>/dev/null | wc -l' 2>&1)'" \
  test "$(job 0 sh -c 'ls /var/cache/apk 2>/dev/null | wc -l' 2>&1)" = 0

want="psql (PostgreSQL) ${PGCLIENT_VERSION%-r*}"
for uid in 70 10001; do
  said=$(job "$uid" psql --version 2>&1)
  check version "uid $uid: psql --version -> '$said'" test "$said" = "$want"
  said=$(job "$uid" pg_isready --version 2>&1)
  check version "uid $uid: pg_isready --version -> '$said'" test "$said" = "pg_isready (PostgreSQL) ${PGCLIENT_VERSION%-r*}"
  # Nothing listens on port 1 of the container's own loopback: pg_isready says no response (exit 2), and psql with
  # sslmode=require fails to connect (not for want of SSL support).
  job "$uid" pg_isready -h 127.0.0.1 -p 1 -t 1 >/dev/null 2>&1
  check client "uid $uid: pg_isready against a closed port exits 2 (no response)" test "$?" = 2
  said=$(job "$uid" env PGSSLMODE=require psql -X -At -h 127.0.0.1 -p 1 -c 'select 1' 2>&1 | head -2 | tr '\n' ' ')
  check client "uid $uid: psql sslmode=require against a closed port -> '${said}'" \
    grep -qiE 'connection refused|could not connect' <<<"$said"
done

libs=$(job 0 sh -c 'ldd /usr/bin/psql; ldd /usr/lib/libpq.so.5' 2>&1)
check libpq "psql links libpq: $(grep -c 'libpq' <<<"$libs") lines" grep -q 'libpq\.so' <<<"$libs"
check libpq "libpq links libssl" grep -q 'libssl\.so' <<<"$libs"
check libpq "libpq links libcrypto" grep -q 'libcrypto\.so' <<<"$libs"

# /bin/sh is busybox's ash; the Jobs run `sh -c` and `sh /mig/migrate.sh`.
check busybox "/bin/sh -> '$(job 0 readlink /bin/sh 2>&1)'" test "$(job 0 readlink /bin/sh 2>&1)" = "/bin/busybox"
tools="sh awk sed grep tr head tail cut sort wc cat date sleep sha256sum find xargs mktemp mkfifo basename dirname printf test expr tee env timeout"
missing=$(job 0 sh -c 'for t in '"$tools"'; do command -v "$t" >/dev/null || printf "%s " "$t"; done' 2>&1)
check busybox "tools the scripts call are present; missing: '${missing}'" test -z "$missing"

# A sample of the partition-runway Job's shell. pg_isready is the real one against a closed port (the wait loop gives
# up after two tries here); psql is a function that fails twice with a lock error (55P03) and then succeeds, or fails
# with another error, which the retry function must not retry.
expected='waiting 1
waiting 2
a attempt 1/3 failed: ERROR:  lock 55P03 line2
a attempt 2/3 failed: ERROR:  lock 55P03 line2
a=ok (attempt 3)
b attempt 1/3 failed: ERROR:  syntax line2
b: not a lock clash; not retrying
rc=1 n=1 port=5432'
sample=$(job 0 sh -s <<'SCRIPT' 2>&1
set -u
tries=0
until pg_isready -h 127.0.0.1 -p "${PGPORT:-1}" -t 1 >/dev/null 2>&1; do
  tries=$((tries + 1))
  echo "waiting $tries"
  [ "$tries" -ge 2 ] && break
  sleep 0
done
ATTEMPTS=3
psql() {
  case "$1" in
    lock2) if [ "$n" -lt 3 ]; then echo "ERROR:  lock 55P03"; echo line2; return 1; fi; echo ok ;;
    *) echo "ERROR:  syntax"; echo line2; return 1 ;;
  esac
}
call() {
  n=1
  while :; do
    if out=$(psql "$2" 2>&1); then
      echo "$1=$out (attempt $n)"; return 0
    fi
    echo "$1 attempt $n/$ATTEMPTS failed: $(printf '%s' "$out" | head -2 | tr '\n' ' ')"
    case "$out" in
      *55P03*|*40P01*|*42P07*) ;;
      *) echo "$1: not a lock clash; not retrying"; return 1 ;;
    esac
    if [ "$n" -ge "$ATTEMPTS" ]; then echo "$1: gave up after $n attempts"; return 1; fi
    n=$((n + 1)); sleep 0
  done
}
rc=0
call a lock2 || rc=1
call b syntax || rc=1
echo "rc=$rc n=${n:-0} port=${PGPORT:-5432}"
SCRIPT
)
check busybox "ash runs the Jobs' wait loop and retry function: $(tr '\n' '|' <<<"$sample")" test "$sample" = "$expected"

echo "SMOKE $([ "$failures" = 0 ] && echo PASS || echo "FAIL ($failures)") $app $img"
[ "$failures" = 0 ]
