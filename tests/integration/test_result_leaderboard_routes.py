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
            f"/rounds/{round_id}/result?challenge_id=attacker&prompt=attacker&score=82&level=m4-m6"
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
        assert (
            completed.leaderboard_deadline
            == (datetime.fromisoformat(completed.completed_at) + timedelta(seconds=15)).isoformat()
        )
        assert completed.generated_artifact.dict() == result_record.generated_artifact.dict()
        assert completed.score.dict() == result_record.score.dict()
        assert completed.feedback == result_record.feedback


@pytest.mark.parametrize(
    ("offset_seconds", "expected_status"),
    ((-1, 200), (0, 303), (1, 303)),
)
def test_leaderboard_get_enforces_persisted_deadline_without_mutation(
    runtime_app, offset_seconds: int, expected_status: int
) -> None:
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
        assert (
            client.post(
                f"/rounds/{round_id}/result/leaderboard",
                follow_redirects=False,
            ).status_code
            == 303
        )
        completed = _stored_record(runtime_app, round_id)
        assert completed.leaderboard_deadline is not None
        before = completed.dict()
        deadline = datetime.fromisoformat(completed.leaderboard_deadline)
        _set_clock_at(runtime_app, (deadline + timedelta(seconds=offset_seconds)).isoformat())

        response = client.get(
            f"/rounds/{round_id}/leaderboard?score=999&prompt=client-fact",
            follow_redirects=False,
        )

        assert response.status_code == expected_status
        if expected_status == 200:
            assert "client-fact" not in response.text
            assert completed.leaderboard_deadline in response.text
        else:
            expected_photo_print = f"/rounds/{round_id}/photo-print"
            assert response.headers["location"] == expected_photo_print
            reloaded = client.get(
                f"/rounds/{round_id}/leaderboard",
                follow_redirects=False,
            )
            assert reloaded.status_code == 303
            assert reloaded.headers["location"] == expected_photo_print
        assert _stored_record(runtime_app, round_id).dict() == before


