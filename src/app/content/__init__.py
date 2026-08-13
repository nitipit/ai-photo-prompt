"""Build-time challenge materialization and runtime catalog loading boundaries."""

from .importer import (
    CATALOG_FILENAME,
    CHALLENGE_ASSET_DIRECTORY,
    EXPECTED_CHALLENGE_COUNT,
    EXPECTED_CHALLENGES_PER_LEVEL,
    ChallengeMaterializationError,
    MaterializationResult,
    discover_challenge_bundles,
    materialize_challenges,
    parse_challenge_bundle,
)
from .repository import CatalogValidationError, ChallengeCatalog

__all__ = [
    "CATALOG_FILENAME",
    "CHALLENGE_ASSET_DIRECTORY",
    "CatalogValidationError",
    "ChallengeCatalog",
    "EXPECTED_CHALLENGE_COUNT",
    "EXPECTED_CHALLENGES_PER_LEVEL",
    "ChallengeMaterializationError",
    "MaterializationResult",
    "discover_challenge_bundles",
    "materialize_challenges",
    "parse_challenge_bundle",
]
