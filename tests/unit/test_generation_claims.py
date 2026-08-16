from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.domain.models import (
    AttemptClaim,
    FailureDetail,
    GameState,
    RoundRecord,
    TerminalDisposition,
)
from app.persistence import (
    GenerationAlreadyRunningError,
    RoundNotClaimableError,
    RoundNotFoundError,
    RoundSnapshotConflictError,
    ShelfDbGenerationClaims,
    ShelfDbRoundRepository,
    StaleAttemptTokenError,
)

NOW = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC).isoformat()
CLAIMED_AT = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
LIVE_EXPIRY = datetime(2026, 1, 1, 0, 1, tzinfo=UTC).isoformat()
EXPIRED_EXPIRY = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC).isoformat()


def make_round(
    *,
    state: GameState = GameState.GENERATING,
    disposition: TerminalDisposition | None = None,
) -> RoundRecord:
    return RoundRecord(
        id=str(uuid4()),
        state=state,
        display_name="Tester",
        terminal_disposition=disposition,
        created_at=CLAIMED_AT,
        updated_at=CLAIMED_AT,
    )


def make_claim(
    token: str = "attempt-1",
    *,
    lease_expires_at: str = LIVE_EXPIRY,
) -> AttemptClaim:
    return AttemptClaim(
        attempt_token=token,
        owner_instance="worker-1",
        claimed_at=CLAIMED_AT,
        lease_expires_at=lease_expires_at,
    )


@pytest.fixture
def persistence(tmp_path: Path):
    db = DB(str(tmp_path / "claims-db"))
    try:
        rounds = ShelfDbRoundRepository(db)
        yield (
            ShelfDbGenerationClaims(
                db,
                lambda: datetime.fromisoformat(NOW),
                timedelta(seconds=30),
            ),
            rounds,
        )
    finally:
        db.close()


