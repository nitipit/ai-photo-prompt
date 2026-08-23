from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import get_ident

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.content.repository import ChallengeCatalog
from app.domain.models import (
    ChallengeSpec,
    GameState,
    LevelGroup,
    PromptSubmissionReason,
    TerminalDisposition,
)
from app.persistence import ShelfDbRoundRepository
from app.services.game_round import (
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


class RecordingRepository:
    def __init__(self, repository: ShelfDbRoundRepository) -> None:
        self.repository = repository
        self.calls: list[tuple[str, int]] = []

    def create(self, record) -> None:
        self.calls.append(("create", get_ident()))
        self.repository.create(record)

    def get(self, round_id: str):
        self.calls.append(("get", get_ident()))
        return self.repository.get(round_id)

    def replace(self, record) -> None:
        self.calls.append(("replace", get_ident()))
        self.repository.replace(record)

    def replace_if_current(self, record, expected) -> None:
        self.calls.append(("replace_if_current", get_ident()))
        self.repository.replace_if_current(record, expected)


def make_catalog() -> ChallengeCatalog:
    challenges = []
    for level in LevelGroup:
        for index in range(5):
            challenges.append(
                ChallengeSpec(
                    id=f"{level.value}-{index}",
                    title=f"Challenge {level.value} {index}",
                    level=level,
                    target_asset_url=f"/assets/challenges/{level.value}-{index}.webp",
                    concept=f"concept {index}",
                    core_anchors=["anchor"],
                    optional_details=[],
                    example_prompt="example prompt",
                    evaluation_notes="notes",
                    feedback_focus="focus",
                )
            )
    return ChallengeCatalog(challenges)


@pytest.fixture
def setup(tmp_path: Path):
    db = DB(str(tmp_path / "rounds"))
    try:
        repository = ShelfDbRoundRepository(db)
        clock = MutableClock()
        yield repository, clock
    finally:
        db.close()


def service_for(
    repository: ShelfDbRoundRepository | RecordingRepository,
    clock: MutableClock,
    selector=None,
) -> GameRoundService:
    return GameRoundService(
        repository,
        make_catalog(),
        selector or (lambda choices: choices[0]),
        clock,
    )


@pytest.mark.asyncio
async def test_create_round_requires_normalized_name_and_enforces_limit(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)

    named = await service.create_round("  น้องมะลิ (ป.3)  ")
    longest = await service.create_round("x" * 50)

    assert named.display_name == "น้องมะลิ (ป.3)"
    assert named.state is GameState.LEVEL_SELECTION
    assert named.created_at == named.updated_at
    assert longest.display_name == "x" * 50

    for missing_name in ("", "   "):
        with pytest.raises(GameRoundValidationError, match="display name is required"):
            await service.create_round(missing_name)

    with pytest.raises(GameRoundValidationError, match="at most 50 characters"):
        await service.create_round("x" * 51)


@pytest.mark.asyncio
async def test_configure_uses_injected_selection_and_persists_level_and_challenge(setup) -> None:
    repository, clock = setup
    selected: list[tuple[str, ...]] = []

    def choose(challenges):
        selected.append(tuple(challenge.id for challenge in challenges))
        return challenges[2]

    service = service_for(repository, clock, choose)
    created = await service.create_round("Tester")
    configured = await service.configure_round(created.id, LevelGroup.P4_P6)

    assert selected == [tuple(f"p4-p6-{index}" for index in range(5))]
    assert configured.level is LevelGroup.P4_P6
    assert configured.challenge_id == "p4-p6-2"
    assert configured.state is GameState.CHALLENGE_REVEAL
    assert (await service.get_round(created.id)).dict() == configured.dict()


@pytest.mark.asyncio
async def test_continue_challenge_sets_prompt_entry_and_ninety_second_deadline(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    configured = await service.configure_round(created.id, LevelGroup.P1_P3)

    continued = await service.continue_challenge(created.id)

    expected_deadline = (clock.current + timedelta(seconds=90)).isoformat()
    assert continued.state is GameState.PROMPT_ENTRY
    assert continued.prompt_deadline == expected_deadline
    assert continued.created_at == configured.created_at
    assert continued.updated_at == clock.current.isoformat()


@pytest.mark.asyncio
async def test_manual_prompt_preserves_text_and_reason(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    await service.continue_challenge(created.id)

    submitted = await service.submit_prompt(
        created.id,
        "  กระต่ายสีฟ้าในสวน  ",
        PromptSubmissionReason.MANUAL,
    )

    assert submitted.state is GameState.GENERATING
    assert submitted.prompt == "  กระต่ายสีฟ้าในสวน  "
    assert submitted.prompt_submission_reason is PromptSubmissionReason.MANUAL


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_seconds", [-1, 0, 1], ids=["before", "exact", "after"])
@pytest.mark.parametrize(
    "reason",
    [PromptSubmissionReason.MANUAL, PromptSubmissionReason.TIMEOUT],
    ids=["manual", "timeout"],
)
@pytest.mark.parametrize("prompt", ["  ", "  timed prompt  "], ids=["blank", "nonblank"])
async def test_prompt_deadline_authorizes_reason_and_blankness(
    setup,
    offset_seconds: int,
    reason: PromptSubmissionReason,
    prompt: str,
) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    continued = await service.continue_challenge(created.id)
    assert continued.prompt_deadline is not None
    before = (await service.get_round(created.id)).dict()
    clock.current = datetime.fromisoformat(continued.prompt_deadline) + timedelta(
        seconds=offset_seconds
    )

    if offset_seconds < 0 and reason is PromptSubmissionReason.MANUAL and prompt.strip():
        submitted = await service.submit_prompt(created.id, prompt, reason)
        assert submitted.state is GameState.GENERATING
        assert submitted.prompt == prompt
        assert submitted.prompt_submission_reason is PromptSubmissionReason.MANUAL
        return

    if offset_seconds < 0:
        expected_error = (
            GameRoundValidationError
            if reason is PromptSubmissionReason.MANUAL
            else GameRoundDeadlineError
        )
        with pytest.raises(expected_error):
            await service.submit_prompt(created.id, prompt, reason)
        assert (await service.get_round(created.id)).dict() == before
        return

    submitted = await service.submit_prompt(created.id, prompt, reason)
    if prompt.strip():
        assert submitted.state is GameState.GENERATING
        assert submitted.prompt == prompt
        assert submitted.prompt_submission_reason is PromptSubmissionReason.TIMEOUT
    else:
        assert submitted.state is GameState.ABANDONED
        assert submitted.terminal_disposition is TerminalDisposition.ABANDONED
        assert submitted.prompt is None
        assert submitted.prompt_submission_reason is None


@pytest.mark.asyncio
async def test_timeout_nonblank_submits_after_authoritative_deadline(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    continued = await service.continue_challenge(created.id)
    assert continued.prompt_deadline is not None
    clock.current = datetime.fromisoformat(continued.prompt_deadline)

    submitted = await service.submit_prompt(
        created.id,
        "  timed prompt  ",
        PromptSubmissionReason.TIMEOUT,
    )

    assert submitted.state is GameState.GENERATING
    assert submitted.prompt == "  timed prompt  "
    assert submitted.prompt_submission_reason is PromptSubmissionReason.TIMEOUT


@pytest.mark.asyncio
async def test_blank_timeout_abandons_terminal_round_without_score(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    continued = await service.continue_challenge(created.id)
    assert continued.prompt_deadline is not None
    clock.current = datetime.fromisoformat(continued.prompt_deadline)

    abandoned = await service.submit_prompt(created.id, "  ", PromptSubmissionReason.TIMEOUT)

    assert abandoned.state is GameState.ABANDONED
    assert abandoned.terminal_disposition is TerminalDisposition.ABANDONED
    assert abandoned.completed_at == clock.current.isoformat()
    assert abandoned.updated_at == clock.current.isoformat()
    assert abandoned.score is None
    assert abandoned.prompt is None
    assert abandoned.prompt_submission_reason is None


@pytest.mark.asyncio
async def test_blank_manual_and_early_timeout_leave_stored_mapping_unchanged(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    continued = await service.continue_challenge(created.id)
    before = (await service.get_round(created.id)).dict()

    with pytest.raises(GameRoundValidationError):
        await service.submit_prompt(created.id, " \t", PromptSubmissionReason.MANUAL)
    assert (await service.get_round(created.id)).dict() == before

    with pytest.raises(GameRoundDeadlineError):
        await service.submit_prompt(created.id, " \t", PromptSubmissionReason.TIMEOUT)
    assert (await service.get_round(created.id)).dict() == before
    assert continued.state is GameState.PROMPT_ENTRY


@pytest.mark.asyncio
async def test_missing_prompt_deadline_rejects_without_mutation(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    continued = await service.continue_challenge(created.id)
    missing_deadline = continued.dict()
    missing_deadline["prompt_deadline"] = None
    repository.replace(type(continued)(missing_deadline))
    before = (await service.get_round(created.id)).dict()

    with pytest.raises(GameRoundDeadlineError, match="no prompt deadline"):
        await service.submit_prompt(created.id, "valid", PromptSubmissionReason.MANUAL)

    assert (await service.get_round(created.id)).dict() == before


@pytest.mark.asyncio
async def test_invalid_state_calls_leave_stored_mapping_unchanged(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    before = (await service.get_round(created.id)).dict()

    with pytest.raises(GameRoundConflictError):
        await service.continue_challenge(created.id)
    with pytest.raises(GameRoundConflictError):
        await service.submit_prompt(created.id, "valid", PromptSubmissionReason.MANUAL)

    assert (await service.get_round(created.id)).dict() == before


@pytest.mark.asyncio
async def test_repository_operations_run_off_event_loop_thread(setup) -> None:
    repository, clock = setup
    recording = RecordingRepository(repository)
    service = service_for(recording, clock)
    event_loop_thread = get_ident()

    created = await service.create_round("Tester")
    configured = await service.configure_round(created.id, LevelGroup.P1_P3)
    await service.continue_challenge(configured.id)
    await service.get_round(created.id)

    assert [name for name, _thread in recording.calls] == [
        "create",
        "get",
        "replace_if_current",
        "get",
        "replace_if_current",
        "get",
    ]
    assert all(thread != event_loop_thread for _name, thread in recording.calls)


@pytest.mark.asyncio
async def test_input_limits_are_rejected_without_mutation(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    await service.continue_challenge(created.id)
    before = (await service.get_round(created.id)).dict()

    with pytest.raises(GameRoundValidationError):
        await service.submit_prompt(created.id, "p" * 1001, PromptSubmissionReason.MANUAL)
    assert (await service.get_round(created.id)).dict() == before


@pytest.mark.asyncio
async def test_completed_created_timestamp_is_preserved_on_every_mutation(setup) -> None:
    repository, clock = setup
    service = service_for(repository, clock)
    created = await service.create_round("Tester")

    clock.current += timedelta(seconds=1)
    configured = await service.configure_round(created.id, LevelGroup.P1_P3)
    clock.current += timedelta(seconds=1)
    continued = await service.continue_challenge(created.id)

    assert configured.created_at == created.created_at
    assert continued.created_at == created.created_at
    assert continued.updated_at != created.updated_at
    assert continued.updated_at == clock.current.isoformat()
