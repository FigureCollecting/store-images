"""The Tailscale rebuild: upstream's image with its three binaries rebuilt from the release tag with the fixed
golang.org/x/net and klauspost/compress, and Alpine's zlib moved to the fixed package. Nothing else changes."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))
import upstream_watch  # noqa: E402
from test_victoria_recipe import instructions, parsed_stage  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
XNET = "golang.org/x/net"
COMPRESS = "github.com/klauspost/compress"
# The lowest versions that clear the findings of the scan gate on 2026-11-23: CVE-2026-78661 (x/net, fixed in
# v0.62.0, trivy), CVE-2026-116591 (compress, fixed in v1.20.2, grype) and CVE-2026-4738 (zlib, fixed in 1.4-r0, grype).
XNET_FLOOR = (0, 62, 0)
COMPRESS_FLOOR = (1, 20, 2)
ZLIB_FIXED = "1.4-r0"
# OpenSSL: the scan gate on 2026-11-25 reports CVE-2026-124709 (trivy, fixed in 3.5.9-r2) and four CVEs fixed in
# 3.5.9-r1 (grype) on upstream's 3.5.9-r0.
OPENSSL_FIXED = "3.5.9-r2"
# The Alpine packages moved to pinned files, each with its fixed version.
PACKAGES = {"zlib": ZLIB_FIXED, "libcrypto3": OPENSSL_FIXED, "libssl3": OPENSSL_FIXED}
# FC_REVISIONs already published to ghcr.io/figurecollecting/tailscale; a tag is never overwritten, so a change to
# what the image holds needs a revision not in this list.
PUBLISHED_REVISIONS = ("fc1",)  # fc1: 2026-11-23, OpenSSL still upstream's 3.5.9-r0
BINARIES = ("tailscale", "tailscaled", "containerboot")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def version(text):
    return tuple(int(x) for x in text.lstrip("v").split("."))


# The whole build stage as reviewed: every instruction after `FROM ${GO_IMAGE} AS build`, compared after parsing
# (tests/test_victoria_recipe.py), so an extra ENV, ARG, SHELL or RUN in any spelling is an extra instruction.
BUILD_STAGE = r"""
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG UPSTREAM_REPO UPSTREAM_TAG UPSTREAM_COMMIT UPSTREAM_GO_VERSION TS_GO_REV TS_GO_TARBALL TS_GO_SHA256 \
    XNET_VERSION COMPRESS_VERSION GO_VERSION
ENV GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOAMD64=v1
ADD --checksum=${TS_GO_SHA256} ${TS_GO_TARBALL} /tmp/tsgo.tar.gz
RUN --network=none test "${TS_GO_TARBALL}" = "https://github.com/tailscale/go/releases/download/build-${TS_GO_REV}/linux-amd64.tar.gz" \
 && rm -rf /usr/local/go && mkdir /usr/local/go \
 && tar -C /usr/local/go --strip-components=1 -xzf /tmp/tsgo.tar.gz && rm /tmp/tsgo.tar.gz \
 && test "$(command -v go)" = /usr/local/go/bin/go && test "$(go env GOVERSION)" = "go${GO_VERSION}"
WORKDIR /src
RUN git -c advice.detachedHead=false clone --quiet --depth 1 --branch "${UPSTREAM_TAG}" "${UPSTREAM_REPO}" . \
 && test "$(git rev-parse HEAD)" = "${UPSTREAM_COMMIT}" \
 && test "$(cat go.toolchain.rev)" = "${TS_GO_REV}"
COPY patches/tailscale.patch /tmp/deps.patch
RUN --network=none git apply --check /tmp/deps.patch && git apply /tmp/deps.patch \
 && test "$(git diff --numstat | tr '\t\n' '  ')" = "2 2 go.mod 4 4 go.sum " \
 && grep -qxP "\tgolang.org/x/net ${XNET_VERSION}" go.mod \
 && grep -qxP "\tgithub.com/klauspost/compress ${COMPRESS_VERSION}" go.mod