def test_fresh_acquisition_starts_full_lease_at_transaction_time(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    rounds.create(record)

    acquired = claims.acquire_fresh(
        record.id,
        "attempt-1",
        "worker-1",
        CLAIMED_AT,
    )

    assert acquired.claimed_at == NOW
    assert (
        acquired.lease_expires_at
        == datetime(
            2026,
            1,
            1,
            0,
            0,
            40,
            tzinfo=UTC,
        ).isoformat()
    )
    assert claims.get(record.id).dict() == acquired.dict()


def test_fresh_acquisition_rejects_exact_request_expiry(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    rounds.create(record)
    fresh_claims = ShelfDbGenerationClaims(
        claims._db,  # noqa: SLF001
        lambda: datetime.fromisoformat(NOW),
        timedelta(seconds=10),
    )

    with pytest.raises(StaleAttemptTokenError):
        fresh_claims.acquire_fresh(
            record.id,
            "attempt-1",
            "worker-1",
            CLAIMED_AT,
        )

    assert claims.get(record.id) is None


@pytest.mark.parametrize("operation", ["renew", "release", "finalize"])
def test_fresh_live_operations_reject_exact_claim_expiry(
    persistence,
    operation: str,
) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim(lease_expires_at=NOW)
    rounds.create(record)
    claims.claim(record.id, existing, CLAIMED_AT)

    with pytest.raises(StaleAttemptTokenError):
        if operation == "renew":
            claims.renew_fresh(record.id, existing.attempt_token)
        elif operation == "release":
            claims.release_fresh(record.id, existing.attempt_token)
        else:
            replacement = RoundRecord(**{**record.dict(), "state": GameState.GENERATED_REVEAL})
            claims.replace_round_and_release_fresh(
                replacement,
                existing.attempt_token,
                expected=record,
            )

    assert rounds.get(record.id).dict() == record.dict()
    assert claims.get(record.id).dict() == existing.dict()


def test_live_claim_is_rejected(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    with pytest.raises(GenerationAlreadyRunningError):
        claims.claim(record.id, make_claim("attempt-2"), NOW)

    assert claims.get(record.id).dict() == existing.dict()


def test_expired_claim_is_replaced_atomically(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    replacement = make_claim("attempt-2")
    rounds.create(record)
    claims.claim(record.id, make_claim(lease_expires_at=EXPIRED_EXPIRY), CLAIMED_AT)

    returned = claims.claim(record.id, replacement, NOW)

    assert returned.dict() == replacement.dict()
    assert claims.get(record.id).dict() == replacement.dict()


@pytest.mark.parametrize(
    ("lease_expires_at", "message"),
    [
        (CLAIMED_AT, "after claimed_at"),
        (NOW, "after now"),
    ],
)
def test_invalid_new_claim_chronology_rolls_back_without_storage(
    persistence,
    lease_expires_at: str,
    message: str,
) -> None:
    claims, rounds = persistence
    record = make_round()
    rounds.create(record)

    with pytest.raises(ValueError, match=message):
        claims.claim(record.id, make_claim(lease_expires_at=lease_expires_at), NOW)

    assert claims.get(record.id) is None


@pytest.mark.parametrize(
    ("state", "disposition", "expected"),
    [
        (GameState.PROMPT_ENTRY, None, RoundNotClaimableError),
        (GameState.ABANDONED, TerminalDisposition.ABANDONED, RoundNotClaimableError),
        (GameState.LEADERBOARD, TerminalDisposition.COMPLETED, RoundNotClaimableError),
    ],
)
def test_non_generating_and_terminal_rounds_cannot_be_claimed(
    persistence,
    state: GameState,
    disposition: TerminalDisposition | None,
    expected: type[Exception],
) -> None:
    claims, rounds = persistence
    record = make_round(state=state, disposition=disposition)
    rounds.create(record)

    with pytest.raises(expected):
        claims.claim(record.id, make_claim(), NOW)


def test_missing_round_cannot_be_claimed(persistence) -> None:
    claims, _rounds = persistence

    with pytest.raises(RoundNotFoundError):
        claims.claim(str(uuid4()), make_claim(), NOW)


def test_renew_requires_token_and_preserves_claim_fields(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    with pytest.raises(StaleAttemptTokenError):
        claims.renew(record.id, "wrong-token", LIVE_EXPIRY, NOW)
    assert claims.get(record.id).dict() == existing.dict()

    renewed = claims.renew(
        record.id,
        existing.attempt_token,
        "2026-01-01T00:02:00Z",
        NOW,
    )

    assert renewed.owner_instance == existing.owner_instance
    assert renewed.claimed_at == existing.claimed_at
    assert renewed.lease_expires_at == "2026-01-01T00:02:00Z"


def test_renew_rejects_expiry_at_or_before_claimed_time(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    with pytest.raises(ValueError, match="after claimed_at"):
        claims.renew(record.id, existing.attempt_token, CLAIMED_AT, NOW)

    assert claims.get(record.id).dict() == existing.dict()


@pytest.mark.parametrize("now", [EXPIRED_EXPIRY, NOW])
def test_renew_rejects_expired_claim_at_and_after_expiry(persistence, now: str) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim(lease_expires_at=EXPIRED_EXPIRY)
    rounds.create(record)
    claims.claim(record.id, existing, CLAIMED_AT)

    with pytest.raises(StaleAttemptTokenError):
        claims.renew(record.id, existing.attempt_token, LIVE_EXPIRY, now)

    assert claims.get(record.id).dict() == existing.dict()


def test_renew_requires_new_expiry_after_now(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    with pytest.raises(ValueError, match="after now"):
        claims.renew(record.id, existing.attempt_token, NOW, NOW)

    assert claims.get(record.id).dict() == existing.dict()


def test_release_requires_token_and_deletes_matching_claim(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    with pytest.raises(StaleAttemptTokenError):
        claims.release(record.id, "wrong-token", NOW)
    assert claims.get(record.id).dict() == existing.dict()

    claims.release(record.id, existing.attempt_token, NOW)

    assert claims.get(record.id) is None


def test_release_matching_cleans_expired_token_without_fencing_replacement(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim(lease_expires_at=EXPIRED_EXPIRY)
    rounds.create(record)
    claims.claim(record.id, existing, CLAIMED_AT)

    claims.release_matching(record.id, existing.attempt_token)

    assert claims.get(record.id) is None


@pytest.mark.parametrize("now", [EXPIRED_EXPIRY, NOW])
def test_release_rejects_expired_claim_at_and_after_expiry(persistence, now: str) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim(lease_expires_at=EXPIRED_EXPIRY)
    rounds.create(record)
    claims.claim(record.id, existing, CLAIMED_AT)

    with pytest.raises(StaleAttemptTokenError):
        claims.release(record.id, existing.attempt_token, now)

    assert claims.get(record.id).dict() == existing.dict()


def test_claim_coupled_replacement_rejects_a_stale_round_snapshot(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    replacement = RoundRecord(**{**record.dict(), "state": GameState.GENERATED_REVEAL})
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)
    changed = RoundRecord(**{**record.dict(), "display_name": "Changed"})
    rounds.replace_unsafe(changed)

    with pytest.raises(RoundSnapshotConflictError):
        claims.replace_round_and_release(
            replacement,
            existing.attempt_token,
            NOW,
            expected=record,
        )

    assert rounds.get(record.id).dict() == changed.dict()
    assert claims.get(record.id).dict() == existing.dict()


def test_replace_round_and_release_is_atomic(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    replacement = RoundRecord(**{**record.dict(), "state": GameState.GENERATED_REVEAL})
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    claims.replace_round_and_release(replacement, existing.attempt_token, NOW)

    assert rounds.get(record.id).dict() == replacement.dict()
    assert claims.get(record.id) is None


@pytest.mark.parametrize("now", [EXPIRED_EXPIRY, NOW])
@pytest.mark.parametrize(
    ("state", "changes"),
    [
        (GameState.GENERATED_REVEAL, {"generated_at": NOW}),
        (
            GameState.GENERATING,
            {
                "pipeline_failure": FailureDetail(
                    code="provider_error",
                    message="retry later",
                ),
            },
        ),
    ],
)
def test_expired_claim_cannot_finalize_or_fail_round(
    persistence,
    now: str,
    state: GameState,
    changes: dict[str, object],
) -> None:
    claims, rounds = persistence
    record = make_round()
    replacement = RoundRecord(**{**record.dict(), "state": state, **changes})
    existing = make_claim(lease_expires_at=EXPIRED_EXPIRY)
    rounds.create(record)
    claims.claim(record.id, existing, CLAIMED_AT)

    with pytest.raises(StaleAttemptTokenError):
        claims.replace_round_and_release(replacement, existing.attempt_token, now)

    assert rounds.get(record.id).dict() == record.dict()
    assert claims.get(record.id).dict() == existing.dict()


def test_replace_round_and_clear_claim_works_without_an_attempt(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    replacement = RoundRecord(**{**record.dict(), "state": GameState.ABANDONED})
    rounds.create(record)

    claims.replace_round_and_clear_claim(replacement)

    assert rounds.get(record.id).dict() == replacement.dict()
    assert claims.get(record.id) is None


def test_replace_round_and_clear_claim_removes_active_attempt(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    replacement = RoundRecord(**{**record.dict(), "state": GameState.ABANDONED})
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    claims.replace_round_and_clear_claim(replacement)

    assert rounds.get(record.id).dict() == replacement.dict()
    assert claims.get(record.id) is None


def test_cleared_attempt_rejects_a_late_token_commit(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    replacement = RoundRecord(**{**record.dict(), "state": GameState.ABANDONED})
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    claims.replace_round_and_clear_claim(replacement)

    with pytest.raises(StaleAttemptTokenError):
        claims.replace_round_and_release(record, existing.attempt_token, NOW)
    assert rounds.get(record.id).dict() == replacement.dict()


def test_stale_token_cannot_commit_after_claim_replacement(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    old_claim = make_claim("old-token", lease_expires_at=EXPIRED_EXPIRY)
    new_claim = make_claim("new-token")
    rounds.create(record)
    claims.claim(record.id, old_claim, CLAIMED_AT)
    claims.claim(record.id, new_claim, NOW)

    replacement = RoundRecord(**{**record.dict(), "state": GameState.GENERATED_REVEAL})
    with pytest.raises(StaleAttemptTokenError):
        claims.replace_round_and_release(replacement, old_claim.attempt_token, NOW)

    assert rounds.get(record.id).dict() == record.dict()
    assert claims.get(record.id).dict() == new_claim.dict()


def test_two_simultaneous_claims_have_exactly_one_success(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    rounds.create(record)
    barrier = Barrier(2)

    def attempt(token: str) -> str:
        barrier.wait()
        try:
            claims.claim(record.id, make_claim(token), NOW)
        except GenerationAlreadyRunningError:
            return "rejected"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ("attempt-1", "attempt-2")))

    assert sorted(outcomes) == ["claimed", "rejected"]
    assert claims.get(record.id).attempt_token in {"attempt-1", "attempt-2"}


def test_failed_transaction_rolls_back_claim_write(persistence) -> None:
    claims, _rounds = persistence
    claim = make_claim()

    with pytest.raises(RuntimeError, match="forced rollback"):
        with claims._db.transaction(write=True) as transaction:  # noqa: SLF001
            transaction.shelf("attempt_claims").put("round-id", claim.dict())
            raise RuntimeError("forced rollback")

    assert claims.get("round-id") is None


def test_claim_survives_close_and_reopen(tmp_path: Path) -> None:
    path = str(tmp_path / "durable-claims")
    record = make_round()
    claim = make_claim()

    first_db = DB(path)
    try:
        rounds = ShelfDbRoundRepository(first_db)
        claims = ShelfDbGenerationClaims(
            first_db,
            lambda: datetime.fromisoformat(NOW),
            timedelta(seconds=30),
        )
        rounds.create(record)
        claims.claim(record.id, claim, NOW)
    finally:
        first_db.close()

    second_db = DB(path)
    try:
        rebuilt = ShelfDbGenerationClaims(
            second_db,
            lambda: datetime.fromisoformat(NOW),
            timedelta(seconds=30),
        ).get(record.id)
    finally:
        second_db.close()

    assert rebuilt is not claim
    assert rebuilt.dict() == claim.dict()
