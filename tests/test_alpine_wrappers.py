"""The three apk-upgrade wrappers: node-npm (node:24 alpine with a current npm and no corepack), psql (Alpine with the
PostgreSQL 17 client) and valkey (valkey's alpine image). Each is an upstream image pinned by digest, `apk upgrade`d and
flattened into one layer, with upstream's own command, entrypoint and settings. A recipe is deleted when upstream's own
image passes the scan gate."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))
import upstream_watch  # noqa: E402
from test_victoria_recipe import instructions, parsed_stage  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
# FC_REVISIONs already published to ghcr.io/figurecollecting/<app>; a tag is never overwritten, so a change to what an
# image holds needs a revision not in its list. Nothing is published yet.
PUBLISHED_REVISIONS = {"node-npm": (), "psql": (), "valkey": ()}
# The lowest versions that clear the findings of the scan gate on 2026-11-25 (docs/README: "What is wrapped").
NPM_FLOOR = (11, 21, 3)
BRACE_EXPANSION_FLOOR = (5, 0, 11)
PGCLIENT_FIXED = "17.12-r0"
BASES = {
    "node-npm": r"docker\.io/library/node:24\.22\.3-alpine@sha256:[0-9a-f]{64}",
    "psql": r"docker\.io/library/alpine:3\.24\.4@sha256:[0-9a-f]{64}",
    "valkey": r"docker\.io/valkey/valkey:9\.1\.4-alpine3\.24@sha256:[0-9a-f]{64}",
}
# What upstream's image runs, which the wrapper must repeat exactly (the smoke compares them with the registry's).
FINAL_KEYS = {
    "node-npm": ["ARG", "COPY", "ENV", "LABEL", "WORKDIR", "ENTRYPOINT", "CMD"],
    "psql": ["ARG", "COPY", "ENV", "LABEL", "WORKDIR", "CMD"],
    "valkey": ["ARG", "COPY", "ENV", "LABEL", "WORKDIR", "EXPOSE", "ENTRYPOINT", "CMD"],
}


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def env_of(app):
    return upstream_watch.read_env(os.path.join(ROOT, "images", f"{app}.env"))


def version(text):
    return tuple(int(x) for x in re.split(r"[.-]", text.lstrip("v").replace("-r", ".")) if x.isdigit())


class WrapperTest(unittest.TestCase):
    app = None

    def setUp(self):
        if self.app is None:
            self.skipTest("base class")
        self.env = env_of(self.app)
        self.text = read(f"Dockerfile.{self.app}")
        self.rootfs = parsed_stage(self.text, "rootfs")
        self.final = parsed_stage(self.text, self.app)

    def test_the_definition_names_the_image_the_dockerfile_and_the_retire_action(self):
        env = self.env
        self.assertEqual(env["APP_NAME"], self.app)
        self.assertEqual(env["IMAGE"], f"ghcr.io/figurecollecting/{self.app}")
        self.assertEqual(env["DOCKERFILE"], f"Dockerfile.{self.app}")
        self.assertEqual(env["SMOKE"], self.app)
        self.assertEqual(env["WATCH_ACTION"], "retire")
        self.assertRegex(env["FC_REVISION"], r"^fc[1-9][0-9]*$")
        self.assertNotIn(env["FC_REVISION"], PUBLISHED_REVISIONS[self.app])

    def test_the_base_is_one_upstream_image_pinned_by_digest_and_the_dockerfile_default_is_the_same(self):
        self.assertRegex(self.env["UPSTREAM_IMAGE"], rf"^{BASES[self.app]}$")
        self.assertIn(f"\nARG UPSTREAM_IMAGE={self.env['UPSTREAM_IMAGE']}\n", self.text)  # the pin check reads defaults
        self.assertEqual([a for k, a in instructions(self.text)[1] if k == "FROM"],
                         ["${UPSTREAM_IMAGE} AS rootfs", f"scratch AS {self.app}"])

    def test_the_dockerfile_has_no_parser_directive_heredoc_or_onbuild(self):
        directives, ins = instructions(self.text)
        self.assertEqual(directives, {})
        self.assertEqual([i for i in ins if "<<" in i[1] or i[0] == "ONBUILD"], [])

    def test_the_rootfs_stage_upgrades_the_alpine_packages_first_in_one_run(self):
        run = [a for k, a in self.rootfs if k == "RUN"]
        self.assertEqual(len(run), 1)
        self.assertTrue(run[0].startswith("apk upgrade --no-cache"), run[0][:60])
        self.assertNotIn("--network=none", run[0])  # the upgrade and any install need the mirror
        # apk leaves its two APKINDEX downloads (3 MB) in /var/cache/apk even with --no-cache; the image carries none.
        self.assertRegex(run[0], r"rm -rf [^&]*/var/cache/apk/\*")
        self.assertLess(run[0].rindex("apk "), run[0].index("/var/cache/apk/*"))

    def test_the_image_is_one_layer_with_upstreams_runtime_settings(self):
        self.assertEqual([k for k, _ in self.final], FINAL_KEYS[self.app])
        self.assertIn(("COPY", "--from=rootfs / /"), self.final)
        self.assertNotIn("USER", [k for k, _ in self.final])  # upstream sets none; the deployment does

    def test_the_labels_say_what_was_built_from_what(self):
        label = next(a for k, a in self.final if k == "LABEL")
        for want in ('org.opencontainers.image.source="https://github.com/FigureCollecting/store-images"',
                     'org.opencontainers.image.revision="${FC_COMMIT}"',
                     'org.opencontainers.image.version="${UPSTREAM_TAG}-${FC_REVISION}"',
                     'org.opencontainers.image.created="${CREATED}"',
                     'org.opencontainers.image.base.name="${UPSTREAM_IMAGE}"',
                     'com.figurecollecting.upstream.tag="${UPSTREAM_TAG}"'):
            with self.subTest(label=want):
                self.assertIn(want, label)

    def test_the_build_is_told_every_key_of_the_definition_the_dockerfile_reads(self):
        build = read("scripts/build.sh")
        for key in ("UPSTREAM_IMAGE", "UPSTREAM_TAG", "FC_REVISION", *self.extra_keys):
            with self.subTest(key=key):
                self.assertIn(key, self.env)
                self.assertRegex(build, rf"\b{key}\b")
                self.assertRegex(self.text, rf"\b{key}\b")

    extra_keys = ()

    def test_the_smoke_is_executable_has_no_network_and_reports_pass_or_fail(self):
        path = os.path.join(ROOT, "scripts", f"smoke-{self.app}.sh")
        self.assertTrue(os.access(path, os.X_OK))
        smoke = read(f"scripts/smoke-{self.app}.sh")
        runs = re.findall(r"docker (?:run|create) [^\n]*", smoke)
        self.assertGreaterEqual(len(runs), 1, runs)
        for line in runs:
            with self.subTest(line=line):
                self.assertIn("--network none", line)
        self.assertIn('echo "SMOKE $([ "$failures" = 0 ] && echo PASS || echo "FAIL ($failures)") $app $img"', smoke)
        for want in ("check config ", "check labels ", "check packages "):
            self.assertIn(want, smoke)

    def test_the_pull_request_and_publish_matrices_build_it(self):
        workflow = read(".github/workflows/images.yml")
        self.assertEqual(len(re.findall(rf"app: \[[^\]]*\b{re.escape(self.app)}\b[^\]]*\]", workflow)), 2)

    def test_the_readme_says_what_is_wrapped_and_when_to_delete_the_recipe(self):
        readme = " ".join(read("README.md").split())
        self.assertIn(f"ghcr.io/figurecollecting/{self.app}:{self.env['UPSTREAM_TAG']}-{self.env['FC_REVISION']}", readme)
        self.assertIn("## Alpine wrappers", read("README.md"))
        self.assertIn(f"### {self.app}", read("README.md"))


class NodeNpmTest(WrapperTest):
    app = "node-npm"
    extra_keys = ("NPM_VERSION", "BRACE_EXPANSION_MIN")

    def test_npm_is_the_newest_eleven_and_brace_expansion_has_its_floor(self):
        self.assertTrue(self.env["NPM_VERSION"].startswith("11."))
        self.assertGreaterEqual(version(self.env["NPM_VERSION"]), NPM_FLOOR)
        self.assertGreaterEqual(version(self.env["BRACE_EXPANSION_MIN"]), BRACE_EXPANSION_FLOOR)

    def test_the_run_installs_that_npm_checks_brace_expansion_and_removes_corepack(self):
        run = next(a for k, a in self.rootfs if k == "RUN")
        self.assertIn('npm install --global "npm@${NPM_VERSION}"', run)
        self.assertIn('test "$(npm --version)" = "${NPM_VERSION}"', run)
        self.assertIn("brace-expansion", run)
        self.assertIn("${BRACE_EXPANSION_MIN}", run)
        self.assertIn("rm -rf /usr/local/lib/node_modules/corepack /usr/local/bin/corepack", run)
        self.assertLess(run.index("apk upgrade"), run.index("npm install"))
        self.assertLess(run.index("npm install"), run.index("brace-expansion"))

    def test_the_settings_are_nodes(self):
        self.assertIn(("ENTRYPOINT", '["docker-entrypoint.sh"]'), self.final)
        self.assertIn(("CMD", '["node"]'), self.final)
        envs = " ".join(a for k, a in self.final if k == "ENV")
        for want in ("NODE_VERSION=24.22.3", "YARN_VERSION=1.22.22", "PATH=/usr/local/sbin:/usr/local/bin:"):
            self.assertIn(want, envs)

    def test_the_smoke_checks_node_npm_brace_expansion_and_that_corepack_is_gone(self):
        smoke = read("scripts/smoke-node-npm.sh")
        for want in ("node --version", "npm --version", "brace-expansion", "corepack", "BRACE_EXPANSION_MIN"):
            with self.subTest(want=want):
                self.assertIn(want, smoke)


class PsqlTest(WrapperTest):
    app = "psql"
    extra_keys = ("PGCLIENT_VERSION",)

    def test_the_client_is_pinned_at_the_fixed_alpine_package(self):
        self.assertEqual(self.env["PGCLIENT_VERSION"], PGCLIENT_FIXED)
        self.assertEqual(self.env["UPSTREAM_TAG"], "17.12")

    def test_the_run_installs_only_the_client_at_the_pinned_version(self):
        run = next(a for k, a in self.rootfs if k == "RUN")
        self.assertIn('apk add --no-cache "postgresql17-client=${PGCLIENT_VERSION}"', run)
        self.assertNotRegex(run, r"postgresql17(?!-client)|gosu|su-exec")
        self.assertIn('psql --version', run)
        self.assertLess(run.index("apk upgrade"), run.index("apk add"))

    def test_the_settings_are_alpines_and_there_is_no_user(self):
        self.assertIn(("CMD", '["/bin/sh"]'), self.final)
        self.assertNotIn("ENTRYPOINT", [k for k, _ in self.final])

    def test_the_smoke_runs_the_clients_as_the_jobs_do_and_a_sample_of_their_shell(self):
        smoke = read("scripts/smoke-psql.sh")
        for want in ("psql --version", "pg_isready", "for uid in 70 10001; do", '--user "$uid:$uid"', "--read-only",
                     "--cap-drop ALL", "busybox", "until ", "case ", "$((", "${n:-", "return 1", "$(printf",
                     "tr '\\n' ' '", "head -2"):
            with self.subTest(want=want):
                self.assertIn(want, smoke)

    def test_there_is_no_upstream_release_feed_so_the_definition_carries_a_note(self):
        self.assertNotIn("UPSTREAM_REPO", self.env)
        self.assertIn("gosu", self.env["WATCH_NOTE"])
        self.assertIn("delete this recipe", self.env["WATCH_NOTE"])


class ValkeyTest(WrapperTest):
    app = "valkey"

    def test_the_settings_are_valkeys(self):
        self.assertIn(("ENTRYPOINT", '["tini", "--", "docker-entrypoint.sh"]'), self.final)
        self.assertIn(("CMD", '["valkey-server"]'), self.final)
        self.assertIn(("EXPOSE", "6379"), self.final)
        self.assertIn(("WORKDIR", "/data"), self.final)

    def test_the_smoke_pings_a_throwaway_container_it_removes_by_recorded_id(self):
        smoke = read("scripts/smoke-valkey.sh")
        for want in ("valkey-server --version", "ping", "PONG", "cid=$(docker run -d", 'docker rm -f "$cid"',
                     "trap cleanup EXIT"):
            with self.subTest(want=want):
                self.assertIn(want, smoke)


class RecipesInThisRepoTest(unittest.TestCase):
    def test_every_definition_is_valid_shell_because_build_sh_sources_it(self):
        import glob
        import subprocess
        for path in sorted(glob.glob(os.path.join(ROOT, "images", "*.env"))):
            with self.subTest(path=os.path.basename(path)):
                done = subprocess.run(["bash", "-c", 'set -eu; . "$1"; printf %s "${WATCH_ACTION:-}"', "_", path],
                                      capture_output=True, text=True, check=False)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout, upstream_watch.read_env(path).get("WATCH_ACTION", ""))

    def test_a_quoted_value_reads_the_same_in_bash_and_in_the_watch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('A="one (two) three"\nB=plain\nC="\nD="x\n')
            env = upstream_watch.read_env(path)
        self.assertEqual((env["A"], env["B"], env["C"], env["D"]), ("one (two) three", "plain", '"', '"x'))

    def test_the_check_pins_script_accepts_every_dockerfile(self):
        import check_pins
        for app in PUBLISHED_REVISIONS:
            with self.subTest(app=app):
                self.assertEqual(check_pins.check_dockerfile(read(f"Dockerfile.{app}")), [])

    def test_the_readme_names_the_scan_dates_and_the_retire_rule(self):
        readme = " ".join(read("README.md").split())
        for want in ("`apk upgrade`", "Delete the recipe", "postgresql17-client 17.12-r0", "npm 11.21.3",
                     "brace-expansion", "corepack"):
            with self.subTest(want=want):
                self.assertIn(want, readme)


if __name__ == "__main__":
    unittest.main()
