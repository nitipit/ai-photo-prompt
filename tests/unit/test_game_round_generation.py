from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.ai.pipeline import FakeAIPipeline
from app.ai.results import AIPipelineResult
from app.content.repository import ChallengeCatalog
from app.domain.models import (
    AttemptClaim,
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

    def replace_if_current(self, record: RoundRecord, expected: RoundRecord) -> None:
        self.calls.append(("replace_if_current", get_ident()))
        self.repository.replace_if_current(record, expected)


class RecordingClaims:
    def __init__(self, claims: ShelfDbGenerationClaims) -> None:
        self.claims = claims
        self.calls: list[tuple[str, int]] = []

    def claim(self, round_id, claim, now):
        self.calls.append(("claim", get_ident()))
        return self.claims.claim(round_id, claim, now)

    def get(self, round_id):
        self.calls.append(("get", get_ident()))
        return self.claims.get(round_id)

    def renew(self, round_id, attempt_token, lease_expires_at, now):
        self.calls.append(("renew", get_ident()))
        return self.claims.renew(round_id, attempt_token, lease_expires_at, now)

    def replace_round_and_release(self, record, attempt_token, now, *, expected=None):
        self.calls.append(("replace_round_and_release", get_ident()))
        return self.claims.replace_round_and_release(
            record,
            attempt_token,
            now,
            expected=expected,
        )

    def release_matching(self, round_id, attempt_token):
        self.calls.append(("release_matching", get_ident()))
        return self.claims.release_matching(round_id, attempt_token)

    def replace_round_and_clear_claim(self, record, *, expected=None):
        self.calls.append(("replace_round_and_clear_claim", get_ident()))
        return self.claims.replace_round_and_clear_claim(record, expected=expected)


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


class RaisingPipeline:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, challenge: ChallengeSpec, prompt: str, timeout: float) -> AIPipelineResult:
        del challenge, prompt, timeout
        self.calls += 1
        raise RuntimeError("provider secret should not be persisted")


class StaticPipeline:
    def __init__(self, result: object) -> None:
        self.calls = 0
        self.result = result

    async def run(self, challenge: ChallengeSpec, prompt: str, timeout: float) -> object:
        del challenge, prompt, timeout
        self.calls += 1
        return self.result


class BlockingPipeline:
    def __init__(
        self,
        expected_timeout: float = 10.0,
        result: AIPipelineResult | None = None,
    ) -> None:
        self.calls = 0
        self.expected_timeout = expected_timeout
        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, challenge: ChallengeSpec, prompt: str, timeout: float) -> AIPipelineResult:
        self.calls += 1
        assert prompt == "เด็กวาดภาพในสวน"
        assert timeout == self.expected_timeout
        self.started.set()
        await self.release.wait()
        return self.result if self.result is not None else success_result(challenge)


class DelayedClaimBoundary:
    def __init__(self, claims: ShelfDbGenerationClaims) -> None:
        self.claims = claims
        self.claim_started = Event()
        self.allow_claim = Event()
        self.claim_finished = Event()
        self.delay_release = False
        self.release_started = Event()
        self.allow_release = Event()

    def claim(self, round_id, claim, now):
        self.claim_started.set()
        self.allow_claim.wait(timeout=5)
        try:
            return self.claims.claim(round_id, claim, now)
        finally:
            self.claim_finished.set()

    def get(self, round_id):
        return self.claims.get(round_id)

    def release_matching(self, round_id, attempt_token):
        self.release_started.set()
        if self.delay_release:
            self.allow_release.wait(timeout=5)
        return self.claims.release_matching(round_id, attempt_token)

    def replace_round_and_release(self, record, attempt_token, now, *, expected=None):
        return self.claims.replace_round_and_release(
            record,
            attempt_token,
            now,
            expected=expected,
        )

    def replace_round_and_clear_claim(self, record, *, expected=None):
        return self.claims.replace_round_and_clear_claim(record, expected=expected)


