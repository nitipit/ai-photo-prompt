from fastapi.testclient import TestClient

from app.server import app


def test_ready_start_redirects_to_level_selection() -> None:
    with TestClient(app) as client:
        ready = client.get("/")
        assert ready.status_code == 200
        assert "เปลี่ยนภาพที่เห็น" in ready.text
        assert 'action="/rounds"' in ready.text

        redirect = client.post("/rounds", follow_redirects=False)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/rounds/demo/level"

        level = client.get(redirect.headers["location"])
        assert level.status_code == 200
        assert "เลือกช่วงชั้น" in level.text
        assert 'name="display_name"' in level.text
        assert 'value="p1-p3"' in level.text
        assert 'value="m4-m6"' in level.text
