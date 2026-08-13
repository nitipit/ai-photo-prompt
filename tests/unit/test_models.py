from __future__ import annotations

from uuid import uuid4

import pytest
from dictify import Model

from app.domain.models import (
    ChallengeSpec,
    GameState,
    ImageArtifact,
    PipelineResult,
    PipelineResultStatus,
    RoundRecord,
)


def challenge_data() -> dict[str, object]:
    return {
        "id": "p1-p3-01",
        "title": "A challenge",
        "level": "p1-p3",
        "target_asset_url": "/assets/challenges/p1-p3-01.webp",
        "concept": "A concept",
        "core_anchors": ["A core anchor"],
        "optional_details": ["A detail"],
        "example_prompt": "A prompt",
        "evaluation_notes": "Notes",
        "feedback_focus": "Feedback",
    }


def test_challenge_spec_is_strict_and_reconstructs_enum_values() -> None:
    spec = ChallengeSpec(challenge_data())

    assert spec.level.value == "p1-p3"
    assert spec.status.value == "approved"
    assert spec.dict()["level"] == "p1-p3"
    assert spec.dict()["status"] == "approved"

    with pytest.raises(Model.Error):
        ChallengeSpec({**challenge_data(), "unexpected": True})
    with pytest.raises(Model.Error):
        ChallengeSpec({**challenge_data(), "core_anchors": ["ok", 3]})
    with pytest.raises(Model.Error):
        ChallengeSpec({**challenge_data(), "core_anchors": []})
    with pytest.raises(Model.Error):
        ChallengeSpec({**challenge_data(), "level": "primary"})
    with pytest.raises(Model.Error):
        ChallengeSpec({**challenge_data(), "target_asset_url": "/tmp/target.webp"})


def test_round_record_serializes_nested_records_to_messagepack_values(
    round_record: RoundRecord,
) -> None:
    round_record.generated_artifact = ImageArtifact(url="/generated/image.webp")
    round_record.state = GameState.PROMPT_ENTRY
    data = round_record.dict()

    assert data["state"] == "prompt_entry"
    assert data["generated_artifact"] == {
        "url": "/generated/image.webp",
        "mime_type": "image/webp",
        "provider": None,
        "width": None,
        "height": None,
    }
    rebuilt = RoundRecord(data)
    assert rebuilt.generated_artifact.url == "/generated/image.webp"
    assert rebuilt.state is GameState.PROMPT_ENTRY


def test_round_record_rejects_invalid_nested_and_timestamp_data(round_record: RoundRecord) -> None:
    with pytest.raises(Model.Error):
        RoundRecord({**round_record.dict(), "generated_artifact": {"url": "/x", "extra": 1}})
    with pytest.raises(Model.Error):
        RoundRecord({**round_record.dict(), "created_at": "not-a-timestamp"})
    with pytest.raises(Model.Error):
        RoundRecord({**round_record.dict(), "id": "round-1"})


def test_pipeline_result_is_a_strict_tagged_envelope() -> None:
    success = PipelineResult(
        status=PipelineResultStatus.SUCCESS,
        artifact={"url": "/generated/image.webp"},
    )
    assert success.dict()["status"] == "success"

    with pytest.raises(Model.Error):
        PipelineResult(status="success")
    with pytest.raises(Model.Error):
        PipelineResult(status="error", artifact={"url": "/generated/image.webp"})
    with pytest.raises(Model.Error):
        PipelineResult(
            status="error",
            failure={"code": "provider", "message": "failed"},
            artifact={"url": "/generated/image.webp"},
        )


def test_round_id_must_be_a_uuid() -> None:
    assert RoundRecord(
        id=str(uuid4()),
        state="level_selection",
        display_name="Name",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    ).id
