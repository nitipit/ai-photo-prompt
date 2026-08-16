from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from app.ai.pi_pipeline import (
    ArtifactWorkspace,
    PiAIPipeline,
    PublishedArtifact,
)
from app.ai.pi_rpc import PiRPCRequest, PiRPCResult
from app.ai.protocols import GenerationAttempt
from app.domain.models import (
    ChallengeSpec,
    ImageArtifact,
    PipelineResultStatus,
)

ATTEMPT = GenerationAttempt(round_id="round-1", attempt_token="attempt-1")


class FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "private"
        self.relative_output_path = "generated.png"
        self.staged_path = self.workspace / self.relative_output_path
        self.published = PublishedArtifact(
            ImageArtifact(url="/generated/round-1/attempt-1.png", mime_type="image/png"),
            root / "published" / "attempt-1.png",
        )
        self.discards: list[GenerationAttempt] = []
        self.reads = 0

    def prepare_workspace(self, attempt: GenerationAttempt) -> ArtifactWorkspace:
        self.workspace.mkdir(parents=True, exist_ok=True)
        return ArtifactWorkspace(self.workspace, self.relative_output_path, self.staged_path)

    def publish(self, attempt: GenerationAttempt, staged_path: Path) -> PublishedArtifact:
        assert attempt is ATTEMPT
        assert staged_path == self.staged_path
        if not staged_path.is_file():
            raise ValueError("missing staged image")
        return self.published

    def discard(self, attempt: GenerationAttempt) -> None:
        self.discards.append(attempt)

    def read(self, published: PublishedArtifact) -> bytes:
        self.reads += 1
        return b"generated-image"


@pytest.fixture
def challenge(tmp_path: Path) -> ChallengeSpec:
    target = tmp_path / "assets" / "challenges" / "dragon.webp"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"target-image")
    return ChallengeSpec(
        id="p4-p6-01",
        title="Dragon goalkeeper",
        level="p4-p6",
        target_asset_url="/assets/challenges/dragon.webp",
        concept="A dragon plays football in a castle courtyard.",
        core_anchors=["purple dragon", "giant soccer ball"],
        optional_details=["rainbow"],
        example_prompt="มังกรสีม่วงเป็นผู้รักษาประตู",
        evaluation_notes="The action and setting matter most.",
        feedback_focus="Praise the action and suggest one setting detail.",
    )


def evaluation_text(
    *,
    prompt: dict[str, Any] | None = None,
    image: dict[str, Any] | None = None,
    feedback: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "prompt_evaluation": prompt
            or {"clarity": 100, "specificity": 50, "relationship": 25, "consistency": 0},
            "image_evaluation": image
            or {"core_concept": 90, "supporting_details": 50, "scene_coherence": 20},
            "feedback": feedback or ["ชัดเจนมาก", "เพิ่มรายละเอียดฉากอีกนิด"],
        },
        ensure_ascii=False,
    )


def rpc_result(
    *,
    assistant_text: str = "",
    image: bool = False,
    tools: tuple[dict[str, Any], ...] = (),
    confirmation_sent: bool | None = None,
) -> PiRPCResult:
    events: list[dict[str, Any]] = [
        {"type": "response", "id": "command", "command": "prompt", "success": True},
    ]
    if image:
        events.extend(
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "tool-1",
                    "toolName": "codex_imagegen",
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "tool-1",
                    "toolName": "codex_imagegen",
                    "isError": False,
                    "result": {"details": {"status": "completed", "outputPath": "generated.png"}},
                },
            ]
        )
    events.append({"type": "agent_settled"})
    return PiRPCResult(
        command_id="command",
        prompt_response=events[0],
        events=tuple(events),
        tool_completions=tools
        or tuple(event for event in events if event["type"] == "tool_execution_end"),
        assistant_text=assistant_text,
        confirmation_sent=image if confirmation_sent is None else confirmation_sent,
    )


def make_pipeline(
    tmp_path: Path,
    challenge: ChallengeSpec,
    rpc: Any,
    *,
    store: FakeStore | None = None,
) -> tuple[PiAIPipeline, FakeStore]:
    actual_store = store or FakeStore(tmp_path)
    return (
        PiAIPipeline(
            ["pi-image"],
            ["pi-evaluator"],
            tmp_path,
            actual_store,
            rpc,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        ),
        actual_store,
    )


