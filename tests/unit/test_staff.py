from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.models import (
    GameState,
    ImageArtifact,
    RoundRecord,
    ScoreResult,
    TerminalDisposition,
)
from app.services.staff import (
    StaffAuth,
    StaffCooldownError,
    StaffCSRFError,
    StaffLoginError,
    artifact_is_available,
    search_completed_rounds,
)


class CompletedRounds:
    def __init__(self, rows: list[RoundRecord]) -> None:
        self.rows = rows

    def list_completed(self) -> list[RoundRecord]:
        return list(self.rows)


def completed(name: str, when: str, *, artifact: ImageArtifact | None) -> RoundRecord:
    return RoundRecord(
        id=str(uuid4()),
        state=GameState.LEADERBOARD,
        display_name=name,
        level="p1-p3",
        challenge_id="p1-p3-01",
        prompt="ไม่ควรปรากฏในผลค้นหา",
        generated_artifact=artifact,
        score=ScoreResult(prompt_score=70, image_score=80, total_score=75),
        feedback=["ดี", "เพิ่มรายละเอียด"],
        terminal_disposition=TerminalDisposition.COMPLETED,
        created_at=when,
        updated_at=when,
        completed_at=when,
        leaderboard_deadline=when,
    )


def test_pin_auth_uses_csrf_cooldown_and_opaque_session() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    auth = StaffAuth("123456", clock=lambda: now)
    csrf = auth.issue_login_csrf()
    assert csrf is not None

    for _ in range(5):
        with pytest.raises(StaffLoginError):
            auth.verify_login("000000", csrf, csrf, "kiosk")
    with pytest.raises(StaffCooldownError):
        auth.verify_login("000000", csrf, csrf, "kiosk")

    auth = StaffAuth("123456", clock=lambda: now)
    csrf = auth.issue_login_csrf()
    assert csrf is not None
    with pytest.raises(StaffCSRFError):
        auth.verify_login("123456", "tampered", csrf, "kiosk")
    token = auth.verify_login("123456", csrf, csrf, "kiosk")
    session = auth.session(token)
    assert session is not None
    assert token not in session.csrf_token
    assert auth.verify_csrf(session, session.csrf_token)


def test_staff_search_is_newest_four_casefolded_and_hides_prompts(tmp_path: Path) -> None:
    root = tmp_path / "generated"
    round_id = uuid4()
    attempt_id = uuid4()
    (root / str(round_id)).mkdir(parents=True)
    (root / str(round_id) / f"{attempt_id}.png").write_bytes(b"image")
    url = f"/generated/{round_id}/{attempt_id}.png"
    rows = [
        completed("น้องฟ้า (ป.3)", "2026-01-01T00:00:00+00:00", artifact=ImageArtifact(url=url)),
        *[
            completed(
                f"ผู้เล่น {index}",
                f"2026-01-0{index + 2}T00:00:00+00:00",
                artifact=None,
            )
            for index in range(5)
        ],
    ]
    result = search_completed_rounds(
        CompletedRounds(rows),
        root,
        term="ฟ้า",
        page=1,
    )
    assert result.total == 1
    assert result.rows[0].display_name == "น้องฟ้า (ป.3)"
    assert result.rows[0].image_available is True
    assert result.rows[0].formatted_completed_at == "01/01/2026 07:00"
    assert "ไม่ควรปรากฏ" not in result.rows[0].display_name

    latest = search_completed_rounds(CompletedRounds(rows), root, term="", page=99)
    assert latest.page == 2
    assert len(latest.rows) == 2
    assert latest.rows[0].score == 75
    assert latest.rows[0].image_available is False


def test_unavailable_artifact_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "generated"
    round_id = uuid4()
    attempt_id = uuid4()
    (root / str(round_id)).mkdir(parents=True)
    (root / str(round_id) / f"{attempt_id}.png").symlink_to("/etc/passwd")
    assert not artifact_is_available(f"/generated/{round_id}/{attempt_id}.png", root)