class HeartbeatClaims(RecordingClaims):
    def __init__(self, claims: ShelfDbGenerationClaims) -> None:
        super().__init__(claims)
        self.renew_calls: list[tuple[str, str, str, str]] = []
        self.renew_started = Event()
        self.renew_finished = Event()
        self.allow_renew = Event()
        self.block_renew = False
        self.fail_renew = False

    def renew(self, round_id, attempt_token, lease_expires_at, now):
        self.renew_calls.append((round_id, attempt_token, lease_expires_at, now))
        self.renew_started.set()
        if self.block_renew:
            self.allow_renew.wait(timeout=5)
        self.renew_finished.set()
        if self.fail_renew:
            raise RuntimeError("renewal lost")
        return self.claims.renew(round_id, attempt_token, lease_expires_at, now)


class AdvancingHeartbeatClaims(HeartbeatClaims):
    def __init__(self, claims: ShelfDbGenerationClaims, clock: MutableClock) -> None:
        super().__init__(claims)
        self.clock = clock

    def renew(self, round_id, attempt_token, lease_expires_at, now):
        renewed = super().renew(round_id, attempt_token, lease_expires_at, now)
        self.clock.current += timedelta(seconds=25)
        return renewed


class ReplacementOnRenewClaims(HeartbeatClaims):
    def __init__(self, claims: ShelfDbGenerationClaims, clock: MutableClock) -> None:
        super().__init__(claims)
        self.clock = clock

    def renew(self, round_id, attempt_token, lease_expires_at, now):
        self.renew_calls.append((round_id, attempt_token, lease_expires_at, now))
        self.renew_started.set()
        self.clock.current += timedelta(seconds=31)
        replacement_now = self.clock.current.isoformat()
        replacement = AttemptClaim(
            attempt_token="replacement-token",
            owner_instance="replacement-worker",
            claimed_at=replacement_now,
            lease_expires_at=(self.clock.current + timedelta(seconds=30)).isoformat(),
        )
        self.claims.claim(round_id, replacement, replacement_now)
        self.renew_finished.set()
        raise RuntimeError("renewal replaced")