RUN go list -tags ts_kube,ts_package_container -deps ./cmd/tailscale ./cmd/tailscaled ./cmd/containerboot >/dev/null
COPY --from=upstream /usr/local/bin/ /tmp/upstream/
RUN --network=none GOPROXY=off go build -trimpath -tags ts_kube,ts_package_container \
      -ldflags "-X tailscale.com/version.longStamp=${UPSTREAM_TAG#v}-t${UPSTREAM_COMMIT:0:9} -X tailscale.com/version.shortStamp=${UPSTREAM_TAG#v} -X tailscale.com/version.gitCommitStamp=${UPSTREAM_COMMIT}" \
      -o /out/ ./cmd/tailscale ./cmd/tailscaled ./cmd/containerboot \
 && moved='^\tdep\t(golang\.org/x/net|github\.com/klauspost/compress)\t' \
 && norm() { tail -n +2 "$1" | grep -vP "${moved}" | grep -vP '^\t(mod\ttailscale\.com|build\tvcs\.modified=)'; } \
 && sections() { readelf -SW "$1" | grep -oP '^\s+\[\s*\d+\]\s\K\S+'; } \
 && segments() { readelf -lW "$1" | awk '/^ +[A-Z_]+ +0x/ { f = ""; for (i = 7; i < NF; i++) f = f $i; print $1, f }'; } \
 && same() { go version -m "/out/$1" >"/tmp/$1.buildinfo" && go version -m "/tmp/upstream/$1" >"/tmp/$1.upstream-buildinfo" \
      && test "$(head -n 1 "/tmp/$1.buildinfo")" = "/out/$1: go${GO_VERSION}" \
      && test "$(head -n 1 "/tmp/$1.upstream-buildinfo")" = "/tmp/upstream/$1: go${UPSTREAM_GO_VERSION}" \
      && grep -qP "^\tdep\tgolang\.org/x/net\t${XNET_VERSION}\t" "/tmp/$1.buildinfo" \
      && test "$(grep -P "${moved}" "/tmp/$1.buildinfo" | cut -f3 | tr '\n' ' ')" = "$(grep -P "${moved}" "/tmp/$1.upstream-buildinfo" | cut -f3 | tr '\n' ' ')" \
      && ! grep -P "^\tdep\tgithub\.com/klauspost/compress\t" "/tmp/$1.buildinfo" | grep -vqP "\t${COMPRESS_VERSION}\t" \
      && grep -qxP "\tbuild\tvcs\.modified=true" "/tmp/$1.buildinfo" \
      && grep -qxP "\tbuild\tvcs\.modified=false" "/tmp/$1.upstream-buildinfo" \
      && grep -qxP "\tmod\ttailscale\.com\t${UPSTREAM_TAG}\+dirty\t" "/tmp/$1.buildinfo" \
      && grep -qxP "\tmod\ttailscale\.com\t${UPSTREAM_TAG}\t" "/tmp/$1.upstream-buildinfo" \
      && diff <(norm "/tmp/$1.upstream-buildinfo") <(norm "/tmp/$1.buildinfo") \
      && diff <(sections "/tmp/upstream/$1") <(sections "/out/$1") \
      && diff <(segments "/tmp/upstream/$1") <(segments "/out/$1") \
      && sections "/out/$1" | grep -qx .symtab && segments "/out/$1" | grep -qx 'GNU_STACK RW'; } \
 && same tailscale && same tailscaled && same containerboot \
 && test "$(git status --porcelain --untracked-files=all --ignored | tr '\n' ' ')" = " M go.mod  M go.sum "
