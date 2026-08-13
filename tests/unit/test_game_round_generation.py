from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import get_ident

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.ai.pipeline import FakeAIPipeline
from app.ai.results import AIPipelineResult
from app.content.repository import ChallengeCatalog
from app.domain.models import (
    ChallengeSpec,
    FailureDetail,
    GameState,
    ImageArtifact,
    ImageMatchEvaluation,
    LevelGroup,
    PipelineResultStatus,
    PromptEvaluation,
    PromptSubmissionReason,
    RoundRecord,
    ScoreResult,
    TerminalDisposition,
)
from app.domain.scoring import score_total
from app.persistence import (
    GenerationAlreadyRunningError,
    ShelfDbGenerationClaims,
    ShelfDbRoundRepository,
)
from app.services import GameRoundConflictError, GameRoundService, GameRoundValidationError


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


class RecordingRepository:
    def __init__(self, repository: ShelfDbRoundRepository) -> None:
        self.repository = repository
        self.calls: list[tuple[str, int]] = []

    def create(self, record: RoundRecord) -> None:
        self.calls.append(("create", get_ident()))
        self.repository.create(record)

    def get(self, round_id: str) -> RoundRecord:
        self.calls.append(("get", get_ident()))
        return self.repository.get(round_id)

    def replace(self, record: RoundRecord) -> None:
        self.calls.append(("replace", get_ident()))
        self.repository.replace(record)


class RecordingClaims:
    def __init__(self, claims: ShelfDbGenerationClaims) -> None:
        self.claims = claims
        self.calls: list[tuple[str, int]] = []

    def claim(self, round_id, claim, now):
        self.calls.append(("claim", get_ident()))
        return self.claims.claim(round_id, claim, now)

    def replace_round_and_release(self, record, attempt_token):
        self.calls.append(("replace_round_and_release", get_ident()))
        return self.claims.replace_round_and_release(record, attempt_token)

    def replace_round_and_clear_claim(self, record):
        self.calls.append(("replace_round_and_clear_claim", get_ident()))
        return self.claims.replace_round_and_clear_claim(record)


class RetryPipeline:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, challenge: ChallengeSpec, prompt: str, timeout: float) -> AIPipelineResult:
        self.calls += 1
        assert prompt == "เด็กวาดภาพในสวน"
        assert timeout == 10.0
        if self.calls == 1:
            return failure_result()
        return success_result(challenge)


class BlockingPipeline:
    def __init__(self, expected_timeout: float = 10.0) -> None:
        self.calls = 0
        self.expected_timeout = expected_timeout
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, challenge: ChallengeSpec, prompt: str, timeout: float) -> AIPipelineResult:
        self.calls += 1
        assert prompt == "เด็กวาดภาพในสวน"
        assert timeout == self.expected_timeout
        self.started.set()
        await self.release.wait()
        return success_result(challenge)


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


def success_result(challenge: ChallengeSpec) -> AIPipelineResult:
    prompt_evaluation = PromptEvaluation(
        clarity=80,
        specificity=70,
        relationship=60,
        consistency=90,
    )
    image_evaluation = ImageMatchEvaluation(
        core_concept=85,
        supporting_details=75,
        scene_coherence=95,
    )
    return AIPipelineResult(
        status=PipelineResultStatus.SUCCESS,
        artifact=ImageArtifact(url=challenge.target_asset_url, provider="test-ai"),
        prompt_evaluation=prompt_evaluation,
        image_evaluation=image_evaluation,
        score=score_total(prompt_evaluation, image_evaluation),
        feedback=["feedback one", "feedback two"],
    )


def failure_result() -> AIPipelineResult:
    return AIPipelineResult(
        status=PipelineResultStatus.ERROR,
        failure=FailureDetail(
            code="provider_timeout",
            message="การประมวลผลใช้เวลานานเกินไป",
            retryable=True,
            provider="test-ai",
        ),
    )


