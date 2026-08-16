"""ShelfDB-backed atomic generation-attempt claims."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.domain.models import AttemptClaim, GameState, RoundRecord

from .rounds import (
    _SHELFDB_WRITE_LOCK,
    RoundNotFoundError,
    RoundSnapshotConflictError,
    _require_expected_snapshot,
)

_CLAIMS_SHELF = "attempt_claims"
_ROUNDS_SHELF = "rounds"


class RoundNotClaimableError(ValueError):
    """Raised when a round is not an active generation round."""


class GenerationAlreadyRunningError(RuntimeError):
    """Raised when a non-expired generation claim already owns a round."""


class StaleAttemptTokenError(ValueError):
    """Raised when an attempt token no longer owns a round claim."""


class ShelfDbGenerationClaims:
    """Atomically lease provider attempts against validated round mappings.

    Production lease operations read the injected clock only after entering the
    shared ShelfDB write unit.  The caller-time methods remain lower-level helpers
    for deterministic repository tests and administrative setup.
    """

    def __init__(
        self,
        db: DB,
        utc_clock: Callable[[], datetime],
        lease_duration: timedelta,
    ) -> None:
        self._db = db
        self._utc_clock = utc_clock
        self._lease_duration = _validate_lease_duration(lease_duration)
        # ShelfDB rejects overlapping top-level writers on one shared environment.
        self._write_lock = _SHELFDB_WRITE_LOCK
        # Initialize the transient shelf so read-only operations work on an empty DB.
        with self._write_lock, self._db.transaction(write=True) as transaction:
            transaction.shelf(_CLAIMS_SHELF)

    @property
    def lease_duration(self) -> timedelta:
        """Return the configured duration used by fresh production operations."""

        return self._lease_duration

    def acquire_fresh(
        self,
        round_id: str,
        attempt_token: str,
        owner_instance: str,
        requested_at: str,
    ) -> AttemptClaim:
        """Acquire a request-time lease only if it is live at transaction-time.

        ``requested_at`` starts the acquisition budget before executor queuing.
        The trusted transaction clock rejects a request whose full budget elapsed,
        then starts the stored lease at transaction-time so an accepted provider
        always receives the complete configured lease.
        """

        request_deadline = _parse_utc_timestamp(requested_at, "requested_at") + self._lease_duration
        with self._write_lock, self._db.transaction(write=True) as transaction:
            now = self._trusted_utc_now()
            if request_deadline <= now:
                raise StaleAttemptTokenError(
                    f"generation acquisition expired for round: {round_id}"
                )
            claim = AttemptClaim(
                attempt_token=attempt_token,
                owner_instance=owner_instance,
                claimed_at=now.isoformat(),
                lease_expires_at=(now + self._lease_duration).isoformat(),
            )
            _claim_in_transaction(transaction, round_id, claim, now)
        return claim

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
            _claim_in_transaction(transaction, round_id, validated_claim, now_value)
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

    def renew_fresh(self, round_id: str, attempt_token: str) -> AttemptClaim:
        """Extend a matching claim using liveness and expiry from transaction-time."""

        with self._write_lock, self._db.transaction(write=True) as transaction:
            now = self._trusted_utc_now()
            shelf = transaction.shelf(_CLAIMS_SHELF)
            existing = _require_live_matching_claim(shelf, round_id, attempt_token, now)
            expiry = now + self._lease_duration
            claimed_at = _parse_utc_timestamp(existing.claimed_at, "claimed_at")
            if expiry <= claimed_at:
                raise ValueError("lease_expires_at must be after claimed_at")
            renewed = AttemptClaim(
                {
                    **existing.dict(),
                    "lease_expires_at": expiry.isoformat(),
                }
            )
            shelf.put(round_id, renewed.dict())
        return renewed

    def release_fresh(self, round_id: str, attempt_token: str) -> None:
        """Delete a matching claim only when it is live at transaction-time."""

        with self._write_lock, self._db.transaction(write=True) as transaction:
            now = self._trusted_utc_now()
            shelf = transaction.shelf(_CLAIMS_SHELF)
            _require_live_matching_claim(shelf, round_id, attempt_token, now)
            shelf.key(round_id).delete()

    def release(self, round_id: str, attempt_token: str, now: str) -> None:
        """Delete only a live claim owned by ``attempt_token`` using caller-time."""

        now_value = _parse_utc_timestamp(now, "now")
        with self._write_lock, self._db.transaction(write=True) as transaction:
            shelf = transaction.shelf(_CLAIMS_SHELF)
            _require_live_matching_claim(shelf, round_id, attempt_token, now_value)
            shelf.key(round_id).delete()

    def release_matching(self, round_id: str, attempt_token: str) -> None:
        """Delete a claim only when its token still matches, even if expired.

        Cancellation cleanup must release an abandoned token without pretending that
        the claim is still live.  Token comparison still fences a replacement claim.
        """

        with self._write_lock, self._db.transaction(write=True) as transaction:
            shelf = transaction.shelf(_CLAIMS_SHELF)
            _require_matching_claim(shelf, round_id, attempt_token)
            shelf.key(round_id).delete()

    def replace_round_and_release_fresh(
        self,
        record: RoundRecord,
        attempt_token: str,
        *,
        expected: RoundRecord | None = None,
    ) -> None:
        """Replace a round only for a matching lease live at transaction-time."""

        validated_record, expected_snapshot = _validate_replacement(record, expected)
        with self._write_lock, self._db.transaction(write=True) as transaction:
            now = self._trusted_utc_now()
            _replace_round_and_release_in_transaction(
                transaction,
                validated_record,
                attempt_token,
                now,
                expected_snapshot,
            )

    def replace_round_and_release(
        self,
        record: RoundRecord,
        attempt_token: str,
        now: str,
        *,
        expected: RoundRecord | None = None,
    ) -> None:
        """Replace a round using caller-time for lower-level repository tests.

        ``expected=None`` is retained for focused repository setup and
        compatibility with the lower-level claim utility tests.
        """

        validated_record, expected_snapshot = _validate_replacement(record, expected)
        now_value = _parse_utc_timestamp(now, "now")
        with self._write_lock, self._db.transaction(write=True) as transaction:
            _replace_round_and_release_in_transaction(
                transaction,
                validated_record,
                attempt_token,
                now_value,
                expected_snapshot,
            )

    def replace_round_and_clear_claim(
        self,
        record: RoundRecord,
        *,
        expected: RoundRecord | None = None,
    ) -> None:
        """Replace a round and clear any current claim atomically.

        Generation exit passes the snapshot read before its event.  The optional
        form remains available to explicitly administrative cleanup/setup code.
        """

        validated_record = _validate_record(record)
        expected_snapshot = _validate_record(expected) if expected is not None else None
        if expected_snapshot is not None and expected_snapshot.id != validated_record.id:
            raise RoundSnapshotConflictError(
                f"round snapshot does not match replacement ID: {validated_record.id}"
            )
        with self._write_lock, self._db.transaction(write=True) as transaction:
            round_shelf = transaction.shelf(_ROUNDS_SHELF)
            current = _read_round(round_shelf, validated_record.id)
            if expected_snapshot is not None:
                _require_expected_snapshot(current, expected_snapshot)
            round_shelf.put(validated_record.id, validated_record.dict())

            claim_shelf = transaction.shelf(_CLAIMS_SHELF)
            if _read_payload(claim_shelf, validated_record.id) is not None:
                claim_shelf.key(validated_record.id).delete()

    def _trusted_utc_now(self) -> datetime:
        current = self._utc_clock()
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            raise ValueError("UTC clock must return an aware datetime")
        return current.astimezone(UTC)


def _claim_in_transaction(
    transaction: Any,
    round_id: str,
    claim: AttemptClaim,
    now: datetime,
) -> None:
    round_record = _read_round(transaction.shelf(_ROUNDS_SHELF), round_id)
    _require_generating_round(round_record)

    claim_shelf = transaction.shelf(_CLAIMS_SHELF)
    existing_payload = _read_payload(claim_shelf, round_id)
    if existing_payload is not None:
        existing = _reconstruct_claim(existing_payload, round_id)
        if _parse_utc_timestamp(existing.lease_expires_at, "lease_expires_at") > now:
            raise GenerationAlreadyRunningError(f"generation already running for round: {round_id}")
        claim_shelf.key(round_id).delete()
    claim_shelf.put(round_id, claim.dict())


def _replace_round_and_release_in_transaction(
    transaction: Any,
    record: RoundRecord,
    attempt_token: str,
    now: datetime,
    expected: RoundRecord | None,
) -> None:
    round_shelf = transaction.shelf(_ROUNDS_SHELF)
    current = _read_round(round_shelf, record.id)
    if expected is not None:
        _require_expected_snapshot(current, expected)

    claim_shelf = transaction.shelf(_CLAIMS_SHELF)
    _require_live_matching_claim(claim_shelf, record.id, attempt_token, now)
    round_shelf.put(record.id, record.dict())
    claim_shelf.key(record.id).delete()


def _validate_replacement(
    record: RoundRecord,
    expected: RoundRecord | None,
) -> tuple[RoundRecord, RoundRecord | None]:
    validated_record = _validate_record(record)
    expected_snapshot = _validate_record(expected) if expected is not None else None
    if expected_snapshot is not None and expected_snapshot.id != validated_record.id:
        raise RoundSnapshotConflictError(
            f"round snapshot does not match replacement ID: {validated_record.id}"
        )
    return validated_record, expected_snapshot


def _validate_lease_duration(value: timedelta) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError("lease_duration must be a timedelta")
    if value <= timedelta(0):
        raise ValueError("lease_duration must be positive")
    return value


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
