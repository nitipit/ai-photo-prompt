from __future__ import annotations

import asyncio
import json
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.ai.generated_artifacts import GeneratedArtifactStore, GenerationAttempt
from app.ai.pi_pipeline import PiAIPipeline
from app.ai.pi_rpc import PiRPCRequest, PiRPCResult
from app.content.repository import ChallengeCatalog
from app.domain.models import (
    AttemptClaim,
    ChallengeSpec,
    GameState,
    LevelGroup,
    PromptSubmissionReason,
    RoundRecord,
)
from app.persistence import ShelfDbGenerationClaims, ShelfDbRoundRepository
from app.services import GameRoundConflictError, GameRoundService


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


class FinalCASBoundary:
    def __init__(
        self,
        claims: ShelfDbGenerationClaims,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.claims = claims
        self.failure = failure
        self.final_started = Event()
        self.allow_final = Event()

    @property
    def lease_duration(self):
        return self.claims.lease_duration

    def acquire_fresh(self, round_id, attempt_token, owner_instance, requested_at):
        return self.claims.acquire_fresh(
            round_id,
            attempt_token,
            owner_instance,
            requested_at,
        )

    def get(self, round_id):
        return self.claims.get(round_id)

    def renew_fresh(self, round_id, attempt_token):
        return self.claims.renew_fresh(round_id, attempt_token)

    def release_matching(self, round_id, attempt_token):
        return self.claims.release_matching(round_id, attempt_token)

    def replace_round_and_clear_claim(self, record, *, expected=None):
        return self.claims.replace_round_and_clear_claim(record, expected=expected)

    def replace_round_and_release_fresh(self, record, attempt_token, *, expected=None):
        self.final_started.set()
        self.allow_final.wait(timeout=5)
        if self.failure is not None:
            raise self.failure
        return self.claims.replace_round_and_release_fresh(
            record,
            attempt_token,
            expected=expected,
        )


def png() -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + name
            + payload
            + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">II", 1, 1) + b"\x08\x06\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


def catalog() -> ChallengeCatalog:
    return ChallengeCatalog(
        ChallengeSpec(
            id=f"{level.value}-{index}",
            title="Challenge",
            level=level,
            target_asset_url=f"/assets/challenges/{level.value}-{index}.webp",
            concept="concept",
            core_anchors=["anchor"],
            example_prompt="example",
            evaluation_notes="notes",
            feedback_focus="focus",
        )
        for level in LevelGroup
        for index in range(5)
    )


def rpc_result(*, image: bool = False) -> PiRPCResult:
    response = {"type": "response", "id": "command", "command": "prompt", "success": True}
    events: list[dict[str, object]] = [response]
    completions: tuple[dict[str, object], ...] = ()
    assistant_text = ""
    if image:
        completion: dict[str, object] = {
            "type": "tool_execution_end",
            "toolCallId": "tool-1",
            "toolName": "codex_imagegen",
            "isError": False,
            "result": {"details": {"status": "completed", "outputPath": "output/generated.png"}},
        }
        events.extend(
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "tool-1",
                    "toolName": "codex_imagegen",
                },
                completion,
            ]
        )
        completions = (completion,)
    else:
        assistant_text = json.dumps(
            {
                "schema_version": 1,
                "prompt_evaluation": {
                    "clarity": 80,
                    "specificity": 70,
                    "relationship": 60,
                    "consistency": 90,
                },
                "image_evaluation": {
                    "core_concept": 85,
                    "supporting_details": 75,
                    "scene_coherence": 95,
                },
                "feedback": ["ทำได้ดี", "ลองเพิ่มรายละเอียดฉาก"],
            },
            ensure_ascii=False,
        )
    events.append({"type": "agent_settled"})
    return PiRPCResult(
        command_id="command",
        prompt_response=response,
        events=tuple(events),
        tool_completions=completions,
        assistant_text=assistant_text,
        confirmation_sent=False,
    )


def real_pipeline(tmp_path: Path, store: GeneratedArtifactStore) -> PiAIPipeline:
    target = tmp_path / "assets" / "challenges" / "p1-p3-0.webp"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"target")

    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        if request.argv == ("pi-image",):
            Path(request.cwd, "output", "generated.png").write_bytes(png())
            return rpc_result(image=True)
        return rpc_result()

    return PiAIPipeline(
        ["pi-image"],
        ["pi-evaluator"],
        tmp_path,
        store,
        rpc,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )


