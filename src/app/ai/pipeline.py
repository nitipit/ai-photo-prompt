"""Direct orchestration boundary for the deterministic fake AI providers."""

from __future__ import annotations

from app.domain.models import (
    ChallengeSpec,
    FailureDetail,
    ImageArtifact,
    ImageMatchEvaluation,
    PipelineResultStatus,
    PromptEvaluation,
    ProviderError,
)
from app.domain.scoring import score_total

from .fake import (
    FakeFeedbackComposer,
    FakeImageGenerator,
    FakeImageMatcher,
    FakePromptEvaluator,
)
from .protocols import GenerationAttempt
from .results import AIPipelineResult


class FakeAIPipeline:
    """Run one deterministic fake generation, evaluation, and feedback attempt."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self._image_generator = FakeImageGenerator()
        self._prompt_evaluator = FakePromptEvaluator()
        self._image_matcher = FakeImageMatcher()
        self._feedback_composer = FakeFeedbackComposer()

    async def run(
        self,
        challenge: ChallengeSpec,
        prompt: str,
        timeout: float = 1.0,
        *,
        attempt: GenerationAttempt | None = None,
    ) -> AIPipelineResult:
        """Return a complete success or a bounded failure with no partial result."""

        del attempt
        if self._fail:
            return AIPipelineResult(
                status=PipelineResultStatus.ERROR,
                failure=FailureDetail(
                    code="fake_pipeline_failure",
                    message="การประมวลผล AI จำลองล้มเหลว",
                    retryable=True,
                    provider="fake-ai",
                ),
            )

        image_result = await self._image_generator.generate(challenge, prompt, timeout)
        if isinstance(image_result, ProviderError):
            return self._failure(image_result)
        artifact = ImageArtifact(image_result.result["artifact"])

        prompt_result = await self._prompt_evaluator.evaluate(challenge, prompt, timeout)
        if isinstance(prompt_result, ProviderError):
            return self._failure(prompt_result)
        prompt_evaluation = PromptEvaluation(prompt_result.result["evaluation"])

        image_match_result = await self._image_matcher.evaluate(challenge, artifact, timeout)
        if isinstance(image_match_result, ProviderError):
            return self._failure(image_match_result)
        image_evaluation = ImageMatchEvaluation(image_match_result.result["evaluation"])

        score = score_total(prompt_evaluation, image_evaluation)
        feedback_result = await self._feedback_composer.compose(
            challenge,
            prompt_evaluation,
            image_evaluation,
            score,
            timeout,
        )
        if isinstance(feedback_result, ProviderError):
            return self._failure(feedback_result)

        return AIPipelineResult(
            status=PipelineResultStatus.SUCCESS,
            artifact=artifact,
            prompt_evaluation=prompt_evaluation,
            image_evaluation=image_evaluation,
            score=score,
            feedback=feedback_result.result["feedback"],
        )

    @staticmethod
    def _failure(result: ProviderError) -> AIPipelineResult:
        return AIPipelineResult(status=PipelineResultStatus.ERROR, failure=result.error)


__all__ = ["FakeAIPipeline"]
