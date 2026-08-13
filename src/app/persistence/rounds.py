"""ShelfDB-backed storage for complete validated round records."""

from __future__ import annotations

from threading import Lock
from typing import Any

from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.domain.models import LevelGroup, RoundRecord, TerminalDisposition

_ROUNDS_SHELF = "rounds"
_SHELFDB_WRITE_LOCK = Lock()


class RoundConflictError(ValueError):
    """Raised when creating a round whose ID is already stored."""


class RoundSnapshotConflictError(ValueError):
    """Raised when a replacement is based on a stale persisted round snapshot."""


class RoundNotFoundError(LookupError):
    """Raised when a requested round ID is not stored."""


class ShelfDbRoundRepository:
    """Store complete ``RoundRecord.dict()`` mappings in ShelfDB."""

    def __init__(self, db: DB) -> None:
        self._db = db
        # ShelfDB permits only one top-level writer per environment.
        self._write_lock = _SHELFDB_WRITE_LOCK
        # ShelfDB creates named shelves on first writable open; initialize it so
        # empty databases can also be read through a read-only transaction.
        with self._write_lock, self._db.transaction(write=True) as transaction:
            transaction.shelf(_ROUNDS_SHELF)

    def create(self, record: RoundRecord) -> None:
        """Validate and insert ``record`` unless its ID already exists."""

        validated = _validate_record(record)
        with self._write_lock, self._db.transaction(write=True) as transaction:
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
        """Replace a round without a snapshot check for administrative setup only.

        Event handlers must use :meth:`replace_if_current` so a stale request cannot
        overwrite a newer persisted event.  This method remains for test fixtures
        and other explicitly administrative replacements.
        """

        self.replace_unsafe(record)

    def replace_unsafe(self, record: RoundRecord) -> None:
        """Atomically replace an existing round without compare-and-swap fencing."""

        validated = _validate_record(record)
        with self._write_lock, self._db.transaction(write=True) as transaction:
            shelf = transaction.shelf(_ROUNDS_SHELF)
            if not shelf.key(validated.id).exists():
                raise RoundNotFoundError(f"round not found: {validated.id}")
            shelf.put(validated.id, validated.dict())

    def replace_if_current(self, record: RoundRecord, expected: RoundRecord) -> None:
        """Replace ``record`` only when the stored snapshot still equals ``expected``.

        Validation, snapshot comparison, and the write all happen in one ShelfDB
        write transaction.  The complete snapshot is compared in addition to its
        ``state`` and ``updated_at`` identity because deterministic clocks can
        legitimately give concurrent events the same timestamp.
        """

        validated = _validate_record(record)
        expected_snapshot = _validate_record(expected)
        if validated.id != expected_snapshot.id:
            raise RoundSnapshotConflictError(
                f"round snapshot does not match replacement ID: {validated.id}"
            )
        with self._write_lock, self._db.transaction(write=True) as transaction:
            shelf = transaction.shelf(_ROUNDS_SHELF)
            current = _read_round(shelf, validated.id)
            _require_expected_snapshot(current, expected_snapshot)
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


def _read_round(shelf: Any, round_id: str) -> RoundRecord:
    payload = next(iter(shelf.key(round_id).items()), None)
    if payload is None:
        raise RoundNotFoundError(f"round not found: {round_id}")
    return _reconstruct(payload.value, round_id)


def _require_expected_snapshot(current: RoundRecord, expected: RoundRecord) -> None:
    if current.state is not expected.state or current.updated_at != expected.updated_at:
        raise RoundSnapshotConflictError(f"round snapshot is stale: {current.id}")
    if current.dict() != expected.dict():
        raise RoundSnapshotConflictError(f"round snapshot is stale: {current.id}")


def _reconstruct(payload: Any, round_id: str) -> RoundRecord:
    if payload is None:
        raise RoundNotFoundError(f"round not found: {round_id}")
    try:
        return RoundRecord(payload)
    except Exception as error:
        raise ValueError(f"stored round is invalid: {round_id}") from error


__all__ = [
    "RoundConflictError",
    "RoundNotFoundError",
    "RoundSnapshotConflictError",
    "ShelfDbRoundRepository",
]
