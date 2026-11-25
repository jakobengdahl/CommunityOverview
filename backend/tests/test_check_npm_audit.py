# covers: scripts/check_npm_audit.py
from datetime import date

import pytest

from scripts.check_npm_audit import check_audit


@pytest.mark.parametrize(
    ("baseline", "report_package", "fails"),
    [
        ([], "example", True),
        ([{"package": "example", "reason": "Pending fix", "expires": "2026-12-01"}], "example", True),
        ([{"advisory": 123, "reason": "Pending fix", "expires": "2026-12-01"}], "example", True),
        ([{"advisory": 123, "package": "example", "expires": "2026-12-01"}], "example", True),
        ([{"advisory": 123, "package": "example", "reason": "Pending fix"}], "example", True),
        (
            [
                {
                    "advisory": 123,
                    "package": "example",
                    "reason": "Pending fix",
                    "expires": "2026-12-01",
                }
            ],
            "example",
            False,
        ),
        (
            [
                {
                    "advisory": 123,
                    "package": "example",
                    "reason": "Pending fix",
                    "expires": "2026-11-24",
                }
            ],
            "example",
            True,
        ),
        (
            [
                {
                    "advisory": 123,
                    "package": "example",
                    "reason": "",
                    "expires": "2026-12-01",
                }
            ],
            "example",
            True,
        ),
        (
            [
                {
                    "advisory": 123,
                    "package": "other",
                    "reason": "Pending fix",
                    "expires": "2026-12-01",
                }
            ],
            "example",
            True,
        ),
    ],
)
def test_advisories_require_valid_unexpired_matching_entries(
    baseline, report_package, fails
):
    report = {
        "auditReportVersion": 2,
        "vulnerabilities": {
            report_package: {
                "name": report_package,
                "via": [{"source": 123, "name": report_package}],
            }
        },
    }
    problems = check_audit(report, baseline, date(2026, 11, 25))
    assert bool(problems) is fails
    if baseline and set(baseline[0]) != {"advisory", "package", "reason", "expires"}:
        assert any(problem.startswith("Invalid baseline entry:") for problem in problems)


def test_invalid_report_and_unused_expired_entry_fail():
    baseline = [
        {
            "advisory": 123,
            "package": "example",
            "reason": "Pending fix",
            "expires": "2026-11-24",
        }
    ]
    assert check_audit({}, [], date(2026, 11, 25))
    assert check_audit(
        {"auditReportVersion": 2, "vulnerabilities": {}}, baseline, date(2026, 11, 25)
    )
