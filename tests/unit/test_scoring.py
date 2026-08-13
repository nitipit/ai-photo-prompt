from __future__ import annotations

import pytest

from app.domain.models import ImageMatchEvaluation, PromptEvaluation
from app.domain.scoring import score_image, score_prompt, score_total


def test_prompt_and_image_scores_use_the_locked_weights() -> None:
    prompt = PromptEvaluation(clarity=100, specificity=50, relationship=25, consistency=0)
    image = ImageMatchEvaluation(core_concept=90, supporting_details=50, scene_coherence=20)

    assert score_prompt(prompt) == pytest.approx(60.0)
    assert score_image(image) == pytest.approx(75.0)

    result = score_total(prompt, image)
    assert result.prompt_score == pytest.approx(60.0)
    assert result.image_score == pytest.approx(75.0)
    assert result.total_score == 68


def test_only_the_final_total_is_rounded_half_up_and_stays_bounded() -> None:
    half = score_total(
        PromptEvaluation(clarity=0, specificity=0, relationship=0, consistency=10),
        ImageMatchEvaluation(core_concept=0, supporting_details=0, scene_coherence=0),
    )
    assert half.prompt_score == pytest.approx(1.0)
    assert half.image_score == pytest.approx(0.0)
    assert half.total_score == 1

    minimum = score_total(
        PromptEvaluation(clarity=0, specificity=0, relationship=0, consistency=0),
        ImageMatchEvaluation(core_concept=0, supporting_details=0, scene_coherence=0),
    )
    maximum = score_total(
        PromptEvaluation(clarity=100, specificity=100, relationship=100, consistency=100),
        ImageMatchEvaluation(core_concept=100, supporting_details=100, scene_coherence=100),
    )
    assert minimum.total_score == 0
    assert maximum.total_score == 100
    assert 0 <= minimum.total_score <= 100
    assert 0 <= maximum.total_score <= 100
