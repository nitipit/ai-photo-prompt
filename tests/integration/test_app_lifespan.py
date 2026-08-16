from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import zlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.ai import FakeAIPipeline, PiAIPipeline
from app.ai.generated_artifacts import GeneratedArtifactStore
from app.ai.pi_rpc import PiRPCRequest, run_pi_rpc
from app.ai.protocols import GenerationAttempt
from app.ai.results import AIPipelineResult
from app.config import AI_PROVIDER_ENV, DEFAULT_GENERATED_ROOT
from app.content.repository import CatalogValidationError, ChallengeCatalog
from app.domain.models import ChallengeSpec, LevelGroup, PromptSubmissionReason
from app.persistence import (
    ChallengeNotFoundError,
    ShelfDbChallengeRepository,
    ShelfDbGenerationClaims,
    ShelfDbRoundRepository,
)
from app.server import app
from app.services import GameRoundService

_FAKE_DETACHED_CHILD = r"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

marker = Path(sys.argv[1])
request = json.loads(sys.stdin.buffer.readline())
child = subprocess.Popen([
    sys.executable,
    "-c",
    "import pathlib,sys,time; time.sleep(0.4); pathlib.Path(sys.argv[1]).write_text('alive')",
    str(marker),
])
marker.with_suffix(".parent-pid").write_text(str(os.getpid()), encoding="utf-8")
marker.with_suffix(".child-pid").write_text(str(child.pid), encoding="utf-8")
response = {"id": request["id"], "type": "response", "command": "prompt", "success": True}
sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\\n")
sys.stdout.flush()
time.sleep(30)
"""


def _valid_png() -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + name
            + payload
            + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">II", 1, 1) + b"\x08\x06\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


class _DetachedRPCPipeline:
    def __init__(self, request: PiRPCRequest, db: DB) -> None:
        self._request = request
        self._db = db
        self.started = asyncio.Event()
        self.settled = asyncio.Event()
        self.db_open_during_settlement = False

    async def run(
        self,
        challenge: ChallengeSpec,
        prompt: str,
        timeout: float,
        *,
        attempt: GenerationAttempt,
    ) -> AIPipelineResult:
        del challenge, prompt, timeout, attempt
        self.started.set()
        try:
            await run_pi_rpc(self._request)
            raise AssertionError("mock Pi child unexpectedly settled")
        finally:
            try:
                with self._db.transaction(write=False):
                    self.db_open_during_settlement = True
            finally:
                self.settled.set()


async def _wait_for_path(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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


def test_lifespan_exposes_typed_runtime_state_and_closes_db(
    runtime_app,
    tmp_path: Path,
) -> None:
    with TestClient(runtime_app) as client:
        assert isinstance(runtime_app.state.db, DB)
        assert isinstance(runtime_app.state.catalog, ChallengeCatalog)
        assert isinstance(runtime_app.state.challenge_repository, ShelfDbChallengeRepository)
        assert isinstance(runtime_app.state.round_repository, ShelfDbRoundRepository)
        assert isinstance(runtime_app.state.generation_claims, ShelfDbGenerationClaims)
        assert isinstance(runtime_app.state.game_round_service, GameRoundService)
        assert isinstance(runtime_app.state.game_round_service._pipeline, FakeAIPipeline)
        generated_mount = next(
            route for route in runtime_app.routes if route.name == "generated-artifacts"
        )
        assert Path(generated_mount.app.directory) == DEFAULT_GENERATED_ROOT.resolve()

        attempt = GenerationAttempt(
            round_id=str(uuid4()),
            attempt_token=str(uuid4()),
        )
        default_store = GeneratedArtifactStore(tmp_path / "private", DEFAULT_GENERATED_ROOT)
        try:
            workspace = default_store.prepare_workspace(attempt)
            workspace.staged_path.write_bytes(_valid_png())
            published = default_store.publish(attempt, workspace.relative_output_path)
            response = client.get(published.url)
            assert response.status_code == 200
            assert response.content == _valid_png()
        finally:
            default_store.discard(attempt)
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

    with TestClient(runtime_app) as client:
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

        attempt = GenerationAttempt(
            round_id=str(uuid4()),
            attempt_token=str(uuid4()),
        )
        workspace = runtime_app.state.artifact_store.prepare_workspace(attempt)
        workspace.staged_path.write_bytes(_valid_png())
        published = runtime_app.state.artifact_store.publish(
            attempt,
            workspace.relative_output_path,
        )
        response = client.get(published.url)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == _valid_png()


def test_missing_ai_provider_fails_without_opening_db(
    runtime_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AI_PROVIDER_ENV, raising=False)
    db_path = runtime_app.state.db_path

    with pytest.raises(RuntimeError, match="must explicitly select"):
        with TestClient(runtime_app):
            pass

    assert not db_path.exists()
    assert not hasattr(runtime_app.state, "db")


def test_pi_preflight_failure_never_falls_back_or_opens_db(runtime_app, tmp_path: Path) -> None:
    runtime_app.state.ai_provider = "pi"
    runtime_app.state.pi_executable = str(tmp_path / "missing-pi")
    db_path = runtime_app.state.db_path

    with pytest.raises(RuntimeError, match="pi executable is unavailable"):
        with TestClient(runtime_app):
            pass

    assert not db_path.exists()
    assert not hasattr(runtime_app.state, "db")


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


@pytest.mark.asyncio
async def test_lifespan_shutdown_settles_active_rpc_before_db_close_and_restart(
    runtime_app,
    tmp_path: Path,
) -> None:
    child_script = tmp_path / "detached_pi.py"
    child_script.write_text(_FAKE_DETACHED_CHILD, encoding="utf-8")
    marker = tmp_path / "descendant-alive.txt"
    parent_pid_path = marker.with_suffix(".parent-pid")
    child_pid_path = marker.with_suffix(".child-pid")
    generation: asyncio.Task[object]

    async with runtime_app.router.lifespan_context(runtime_app):
        service = runtime_app.state.game_round_service
        created = await service.create_round("Shutdown")
        await service.configure_round(created.id, LevelGroup.P1_P3)
        await service.continue_challenge(created.id)
        generating = await service.submit_prompt(
            created.id,
            "เด็กวาดภาพในสวน",
            PromptSubmissionReason.MANUAL,
        )
        db = runtime_app.state.db
        pipeline = _DetachedRPCPipeline(
            PiRPCRequest(
                argv=(sys.executable, str(child_script), str(marker)),
                cwd=tmp_path,
                prompt="start mocked generation",
                timeout=30,
                max_stdout_bytes=4096,
                max_stderr_bytes=4096,
            ),
            db,
        )
        service._pipeline = pipeline
        generation = asyncio.create_task(service.generate_round(generating.id))
        await pipeline.started.wait()
        await _wait_for_path(child_pid_path)
        parent_pid = int(parent_pid_path.read_text(encoding="utf-8"))
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

    with pytest.raises(asyncio.CancelledError):
        await generation
    assert pipeline.settled.is_set()
    assert pipeline.db_open_during_settlement is True
    with pytest.raises(Exception, match="closed"):
        with db.transaction(write=False):
            pass

    await asyncio.sleep(0.6)
    assert not marker.exists()
    assert not _process_exists(parent_pid)
    assert not _process_exists(child_pid)

    async with runtime_app.router.lifespan_context(runtime_app):
        restarted = runtime_app.state.game_round_service
        assert runtime_app.state.generation_claims.get(generating.id) is None
        retried = await restarted.generate_round(generating.id)
        assert retried.generated_artifact is not None
        assert retried.generated_artifact.provider == "fake-ai"


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
