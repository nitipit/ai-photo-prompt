"""Pure, level-independent scoring policy for prompts and generated images."""

from __future__ import annotations

from math import floor

from .models import ImageMatchEvaluation, PromptEvaluation, ScoreResult

PROMPT_WEIGHTS = {
    "clarity": 0.40,
    "specificity": 0.30,
    "relationship": 0.20,
    "consistency": 0.10,
}
IMAGE_WEIGHTS = {
    "core_concept": 0.70,
    "supporting_details": 0.20,
    "scene_coherence": 0.10,
}


def score_prompt(evaluation: PromptEvaluation) -> float:
    """Return the unrounded weighted prompt score from 0 to 100."""

    return (
        evaluation.clarity * PROMPT_WEIGHTS["clarity"]
        + evaluation.specificity * PROMPT_WEIGHTS["specificity"]
        + evaluation.relationship * PROMPT_WEIGHTS["relationship"]
        + evaluation.consistency * PROMPT_WEIGHTS["consistency"]
    )


def score_image(evaluation: ImageMatchEvaluation) -> float:
    """Return the unrounded weighted image-match score from 0 to 100."""

    return (
        evaluation.core_concept * IMAGE_WEIGHTS["core_concept"]
        + evaluation.supporting_details * IMAGE_WEIGHTS["supporting_details"]
        + evaluation.scene_coherence * IMAGE_WEIGHTS["scene_coherence"]
    )


def score_total(prompt: PromptEvaluation, image: ImageMatchEvaluation) -> ScoreResult:
    """Return weighted components and the visible 50/50 score.

    Component scores remain at their full floating-point precision.  The visible
    total uses non-negative half-up rounding: ``floor(value + 0.5)``.
    """

    prompt_score = score_prompt(prompt)
    image_score = score_image(image)
    total = (prompt_score + image_score) / 2
    return ScoreResult(
        prompt_score=prompt_score,
        image_score=image_score,
        total_score=floor(total + 0.5),
    )


__all__ = ["score_image", "score_prompt", "score_total"]
