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
TS = "APP_NAME=tailscale\nUPSTREAM_REPO=https://github.com/tailscale/tailscale\nUPSTREAM_TAG=v1.104.7\nWATCH_ACTION=retire\n"
NODE = ("APP_NAME=node-npm\nUPSTREAM_REPO=https://github.com/nodejs/node\nUPSTREAM_TAG=v24.22.3\n"
        "UPSTREAM_TAG_PREFIX=v24.\nWATCH_ACTION=retire\n")
VK = ("APP_NAME=valkey\nUPSTREAM_REPO=https://github.com/valkey-io/valkey\nUPSTREAM_TAG=9.1.4\n"
      "UPSTREAM_TAG_PREFIX=9.1.\nWATCH_ACTION=retire\n")
PSQL = ("APP_NAME=psql\nUPSTREAM_TAG=17.12\nWATCH_ACTION=retire\n"
        "WATCH_NOTE=retire when postgres:17-alpine passes the scan gate\n")
PATCHED ="UPSTREAM_PATCH_MODULE=github.com/apache/thrift\nUPSTREAM_PATCH_VERSION=v0.25.0\n"
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

    def test_the_grafana_definition_in_this_repository_has_no_server_rebuild_to_drop(self):
        # 13.3.2 ships thrift v0.25.0 itself, so the recipe uses upstream's server and the watch gives no advice.
        env = upstream_watch.read_env(os.path.join(os.path.dirname(__file__), "..", "images", "grafana.env"))
        self.assertNotIn("UPSTREAM_PATCH_MODULE", env)
        self.assertNotIn("UPSTREAM_PATCH_VERSION", env)
        self.assertEqual(env.get("UPSTREAM_FIXED_MODULE"), "github.com/apache/thrift")

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

    def test_an_out_of_band_security_release_is_newer_than_its_base(self):
        self.images(grafana=GF)
        others = {"VictoriaMetrics/victorialogs-datasource": [rel("v0.34.0")],
                  "grafana/grafana-jaeger-datasource": [rel("v13.0.8")]}
        rc, out = self.run_watch({"grafana/grafana": [rel("v13.3.1"), rel("v13.3.1+security-01")], **others})
        self.assertEqual(rc, 1, out)
        self.assertIn("::error title=Upstream released grafana v13.3.1+security-01::grafana: base v13.3.1, upstream "
                      "released v13.3.1+security-01. Re-pin", out)
        for tags, newest in ((["v13.3.1+security-02", "v13.3.1+security-01", "v13.3.1+security-10"], "v13.3.1+security-10"),
                             (["v13.3.2", "v13.3.1+security-04"], "v13.3.2"),
                             (["v13.3.2+security-01", "v13.3.2", "v13.4.0-rc1"], "v13.3.2+security-01"),
                             (["v12.4.15+security-09", "v13.3.1+security-01"], "v13.3.1+security-01")):
            with self.subTest(tags=tags):
                rc, out = self.run_watch({"grafana/grafana": [rel(t) for t in tags], **others})
                self.assertEqual(rc, 1, out)
                self.assertIn(f"::error title=Upstream released grafana {newest}::", out)

    def test_a_security_release_can_be_the_base_and_odd_suffixes_are_not_releases(self):
        self.images(grafana=GF.replace("UPSTREAM_TAG=v13.3.1\n", "UPSTREAM_TAG=v13.3.1+security-01\n"))
        others = {"VictoriaMetrics/victorialogs-datasource": [rel("v0.34.0")],
                  "grafana/grafana-jaeger-datasource": [rel("v13.0.8")]}
        rc, out = self.run_watch({"grafana/grafana": [
            rel("v13.3.1"), rel("v13.3.1+security-01"), rel("v13.3.1+security-"), rel("v13.3.1+build-02"),
            rel("v13.3.1+security-02x"), rel("v13.3.1-security-03"), rel("v13.3.1+security-01+security-02")], **others})
        self.assertEqual(rc, 0, out)
        self.assertIn("grafana: base v13.3.1+security-01, newest upstream release v13.3.1+security-01: no newer", out)
        rc, out = self.run_watch({"grafana/grafana": [rel("v13.3.1+security-02")], **others})
        self.assertEqual(rc, 1, out)

    def test_the_go_mod_of_a_security_release_is_read_at_its_own_tag(self):
        rc, out = self.grafana_release("v13.3.1+security-01", GOMOD)
        self.assertEqual(rc, 1)
        self.assertEqual(self.gomod_calls, [("grafana/grafana", "v13.3.1+security-01")])
        self.assertIn("upstream released v13.3.1+security-01.", out)
        self.assertIn("It still ships github.com/apache/thrift v0.24.0 (ours v0.25.0): KEEP the server rebuild.", out)

    def test_the_single_line_require_form_is_read(self):
        rc, out = self.grafana_release("v13.3.2", "module m\n\nrequire github.com/apache/thrift v0.25.0\n")
        self.assertEqual(rc, 1)
        self.assertIn("It ships github.com/apache/thrift v0.25.0 (ours v0.25.0): DROP", out)
        rc, out = self.grafana_release("v13.3.2", "module m\n\nrequire   github.com/apache/thrift   v0.24.0 // indirect\n")
        self.assertIn("It still ships github.com/apache/thrift v0.24.0 (ours v0.25.0): KEEP", out)

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
        self.assertIn("::error title=Upstream watch failed::grafana: WATCH_ACTION 'ignore' is not archive, rebuild "
                      "or retire\n", out)
        self.assertEqual(self.calls, [])

    def test_a_newer_release_of_a_retire_definition_asks_for_a_scan_of_upstreams_image(self):
        # A retire image exists only until upstream's own image passes the scan gate. The repository and its
        # other rebuilds stay, and whether the release is fixed is the gate's answer, not a go.mod's.
        self.images(tailscale=TS + "UPSTREAM_PATCH_MODULE=golang.org/x/net\nUPSTREAM_PATCH_VERSION=v0.62.0\n",
                    victoria_traces=VT)
        rc, out = self.run_watch({"tailscale/tailscale": [rel("v1.106.0"), rel("v1.105.0-pre"), rel("v1.104.7")],
                                  "VictoriaMetrics/VictoriaTraces": [rel("v0.12.1")]})
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Upstream released tailscale v1.106.0::tailscale: base v1.104.7, upstream "
                      "released v1.106.0. Run scripts/scan.sh on upstream's image of it. If the gate passes, re-pin "
                      "consumers to upstream's image and delete images/tailscale.env with its Dockerfile, patch and "
                      "smoke test; if it fails, re-pin images/tailscale.env and its Dockerfile to it, rebuild and "
                      "bump FC_REVISION.\n", out)
        self.assertNotIn("archive this repository", out)
        self.assertEqual(self.gomod_calls, [])
        self.assertEqual(out.count("::error"), 1, out)

    def test_a_retire_definition_at_the_newest_release_passes(self):
        self.images(tailscale=TS)
        rc, out = self.run_watch({"tailscale/tailscale": [rel("v1.105.0-pre"), rel("v1.104.7"), rel("v1.104.6")]})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "tailscale: base v1.104.7, newest upstream release v1.104.7: no newer release\n")

    def test_the_tailscale_definition_in_this_repository_is_retired_on_a_release(self):
        env = upstream_watch.read_env(os.path.join(os.path.dirname(__file__), "..", "images", "tailscale.env"))
        self.assertEqual(env.get("WATCH_ACTION"), "retire")
        self.assertEqual(upstream_watch.watched(env), ["UPSTREAM"])
        self.assertEqual(env["UPSTREAM_REPO"], "https://github.com/tailscale/tailscale")
        self.assertIsNotNone(upstream_watch.version(env["UPSTREAM_TAG"]))

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

    def test_a_tag_prefix_keeps_the_watch_on_one_release_line(self):
        # nodejs/node publishes 22.x, 24.x and 26.x releases side by side; only the base's line is a newer release.
        self.images(node_npm=NODE)
        rc, out = self.run_watch({"nodejs/node": [rel("v26.13.1"), rel("v24.22.3"), rel("v22.23.5")]})
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, "node-npm: base v24.22.3, newest upstream release v24.22.3: no newer release\n")
        rc, out = self.run_watch({"nodejs/node": [rel("v26.13.1"), rel("v24.22.4"), rel("v24.22.3")]})
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Upstream released node-npm v24.22.4::node-npm: base v24.22.3, upstream "
                      "released v24.22.4. Run scripts/scan.sh on upstream's image of it.", out)
        rc, out = self.run_watch({"nodejs/node": [rel("v26.13.1")]})
        self.assertEqual(rc, 2)
        self.assertIn("::error title=Upstream watch failed::node-npm: no stable release found in nodejs/node "
                      "with the tag prefix v24.\n", out)

    def test_a_bare_tag_prefix_reads_tags_without_the_v(self):
        # valkey-io/valkey tags are 9.1.4, not v9.1.4; a digit prefix says so. 9.1.10 is newer than 9.1.9.
        self.images(valkey=VK)
        rc, out = self.run_watch({"valkey-io/valkey": [rel("9.2.1"), rel("9.1.4"), rel("9.2.0-rc2", prerelease=True)]})
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, "valkey: base 9.1.4, newest upstream release 9.1.4: no newer release\n")
        rc, out = self.run_watch({"valkey-io/valkey": [rel("9.1.10"), rel("9.1.9"), rel("9.1.4")]})
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Upstream released valkey 9.1.10::valkey: base 9.1.4, upstream released 9.1.10.",
                      out)

    def test_a_bare_prefix_ignores_tags_that_are_not_bare_releases(self):
        self.images(valkey=VK)
        rc, out = self.run_watch({"valkey-io/valkey": [rel("9.1.4"), rel("v9.1.9"), rel("9.1.5-rc1"), rel("9.1")]})
        self.assertEqual(rc, 0, out)

    def test_a_definition_with_a_note_and_no_repository_prints_the_note_and_passes(self):
        # psql has no upstream release feed (PostgreSQL and Alpine publish no GitHub releases).
        self.images(psql=PSQL)
        rc, out = self.run_watch({})
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, "psql: no upstream release feed to watch; retire when postgres:17-alpine passes the "
                              "scan gate\n")
        self.assertEqual(self.calls, [])

    def test_a_definition_with_neither_repository_nor_note_is_still_an_error(self):
        self.images(psql=PSQL.replace("WATCH_NOTE=", "NOTE="))
        rc, out = self.run_watch({})
        self.assertEqual(rc, 2)
        self.assertIn("psql: UPSTREAM_REPO is not a github.com repository", out)

    def test_the_wrapper_definitions_in_this_repository_are_watched_or_noted(self):
        here = os.path.join(os.path.dirname(__file__), "..", "images")
        for name, repo in (("node-npm", "https://github.com/nodejs/node"),
                           ("valkey", "https://github.com/valkey-io/valkey")):
            with self.subTest(name=name):
                env = upstream_watch.read_env(os.path.join(here, f"{name}.env"))
                self.assertEqual(env["WATCH_ACTION"], "retire")
                self.assertEqual(env["UPSTREAM_REPO"], repo)
                self.assertTrue(env["UPSTREAM_TAG"].startswith(env["UPSTREAM_TAG_PREFIX"]))
                self.assertIsNotNone(upstream_watch.version(env["UPSTREAM_TAG"], env["UPSTREAM_TAG_PREFIX"]))
        env = upstream_watch.read_env(os.path.join(here, "psql.env"))
        self.assertEqual(env["WATCH_ACTION"], "retire")
        self.assertTrue(env["WATCH_NOTE"])
        self.assertNotIn("UPSTREAM_REPO", env)


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
        upstream_watch.gomod_at("grafana/grafana", "v13.3.1+security-01", opener=opener)
        self.assertEqual(seen["url"], "https://raw.githubusercontent.com/grafana/grafana/v13.3.1%2Bsecurity-01/go.mod")

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
