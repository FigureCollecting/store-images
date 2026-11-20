"""The VictoriaTraces rebuild pins the fixed Go and klauspost/compress, and the repository holds only what it builds."""
import glob
import io
import os
import re
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import upstream_watch  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
# The lowest versions that clear the findings of the scan gate on 2026-11-20:
# CVE-2026-97054 (Go stdlib, fixed in 1.27.4) and CVE-2026-116591 (klauspost/compress, fixed in v1.20.2).
GO_FLOOR = (1, 27, 4)
COMPRESS = "github.com/klauspost/compress"
COMPRESS_FLOOR = (1, 20, 2)


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def version(text):
    return tuple(int(x) for x in text.lstrip("v").split("."))


def definitions():
    return sorted(os.path.basename(p)[: -len(".env")] for p in glob.glob(os.path.join(ROOT, "images", "*.env")))


def stage(dockerfile, name):
    return dockerfile.split(f" AS {name}\n", 1)[1].split("\nFROM ", 1)[0]


class TracesPinsTest(unittest.TestCase):
    def setUp(self):
        self.env = upstream_watch.read_env(os.path.join(ROOT, "images", "victoria-traces.env"))

    def test_the_base_is_the_release_the_live_store_runs(self):
        self.assertEqual(self.env["UPSTREAM_TAG"], "v0.13.0")
        self.assertEqual(self.env["UPSTREAM_COMMIT"], "f0295d1f796d2c7880ee2fdaf899d0ba754f4d8a")
        self.assertEqual(self.env["UPSTREAM_DESCRIBE"], "tags-v0.13.0-0-gf0295d1f7")
        self.assertRegex(self.env["UPSTREAM_IMAGE"],
                         r"^docker\.io/victoriametrics/victoria-traces:v0\.13\.0@sha256:[0-9a-f]{64}$")
        self.assertEqual(self.env["UPSTREAM_GO_VERSION"], "1.27.3")

    def test_go_is_at_or_above_the_fixed_patch_release_and_the_builder_matches(self):
        go = self.env["GO_VERSION"]
        self.assertGreaterEqual(version(go), GO_FLOOR)
        self.assertGreater(version(go), version(self.env["UPSTREAM_GO_VERSION"]))
        self.assertRegex(read("Dockerfile"),
                         rf"(?m)^ARG GO_IMAGE=docker\.io/library/golang:{re.escape(go)}@sha256:[0-9a-f]{{64}}$")

    def test_compress_is_at_or_above_the_fixed_version(self):
        self.assertEqual(self.env["UPSTREAM_PATCH_MODULE"], COMPRESS)
        self.assertGreaterEqual(version(self.env["UPSTREAM_PATCH_VERSION"]), COMPRESS_FLOOR)

    def test_the_patch_moves_exactly_compress_in_go_mod_and_go_sum(self):
        patch = read("patches/victoria-traces.patch")
        want = self.env["UPSTREAM_PATCH_VERSION"]
        added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
        removed = [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(re.findall(r"(?m)^diff --git a/(\S+) ", patch), ["go.mod", "go.sum"])
        self.assertEqual(len(added), 3, added)
        self.assertEqual(len(removed), 3, removed)
        self.assertEqual(added[0], f"\t{COMPRESS} {want}")
        self.assertRegex(added[1], rf"^{re.escape(COMPRESS)} {re.escape(want)} h1:[A-Za-z0-9+/]{{43}}=$")
        self.assertRegex(added[2], rf"^{re.escape(COMPRESS)} {re.escape(want)}/go\.mod h1:[A-Za-z0-9+/]{{43}}=$")
        for line in removed:
            self.assertTrue(line.strip().startswith(COMPRESS + " "), line)
            self.assertNotIn(want, line)


class BuildGuardsTest(unittest.TestCase):
    def setUp(self):
        self.text = read("Dockerfile")
        self.build = stage(self.text, "build")

    def test_the_build_checks_the_toolchain_and_the_patch_footprint(self):
        self.assertIn('RUN --network=none test "$(go env GOVERSION)" = "go${GO_VERSION}"', self.build)
        self.assertIn("git apply --check /tmp/deps.patch && git apply /tmp/deps.patch", self.build)
        self.assertIn('test "$(git diff --numstat | tr \'\\t\\n\' \'  \')" = "1 1 go.mod 2 2 go.sum "', self.build)
        self.assertIn('grep -qxP "\\t${UPSTREAM_PATCH_MODULE} ${UPSTREAM_PATCH_VERSION}" go.mod', self.build)

    def test_the_build_is_offline_and_from_the_module_cache(self):
        self.assertIn("GOFLAGS=-mod=readonly", self.build)
        self.assertNotIn("-mod=vendor", self.build)
        self.assertRegex(self.build, r"(?m)^RUN go mod download$")
        self.assertIn("RUN --network=none GOPROXY=off go build ", self.build)

    def test_the_binary_differs_from_upstream_only_in_go_and_the_patched_module(self):
        self.assertIn("COPY --from=upstream /${APP_NAME}-prod /tmp/upstream", self.build)
        for guard in ('test "$(head -n 1 /tmp/buildinfo)" = "/out/${APP_NAME}-prod: go${GO_VERSION}"',
                      'test "$(head -n 1 /tmp/upstream-buildinfo)" = "/tmp/upstream: go${UPSTREAM_GO_VERSION}"',
                      'grep -qP "^\\tdep\\t${UPSTREAM_PATCH_MODULE}\\t${UPSTREAM_PATCH_VERSION}\\t" /tmp/buildinfo',
                      'grep -cP "^\\tdep\\t${UPSTREAM_PATCH_MODULE}\\t" /tmp/upstream-buildinfo | grep -qx 1',
                      "diff <(norm /tmp/upstream-buildinfo) <(norm /tmp/buildinfo)",
                      'test "$(git status --porcelain --untracked-files=no | tr \'\\n\' \' \')" = " M go.mod  M go.sum "'):
            with self.subTest(guard=guard):
                self.assertIn(guard, self.build)

    def test_the_upstream_image_is_a_digest_pinned_stage(self):
        self.assertIn("FROM ${UPSTREAM_IMAGE} AS upstream\n", self.text)
        env = upstream_watch.read_env(os.path.join(ROOT, "images", "victoria-traces.env"))
        self.assertIn(f"\nARG UPSTREAM_IMAGE={env['UPSTREAM_IMAGE']}\n", self.text)  # the pin check reads defaults
        self.assertRegex(read("scripts/build.sh"), r"\bUPSTREAM_IMAGE\b")
        for key in ("UPSTREAM_GO_VERSION", "UPSTREAM_PATCH_MODULE", "UPSTREAM_PATCH_VERSION"):
            self.assertRegex(read("scripts/build.sh"), rf"\b{key}\b")
        self.assertNotIn("UPSTREAM_GO_FIX_COMMIT", read("scripts/build.sh") + self.text)


class InventoryTest(unittest.TestCase):
    def test_only_victoria_traces_is_rebuilt(self):
        # VictoriaLogs v1.54.0 ships Go 1.27.4 and compress v1.20.4 and passes the gate: the store runs upstream's image.
        self.assertEqual(definitions(), ["victoria-traces"])
        self.assertFalse(os.path.exists(os.path.join(ROOT, "patches", "victoria-logs.patch")))

    def test_every_workflow_matrix_and_dockerfile_target_matches_the_definitions(self):
        names = definitions()
        workflow = read(".github/workflows/images.yml")
        matrices = re.findall(r"(?m)^\s+app: \[([^\]]*)\]$", workflow)
        self.assertEqual(len(matrices), 2)
        for matrix in matrices:
            self.assertEqual(sorted(a.strip() for a in matrix.split(",")), names)
        targets = re.findall(r"(?m)^FROM runtime AS (\S+)$", read("Dockerfile"))
        self.assertEqual(sorted(targets), names)

    def test_the_weekly_watch_passes_against_todays_upstream_releases(self):
        releases = {"VictoriaMetrics/VictoriaTraces": [{"tag_name": "v0.13.0"}, {"tag_name": "v0.12.1"}]}
        out = io.StringIO()
        with redirect_stdout(out):
            rc = upstream_watch.main(["--images-dir", os.path.join(ROOT, "images")], fetch=releases.__getitem__)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("victoria-traces: base v0.13.0, newest upstream release v0.13.0: no newer release", out.getvalue())


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.smoke = read("scripts/smoke.sh")

    def test_the_smoke_writes_a_gzip_otlp_span_and_reads_it_back(self):
        self.assertIn("/insert/opentelemetry/v1/traces", self.smoke)
        self.assertIn("Content-Encoding: gzip", self.smoke)
        self.assertIn("/select/jaeger/api/services", self.smoke)
        self.assertRegex(self.smoke, r'check write ')
        self.assertRegex(self.smoke, r'check query ')

    def test_the_smoke_checks_the_patched_module_in_the_binary(self):
        self.assertIn("dep[[:space:]]+${UPSTREAM_PATCH_MODULE}[[:space:]]+${UPSTREAM_PATCH_VERSION}[[:space:]]",
                      self.smoke)

    def test_the_check_helper_does_not_shadow_the_container_name(self):
        # post_gzip runs inside check and needs the global $name (the docker network); a local "name" hid it.
        body = self.smoke.split("check() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotRegex(body, r"\blocal\b[^\n]*\bname=")
        self.assertIn('--network "$name"', self.smoke.split("post_gzip() {", 1)[1].split("\n}\n", 1)[0])

    def test_the_smoke_network_has_no_way_out(self):
        self.assertIn('docker network create --internal "$name"', self.smoke)

    def test_the_smoke_object_names_can_be_prefixed(self):
        self.assertIn('name="${SMOKE_PREFIX:-store-images-smoke}-$app"', self.smoke)


if __name__ == "__main__":
    unittest.main()
