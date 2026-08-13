from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

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


def test_ready_starts_a_real_round_and_reaches_prompt(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        ready = client.get("/")
        assert ready.status_code == 200
        assert "เปลี่ยนภาพที่เห็น" in ready.text
        assert 'action="/rounds"' in ready.text

        start = client.post(
            "/rounds",
            data={"display_name": "น้องฟ้า"},
            follow_redirects=False,
        )
        assert start.status_code == 303
        level_location = urlsplit(start.headers["location"])
        round_id = level_location.path.split("/")[2]
        UUID(round_id)
        assert level_location.path == f"/rounds/{round_id}/level"

        level = client.get(level_location.path)
        assert level.status_code == 200
        assert 'data-scene="level-selection"' in level.text
        assert 'name="display_name"' not in level.text

        configured = client.post(
            level_location.path,
            data={"level": "p1-p3"},
            follow_redirects=False,
        )
        assert configured.status_code == 303
        assert configured.headers["location"] == f"/rounds/{round_id}/challenge"

        challenge = client.get(configured.headers["location"])
        assert challenge.status_code == 200
        assert 'data-scene="challenge-reveal"' in challenge.text
        assert 'name="challenge_id"' not in challenge.text

        continued = client.post(
            f"/rounds/{round_id}/challenge/continue",
            data={"challenge_id": "ignored-tampering"},
            follow_redirects=False,
        )
        assert continued.status_code == 303
        assert continued.headers["location"] == f"/rounds/{round_id}/prompt"

        prompt = client.get(continued.headers["location"])
        assert prompt.status_code == 200
        assert 'data-scene="prompt-entry"' in prompt.text
        assert 'data-deadline="' in prompt.text
        assert 'name="challenge_id"' not in prompt.text


def test_persisted_setup_reaches_existing_temporary_generating_result_and_leaderboard(
    runtime_app,
) -> None:
    prompt_text = "กระต่ายเชฟทำแพนเค้กยักษ์"
    with TestClient(runtime_app) as client:
        start = client.post("/rounds", follow_redirects=False)
        round_id = urlsplit(start.headers["location"]).path.split("/")[2]
        client.post(f"/rounds/{round_id}/level", data={"level": "p1-p3"})
        client.post(f"/rounds/{round_id}/challenge/continue")

        submitted = client.post(
            f"/rounds/{round_id}/prompt",
            data={"prompt": prompt_text, "submission_reason": "manual"},
            follow_redirects=False,
        )
        assert submitted.status_code == 303
        generating_location = urlsplit(submitted.headers["location"])
        assert generating_location.path == f"/rounds/{round_id}/generating"
        assert parse_qs(generating_location.query)["prompt"] == [prompt_text]

        generating = client.get(submitted.headers["location"])
        assert generating.status_code == 200
        assert 'data-scene="generating"' in generating.text
        assert 'data-fake-generated-placeholder="true"' in generating.text

        continue_response = client.post(
            f"/rounds/{round_id}/generating/continue",
            follow_redirects=False,
        )
        assert continue_response.status_code == 303
        result_location = urlsplit(continue_response.headers["location"])
        assert result_location.path == f"/rounds/{round_id}/result"

        result = client.get(continue_response.headers["location"])
        assert result.status_code == 200
        assert 'data-scene="result"' in result.text
        assert 'data-demo-score="82"' in result.text

        leaderboard_redirect = client.post(
            f"/rounds/{round_id}/result/leaderboard",
            data={
                "challenge_id": parse_qs(generating_location.query)["challenge_id"][0],
                "prompt": prompt_text,
                "score": "82",
            },
            follow_redirects=False,
        )
        assert leaderboard_redirect.status_code == 303
        leaderboard = client.get(leaderboard_redirect.headers["location"])
        assert leaderboard.status_code == 200
        assert 'data-scene="leaderboard"' in leaderboard.text
        assert "รอบนี้ได้อันดับ 2" in leaderboard.text
