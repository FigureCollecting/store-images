"""Tests for scripts/gate.py: the trivy + grype decision on one image."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import gate  # noqa: E402


def trivy_report(vulns=(), packages=3, stdlib="v1.27.3"):
    pkgs = [{"Name": "stdlib", "Version": stdlib}] if stdlib else []
    pkgs += [{"Name": f"mod{i}", "Version": "v1.0.0"} for i in range(packages - len(pkgs))]
    return {"Results": [{"Target": "victoria-logs-prod", "Packages": pkgs, "Vulnerabilities": list(vulns)}]}


def tv(cve, sev="HIGH", pkg="stdlib", installed="v1.27.1", fixed="1.26.10, 1.27.3"):
    return {"VulnerabilityID": cve, "Severity": sev, "PkgName": pkg,
            "InstalledVersion": installed, "FixedVersion": fixed}


def grype_report(matches=()):
    return {"matches": list(matches)}


def gm(cve, sev="High", state="fixed", name="stdlib", version="go1.27.1", fixes=("1.27.3",)):
    return {"vulnerability": {"id": cve, "severity": sev, "fix": {"state": state, "versions": list(fixes)}},
            "artifact": {"name": name, "version": version}}


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def run_gate(self, trivy, grype):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = gate.main(["--label", "img", "--trivy", self.write("t.json", trivy),
                            "--grype", self.write("g.json", grype)])
        return rc, out.getvalue()

    def test_clean_image_passes_and_reports_inventory(self):
        rc, out = self.run_gate(trivy_report(packages=7), grype_report())
        self.assertEqual(rc, 0, out)
        self.assertIn("img trivy: PASS 0 fixable HIGH/CRITICAL (7 packages inventoried, stdlib v1.27.3)", out)
        self.assertIn("img grype: PASS 0 fixable HIGH/CRITICAL", out)
        self.assertIn("img GATE PASS", out)

    def test_trivy_fixable_high_fails_and_names_the_cve(self):
        rc, out = self.run_gate(trivy_report([tv("CVE-2026-111943")]), grype_report())
        self.assertEqual(rc, 1)
        self.assertIn("img trivy: FAIL 1 fixable HIGH/CRITICAL: "
                      "CVE-2026-111943(HIGH stdlib v1.27.1 -> 1.26.10, 1.27.3)", out)
        self.assertIn("img GATE FAIL", out)

    def test_critical_counts(self):
        rc, out = self.run_gate(trivy_report([tv("CVE-1", sev="CRITICAL")]), grype_report())
        self.assertEqual(rc, 1)
        self.assertIn("CVE-1(CRITICAL", out)

    def test_trivy_ignores_unfixed_and_lower_severities(self):
        vulns = [tv("CVE-UNFIXED", fixed=""), tv("CVE-MED", sev="MEDIUM"), tv("CVE-LOW", sev="LOW")]
        rc, out = self.run_gate(trivy_report(vulns), grype_report())
        self.assertEqual(rc, 0, out)

    def test_grype_fixable_high_fails_even_when_trivy_passes(self):
        rc, out = self.run_gate(trivy_report(), grype_report([gm("CVE-2026-97031")]))
        self.assertEqual(rc, 1)
        self.assertIn("img trivy: PASS", out)
        self.assertIn("img grype: FAIL 1 fixable HIGH/CRITICAL: CVE-2026-97031(High stdlib go1.27.1 -> 1.27.3)", out)

    def test_grype_ignores_unfixed_wontfix_and_lower_severities(self):
        matches = [gm("CVE-NF", state="not-fixed", fixes=()), gm("CVE-WF", state="wont-fix", fixes=()),
                   gm("CVE-MED", sev="Medium"), gm("CVE-UNK", state="unknown", fixes=())]
        rc, out = self.run_gate(trivy_report(), grype_report(matches))
        self.assertEqual(rc, 0, out)

    def test_same_cve_in_two_packages_counts_once(self):
        vulns = [tv("CVE-2026-78669", pkg="stdlib"), tv("CVE-2026-78669", pkg="golang.org/x/net")]
        rc, out = self.run_gate(trivy_report(vulns), grype_report())
        self.assertEqual(rc, 1)
        self.assertIn("FAIL 1 fixable HIGH/CRITICAL", out)
        self.assertIn("CVE-2026-78669(HIGH golang.org/x/net", out)
        self.assertIn("CVE-2026-78669(HIGH stdlib", out)

    def test_blind_scan_is_an_error_not_a_pass(self):
        rc, out = self.run_gate({"Results": [{"Target": "x", "Packages": []}]}, grype_report())
        self.assertEqual(rc, 2)
        self.assertIn("img trivy: BLIND 0 packages inventoried", out)
        self.assertIn("img GATE ERROR", out)

    def test_report_without_results_is_blind(self):
        rc, out = self.run_gate({"SchemaVersion": 2}, grype_report())
        self.assertEqual(rc, 2)
        self.assertIn("BLIND", out)

    def test_null_vulnerability_lists_are_tolerated(self):
        report = {"Results": [{"Target": "x", "Packages": [{"Name": "a", "Version": "1"}], "Vulnerabilities": None}]}
        rc, out = self.run_gate(report, {"matches": None})
        self.assertEqual(rc, 0, out)
        self.assertIn("(1 packages inventoried, stdlib none)", out)

    def test_malformed_json_is_an_error(self):
        rc, out = self.run_gate("{not json", grype_report())
        self.assertEqual(rc, 2)
        self.assertIn("img GATE ERROR: cannot read", out)

    def test_wrong_shape_is_an_error(self):
        rc, out = self.run_gate(trivy_report(), ["not", "an", "object"])
        self.assertEqual(rc, 2)
        self.assertIn("img GATE ERROR: cannot read", out)

    def test_missing_file_is_an_error(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = gate.main(["--label", "img", "--trivy", os.path.join(self.tmp.name, "absent.json"),
                            "--grype", self.write("g.json", grype_report())])
        self.assertEqual(rc, 2)
        self.assertIn("img GATE ERROR: cannot read", out.getvalue())


if __name__ == "__main__":
    unittest.main()