"""


class DefinitionTest(unittest.TestCase):
    def setUp(self):
        self.env = upstream_watch.read_env(os.path.join(ROOT, "images", "tailscale.env"))
        self.text = read("Dockerfile.tailscale")

    def test_the_base_is_upstreams_release_tag_and_its_own_image(self):
        self.assertEqual(self.env["APP_NAME"], "tailscale")
        self.assertEqual(self.env["IMAGE"], "ghcr.io/figurecollecting/tailscale")
        self.assertEqual(self.env["DOCKERFILE"], "Dockerfile.tailscale")
        self.assertEqual(self.env["SMOKE"], "tailscale")
        self.assertEqual(self.env["UPSTREAM_REPO"], "https://github.com/tailscale/tailscale")
        self.assertEqual(self.env["UPSTREAM_TAG"], "v1.104.7")
        self.assertEqual(self.env["UPSTREAM_COMMIT"], "cbc74fe48ef1b1649f69a4d9a602319427e25102")
        self.assertRegex(self.env["UPSTREAM_IMAGE"],
                         rf"^docker\.io/tailscale/tailscale:{re.escape(self.env['UPSTREAM_TAG'])}@sha256:[0-9a-f]{{64}}$")
        self.assertRegex(self.env["FC_REVISION"], r"^fc[1-9][0-9]*$")
        self.assertNotIn(self.env["FC_REVISION"], PUBLISHED_REVISIONS)
        self.assertIn(f"\nARG UPSTREAM_IMAGE={self.env['UPSTREAM_IMAGE']}\n", self.text)  # the pin check reads defaults
        self.assertIn("FROM ${UPSTREAM_IMAGE} AS upstream\n", self.text)

    def test_the_moved_modules_are_at_or_above_their_fixed_versions(self):
        self.assertGreaterEqual(version(self.env["XNET_VERSION"]), XNET_FLOOR)
        self.assertGreaterEqual(version(self.env["COMPRESS_VERSION"]), COMPRESS_FLOOR)
        for key in ("XNET_FIX_COMMIT", "COMPRESS_FIX_COMMIT"):
            self.assertRegex(self.env[key], r"^[0-9a-f]{40}$")

    def test_the_toolchain_is_tailscales_own_go_at_the_revision_the_tag_names(self):
        # Upstream builds with its fork of Go (go.toolchain.rev); `tailscaled --version` reports the revision.
        rev = self.env["TS_GO_REV"]
        self.assertRegex(rev, r"^[0-9a-f]{40}$")
        self.assertIn(f"\nARG TS_GO_TARBALL=https://github.com/tailscale/go/releases/download/build-{rev}/"
                      "linux-amd64.tar.gz\n", self.text)
        self.assertRegex(self.text, r"(?m)^ARG TS_GO_SHA256=sha256:[0-9a-f]{64}$")
        self.assertEqual(self.env["GO_VERSION"], self.env["UPSTREAM_GO_VERSION"])
        self.assertRegex(self.text, r"(?m)^ARG GO_IMAGE=docker\.io/library/golang:[0-9.]+@sha256:[0-9a-f]{64}$")

    def test_the_build_is_told_every_key_of_the_definition_the_dockerfile_reads(self):
        build = read("scripts/build.sh")
        for key in ("UPSTREAM_IMAGE", "UPSTREAM_GO_VERSION", "TS_GO_REV", "XNET_VERSION", "COMPRESS_VERSION",
                    "XNET_FIX_COMMIT", "COMPRESS_FIX_COMMIT"):
            with self.subTest(key=key):
                self.assertIn(key, self.env)
                self.assertRegex(build, rf"\b{key}\b")
                self.assertRegex(self.text, rf"\b{key}\b")


class PatchTest(unittest.TestCase):
    def test_the_patch_moves_exactly_the_two_modules_in_go_mod_and_go_sum(self):
        env = upstream_watch.read_env(os.path.join(ROOT, "images", "tailscale.env"))
        patch = read("patches/tailscale.patch")
        added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
        removed = [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(re.findall(r"(?m)^diff --git a/(\S+) ", patch), ["go.mod", "go.sum"])
        want = {COMPRESS: env["COMPRESS_VERSION"], XNET: env["XNET_VERSION"]}
        expected = [f"\t{module} {ver}" for module, ver in want.items()]
        self.assertEqual(added[:2], expected)
        hashes = added[2:]
        self.assertEqual(len(hashes), 4, hashes)
        for (module, ver), pair in zip(want.items(), (hashes[:2], hashes[2:])):
            self.assertRegex(pair[0], rf"^{re.escape(module)} {re.escape(ver)} h1:[A-Za-z0-9+/]{{43}}=$")
            self.assertRegex(pair[1], rf"^{re.escape(module)} {re.escape(ver)}/go\.mod h1:[A-Za-z0-9+/]{{43}}=$")
        self.assertEqual(len(removed), 6, removed)
        for line in removed:
            module = line.split()[0]
            self.assertIn(module, want, line)
            self.assertNotIn(f"{module} {want[module]}", line)


class BuildStageTest(unittest.TestCase):
    def setUp(self):
        self.text = read("Dockerfile.tailscale")

    def test_the_dockerfile_has_no_parser_directive_heredoc_or_onbuild(self):
        directives, ins = instructions(self.text)
        self.assertEqual(directives, {})
        self.assertEqual([i for i in ins if "<<" in i[1] or i[0] == "ONBUILD"], [])

    def test_the_stages_are_exactly_the_reviewed_from_sequence(self):
        self.assertEqual([a for k, a in instructions(self.text)[1] if k == "FROM"],
                         ["${UPSTREAM_IMAGE} AS upstream", "${GO_IMAGE} AS build", "upstream AS rootfs",
                          "scratch AS tailscale"])

    def test_the_build_stage_is_exactly_the_reviewed_instruction_list(self):
        # -trimpath hides -ldflags from `go version -m`; an extra ENV, ARG or RUN could change the binaries
        # without changing their buildinfo. The stage is compared whole, as BuildKit parses it.
        self.assertEqual(parsed_stage(self.text, "build"), instructions(BUILD_STAGE)[1])

    def test_an_extra_instruction_in_any_spelling_changes_the_parsed_stage(self):
        for name, new in (("lower-case env", "WORKDIR /src\nenv CGO_ENABLED=1\n"),
                          ("continued ENV", "WORKDIR /src\nENV \\\n  GOFLAGS=-ldflags=-s\n"),
                          ("go env -w", "WORKDIR /src\nRUN go env -w GOFLAGS=-ldflags=-s\n")):
            with self.subTest(name=name):
                bad = self.text.replace("WORKDIR /src\n", new, 1)
                self.assertNotEqual(bad, self.text)
                self.assertNotEqual(parsed_stage(bad, "build"), instructions(BUILD_STAGE)[1])

    def test_the_go_build_command_is_upstreams_container_build(self):
        # build_docker.sh at the tag (target client): mkctr with --gotags=ts_kube,ts_package_container and the
        # three version stamps; mkctr builds with CGO off and -trimpath. The stamps are upstream's own values.
        stage = BUILD_STAGE.replace("\\\n", " ")
        match = re.search(r"GOPROXY=off (go build .*? \./cmd/containerboot)", stage)
        self.assertIsNotNone(match)
        self.assertEqual(re.sub(r"\s+", " ", match.group(1)),
                         "go build -trimpath -tags ts_kube,ts_package_container -ldflags "
                         "\"-X tailscale.com/version.longStamp=${UPSTREAM_TAG#v}-t${UPSTREAM_COMMIT:0:9} "
                         "-X tailscale.com/version.shortStamp=${UPSTREAM_TAG#v} "
                         "-X tailscale.com/version.gitCommitStamp=${UPSTREAM_COMMIT}\" "
                         "-o /out/ ./cmd/tailscale ./cmd/tailscaled ./cmd/containerboot")
        self.assertIn("CGO_ENABLED=0", BUILD_STAGE)
        self.assertNotIn("-buildvcs=false", BUILD_STAGE)  # upstream's binaries carry the tag's commit and time

    def test_every_binary_is_compared_with_upstreams(self):
        for name in BINARIES:
            self.assertIn(f"same {name}", BUILD_STAGE)
        self.assertEqual(BUILD_STAGE.count("same "), len(BINARIES))


class RuntimeStageTest(unittest.TestCase):
    def setUp(self):
        self.text = read("Dockerfile.tailscale")
        self.rootfs = parsed_stage(self.text, "rootfs")
        self.final = parsed_stage(self.text, "tailscale")

    def test_the_three_binaries_replace_upstreams_in_place_with_upstreams_mode(self):
        run = [a for k, a in self.rootfs if k == "RUN"]
        self.assertEqual(len(run), 1)
        self.assertTrue(run[0].startswith("--network=none "), run[0][:40])
        self.assertIn("for b in tailscale tailscaled containerboot; do", run[0])
        self.assertIn('cat "/tmp/out/$b" >"/usr/local/bin/$b"', run[0])
        self.assertEqual(run[0].count('test "$(stat -c \'%a %u %g\' "/usr/local/bin/$b")" = "555 0 0"'), 2)
        # tailscale appends -dirty to the commit it prints when the build's tree was modified (go.mod, go.sum).
        # The rebuilt tools must print upstream's text with that one mark and nothing else changed.
        self.assertIn('want="$(printf \'%s\\n\' "${upstream_version}" | sed "s/^  tailscale commit: '
                      '${UPSTREAM_COMMIT}\\$/&-dirty/")"', run[0])
        self.assertIn('test "${want}" != "${upstream_version}"', run[0])
        self.assertIn('test "$(tailscaled --version)" = "${want}"', run[0])
        self.assertIn('test "$(tailscale version)" = "${want}"', run[0])
        self.assertIn("-dirty", read("scripts/smoke-tailscale.sh"))
        self.assertIn("rm -r /tmp/out /tmp/zlib.apk /tmp/libcrypto3.apk /tmp/libssl3.apk", run[0])

    def test_the_alpine_packages_are_pinned_files_at_the_fixed_versions(self):
        run = next(a for k, a in self.rootfs if k == "RUN")
        self.assertIn("apk add --no-network /tmp/zlib.apk /tmp/libcrypto3.apk /tmp/libssl3.apk", run)
        args = next(a for k, a in self.rootfs if k == "ARG").split()
        for name, fixed in PACKAGES.items():
            var = name.upper()
            with self.subTest(package=name):
                self.assertRegex(self.text, rf"(?m)^ARG {var}_APK=https://dl-cdn\.alpinelinux\.org/alpine/v3\.22/main/"
                                            rf"x86_64/{name}-{re.escape(fixed)}\.apk$")
                self.assertRegex(self.text, rf"(?m)^ARG {var}_SHA256=sha256:[0-9a-f]{{64}}$")
                self.assertIn(f"{var}_APK", args)
                self.assertIn(f"{var}_SHA256", args)
                self.assertIn(("ADD", f"--checksum=${{{var}_SHA256}} ${{{var}_APK}} /tmp/{name}.apk"), self.rootfs)
                self.assertIn(f'apk info -v | grep -qx "$(basename "${{{var}_APK}}" .apk)"', run)
        # The fixed packages go in after the binaries are checked, before the stage's files are removed.
        self.assertLess(run.index('test "$(tailscale version)" = "${want}"'), run.index("apk add --no-network"))
        self.assertLess(run.index("apk add --no-network"), run.index("rm -r /tmp/out"))

    def test_both_openssl_packages_come_from_one_build(self):
        # libssl3 requires the libcrypto3 of its own build; a mixed pair would not install.
        apk = {name: re.search(rf"(?m)^ARG {name.upper()}_APK=\S+/{name}-(\S+)\.apk$", self.text)
               for name in ("libcrypto3", "libssl3")}
        self.assertTrue(all(apk.values()), apk)
        self.assertEqual(apk["libcrypto3"].group(1), apk["libssl3"].group(1))

    def test_the_image_is_one_layer_with_upstreams_runtime_settings(self):
        # Upstream's image: no user (the deployment sets one), Cmd containerboot, no entrypoint, PATH, workdir /.
        keys = [k for k, _ in self.final]
        self.assertEqual(keys, ["ARG", "COPY", "ENV", "LABEL", "WORKDIR", "CMD"])
        self.assertIn(("COPY", "--from=rootfs / /"), self.final)
        self.assertIn(("ENV", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"), self.final)
        self.assertIn(("WORKDIR", "/"), self.final)
        self.assertIn(("CMD", '["/usr/local/bin/containerboot"]'), self.final)

    def test_the_labels_say_what_was_built_from_what(self):
        label = next(a for k, a in self.final if k == "LABEL")
        for want in ('org.opencontainers.image.source="https://github.com/FigureCollecting/store-images"',
                     'org.opencontainers.image.revision="${FC_COMMIT}"',
                     'org.opencontainers.image.version="${UPSTREAM_TAG}-${FC_REVISION}"',
                     'org.opencontainers.image.created="${CREATED}"',
                     'org.opencontainers.image.base.name="${UPSTREAM_IMAGE}"',
                     'org.opencontainers.image.licenses="BSD-3-Clause"',
                     'com.figurecollecting.upstream.tag="${UPSTREAM_TAG}"',
                     'com.figurecollecting.upstream.commit="${UPSTREAM_COMMIT}"',
                     'com.figurecollecting.tailscale-go.rev="${TS_GO_REV}"',
                     'com.figurecollecting.x-net.version="${XNET_VERSION}"',
                     'com.figurecollecting.x-net.fix-commit="${XNET_FIX_COMMIT}"',
                     'com.figurecollecting.compress.version="${COMPRESS_VERSION}"',
                     'com.figurecollecting.compress.fix-commit="${COMPRESS_FIX_COMMIT}"',
                     'com.figurecollecting.zlib.package="${ZLIB_APK}"',
                     'com.figurecollecting.libcrypto3.package="${LIBCRYPTO3_APK}"',
                     'com.figurecollecting.libssl3.package="${LIBSSL3_APK}"'):
            with self.subTest(label=want):
                self.assertIn(want, label)
        args = next(a for k, a in self.final if k == "ARG").split()
        for var in ("ZLIB_APK", "LIBCRYPTO3_APK", "LIBSSL3_APK"):
            self.assertIn(var, args)


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.smoke = read("scripts/smoke-tailscale.sh")

    def test_the_smoke_never_gives_a_container_a_network_or_a_key(self):
        runs = re.findall(r"docker (?:run|create) [^\n]*", self.smoke)
        self.assertGreaterEqual(len(runs), 4, runs)
        for line in runs:
            with self.subTest(line=line):
                self.assertIn("--network none", line)
        for word in ("TS_AUTHKEY", "TS_AUTH_KEY", "--auth-key", "--authkey", "tailscale up", "tailscale login"):
            self.assertNotIn(word, self.smoke)

    def test_the_smoke_runs_it_as_the_deployments_do(self):
        for want in ("--user 10001:10001", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                     "-e TS_USERSPACE=true", "-e TS_KUBE_SECRET=", "NeedsLogin"):
            self.assertIn(want, self.smoke)

    def test_the_smoke_checks_versions_modules_packages_and_the_file_system(self):
        for want in ("check version ", "check config ", "check labels ", "check packages ", "check rootfs ",
                     "for pkg in zlib libcrypto3 libssl3; do",
                     "com.figurecollecting.libcrypto3.package $LIBCRYPTO3_APK",
                     "com.figurecollecting.libssl3.package $LIBSSL3_APK",
                     r"(usr/)?lib/lib(crypto|ssl)\.so\.",
                     "check boot ", "check buildinfo ",
                     "dep[[:space:]]+golang.org/x/net[[:space:]]+${XNET_VERSION}[[:space:]]",
                     "dep[[:space:]]+github.com/klauspost/compress[[:space:]]+${COMPRESS_VERSION}[[:space:]]",
                     "scripts/rootfs_diff.py"):
            with self.subTest(want=want):
                self.assertIn(want, self.smoke)

    def test_the_smoke_is_handed_off_and_keeps_the_smoke_prefix(self):
        self.assertIn('name="${SMOKE_NAME:-${SMOKE_PREFIX:-store-images-smoke}-$app}"', self.smoke)
        self.assertTrue(os.access(os.path.join(ROOT, "scripts", "smoke-tailscale.sh"), os.X_OK))


class ReadmeTest(unittest.TestCase):
    def test_the_readme_lists_what_moved_and_when_to_delete_the_recipe(self):
        env = upstream_watch.read_env(os.path.join(ROOT, "images", "tailscale.env"))
        readme = " ".join(read("README.md").split())
        self.assertIn("## Tailscale", read("README.md"))
        for want in (f"`{XNET}` {env['XNET_VERSION']}", f"`{COMPRESS}` {env['COMPRESS_VERSION']}",
                     f"zlib {ZLIB_FIXED}", "CVE-2026-78661", "CVE-2026-116591", "CVE-2026-4738",
                     f"ghcr.io/figurecollecting/tailscale:{env['UPSTREAM_TAG']}-{env['FC_REVISION']}",
                     "Delete the recipe", f"libcrypto3 and libssl3 {OPENSSL_FIXED}", "CVE-2026-124709"):
            with self.subTest(want=want):
                self.assertIn(want, readme)
        self.assertNotIn("OpenSSL stays upstream's", readme)


if __name__ == "__main__":
    unittest.main()
