# Rebuild of an upstream VictoriaMetrics release with two changes: a newer Go patch release (GO_VERSION
# against UPSTREAM_GO_VERSION) and ONE module moved to its fixed version (patches/<app>.patch, go.mod and
# go.sum only). Mirrors upstream's deployment/docker build for linux/amd64 at the tag: same go build flags
# (-trimpath -buildvcs=false, -extldflags '-static', -tags 'netgo osusergo', CGO_ENABLED=1 with
# CC=x86_64-linux-gnu-gcc), same runtime image pin, same binary path and entrypoint.
# The tag vendors its modules; the patched go.mod is built from the module cache instead (-mod=readonly),
# filled from go.sum-verified downloads. The build fails unless the result is built by GO_VERSION, has the
# patched module version and otherwise has exactly the build settings and module versions of the binary in
# upstream's own image of the tag (UPSTREAM_IMAGE).
# Build arguments come from images/<app>.env via scripts/build.sh; target = the app name.
# Every RUN has no network, except the two that fetch the pinned source tree and its go.sum-verified modules.
ARG GO_IMAGE=docker.io/library/golang:1.27.4@sha256:205c7636d74e0ffbe3d007162df7d74f859f5a8c8322dfb9f907977686c126cf
ARG ROOT_IMAGE=gcr.io/distroless/static:latest@sha256:47b2d72ff90843eb8a768b5c2f89b40741843b639d065b9b937b07cd59b479c6
# The same pin as UPSTREAM_IMAGE in images/victoria-traces.env, which scripts/build.sh passes.
ARG UPSTREAM_IMAGE=docker.io/victoriametrics/victoria-traces:v0.13.0@sha256:3018df8f99429d7d3029167ab9f70848bf0a778c31c49dff16c86c7e34759fbd

FROM ${UPSTREAM_IMAGE} AS upstream

FROM ${GO_IMAGE} AS build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG APP_NAME UPSTREAM_REPO UPSTREAM_TAG UPSTREAM_COMMIT UPSTREAM_DESCRIBE UPSTREAM_GO_VERSION \
    UPSTREAM_PATCH_MODULE UPSTREAM_PATCH_VERSION GO_VERSION BUILD_DATE FC_REVISION
ENV GOTOOLCHAIN=local GOFLAGS=-mod=readonly \
    CGO_ENABLED=1 GOOS=linux GOARCH=amd64 GOAMD64=v1 CC=x86_64-linux-gnu-gcc
WORKDIR /src
RUN --network=none test "$(go env GOVERSION)" = "go${GO_VERSION}"
RUN git -c advice.detachedHead=false clone --quiet --depth 1 --branch "${UPSTREAM_TAG}" "${UPSTREAM_REPO}" . \
 && test "$(git rev-parse HEAD)" = "${UPSTREAM_COMMIT}" \
 && h="${UPSTREAM_DESCRIBE##*-g}" \
 && test "${UPSTREAM_DESCRIBE}" = "tags-${UPSTREAM_TAG}-0-g${h}" \
 && test "$(git rev-parse --short="${#h}" HEAD)" = "${h}"
COPY patches/${APP_NAME}.patch /tmp/deps.patch
RUN --network=none git apply --check /tmp/deps.patch && git apply /tmp/deps.patch \
 && test "$(git diff --numstat | tr '\t\n' '  ')" = "1 1 go.mod 2 2 go.sum " \
 && grep -qxP "\t${UPSTREAM_PATCH_MODULE} ${UPSTREAM_PATCH_VERSION}" go.mod
RUN go mod download
COPY --from=upstream /${APP_NAME}-prod /tmp/upstream
# norm: the build settings and the module path and version of every dependency but the patched one
# (upstream's vendored build records no module hashes, a module-cache build does).
RUN --network=none GOPROXY=off go build -trimpath -buildvcs=false \
      -ldflags "-extldflags '-static' -X 'github.com/VictoriaMetrics/VictoriaMetrics/lib/buildinfo.Version=${APP_NAME}-${BUILD_DATE}-${UPSTREAM_DESCRIBE}-${FC_REVISION}'" \
      -tags 'netgo osusergo' \
      -o "/out/${APP_NAME}-prod" "./app/${APP_NAME}" \
 && go version -m "/out/${APP_NAME}-prod" >/tmp/buildinfo && go version -m /tmp/upstream >/tmp/upstream-buildinfo \
 && test "$(head -n 1 /tmp/buildinfo)" = "/out/${APP_NAME}-prod: go${GO_VERSION}" \
 && test "$(head -n 1 /tmp/upstream-buildinfo)" = "/tmp/upstream: go${UPSTREAM_GO_VERSION}" \
 && grep -qP "^\tdep\t${UPSTREAM_PATCH_MODULE}\t${UPSTREAM_PATCH_VERSION}\t" /tmp/buildinfo \
 && grep -cP "^\tdep\t${UPSTREAM_PATCH_MODULE}\t" /tmp/upstream-buildinfo | grep -qx 1 \
 && norm() { tail -n +2 "$1" | grep -vP "^\tdep\t${UPSTREAM_PATCH_MODULE}\t" | cut -f1-4; } \
 && diff <(norm /tmp/upstream-buildinfo) <(norm /tmp/buildinfo) \
 && test "$(git status --porcelain --untracked-files=all | tr '\n' ' ')" = " M go.mod  M go.sum "

FROM ${ROOT_IMAGE} AS runtime
ARG ROOT_IMAGE APP_NAME UPSTREAM_REPO UPSTREAM_TAG UPSTREAM_COMMIT UPSTREAM_GO_VERSION UPSTREAM_PATCH_MODULE \
    UPSTREAM_PATCH_VERSION GO_VERSION FC_REVISION FC_COMMIT CREATED DOCS_URL
LABEL org.opencontainers.image.source="https://github.com/FigureCollecting/store-images" \
      org.opencontainers.image.revision="${FC_COMMIT}" \
      org.opencontainers.image.version="${UPSTREAM_TAG}-${FC_REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.title="${APP_NAME}" \
      org.opencontainers.image.description="${APP_NAME} ${UPSTREAM_TAG} rebuilt with Go ${GO_VERSION} and ${UPSTREAM_PATCH_MODULE} ${UPSTREAM_PATCH_VERSION}; no other change" \
      org.opencontainers.image.documentation="${DOCS_URL}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="FigureCollecting" \
      org.opencontainers.image.base.name="${ROOT_IMAGE}" \
      com.figurecollecting.upstream.source="${UPSTREAM_REPO}" \
      com.figurecollecting.upstream.tag="${UPSTREAM_TAG}" \
      com.figurecollecting.upstream.commit="${UPSTREAM_COMMIT}" \
      com.figurecollecting.upstream.go.version="${UPSTREAM_GO_VERSION}" \
      com.figurecollecting.go.version="${GO_VERSION}" \
      com.figurecollecting.patch.module="${UPSTREAM_PATCH_MODULE}" \
      com.figurecollecting.patch.version="${UPSTREAM_PATCH_VERSION}"

FROM runtime AS victoria-traces
EXPOSE 10428
ENTRYPOINT ["/victoria-traces-prod"]
COPY --from=build /out/victoria-traces-prod /victoria-traces-prod
