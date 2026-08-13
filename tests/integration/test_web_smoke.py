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
        assert 'method="post"' in level.text
        assert 'value="p1-p3"' in level.text
        assert 'value="m4-m6"' in level.text


def test_level_post_reveals_sorted_challenge_and_serves_target_asset() -> None:
    with TestClient(app) as client:
        invalid = client.post(
            "/rounds/demo/level",
            data={"display_name": "น้องฟ้า", "level": "unknown"},
            follow_redirects=False,
        )
        assert invalid.status_code == 422

        redirect = client.post(
            "/rounds/demo/level",
            data={"display_name": "น้องฟ้า", "level": "p1-p3"},
            follow_redirects=False,
        )
        assert redirect.status_code == 303
        assert redirect.headers["location"] == (
            "/rounds/demo/challenge?challenge_id=p1-p3-01"
        )

        challenge = client.get(redirect.headers["location"])
        assert challenge.status_code == 200
        assert 'data-scene="challenge-reveal"' in challenge.text
        assert 'src="/assets/challenges/p1-p3-01.webp"' in challenge.text
        assert "Rabbit chef and giant pancake" not in challenge.text

        target = client.get("/assets/challenges/p1-p3-01.webp")
        assert target.status_code == 200
        assert target.headers["content-type"] == "image/webp"

        next_seam = client.post("/rounds/demo/challenge/continue")
        assert next_seam.status_code == 501
