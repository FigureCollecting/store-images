#!/usr/bin/env python3
"""Decide the scan gate for one image from a trivy report and a grype report (both JSON).

FAIL (exit 1) if EITHER scanner reports a HIGH or CRITICAL vulnerability that has a fix.
ERROR (exit 2) if a report cannot be read, or trivy inventoried zero packages: a scan that sees
nothing proves nothing. PASS (exit 0) otherwise. There is no ignore list, by design.
"""
import argparse
import json
import sys

SEVERE = {"HIGH", "CRITICAL"}


def trivy_findings(report):
    """Return (findings, package_count, stdlib_version) from a trivy JSON report."""
    findings, packages, stdlib = [], 0, None
    for result in report.get("Results") or []:
        for pkg in result.get("Packages") or []:
            packages += 1
            if pkg.get("Name") == "stdlib":
                stdlib = pkg.get("Version")
        for v in result.get("Vulnerabilities") or []:
            if v.get("Severity") in SEVERE and v.get("FixedVersion"):
                findings.append((v["VulnerabilityID"], v["Severity"], v.get("PkgName"),
                                 v.get("InstalledVersion"), v["FixedVersion"]))
    return findings, packages, stdlib


def grype_findings(report):
    """Return findings from a grype JSON report: HIGH/CRITICAL matches whose fix state is 'fixed'."""
    findings = []
    for m in report.get("matches") or []:
        vuln, art = m["vulnerability"], m["artifact"]
        fix = vuln.get("fix") or {}
        if vuln.get("severity", "").upper() in SEVERE and fix.get("state") == "fixed":
            findings.append((vuln["id"], vuln["severity"], art.get("name"), art.get("version"),
                             ", ".join(fix.get("versions") or [])))
    return findings


def describe(findings):
    ids = sorted({f[0] for f in findings})
    detail = " ".join(f"{i}({s} {p} {have} -> {fixed})" for i, s, p, have, fixed in sorted(set(findings)))
    return len(ids), detail


def line(label, tool, findings, extra=""):
    count, detail = describe(findings)
    verdict = "PASS" if count == 0 else "FAIL"
    text = f"{label} {tool}: {verdict} {count} fixable HIGH/CRITICAL"
    text += f": {detail}" if count else ""
    print(text + extra)
    return count


def load(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True)
    ap.add_argument("--trivy", required=True)
    ap.add_argument("--grype", required=True)
    args = ap.parse_args(argv)
    try:
        t_findings, packages, stdlib = trivy_findings(load(args.trivy))
        g_findings = grype_findings(load(args.grype))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"{args.label} GATE ERROR: cannot read the scan reports: {exc}")
        return 2
    if packages == 0:
        print(f"{args.label} trivy: BLIND 0 packages inventoried (the scan proves nothing)")
        print(f"{args.label} GATE ERROR")
        return 2
    failed = line(args.label, "trivy", t_findings,
                  f" ({packages} packages inventoried, stdlib {stdlib or 'none'})")
    failed += line(args.label, "grype", g_findings)
    print(f"{args.label} GATE {'FAIL' if failed else 'PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