def service_for(
    repository: ShelfDbRoundRepository,
    claims,
    clock: MutableClock,
    pipeline: PiAIPipeline,
) -> GameRoundService:
    return GameRoundService(
        repository,
        catalog(),
        lambda choices: choices[0],
        clock,
        generation_claims=claims,
        pipeline=pipeline,
        owner_instance="artifact-lifecycle-test",
        claim_lease_duration=timedelta(seconds=30),
        claim_heartbeat_interval=timedelta(seconds=5),
        provider_timeout=5,
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


@pytest.fixture
def lifecycle(tmp_path: Path):
    db = DB(str(tmp_path / "lifecycle-db"))
    try:
        repository = ShelfDbRoundRepository(db)
        clock = MutableClock()
        claims = ShelfDbGenerationClaims(db, clock, timedelta(seconds=30))
        store = GeneratedArtifactStore(tmp_path / "private", tmp_path / "published")
        pipeline = real_pipeline(tmp_path, store)
        yield repository, claims, clock, store, pipeline
    finally:
        db.close()


@pytest.mark.asyncio
async def test_committed_success_removes_staging_and_retains_public(lifecycle) -> None:
    repository, claims, clock, store, pipeline = lifecycle
    service = service_for(repository, claims, clock, pipeline)
    generating = await prepare_generating(service)

    generated = await service.generate_round(generating.id)

    assert generated.state is GameState.GENERATED_REVEAL
    assert generated.generated_artifact is not None
    assert not (store.private_root / generating.id).exists()
    public_files = list((store.published_root / generating.id).glob("*.png"))
    assert len(public_files) == 1
    assert generated.generated_artifact.url.endswith(f"/{public_files[0].name}")


@pytest.mark.asyncio
async def test_abandon_after_publish_makes_stale_cas_rollback_public(lifecycle) -> None:
    repository, claims, clock, store, pipeline = lifecycle
    boundary = FinalCASBoundary(claims)
    service = service_for(repository, boundary, clock, pipeline)
    generating = await prepare_generating(service)

    generation = asyncio.create_task(service.generate_round(generating.id))
    assert await asyncio.to_thread(boundary.final_started.wait, 5)
    assert list((store.published_root / generating.id).glob("*.png"))
    abandoned = await service.abandon_generation(generating.id)
    boundary.allow_final.set()

    with pytest.raises(GameRoundConflictError, match="stale"):
        await generation
    assert abandoned.state is GameState.ABANDONED
    assert not (store.published_root / generating.id).exists()
    assert not (store.private_root / generating.id).exists()


@pytest.mark.asyncio
async def test_stale_attempt_rollback_never_deletes_replacement_token(lifecycle) -> None:
    repository, claims, clock, store, pipeline = lifecycle
    boundary = FinalCASBoundary(claims)
    service = service_for(repository, boundary, clock, pipeline)
    generating = await prepare_generating(service)

    generation = asyncio.create_task(service.generate_round(generating.id))
    assert await asyncio.to_thread(boundary.final_started.wait, 5)
    original_claim = claims.get(generating.id)
    assert original_claim is not None
    original_public = store.resolve_public(
        GenerationAttempt(generating.id, original_claim.attempt_token)
    )

    clock.current += timedelta(seconds=31)
    replacement_token = str(uuid4())
    replacement_now = clock.current.isoformat()
    replacement_claim = AttemptClaim(
        attempt_token=replacement_token,
        owner_instance="replacement",
        claimed_at=replacement_now,
        lease_expires_at=(clock.current + timedelta(seconds=30)).isoformat(),
    )
    claims.claim(generating.id, replacement_claim, replacement_now)
    replacement = GenerationAttempt(generating.id, replacement_token)
    replacement_destination = store.prepare(replacement)
    replacement_destination.write_bytes(png())
    replacement_public = store.publish(replacement, "output/generated.png")
    store.cleanup_workspace(replacement)
    boundary.allow_final.set()

    with pytest.raises(GameRoundConflictError, match="stale"):
        await generation
    assert not original_public.exists()
    assert replacement_public.final_path.exists()
    assert claims.get(generating.id) == replacement_claim


@pytest.mark.asyncio
async def test_final_cas_failure_rolls_back_success_artifact(lifecycle) -> None:
    repository, claims, clock, store, pipeline = lifecycle
    boundary = FinalCASBoundary(claims, failure=RuntimeError("database unavailable"))
    boundary.allow_final.set()
    service = service_for(repository, boundary, clock, pipeline)
    generating = await prepare_generating(service)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.generate_round(generating.id)

    assert not (store.published_root / generating.id).exists()
    assert not (store.private_root / generating.id).exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_settles_failed_final_cas_and_rollback(lifecycle) -> None:
    repository, claims, clock, store, pipeline = lifecycle
    boundary = FinalCASBoundary(claims, failure=RuntimeError("cancelled final CAS"))
    service = service_for(repository, boundary, clock, pipeline)
    generating = await prepare_generating(service)

    generation = asyncio.create_task(service.generate_round(generating.id))
    assert await asyncio.to_thread(boundary.final_started.wait, 5)
    generation.cancel()
    await asyncio.sleep(0)
    generation.cancel()
    boundary.allow_final.set()

    with pytest.raises(asyncio.CancelledError):
        await generation
    assert not (store.published_root / generating.id).exists()
    assert not (store.private_root / generating.id).exists()
    assert claims.get(generating.id) is None
    assert repository.get(generating.id).state is GameState.GENERATING
