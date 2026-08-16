from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.ai.pipeline import FakeAIPipeline
from app.content.repository import ChallengeCatalog
from app.domain.models import (
    AttemptClaim,
    ChallengeSpec,
    GameState,
    LevelGroup,
    PromptSubmissionReason,
    RoundRecord,
    TerminalDisposition,
)
from app.persistence import (
    RoundSnapshotConflictError,
    ShelfDbGenerationClaims,
    ShelfDbRoundRepository,
)
from app.services import GameRoundConflictError, GameRoundService


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


class BarrierRepository:
    """Release a fixed number of reads before allowing event writes to race."""

    def __init__(self, repository: ShelfDbRoundRepository, readers: int) -> None:
        self.repository = repository
        self.barrier = Barrier(readers)
        self._remaining = readers
        self._lock = Lock()

    def create(self, record: RoundRecord) -> None:
        self.repository.create(record)

    def get(self, round_id: str) -> RoundRecord:
        record = self.repository.get(round_id)
        with self._lock:
            wait = self._remaining > 0
            if wait:
                self._remaining -= 1
        if wait:
            self.barrier.wait(timeout=10)
        return record

    def replace_if_current(self, record: RoundRecord, expected: RoundRecord) -> None:
        self.repository.replace_if_current(record, expected)

    def replace_unsafe(self, record: RoundRecord) -> None:
        self.repository.replace_unsafe(record)


def make_catalog() -> ChallengeCatalog:
    return ChallengeCatalog(
        ChallengeSpec(
            id=f"{level.value}-{index}",
            title="Challenge",
            level=level,
            target_asset_url=f"/assets/challenges/{level.value}-{index}.webp",
            concept="concept",
            core_anchors=["anchor"],
            example_prompt="example prompt",
            evaluation_notes="notes",
            feedback_focus="focus",
        )
        for level in LevelGroup
        for index in range(5)
    )


@pytest.fixture
def setup(tmp_path: Path):
    db = DB(str(tmp_path / "cas-races"))
    try:
        repository = ShelfDbRoundRepository(db)
        clock = MutableClock()
        claims = ShelfDbGenerationClaims(db, clock, timedelta(seconds=30))
        yield repository, claims, clock
    finally:
        db.close()


def service_for(repository, clock, claims=None) -> GameRoundService:
    return GameRoundService(
        repository,
        make_catalog(),
        lambda choices: choices[0],
        clock,
        generation_claims=claims,
        pipeline=FakeAIPipeline() if claims is not None else None,
        owner_instance="cas-test" if claims is not None else None,
    )


async def prepare_prompt_entry(repository, clock) -> RoundRecord:
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    return await service.continue_challenge(created.id)


async def prepare_generated(repository, claims, clock) -> RoundRecord:
    service = service_for(repository, clock, claims)
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    await service.continue_challenge(created.id)
    await service.submit_prompt(created.id, "เด็กวาดภาพในสวน", PromptSubmissionReason.MANUAL)
    generated = await service.generate_round(created.id)
    assert generated.reveal_deadline is not None
    clock.current = datetime.fromisoformat(generated.reveal_deadline)
    return generated


@pytest.mark.asyncio
async def test_duplicate_setup_events_allow_one_winner_and_nine_conflicts(setup) -> None:
    repository, _claims, clock = setup
    created = await service_for(repository, clock).create_round("Tester")
    racing_repository = BarrierRepository(repository, 10)
    service = service_for(racing_repository, clock)

    outcomes = await asyncio.gather(
        *(service.configure_round(created.id, LevelGroup.P1_P3) for _ in range(10)),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, RoundRecord) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, GameRoundConflictError) for outcome in outcomes) == 9
    assert repository.get(created.id).state is GameState.CHALLENGE_REVEAL


@pytest.mark.asyncio
async def test_duplicate_prompt_events_allow_one_winner_and_nine_conflicts(setup) -> None:
    repository, _claims, clock = setup
    prompt_entry = await prepare_prompt_entry(repository, clock)
    racing_repository = BarrierRepository(repository, 10)
    service = service_for(racing_repository, clock)

    outcomes = await asyncio.gather(
        *(
            service.submit_prompt(
                prompt_entry.id,
                "เด็กวาดภาพในสวน",
                PromptSubmissionReason.MANUAL,
            )
            for _ in range(10)
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, RoundRecord) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, GameRoundConflictError) for outcome in outcomes) == 9
    assert repository.get(prompt_entry.id).state is GameState.GENERATING


@pytest.mark.asyncio
async def test_duplicate_reveal_events_allow_one_winner_and_nine_conflicts(setup) -> None:
    repository, claims, clock = setup
    generated = await prepare_generated(repository, claims, clock)
    racing_repository = BarrierRepository(repository, 10)
    service = service_for(racing_repository, clock, claims)

    outcomes = await asyncio.gather(
        *(service.show_result(generated.id) for _ in range(10)),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, RoundRecord) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, GameRoundConflictError) for outcome in outcomes) == 9
    assert repository.get(generated.id).state is GameState.RESULT


@pytest.mark.asyncio
async def test_duplicate_completion_events_allow_one_winner_and_nine_conflicts(setup) -> None:
    repository, claims, clock = setup
    generated = await prepare_generated(repository, claims, clock)
    service = service_for(repository, clock, claims)
    result = await service.show_result(generated.id)
    clock.current += timedelta(seconds=1)

    racing_repository = BarrierRepository(repository, 10)
    racing_service = service_for(racing_repository, clock, claims)
    outcomes = await asyncio.gather(
        *(racing_service.complete_round(result.id) for _ in range(10)),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, RoundRecord) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, GameRoundConflictError) for outcome in outcomes) == 9
    assert repository.get(result.id).state is GameState.LEADERBOARD


def test_stale_generation_exit_and_success_cannot_overwrite_each_other(setup) -> None:
    repository, claims, clock = setup
    timestamp = clock.current.isoformat()
    generating = RoundRecord(
        id=str(uuid4()),
        state=GameState.GENERATING,
        display_name="Tester",
        created_at=timestamp,
        updated_at=timestamp,
    )
    abandoned = RoundRecord(
        **{
            **generating.dict(),
            "state": GameState.ABANDONED,
            "terminal_disposition": TerminalDisposition.ABANDONED,
            "updated_at": timestamp,
            "completed_at": timestamp,
        }
    )
    success = RoundRecord(
        **{
            **generating.dict(),
            "state": GameState.GENERATED_REVEAL,
            "updated_at": timestamp,
        }
    )
    claim = AttemptClaim(
        attempt_token="attempt",
        owner_instance="worker",
        claimed_at=timestamp,
        lease_expires_at=(clock.current.replace(second=30)).isoformat(),
    )
    repository.create(generating)

    for success_first in (False, True):
        repository.replace_unsafe(generating)
        claims.claim(generating.id, claim, timestamp)

        if success_first:
            claims.replace_round_and_release(
                success,
                claim.attempt_token,
                timestamp,
                expected=generating,
            )
            with pytest.raises(RoundSnapshotConflictError):
                claims.replace_round_and_clear_claim(abandoned, expected=generating)
            assert repository.get(generating.id).state is GameState.GENERATED_REVEAL
        else:
            claims.replace_round_and_clear_claim(abandoned, expected=generating)
            with pytest.raises(RoundSnapshotConflictError):
                claims.replace_round_and_release(
                    success,
                    claim.attempt_token,
                    timestamp,
                    expected=generating,
                )
            assert repository.get(generating.id).state is GameState.ABANDONED
