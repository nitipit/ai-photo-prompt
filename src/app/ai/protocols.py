"""Small async provider boundaries used by the AI pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.models import (
    ChallengeSpec,
    ImageArtifact,
    ImageMatchEvaluation,
    PromptEvaluation,
    ProviderError,
    ProviderSuccess,
    ScoreResult,
)

ProviderResult = ProviderSuccess | ProviderError


@dataclass(frozen=True, slots=True)
class GenerationAttempt:
    """Server-owned identity used for private workspace and artifact fencing."""

    round_id: str
    attempt_token: str


class ImageGenerator(Protocol):
    """Generate one image artifact for a challenge and prompt."""

    async def generate(
        self, challenge: ChallengeSpec, prompt: str, timeout: float
    ) -> ProviderResult: ...


class PromptEvaluator(Protocol):
    """Evaluate the prompt against the challenge brief."""

    async def evaluate(
        self, challenge: ChallengeSpec, prompt: str, timeout: float
    ) -> ProviderResult: ...


class ImageMatcher(Protocol):
    """Evaluate the generated artifact against the challenge brief."""

    async def evaluate(
        self, challenge: ChallengeSpec, artifact: ImageArtifact, timeout: float
    ) -> ProviderResult: ...


class FeedbackComposer(Protocol):
    """Compose concise player-facing feedback from the completed evaluations."""

    async def compose(
        self,
        challenge: ChallengeSpec,
        prompt_evaluation: PromptEvaluation,
        image_evaluation: ImageMatchEvaluation,
        score: ScoreResult,
        timeout: float,
    ) -> ProviderResult: ...


__all__ = [
    "FeedbackComposer",
    "GenerationAttempt",
    "ImageGenerator",
    "ImageMatcher",
    "PromptEvaluator",
    "ProviderResult",
]