class CancellablePipeline(BlockingPipeline):
    def __init__(self, expected_timeout: float = 10.0) -> None:
        super().__init__(expected_timeout=expected_timeout)
        self.cancelled = Event()

    async def run(self, challenge: ChallengeSpec, prompt: str, timeout: float) -> AIPipelineResult:
        self.calls += 1
        assert prompt == "เด็กวาดภาพในสวน"
        assert timeout == self.expected_timeout
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
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
    repository,
    claims,
    clock,
    pipeline=None,
    provider_timeout=10.0,
    claim_lease_duration=timedelta(seconds=30),
    claim_heartbeat_interval=timedelta(seconds=5),
) -> GameRoundService:
    return GameRoundService(
        repository,
        make_catalog(),
        lambda choices: choices[0],
        clock,
        generation_claims=claims,
        pipeline=pipeline if pipeline is not None else FakeAIPipeline(),
        owner_instance="test-worker",
        claim_lease_duration=claim_lease_duration,
        claim_heartbeat_interval=claim_heartbeat_interval,
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
async def test_provider_exception_is_safe_retryable_failure_and_releases_claim(setup) -> None:
    repository, claims, clock = setup
    pipeline = RaisingPipeline()
    service = service_for(repository, claims, clock, pipeline)
    generating = await prepare_generating(service)

    failed = await service.generate_round(generating.id)
    retried = await service.generate_round(generating.id)

    for result in (failed, retried):
        assert result.state is GameState.GENERATING
        assert result.pipeline_failure is not None
        assert result.pipeline_failure.code == "provider_error"
        assert result.pipeline_failure.retryable is True
        assert "provider secret" not in result.pipeline_failure.message
        assert result.generated_artifact is None
        assert result.score is None
    assert pipeline.calls == 2
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [None, object(), SimpleNamespace(status=PipelineResultStatus.SUCCESS)],
    ids=["none", "wrong-type", "malformed-envelope"],
)
async def test_invalid_pipeline_return_is_safe_retryable_failure_and_retryable(
    setup,
    malformed: object,
) -> None:
    repository, claims, clock = setup
    pipeline = StaticPipeline(malformed)
    service = service_for(repository, claims, clock, pipeline)
    generating = await prepare_generating(service)

    failed = await service.generate_round(generating.id)
    retried = await service.generate_round(generating.id)

    for result in (failed, retried):
        assert result.state is GameState.GENERATING
        assert result.pipeline_failure is not None
        assert result.pipeline_failure.code == "invalid_pipeline_result"
        assert result.pipeline_failure.retryable is True
        assert result.generated_artifact is None
        assert result.score is None
    assert pipeline.calls == 2
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_incomplete_pipeline_envelopes_are_persisted_as_safe_failures(setup) -> None:
    repository, claims, clock = setup
    generating = await prepare_generating(service_for(repository, claims, clock))
    challenge = make_catalog().get(generating.challenge_id)

    incomplete_success = success_result(challenge)
    success_data = incomplete_success.dict()
    success_data["artifact"] = None
    object.__setattr__(incomplete_success, "_data", success_data)

    incomplete_error = failure_result()
    error_data = incomplete_error.dict()
    error_data["failure"] = None
    object.__setattr__(incomplete_error, "_data", error_data)

    for malformed in (incomplete_success, incomplete_error):
        pipeline = StaticPipeline(malformed)
        service = service_for(repository, claims, clock, pipeline)
        failed = await service.generate_round(generating.id)
        assert failed.state is GameState.GENERATING
        assert failed.pipeline_failure is not None
        assert failed.pipeline_failure.code == "invalid_pipeline_result"
        assert failed.pipeline_failure.retryable is True
        assert failed.score is None
        assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_cancellation_releases_claim_preserves_round_and_allows_retry(setup) -> None:
    repository, claims, clock = setup
    pipeline = BlockingPipeline()
    service = service_for(repository, claims, clock, pipeline)
    generating = await prepare_generating(service)
    before = await service.get_round(generating.id)

    attempt = asyncio.create_task(service.generate_round(generating.id))
    await pipeline.started.wait()
    attempt.cancel()

    with pytest.raises(asyncio.CancelledError):
        await attempt

    stored = await service.get_round(generating.id)
    assert stored.dict() == before.dict()
    assert stored.state is GameState.GENERATING
    assert stored.generated_artifact is None
    assert stored.score is None
    assert claims.get(generating.id) is None

    pipeline.release.set()
    retried = await service.generate_round(generating.id)
    assert retried.state is GameState.GENERATED_REVEAL
    assert pipeline.calls == 2
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_cancellation_waits_for_claim_acquisition_before_cleanup(setup) -> None:
    repository, claims, clock = setup
    delayed_claims = DelayedClaimBoundary(claims)
    service = service_for(repository, delayed_claims, clock)
    generating = await prepare_generating(service)
    before = await service.get_round(generating.id)

    attempt = asyncio.create_task(service.generate_round(generating.id))
    assert await asyncio.to_thread(delayed_claims.claim_started.wait, 5)
    attempt.cancel()

    await asyncio.sleep(0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(attempt), timeout=0.05)

    delayed_claims.allow_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await attempt

    assert delayed_claims.claim_finished.is_set()
    assert claims.get(generating.id) is None
    assert (await service.get_round(generating.id)).dict() == before.dict()

    retried = await service.generate_round(generating.id)
    assert retried.state is GameState.GENERATED_REVEAL
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_second_cancellation_waits_for_claim_cleanup(setup) -> None:
    repository, claims, clock = setup
    delayed_claims = DelayedClaimBoundary(claims)
    pipeline = BlockingPipeline()
    service = service_for(repository, delayed_claims, clock, pipeline)
    generating = await prepare_generating(service)

    attempt = asyncio.create_task(service.generate_round(generating.id))
    assert await asyncio.to_thread(delayed_claims.claim_started.wait, 5)
    delayed_claims.allow_claim.set()
    await pipeline.started.wait()
    delayed_claims.delay_release = True
    attempt.cancel()
    assert await asyncio.to_thread(delayed_claims.release_started.wait, 5)
    attempt.cancel()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(attempt), timeout=0.05)

    delayed_claims.allow_release.set()
    with pytest.raises(asyncio.CancelledError):
        await attempt

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
async def test_long_provider_renews_claim_multiple_times_before_success(setup) -> None:
    repository, claims, clock = setup
    heartbeat_claims = AdvancingHeartbeatClaims(claims, clock)
    pipeline = BlockingPipeline(expected_timeout=70.0)
    service = service_for(
        repository,
        heartbeat_claims,
        clock,
        pipeline,
        provider_timeout=70.0,
        claim_lease_duration=timedelta(seconds=30),
        claim_heartbeat_interval=timedelta(milliseconds=5),
    )
    generating = await prepare_generating(service)

    attempt = asyncio.create_task(service.generate_round(generating.id))
    await pipeline.started.wait()
    for _ in range(3):
        assert await asyncio.to_thread(heartbeat_claims.renew_finished.wait, 5)
        heartbeat_claims.renew_finished.clear()

    pipeline.release.set()
    generated = await attempt

    assert generated.state is GameState.GENERATED_REVEAL
    assert len(heartbeat_claims.renew_calls) >= 3
    assert clock.current >= datetime(2026, 1, 1, 0, 1, 15, tzinfo=UTC)
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_renewal_loss_cancels_provider_and_does_not_write_round(setup) -> None:
    repository, claims, clock = setup
    heartbeat_claims = HeartbeatClaims(claims)
    heartbeat_claims.fail_renew = True
    pipeline = CancellablePipeline(expected_timeout=70.0)
    service = service_for(
        repository,
        heartbeat_claims,
        clock,
        pipeline,
        provider_timeout=70.0,
        claim_heartbeat_interval=timedelta(milliseconds=5),
    )
    generating = await prepare_generating(service)
    before = await service.get_round(generating.id)

    attempt = asyncio.create_task(service.generate_round(generating.id))
    await pipeline.started.wait()
    assert await asyncio.to_thread(heartbeat_claims.renew_started.wait, 5)

    with pytest.raises(GameRoundConflictError, match="stale"):
        await attempt

    assert pipeline.cancelled.is_set()
    assert (await service.get_round(generating.id)).dict() == before.dict()
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("_iteration", range(20))
async def test_renewal_loss_never_deletes_replacement_token(setup, _iteration: int) -> None:
    repository, claims, clock = setup
    heartbeat_claims = ReplacementOnRenewClaims(claims, clock)
    pipeline = CancellablePipeline(expected_timeout=70.0)
    service = service_for(
        repository,
        heartbeat_claims,
        clock,
        pipeline,
        provider_timeout=70.0,
        claim_heartbeat_interval=timedelta(milliseconds=5),
    )
    generating = await prepare_generating(service)

    attempt = asyncio.create_task(service.generate_round(generating.id))
    await pipeline.started.wait()
    assert await asyncio.to_thread(heartbeat_claims.renew_started.wait, 5)

    with pytest.raises(GameRoundConflictError, match="stale"):
        await attempt

    replacement = claims.get(generating.id)
    assert replacement is not None
    assert replacement.attempt_token == "replacement-token"
    assert pipeline.cancelled.is_set()


