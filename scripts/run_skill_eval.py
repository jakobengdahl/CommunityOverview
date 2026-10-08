#!/usr/bin/env python3
"""
Run the skill-execution evaluation suite against one or more provider configurations.

    python scripts/run_skill_eval.py --profiles my-providers.json
    python scripts/run_skill_eval.py --profiles my-providers.json --profile-id openai-gpt4o
    python scripts/run_skill_eval.py --dimensions

Each provider configuration names the environment variable holding its
credential (``credential_ref``); this script reads that variable and nothing
else. It accepts no API key as an argument and prints none: a key on a command
line lands in shell history and in the process table, which is why there is no
flag for one.

See docs/SKILL_EVALUATION.md for the profile file format and the variables to
export.
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.evaluation import (  # noqa: E402
    DIMENSIONS,
    build_report,
    load_cases,
    load_profiles,
    run_suite,
    unscored_dimensions,
)
from backend.evaluation.dimensions import Mechanical  # noqa: E402


def print_dimensions() -> None:
    """Print what the harness scores mechanically and what it does not."""
    for key, dim in DIMENSIONS.items():
        print(f"\n{key}  [{dim.mechanical.value}]")
        print(f"  {dim.title}")
        if dim.measured_by:
            print(f"  scored from: {', '.join(dim.measured_by)}")
        elif dim.mechanical is Mechanical.REPORTED:
            print("  scored from: measured per run, no pass condition")
        else:
            print("  scored from: NOTHING — deliberately not scored, see below")
        for line in _wrap(dim.caveat, 72):
            print(f"    {line}")


def _wrap(text: str, width: int):
    words, line = text.split(), ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            yield line
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        yield line


def check_credentials(profiles) -> list:
    """Report which profiles cannot run because their credential is unset."""
    missing = []
    for profile in profiles:
        if not profile.credential_ref:
            missing.append((profile.id, "(no credential_ref configured)"))
        elif not os.environ.get(profile.credential_ref):
            missing.append((profile.id, profile.credential_ref))
    return missing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the skill-execution evaluation suite.",
        epilog="Credentials are read from the environment variables the profiles "
        "name. This script takes no API key argument.",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        help="JSON file of model profiles (see docs/SKILL_EVALUATION.md).",
    )
    parser.add_argument(
        "--profile-id",
        action="append",
        default=[],
        help="Only run this profile id; repeatable. Default: every profile in the file.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Write the JSON report here instead of to stdout.",
    )
    parser.add_argument(
        "--dimensions",
        action="store_true",
        help="Print the measured dimensions and how each is scored, then exit.",
    )
    args = parser.parse_args(argv)

    if args.dimensions:
        print_dimensions()
        return 0
    if not args.profiles:
        parser.error("--profiles is required (or use --dimensions)")

    profiles = load_profiles(args.profiles)
    if args.profile_id:
        wanted = set(args.profile_id)
        unknown = wanted - {p.id for p in profiles}
        if unknown:
            parser.error(f"no such profile id(s) in {args.profiles}: {sorted(unknown)}")
        profiles = [p for p in profiles if p.id in wanted]

    missing = check_credentials(profiles)
    if missing:
        for profile_id, ref in missing:
            print(
                f"profile {profile_id!r}: environment variable {ref} is not set",
                file=sys.stderr,
            )
        print(
            "\nExport the variables these profiles name and re-run. See "
            "docs/SKILL_EVALUATION.md.",
            file=sys.stderr,
        )
        return 2

    cases = load_cases()
    reports = []
    for profile in profiles:
        print(
            f"running {len(cases)} case(s) against {profile.id} "
            f"({profile.provider}/{profile.model})",
            file=sys.stderr,
        )
        result = run_suite(profile, cases=cases)
        errors = (
            f", {result.run_errors} case(s) never reached the model"
            if result.run_errors
            else ""
        )
        print(
            f"  {result.passed}/{result.total} passed{errors}, "
            f"{result.total_latency_ms / 1000:.1f}s of provider time",
            file=sys.stderr,
        )
        reports.append(build_report(result))

    unscored = unscored_dimensions()
    document = {"reports": reports, "unscored_dimensions": unscored}
    payload = json.dumps(document, indent=2)
    if args.out:
        args.out.write_text(payload + "\n", encoding="utf-8")
        print(f"report written to {args.out}", file=sys.stderr)
    else:
        print(payload)

    if unscored:
        print(
            f"\nNote: {', '.join(unscored)} is not scored by this harness. Run "
            "`--dimensions` to see why.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
