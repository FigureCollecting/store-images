"""The Grafana recipe is at or above the release the cluster requires, and uses upstream's server unchanged
only while that server carries the module fix the earlier builds had to patch in."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import upstream_watch  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
# fc-infra refuses to apply a Grafana image below this release (viewer APPLY.md, GRAFANA_FLOOR): 13.3.2 fixes
# CVE-2026-107561 and CVE-2026-88960 in the server's own code.
GRAFANA_FLOOR = (13, 3, 2, 0)
# github.com/apache/thrift v0.24.0 is what the scan gate reported in upstream's 13.3.1 server.
FIXED_MODULE, FIXED_FLOOR = "github.com/apache/thrift", (0, 25, 0, 0)


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.env = upstream_watch.read_env(os.path.join(ROOT, "images", "grafana.env"))

    def test_the_recipe_is_at_or_above_the_release_the_cluster_requires(self):
        self.assertGreaterEqual(upstream_watch.version(self.env["UPSTREAM_TAG"]), GRAFANA_FLOOR)

    def test_the_pins_name_the_13_3_2_release(self):
        self.assertEqual(self.env["UPSTREAM_TAG"], "v13.3.2")
        self.assertEqual(self.env["UPSTREAM_COMMIT"], "b8e90ef2a99c8629339e7233a7a813456d87c075")
        self.assertEqual(self.env["UPSTREAM_BUILD_COMMIT"], "bd754986872f554d30a147ebd605a7fe15881a28")
        self.assertEqual(self.env["UPSTREAM_BUILD_BRANCH"], "release-13.3.2#patched")
        self.assertEqual(self.env["FC_REVISION"], "fc1")

    def test_the_base_image_is_upstreams_image_of_that_tag_pinned_by_digest(self):
        tag = self.env["UPSTREAM_TAG"][1:]
        self.assertRegex(read("Dockerfile.grafana"),
                         rf"(?m)^ARG GRAFANA_IMAGE=docker\.io/grafana/grafana:{re.escape(tag)}@sha256:[0-9a-f]{{64}}$")

    def test_no_file_names_the_old_release(self):
        for name in ("Dockerfile.grafana", "images/grafana.env", "README.md", "scripts/smoke-grafana.sh"):
            with self.subTest(name=name):
                self.assertNotIn("13.3.1", read(name))


class UpstreamServerTest(unittest.TestCase):
    def setUp(self):
        self.env = upstream_watch.read_env(os.path.join(ROOT, "images", "grafana.env"))
        self.module = self.env.get("UPSTREAM_FIXED_MODULE", "")
        self.version = self.env.get("UPSTREAM_FIXED_VERSION", "")

    def test_the_server_rebuild_and_its_patch_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "patches", "grafana-server.patch")))
        text = read("Dockerfile.grafana")
        self.assertNotIn("grafana-server.patch", text)
        self.assertNotIn("/tmp/grafana-server", text)
        self.assertNotIn("/usr/share/grafana/bin/grafana;", text)
        for key in ("UPSTREAM_PATCH_MODULE", "UPSTREAM_PATCH_VERSION", "UPSTREAM_FIX_COMMIT", "UPSTREAM_BUILD_STAMP"):
            with self.subTest(key=key):
                self.assertNotIn(key, self.env)
                self.assertNotIn(key, text)

    def test_the_fixed_module_is_at_or_above_the_version_that_cleared_the_scan(self):
        self.assertEqual(self.module, FIXED_MODULE)
        self.assertGreaterEqual(upstream_watch.version(self.version), FIXED_FLOOR)

    def test_the_build_refuses_an_upstream_server_without_the_fix_or_of_another_build(self):
        text = read("Dockerfile.grafana")
        stage = text.split("FROM go AS server\n", 1)[1].split("\nFROM ", 1)[0]
        self.assertIn("COPY --from=upstream /usr/share/grafana/bin/grafana /tmp/upstream", stage)
        self.assertIn('test "$(head -n 1 /tmp/buildinfo)" = "/tmp/upstream: go${UPSTREAM_GO_VERSION}"', stage)
        self.assertIn('grep -cP "^\\tdep\\t${UPSTREAM_FIXED_MODULE}\\t${UPSTREAM_FIXED_VERSION}\\t" /tmp/buildinfo '
                      '| grep -qx 1', stage)
        self.assertIn('grep -qxP "\\tpath\\tgithub.com/grafana/grafana/pkg/cmd/grafana" /tmp/buildinfo', stage)
        self.assertIn("COPY --from=server /tmp/buildinfo /tmp/server-buildinfo", text)
        rootfs = text.split("FROM upstream AS rootfs", 1)[1].split("\nFROM ", 1)[0]
        self.assertIn('test "$(grafana server -v)" = "Version ${UPSTREAM_TAG#v} (commit: ${UPSTREAM_BUILD_COMMIT}, '
                      'branch: ${UPSTREAM_BUILD_BRANCH})"', rootfs)
        self.assertIn("/tmp/server-buildinfo", rootfs.split("    rm /tmp/vlogs.zip", 1)[1].split("\n", 1)[0])

    def test_the_build_is_told_the_server_pins(self):
        build = read("scripts/build.sh")
        for key in ("UPSTREAM_COMMIT", "UPSTREAM_BUILD_COMMIT", "UPSTREAM_BUILD_BRANCH", "UPSTREAM_GO_VERSION",
                    "UPSTREAM_FIXED_MODULE", "UPSTREAM_FIXED_VERSION"):
            with self.subTest(key=key):
                self.assertIn(key, self.env)
                self.assertRegex(build, rf"\b{key}\b")

    def test_the_smoke_test_checks_the_running_server(self):
        smoke = read("scripts/smoke-grafana.sh")
        self.assertIn("server ^/b/server: go$UPSTREAM_GO_VERSION\\$", smoke)
        self.assertIn("server dep[[:space:]]+${UPSTREAM_FIXED_MODULE}[[:space:]]+${UPSTREAM_FIXED_VERSION}[[:space:]]",
                      smoke)
        self.assertIn('"Version ${UPSTREAM_TAG#v} (commit: ${UPSTREAM_BUILD_COMMIT}, branch: ${UPSTREAM_BUILD_BRANCH})"',
                      smoke)
        self.assertIn('is "$health" .commit "$UPSTREAM_BUILD_COMMIT"', smoke)
        self.assertNotIn("UPSTREAM_PATCH", smoke)

    def test_every_rebuilt_binary_is_checked_against_the_pinned_go(self):
        text = read("Dockerfile.grafana")
        go = self.env["GO_VERSION"]
        self.assertRegex(text, rf"(?m)^ARG GO_IMAGE=docker\.io/library/golang:{re.escape(go)}@sha256:[0-9a-f]{{64}}$")
        self.assertIn('RUN --network=none test "$(go env GOVERSION)" = "go${GO_VERSION}"', text)
        for stage in ("jaeger", "vlogs"):
            with self.subTest(stage=stage):
                body = text.split(f"FROM go AS {stage}\n", 1)[1].split("\nFROM ", 1)[0]
                self.assertRegex(body, r"(?m)^ARG .*\bGO_VERSION\b|^    .*\bGO_VERSION\b")
                self.assertIn('test "$(head -n 1 /tmp/buildinfo)" = "/out/backend: go${GO_VERSION}"', body)
        # upstream's server is not rebuilt, so its Go must be no older than the one the backends get.
        self.assertGreaterEqual(tuple(int(x) for x in self.env["UPSTREAM_GO_VERSION"].split(".")),
                                tuple(int(x) for x in go.split(".")))


class HardeningTest(unittest.TestCase):
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

    def test_the_readme_says_the_server_is_upstreams_and_what_the_build_checks(self):
        readme = " ".join(read("README.md").split())
        env = upstream_watch.read_env(os.path.join(ROOT, "images", "grafana.env"))
        self.assertIn("UPSTREAM_FIXED_MODULE", env)
        self.assertIn(f"`{env['UPSTREAM_FIXED_MODULE']}` {env['UPSTREAM_FIXED_VERSION']}", readme)
        self.assertIn("upstream's binary, unchanged", readme)
        self.assertNotIn("Drop the server rebuild", readme)

    def test_the_readme_says_upstreams_server_is_a_private_build_that_lacks_two_public_tag_commits(self):
        readme = " ".join(read("README.md").split())
        self.assertIn("| Grafana server | release build bd75498687 (tag v13.3.2, b8e90ef2a9):", readme)
        self.assertNotIn("Up to the 13.3 release before it", readme)
        limits = readme.split("## Grafana", 1)[1].split("## Tailscale", 1)[0].split("Known limits:", 1)[1]
        for needle in ("not a build of the public tag", "#138361", "#138800", "both security patches",
                       "`com.figurecollecting.upstream.commit`"):
            with self.subTest(needle=needle):
                self.assertIn(needle, limits)

    def test_the_header_does_not_call_upstreams_grafana_files_root_owned(self):
        header = " ".join(line.lstrip("# ") for line in read("Dockerfile.grafana").split("\nARG ", 1)[0].splitlines())
        self.assertNotIn("everything else outside the data, log and temp paths root-owned", header)
        self.assertIn("1000:1000", header)


if __name__ == "__main__":
    unittest.main()
