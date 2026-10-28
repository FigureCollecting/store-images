#!/usr/bin/env bash
# Scan one image with trivy AND grype, then let scripts/gate.py decide.
#
#   scripts/scan.sh <label> <image.tar | registry/repo:tag@sha256:...> <outdir>
#
# Both scanners run from images pinned by digest, linux/amd64, and write JSON to <outdir>.
# The gate fails on any HIGH or CRITICAL finding with a fix available, from either scanner.
# There is no ignore file and no VEX, by design.
# Exit: 0 pass, 1 findings, 2 scanner or report error.
set -uo pipefail
TRIVY_IMAGE=docker.io/aquasec/trivy:0.76.1@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c
GRYPE_IMAGE=docker.io/anchore/grype:v0.121.0@sha256:621d56d1d68106b787022c81546f7e0a36202a25d1239f0c20195fab8c16f7ea
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ $# -eq 3 ] || { echo "usage: $0 <label> <image.tar | image-ref> <outdir>" >&2; exit 2; }
label=$1 target=$2 out=$3
cache="${SCAN_CACHE_DIR:-$HOME/.cache/store-images}"
mkdir -p "$out" "$cache/trivy" "$cache/grype"
out=$(cd "$out" && pwd)

run=(docker run --rm --user "$(id -u):$(id -g)" --tmpfs "/tmp:rw,mode=1777" -e HOME=/tmp -v "$out:/out")
if [ -f "$target" ]; then  # unpack the archive once; both scanners read the OCI layout directory
  if ! { rm -rf "$out/oci" && mkdir -p "$out/oci" && tar -xf "$target" -C "$out/oci"; }; then
    echo "$label GATE ERROR: cannot unpack $target"; exit 2
  fi
  trivy_src=(--input /out/oci) grype_src=oci-dir:/out/oci
else
  trivy_src=(--image-src remote "$target") grype_src="registry:$target"
fi

"${run[@]}" -v "$cache/trivy:/cache" "$TRIVY_IMAGE" image --cache-dir /cache --quiet \
  --platform linux/amd64 --severity HIGH,CRITICAL --ignore-unfixed --list-all-pkgs \
  --format json --output /out/trivy.json "${trivy_src[@]}" \
  || { echo "$label GATE ERROR: trivy failed (exit $?), not a finding" ; exit 2; }

"${run[@]}" -v "$cache/grype:/cache" -e GRYPE_DB_CACHE_DIR=/cache -e GRYPE_CHECK_FOR_APP_UPDATE=false \
  "$GRYPE_IMAGE" "$grype_src" --platform linux/amd64 --only-fixed --by-cve -q -o json --file /out/grype.json \
  || { echo "$label GATE ERROR: grype failed (exit $?), not a finding"; exit 2; }

python3 "$ROOT/scripts/gate.py" --label "$label" --trivy "$out/trivy.json" --grype "$out/grype.json"
