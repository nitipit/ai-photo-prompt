import re
from urllib.parse import parse_qs, urlsplit

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


def test_prompt_submission_redirects_to_encoded_generating_scene() -> None:
    prompt = "กระต่ายเชฟ & สีฟ้า + 50%"
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
                "prompt": prompt,
                "submission_reason": "manual",
            },
            follow_redirects=False,
        )
        assert valid_manual.status_code == 303
        location = urlsplit(valid_manual.headers["location"])
        assert location.path == "/rounds/demo/generating"
        assert parse_qs(location.query) == {
            "challenge_id": ["p1-p3-01"],
            "prompt": [prompt],
        }
        assert "+" in location.query
        assert "%" in location.query

        valid_timeout = client.post(
            "/rounds/demo/prompt",
            data={
                "challenge_id": "p1-p3-01",
                "prompt": prompt,
                "submission_reason": "timeout",
            },
            follow_redirects=False,
        )
        assert valid_timeout.status_code == 303
        assert urlsplit(valid_timeout.headers["location"]).path == "/rounds/demo/generating"

        too_long = client.post(
            "/rounds/demo/prompt",
            data={
                "challenge_id": "p1-p3-01",
                "prompt": "x" * 1001,
                "submission_reason": "manual",
            },
        )
        assert too_long.status_code == 422


def test_generating_success_renders_the_explicit_fake_placeholder() -> None:
    with TestClient(app) as client:
        generating = client.get(
            "/rounds/demo/generating",
            params={
                "challenge_id": "p1-p3-01",
                "prompt": "กระต่ายเชฟทำแพนเค้กยักษ์",
            },
        )

        assert generating.status_code == 200
        assert 'data-scene="generating"' in generating.text
        assert 'data-generating-state="success"' in generating.text
        assert 'data-fake-generated-placeholder="true"' in generating.text
        assert 'data-placeholder-source="selected-target-asset"' in generating.text
        assert 'src="/assets/challenges/p1-p3-01.webp"' in generating.text
        assert 'action="/rounds/demo/generating/continue"' in generating.text


def test_generating_failure_retry_preserves_context_and_exit_returns_ready() -> None:
    prompt = "กระต่ายเชฟทำแพนเค้กยักษ์"
    with TestClient(app) as client:
        failure = client.get(
            "/rounds/demo/generating",
            params={
                "challenge_id": "p1-p3-01",
                "prompt": prompt,
                "failure": "1",
            },
        )
        assert failure.status_code == 200
        assert 'data-generating-state="failure"' in failure.text
        assert 'action="/rounds/demo/generating/retry"' in failure.text
        assert 'action="/rounds/demo/generating/exit"' in failure.text
        assert f'value="{prompt}"' in failure.text

        retry = client.post(
            "/rounds/demo/generating/retry",
            data={"challenge_id": "p1-p3-01", "prompt": prompt},
            follow_redirects=False,
        )
        assert retry.status_code == 303
        retry_location = urlsplit(retry.headers["location"])
        assert retry_location.path == "/rounds/demo/generating"
        assert parse_qs(retry_location.query) == {
            "challenge_id": ["p1-p3-01"],
            "prompt": [prompt],
        }

        exit_response = client.post(
            "/rounds/demo/generating/exit",
            data={"challenge_id": "p1-p3-01", "prompt": prompt},
            follow_redirects=False,
        )
        assert exit_response.status_code == 303
        assert exit_response.headers["location"] == "/"


def test_generating_continue_redirects_to_encoded_result_scene() -> None:
    prompt = "กระต่ายเชฟ & สีฟ้า + 50%"
    with TestClient(app) as client:
        continue_response = client.post(
            "/rounds/demo/generating/continue",
            data={"challenge_id": "p1-p3-01", "prompt": prompt},
            follow_redirects=False,
        )

        assert continue_response.status_code == 303
        location = urlsplit(continue_response.headers["location"])
        assert location.path == "/rounds/demo/result"
        assert parse_qs(location.query) == {
            "challenge_id": ["p1-p3-01"],
            "prompt": [prompt],
        }
        assert "+" in location.query
        assert "%" in location.query


def test_result_renders_deterministic_feedback_without_challenge_title() -> None:
    with TestClient(app) as client:
        result = client.get(
            "/rounds/demo/result",
            params={
                "challenge_id": "p1-p3-01",
                "prompt": "กระต่ายเชฟทำแพนเค้กยักษ์",
            },
        )

        assert result.status_code == 200
        assert 'data-scene="result"' in result.text
        assert 'data-result-data="deterministic-demo"' in result.text
        assert result.text.count('src="/assets/challenges/p1-p3-01.webp"') == 2
        assert 'data-fake-generated-placeholder="true"' in result.text
        assert 'data-placeholder-source="selected-target-asset"' in result.text
        assert 'data-real-ai-output="false"' in result.text
        assert 'data-demo-score="82"' in result.text
        assert 'data-score-source="deterministic-demo-data"' in result.text
        assert 'data-demo-feedback="true"' in result.text
        assert 'data-feedback-source="deterministic-demo-data"' in result.text
        assert result.text.count('data-feedback-kind="strength"') == 2
        assert result.text.count('data-feedback-kind="improvement">') == 1
        assert "82" in result.text
        assert "เก่งมาก!" in result.text
        assert "Rabbit chef and giant pancake" not in result.text
        assert 'action="/rounds/demo/result/leaderboard"' in result.text
        assert 'name="score" value="82"' in result.text


def test_result_unknown_challenge_is_not_found() -> None:
    with TestClient(app) as client:
        result = client.get(
            "/rounds/demo/result",
            params={"challenge_id": "missing", "prompt": "ข้อความ"},
        )
        assert result.status_code == 404


