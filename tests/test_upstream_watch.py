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

    def run_watch(self, releases):
        def fetch(owner_repo):
            self.calls.append(owner_repo)
            value = releases[owner_repo]
            if isinstance(value, Exception):
                raise value
            return value
        out = io.StringIO()
        with redirect_stdout(out):
            rc = upstream_watch.main(["--images-dir", self.tmp.name], fetch=fetch)
        return rc, out.getvalue()

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