@pytest.fixture
def setup(tmp_path: Path):
    db = DB(str(tmp_path / "generation"))
    try:
        repository = ShelfDbRoundRepository(db)
        claims = ShelfDbGenerationClaims(db)
        yield repository, claims, MutableClock()
    finally:
        db.close()


def service_for(
    repository, claims, clock, pipeline=None, provider_timeout=10.0
) -> GameRoundService:
    return GameRoundService(
        repository,
        make_catalog(),
        lambda choices: choices[0],
        clock,
        generation_claims=claims,
        pipeline=pipeline if pipeline is not None else FakeAIPipeline(),
        owner_instance="test-worker",
        provider_timeout=provider_timeout,
    )


async def prepare_generating(service: GameRoundService) -> RoundRecord:
    created = await service.create_round("Tester")
    await service.configure_round(created.id, LevelGroup.P1_P3)
    await service.continue_challenge(created.id)
    return await service.submit_prompt(
        created.id,
        "เด็กวาดภาพในสวน",
        PromptSubmissionReason.MANUAL,
    )


@pytest.mark.asyncio
async def test_success_persists_exact_fields_and_five_second_reveal(setup) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    generating = await prepare_generating(service)

    generated = await service.generate_round(generating.id)

    assert generated.state is GameState.GENERATED_REVEAL
    assert generated.generated_artifact is not None
    assert generated.generated_artifact.url == generating.challenge_id.replace(
        "p1-p3-0", "/assets/challenges/p1-p3-0.webp"
    )
    assert generated.prompt_evaluation is not None
    assert generated.image_evaluation is not None
    assert generated.score == ScoreResult(prompt_score=74, image_score=84, total_score=79)
    assert generated.feedback == [
        "อธิบายภาพได้ชัดเจน",
        "เพิ่มรายละเอียดสำคัญได้ดี",
        "ลองตรวจความสัมพันธ์ของตัวละครกับฉากอีกครั้ง",
    ]
    assert generated.pipeline_failure is None
    assert generated.generated_at == clock.current.isoformat()
    assert generated.reveal_deadline == (clock.current + timedelta(seconds=5)).isoformat()
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_failure_remains_generating_and_retry_is_available(setup) -> None:
    repository, claims, clock = setup
    pipeline = RetryPipeline()
    service = service_for(repository, claims, clock, pipeline)
    generating = await prepare_generating(service)

    failed = await service.generate_round(generating.id)
    retried = await service.generate_round(generating.id)

    assert failed.state is GameState.GENERATING
    assert failed.pipeline_failure is not None
    assert failed.generated_artifact is None
    assert failed.prompt_evaluation is None
    assert failed.image_evaluation is None
    assert failed.score is None
    assert failed.feedback == []
    assert failed.completed_at is None
    assert retried.state is GameState.GENERATED_REVEAL
    assert pipeline.calls == 2
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_pipeline_timeout_is_a_retryable_failure_and_releases_claim(setup) -> None:
    repository, claims, clock = setup
    pipeline = BlockingPipeline(expected_timeout=0.01)
    service = service_for(repository, claims, clock, pipeline, provider_timeout=0.01)
    generating = await prepare_generating(service)

    timed_out = await service.generate_round(generating.id)
    retried = await service.generate_round(generating.id)

    assert timed_out.state is GameState.GENERATING
    assert timed_out.pipeline_failure is not None
    assert timed_out.pipeline_failure.code == "provider_timeout"
    assert timed_out.pipeline_failure.retryable is True
    assert timed_out.generated_artifact is None
    assert timed_out.score is None
    assert claims.get(generating.id) is None
    assert retried.state is GameState.GENERATING
    assert pipeline.calls == 2
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_concurrent_generation_claims_run_blocking_pipeline_once(setup) -> None:
    repository, claims, clock = setup
    pipeline = BlockingPipeline()
    service = service_for(repository, claims, clock, pipeline)
    generating = await prepare_generating(service)

    first = asyncio.create_task(service.generate_round(generating.id))
    await pipeline.started.wait()
    second = asyncio.create_task(service.generate_round(generating.id))

    with pytest.raises(GenerationAlreadyRunningError):
        await second

    pipeline.release.set()
    generated = await first

    assert generated.state is GameState.GENERATED_REVEAL
    assert pipeline.calls == 1


