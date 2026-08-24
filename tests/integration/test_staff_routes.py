from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.ai import FakeAIPipeline
from app.domain.models import (
    GameState,
    ImageArtifact,
    ImageMatchEvaluation,
    LevelGroup,
    PromptEvaluation,
    RoundRecord,
    ScoreResult,
    TerminalDisposition,
)
from app.server import app

_PIN = "123456"
_TIMESTAMP = "2026-01-01T00:00:00+00:00"
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')


@pytest.fixture
def staff_app(tmp_path: Path, materialized_catalog, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PHOTO_PROMPT_STAFF_PIN", _PIN)
    app.state.db_path = tmp_path / "staff.shelfdb"
    app.state.catalog_path = materialized_catalog.catalog_path
    app.state.generated_root = tmp_path / "generated"
    app.state.ai_pipeline = FakeAIPipeline()
    yield app
    for name in ("db_path", "catalog_path", "generated_root", "ai_pipeline"):
        if hasattr(app.state, name):
            delattr(app.state, name)


def _add_round(
    application,
    name: str,
    *,
    image_available: bool = False,
) -> str:
    round_id = uuid4()
    attempt_id = uuid4()
    url = f"/generated/{round_id}/{attempt_id}.png"
    if image_available:
        root = application.state.generated_root / str(round_id)
        root.mkdir(parents=True)
        (root / f"{attempt_id}.png").write_bytes(b"not-a-real-png-for-route-test")
    application.state.round_repository.create(
        RoundRecord(
            id=str(round_id),
            state=GameState.LEADERBOARD,
            display_name=name,
            level=LevelGroup.P1_P3,
            challenge_id="p1-p3-01",
            prompt="private prompt must not render",
            generated_artifact=ImageArtifact(url=url, mime_type="image/png"),
            prompt_evaluation=PromptEvaluation(
                clarity=70,
                specificity=70,
                relationship=70,
                consistency=70,
            ),
            image_evaluation=ImageMatchEvaluation(
                core_concept=70,
                supporting_details=70,
                scene_coherence=70,
            ),
            score=ScoreResult(prompt_score=70, image_score=70, total_score=70),
            feedback=["ดี", "เพิ่มรายละเอียด"],
            terminal_disposition=TerminalDisposition.COMPLETED,
            created_at=_TIMESTAMP,
            updated_at=_TIMESTAMP,
            completed_at=_TIMESTAMP,
            leaderboard_deadline=_TIMESTAMP,
        )
    )
    return str(round_id)


def _login(client: TestClient) -> str:
    page = client.get("/staff/login")
    assert page.status_code == 200
    login_csrf = _CSRF.search(page.text)
    assert login_csrf is not None
    response = client.post(
        "/staff/login",
        data={"pin": _PIN, "csrf": login_csrf.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    search = client.get("/staff/search")
    token = _CSRF.search(search.text)
    assert token is not None
    return token.group(1)


def test_invalid_pin_hides_staff_and_unauthenticated_routes_are_unavailable(
    staff_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHOTO_PROMPT_STAFF_PIN")
    with TestClient(staff_app, base_url="https://testserver") as client:
        assert "ค้นหาผู้เล่น" not in client.get("/").text
        assert client.get("/staff/login").status_code == 404
        assert client.get("/staff/search").status_code == 404


def test_login_cookie_flags_csrf_and_five_failure_cooldown(staff_app) -> None:
    with TestClient(staff_app, base_url="https://testserver") as client:
        page = client.get("/staff/login")
        assert page.headers["cache-control"] == "no-store"
        login_csrf = _CSRF.search(page.text)
        assert login_csrf is not None
        csrf = login_csrf.group(1)
        assert (
            client.post(
                "/staff/login",
                data={"pin": _PIN, "csrf": "tampered"},
            ).status_code
            == 403
        )
        for _ in range(5):
            assert (
                client.post(
                    "/staff/login",
                    data={"pin": "000000", "csrf": csrf},
                ).status_code
                == 401
            )
        assert (
            client.post(
                "/staff/login",
                data={"pin": "000000", "csrf": csrf},
            ).status_code
            == 429
        )

    with TestClient(staff_app, base_url="https://testserver") as client:
        page = client.get("/staff/login")
        csrf_match = _CSRF.search(page.text)
        assert csrf_match is not None
        response = client.post(
            "/staff/login",
            data={"pin": _PIN, "csrf": csrf_match.group(1)},
            follow_redirects=False,
        )
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "samesite=strict" in cookie.lower()
        assert "Path=/staff" in cookie
        assert "Max-Age=43200" in cookie


def test_private_search_is_posted_and_page_urls_do_not_contain_term(staff_app) -> None:
    with TestClient(staff_app, base_url="https://testserver") as client:
        csrf = _login(client)
        assert client.post("/staff/search", data={"query": "ฟ้า"}).status_code == 403
        response = client.post(
            "/staff/search",
            data={"query": "ฟ้า", "csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/staff/search?page=1"
        page = client.get(response.headers["location"])
        assert page.headers["cache-control"] == "no-store"
        assert "ฟ้า" in page.text
        assert "?query=" not in str(page.url)
        assert "private prompt" not in page.text


def test_blank_search_is_newest_paged_four_and_missing_image_disables_print(staff_app) -> None:
    with TestClient(staff_app, base_url="https://testserver") as client:
        missing_ids = [_add_round(staff_app, f"ผู้เล่น {index}") for index in range(5)]
        _add_round(staff_app, "ผู้เล่นภาพพร้อม", image_available=True)
        _login(client)
        page = client.get("/staff/search?page=bad")
        assert page.status_code == 200
        assert page.text.count('class="staff-row"') == 4
        assert "ถัดไป" in page.text
        assert "ภาพไม่พร้อม" in page.text
        assert all(f"/staff/rounds/{round_id}" not in page.text for round_id in missing_ids)


def test_staff_print_is_private_no_store_and_done_returns_to_search_then_logout(staff_app) -> None:
    with TestClient(staff_app, base_url="https://testserver") as client:
        round_id = _add_round(staff_app, "น้องฟ้า (ป.3)", image_available=True)
        csrf = _login(client)
        search = client.get("/staff/search")
        assert f"/staff/rounds/{round_id}/photo-print?page=1" in search.text
        print_page = client.get(f"/staff/rounds/{round_id}/photo-print?page=1")
        assert print_page.status_code == 200
        assert print_page.headers["cache-control"] == "no-store"
        assert "กลับไปค้นหาผู้เล่น" in print_page.text
        assert "เริ่มเล่นเกมใหม่" not in print_page.text
        logout = client.post("/staff/logout", data={"csrf": csrf}, follow_redirects=False)
        assert logout.status_code == 303
        assert logout.headers["cache-control"] == "no-store"
        assert client.get("/staff/search", follow_redirects=False).status_code == 303
