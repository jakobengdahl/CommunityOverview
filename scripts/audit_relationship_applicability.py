#!/usr/bin/env python3
"""Audit persisted edges against relationship source/target applicability rules."""

import argparse
import os
import sys

from backend.core.storage import GraphStorage


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report edges that violate configured relationship applicability."
    )
    parser.add_argument(
        "--graph",
        default="config/stat-metadata/graph.json",
        help="Path to graph JSON file to audit.",
    )
    parser.add_argument(
        "--schema",
        default="config/stat-metadata/schema_config.json",
        help="Path to schema_config.json containing relationship rules.",
    )
    args = parser.parse_args()

    os.environ["SCHEMA_FILE"] = args.schema
    storage = GraphStorage(args.graph)
    violations = storage.audit_relationship_applicability()

    if not violations:
        print("No relationship applicability violations found.")
        storage.shutdown_events()
        return 0

    print(f"Found {len(violations)} relationship applicability violation(s):")
    for violation in violations:
        print(
            "- {edge_id}: {type} {source}({source_type}) -> "
            "{target}({target_type}) - {message}".format(**violation)
        )
    storage.shutdown_events()
    return 1


if __name__ == "__main__":
    sys.exit(main())
