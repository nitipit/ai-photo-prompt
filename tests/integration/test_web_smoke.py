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
        assert redirect.headers["location"] == ("/rounds/demo/challenge?challenge_id=p1-p3-01")

        challenge = client.get(redirect.headers["location"])
        assert challenge.status_code == 200
        assert 'data-scene="challenge-reveal"' in challenge.text
        assert 'src="/assets/challenges/p1-p3-01.webp"' in challenge.text
        assert 'name="challenge_id"' in challenge.text
        assert "Rabbit chef and giant pancake" not in challenge.text

        target = client.get("/assets/challenges/p1-p3-01.webp")
        assert target.status_code == 200
        assert target.headers["content-type"] == "image/webp"

        next_seam = client.post(
            "/rounds/demo/challenge/continue",
            data={"challenge_id": "p1-p3-01"},
            follow_redirects=False,
        )
        assert next_seam.status_code == 303
        assert next_seam.headers["location"] == ("/rounds/demo/prompt?challenge_id=p1-p3-01")

        prompt = client.get(next_seam.headers["location"])
        assert prompt.status_code == 200
        assert 'data-scene="prompt-entry"' in prompt.text
        assert 'src="/assets/challenges/p1-p3-01.webp"' in prompt.text
        assert 'name="challenge_id"' in prompt.text
        assert 'name="prompt"' in prompt.text
        assert 'maxlength="1000"' in prompt.text
        assert 'name="submission_reason"' in prompt.text
        assert "Rabbit chef and giant pancake" not in prompt.text


def test_prompt_submission_seams_validate_reason_and_content() -> None:
    with TestClient(app) as client:
        blank_manual = client.post(
            "/rounds/demo/prompt",
            data={
                "challenge_id": "p1-p3-01",
                "prompt": "   ",
                "submission_reason": "manual",
            },
        )
        assert blank_manual.status_code == 422

        blank_timeout = client.post(
            "/rounds/demo/prompt",
            data={
                "challenge_id": "p1-p3-01",
                "prompt": "",
                "submission_reason": "timeout",
            },
            follow_redirects=False,
        )
        assert blank_timeout.status_code == 303
        assert blank_timeout.headers["location"] == "/"

        valid_manual = client.post(
            "/rounds/demo/prompt",
            data={
                "challenge_id": "p1-p3-01",
                "prompt": "กระต่ายเชฟทำแพนเค้กยักษ์",
                "submission_reason": "manual",
            },
        )
        assert valid_manual.status_code == 501
        assert "Generating scene is not implemented" in valid_manual.json()["detail"]

        valid_timeout = client.post(
            "/rounds/demo/prompt",
            data={
                "challenge_id": "p1-p3-01",
                "prompt": "กระต่ายเชฟทำแพนเค้กยักษ์",
                "submission_reason": "timeout",
            },
        )
        assert valid_timeout.status_code == 501

        too_long = client.post(
            "/rounds/demo/prompt",
            data={
                "challenge_id": "p1-p3-01",
                "prompt": "x" * 1001,
                "submission_reason": "manual",
            },
        )
        assert too_long.status_code == 422


def test_prompt_unknown_challenge_is_not_found() -> None:
    with TestClient(app) as client:
        prompt = client.get("/rounds/demo/prompt?challenge_id=missing")
        assert prompt.status_code == 404

        continue_response = client.post(
            "/rounds/demo/challenge/continue",
            data={"challenge_id": "missing"},
        )
        assert continue_response.status_code == 404