@pytest.mark.asyncio
async def test_success_uses_two_attachments_and_computes_only_application_score(
    tmp_path: Path, challenge: ChallengeSpec
) -> None:
    requests: list[PiRPCRequest] = []

    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        requests.append(request)
        if request.argv == ("pi-image",):
            assert request.authorize_confirmation is True
            Path(request.cwd, "generated.png").write_bytes(b"generated-image")
            image_result = rpc_result(image=True, confirmation_sent=False)
            assert len(image_result.tool_completions) == 1
            return image_result
        assert len(request.attachments) == 2
        assert request.attachments[0].mime_type == "image/webp"
        assert request.attachments[1].mime_type == "image/png"
        assert base64.b64decode(request.attachments[0].data) == b"target-image"
        assert base64.b64decode(request.attachments[1].data) == b"generated-image"
        assert request.authorize_confirmation is False
        return rpc_result(assistant_text=evaluation_text())

    pipeline, store = make_pipeline(tmp_path, challenge, rpc)
    result = await pipeline.run(challenge, "มังกรรับบอล", 2, attempt=ATTEMPT)

    assert result.status is PipelineResultStatus.SUCCESS
    assert result.artifact == store.published.artifact
    assert result.score is not None
    assert result.score.prompt_score == pytest.approx(60)
    assert result.score.image_score == pytest.approx(75)
    assert result.score.total_score == 68
    assert len(requests) == 2
    image_payload = json.loads(requests[0].prompt)
    evaluator_payload = json.loads(requests[1].prompt)
    assert image_payload["untrusted_input"]["student_prompt"] == "มังกรรับบอล"
    assert image_payload["image_request"] == {
        "count": 1,
        "aspect_ratio": "16:9",
        "kind": "raster",
        "output_path": "generated.png",
    }
    assert evaluator_payload["untrusted_input"]["visual_inputs"] == [
        {
            "attachment": 1,
            "role": "approved target reference",
            "trust": "untrusted visual input",
        },
        {
            "attachment": 2,
            "role": "generated candidate",
            "trust": "untrusted visual input",
        },
    ]
    assert "Attachment 1 is the approved target reference" in evaluator_payload["instruction"]
    assert "Attachment 2 is the generated candidate" in evaluator_payload["instruction"]
    assert store.discards == []


@pytest.mark.asyncio
async def test_timeout_is_one_total_budget(tmp_path: Path, challenge: ChallengeSpec) -> None:
    calls = 0

    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return rpc_result(image=True)

    pipeline, store = make_pipeline(tmp_path, challenge, rpc)
    result = await pipeline.run(challenge, "prompt", 0.01, attempt=ATTEMPT)

    assert result.failure is not None
    assert result.failure.code == "pi_timeout"
    assert calls == 1
    assert store.discards == [ATTEMPT]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_completion",
    [
        {"toolName": "other_tool", "isError": False},
        {"toolName": "codex_imagegen", "isError": True},
        {"toolName": "codex_imagegen", "isError": False, "result": {}},
        {
            "toolName": "codex_imagegen",
            "isError": False,
            "result": {"details": {"status": "completed", "outputPath": "assistant.png"}},
        },
    ],
)
async def test_image_requires_one_authoritative_completion(
    tmp_path: Path, challenge: ChallengeSpec, bad_completion: dict[str, Any]
) -> None:
    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        Path(request.cwd, "generated.png").write_bytes(b"generated-image")
        event = {
            "type": "tool_execution_end",
            "toolCallId": "tool-1",
            **bad_completion,
        }
        return rpc_result(image=True, tools=(event,))

    pipeline, store = make_pipeline(tmp_path, challenge, rpc)
    result = await pipeline.run(challenge, "prompt", 2, attempt=ATTEMPT)

    assert result.failure is not None
    assert result.failure.code == "pi_protocol_error"
    assert store.discards == [ATTEMPT]


@pytest.mark.asyncio
async def test_duplicate_image_completion_is_rejected(
    tmp_path: Path, challenge: ChallengeSpec
) -> None:
    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        if request.argv == ("pi-image",):
            Path(request.cwd, "generated.png").write_bytes(b"generated-image")
            completion = {
                "type": "tool_execution_end",
                "toolCallId": "tool-1",
                "toolName": "codex_imagegen",
                "isError": False,
                "result": {"details": {"status": "completed", "outputPath": "generated.png"}},
            }
            return rpc_result(image=True, tools=(completion, completion))
        return rpc_result(assistant_text=evaluation_text())

    pipeline, store = make_pipeline(tmp_path, challenge, rpc)
    result = await pipeline.run(challenge, "prompt", 2, attempt=ATTEMPT)

    assert result.failure is not None
    assert result.failure.code == "pi_protocol_error"
    assert store.discards == [ATTEMPT]


