"""Tests for scripts/upstream_watch.py: does upstream have a release newer than our base tag?"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import upstream_watch  # noqa: E402

VL = "UPSTREAM_REPO=https://github.com/VictoriaMetrics/VictoriaLogs\nUPSTREAM_TAG=v1.53.0\n"
VT = "# comment\nAPP_NAME=victoria-traces\nUPSTREAM_REPO=https://github.com/VictoriaMetrics/VictoriaTraces\nUPSTREAM_TAG=v0.12.1\n"
GF = ("APP_NAME=grafana\nUPSTREAM_REPO=https://github.com/grafana/grafana\nUPSTREAM_TAG=v13.3.1\nWATCH_ACTION=rebuild\n"
      "VLOGS_DS_REPO=https://github.com/VictoriaMetrics/victorialogs-datasource\nVLOGS_DS_TAG=v0.34.0\n"
      "JAEGER_DS_REPO=https://github.com/grafana/grafana-jaeger-datasource\nJAEGER_DS_TAG=v13.0.8\n")
PATCHED = "UPSTREAM_PATCH_MODULE=github.com/apache/thrift\nUPSTREAM_PATCH_VERSION=v0.25.0\n"
GOMOD = ("module github.com/grafana/grafana\n\ngo 1.27.4\n\nrequire (\n"
         "\tgithub.com/apache/thrift-extra v9.9.9\n"
         "\tgithub.com/apache/thrift v0.24.0 // indirect\n"
         "\tgithub.com/apparentlymart/go-textseg/v15 v15.0.0 // indirect\n)\n"
         "replace example.org/x => github.com/apache/thrift v0.1.0\n")


def rel(tag, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

    def images(self, **files):
        for name, body in files.items():
            with open(os.path.join(self.tmp.name, name.replace("_", "-") + ".env"), "w", encoding="utf-8") as fh:
                fh.write(body)

    def run_watch(self, releases, gomods=None):
        def fetch(owner_repo):
            self.calls.append(owner_repo)
            value = releases[owner_repo]
            if isinstance(value, Exception):
                raise value
            return value

        def read_gomod(owner_repo, tag):
            self.gomod_calls.append((owner_repo, tag))
            value = (gomods or {})[tag]
            if isinstance(value, Exception):
                raise value
            return value
        self.gomod_calls = []
        out = io.StringIO()
        with redirect_stdout(out):
            rc = upstream_watch.main(["--images-dir", self.tmp.name], fetch=fetch, read_gomod=read_gomod)
        return rc, out.getvalue()

    def grafana_release(self, tag, gomod):
        self.images(grafana=GF + PATCHED)
        return self.run_watch({"grafana/grafana": [rel(tag), rel("v13.3.1")],
                               "VictoriaMetrics/victorialogs-datasource": [rel("v0.34.0")],
                               "grafana/grafana-jaeger-datasource": [rel("v13.0.8")]}, {tag: gomod})

    def test_a_release_that_ships_the_patched_module_says_drop_the_server_rebuild(self):
        for theirs in ("v0.25.0", "v0.26.1", "v1.0.0"):
            with self.subTest(theirs=theirs):
                rc, out = self.grafana_release("v13.3.2", GOMOD.replace("v0.24.0", theirs))
                self.assertEqual(rc, 1)
                self.assertIn("::error title=Upstream released grafana v13.3.2::grafana: base v13.3.1, upstream "
                              "released v13.3.2. Re-pin images/grafana.env and its Dockerfile to it, rebuild and bump "
                              f"FC_REVISION. It ships github.com/apache/thrift {theirs} (ours v0.25.0): DROP the server "
                              "rebuild and patches/grafana-server.patch.", out)
                self.assertEqual(self.gomod_calls, [("grafana/grafana", "v13.3.2")])

    def test_a_release_that_still_ships_the_old_module_says_keep_the_server_rebuild(self):
        rc, out = self.grafana_release("v13.4.0", GOMOD)
        self.assertEqual(rc, 1)
        self.assertIn("upstream released v13.4.0. Re-pin images/grafana.env and its Dockerfile to it, rebuild and bump "
                      "FC_REVISION. It still ships github.com/apache/thrift v0.24.0 (ours v0.25.0): KEEP the server "
                      "rebuild.", out)
        self.assertNotIn("DROP", out)

    def test_a_release_without_the_patched_module_says_drop_the_server_rebuild(self):
        rc, out = self.grafana_release("v13.3.2", "module github.com/grafana/grafana\n\ngo 1.27.4\n")
        self.assertEqual(rc, 1)
        self.assertIn("It no longer requires github.com/apache/thrift: DROP the server rebuild and "
                      "patches/grafana-server.patch.", out)

    def test_an_unreadable_or_odd_go_mod_is_an_error_not_advice(self):
        for gomod, why in ((OSError("HTTP 404"), "HTTP 404"),
                           (GOMOD.replace("v0.24.0", "v0.25.1-0.20261101000000-abcdef123456"),
                            "github.com/apache/thrift v0.25.1-0.20261101000000-abcdef123456 is not vX.Y.Z")):
            with self.subTest(why=why):
                rc, out = self.grafana_release("v13.3.2", gomod)
                self.assertEqual(rc, 2)
                self.assertIn("::error title=Upstream watch failed::grafana: upstream released v13.3.2, but its go.mod "
                              f"cannot be checked for the server rebuild: {why}", out)
                self.assertNotIn("DROP", out)
                self.assertNotIn("KEEP", out)

    def test_a_bad_patch_version_in_the_definition_is_an_error(self):
        self.images(grafana=GF + PATCHED.replace("UPSTREAM_PATCH_VERSION=v0.25.0", "UPSTREAM_PATCH_VERSION=0.25"))
        rc, out = self.run_watch({"grafana/grafana": [rel("v13.3.2")],
                                  "VictoriaMetrics/victorialogs-datasource": [rel("v0.34.0")],
                                  "grafana/grafana-jaeger-datasource": [rel("v13.0.8")]}, {"v13.3.2": GOMOD})
        self.assertEqual(rc, 2)
        self.assertIn("cannot be checked for the server rebuild: UPSTREAM_PATCH_VERSION '0.25' is not vX.Y.Z", out)
        self.assertEqual(self.gomod_calls, [])

    def test_no_newer_release_reads_no_go_mod(self):
        self.images(grafana=GF + PATCHED)
        rc, out = self.run_watch({"grafana/grafana": [rel("v13.3.1")],
                                  "VictoriaMetrics/victorialogs-datasource": [rel("v0.35.0")],
                                  "grafana/grafana-jaeger-datasource": [rel("v13.0.8")]})
        self.assertEqual(rc, 1)
        self.assertEqual(self.gomod_calls, [])
        self.assertNotIn("server rebuild", out)

    def test_the_grafana_definition_in_this_repository_names_the_patched_module(self):
        env = upstream_watch.read_env(os.path.join(os.path.dirname(__file__), "..", "images", "grafana.env"))
        self.assertEqual(env.get("UPSTREAM_PATCH_MODULE"), "github.com/apache/thrift")
        self.assertGreaterEqual(upstream_watch.version(env.get("UPSTREAM_PATCH_VERSION")), (0, 25, 0))

    def test_no_newer_release_passes(self):
        self.images(victoria_logs=VL, victoria_traces=VT)
        rc, out = self.run_watch({"VictoriaMetrics/VictoriaLogs": [rel("v1.53.0"), rel("v1.52.2")],
                                  "VictoriaMetrics/VictoriaTraces": [rel("v0.12.1")]})
        self.assertEqual(rc, 0, out)
        self.assertIn("victoria-logs: base v1.53.0, newest upstream release v1.53.0: no newer release", out)
        self.assertIn("victoria-traces: base v0.12.1, newest upstream release v0.12.1: no newer release", out)
        self.assertEqual(sorted(self.calls), ["VictoriaMetrics/VictoriaLogs", "VictoriaMetrics/VictoriaTraces"])

    def test_newer_release_fails_loudly_with_the_next_step(self):
        self.images(victoria_logs=VL, victoria_traces=VT)
        rc, out = self.run_watch({"VictoriaMetrics/VictoriaLogs": [rel("v1.53.0"), rel("v1.54.0")],
                                  "VictoriaMetrics/VictoriaTraces": [rel("v0.12.1")]})
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Upstream released victoria-logs v1.54.0::", out)
        self.assertIn("re-pin to upstream's image", out)
        self.assertIn("archive this repository", out)
        self.assertIn("victoria-traces: base v0.12.1, newest upstream release v0.12.1: no newer release", out)

    def test_patch_and_minor_bumps_count_numerically(self):
        self.images(victoria_traces=VT)
        rc, out = self.run_watch({"VictoriaMetrics/VictoriaTraces": [rel("v0.12.10"), rel("v0.9.0")]})
        self.assertEqual(rc, 1)
        self.assertIn("v0.12.10", out)

    def test_drafts_prereleases_and_odd_tags_are_ignored(self):
        self.images(victoria_logs=VL)
        rc, out = self.run_watch({"VictoriaMetrics/VictoriaLogs": [
            rel("v1.53.0"), rel("v1.60.0", draft=True), rel("v1.59.0", prerelease=True),
            rel("v1.58.0-rc1"), rel("v1.57.0-enterprise"), rel("latest"), {"tag_name": None}]})
        self.assertEqual(rc, 0, out)

    def test_older_latest_is_not_newer(self):
        self.images(victoria_logs=VL)
        rc, out = self.run_watch({"VictoriaMetrics/VictoriaLogs": [rel("v1.52.9")]})
        self.assertEqual(rc, 0, out)
        self.assertIn("newest upstream release v1.52.9: no newer release", out)

    def test_no_stable_release_found_is_an_error(self):
        self.images(victoria_logs=VL)
        rc, out = self.run_watch({"VictoriaMetrics/VictoriaLogs": [rel("v2.0.0", prerelease=True)]})
        self.assertEqual(rc, 2)
        self.assertIn("::error title=Upstream watch failed::victoria-logs: no stable release found", out)

    def test_fetch_error_is_an_error_not_a_pass(self):
        self.images(victoria_logs=VL, victoria_traces=VT)
        rc, out = self.run_watch({"VictoriaMetrics/VictoriaLogs": OSError("HTTP 403 rate limited"),
                                  "VictoriaMetrics/VictoriaTraces": [rel("v0.13.0")]})
        self.assertEqual(rc, 2)
        self.assertIn("victoria-logs: HTTP 403 rate limited", out)
        self.assertIn("::error title=Upstream released victoria-traces v0.13.0::", out)

    def test_bad_definition_is_an_error(self):
        self.images(victoria_logs="UPSTREAM_REPO=https://gitlab.com/x/y\nUPSTREAM_TAG=v1.0.0\n",
                    victoria_traces="UPSTREAM_TAG=1.2\n")
        rc, out = self.run_watch({})
        self.assertEqual(rc, 2)
        self.assertIn("victoria-logs: UPSTREAM_REPO is not a github.com repository", out)
        self.assertIn("victoria-traces: UPSTREAM_REPO is not a github.com repository", out)

    def test_unparseable_base_tag_is_an_error(self):
        self.images(victoria_logs="UPSTREAM_REPO=https://github.com/VictoriaMetrics/VictoriaLogs\nUPSTREAM_TAG=main\n")
        rc, out = self.run_watch({"VictoriaMetrics/VictoriaLogs": [rel("v1.53.0")]})
        self.assertEqual(rc, 2)
        self.assertIn("victoria-logs: UPSTREAM_TAG 'main' is not vX.Y.Z", out)

    def test_every_repo_and_tag_pair_of_a_definition_is_watched(self):
        self.images(grafana=GF)
        rc, out = self.run_watch({"grafana/grafana": [rel("v13.3.1"), rel("v13.2.6"), rel("v12.4.15")],
                                  "VictoriaMetrics/victorialogs-datasource": [rel("v0.34.0")],
                                  "grafana/grafana-jaeger-datasource": [rel("v13.0.8")]})
        self.assertEqual(rc, 0, out)
        self.assertIn("grafana: base v13.3.1, newest upstream release v13.3.1: no newer release", out)
        self.assertIn("grafana VLOGS_DS: base v0.34.0, newest upstream release v0.34.0: no newer release", out)
        self.assertIn("grafana JAEGER_DS: base v13.0.8, newest upstream release v13.0.8: no newer release", out)
        self.assertEqual(sorted(self.calls), ["VictoriaMetrics/victorialogs-datasource", "grafana/grafana",
                                              "grafana/grafana-jaeger-datasource"])

    def test_a_newer_baked_in_component_fails_with_the_rebuild_step(self):
        self.images(grafana=GF)
        rc, out = self.run_watch({"grafana/grafana": [rel("v13.3.1")],
                                  "VictoriaMetrics/victorialogs-datasource": [rel("v0.34.0")],
                                  "grafana/grafana-jaeger-datasource": [rel("v13.0.9"), rel("v13.0.8")]})
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Upstream released grafana JAEGER_DS v13.0.9::grafana JAEGER_DS: base v13.0.8, "
                      "upstream released v13.0.9. Re-pin images/grafana.env and its Dockerfile to it, rebuild and "
                      "bump FC_REVISION.", out)
        self.assertNotIn("archive this repository", out)
        self.assertIn("grafana: base v13.3.1, newest upstream release v13.3.1: no newer release", out)

    def test_a_newer_base_release_of_a_rebuild_definition_asks_for_a_rebuild(self):
        self.images(grafana=GF, victoria_logs=VL)
        rc, out = self.run_watch({"grafana/grafana": [rel("v13.3.2"), rel("v14.0.0", prerelease=True)],
                                  "VictoriaMetrics/victorialogs-datasource": [rel("v0.34.0")],
                                  "grafana/grafana-jaeger-datasource": [rel("v13.0.8")],
                                  "VictoriaMetrics/VictoriaLogs": [rel("v1.54.0")]})
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Upstream released grafana v13.3.2::grafana: base v13.3.1, upstream released "
                      "v13.3.2. Re-pin images/grafana.env and its Dockerfile to it, rebuild and bump FC_REVISION.", out)
        self.assertIn("victoria-logs: base v1.53.0, upstream released v1.54.0. Check its image's Go version, "
                      "re-pin to upstream's image, and archive this repository.", out)

    def test_a_repo_without_its_tag_is_an_error(self):
        self.images(grafana="UPSTREAM_REPO=https://github.com/grafana/grafana\nUPSTREAM_TAG=v13.3.1\n"
                            "VLOGS_DS_REPO=https://github.com/VictoriaMetrics/victorialogs-datasource\n"
                            "JAEGER_DS_REPO=git@github.com:grafana/grafana-jaeger-datasource\nJAEGER_DS_TAG=v13.0.8\n")
        rc, out = self.run_watch({"grafana/grafana": [rel("v13.3.1")]})
        self.assertEqual(rc, 2)
        self.assertIn("::error title=Upstream watch failed::grafana VLOGS_DS: VLOGS_DS_TAG '' is not vX.Y.Z", out)
        self.assertIn("grafana JAEGER_DS: JAEGER_DS_REPO is not a github.com repository", out)
        self.assertIn("grafana: base v13.3.1, newest upstream release v13.3.1: no newer release", out)

    def test_an_unknown_watch_action_is_an_error(self):
        self.images(grafana=GF.replace("WATCH_ACTION=rebuild", "WATCH_ACTION=ignore"))
        rc, out = self.run_watch({})
        self.assertEqual(rc, 2)
        self.assertIn("::error title=Upstream watch failed::grafana: WATCH_ACTION 'ignore' is not archive or rebuild", out)
        self.assertEqual(self.calls, [])

    def test_the_grafana_definition_in_this_repository_watches_all_three_upstreams(self):
        env = upstream_watch.read_env(os.path.join(os.path.dirname(__file__), "..", "images", "grafana.env"))
        self.assertEqual(env.get("WATCH_ACTION"), "rebuild")
        self.assertEqual([(key, env[f"{key}_REPO"]) for key in upstream_watch.watched(env)],
                         [("UPSTREAM", "https://github.com/grafana/grafana"),
                          ("JAEGER_DS", "https://github.com/grafana/grafana-jaeger-datasource"),
                          ("VLOGS_DS", "https://github.com/VictoriaMetrics/victorialogs-datasource")])
        for key in upstream_watch.watched(env):
            self.assertIsNotNone(upstream_watch.version(env[f"{key}_TAG"]), key)

    def test_no_image_definitions_is_an_error(self):
        rc, out = self.run_watch({})
        self.assertEqual(rc, 2)
        self.assertIn("no image definitions", out)


class FetchTest(unittest.TestCase):
    def test_github_fetch_builds_the_request(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(req, timeout):
            seen["url"], seen["auth"], seen["timeout"] = req.full_url, req.get_header("Authorization"), timeout
            return Resp(b'[{"tag_name": "v1.53.0"}]')

        os.environ["GITHUB_TOKEN"] = "t0ken"  # noqa: S105 (a fake token for the header test)
        self.addCleanup(os.environ.pop, "GITHUB_TOKEN", None)
        got = upstream_watch.github_releases("VictoriaMetrics/VictoriaLogs", opener=opener)
        self.assertEqual(got, [{"tag_name": "v1.53.0"}])
        self.assertEqual(seen["url"], "https://api.github.com/repos/VictoriaMetrics/VictoriaLogs/releases?per_page=50")
        self.assertEqual(seen["auth"], "Bearer t0ken")
        self.assertEqual(seen["timeout"], 30)

    def test_go_mod_fetch_reads_the_file_at_the_tag(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(req, timeout):
            seen["url"], seen["auth"], seen["timeout"] = req.full_url, req.get_header("Authorization"), timeout
            return Resp(b"module github.com/grafana/grafana\n")

        os.environ["GITHUB_TOKEN"] = "t0ken"  # noqa: S105 (a fake token: it must NOT be sent to the raw host)
        self.addCleanup(os.environ.pop, "GITHUB_TOKEN", None)
        got = upstream_watch.gomod_at("grafana/grafana", "v13.3.2", opener=opener)
        self.assertEqual(got, "module github.com/grafana/grafana\n")
        self.assertEqual(seen["url"], "https://raw.githubusercontent.com/grafana/grafana/v13.3.2/go.mod")
        self.assertIsNone(seen["auth"])
        self.assertEqual(seen["timeout"], 30)

    def test_github_fetch_without_token_sends_no_auth(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(req, timeout):
            seen["auth"] = req.get_header("Authorization")
            return Resp(b"[]")

        os.environ.pop("GITHUB_TOKEN", None)
        self.assertEqual(upstream_watch.github_releases("a/b", opener=opener), [])
        self.assertIsNone(seen["auth"])


if __name__ == "__main__":
    unittest.main()
