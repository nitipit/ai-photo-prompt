"""Command-line entry point for challenge catalog materialization."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.content.importer import (
    ChallengeMaterializationError,
    MaterializationResult,
    discover_challenge_bundles,
    materialize_challenges,
    parse_challenge_bundle,
)


def main() -> int:
    """Materialize ``design/challenges`` for local builds and CI checks."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("design/challenges"),
        help="directory containing challenge bundle directories",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist/challenges"),
        help="directory to atomically replace with catalog and assets",
    )
    args = parser.parse_args()
    result = materialize_challenges(args.source, args.output)
    print(f"materialized {len(result.challenges)} challenges to {result.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ChallengeMaterializationError",
    "MaterializationResult",
    "discover_challenge_bundles",
    "main",
    "materialize_challenges",
    "parse_challenge_bundle",
]