@pytest.mark.asyncio
async def test_normal_completion_settles_blocked_heartbeat_before_commit(setup) -> None:
    repository, claims, clock = setup
    heartbeat_claims = HeartbeatClaims(claims)
    heartbeat_claims.block_renew = True
    pipeline = BlockingPipeline(expected_timeout=70.0)
    service = service_for(
        repository,
        heartbeat_claims,
        clock,
        pipeline,
        provider_timeout=70.0,
        claim_heartbeat_interval=timedelta(milliseconds=5),
    )
    generating = await prepare_generating(service)

    attempt = asyncio.create_task(service.generate_round(generating.id))
    await pipeline.started.wait()
    assert await asyncio.to_thread(heartbeat_claims.renew_started.wait, 5)
    pipeline.release.set()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(attempt), timeout=0.05)
    assert not attempt.done()

    heartbeat_claims.allow_renew.set()
    generated = await attempt
    assert generated.state is GameState.GENERATED_REVEAL
    assert claims.get(generating.id) is None


@pytest.mark.asyncio
async def test_cancellation_settles_blocked_heartbeat_and_second_cancellation(setup) -> None:
    repository, claims, clock = setup
    heartbeat_claims = HeartbeatClaims(claims)
    heartbeat_claims.block_renew = True
    pipeline = CancellablePipeline()
    service = service_for(
        repository,
        heartbeat_claims,
        clock,
        pipeline,
        claim_heartbeat_interval=timedelta(milliseconds=5),
    )
    generating = await prepare_generating(service)

    attempt = asyncio.create_task(service.generate_round(generating.id))
    await pipeline.started.wait()
    assert await asyncio.to_thread(heartbeat_claims.renew_started.wait, 5)
    attempt.cancel()
    await asyncio.sleep(0)
    attempt.cancel()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(attempt), timeout=0.05)
    assert pipeline.cancelled.is_set()

    heartbeat_claims.allow_renew.set()
    with pytest.raises(asyncio.CancelledError):
        await attempt
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
@pytest.mark.parametrize(
    "result",
    [None, failure_result()],
    ids=["success", "failure"],
)
async def test_expired_attempt_cannot_commit_success_or_failure(
    setup,
    result: AIPipelineResult | None,
) -> None:
    repository, claims, clock = setup
    pipeline = BlockingPipeline(result=result)
    service = service_for(repository, claims, clock, pipeline)
    generating = await prepare_generating(service)
    attempt = asyncio.create_task(service.generate_round(generating.id))
    await pipeline.started.wait()

    before = await service.get_round(generating.id)
    clock.current += timedelta(seconds=30)
    pipeline.release.set()

    with pytest.raises(GameRoundConflictError):
        await attempt

    stored = await service.get_round(generating.id)
    assert stored.dict() == before.dict()
    stored_claim = claims.get(generating.id)
    assert stored_claim is not None
    assert stored_claim.lease_expires_at == clock.current.isoformat()


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
    assert (await service.get_generation_status(generating.id)).state.value == "waiting"
    await service.generate_round(generating.id)
    await service.get_round(generating.id)

    assert [name for name, _thread in recording_claims.calls] == [
        "get",
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


@pytest.mark.parametrize(
    ("lease", "heartbeat", "message"),
    [
        (timedelta(0), timedelta(seconds=1), "lease duration must be positive"),
        (timedelta(seconds=10), timedelta(0), "heartbeat interval must be positive"),
        (
            timedelta(seconds=10),
            timedelta(seconds=10),
            "heartbeat interval must be shorter",
        ),
        (
            timedelta(seconds=10),
            timedelta(seconds=11),
            "heartbeat interval must be shorter",
        ),
    ],
)
def test_claim_timing_rejects_unsafe_lease_and_heartbeat_values(
    setup,
    lease: timedelta,
    heartbeat: timedelta,
    message: str,
) -> None:
    repository, claims, clock = setup

    with pytest.raises(GameRoundValidationError, match=message):
        GameRoundService(
            repository,
            make_catalog(),
            lambda choices: choices[0],
            clock,
            generation_claims=claims,
            pipeline=FakeAIPipeline(),
            owner_instance="test-worker",
            claim_lease_duration=lease,
            claim_heartbeat_interval=heartbeat,
            provider_timeout=70,
        )


def test_provider_timeout_may_exceed_claim_lease_with_heartbeat(setup) -> None:
    repository, claims, clock = setup

    GameRoundService(
        repository,
        make_catalog(),
        lambda choices: choices[0],
        clock,
        generation_claims=claims,
        pipeline=FakeAIPipeline(),
        owner_instance="test-worker",
        claim_lease_duration=timedelta(seconds=10),
        claim_heartbeat_interval=timedelta(seconds=5),
        provider_timeout=70,
    )
