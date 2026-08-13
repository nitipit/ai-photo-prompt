from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_prompt_countdown_uses_persisted_deadline_and_not_static_seconds() -> None:
    script = (ROOT / "src" / "app" / "templates" / "prompt.ts").read_text()
    template = (ROOT / "src" / "app" / "templates" / "prompt.html").read_text()

    assert 'Date.parse(countdown.dataset.deadline ?? "")' in script
    assert "Math.ceil((deadline - Date.now()) / 1000)" in script
    assert "Math.min(" in script
    assert "MAX_PROMPT_SECONDS" in script
    assert "form.requestSubmit()" in script
    assert "dataset.seconds" not in script
    assert 'data-deadline="{{ prompt_deadline }}"' in template
    assert "data-seconds" not in template
