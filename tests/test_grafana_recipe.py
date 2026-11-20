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

    def test_every_rebuilt_binary_is_checked_against_the_pinned_go(self):
        text = read("Dockerfile.grafana")
        go = self.env["GO_VERSION"]
        self.assertRegex(text, rf"(?m)^ARG GO_IMAGE=docker\.io/library/golang:{re.escape(go)}@sha256:[0-9a-f]{{64}}$")
        self.assertIn('RUN --network=none test "$(go env GOVERSION)" = "go${GO_VERSION}"', text)
        for stage, binary in (("server", "/out/grafana"), ("jaeger", "/out/backend"), ("vlogs", "/out/backend")):
            with self.subTest(stage=stage):
                body = text.split(f"FROM go AS {stage}\n", 1)[1].split("\nFROM ", 1)[0]
                self.assertRegex(body, r"(?m)^ARG .*\bGO_VERSION\b|^    .*\bGO_VERSION\b")
                self.assertIn(f'test "$(head -n 1 /tmp/buildinfo)" = "{binary}: go${{GO_VERSION}}"', body)
        self.assertGreaterEqual(tuple(int(x) for x in go.split(".")),
                                tuple(int(x) for x in self.env["UPSTREAM_GO_VERSION"].split(".")))

    def test_the_image_defaults_point_every_grafana_com_address_at_loopback(self):
        text = read("Dockerfile.grafana")
        for line in ("GF_GRAFANA_COM_URL=http://127.0.0.1:9 \\", "GF_GRAFANA_COM_API_URL=http://127.0.0.1:9/api \\",
                     "GF_GRAFANA_NET_URL=http://127.0.0.1:9 \\"):
            self.assertIn(f"    {line}\n", text)
        smoke = read("scripts/smoke-grafana.sh")
        self.assertIn("api /api/gnet/plugins", smoke)
        self.assertIn(".grafana_com.api_url http://127.0.0.1:9/api", smoke)

    def test_nothing_time_stamped_is_written_and_the_plugin_trees_are_locked(self):
        text = read("Dockerfile.grafana")
        self.assertIn("apk add --no-network --no-logfile /tmp/zlib.apk /tmp/libcrypto3.apk /tmp/libssl3.apk", text)
        self.assertIn('test "$(sha256sum /var/log/apk.log)" = "${apklog}"', text)
        self.assertNotIn("-o grafana", text)
        self.assertIn("/usr/share/grafana/plugins /usr/share/grafana/data/plugins-bundled ! -type l "
                      "\\( ! -user root -o -perm -0020 -o -perm -0002 \\)", text)

    def test_the_alpine_packages_are_pinned_files_at_the_fixed_versions_and_the_readme_lists_them(self):
        text, readme = read("Dockerfile.grafana"), read("README.md")
        fixed = {"zlib": "1.4-r0", "libcrypto3": "3.5.9-r1", "libssl3": "3.5.9-r1"}
        for name, version in fixed.items():
            with self.subTest(package=name):
                var = name.upper()
                self.assertRegex(text, rf"(?m)^ARG {var}_APK=https://dl-cdn\.alpinelinux\.org/alpine/v3\.24/main/"
                                       rf"x86_64/{name}-{re.escape(version)}\.apk$")
                self.assertRegex(text, rf"(?m)^ARG {var}_SHA256=sha256:[0-9a-f]{{64}}$")
                self.assertIn(f"ADD --checksum=${{{var}_SHA256}} ${{{var}_APK}} /tmp/{name}.apk", text)
                self.assertIn(f'apk info -v | grep -qx "$(basename "${{{var}_APK}}" .apk)"', text)
                self.assertIn(f"/tmp/{name}.apk", text.split("    rm /tmp/vlogs.zip", 1)[1].split("\n", 1)[0])
                self.assertRegex(readme, rf"(?m)^\| {name}[^|]*\| [^|]+ \| {re.escape(version)} \|$")

    def test_the_grafana_smoke_is_handed_off_and_keeps_the_smoke_prefix(self):
        # scripts/smoke.sh execs smoke-grafana.sh before it names anything, so smoke-grafana.sh must honour
        # SMOKE_PREFIX itself, or a prefixed run (one hold, several smokes) would collide on the default name.
        main = read("scripts/smoke.sh")
        handoff = main.index('[ -z "${SMOKE:-}" ] || exec "$ROOT/scripts/smoke-$SMOKE.sh" "$@"')
        self.assertLess(handoff, main.index('name="${SMOKE_PREFIX:-store-images-smoke}-$app"'))
        self.assertIn('name="${SMOKE_NAME:-${SMOKE_PREFIX:-store-images-smoke}-$app}"',
                      read("scripts/smoke-grafana.sh"))

    def test_cloud_migration_is_off_and_the_smoke_test_asks_the_server(self):
        self.assertIn("    GF_CLOUD_MIGRATION_ENABLED=false \\\n", read("Dockerfile.grafana"))
        smoke = read("scripts/smoke-grafana.sh")
        self.assertIn("\n.cloud_migration.enabled false\n", smoke)
        self.assertIn("api /api/cloudmigration/migration -X POST", smoke)

    def test_every_default_of_ours_is_listed_in_the_readme_and_read_back_by_the_smoke_test(self):
        text = read("Dockerfile.grafana")
        ours = text.split("ENV GF_PATHS_PLUGINS=", 1)[1].split("\nLABEL ", 1)[0]
        names = ["GF_PATHS_PLUGINS"] + re.findall(r"(?m)^    (GF_[A-Z_]+)=", ours)
        self.assertGreaterEqual(len(names), 16, names)
        readme, smoke = read("README.md"), read("scripts/smoke-grafana.sh")
        for name in names:
            with self.subTest(name=name):
                self.assertRegex(readme, rf"`{name}[=`]")
                if name not in ("GF_PATHS_PLUGINS", "GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS"):
                    section = next(s for s in ("cloud_migration", "grafana_com", "grafana_net", "analytics", "news",
                                               "plugins", "security", "snapshots", "provisioning")
                                   if name.startswith(f"GF_{s.upper()}_"))
                    key = name[len(section) + 4:].lower()
                    self.assertRegex(smoke, rf"(?m)^\.{section}\.{key} \S+$")

    def test_git_sync_is_off_so_the_server_cannot_dial_a_repository_host_and_file_provisioning_stays(self):
        self.assertIn("    GF_PROVISIONING_ENABLED=false \\\n", read("Dockerfile.grafana"))
        smoke = read("scripts/smoke-grafana.sh")
        self.assertIn("\n.provisioning.enabled false\n", smoke)
        self.assertIn("apis/provisioning.grafana.app/", smoke)
        readme = read("README.md")
        self.assertIn("`[provisioning] enabled`", readme)
        self.assertNotIn("provisioned Git repositories reach the hosts", readme)

    def test_nothing_outside_the_data_log_and_temp_paths_is_writable_by_the_grafana_user(self):
        text = read("Dockerfile.grafana")
        rootfs = text.split("FROM upstream AS rootfs", 1)[1].split("\nFROM ", 1)[0]
        self.assertIn("chmod 0755 /run.sh", rootfs)
        guard = ("find / -xdev \\( -path /proc -o -path /sys -o -path /dev -o -path /tmp -o -path /var/tmp "
                 "-o -path /var/lib/grafana -o -path /var/log/grafana \\) -prune "
                 "-o ! -type l \\( -user 472 -o -group 0 -perm -0020 -o -perm -0002 \\) -print")
        self.assertIn(f'test -z "$({guard})"', rootfs)
        smoke = read("scripts/smoke-grafana.sh")
        self.assertIn("check writable ", smoke)
        self.assertIn("check entrypoint ", smoke)
        self.assertIn("readOnlyRootFilesystem", read("README.md"))

    def test_the_readme_says_when_to_drop_the_rebuild(self):
        readme = " ".join(read("README.md").split())
        self.assertIn(f"`{self.module}` {self.version}", readme)
        self.assertIn("Drop the server rebuild", readme)


if __name__ == "__main__":
    unittest.main()
