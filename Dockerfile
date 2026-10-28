# Rebuild of an upstream VictoriaMetrics release with ONE change: the Go toolchain (patches/<app>.patch).
# Mirrors upstream's deployment/docker build for linux/amd64 at the tag: same go build flags
# (-trimpath -buildvcs=false, -extldflags '-static', -tags 'netgo osusergo', CGO_ENABLED=1 with
# CC=x86_64-linux-gnu-gcc), same runtime image pin, same binary path and entrypoint.
# Build arguments come from images/<app>.env via scripts/build.sh; target = the app name.
ARG GO_IMAGE=docker.io/library/golang:1.27.3@sha256:2669ec97bbb6e54602952b4171211116e3fe90e5f9c47619920c74554b3d7d09
ARG ROOT_IMAGE=gcr.io/distroless/static:latest@sha256:47b2d72ff90843eb8a768b5c2f89b40741843b639d065b9b937b07cd59b479c6

FROM ${GO_IMAGE} AS build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG APP_NAME UPSTREAM_REPO UPSTREAM_TAG UPSTREAM_COMMIT UPSTREAM_DESCRIBE GO_VERSION BUILD_DATE FC_REVISION
ENV GOTOOLCHAIN=local GOFLAGS=-mod=vendor GOPROXY=off \
    CGO_ENABLED=1 GOOS=linux GOARCH=amd64 GOAMD64=v1 CC=x86_64-linux-gnu-gcc
WORKDIR /src
RUN git -c advice.detachedHead=false clone --quiet --depth 1 --branch "${UPSTREAM_TAG}" "${UPSTREAM_REPO}" . \
 && test "$(git rev-parse HEAD)" = "${UPSTREAM_COMMIT}" \
 && h="${UPSTREAM_DESCRIBE##*-g}" \
 && test "${UPSTREAM_DESCRIBE}" = "tags-${UPSTREAM_TAG}-0-g${h}" \
 && test "$(git rev-parse --short="${#h}" HEAD)" = "${h}"
COPY patches/${APP_NAME}.patch /tmp/go-version.patch
RUN git apply --check /tmp/go-version.patch && git apply /tmp/go-version.patch \
 && test "$(git diff --name-only | tr '\n' ' ')" = "deployment/docker/Makefile go.mod " \
 && grep -qx "go ${GO_VERSION}" go.mod \
 && test "$(go env GOVERSION)" = "go${GO_VERSION}"
RUN go build -trimpath -buildvcs=false \
      -ldflags "-extldflags '-static' -X 'github.com/VictoriaMetrics/VictoriaMetrics/lib/buildinfo.Version=${APP_NAME}-${BUILD_DATE}-${UPSTREAM_DESCRIBE}-${FC_REVISION}'" \
      -tags 'netgo osusergo' \
      -o "/out/${APP_NAME}-prod" "./app/${APP_NAME}" \
 && test "$(go version "/out/${APP_NAME}-prod")" = "/out/${APP_NAME}-prod: go${GO_VERSION}"

FROM ${ROOT_IMAGE} AS runtime
ARG ROOT_IMAGE APP_NAME UPSTREAM_REPO UPSTREAM_TAG UPSTREAM_COMMIT UPSTREAM_GO_FIX_COMMIT GO_VERSION \
    FC_REVISION FC_COMMIT CREATED DOCS_URL
LABEL org.opencontainers.image.source="https://github.com/FigureCollecting/store-images" \
      org.opencontainers.image.revision="${FC_COMMIT}" \
      org.opencontainers.image.version="${UPSTREAM_TAG}-${FC_REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.title="${APP_NAME}" \
      org.opencontainers.image.description="${APP_NAME} ${UPSTREAM_TAG} rebuilt with Go ${GO_VERSION}; no other change" \
      org.opencontainers.image.documentation="${DOCS_URL}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="FigureCollecting" \
      org.opencontainers.image.base.name="${ROOT_IMAGE}" \
      com.figurecollecting.upstream.source="${UPSTREAM_REPO}" \
      com.figurecollecting.upstream.tag="${UPSTREAM_TAG}" \
      com.figurecollecting.upstream.commit="${UPSTREAM_COMMIT}" \
      com.figurecollecting.upstream.go-fix-commit="${UPSTREAM_GO_FIX_COMMIT}" \
      com.figurecollecting.go.version="${GO_VERSION}"

FROM runtime AS victoria-logs
EXPOSE 9428
ENTRYPOINT ["/victoria-logs-prod"]
COPY --from=build /out/victoria-logs-prod /victoria-logs-prod

FROM runtime AS victoria-traces
EXPOSE 10428
ENTRYPOINT ["/victoria-traces-prod"]
COPY --from=build /out/victoria-traces-prod /victoria-traces-prod
