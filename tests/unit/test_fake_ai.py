from __future__ import annotations

import pytest

from app.ai import FAKE_FEEDBACK_LINES, FakeAIPipeline
from app.domain.models import ChallengeSpec, PipelineResultStatus


def challenge() -> ChallengeSpec:
    return ChallengeSpec(
        id="p1-p3-01",
        title="สวนสนุก",
        level="p1-p3",
        target_asset_url="/assets/challenges/p1-p3-01.webp",
        concept="เด็กเล่นชิงช้า",
        core_anchors=["เด็ก", "ชิงช้า"],
        optional_details=["ท้องฟ้า"],
        example_prompt="เด็กเล่นชิงช้าในสวนสนุก",
        evaluation_notes="ตรวจตัวละครและการกระทำ",
        feedback_focus="สังเกตความสัมพันธ์ในภาพ",
    )


@pytest.mark.asyncio
async def test_fake_pipeline_is_deterministic_and_returns_three_thai_lines() -> None:
    first = await FakeAIPipeline().run(challenge(), "เด็กเล่นชิงช้าในสวนสนุก")
    second = await FakeAIPipeline().run(challenge(), "เด็กเล่นชิงช้าในสวนสนุก")

    assert first.status is PipelineResultStatus.SUCCESS
    assert first.dict() == second.dict()
    assert first.artifact.url == "/generated/fake-ai/p1-p3-01.webp"
    assert first.artifact.provider == "fake-ai"
    assert first.prompt_evaluation.dict() == {
        "clarity": 80,
        "specificity": 70,
        "relationship": 60,
        "consistency": 90,
    }
    assert first.image_evaluation.dict() == {
        "core_concept": 85,
        "supporting_details": 75,
        "scene_coherence": 95,
    }
    assert first.score.dict() == {
        "prompt_score": 74.0,
        "image_score": 84.0,
        "total_score": 79,
    }
    assert first.feedback == list(FAKE_FEEDBACK_LINES)
    assert len(first.feedback) == 3
    assert all(
        any("\u0e00" <= character <= "\u0e7f" for character in line) for line in first.feedback
    )


@pytest.mark.asyncio
async def test_fake_pipeline_failure_has_no_artifact_or_score() -> None:
    result = await FakeAIPipeline(fail=True).run(challenge(), "เด็กเล่นชิงช้าในสวนสนุก")

    assert result.status is PipelineResultStatus.ERROR
    assert result.failure.code == "fake_pipeline_failure"
    assert result.failure.provider == "fake-ai"
    assert result.artifact is None
    assert result.prompt_evaluation is None
    assert result.image_evaluation is None
    assert result.score is None
    assert result.feedback == []
