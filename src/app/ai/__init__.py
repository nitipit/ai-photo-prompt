"""Bounded AI provider protocols and the deterministic local pipeline."""

from .fake import (
    FAKE_FEEDBACK_LINES,
    FakeFeedbackComposer,
    FakeImageGenerator,
    FakeImageMatcher,
    FakePromptEvaluator,
)
from .generated_artifacts import GeneratedArtifactStore
from .pi_pipeline import PiAIPipeline
from .pipeline import FakeAIPipeline
from .protocols import (
    FeedbackComposer,
    GenerationAttempt,
    ImageGenerator,
    ImageMatcher,
    PromptEvaluator,
)
from .results import AIPipelineResult

__all__ = [
    "AIPipelineResult",
    "FAKE_FEEDBACK_LINES",
    "FakeAIPipeline",
    "FakeFeedbackComposer",
    "FakeImageGenerator",
    "FakeImageMatcher",
    "FakePromptEvaluator",
    "GeneratedArtifactStore",
    "GenerationAttempt",
    "FeedbackComposer",
    "ImageGenerator",
    "ImageMatcher",
    "PiAIPipeline",
    "PromptEvaluator",
]
