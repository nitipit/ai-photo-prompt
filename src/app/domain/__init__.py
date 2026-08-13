"""Validated domain records and the round lifecycle state machine."""

from .models import (
    AttemptClaim,
    ChallengeSpec,
    ChallengeStatus,
    FailureDetail,
    GameState,
    ImageArtifact,
    ImageMatchEvaluation,
    LeaderboardEntry,
    LevelGroup,
    PipelineResult,
    PipelineResultStatus,
    PromptEvaluation,
    PromptSubmissionReason,
    ProviderError,
    ProviderSuccess,
    RoundRecord,
    ScoreResult,
    TerminalDisposition,
)
from .scoring import score_image, score_prompt, score_total
from .state import RoundStateMachine

__all__ = [
    "AttemptClaim",
    "ChallengeSpec",
    "ChallengeStatus",
    "FailureDetail",
    "GameState",
    "ImageArtifact",
    "ImageMatchEvaluation",
    "LeaderboardEntry",
    "LevelGroup",
    "PipelineResult",
    "PipelineResultStatus",
    "PromptEvaluation",
    "PromptSubmissionReason",
    "ProviderError",
    "ProviderSuccess",
    "RoundRecord",
    "RoundStateMachine",
    "ScoreResult",
    "TerminalDisposition",
    "score_image",
    "score_prompt",
    "score_total",
]