def test_photo_print_route_renders_completed_round_projection_and_escapes_name(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        round_id = _start_generated(client, display_name="น้อง <ภาพ>")
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
        assert (
            client.post(
                f"/rounds/{round_id}/result/leaderboard",
                follow_redirects=False,
            ).status_code
            == 303
        )
        completed = _stored_record(runtime_app, round_id)
        assert completed.generated_artifact is not None
        assert completed.score is not None

        photo_print = client.get(f"/rounds/{round_id}/photo-print?score=1&display_name=attacker")

        assert photo_print.status_code == 200
        assert 'data-scene="photo-print"' in photo_print.text
        assert 'data-photo-print-data="completed-round-projection"' in photo_print.text
        assert f'src="{completed.generated_artifact.url}"' in photo_print.text
        assert f"{completed.score.total_score}" in photo_print.text
        assert "น้อง &lt;ภาพ&gt;" in photo_print.text
        assert "น้อง <ภาพ>" not in photo_print.text
        assert 'id="print-photo-button"' in photo_print.text
        assert "พิมพ์ภาพนี้" in photo_print.text
        assert "หากมีหน้าต่างพิมพ์" in photo_print.text
        assert "อาจแสดงหน้าต่างพิมพ์" in photo_print.text
        assert "เสร็จแล้ว เริ่มเล่นเกมใหม่" in photo_print.text
        assert "A5 landscape" in photo_print.text
        assert "object-fit: contain" in photo_print.text


def test_photo_print_frontend_contract_allows_repeat_print_after_afterprint() -> None:
    root = Path(__file__).parents[2] / "src/app/templates"
    template_source = (root / "photo_print.html").read_text(encoding="utf-8")
    script_source = (root / "photo_print.ts").read_text(encoding="utf-8")
    leaderboard_script = (root / "leaderboard.ts").read_text(encoding="utf-8")

    assert 'type="button"' in template_source
    assert 'href="/"' in template_source
    assert "data-leaderboard-deadline" not in template_source
    assert "setTimeout" not in script_source
    assert "globalThis.print()" in script_source
    assert 'addEventListener("afterprint"' in script_source
    assert "printInFlight" in script_source
    assert "printButton.disabled = true" in script_source
    assert "printButton.innerHTML = 'พิมพ์อีกครั้ง" in script_source
    assert "currentRoundId" in leaderboard_script
    assert "photoPrintUrl" in leaderboard_script
    assert "navigateToPhotoPrint" in leaderboard_script
    assert "navigateToReady" not in leaderboard_script
    assert 'location.assign(photoPrintUrl ?? "/")' in leaderboard_script


def test_photo_print_page_size_rule_is_in_document_head() -> None:
    base_source = (Path(__file__).parents[2] / "src/app/templates/_base.html").read_text(
        encoding="utf-8"
    )
    head_source = base_source.split("</head>", maxsplit=1)[0]
    shadow_source = base_source.split("<component-shell>", maxsplit=1)[1]

    assert '<style id="photo-print-page-rules">' in head_source
    assert "@page" in head_source
    assert "size: A5 landscape" in head_source
    assert head_source.count("@page") == 1
    assert "@page" not in shadow_source


def test_leaderboard_missing_deadline_remains_validation_error(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        round_id = _start_generated(client, display_name="Current")
        generated = _stored_record(runtime_app, round_id)
        assert generated.reveal_deadline is not None
        _set_clock_at(runtime_app, generated.reveal_deadline)
        assert (
            client.post(
                f"/rounds/{round_id}/generating/continue", follow_redirects=False
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/rounds/{round_id}/result/leaderboard", follow_redirects=False
            ).status_code
            == 303
        )
        completed = _stored_record(runtime_app, round_id)
        invalid = RoundRecord({**completed.dict(), "leaderboard_deadline": None})
        runtime_app.state.round_repository.replace(invalid)

        response = client.get(f"/rounds/{round_id}/leaderboard", follow_redirects=False)

        assert response.status_code == 422
        assert _stored_record(runtime_app, round_id).dict() == invalid.dict()


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
        assert leaderboard.text.count('data-leaderboard-entry="completed-round-projection"') <= 4
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


def test_public_leaderboard_is_direct_persistent_and_level_filtered(runtime_app) -> None:
    with TestClient(runtime_app) as client:
        ready = client.get("/")
        assert ready.status_code == 200
        assert 'href="/leaderboard"' in ready.text

        empty = client.get("/leaderboard")
        assert empty.status_code == 200
        assert 'data-leaderboard-mode="public"' in empty.text
        assert 'data-leaderboard-level="p1-p3"' in empty.text
        assert 'data-leaderboard-empty="true"' in empty.text
        assert "data-photo-print-url" not in empty.text
        assert "leaderboard.js" not in empty.text
        assert "data-leaderboard-countdown" not in empty.text

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
        assert (
            client.post(
                f"/rounds/{round_id}/result/leaderboard",
                follow_redirects=False,
            ).status_code
            == 303
        )
        current = _stored_record(runtime_app, round_id)
        repository = runtime_app.state.round_repository
        scores = (100, 92, 92, 80, 70)
        for index, score in enumerate(scores, start=1):
            repository.create(
                _completed_copy(
                    current,
                    name=f"Top {index}",
                    score=score,
                    prompt=f"public prompt {index}",
                    image_url=f"/generated/public-{index}.webp",
                    completed_at=f"2026-01-01T00:00:{index:02d}+00:00",
                )
            )
        repository.create(
            _completed_copy(
                current,
                name="Other level",
                score=99,
                prompt="other level public prompt",
                image_url="/generated/other-public.webp",
                level=LevelGroup.P4_P6,
            )
        )
        assert current.leaderboard_deadline is not None
        expired = datetime.fromisoformat(current.leaderboard_deadline) + timedelta(minutes=5)
        _set_clock_at(runtime_app, expired.isoformat())

        leaderboard = client.get("/leaderboard?level=p1-p3")

        assert leaderboard.status_code == 200
        assert 'data-leaderboard-data="public-level-projection"' in leaderboard.text
        assert leaderboard.text.count('data-leaderboard-entry="public-level-projection"') == 4
        assert [f"Top {index}" in leaderboard.text for index in range(1, 5)] == [
            True,
            True,
            True,
            True,
        ]
        assert "Top 5" not in leaderboard.text
        assert "Other level" not in leaderboard.text
        assert 'data-entry-rank="1"' in leaderboard.text
        assert leaderboard.text.count('data-entry-rank="2"') == 2
        assert 'data-entry-rank="4"' in leaderboard.text
        assert 'aria-current="page"' in leaderboard.text
        assert "leaderboard.js" not in leaderboard.text
        assert "data-leaderboard-countdown" not in leaderboard.text

        other_level = client.get("/leaderboard?level=p4-p6")
        assert other_level.status_code == 200
        assert "Other level" in other_level.text
        assert "Top 1" not in other_level.text
        assert client.get("/leaderboard?level=invalid").status_code == 422


def test_leaderboard_frontend_contract_is_single_screen_without_list_clipping() -> None:
    base_template = Path(__file__).parents[2] / "src/app/templates/_base.html"
    leaderboard_template = Path(__file__).parents[2] / "src/app/templates/leaderboard.html"
    base_source = base_template.read_text(encoding="utf-8")
    leaderboard_source = leaderboard_template.read_text(encoding="utf-8")
    scene_rule = base_source[
        base_source.index("        .scene {") : base_source.index(
            "        .scene::before", base_source.index("        .scene {")
        )
    ]
    list_rule = base_source[
        base_source.index("        .leaderboard-list {") : base_source.index(
            "        .leaderboard-row {", base_source.index("        .leaderboard-list {")
        )
    ]

    assert "width: min(100vw, calc(100vh * 16 / 9));" in scene_rule
    assert "height: min(100vh, calc(100vw * 9 / 16));" in scene_rule
    assert "grid-template-rows: repeat(4" in list_rule
    assert "overflow" not in list_rule
    assert 'data-leaderboard-deadline="{{ leaderboard_deadline }}"' in leaderboard_source
    assert 'data-current-round="{{ round_id }}"' in leaderboard_source
    assert "photo-print" not in leaderboard_source
    assert "public-level-projection" in leaderboard_source
    assert "data-leaderboard-countdown" in leaderboard_source


def test_result_and_leaderboard_routes_map_missing_and_stale_rounds(runtime_app) -> None:
    missing_id = str(uuid4())
    with TestClient(runtime_app) as client:
        assert client.get(f"/rounds/{missing_id}/result").status_code == 404
        assert client.post(f"/rounds/{missing_id}/result/leaderboard").status_code == 404
        assert client.get(f"/rounds/{missing_id}/leaderboard").status_code == 404
        assert client.get(f"/rounds/{missing_id}/photo-print").status_code == 404
        assert client.post(f"/rounds/{missing_id}/generating/continue").status_code == 404

        started = client.post("/rounds", data={"display_name": "Tester"}, follow_redirects=False)
        round_id = urlsplit(started.headers["location"]).path.split("/")[2]
        assert client.get(f"/rounds/{round_id}/result").status_code == 409
        assert client.post(f"/rounds/{round_id}/result/leaderboard").status_code == 409
        assert client.get(f"/rounds/{round_id}/leaderboard").status_code == 409
        assert client.get(f"/rounds/{round_id}/photo-print").status_code == 409
        assert client.post(f"/rounds/{round_id}/generating/continue").status_code == 409
