"""Strict local result for the complete AI pipeline boundary."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from dictify import Field, ListOf, Model

from app.domain.models import (
    FailureDetail,
    ImageArtifact,
    ImageMatchEvaluation,
    PipelineResultStatus,
    PromptEvaluation,
    ScoreResult,
)


def _to_primitives(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _to_primitives(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_primitives(item) for item in value]
    return value


def _status(value: Any) -> PipelineResultStatus:
    if isinstance(value, PipelineResultStatus):
        return value
    if type(value) is not str:
        raise TypeError("expected PipelineResultStatus or string")
    try:
        return PipelineResultStatus(value)
    except ValueError as error:
        raise ValueError(f"unsupported pipeline result status: {value!r}") from error


def _nested_model(model_type: type[Model], value: Any) -> Model:
    if isinstance(value, model_type):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(f"expected {model_type.__name__} or mapping")
    return model_type(value)


def _optional_model(model_type: type[Model], value: Any) -> Model | None:
    return None if value is None else _nested_model(model_type, value)


def _feedback(value: Any) -> ListOf:
    if type(value) is not list:
        raise TypeError("feedback must be a list")
    if any(type(line) is not str for line in value):
        raise TypeError("feedback must be a list of strings")
    if any(not line.strip() for line in value):
        raise ValueError("feedback lines must be non-empty")
    return ListOf(value, str)


class AIPipelineResult(Model):
    """Strict success/error result containing one complete AI attempt."""

    status = Field(required=True).func(_status)
    artifact = Field(default=None).func(lambda value: _optional_model(ImageArtifact, value))
    prompt_evaluation = Field(default=None).func(
        lambda value: _optional_model(PromptEvaluation, value)
    )
    image_evaluation = Field(default=None).func(
        lambda value: _optional_model(ImageMatchEvaluation, value)
    )
    score = Field(default=None).func(lambda value: _optional_model(ScoreResult, value))
    feedback = Field(default=list).func(_feedback)
    failure = Field(default=None).func(lambda value: _optional_model(FailureDetail, value))

    def post_validate(self) -> None:
        if self.status is PipelineResultStatus.SUCCESS:
            if any(
                value is None
                for value in (
                    self.artifact,
                    self.prompt_evaluation,
                    self.image_evaluation,
                    self.score,
                )
            ):
                raise self.Error({"result": "success result is incomplete"})
            if self.failure is not None:
                raise self.Error({"failure": "success result cannot contain failure detail"})
            if len(self.feedback) not in (2, 3):
                raise self.Error({"feedback": "success result needs two or three feedback lines"})
            return

        if self.failure is None:
            raise self.Error({"failure": "error result requires failure detail"})
        if any(
            value is not None
            for value in (
                self.artifact,
                self.prompt_evaluation,
                self.image_evaluation,
                self.score,
            )
        ):
            raise self.Error({"result": "error result cannot contain success fields"})
        if self.feedback:
            raise self.Error({"feedback": "error result cannot contain feedback"})

    def dict(self) -> dict[str, Any]:
        return _to_primitives(super().dict())


__all__ = ["AIPipelineResult"]
