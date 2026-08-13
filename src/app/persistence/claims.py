"""ShelfDB-backed atomic generation-attempt claims."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.domain.models import AttemptClaim, GameState, RoundRecord

from .rounds import RoundNotFoundError

_CLAIMS_SHELF = "attempt_claims"
_ROUNDS_SHELF = "rounds"


class RoundNotClaimableError(ValueError):
    """Raised when a round is not an active generation round."""


class GenerationAlreadyRunningError(RuntimeError):
    """Raised when a non-expired generation claim already owns a round."""


class StaleAttemptTokenError(ValueError):
    """Raised when an attempt token no longer owns a round claim."""


class ShelfDbGenerationClaims:
    """Atomically lease provider attempts against validated round mappings."""

    def __init__(self, db: DB) -> None:
        self._db = db
        # ShelfDB rejects overlapping top-level writers on one shared environment.
        self._write_lock = Lock()
        # Initialize the transient shelf so read-only operations work on an empty DB.
        with self._db.transaction(write=True) as transaction:
            transaction.shelf(_CLAIMS_SHELF)

    def claim(self, round_id: str, claim: AttemptClaim, now: str) -> AttemptClaim:
        """Create a claim when ``round_id`` is generating and no live claim exists."""

        validated_claim = _validate_claim(claim)
        now_value = _parse_utc_timestamp(now, "now")
        claimed_at = _parse_utc_timestamp(validated_claim.claimed_at, "claimed_at")
        lease_expires_at = _parse_utc_timestamp(
            validated_claim.lease_expires_at,
            "lease_expires_at",
        )
        if lease_expires_at <= claimed_at:
            raise ValueError("lease_expires_at must be after claimed_at")
        if lease_expires_at <= now_value:
            raise ValueError("lease_expires_at must be after now")

        with self._write_lock, self._db.transaction(write=True) as transaction:
            round_shelf = transaction.shelf(_ROUNDS_SHELF)
            round_record = _read_round(round_shelf, round_id)
            _require_generating_round(round_record)

            claim_shelf = transaction.shelf(_CLAIMS_SHELF)
            existing_payload = _read_payload(claim_shelf, round_id)
            if existing_payload is not None:
                existing = _reconstruct_claim(existing_payload, round_id)
                if _parse_utc_timestamp(existing.lease_expires_at, "lease_expires_at") > now_value:
                    raise GenerationAlreadyRunningError(
                        f"generation already running for round: {round_id}"
                    )
                claim_shelf.key(round_id).delete()

            claim_shelf.put(round_id, validated_claim.dict())
        return validated_claim

    def get(self, round_id: str) -> AttemptClaim | None:
        """Return a freshly validated claim for ``round_id``, if present."""

        with self._db.transaction(write=False) as transaction:
            payload = _read_payload(transaction.shelf(_CLAIMS_SHELF), round_id)
        if payload is None:
            return None
        return _reconstruct_claim(payload, round_id)

    def renew(
        self,
        round_id: str,
        attempt_token: str,
        lease_expires_at: str,
        now: str,
    ) -> AttemptClaim:
        """Extend a matching live claim without changing its owner or claim timestamp."""

        expiry = _parse_utc_timestamp(lease_expires_at, "lease_expires_at")
        now_value = _parse_utc_timestamp(now, "now")
        with self._write_lock, self._db.transaction(write=True) as transaction:
            shelf = transaction.shelf(_CLAIMS_SHELF)
            existing = _require_live_matching_claim(
                shelf,
                round_id,
                attempt_token,
                now_value,
            )
            claimed_at = _parse_utc_timestamp(existing.claimed_at, "claimed_at")
            if expiry <= claimed_at:
                raise ValueError("lease_expires_at must be after claimed_at")
            if expiry <= now_value:
                raise ValueError("lease_expires_at must be after now")

            renewed = AttemptClaim(
                {
                    **existing.dict(),
                    "lease_expires_at": lease_expires_at,
                }
            )
            shelf.put(round_id, renewed.dict())
        return renewed

    def release(self, round_id: str, attempt_token: str, now: str) -> None:
        """Delete only a live claim owned by ``attempt_token``."""

        now_value = _parse_utc_timestamp(now, "now")
        with self._write_lock, self._db.transaction(write=True) as transaction:
            shelf = transaction.shelf(_CLAIMS_SHELF)
            _require_live_matching_claim(shelf, round_id, attempt_token, now_value)
            shelf.key(round_id).delete()

    def replace_round_and_release(
        self,
        record: RoundRecord,
        attempt_token: str,
        now: str,
    ) -> None:
        """Replace a round and delete its matching live claim atomically."""

        validated_record = _validate_record(record)
        now_value = _parse_utc_timestamp(now, "now")
        with self._write_lock, self._db.transaction(write=True) as transaction:
            round_shelf = transaction.shelf(_ROUNDS_SHELF)
            _read_round(round_shelf, validated_record.id)

            claim_shelf = transaction.shelf(_CLAIMS_SHELF)
            _require_live_matching_claim(
                claim_shelf,
                validated_record.id,
                attempt_token,
                now_value,
            )
            round_shelf.put(validated_record.id, validated_record.dict())
            claim_shelf.key(validated_record.id).delete()

    def replace_round_and_clear_claim(self, record: RoundRecord) -> None:
        """Replace a validated round and clear any current claim atomically."""

        validated_record = _validate_record(record)
        with self._write_lock, self._db.transaction(write=True) as transaction:
            round_shelf = transaction.shelf(_ROUNDS_SHELF)
            _read_round(round_shelf, validated_record.id)
            round_shelf.put(validated_record.id, validated_record.dict())

            claim_shelf = transaction.shelf(_CLAIMS_SHELF)
            if _read_payload(claim_shelf, validated_record.id) is not None:
                claim_shelf.key(validated_record.id).delete()


def _read_payload(shelf: Any, key: str) -> Any | None:
    item = next(iter(shelf.key(key).items()), None)
    return None if item is None else item.value


def _read_round(shelf: Any, round_id: str) -> RoundRecord:
    payload = _read_payload(shelf, round_id)
    if payload is None:
        raise RoundNotFoundError(f"round not found: {round_id}")
    return _reconstruct_round(payload, round_id)


def _require_generating_round(record: RoundRecord) -> None:
    if record.state is not GameState.GENERATING or record.terminal_disposition is not None:
        raise RoundNotClaimableError(f"round is not claimable: {record.id}")


def _require_matching_claim(shelf: Any, round_id: str, attempt_token: str) -> AttemptClaim:
    payload = _read_payload(shelf, round_id)
    if payload is None:
        raise StaleAttemptTokenError(f"stale attempt token for round: {round_id}")
    claim = _reconstruct_claim(payload, round_id)
    if claim.attempt_token != attempt_token:
        raise StaleAttemptTokenError(f"stale attempt token for round: {round_id}")
    return claim


def _require_live_matching_claim(
    shelf: Any,
    round_id: str,
    attempt_token: str,
    now: datetime,
) -> AttemptClaim:
    claim = _require_matching_claim(shelf, round_id, attempt_token)
    if _parse_utc_timestamp(claim.lease_expires_at, "lease_expires_at") <= now:
        raise StaleAttemptTokenError(f"stale attempt token for round: {round_id}")
    return claim


def _validate_claim(claim: AttemptClaim) -> AttemptClaim:
    if not isinstance(claim, AttemptClaim):
        raise TypeError("claim must be an AttemptClaim")
    return AttemptClaim(claim.dict())


def _reconstruct_claim(payload: Any, round_id: str) -> AttemptClaim:
    try:
        return AttemptClaim(payload)
    except Exception as error:
        raise ValueError(f"stored claim is invalid: {round_id}") from error


def _validate_record(record: RoundRecord) -> RoundRecord:
    if not isinstance(record, RoundRecord):
        raise TypeError("record must be a RoundRecord")
    return RoundRecord(record.dict())


def _reconstruct_round(payload: Any, round_id: str) -> RoundRecord:
    try:
        return RoundRecord(payload)
    except Exception as error:
        raise ValueError(f"stored round is invalid: {round_id}") from error


def _parse_utc_timestamp(value: str, field_name: str) -> datetime:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a UTC ISO-8601 string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a UTC ISO-8601 string") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must include UTC timezone")
    return parsed.astimezone(UTC)


__all__ = [
    "GenerationAlreadyRunningError",
    "RoundNotClaimableError",
    "ShelfDbGenerationClaims",
    "StaleAttemptTokenError",
]
