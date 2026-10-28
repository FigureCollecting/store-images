#!/usr/bin/env bash
# Build one image from images/<app>.env (linux/amd64).
#
#   scripts/build.sh <app> archive <out.tar>    image tarball (docker + OCI layout), no attestations;
#                                              the input to scan.sh and smoke.sh
#   scripts/build.sh <app> push <scanned.tar>   publish what was scanned, with SBOM + provenance attestations
#
# Builds are reproducible: SOURCE_DATE_EPOCH is the commit time and layer timestamps are rewritten to it.
# push pushes by digest (no tag), checks that the pushed linux/amd64 manifest is byte-for-byte the scanned
# one, and only then adds the tag <UPSTREAM_TAG>-<FC_REVISION>. An existing tag is never overwritten:
# bump FC_REVISION in images/<app>.env to publish a new build.
set -euo pipefail
SBOM_GENERATOR=docker.io/docker/buildkit-syft-scanner:1.12.1@sha256:873a7458c3c8ac8e32c7b796c10e1600b07b963d2aa000a72a2d97aaaa2d6315
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
usage() { echo "usage: $0 <app> archive <out.tar> | $0 <app> push <scanned.tar>" >&2; exit 2; }
[ $# -eq 3 ] || usage
app=$1 mode=$2 tar=$3
[ -f "$ROOT/images/$app.env" ] || { echo "no image definition images/$app.env" >&2; exit 2; }
set -a
# shellcheck source=/dev/null
. "$ROOT/images/$app.env"
set +a

SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct)}"
export SOURCE_DATE_EPOCH
fc_commit=$(git -C "$ROOT" rev-parse HEAD)
[ -z "$(git -C "$ROOT" status --porcelain)" ] || fc_commit="$fc_commit-dirty"  # untracked files count
tag="$UPSTREAM_TAG-$FC_REVISION"

args=(--platform linux/amd64 --target "$APP_NAME" --file "$ROOT/Dockerfile")
for v in APP_NAME UPSTREAM_REPO UPSTREAM_TAG UPSTREAM_COMMIT UPSTREAM_DESCRIBE UPSTREAM_GO_FIX_COMMIT \
         GO_VERSION FC_REVISION DOCS_URL; do
  args+=(--build-arg "$v=${!v}")
done
args+=(--build-arg "FC_COMMIT=$fc_commit"
       --build-arg "BUILD_DATE=$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y%m%d-%H%M%S)"
       --build-arg "CREATED=$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)")

amd64_manifest() {  # amd64_manifest <index-or-manifest JSON on stdin> -> digest of the linux/amd64 manifest
  jq -r '.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest'
}

case "$mode" in
  archive)
    docker buildx build "${args[@]}" --provenance=false --sbom=false --tag "$IMAGE:$tag" \
      --output "type=docker,dest=$tar,oci-mediatypes=true,rewrite-timestamp=true" "$ROOT"
    ;;
  push)
    [ -f "$tar" ] || { echo "no scanned archive $tar" >&2; exit 2; }
    case "$fc_commit" in *-dirty) echo "refusing to publish from a dirty tree" >&2; exit 2 ;; esac
    if existing=$(docker buildx imagetools inspect "$IMAGE:$tag" --format '{{json .Manifest.Digest}}' 2>/dev/null); then
      echo "::notice::$IMAGE:$tag already exists ($existing); not overwritten. Bump FC_REVISION to publish."
      exit 0
    fi
    index=$(tar -xOf "$tar" index.json)
    scanned=$(jq -r '.manifests[0].digest' <<<"$index")
    if [ "$(jq -r '.manifests[0].mediaType' <<<"$index")" = "application/vnd.oci.image.index.v1+json" ]; then
      scanned=$(tar -xOf "$tar" "blobs/sha256/${scanned#sha256:}" | amd64_manifest)
    fi
    meta=$(mktemp); trap 'rm -f "$meta"' EXIT
    docker buildx build "${args[@]}" --provenance=mode=max --attest "type=sbom,generator=$SBOM_GENERATOR" \
      --output "type=image,name=$IMAGE,oci-mediatypes=true,rewrite-timestamp=true,push-by-digest=true,name-canonical=true,push=true" \
      --metadata-file "$meta" "$ROOT"
    digest=$(jq -r '."containerimage.digest"' "$meta")
    pushed=$(docker buildx imagetools inspect --raw "$IMAGE@$digest" | amd64_manifest)
    if [ "$pushed" != "$scanned" ]; then
      echo "::error::pushed linux/amd64 manifest $pushed is not the scanned $scanned; tag NOT applied" >&2
      exit 1
    fi
    docker buildx imagetools create --tag "$IMAGE:$tag" "$IMAGE@$digest"
    echo "published $IMAGE:$tag@$digest (linux/amd64 $pushed, scanned)"
    ;;
  *) usage ;;
esac