@pytest.mark.asyncio
async def test_target_traversal_is_rejected_and_published_artifact_cleaned(
    tmp_path: Path, challenge: ChallengeSpec
) -> None:
    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        Path(request.cwd, "generated.png").write_bytes(b"generated-image")
        return rpc_result(image=True)

    class UnsafeChallenge(ChallengeSpec):
        @property
        def target_asset_url(self) -> str:
            return "/assets/challenges/../secret.webp"

    unsafe = UnsafeChallenge(**challenge.dict())
    pipeline, store = make_pipeline(tmp_path, unsafe, rpc)
    result = await pipeline.run(unsafe, "prompt", 2, attempt=ATTEMPT)

    assert result.failure is not None
    assert result.failure.code == "invalid_target_asset"
    assert store.discards == [ATTEMPT]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "```json\n{}\n```",
        '{"schema_version":1,"prompt_evaluation":{},"image_evaluation":{},"feedback":[]} trailing',
        '{"schema_version":1,"schema_version":1,"prompt_evaluation":{},"image_evaluation":{},"feedback":[]}',
        '{"schema_version":1,"prompt_evaluation":{},"image_evaluation":{},"feedback":[],"url":"/bad"}',
        '{"schema_version":1,"prompt_evaluation":{"clarity":true,"specificity":1,"relationship":1,"consistency":1},"image_evaluation":{"core_concept":1,"supporting_details":1,"scene_coherence":1},"feedback":["a","b"]}',
        '{"schema_version":1,"prompt_evaluation":{"clarity":101,"specificity":1,"relationship":1,"consistency":1},"image_evaluation":{"core_concept":1,"supporting_details":1,"scene_coherence":1},"feedback":["a","b"]}',
        '{"schema_version":1,"prompt_evaluation":{"clarity":NaN,"specificity":1,"relationship":1,"consistency":1},"image_evaluation":{"core_concept":1,"supporting_details":1,"scene_coherence":1},"feedback":["a","b"]}',
    ],
)
async def test_evaluation_json_is_strict_and_cleanup_is_token_scoped(
    tmp_path: Path, challenge: ChallengeSpec, text: str
) -> None:
    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        if request.argv == ("pi-image",):
            Path(request.cwd, "generated.png").write_bytes(b"generated-image")
            return rpc_result(image=True)
        return rpc_result(assistant_text=text)

    pipeline, store = make_pipeline(tmp_path, challenge, rpc)
    result = await pipeline.run(challenge, "prompt", 2, attempt=ATTEMPT)

    assert result.failure is not None
    assert result.failure.code == "invalid_provider_json"
    assert store.discards == [ATTEMPT]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_two_or_three_feedback_lines_are_accepted(
    tmp_path: Path, challenge: ChallengeSpec, count: int
) -> None:
    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        if request.argv == ("pi-image",):
            Path(request.cwd, "generated.png").write_bytes(b"generated-image")
            return rpc_result(image=True)
        return rpc_result(assistant_text=evaluation_text(feedback=["ดีมาก"] * count))

    pipeline, store = make_pipeline(tmp_path, challenge, rpc)
    result = await pipeline.run(challenge, "prompt", 2, attempt=ATTEMPT)

    assert result.status is PipelineResultStatus.SUCCESS
    assert len(result.feedback) == count
    assert store.discards == []


@pytest.mark.asyncio
async def test_cancellation_reraises_after_cleanup(
    tmp_path: Path, challenge: ChallengeSpec
) -> None:
    entered = asyncio.Event()

    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        if request.argv == ("pi-image",):
            Path(request.cwd, "generated.png").write_bytes(b"generated-image")
            return rpc_result(image=True)
        entered.set()
        await asyncio.sleep(30)
        return rpc_result(assistant_text=evaluation_text())

    pipeline, store = make_pipeline(tmp_path, challenge, rpc)
    task = asyncio.create_task(pipeline.run(challenge, "prompt", 30, attempt=ATTEMPT))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.discards == [ATTEMPT]


@pytest.mark.asyncio
async def test_failures_do_not_leak_provider_text_paths_or_stderr(
    tmp_path: Path, challenge: ChallengeSpec
) -> None:
    async def rpc(request: PiRPCRequest) -> PiRPCResult:
        raise RuntimeError("/private/secret/stderr model prose")

    pipeline, store = make_pipeline(tmp_path, challenge, rpc)
    result = await pipeline.run(challenge, "prompt", 2, attempt=ATTEMPT)

    assert result.failure is not None
    assert result.failure.code == "pi_process_error"
    assert "/private" not in result.failure.message
    assert "model" not in result.failure.message
    assert store.discards == [ATTEMPT]
