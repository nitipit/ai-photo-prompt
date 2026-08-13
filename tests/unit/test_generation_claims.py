from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.domain.models import AttemptClaim, GameState, RoundRecord, TerminalDisposition
from app.persistence import (
    GenerationAlreadyRunningError,
    RoundNotClaimableError,
    RoundNotFoundError,
    ShelfDbGenerationClaims,
    ShelfDbRoundRepository,
    StaleAttemptTokenError,
)

NOW = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC).isoformat()
CLAIMED_AT = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
LIVE_EXPIRY = datetime(2026, 1, 1, 0, 1, tzinfo=UTC).isoformat()
EXPIRED_EXPIRY = datetime(2025, 12, 31, 23, 59, tzinfo=UTC).isoformat()


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
        yield ShelfDbGenerationClaims(db), rounds
    finally:
        db.close()


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
    claims.claim(record.id, make_claim(lease_expires_at=EXPIRED_EXPIRY), NOW)

    returned = claims.claim(record.id, replacement, NOW)

    assert returned.dict() == replacement.dict()
    assert claims.get(record.id).dict() == replacement.dict()


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
        claims.renew(record.id, "wrong-token", LIVE_EXPIRY)
    assert claims.get(record.id).dict() == existing.dict()

    renewed = claims.renew(record.id, existing.attempt_token, "2026-01-01T00:02:00Z")

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
        claims.renew(record.id, existing.attempt_token, CLAIMED_AT)

    assert claims.get(record.id).dict() == existing.dict()


def test_release_requires_token_and_deletes_matching_claim(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    with pytest.raises(StaleAttemptTokenError):
        claims.release(record.id, "wrong-token")
    assert claims.get(record.id).dict() == existing.dict()

    claims.release(record.id, existing.attempt_token)

    assert claims.get(record.id) is None


def test_replace_round_and_release_is_atomic(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    replacement = RoundRecord(**{**record.dict(), "state": GameState.GENERATED_REVEAL})
    existing = make_claim()
    rounds.create(record)
    claims.claim(record.id, existing, NOW)

    claims.replace_round_and_release(replacement, existing.attempt_token)

    assert rounds.get(record.id).dict() == replacement.dict()
    assert claims.get(record.id) is None


def test_stale_token_cannot_commit_after_claim_replacement(persistence) -> None:
    claims, rounds = persistence
    record = make_round()
    old_claim = make_claim("old-token", lease_expires_at=EXPIRED_EXPIRY)
    new_claim = make_claim("new-token")
    rounds.create(record)
    claims.claim(record.id, old_claim, NOW)
    claims.claim(record.id, new_claim, NOW)

    replacement = RoundRecord(**{**record.dict(), "state": GameState.GENERATED_REVEAL})
    with pytest.raises(StaleAttemptTokenError):
        claims.replace_round_and_release(replacement, old_claim.attempt_token)

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
        claims = ShelfDbGenerationClaims(first_db)
        rounds.create(record)
        claims.claim(record.id, claim, NOW)
    finally:
        first_db.close()

    second_db = DB(path)
    try:
        rebuilt = ShelfDbGenerationClaims(second_db).get(record.id)
    finally:
        second_db.close()

    assert rebuilt is not claim
    assert rebuilt.dict() == claim.dict()
