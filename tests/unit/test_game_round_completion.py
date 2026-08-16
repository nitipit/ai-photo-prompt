from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.ai.pipeline import FakeAIPipeline
from app.content.repository import ChallengeCatalog
from app.domain.models import (
    ChallengeSpec,
    GameState,
    ImageArtifact,
    ImageMatchEvaluation,
    LevelGroup,
    PromptEvaluation,
    PromptSubmissionReason,
    RoundRecord,
    ScoreResult,
    TerminalDisposition,
)
from app.persistence import ShelfDbGenerationClaims, ShelfDbRoundRepository
from app.services import (
    GameRoundConflictError,
    GameRoundDeadlineError,
    GameRoundService,
    GameRoundValidationError,
)


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


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
    db = DB(str(tmp_path / "completion"))
    try:
        repository = ShelfDbRoundRepository(db)
        clock = MutableClock()
        yield (
            repository,
            ShelfDbGenerationClaims(
                db,
                clock,
                timedelta(seconds=30),
            ),
            clock,
        )
    finally:
        db.close()


def service_for(repository, claims, clock) -> GameRoundService:
    return GameRoundService(
        repository,
        make_catalog(),
        lambda choices: choices[0],
        clock,
        generation_claims=claims,
        pipeline=FakeAIPipeline(),
        owner_instance="completion-test",
    )


async def prepare_generated(service: GameRoundService) -> RoundRecord:
    created = await service.create_round("  Current Player  ")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    await service.continue_challenge(created.id)
    await service.submit_prompt(
        created.id,
        "  full raw prompt  ",
        PromptSubmissionReason.MANUAL,
    )
    return await service.generate_round(created.id)


def make_completed(
    *,
    round_id: str | None = None,
    level: LevelGroup = LevelGroup.P1_P3,
    score: int | float = 82,
    name: str = "Player",
    prompt: str = "  full raw prompt  ",
    completed_at: str = "2026-01-01T00:00:00+00:00",
    state: GameState = GameState.LEADERBOARD,
    disposition: TerminalDisposition | None = TerminalDisposition.COMPLETED,
) -> RoundRecord:
    return RoundRecord(
        id=round_id or str(uuid4()),
        state=state,
        display_name=name,
        level=level,
        challenge_id=f"{level.value}-0",
        prompt=prompt,
        prompt_submission_reason=PromptSubmissionReason.MANUAL,
        generated_artifact=ImageArtifact(url=f"/generated/{name}.webp"),
        prompt_evaluation=PromptEvaluation(
            clarity=80,
            specificity=80,
            relationship=80,
            consistency=80,
        ),
        image_evaluation=ImageMatchEvaluation(
            core_concept=80,
            supporting_details=80,
            scene_coherence=80,
        ),
        score=ScoreResult(prompt_score=score, image_score=score, total_score=score),
        feedback=["feedback one", "feedback two"],
        terminal_disposition=disposition,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at=completed_at,
        reveal_deadline="2026-01-01T00:00:05+00:00",
        generated_at="2026-01-01T00:00:00+00:00",
        leaderboard_deadline=(
            datetime.fromisoformat(completed_at) + timedelta(seconds=15)
        ).isoformat()
        if disposition is TerminalDisposition.COMPLETED
        else None,
        completed_at=completed_at if disposition is not None else None,
    )


@pytest.mark.asyncio
async def test_show_result_requires_elapsed_deadline_without_mutation(setup) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    generated = await prepare_generated(service)
    before = generated.dict()

    with pytest.raises(GameRoundDeadlineError):
        await service.show_result(generated.id)

    assert (await service.get_round(generated.id)).dict() == before

    missing_deadline = RoundRecord(**{**generated.dict(), "reveal_deadline": None})
    repository.replace(missing_deadline)
    with pytest.raises(GameRoundDeadlineError):
        await service.show_result(generated.id)
    assert (await service.get_round(generated.id)).dict() == missing_deadline.dict()
    repository.replace(generated)

    clock.current = datetime.fromisoformat(generated.reveal_deadline)
    result = await service.show_result(generated.id)

    assert result.state is GameState.RESULT
    assert result.updated_at == clock.current.isoformat()
    assert result.generated_artifact.dict() == generated.generated_artifact.dict()
    assert result.score.dict() == generated.score.dict()
    assert result.feedback == generated.feedback

    with pytest.raises(GameRoundConflictError):
        await service.show_result(generated.id)


@pytest.mark.asyncio
async def test_completion_sets_terminal_fields_and_preserves_result(setup) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    generated = await prepare_generated(service)
    clock.current = datetime.fromisoformat(generated.reveal_deadline)
    result = await service.show_result(generated.id)
    clock.current += timedelta(seconds=2)

    completed = await service.complete_round(result.id)

    assert completed.state is GameState.LEADERBOARD
    assert completed.terminal_disposition is TerminalDisposition.COMPLETED
    assert completed.completed_at == clock.current.isoformat()
    assert completed.updated_at == clock.current.isoformat()
    assert completed.leaderboard_deadline == (clock.current + timedelta(seconds=15)).isoformat()
    assert completed.prompt == result.prompt
    assert completed.generated_artifact.dict() == result.generated_artifact.dict()
    assert completed.score.dict() == result.score.dict()
    assert completed.feedback == result.feedback

    with pytest.raises(GameRoundConflictError):
        await service.complete_round(completed.id)