@pytest.mark.asyncio
async def test_abandon_generation_without_attempt_is_terminal_and_unclaimed(setup) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    generating = await prepare_generating(service)

    abandoned = await service.abandon_generation(generating.id)

    assert abandoned.state is GameState.ABANDONED
    assert abandoned.terminal_disposition is TerminalDisposition.ABANDONED
    assert abandoned.updated_at == clock.current.isoformat()
    assert abandoned.completed_at == clock.current.isoformat()
    assert abandoned.generated_artifact is None
    assert abandoned.prompt_evaluation is None
    assert abandoned.image_evaluation is None
    assert abandoned.score is None
    assert abandoned.feedback == []
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_abandon_during_attempt_fences_late_success(setup) -> None:
    repository, claims, clock = setup
    pipeline = BlockingPipeline()
    service = service_for(repository, claims, clock, pipeline)
    generating = await prepare_generating(service)

    attempt = asyncio.create_task(service.generate_round(generating.id))
    await pipeline.started.wait()
    abandoned = await service.abandon_generation(generating.id)
    pipeline.release.set()

    with pytest.raises(GameRoundConflictError):
        await attempt

    stored = await service.get_round(generating.id)
    assert abandoned.state is GameState.ABANDONED
    assert stored.dict() == abandoned.dict()
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_repository_and_claims_operations_run_off_event_loop_thread(setup) -> None:
    repository, claims, clock = setup
    recording_repository = RecordingRepository(repository)
    recording_claims = RecordingClaims(claims)
    service = service_for(recording_repository, recording_claims, clock)
    event_loop_thread = get_ident()

    generating = await prepare_generating(service)
    await service.generate_round(generating.id)
    await service.get_round(generating.id)

    assert [name for name, _thread in recording_claims.calls] == [
        "claim",
        "replace_round_and_release",
    ]
    assert all(thread != event_loop_thread for _name, thread in recording_repository.calls)
    assert all(thread != event_loop_thread for _name, thread in recording_claims.calls)


@pytest.mark.asyncio
async def test_invalid_state_or_context_leaves_persistence_unchanged(setup) -> None:
    repository, claims, clock = setup
    service = service_for(repository, claims, clock)
    created = await service.create_round("Tester")
    before = (await service.get_round(created.id)).dict()

    with pytest.raises(GameRoundConflictError):
        await service.generate_round(created.id)
    assert (await service.get_round(created.id)).dict() == before
    assert claims.get(created.id) is None

    invalid_context = RoundRecord(
        **{
            **created.dict(),
            "state": GameState.GENERATING,
        }
    )
    repository.replace(invalid_context)
    with pytest.raises(GameRoundValidationError):
        await service.generate_round(created.id)
    assert (await service.get_round(created.id)).dict() == invalid_context.dict()
    assert claims.get(created.id) is None


@pytest.mark.asyncio
async def test_setup_only_service_rejects_generation_without_mutation(setup) -> None:
    repository, _claims, clock = setup
    service = GameRoundService(repository, make_catalog(), lambda choices: choices[0], clock)
    created = await service.create_round("Tester")
    before = (await service.get_round(created.id)).dict()

    with pytest.raises(GameRoundValidationError, match="dependencies"):
        await service.generate_round(created.id)
    with pytest.raises(GameRoundValidationError, match="claims"):
        await service.abandon_generation(created.id)

    assert (await service.get_round(created.id)).dict() == before


def test_claim_lease_must_outlast_provider_timeout(setup) -> None:
    repository, claims, clock = setup

    with pytest.raises(GameRoundValidationError, match="longer"):
        GameRoundService(
            repository,
            make_catalog(),
            lambda choices: choices[0],
            clock,
            generation_claims=claims,
            pipeline=FakeAIPipeline(),
            owner_instance="test-worker",
            claim_lease_duration=timedelta(seconds=10),
            provider_timeout=10,
        )