def test_leaderboard_redirects_with_encoded_context_and_rejects_tampering() -> None:
    prompt = "กระต่ายเชฟ & สีฟ้า + 50%"
    data = {
        "challenge_id": "p1-p3-01",
        "prompt": prompt,
        "score": "82",
        "level": "p1-p3",
    }
    with TestClient(app) as client:
        tampered_score = client.post(
            "/rounds/demo/result/leaderboard",
            data={**data, "score": "81"},
        )
        assert tampered_score.status_code == 422

        tampered_level = client.post(
            "/rounds/demo/result/leaderboard",
            data={**data, "level": "p4-p6"},
        )
        assert tampered_level.status_code == 422

        redirect = client.post(
            "/rounds/demo/result/leaderboard",
            data=data,
            follow_redirects=False,
        )
        assert redirect.status_code == 303
        location = urlsplit(redirect.headers["location"])
        assert location.path == "/rounds/demo/leaderboard"
        assert parse_qs(location.query) == {
            "challenge_id": ["p1-p3-01"],
            "prompt": [prompt],
            "score": ["82"],
            "level": ["p1-p3"],
        }
        assert "+" in location.query
        assert "%" in location.query

        fallback_form = client.post(
            "/rounds/demo/result/leaderboard",
            data={key: value for key, value in data.items() if key != "level"},
            follow_redirects=False,
        )
        assert fallback_form.status_code == 303
        assert "level=p1-p3" in fallback_form.headers["location"]


def test_leaderboard_derives_level_from_non_p1_challenge() -> None:
    prompt = "มังกรผู้รักษาประตูยืนหน้าประตูฟุตบอล"
    with TestClient(app) as client:
        redirect = client.post(
            "/rounds/demo/result/leaderboard",
            data={
                "challenge_id": "p4-p6-01",
                "prompt": prompt,
                "score": "82",
            },
            follow_redirects=False,
        )

        assert redirect.status_code == 303
        location = urlsplit(redirect.headers["location"])
        assert parse_qs(location.query)["level"] == ["p4-p6"]

        leaderboard = client.get(redirect.headers["location"])
        assert leaderboard.status_code == 200
        assert 'data-leaderboard-level="p4-p6"' in leaderboard.text
        assert "ระดับ p4-p6" in leaderboard.text
        assert "รอบนี้ได้อันดับ 2" in leaderboard.text
        assert "มังกรผู้รักษาประตูยืนหน้าประตูฟุตบอล" in leaderboard.text
        assert re.findall(r'data-entry-rank="(\d+)"', leaderboard.text) == ["1", "2", "2", "4"]


def test_leaderboard_renders_competition_tie_current_prompt_and_ready_fallback() -> None:
    prompt = "กระต่ายเชฟ & สีฟ้า + 50%"
    with TestClient(app) as client:
        leaderboard = client.get(
            "/rounds/demo/leaderboard",
            params={
                "challenge_id": "p1-p3-01",
                "prompt": prompt,
                "score": "82",
                "level": "p1-p3",
            },
        )

        assert leaderboard.status_code == 200
        assert 'data-scene="leaderboard"' in leaderboard.text
        assert 'data-leaderboard-data="deterministic-demo"' in leaderboard.text
        assert 'data-leaderboard-list="deterministic-demo-data"' in leaderboard.text
        assert 'data-ranking="competition-fixed-demo"' in leaderboard.text
        assert "รอบนี้ได้อันดับ 2" in leaderboard.text
        assert "กระต่ายเชฟ &amp; สีฟ้า + 50%" in leaderboard.text
        assert re.findall(r'data-entry-rank="(\d+)"', leaderboard.text) == ["1", "2", "2", "4"]
        assert re.findall(r'data-entry-score="(\d+)"', leaderboard.text) == ["96", "82", "82", "74"]
        assert leaderboard.text.count('data-leaderboard-entry="deterministic-demo"') == 4
        assert leaderboard.text.count('data-entry-name="') == 4
        assert leaderboard.text.count('data-entry-prompt="true"') == 4
        assert leaderboard.text.count('data-entry-image="true"') == 4
        assert leaderboard.text.count('data-temporary-image-placeholder="true"') == 4
        assert 'data-current-entry="true"' in leaderboard.text
        assert 'data-current-score="82"' in leaderboard.text
        assert 'data-leaderboard-countdown="15"' in leaderboard.text
        assert 'href="/"' in leaderboard.text
        assert 'data-ready-fallback="true"' in leaderboard.text

        tampered_score = client.get(
            "/rounds/demo/leaderboard",
            params={
                "challenge_id": "p1-p3-01",
                "prompt": prompt,
                "score": "81",
                "level": "p1-p3",
            },
        )
        assert tampered_score.status_code == 422

        tampered_level = client.get(
            "/rounds/demo/leaderboard",
            params={
                "challenge_id": "p1-p3-01",
                "prompt": prompt,
                "score": "82",
                "level": "p4-p6",
            },
        )
        assert tampered_level.status_code == 422


def test_prompt_unknown_challenge_is_not_found() -> None:
    with TestClient(app) as client:
        prompt = client.get("/rounds/demo/prompt?challenge_id=missing")
        assert prompt.status_code == 404

        generating = client.get(
            "/rounds/demo/generating",
            params={"challenge_id": "missing", "prompt": "ข้อความ"},
        )
        assert generating.status_code == 404

        result = client.get(
            "/rounds/demo/result",
            params={"challenge_id": "missing", "prompt": "ข้อความ"},
        )
        assert result.status_code == 404

        continue_response = client.post(
            "/rounds/demo/challenge/continue",
            data={"challenge_id": "missing"},
        )
        assert continue_response.status_code == 404
