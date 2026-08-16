from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.ai import FakeAIPipeline, PiAIPipeline
from app.content.repository import CatalogValidationError, ChallengeCatalog
from app.domain.models import LevelGroup
from app.persistence import (
    ChallengeNotFoundError,
    ShelfDbChallengeRepository,
    ShelfDbGenerationClaims,
    ShelfDbRoundRepository,
)
from app.server import app
from app.services import GameRoundService


@pytest.fixture
def runtime_app(tmp_path: Path, materialized_catalog):
    app.state.db_path = tmp_path / "runtime.shelfdb"
    app.state.catalog_path = materialized_catalog.catalog_path
    yield app
    for name in (
        "db_path",
        "catalog_path",
        "ai_provider",
        "pi_executable",
        "pi_bridge_path",
        "pi_workspace_root",
        "generated_root",
        "dist_root",
        "provider_timeout",
    ):
        if hasattr(app.state, name):
            delattr(app.state, name)


def test_lifespan_exposes_typed_runtime_state_and_closes_db(runtime_app) -> None:
    with TestClient(runtime_app):
        assert isinstance(runtime_app.state.db, DB)
        assert isinstance(runtime_app.state.catalog, ChallengeCatalog)
        assert isinstance(runtime_app.state.challenge_repository, ShelfDbChallengeRepository)
        assert isinstance(runtime_app.state.round_repository, ShelfDbRoundRepository)
        assert isinstance(runtime_app.state.generation_claims, ShelfDbGenerationClaims)
        assert isinstance(runtime_app.state.game_round_service, GameRoundService)
        assert isinstance(runtime_app.state.game_round_service._pipeline, FakeAIPipeline)
        db = runtime_app.state.db

    for name in (
        "db",
        "catalog",
        "challenge_repository",
        "round_repository",
        "generation_claims",
        "game_round_service",
    ):
        assert not hasattr(runtime_app.state, name)

    with pytest.raises(Exception, match="closed"):
        with db.transaction(write=False):
            pass


def test_pi_provider_is_explicit_and_never_falls_back_to_fake(
    runtime_app,
    tmp_path: Path,
) -> None:
    bridge = tmp_path / "codex-bridge.ts"
    bridge.write_text("// startup presence check", encoding="utf-8")
    runtime_app.state.ai_provider = "pi"
    runtime_app.state.pi_executable = sys.executable
    runtime_app.state.pi_bridge_path = bridge
    runtime_app.state.pi_workspace_root = tmp_path / "pi-rpc"
    runtime_app.state.generated_root = tmp_path / "generated"
    runtime_app.state.dist_root = tmp_path / "dist"
    runtime_app.state.provider_timeout = 240.0

    with TestClient(runtime_app):
        service = runtime_app.state.game_round_service
        assert runtime_app.state.active_ai_provider == "pi"
        assert isinstance(service._pipeline, PiAIPipeline)
        assert not isinstance(service._pipeline, FakeAIPipeline)
        assert runtime_app.state.artifact_store.private_root == tmp_path / "pi-rpc"
        assert runtime_app.state.artifact_store.published_root == tmp_path / "generated"
        assert service._provider_timeout == 240.0
        assert service._pipeline._max_stdout_bytes == 32 * 1024 * 1024
        assert "codex_imagegen" in service._pipeline._image_argv
        assert "codex_imagegen" not in service._pipeline._evaluator_argv


def test_unknown_ai_provider_fails_without_opening_db(runtime_app, tmp_path: Path) -> None:
    runtime_app.state.ai_provider = "unknown"
    db_path = runtime_app.state.db_path

    with pytest.raises(RuntimeError, match="unsupported AI provider"):
        with TestClient(runtime_app):
            pass

    assert not db_path.exists()
    assert not hasattr(runtime_app.state, "db")


def test_service_round_survives_lifespan_reopen(runtime_app) -> None:
    with TestClient(runtime_app):
        service = runtime_app.state.game_round_service
        created = asyncio.run(service.create_round("  น้องทดสอบ  "))
        configured = asyncio.run(service.configure_round(created.id, LevelGroup.P1_P3))
        assert configured.challenge_id is not None
        stored_db = runtime_app.state.db

    with pytest.raises(Exception, match="closed"):
        with stored_db.transaction(write=False):
            pass

    with TestClient(runtime_app):
        rebuilt = asyncio.run(runtime_app.state.game_round_service.get_round(created.id))

    assert rebuilt.dict() == configured.dict()


def test_lifespan_sync_removes_stale_challenge_ids_on_catalog_change(
    runtime_app,
    tmp_path: Path,
    materialized_catalog,
) -> None:
    changed_payload = json.loads(materialized_catalog.catalog_path.read_text(encoding="utf-8"))
    changed = next(
        challenge for challenge in changed_payload["challenges"] if challenge["id"] == "p1-p3-01"
    )
    changed["id"] = "p1-p3-01-replacement"
    changed["target_asset_url"] = "/assets/challenges/p1-p3-01-replacement.webp"
    changed_catalog_path = tmp_path / "changed-catalog.json"
    changed_catalog_path.write_text(json.dumps(changed_payload), encoding="utf-8")

    with TestClient(runtime_app):
        assert runtime_app.state.challenge_repository.get("p1-p3-01").id == "p1-p3-01"

    runtime_app.state.catalog_path = changed_catalog_path
    with TestClient(runtime_app):
        repository = runtime_app.state.challenge_repository
        with pytest.raises(ChallengeNotFoundError):
            repository.get("p1-p3-01")
        assert repository.get("p1-p3-01-replacement").id == "p1-p3-01-replacement"
        assert len(repository.all()) == 20


def test_missing_catalog_fails_startup_without_opening_db(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-catalog.shelfdb"
    app.state.db_path = db_path
    app.state.catalog_path = tmp_path / "missing-catalog.json"
    try:
        with pytest.raises(CatalogValidationError):
            with TestClient(app):
                pass

        assert not db_path.exists()
        assert not hasattr(app.state, "db")
    finally:
        for name in ("db_path", "catalog_path"):
            if hasattr(app.state, name):
                delattr(app.state, name)
