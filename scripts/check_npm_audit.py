#!/usr/bin/env python3
"""Check npm's full audit report against expiring advisory exceptions."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path


def check_audit(report, baseline, today):
    """Return problems found in the report or its baseline."""
    problems = []
    allowed = set()
    if not isinstance(baseline, list):
        return ["Baseline must be a list"]
    for entry in baseline:
        if not isinstance(entry, dict):
            problems.append("Invalid baseline entry")
            continue
        advisory = entry.get("advisory")
        package = entry.get("package")
        reason = entry.get("reason")
        expiry = entry.get("expires")
        if (
            type(advisory) is not int
            or advisory <= 0
            or not isinstance(package, str)
            or not package.strip()
            or not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(expiry, str)
        ):
            problems.append(f"Invalid baseline entry: {entry!r}")
            continue
        try:
            expires = date.fromisoformat(expiry)
            if expires.isoformat() != expiry:
                raise ValueError
        except ValueError:
            problems.append(f"Invalid expiry for {package}: {expiry!r}")
            continue
        key = (advisory, package)
        if key in allowed:
            problems.append(f"Duplicate baseline entry: {key}")
        allowed.add(key)
        if expires < today:
            problems.append(f"Expired baseline entry: {key} ({expiry})")

    if (
        not isinstance(report, dict)
        or report.get("auditReportVersion") != 2
        or not isinstance(report.get("vulnerabilities"), dict)
        or "error" in report
    ):
        return problems + ["Invalid npm audit report"]

    advisories = set()
    vulnerabilities = report["vulnerabilities"]
    for name, vulnerability in vulnerabilities.items():
        if (
            not isinstance(vulnerability, dict)
            or vulnerability.get("name") != name
            or not isinstance(vulnerability.get("via"), list)
            or not vulnerability["via"]
        ):
            problems.append(f"Invalid vulnerability: {name}")
            continue
        for source in vulnerability["via"]:
            if isinstance(source, str) and source:
                continue  # Transitive impact; its advisory appears on the source package.
            if (
                not isinstance(source, dict)
                or type(source.get("source")) is not int
                or source["source"] <= 0
                or not isinstance(source.get("name"), str)
                or not source["name"].strip()
            ):
                problems.append(f"Invalid advisory for {name}")
                continue
            advisories.add((source["source"], source["name"]))
    if vulnerabilities and not advisories:
        problems.append("Audit report contains no advisory details")
    for key in sorted(advisories - allowed):
        problems.append(f"Unlisted advisory: {key}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text())
        baseline = json.loads(args.baseline.read_text())
        problems = check_audit(report, baseline, date.today())
    except (OSError, ValueError) as exc:
        problems = [str(exc)]
    for problem in problems:
        print(problem, file=sys.stderr)
    return int(bool(problems))


if __name__ == "__main__":
    sys.exit(main())
