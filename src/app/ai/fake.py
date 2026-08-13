"""Deterministic fake providers for the initial local AI boundary."""

from __future__ import annotations

from app.domain.models import (
    ChallengeSpec,
    ImageArtifact,
    ImageMatchEvaluation,
    PromptEvaluation,
    ProviderSuccess,
    ScoreResult,
)

FAKE_FEEDBACK_LINES = (
    "อธิบายภาพได้ชัดเจน",
    "เพิ่มรายละเอียดสำคัญได้ดี",
    "ลองตรวจความสัมพันธ์ของตัวละครกับฉากอีกครั้ง",
)


class FakeImageGenerator:
    """Return a clearly marked, deterministic fake artifact."""

    async def generate(
        self, challenge: ChallengeSpec, prompt: str, timeout: float
    ) -> ProviderSuccess:
        del prompt, timeout
        artifact = ImageArtifact(
            url=challenge.target_asset_url,
            provider="fake-ai",
            width=1024,
            height=1024,
        )
        return ProviderSuccess(result={"artifact": artifact.dict()})


class FakePromptEvaluator:
    """Return fixed valid prompt dimensions without external work."""

    async def evaluate(
        self, challenge: ChallengeSpec, prompt: str, timeout: float
    ) -> ProviderSuccess:
        del challenge, prompt, timeout
        evaluation = PromptEvaluation(
            clarity=80,
            specificity=70,
            relationship=60,
            consistency=90,
        )
        return ProviderSuccess(result={"evaluation": evaluation.dict()})


class FakeImageMatcher:
    """Return fixed valid image-match dimensions without external work."""

    async def evaluate(
        self, challenge: ChallengeSpec, artifact: ImageArtifact, timeout: float
    ) -> ProviderSuccess:
        del challenge, artifact, timeout
        evaluation = ImageMatchEvaluation(
            core_concept=85,
            supporting_details=75,
            scene_coherence=95,
        )
        return ProviderSuccess(result={"evaluation": evaluation.dict()})


class FakeFeedbackComposer:
    """Return exactly three concise Thai feedback lines."""

    async def compose(
        self,
        challenge: ChallengeSpec,
        prompt_evaluation: PromptEvaluation,
        image_evaluation: ImageMatchEvaluation,
        score: ScoreResult,
        timeout: float,
    ) -> ProviderSuccess:
        del challenge, prompt_evaluation, image_evaluation, score, timeout
        return ProviderSuccess(result={"feedback": list(FAKE_FEEDBACK_LINES)})


__all__ = [
    "FAKE_FEEDBACK_LINES",
    "FakeFeedbackComposer",
    "FakeImageGenerator",
    "FakeImageMatcher",
    "FakePromptEvaluator",
]
