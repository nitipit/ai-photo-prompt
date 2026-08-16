"""Shared fixtures for domain, lifecycle, and challenge-catalog tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import AI_PROVIDER_ENV
from app.content.importer import materialize_challenges
from app.domain.models import GameState, RoundRecord

ROOT = Path(__file__).resolve().parents[1]
CHALLENGE_SOURCE = ROOT / "design" / "challenges"
FIXED_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC).isoformat()


@pytest.fixture(autouse=True)
def explicit_fake_ai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep deterministic tests explicit now that startup has no provider default."""

    monkeypatch.setenv(AI_PROVIDER_ENV, "fake")


@pytest.fixture
def challenge_source() -> Path:
    """Point at the checked-in approved challenge bundles."""

    return CHALLENGE_SOURCE


@pytest.fixture
def materialized_catalog(tmp_path: Path, challenge_source: Path):
    """Materialize the checked-in bundles into an isolated output directory."""

    return materialize_challenges(challenge_source, tmp_path / "generated")


@pytest.fixture
def round_record() -> RoundRecord:
    """Return a minimal valid round record for state reconstruction tests."""

    return RoundRecord(
        id=str(uuid4()),
        state=GameState.LEVEL_SELECTION,
        display_name="Tester",
        created_at=FIXED_TIMESTAMP,
        updated_at=FIXED_TIMESTAMP,
    )