@pytest.mark.asyncio
async def test_missing_result_data_and_complete_context_do_not_mutate(setup) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    generated = await prepare_generated(service)
    invalid_generated = RoundRecord(**{**generated.dict(), "score": None, "feedback": []})
    repository.replace(invalid_generated)

    with pytest.raises(GameRoundValidationError):
        await service.show_result(generated.id)
    assert (await service.get_round(generated.id)).dict() == invalid_generated.dict()

    valid_generated = await prepare_generated(service)
    clock.current = datetime.fromisoformat(valid_generated.reveal_deadline)
    result = await service.show_result(valid_generated.id)
    invalid_result = RoundRecord(**{**result.dict(), "prompt": "   "})
    repository.replace(invalid_result)

    with pytest.raises(GameRoundValidationError):
        await service.complete_round(result.id)
    assert (await service.get_round(result.id)).dict() == invalid_result.dict()


@pytest.mark.asyncio
async def test_leaderboard_projects_current_level_competition_rank_and_full_fields(
    setup,
) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 7)]
    current = make_completed(
        round_id=ids[1], score=82, name="Current", completed_at="2026-01-01T00:00:02+00:00"
    )
    rows = [
        make_completed(round_id=ids[0], score=96, name="Top"),
        current,
        make_completed(
            round_id=ids[2], score=82, name="Tie", completed_at="2026-01-01T00:00:02+00:00"
        ),
        make_completed(round_id=ids[3], score=74, name="Bottom"),
        make_completed(round_id=ids[4], level=LevelGroup.P4_P6, score=100, name="Other level"),
        make_completed(
            round_id=ids[5],
            score=100,
            name="Abandoned",
            state=GameState.ABANDONED,
            disposition=TerminalDisposition.ABANDONED,
        ),
    ]
    for row in rows:
        repository.create(row)

    projection = await service.get_leaderboard(current.id)

    assert [entry.score for entry in projection.entries] == [96, 82, 82, 74]
    assert [entry.rank for entry in projection.entries] == [1, 2, 2, 4]
    assert projection.current_rank == 2
    assert [entry.round_id for entry in projection.entries] == ids[:4]
    assert [entry.is_current for entry in projection.entries].count(True) == 1
    assert projection.entries[1].is_current is True
    assert projection.entries[1].name == "Current"
    assert projection.entries[1].prompt == "  full raw prompt  "
    assert projection.entries[1].generated_image.url == "/generated/Current.webp"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_index", "expected_indexes"),
    (
        (0, (0, 1, 2, 3)),
        (3, (0, 1, 2, 3)),
        (4, (2, 3, 4, 5)),
        (7, (4, 5, 6, 7)),
    ),
)
async def test_leaderboard_window_is_bounded_and_contains_current_entry(
    setup, current_index: int, expected_indexes: tuple[int, ...]
) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 9)]
    rows = [
        make_completed(
            round_id=round_id,
            score=100 - index,
            name=f"Player {index}",
        )
        for index, round_id in enumerate(ids)
    ]
    current = rows[current_index]
    for row in rows:
        repository.create(row)

    projection = await service.get_leaderboard(current.id)

    assert len(projection.entries) == 4
    assert [entry.round_id for entry in projection.entries] == [ids[i] for i in expected_indexes]
    assert sum(entry.is_current for entry in projection.entries) == 1
    assert projection.current_rank == current_index + 1


@pytest.mark.asyncio
async def test_leaderboard_window_preserves_competition_ranks_across_ties(setup) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 7)]
    scores = (100, 90, 90, 80, 80, 70)
    rows = [
        make_completed(round_id=round_id, score=score, name=f"Player {index}")
        for index, (round_id, score) in enumerate(zip(ids, scores, strict=True))
    ]
    current = rows[4]
    for row in rows:
        repository.create(row)

    projection = await service.get_leaderboard(current.id)

    assert [entry.round_id for entry in projection.entries] == ids[2:]
    assert [entry.rank for entry in projection.entries] == [2, 4, 4, 6]
    assert projection.current_rank == 4
    assert projection.entries[2].is_current is True


@pytest.mark.asyncio
async def test_malformed_completed_row_rejects_projection_instead_of_partial_data(
    setup,
) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    current = make_completed(score=96)
    malformed = RoundRecord(**{**make_completed(score=82).dict(), "score": None})
    repository.create(current)
    repository.create(malformed)

    with pytest.raises(GameRoundValidationError):
        await service.get_leaderboard(current.id)


@pytest.mark.asyncio
async def test_leaderboard_reads_get_and_list_as_worker_thread_units(setup) -> None:
    repository, claims, clock = setup
    current = make_completed()
    repository.create(current)
    calls: list[tuple[str, int]] = []

    class RecordingRepository:
        def get(self, round_id: str) -> RoundRecord:
            from threading import get_ident

            calls.append(("get", get_ident()))
            return repository.get(round_id)

        def list_completed(self, level: LevelGroup) -> list[RoundRecord]:
            from threading import get_ident

            calls.append(("list_completed", get_ident()))
            return repository.list_completed(level)

    service = GameRoundService(
        RecordingRepository(),
        make_catalog(),
        lambda choices: choices[0],
        clock,
    )
    from threading import get_ident

    event_loop_thread = get_ident()
    await service.get_leaderboard(current.id)

    assert [name for name, _thread in calls] == ["get", "list_completed"]
    assert all(thread != event_loop_thread for _name, thread in calls)
