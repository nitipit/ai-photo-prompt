from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.models import GameState, TerminalDisposition
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


def _stored_record(runtime_app, round_id: str):
    return asyncio.run(runtime_app.state.game_round_service.get_round(round_id))


def _start_round(client: TestClient, *, display_name: str = "") -> str:
    response = client.post(
        "/rounds",
        data={"display_name": display_name},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path.endswith("/level")
    round_id = location.path.split("/")[2]
    UUID(round_id)
    return round_id


def _configure_round(client: TestClient, round_id: str, level: str = "p1-p3") -> str:
    response = client.post(
        f"/rounds/{round_id}/level",
        data={"level": level},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/rounds/{round_id}/challenge"
    return response.headers["location"]


def _continue_to_prompt(client: TestClient, round_id: str) -> str:
    response = client.post(
        f"/rounds/{round_id}/challenge/continue",
        data={"challenge_id": "tampered-by-client"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/rounds/{round_id}/prompt"
    return response.headers["location"]


def test_persisted_round_flow_stores_uuid_name_level_challenge_and_deadline(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        ready = client.get("/")
        assert ready.status_code == 200
        assert 'action="/rounds"' in ready.text
        assert 'name="display_name"' in ready.text

        round_id = _start_round(client, display_name="  น้องฟ้า  ")
        created = _stored_record(runtime_app, round_id)
        assert created.id == round_id
        assert created.state is GameState.LEVEL_SELECTION
        assert created.display_name == "น้องฟ้า"
        assert created.challenge_id is None

        level = client.get(f"/rounds/{round_id}/level")
        assert level.status_code == 200
        assert 'name="display_name"' not in level.text
        assert 'name="level"' in level.text

        challenge_url = _configure_round(client, round_id, "p4-p6")
        configured = _stored_record(runtime_app, round_id)
        assert configured.state is GameState.CHALLENGE_REVEAL
        assert configured.level.value == "p4-p6"
        assert configured.challenge_id is not None
        selected = runtime_app.state.catalog.get(configured.challenge_id)
        assert selected.level is configured.level
        assert selected.status.value == "approved"

        challenge = client.get(f"{challenge_url}?challenge_id=not-authoritative")
        assert challenge.status_code == 200
        assert f'src="{selected.target_asset_url}"' in challenge.text
        assert 'name="challenge_id"' not in challenge.text

        prompt_url = _continue_to_prompt(client, round_id)
        prompt_state = _stored_record(runtime_app, round_id)
        assert prompt_state.state is GameState.PROMPT_ENTRY
        assert prompt_state.prompt_deadline is not None

        prompt = client.get(f"{prompt_url}?challenge_id=not-authoritative")
        assert prompt.status_code == 200
        assert f'src="{selected.target_asset_url}"' in prompt.text
        assert f'data-deadline="{prompt_state.prompt_deadline}"' in prompt.text
        assert 'name="challenge_id"' not in prompt.text


def test_setup_routes_require_authoritative_scene_state_and_unknown_round_is_404(
    runtime_app,
) -> None:
    unknown_id = str(uuid4())
    with TestClient(runtime_app) as client:
        assert client.get(f"/rounds/{unknown_id}/level").status_code == 404
        assert client.get(f"/rounds/{unknown_id}/challenge").status_code == 404
        assert client.get(f"/rounds/{unknown_id}/prompt").status_code == 404

        round_id = _start_round(client)
        assert client.get(f"/rounds/{round_id}/challenge").status_code == 409
        assert client.post(f"/rounds/{round_id}/challenge/continue").status_code == 409

        _configure_round(client, round_id)
        assert client.get(f"/rounds/{round_id}/level").status_code == 409
        assert client.get(f"/rounds/{round_id}/prompt").status_code == 409
        assert client.post(f"/rounds/{round_id}/level", data={"level": "p1-p3"}).status_code == 409

        _continue_to_prompt(client, round_id)
        assert client.get(f"/rounds/{round_id}/challenge").status_code == 409


def test_prompt_is_stored_exactly_and_generating_ignores_tampered_context(runtime_app) -> None:
    prompt_text = "  กระต่ายเชฟ & สีฟ้า + 50%  "
    with TestClient(runtime_app) as client:
        round_id = _start_round(client)
        _configure_round(client, round_id)
        _continue_to_prompt(client, round_id)
        configured = _stored_record(runtime_app, round_id)
        selected = runtime_app.state.catalog.get(configured.challenge_id)

        submitted = client.post(
            f"/rounds/{round_id}/prompt?challenge_id=wrong-query",
            data={
                "challenge_id": "wrong-form",
                "prompt": prompt_text,
                "submission_reason": "manual",
            },
            follow_redirects=False,
        )
        assert submitted.status_code == 303
        location = urlsplit(submitted.headers["location"])
        assert location.path == f"/rounds/{round_id}/generating"
        assert parse_qs(location.query) == {
            "challenge_id": [selected.id],
            "prompt": [prompt_text],
        }

        stored = _stored_record(runtime_app, round_id)
        assert stored.state is GameState.GENERATING
        assert stored.challenge_id == selected.id
        assert stored.prompt == prompt_text

        generating = client.get(f"{location.path}?challenge_id=wrong-query&prompt=wrong-query")
        assert generating.status_code == 200
        assert f'src="{selected.target_asset_url}"' in generating.text
        assert f'value="{escape(prompt_text)}"' in generating.text
        assert "wrong-query" not in generating.text


def test_blank_manual_and_early_blank_timeout_are_422_without_mutation(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        round_id = _start_round(client)
        _configure_round(client, round_id)
        _continue_to_prompt(client, round_id)

        blank_manual = client.post(
            f"/rounds/{round_id}/prompt",
            data={"prompt": "   ", "submission_reason": "manual"},
        )
        assert blank_manual.status_code == 422

        early_timeout = client.post(
            f"/rounds/{round_id}/prompt",
            data={"prompt": "", "submission_reason": "timeout"},
        )
        assert early_timeout.status_code == 422
        current = _stored_record(runtime_app, round_id)
        assert current.state is GameState.PROMPT_ENTRY
        assert current.terminal_disposition is None


def test_elapsed_blank_timeout_abandons_round_using_service_clock(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        round_id = _start_round(client)
        _configure_round(client, round_id)
        _continue_to_prompt(client, round_id)
        before_timeout = _stored_record(runtime_app, round_id)
        deadline = datetime.fromisoformat(before_timeout.prompt_deadline)
        runtime_app.state.game_round_service._utc_clock = lambda: (
            deadline.astimezone(UTC) + timedelta(seconds=1)
        )

        response = client.post(
            f"/rounds/{round_id}/prompt",
            data={"prompt": "", "submission_reason": "timeout"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"

        abandoned = _stored_record(runtime_app, round_id)
        assert abandoned.state is GameState.ABANDONED
        assert abandoned.terminal_disposition is TerminalDisposition.ABANDONED
        assert abandoned.completed_at is not None
