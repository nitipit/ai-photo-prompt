from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.ai.pipeline import FakeAIPipeline
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


def _start_generating(client: TestClient) -> str:
    started = client.post("/rounds", follow_redirects=False)
    assert started.status_code == 303
    round_id = urlsplit(started.headers["location"]).path.split("/")[2]
    configured = client.post(
        f"/rounds/{round_id}/level",
        data={"level": "p1-p3"},
        follow_redirects=False,
    )
    assert configured.status_code == 303
    continued = client.post(
        f"/rounds/{round_id}/challenge/continue",
        data={"challenge_id": "ignored"},
        follow_redirects=False,
    )
    assert continued.status_code == 303
    submitted = client.post(
        f"/rounds/{round_id}/prompt",
        data={"prompt": "เด็กวาดภาพในสวน", "submission_reason": "manual"},
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    return round_id


def test_generation_get_and_run_use_only_persisted_round(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        round_id = _start_generating(client)

        waiting = client.get(
            f"/rounds/{round_id}/generating?challenge_id=attacker&prompt=attacker&failure=1"
        )
        assert waiting.status_code == 200
        assert 'data-generating-state="waiting"' in waiting.text
        assert "attacker" not in waiting.text
        assert f'action="/rounds/{round_id}/generating/run"' in waiting.text
        assert 'name="challenge_id"' not in waiting.text
        assert 'name="prompt"' not in waiting.text

        run = client.post(
            f"/rounds/{round_id}/generating/run",
            data={"challenge_id": "attacker", "prompt": "attacker"},
            follow_redirects=False,
        )
        assert run.status_code == 303
        assert run.headers["location"] == f"/rounds/{round_id}/generating"

        generated = _stored_record(runtime_app, round_id)
        assert generated.state is GameState.GENERATED_REVEAL
        assert generated.generated_artifact is not None
        assert generated.generated_artifact.provider == "fake-ai"
        assert generated.score is not None
        assert generated.reveal_deadline is not None

        reveal = client.get(f"/rounds/{round_id}/generating?challenge_id=attacker&prompt=attacker")
        assert reveal.status_code == 200
        assert f'src="{generated.generated_artifact.url}"' in reveal.text
        assert f'data-generated-score="{generated.score.total_score}"' in reveal.text
        assert "fake-ai" in reveal.text
        assert generated.reveal_deadline in reveal.text
        assert 'name="challenge_id"' not in reveal.text
        assert 'name="prompt"' not in reveal.text


def test_generation_failure_has_no_result_and_retry_runs_service_again(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        round_id = _start_generating(client)
        runtime_app.state.game_round_service._pipeline = FakeAIPipeline(fail=True)

        run = client.post(f"/rounds/{round_id}/generating/run", follow_redirects=False)
        assert run.status_code == 303
        failure = client.get(run.headers["location"])
        assert failure.status_code == 200
        assert 'data-generating-state="failure"' in failure.text
        assert "เกิดข้อผิดพลาดชั่วคราว" in failure.text
        assert 'action="/rounds/' + round_id + '/generating/retry"' in failure.text
        assert 'name="challenge_id"' not in failure.text
        assert 'name="prompt"' not in failure.text

        record = _stored_record(runtime_app, round_id)
        assert record.state is GameState.GENERATING
        assert record.pipeline_failure is not None
        assert record.score is None
        assert (
            client.post(
                f"/rounds/{round_id}/generating/continue", follow_redirects=False
            ).status_code
            == 409
        )
        assert client.get(f"/rounds/{round_id}/result").status_code == 409

        runtime_app.state.game_round_service._pipeline = FakeAIPipeline()
        retry = client.post(
            f"/rounds/{round_id}/generating/retry",
            data={"challenge_id": "attacker", "prompt": "attacker"},
            follow_redirects=False,
        )
        assert retry.status_code == 303
        retried = _stored_record(runtime_app, round_id)
        assert retried.state is GameState.GENERATED_REVEAL
        assert retried.pipeline_failure is None
        assert retried.score is not None


def test_exit_abandons_generation_and_rejects_late_scene_actions(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        round_id = _start_generating(client)
        runtime_app.state.game_round_service._pipeline = FakeAIPipeline(fail=True)
        assert (
            client.post(f"/rounds/{round_id}/generating/run", follow_redirects=False).status_code
            == 303
        )

        exited = client.post(
            f"/rounds/{round_id}/generating/exit",
            data={"challenge_id": "attacker", "prompt": "attacker"},
            follow_redirects=False,
        )
        assert exited.status_code == 303
        assert exited.headers["location"] == "/"

        abandoned = _stored_record(runtime_app, round_id)
        assert abandoned.state is GameState.ABANDONED
        assert abandoned.terminal_disposition is TerminalDisposition.ABANDONED
        assert abandoned.score is None
        assert abandoned.generated_artifact is None
        assert client.get(f"/rounds/{round_id}/generating").status_code == 409
        assert (
            client.post(f"/rounds/{round_id}/generating/run", follow_redirects=False).status_code
            == 409
        )


def test_generation_run_maps_missing_and_stale_rounds(runtime_app) -> None:
    unknown_id = str(uuid4())
    with TestClient(runtime_app) as client:
        assert (
            client.post(f"/rounds/{unknown_id}/generating/run", follow_redirects=False).status_code
            == 404
        )

        started = client.post("/rounds", follow_redirects=False)
        round_id = urlsplit(started.headers["location"]).path.split("/")[2]
        stale = client.post(f"/rounds/{round_id}/generating/run", follow_redirects=False)
        assert stale.status_code == 409
        assert client.get(f"/rounds/{round_id}/generating").status_code == 409


def test_already_running_generation_is_a_stable_conflict(runtime_app) -> None:
    class BlockingPipeline:
        def __init__(self) -> None:
            self.started = Event()
            self.release = Event()

        async def run(self, challenge, prompt: str, timeout: float):
            del prompt, timeout
            self.started.set()
            await asyncio.to_thread(self.release.wait)
            return await FakeAIPipeline().run(challenge, "prompt", 1.0)

    with TestClient(runtime_app) as client:
        round_id = _start_generating(client)
        pipeline = BlockingPipeline()
        runtime_app.state.game_round_service._pipeline = pipeline
        first_response = []

        def run_first_attempt() -> None:
            first_response.append(
                client.post(f"/rounds/{round_id}/generating/run", follow_redirects=False)
            )

        first = Thread(target=run_first_attempt)
        first.start()
        assert pipeline.started.wait(timeout=5)

        second = client.post(f"/rounds/{round_id}/generating/run", follow_redirects=False)
        assert second.status_code == 409
        assert second.json()["detail"] == "Generation already running"

        pipeline.release.set()
        first.join(timeout=5)
        assert not first.is_alive()
        assert first_response[0].status_code == 303
