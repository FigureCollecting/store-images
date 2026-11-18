"""The Grafana recipe keeps its server rebuild: the patched module version cannot be dropped quietly."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import upstream_watch  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class ServerRebuildTest(unittest.TestCase):
    def setUp(self):
        self.env = upstream_watch.read_env(os.path.join(ROOT, "images", "grafana.env"))
        self.module = self.env["UPSTREAM_PATCH_MODULE"]
        self.version = self.env["UPSTREAM_PATCH_VERSION"]

    def test_the_patch_moves_exactly_the_named_module_to_the_named_version(self):
        patch = read("patches/grafana-server.patch")
        added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
        removed = [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(len(added), 3, added)
        self.assertEqual(len(removed), 3, removed)
        self.assertEqual(added[0], f"\t{self.module} {self.version} // indirect")
        self.assertRegex(added[1], rf"^{re.escape(self.module)} {re.escape(self.version)} h1:[A-Za-z0-9+/]{{43}}=$")
        self.assertRegex(added[2], rf"^{re.escape(self.module)} {re.escape(self.version)}/go\.mod h1:[A-Za-z0-9+/]{{43}}=$")
        for line in removed:
            self.assertTrue(line.strip().startswith(self.module + " "), line)
            self.assertNotIn(self.version, line)
        self.assertEqual(re.findall(r"(?m)^diff --git a/(\S+) ", patch), ["go.mod", "go.sum"])

    def test_the_build_refuses_a_server_without_the_patched_version(self):
        text = read("Dockerfile.grafana")
        stage = text.split("FROM go AS server", 1)[1].split("\nFROM ", 1)[0]
        self.assertIn("COPY patches/grafana-server.patch", stage)
        self.assertIn("git apply /tmp/deps.patch", stage)
        self.assertIn('grep -qP "^\\tdep\\t${UPSTREAM_PATCH_MODULE}\\t${UPSTREAM_PATCH_VERSION}\\t" /tmp/buildinfo', stage)
        self.assertIn("COPY --from=server /out/grafana ", text)

    def test_the_build_is_told_the_module_and_version(self):
        build = read("scripts/build.sh")
        for key in ("UPSTREAM_PATCH_MODULE", "UPSTREAM_PATCH_VERSION", "UPSTREAM_COMMIT", "UPSTREAM_BUILD_STAMP"):
            self.assertIn(key, self.env)
            self.assertRegex(build, rf"\b{key}\b")

    def test_the_smoke_test_checks_the_running_server(self):
        smoke = read("scripts/smoke-grafana.sh")
        self.assertIn("server dep[[:space:]]+${UPSTREAM_PATCH_MODULE}[[:space:]]+${UPSTREAM_PATCH_VERSION}[[:space:]]", smoke)
        self.assertIn("fc-${UPSTREAM_PATCH_MODULE##*/}-${UPSTREAM_PATCH_VERSION}", smoke)

    def test_the_readme_says_when_to_drop_the_rebuild(self):
        readme = " ".join(read("README.md").split())
        self.assertIn(f"`{self.module}` {self.version}", readme)
        self.assertIn("Drop the server rebuild", readme)


if __name__ == "__main__":
    unittest.main()
