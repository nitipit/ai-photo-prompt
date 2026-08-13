"""ShelfDB-backed storage for complete validated round records."""

from __future__ import annotations

from typing import Any

from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.domain.models import LevelGroup, RoundRecord, TerminalDisposition

_ROUNDS_SHELF = "rounds"


class RoundConflictError(ValueError):
    """Raised when creating a round whose ID is already stored."""


class RoundNotFoundError(LookupError):
    """Raised when a requested round ID is not stored."""


class ShelfDbRoundRepository:
    """Store complete ``RoundRecord.dict()`` mappings in ShelfDB."""

    def __init__(self, db: DB) -> None:
        self._db = db
        # ShelfDB creates named shelves on first writable open; initialize it so
        # empty databases can also be read through a read-only transaction.
        with self._db.transaction(write=True) as transaction:
            transaction.shelf(_ROUNDS_SHELF)

    def create(self, record: RoundRecord) -> None:
        """Validate and insert ``record`` unless its ID already exists."""

        validated = _validate_record(record)
        with self._db.transaction(write=True) as transaction:
            shelf = transaction.shelf(_ROUNDS_SHELF)
            if shelf.key(validated.id).exists():
                raise RoundConflictError(f"round already exists: {validated.id}")
            shelf.put(validated.id, validated.dict())

    def get(self, round_id: str) -> RoundRecord:
        """Return a freshly validated record for ``round_id``."""

        with self._db.transaction(write=False) as transaction:
            item = next(iter(transaction.shelf(_ROUNDS_SHELF).key(round_id).items()), None)
        if item is None:
            raise RoundNotFoundError(f"round not found: {round_id}")
        return _reconstruct(item.value, round_id)

    def replace(self, record: RoundRecord) -> None:
        """Atomically replace an existing round with the fully validated record."""

        validated = _validate_record(record)
        with self._db.transaction(write=True) as transaction:
            shelf = transaction.shelf(_ROUNDS_SHELF)
            if not shelf.key(validated.id).exists():
                raise RoundNotFoundError(f"round not found: {validated.id}")
            shelf.put(validated.id, validated.dict())

    def list_completed(self, level: LevelGroup | None = None) -> list[RoundRecord]:
        """Return completed rounds ordered by completion timestamp and ID."""

        with self._db.transaction(write=False) as transaction:
            records = [
                _reconstruct(item.value, "stored round")
                for item in transaction.shelf(_ROUNDS_SHELF).items()
            ]
        return sorted(
            (
                record
                for record in records
                if record.terminal_disposition is TerminalDisposition.COMPLETED
                and (level is None or record.level is level)
            ),
            key=lambda record: (record.completed_at or "", record.id),
        )


def _validate_record(record: RoundRecord) -> RoundRecord:
    if not isinstance(record, RoundRecord):
        raise TypeError("record must be a RoundRecord")
    return RoundRecord(record.dict())


def _reconstruct(payload: Any, round_id: str) -> RoundRecord:
    if payload is None:
        raise RoundNotFoundError(f"round not found: {round_id}")
    try:
        return RoundRecord(payload)
    except Exception as error:
        raise ValueError(f"stored round is invalid: {round_id}") from error


__all__ = ["RoundConflictError", "RoundNotFoundError", "ShelfDbRoundRepository"]
