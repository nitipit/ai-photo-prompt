"""Strict, mapping-shaped records shared by application boundaries.

The models deliberately store only JSON/MessagePack-compatible primitives after
``dict()``.  Constructors validate both freshly decoded mappings and nested
records, so persistence and provider adapters can reconstruct the same contract
on every read.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Any
from uuid import UUID

from dictify import Field, ListOf, Model


class _StrictModel(Model):
    """Dictify model with recursive conversion to wire-safe primitives."""

    def dict(self) -> dict[str, Any]:
        return _to_primitives(super().dict())


class LevelGroup(StrEnum):
    """The four age bands used to select an approved challenge."""

    P1_P3 = "p1-p3"
    P4_P6 = "p4-p6"
    M1_M3 = "m1-m3"
    M4_M6 = "m4-m6"


class ChallengeStatus(StrEnum):
    """Publication status accepted by the build-time challenge catalog."""

    APPROVED = "approved"


class GameState(StrEnum):
    """Persisted round scenes; Ready is intentionally not a round state."""

    LEVEL_SELECTION = "level_selection"
    CHALLENGE_REVEAL = "challenge_reveal"
    PROMPT_ENTRY = "prompt_entry"
    GENERATING = "generating"
    GENERATED_REVEAL = "generated_reveal"
    RESULT = "result"
    ABANDONED = "abandoned"
    LEADERBOARD = "leaderboard"


class PromptSubmissionReason(StrEnum):
    """How a prompt was submitted to the generation pipeline."""

    MANUAL = "manual"
    TIMEOUT = "timeout"


class TerminalDisposition(StrEnum):
    """Historical outcome retained after a round leaves the kiosk flow."""

    COMPLETED = "completed"
    ABANDONED = "abandoned"


class PipelineResultStatus(StrEnum):
    """Tag carried by a strict provider/pipeline result envelope."""

    SUCCESS = "success"
    ERROR = "error"


def _to_primitives(value: Any) -> Any:
    """Recursively turn enums and nested containers into MessagePack values."""

    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _to_primitives(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_primitives(item) for item in value]
    return value


def _schema_version(value: Any) -> int:
    if type(value) is not int:
        raise TypeError("schema_version must be an integer")
    if value != 1:
        raise ValueError("unsupported schema version")
    return value


def _strict_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise TypeError("expected a boolean")
    return value


def _text(value: Any) -> str:
    if type(value) is not str:
        raise TypeError("expected a string")
    if not value.strip():
        raise ValueError("expected a non-empty string")
    return value


def _any_text(value: Any) -> str:
    if type(value) is not str:
        raise TypeError("expected a string")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _any_text(value)


def _asset_url(value: Any) -> str:
    value = _text(value)
    if not value.startswith("/assets/challenges/") or "://" in value or "\\" in value:
        raise ValueError("target_asset_url must be a browser challenge asset path")
    if ".." in value.split("/"):
        raise ValueError("target_asset_url cannot escape the asset directory")
    return value


def _enum(enum_type: type[StrEnum]):
    def convert(value: Any) -> StrEnum:
        if isinstance(value, enum_type):
            return value
        if type(value) is not str:
            raise TypeError(f"expected {enum_type.__name__} or string")
        try:
            return enum_type(value)
        except ValueError as error:
            raise ValueError(f"unsupported {enum_type.__name__}: {value!r}") from error

    return convert


def _optional_enum(enum_type: type[StrEnum]):
    convert = _enum(enum_type)

    def optional(value: Any) -> StrEnum | None:
        return None if value is None else convert(value)

    return optional


def _tag(expected: PipelineResultStatus):
    def convert(value: Any) -> PipelineResultStatus:
        result = _enum(PipelineResultStatus)(value)
        if result is not expected:
            raise ValueError(f"expected result status {expected.value!r}")
        return result

    return convert


def _string_list(value: Any) -> ListOf:
    if type(value) is not list:
        raise TypeError("expected a list")
    if any(type(item) is not str for item in value):
        raise TypeError("expected a list of strings")
    return ListOf(value, str)


def _non_empty_string_list(value: Any) -> ListOf:
    result = _string_list(value)
    if not result:
        raise ValueError("expected at least one item")
    if any(not item.strip() for item in result):
        raise ValueError("list items must be non-empty strings")
    return result


def _model(model_type: type[_StrictModel]):
    def convert(value: Any) -> _StrictModel:
        if isinstance(value, model_type):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"expected {model_type.__name__} or mapping")
        return model_type(value)

    return convert


def _model_or_none(model_type: type[_StrictModel]):
    convert = _model(model_type)

    def optional(value: Any) -> _StrictModel | None:
        return None if value is None else convert(value)

    return optional


def _uuid_string(value: Any) -> str:
    value = _text(value)
    try:
        UUID(value)
    except ValueError as error:
        raise ValueError("expected a UUID string") from error
    return value


def _utc_timestamp(value: Any) -> str:
    value = _text(value)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError("expected an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("timestamp must include UTC timezone")
    return value


def _optional_utc_timestamp(value: Any) -> str | None:
    return None if value is None else _utc_timestamp(value)


def _score(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("score must be a number")
    if not isfinite(value) or not 0 <= value <= 100:
        raise ValueError("score must be between 0 and 100")
    return value


def _non_negative_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise TypeError("expected a non-negative integer")
    return value


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value)


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected a mapping")
    return dict(value)


class ChallengeSpec(_StrictModel):
    """Materialized brief used at runtime instead of parsing challenge Markdown."""

    schema_version = Field(default=1).func(_schema_version)
    id = Field(required=True).func(_text)
    title = Field(required=True).func(_text)
    level = Field(required=True).func(_enum(LevelGroup))
    status = Field(default=ChallengeStatus.APPROVED).func(_enum(ChallengeStatus))
    target_asset_url = Field(required=True).func(_asset_url)
    concept = Field(required=True).func(_text)
    core_anchors = Field(required=True).func(_non_empty_string_list)
    optional_details = Field(default=list).func(_string_list)
    example_prompt = Field(required=True).func(_text)
    evaluation_notes = Field(required=True).func(_text)
    feedback_focus = Field(required=True).func(_text)


class ImageArtifact(_StrictModel):
    """Generated image reference retained in a round and leaderboard entry."""

    url = Field(required=True).func(_text)
    mime_type = Field(default="image/webp").func(_text)
    provider = Field(default=None).func(_optional_text)
    width = Field(default=None).func(_optional_non_negative_int)
    height = Field(default=None).func(_optional_non_negative_int)


class PromptEvaluation(_StrictModel):
    """Four bounded dimensions used by the prompt-scoring policy."""

    clarity = Field(required=True).func(_score)
    specificity = Field(required=True).func(_score)
    relationship = Field(required=True).func(_score)
    consistency = Field(required=True).func(_score)


class ImageMatchEvaluation(_StrictModel):
    """Three bounded dimensions describing generated-image alignment."""

    core_concept = Field(required=True).func(_score)
    supporting_details = Field(required=True).func(_score)
    scene_coherence = Field(required=True).func(_score)


class ScoreResult(_StrictModel):
    """Validated prompt, image, and final scores exposed to the result scene."""

    prompt_score = Field(required=True).func(_score)
    image_score = Field(required=True).func(_score)
    total_score = Field(required=True).func(_score)


class FailureDetail(_StrictModel):
    """Safe bounded failure information that may cross a provider boundary."""

    code = Field(required=True).func(_text)
    message = Field(required=True).func(_text)
    retryable = Field(default=True).func(_strict_bool)
    provider = Field(default=None).func(_optional_text)


class ProviderSuccess(_StrictModel):
    """Tagged successful envelope for a provider result payload."""

    status = Field(default=PipelineResultStatus.SUCCESS).func(_tag(PipelineResultStatus.SUCCESS))
    result = Field(required=True).func(_mapping)


class ProviderError(_StrictModel):
    """Tagged error envelope whose failure detail is safe to show or retry."""

    status = Field(default=PipelineResultStatus.ERROR).func(_tag(PipelineResultStatus.ERROR))
    error = Field(required=True).func(_model(FailureDetail))


class PipelineResult(_StrictModel):
    """Strict success/error envelope for the complete generation pipeline."""

    status = Field(required=True).func(_enum(PipelineResultStatus))
    artifact = Field(default=None).func(_model_or_none(ImageArtifact))
    failure = Field(default=None).func(_model_or_none(FailureDetail))

    def post_validate(self) -> None:
        if self.status is PipelineResultStatus.SUCCESS and self.artifact is None:
            raise self.Error({"artifact": "success result requires an artifact"})
        if self.status is PipelineResultStatus.ERROR and self.failure is None:
            raise self.Error({"failure": "error result requires failure detail"})
        if self.status is PipelineResultStatus.SUCCESS and self.failure is not None:
            raise self.Error({"failure": "success result cannot contain failure detail"})
        if self.status is PipelineResultStatus.ERROR and self.artifact is not None:
            raise self.Error({"artifact": "error result cannot contain an artifact"})


class LeaderboardEntry(_StrictModel):
    """A completed round projection suitable for leaderboard rendering."""

    rank = Field(required=True).func(_non_negative_int)
    name = Field(required=True).func(_text)
    score = Field(required=True).func(_score)
    generated_image = Field(required=True).func(_model(ImageArtifact))
    prompt = Field(required=True).func(_any_text)


class AttemptClaim(_StrictModel):
    """MessagePack-compatible lease proving ownership of one provider attempt."""

    attempt_token = Field(required=True).func(_text)
    owner_instance = Field(required=True).func(_text)
    claimed_at = Field(required=True).func(_utc_timestamp)
    lease_expires_at = Field(required=True).func(_utc_timestamp)


class RoundRecord(_StrictModel):
    """Durable round record whose state remains terminal after kiosk navigation."""

    id = Field(required=True).func(_uuid_string)
    state = Field(required=True).func(_enum(GameState))
    display_name = Field(required=True).func(_any_text)
    level = Field(default=None).func(_optional_enum(LevelGroup))
    challenge_id = Field(default=None).func(_optional_text)
    prompt = Field(default=None).func(_optional_text)
    prompt_submission_reason = Field(default=None).func(_optional_enum(PromptSubmissionReason))
    generated_artifact = Field(default=None).func(_model_or_none(ImageArtifact))
    prompt_evaluation = Field(default=None).func(_model_or_none(PromptEvaluation))
    image_evaluation = Field(default=None).func(_model_or_none(ImageMatchEvaluation))
    score = Field(default=None).func(_model_or_none(ScoreResult))
    pipeline_failure = Field(default=None).func(_model_or_none(FailureDetail))
    feedback = Field(default=list).func(_string_list)
    terminal_disposition = Field(default=None).func(_optional_enum(TerminalDisposition))
    created_at = Field(required=True).func(_utc_timestamp)
    updated_at = Field(required=True).func(_utc_timestamp)
    prompt_deadline = Field(default=None).func(_optional_utc_timestamp)
    reveal_deadline = Field(default=None).func(_optional_utc_timestamp)
    leaderboard_deadline = Field(default=None).func(_optional_utc_timestamp)
    generated_at = Field(default=None).func(_optional_utc_timestamp)
    completed_at = Field(default=None).func(_optional_utc_timestamp)


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
    "ScoreResult",
    "TerminalDisposition",
]
