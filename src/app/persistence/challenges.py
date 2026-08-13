"""ShelfDB-backed storage and atomic synchronization for challenge records."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.content.repository import CatalogValidationError, ChallengeCatalog
from app.domain.models import ChallengeSpec, LevelGroup

from .rounds import _SHELFDB_WRITE_LOCK

CHALLENGES_SHELF = "challenges"


class ChallengeRepositoryError(ValueError):
    """Raised when a persisted challenge record is corrupted or inconsistent."""


class ChallengeNotFoundError(LookupError):
    """Raised when a challenge ID is not present in the durable catalog."""


class ShelfDbChallengeRepository:
    """Store complete validated ``ChallengeSpec`` primitive mappings by ID."""

    def __init__(self, db: DB) -> None:
        self._db = db
        self._write_lock = _SHELFDB_WRITE_LOCK
        # Create the named shelf while holding the shared writer lock so this
        # setup cannot overlap with another top-level write transaction.
        with self._write_lock, self._db.transaction(write=True) as transaction:
            transaction.shelf(CHALLENGES_SHELF)

    def sync(self, catalog: Sequence[ChallengeSpec]) -> None:
        """Atomically replace the durable catalog with one complete catalog.

        The full incoming catalog is validated before opening a write
        transaction.  ShelfDB aborts the transaction if any write fails, so a
        failed synchronization leaves the previous shelf contents unchanged.
        """

        validated = _validate_incoming_catalog(catalog)
        payloads = {challenge.id: challenge.dict() for challenge in validated}
        current_ids = set(payloads)
        with self._write_lock, self._db.transaction(write=True) as transaction:
            shelf = transaction.shelf(CHALLENGES_SHELF)
            stale_ids = {item.key for item in shelf.keys()} - current_ids
            for challenge_id in stale_ids:
                shelf.key(challenge_id).delete()
            for challenge_id, payload in payloads.items():
                shelf.put(challenge_id, payload)

    def get(self, challenge_id: str) -> ChallengeSpec:
        """Return one freshly validated challenge or a bounded lookup error."""

        with self._db.transaction(write=False) as transaction:
            item = next(iter(transaction.shelf(CHALLENGES_SHELF).key(challenge_id).items()), None)
        if item is None:
            raise ChallengeNotFoundError(f"challenge not found: {challenge_id}")
        return _reconstruct(item.value, challenge_id)

    def for_level(self, level: LevelGroup | str) -> tuple[ChallengeSpec, ...]:
        """Return the validated durable challenges for one level."""

        try:
            selected_level = level if isinstance(level, LevelGroup) else LevelGroup(level)
        except (TypeError, ValueError) as error:
            raise ChallengeRepositoryError(f"invalid challenge level: {level!r}") from error
        return tuple(challenge for challenge in self.all() if challenge.level is selected_level)

    def all(self) -> tuple[ChallengeSpec, ...]:
        """Return the complete validated durable catalog in ID order."""

        with self._db.transaction(write=False) as transaction:
            payloads = [item.value for item in transaction.shelf(CHALLENGES_SHELF).items()]
        try:
            challenges = [_reconstruct(payload, "stored challenge") for payload in payloads]
            return ChallengeCatalog(challenges).challenges
        except CatalogValidationError as error:
            raise ChallengeRepositoryError(
                f"stored challenge catalog is invalid: {error}"
            ) from error


def _validate_incoming_catalog(catalog: Sequence[ChallengeSpec]) -> tuple[ChallengeSpec, ...]:
    try:
        raw_challenges = tuple(catalog)
    except TypeError as error:
        raise CatalogValidationError(f"challenge catalog must be a sequence: {error}") from error
    if any(not isinstance(challenge, ChallengeSpec) for challenge in raw_challenges):
        raise CatalogValidationError("challenge catalog entries must be ChallengeSpec records")
    try:
        validated = [ChallengeSpec(challenge.dict()) for challenge in raw_challenges]
        return ChallengeCatalog(validated).challenges
    except (TypeError, ValueError, KeyError, ChallengeSpec.Error, CatalogValidationError) as error:
        raise CatalogValidationError(f"invalid challenge catalog: {error}") from error


def _reconstruct(payload: Any, challenge_id: str) -> ChallengeSpec:
    try:
        return ChallengeSpec(payload)
    except Exception as error:
        raise ChallengeRepositoryError(f"stored challenge is invalid: {challenge_id}") from error


__all__ = [
    "CHALLENGES_SHELF",
    "ChallengeNotFoundError",
    "ChallengeRepositoryError",
    "ShelfDbChallengeRepository",
]
