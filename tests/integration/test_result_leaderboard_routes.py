from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.models import (
    GameState,
    ImageArtifact,
    LevelGroup,
    RoundRecord,
    ScoreResult,
    TerminalDisposition,
)
from app.server import app


@pytest.fixture
def runtime_app(tmp_path: Path, materialized_catalog):
    app.state.db_path = tmp_path / "runtime.shelfdb"
    app.state.catalog_path = materialized_catalog.catalog_path
    try:
        yield app
    finally:
        for name in ("db_path", "catalog_path"):
            if hasattr(app.state, name):
                delattr(app.state, name)


def _stored_record(runtime_app, round_id: str) -> RoundRecord:
    return asyncio.run(runtime_app.state.game_round_service.get_round(round_id))


def _start_generated(client: TestClient, *, display_name: str, level: str = "p1-p3") -> str:
    started = client.post(
        "/rounds",
        data={"display_name": display_name},
        follow_redirects=False,
    )
    assert started.status_code == 303
    round_id = urlsplit(started.headers["location"]).path.split("/")[2]
    assert (
        client.post(
            f"/rounds/{round_id}/level",
            data={"level": level},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/rounds/{round_id}/challenge/continue",
            follow_redirects=False,
        ).status_code
        == 303
    )
    submitted = client.post(
        f"/rounds/{round_id}/prompt",
        data={"prompt": f"prompt for {display_name}", "submission_reason": "manual"},
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    assert urlsplit(submitted.headers["location"]).query == ""
    run = client.post(
        f"/rounds/{round_id}/generating/run",
        data={"prompt": "tampered", "challenge_id": "tampered"},
        follow_redirects=False,
    )
    assert run.status_code == 303
    return round_id


def _set_clock_at(runtime_app, timestamp: str) -> None:
    runtime_app.state.game_round_service._utc_clock = lambda: datetime.fromisoformat(timestamp)


def _completed_copy(
    base: RoundRecord,
    *,
    name: str,
    score: int,
    prompt: str,
    image_url: str,
    level: LevelGroup | None = None,
    completed_at: str = "2026-01-01T00:00:20+00:00",
) -> RoundRecord:
    selected_level = level or base.level
    assert selected_level is not None
    challenge_id = base.challenge_id
    if selected_level is not base.level:
        challenge_id = f"{selected_level.value}-00"
    return RoundRecord(
        {
            **base.dict(),
            "id": str(uuid4()),
            "display_name": name,
            "level": selected_level.value,
            "challenge_id": challenge_id,
            "prompt": prompt,
            "generated_artifact": ImageArtifact(
                url=image_url,
                provider="stored-provider",
            ).dict(),
            "score": ScoreResult(
                prompt_score=score,
                image_score=score,
                total_score=score,
            ).dict(),
            "state": GameState.LEADERBOARD.value,
            "terminal_disposition": TerminalDisposition.COMPLETED.value,
            "completed_at": completed_at,
            "updated_at": completed_at,
            "leaderboard_deadline": (
                datetime.fromisoformat(completed_at) + timedelta(seconds=15)
            ).isoformat(),
        }
    )


def test_result_flow_uses_persisted_lifecycle_and_ignores_tampering(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        round_id = _start_generated(client, display_name="Current")
        generated = _stored_record(runtime_app, round_id)
        assert generated.state is GameState.GENERATED_REVEAL
        assert generated.reveal_deadline is not None
        before = generated.dict()

        early = client.post(
            f"/rounds/{round_id}/generating/continue",
            data={"challenge_id": "attacker", "prompt": "attacker"},
            follow_redirects=False,
        )
        assert early.status_code == 422
        assert "reveal deadline has not elapsed" in early.json()["detail"]
        assert _stored_record(runtime_app, round_id).dict() == before

        _set_clock_at(runtime_app, generated.reveal_deadline)
        continued = client.post(
            f"/rounds/{round_id}/generating/continue",
            data={"challenge_id": "attacker", "prompt": "attacker"},
            follow_redirects=False,
        )
        assert continued.status_code == 303
        assert continued.headers["location"] == f"/rounds/{round_id}/result"

        result_record = _stored_record(runtime_app, round_id)
        assert result_record.state is GameState.RESULT
        assert result_record.generated_artifact is not None
        assert result_record.score is not None
        assert len(result_record.feedback) == 3

        result = client.get(
            f"/rounds/{round_id}/result"
            "?challenge_id=attacker&prompt=attacker&score=82&level=m4-m6"
        )
        assert result.status_code == 200
        assert f'src="{result_record.generated_artifact.url}"' in result.text
        assert f"{result_record.score.total_score}" in result.text
        assert all(line in result.text for line in result_record.feedback)
        assert "attacker" not in result.text
        assert "deterministic-demo" not in result.text
        assert 'name="challenge_id"' not in result.text
        assert 'name="prompt"' not in result.text
        assert 'name="score"' not in result.text

        completed_response = client.post(
            f"/rounds/{round_id}/result/leaderboard",
            data={
                "challenge_id": "attacker",
                "prompt": "attacker",
                "score": "82",
                "level": "m4-m6",
            },
            follow_redirects=False,
        )
        assert completed_response.status_code == 303
        assert completed_response.headers["location"] == f"/rounds/{round_id}/leaderboard"

        completed = _stored_record(runtime_app, round_id)
        assert completed.state is GameState.LEADERBOARD
        assert completed.terminal_disposition is TerminalDisposition.COMPLETED
        assert completed.completed_at is not None
        assert completed.leaderboard_deadline == (
            datetime.fromisoformat(completed.completed_at) + timedelta(seconds=15)
        ).isoformat()
        assert completed.generated_artifact.dict() == result_record.generated_artifact.dict()
        assert completed.score.dict() == result_record.score.dict()
        assert completed.feedback == result_record.feedback


def test_leaderboard_route_renders_only_completed_current_level_projection(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        round_id = _start_generated(client, display_name="Current")
        generated = _stored_record(runtime_app, round_id)
        assert generated.reveal_deadline is not None
        _set_clock_at(runtime_app, generated.reveal_deadline)
        assert (
            client.post(
                f"/rounds/{round_id}/generating/continue",
                follow_redirects=False,
            ).status_code
            == 303
        )
        result_record = _stored_record(runtime_app, round_id)
        assert result_record.state is GameState.RESULT
        assert (
            client.post(
                f"/rounds/{round_id}/result/leaderboard",
                follow_redirects=False,
            ).status_code
            == 303
        )
        current = _stored_record(runtime_app, round_id)

        repository = runtime_app.state.round_repository
        top = _completed_copy(
            current,
            name="Top",
            score=96,
            prompt="top persisted prompt",
            image_url="/generated/top.webp",
            completed_at="2026-01-01T00:00:01+00:00",
        )
        tie = _completed_copy(
            current,
            name="Tie",
            score=79,
            prompt="tie persisted prompt with every detail",
            image_url="/generated/tie.webp",
            completed_at="2026-01-01T00:00:02+00:00",
        )
        other_level = _completed_copy(
            current,
            name="Other level",
            score=100,
            prompt="other level prompt",
            image_url="/generated/other.webp",
            level=LevelGroup.P4_P6,
            completed_at="2026-01-01T00:00:03+00:00",
        )
        abandoned = _completed_copy(
            current,
            name="Abandoned",
            score=100,
            prompt="abandoned prompt",
            image_url="/generated/abandoned.webp",
            completed_at="2026-01-01T00:00:04+00:00",
        )
        abandoned = RoundRecord(
            {
                **abandoned.dict(),
                "state": GameState.ABANDONED.value,
                "terminal_disposition": TerminalDisposition.ABANDONED.value,
                "completed_at": None,
                "leaderboard_deadline": None,
            }
        )
        for record in (top, tie, other_level, abandoned):
            repository.create(record)

        leaderboard = client.get(
            f"/rounds/{round_id}/leaderboard"
            "?level=m4-m6&score=1&prompt=attacker&challenge_id=attacker"
        )
        assert leaderboard.status_code == 200
        assert 'data-leaderboard-data="completed-round-projection"' in leaderboard.text
        assert 'data-leaderboard-level="p1-p3"' in leaderboard.text
        assert 'data-leaderboard-deadline="' in leaderboard.text
        assert 'data-current-rank="2"' in leaderboard.text
        assert 'data-entry-rank="1"' in leaderboard.text
        assert leaderboard.text.count('data-current-entry="true"') == 2
        assert 'data-current-entry="true"' in leaderboard.text
        assert "tie persisted prompt with every detail" in leaderboard.text
        assert "/generated/tie.webp" in leaderboard.text
        assert "/generated/other.webp" not in leaderboard.text
        assert "/generated/abandoned.webp" not in leaderboard.text
        assert "attacker" not in leaderboard.text
        assert "deterministic-demo" not in leaderboard.text
        assert 'data-image-source="persisted-generated-artifact"' in leaderboard.text
        assert 'data-ranking="competition-rank"' in leaderboard.text


def test_result_and_leaderboard_routes_map_missing_and_stale_rounds(runtime_app) -> None:
    missing_id = str(uuid4())
    with TestClient(runtime_app) as client:
        assert client.get(f"/rounds/{missing_id}/result").status_code == 404
        assert client.post(f"/rounds/{missing_id}/result/leaderboard").status_code == 404
        assert client.get(f"/rounds/{missing_id}/leaderboard").status_code == 404
        assert client.post(f"/rounds/{missing_id}/generating/continue").status_code == 404

        started = client.post("/rounds", follow_redirects=False)
        round_id = urlsplit(started.headers["location"]).path.split("/")[2]
        assert client.get(f"/rounds/{round_id}/result").status_code == 409
        assert client.post(f"/rounds/{round_id}/result/leaderboard").status_code == 409
        assert client.get(f"/rounds/{round_id}/leaderboard").status_code == 409
        assert client.post(f"/rounds/{round_id}/generating/continue").status_code == 409
