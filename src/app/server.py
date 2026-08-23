"""FastAPI entry point for the first visible Photo Prompt checkpoint."""

import asyncio
import os
import random
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from shelfdb.shelf import DB  # type: ignore[import-untyped]
from starlette.routing import Mount

from .ai import FakeAIPipeline
from .ai.generated_artifacts import GeneratedArtifactStore
from .ai.pi_pipeline import PiAIPipeline
from .config import (
    AI_PROVIDER_ENV,
    DEFAULT_CATALOG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_GENERATED_ROOT,
    DEFAULT_PI_BRIDGE_PATH,
    DEFAULT_PI_EVALUATOR_THINKING,
    DEFAULT_PI_EXECUTABLE,
    DEFAULT_PI_IMAGE_THINKING,
    DEFAULT_PI_MAX_OUTPUT_BYTES,
    DEFAULT_PI_MODEL,
    DEFAULT_PI_PROVIDER,
    DEFAULT_PI_TIMEOUT_SECONDS,
    DEFAULT_PI_WORKSPACE_ROOT,
    DIST_DIR,
)
from .content.repository import ChallengeCatalog
from .domain.models import (
    ChallengeSpec,
    GameState,
    GenerationStatusState,
    LevelGroup,
    PromptSubmissionReason,
    TerminalDisposition,
)
from .persistence import (
    ChallengeNotFoundError,
    ChallengeRepositoryError,
    GenerationAlreadyRunningError,
    RoundNotFoundError,
    ShelfDbChallengeRepository,
    ShelfDbGenerationClaims,
    ShelfDbRoundRepository,
)
from .services import (
    GameRoundConflictError,
    GameRoundDeadlineError,
    GameRoundService,
    GameRoundValidationError,
    GenerationStatus,
)
from .web import (
    render_challenge_reveal,
    render_generating,
    render_leaderboard,
    render_level_selection,
    render_photo_print,
    render_prompt_entry,
    render_public_leaderboard,
    render_ready,
    render_result,
)

_DEFAULT_ARTIFACT_RECONCILIATION_MAX_ENTRIES = 10_000


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Own the one-process local runtime dependencies for the application."""

    catalog_path = Path(getattr(application.state, "catalog_path", DEFAULT_CATALOG_PATH))
    db_path = Path(getattr(application.state, "db_path", DEFAULT_DB_PATH))
    configured_generated_root = Path(
        getattr(application.state, "generated_root", DEFAULT_GENERATED_ROOT)
    )
    pipeline, artifact_store, provider_timeout = _build_ai_pipeline(
        application,
        configured_generated_root,
    )
    generated_root = _prepare_runtime_directory(
        artifact_store.published_root if artifact_store is not None else configured_generated_root,
        "generated artifact root",
    )
    _configure_generated_static_mount(application, generated_root)
    db: DB | None = None
    game_round_service: GameRoundService | None = None
    try:
        source_catalog = ChallengeCatalog.load(catalog_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = DB(str(db_path))
        challenge_repository = ShelfDbChallengeRepository(db)
        challenge_repository.sync(source_catalog)
        runtime_catalog = ChallengeCatalog.from_repository(challenge_repository)
        runtime_catalog.all()
        round_repository = ShelfDbRoundRepository(db)
        artifact_reconciliation = None
        if artifact_store is not None:
            reconciliation_limit = _artifact_reconciliation_limit(application)
            referenced_urls = await asyncio.to_thread(
                round_repository.list_generated_artifact_urls,
                max_records=reconciliation_limit,
            )
            artifact_reconciliation = await asyncio.to_thread(
                artifact_store.reconcile,
                referenced_urls,
                max_entries=reconciliation_limit,
            )
        claim_lease_duration = timedelta(
            seconds=float(getattr(application.state, "claim_lease_seconds", 30.0))
        )
        generation_claims = ShelfDbGenerationClaims(
            db,
            _utc_now,
            claim_lease_duration,
        )
        game_round_service = GameRoundService(
            round_repository,
            runtime_catalog,
            _select_challenge,
            _utc_now,
            generation_claims=generation_claims,
            pipeline=pipeline,
            owner_instance=str(uuid4()),
            claim_lease_duration=claim_lease_duration,
            claim_heartbeat_interval=timedelta(
                seconds=float(getattr(application.state, "claim_heartbeat_seconds", 5.0))
            ),
            provider_timeout=provider_timeout,
        )
        application.state.db = db
        application.state.catalog = runtime_catalog
        application.state.challenge_repository = challenge_repository
        application.state.round_repository = round_repository
        application.state.generation_claims = generation_claims
        application.state.artifact_store = artifact_store
        application.state.artifact_reconciliation = artifact_reconciliation
        application.state.active_ai_provider = (
            "pi" if isinstance(pipeline, PiAIPipeline) else "fake"
        )
        application.state.game_round_service = game_round_service
        yield
    finally:
        try:
            if game_round_service is not None:
                await game_round_service.close()
        finally:
            try:
                if db is not None:
                    db.close()
            finally:
                for name in (
                    "db",
                    "catalog",
                    "challenge_repository",
                    "round_repository",
                    "generation_claims",
                    "artifact_store",
                    "artifact_reconciliation",
                    "active_ai_provider",
                    "game_round_service",
                ):
                    if hasattr(application.state, name):
                        delattr(application.state, name)


def _artifact_reconciliation_limit(application: FastAPI) -> int:
    value = getattr(
        application.state,
        "artifact_reconciliation_max_entries",
        _DEFAULT_ARTIFACT_RECONCILIATION_MAX_ENTRIES,
    )
    if type(value) is not int or value <= 0:
        raise ValueError("artifact_reconciliation_max_entries must be a positive integer")
    return value


def _build_ai_pipeline(
    application: FastAPI,
    generated_root: Path,
) -> tuple[FakeAIPipeline | PiAIPipeline, GeneratedArtifactStore | None, float]:
    """Build the explicitly selected provider without silent fallback."""

    configured = (
        application.state.ai_provider
        if hasattr(application.state, "ai_provider")
        else os.environ.get(AI_PROVIDER_ENV)
    )
    if configured is None or not str(configured).strip():
        raise RuntimeError(
            f"{AI_PROVIDER_ENV} must explicitly select the 'fake' or 'pi' AI provider"
        )
    selected = str(configured).strip()
    if selected == "fake":
        return FakeAIPipeline(), None, 10.0
    if selected != "pi":
        raise RuntimeError(f"unsupported AI provider: {selected!r}")

    executable = str(getattr(application.state, "pi_executable", DEFAULT_PI_EXECUTABLE))
    resolved_executable = shutil.which(executable)
    if resolved_executable is None:
        raise RuntimeError("Pi AI provider is configured but the pi executable is unavailable")
    bridge_path = Path(getattr(application.state, "pi_bridge_path", DEFAULT_PI_BRIDGE_PATH))
    if not bridge_path.is_file():
        raise RuntimeError("Pi AI provider is configured but the Codex bridge is unavailable")

    workspace_root = Path(
        getattr(application.state, "pi_workspace_root", DEFAULT_PI_WORKSPACE_ROOT)
    )
    artifact_store = GeneratedArtifactStore(workspace_root, generated_root)
    workspace_root = _prepare_runtime_directory(
        artifact_store.private_root,
        "Pi workspace root",
    )
    generated_root = _prepare_runtime_directory(
        artifact_store.published_root,
        "generated artifact root",
    )
    provider = str(getattr(application.state, "pi_provider", DEFAULT_PI_PROVIDER))
    model = str(getattr(application.state, "pi_model", DEFAULT_PI_MODEL))
    image_argv = _pi_rpc_argv(
        resolved_executable,
        provider,
        model,
        str(getattr(application.state, "pi_image_thinking", DEFAULT_PI_IMAGE_THINKING)),
    ) + (
        "--extension",
        str(bridge_path),
        "--no-tools",
        "--tools",
        "codex_imagegen",
    )
    evaluator_argv = _pi_rpc_argv(
        resolved_executable,
        provider,
        model,
        str(
            getattr(
                application.state,
                "pi_evaluator_thinking",
                DEFAULT_PI_EVALUATOR_THINKING,
            )
        ),
    ) + ("--no-tools",)
    timeout = float(getattr(application.state, "provider_timeout", DEFAULT_PI_TIMEOUT_SECONDS))
    pipeline = PiAIPipeline(
        image_argv=image_argv,
        evaluator_argv=evaluator_argv,
        target_static_root=Path(getattr(application.state, "dist_root", DIST_DIR)),
        artifact_store=artifact_store,
        max_stdout_bytes=int(
            getattr(application.state, "pi_max_output_bytes", DEFAULT_PI_MAX_OUTPUT_BYTES)
        ),
        max_stderr_bytes=int(
            getattr(application.state, "pi_max_output_bytes", DEFAULT_PI_MAX_OUTPUT_BYTES)
        ),
        rpc_cwd=workspace_root,
    )
    return pipeline, artifact_store, timeout


def _prepare_runtime_directory(path: Path, label: str) -> Path:
    """Create and resolve one configured runtime directory before serving requests."""

    candidate = path.expanduser()
    if candidate.is_symlink():
        raise RuntimeError(f"{label} must not be a symlink")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError(f"{label} is unavailable") from error
    if not candidate.is_dir():
        raise RuntimeError(f"{label} must be a directory")
    return candidate.resolve()


def _configure_generated_static_mount(application: FastAPI, generated_root: Path) -> None:
    """Bind /generated to the validated store root before request admission."""

    for route in application.router.routes:
        if isinstance(route, Mount) and route.name == "generated-artifacts":
            route.app = StaticFiles(directory=generated_root)
            return
    raise RuntimeError("generated artifact static mount is unavailable")


def _pi_rpc_argv(
    executable: str,
    provider: str,
    model: str,
    thinking: str,
) -> tuple[str, ...]:
    """Return the context-free, ephemeral baseline shared by Pi RPC workers."""

    return (
        executable,
        "--mode",
        "rpc",
        "--no-session",
        "--provider",
        provider,
        "--model",
        model,
        "--thinking",
        thinking,
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
        "--no-extensions",
    )


def _select_challenge(candidates: tuple[ChallengeSpec, ...]) -> ChallengeSpec:
    """Select one candidate independently for each service configure event."""

    return random.choice(candidates)


def _utc_now() -> datetime:
    """Return the aware UTC clock used by the round service."""

    return datetime.now(UTC)


app = FastAPI(title="Photo Prompt", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def ready(request: Request):
    """Render the global Ready scene."""

    return render_ready(request)


@app.post("/rounds", status_code=303)
async def start_round(
    request: Request,
    display_name: str = Form(default=""),
) -> RedirectResponse:
    """Create a durable anonymous-or-named round and open level selection."""

    try:
        record = await request.app.state.game_round_service.create_round(display_name)
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return RedirectResponse(url=f"/rounds/{record.id}/level", status_code=303)


@app.get("/rounds/{round_id}/level", response_class=HTMLResponse)
async def level_selection(request: Request, round_id: str):
    """Render level selection only for a round in its persisted setup state."""

    await _get_scene_round(request, round_id, GameState.LEVEL_SELECTION)
    return render_level_selection(request, round_id)


@app.post("/rounds/{round_id}/level", status_code=303)
async def configure_round(
    request: Request,
    round_id: str,
    level: str = Form(...),
) -> RedirectResponse:
    """Persist level and challenge selection before opening the challenge scene."""

    try:
        record = await request.app.state.game_round_service.configure_round(round_id, level)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if record.challenge_id is None:
        raise HTTPException(status_code=409, detail="Configured round has no challenge")
    return RedirectResponse(
        url=f"/rounds/{round_id}/challenge",
        status_code=303,
    )


@app.get("/rounds/{round_id}/challenge", response_class=HTMLResponse)
async def challenge_reveal(request: Request, round_id: str):
    """Render the durable challenge selected for this round."""

    record = await _get_scene_round(request, round_id, GameState.CHALLENGE_REVEAL)
    challenge = _challenge_for_round(request, record)
    return render_challenge_reveal(request, round_id, challenge)


@app.post("/rounds/{round_id}/challenge/continue", status_code=303)
async def continue_challenge(request: Request, round_id: str) -> RedirectResponse:
    """Advance the persisted challenge reveal into prompt entry."""

    try:
        await request.app.state.game_round_service.continue_challenge(round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse(url=f"/rounds/{round_id}/prompt", status_code=303)


@app.get("/rounds/{round_id}/prompt", response_class=HTMLResponse)
async def prompt_entry(request: Request, round_id: str):
    """Render the durable challenge and server-authorized prompt deadline."""

    record = await _get_scene_round(request, round_id, GameState.PROMPT_ENTRY)
    challenge = _challenge_for_round(request, record)
    if record.prompt_deadline is None:
        raise HTTPException(status_code=409, detail="Prompt deadline is unavailable")
    return render_prompt_entry(
        request,
        round_id,
        challenge,
        prompt_deadline=record.prompt_deadline,
    )


@app.post("/rounds/{round_id}/prompt")
async def submit_prompt(
    request: Request,
    round_id: str,
    prompt: str = Form(default=""),
    submission_reason: str = Form(...),
):
    """Persist prompt submission before handing off to the Generating scene."""

    try:
        reason = PromptSubmissionReason(submission_reason)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Unknown prompt submission reason") from error

    try:
        record = await request.app.state.game_round_service.submit_prompt(
            round_id,
            prompt,
            reason,
        )
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GameRoundDeadlineError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    if record.state is GameState.ABANDONED:
        return RedirectResponse(url="/", status_code=303)
    if record.challenge_id is None or record.prompt is None:
        raise HTTPException(status_code=409, detail="Submitted round is missing generation context")
    return RedirectResponse(url=f"/rounds/{round_id}/generating", status_code=303)


@app.get("/rounds/{round_id}/generating", response_class=HTMLResponse)
async def generating_scene(request: Request, round_id: str):
    """Render the persisted generation state and its server-owned context."""

    record = await _require_round_context(request, round_id)
    if record.state is GameState.GENERATING:
        challenge, prompt = _generation_context(request, record)
        status = await _get_generation_status(request, round_id)
        return render_generating(
            request,
            round_id,
            challenge,
            prompt,
            state=record.state,
            generation_status=status.state,
            failure=record.pipeline_failure,
        )
    if record.state is GameState.GENERATED_REVEAL:
        challenge, prompt = _generation_context(request, record)
        if (
            record.generated_artifact is None
            or record.score is None
            or record.reveal_deadline is None
        ):
            raise HTTPException(status_code=409, detail="Generated round is missing reveal data")
        status = await _get_generation_status(request, round_id)
        return render_generating(
            request,
            round_id,
            challenge,
            prompt,
            state=record.state,
            generation_status=status.state,
            generated_artifact=record.generated_artifact,
            score=record.score,
            reveal_deadline=record.reveal_deadline,
        )
    raise HTTPException(
        status_code=409,
        detail=f"Round is in {record.state.value}, not a generating state",
    )


@app.get("/rounds/{round_id}/generating/status")
async def generation_status(request: Request, round_id: str):
    """Return only the bounded persisted status of one generation round."""

    status = await _get_generation_status(request, round_id)
    content = {"state": status.state.value}
    if status.state is GenerationStatusState.CONFLICT:
        return JSONResponse(status_code=409, content=content)
    return content


@app.post("/rounds/{round_id}/generating/run", status_code=303)
async def run_generation(request: Request, round_id: str) -> RedirectResponse:
    """Start one service-owned generation attempt and return to its scene."""

    return await _run_generation(request, round_id)


@app.post("/rounds/{round_id}/generating/retry", status_code=303)
async def retry_generation(request: Request, round_id: str) -> RedirectResponse:
    """Retry generation through the same service-owned execution route."""

    return await _run_generation(request, round_id)


@app.post("/rounds/{round_id}/generating/exit", status_code=303)
async def exit_generation(request: Request, round_id: str) -> RedirectResponse:
    """Abandon generation through the service, fencing any late completion."""

    try:
        await request.app.state.game_round_service.abandon_generation(round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse(url="/", status_code=303)


@app.post("/rounds/{round_id}/generating/continue", status_code=303)
async def continue_generation(request: Request, round_id: str) -> RedirectResponse:
    """Persist the reveal transition before opening the Result scene."""

    try:
        await request.app.state.game_round_service.show_result(round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GameRoundDeadlineError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse(url=f"/rounds/{round_id}/result", status_code=303)


@app.get("/rounds/{round_id}/result", response_class=HTMLResponse)
async def result_scene(request: Request, round_id: str):
    """Render the persisted artifact, score, and feedback for a completed result."""

    record = await _get_scene_round(request, round_id, GameState.RESULT)
    if (
        record.generated_artifact is None
        or record.prompt_evaluation is None
        or record.image_evaluation is None
        or record.score is None
        or len(record.feedback) not in (2, 3)
        or any(not line.strip() for line in record.feedback)
        or record.pipeline_failure is not None
    ):
        raise HTTPException(status_code=422, detail="Result data is incomplete")

    challenge = _challenge_for_round(request, record)
    feedback = tuple(
        ("improvement" if index == len(record.feedback) - 1 else "strength", line)
        for index, line in enumerate(record.feedback)
    )
    return render_result(
        request,
        round_id,
        challenge,
        generated_artifact=record.generated_artifact,
        score=record.score,
        feedback=feedback,
    )


@app.get("/leaderboard", response_class=HTMLResponse)
async def public_leaderboard_scene(
    request: Request,
    level: Annotated[LevelGroup, Query()] = LevelGroup.P1_P3,
):
    """Render a persistent Top 4 leaderboard for one selected level."""

    try:
        rows = await request.app.state.game_round_service.get_public_leaderboard(level)
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return render_public_leaderboard(
        request,
        level=level.value,
        rows=rows,
    )


@app.post("/rounds/{round_id}/result/leaderboard", status_code=303)
async def continue_leaderboard(request: Request, round_id: str) -> RedirectResponse:
    """Persist completion before opening the terminal leaderboard projection."""

    try:
        await request.app.state.game_round_service.complete_round(round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse(url=f"/rounds/{round_id}/leaderboard", status_code=303)


@app.get("/rounds/{round_id}/leaderboard", response_class=HTMLResponse)
async def leaderboard_scene(request: Request, round_id: str):
    """Render the persisted completed-round leaderboard projection."""

    record = await _get_scene_round(request, round_id, GameState.LEADERBOARD)
    try:
        if request.app.state.game_round_service.leaderboard_deadline_elapsed(record):
            return RedirectResponse(
                url=f"/rounds/{round_id}/photo-print",
                status_code=303,
            )
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    try:
        projection = await request.app.state.game_round_service.get_leaderboard(round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    if record.level is None or record.score is None or record.leaderboard_deadline is None:
        raise HTTPException(status_code=422, detail="Leaderboard data is incomplete")
    return render_leaderboard(
        request,
        round_id,
        level=record.level.value,
        score=record.score.total_score,
        current_rank=projection.current_rank,
        rows=projection.entries,
        leaderboard_deadline=record.leaderboard_deadline,
    )


@app.get("/rounds/{round_id}/photo-print", response_class=HTMLResponse)
async def photo_print_scene(request: Request, round_id: str):
    """Render the read-only A5 print projection for a completed round."""

    record = await _get_scene_round(request, round_id, GameState.LEADERBOARD)
    if record.terminal_disposition is not TerminalDisposition.COMPLETED:
        raise HTTPException(status_code=409, detail="Photo Print requires a completed round")
    if record.level is None or record.generated_artifact is None or record.score is None:
        raise HTTPException(status_code=422, detail="Photo Print data is incomplete")
    return render_photo_print(
        request,
        round_id,
        display_name=record.display_name,
        level=record.level.value,
        generated_artifact=record.generated_artifact,
        score=record.score,
    )


async def _run_generation(request: Request, round_id: str) -> RedirectResponse:
    """Map one generation attempt's service outcomes to the native route contract."""

    try:
        await request.app.state.game_round_service.generate_round(round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GenerationAlreadyRunningError as error:
        raise HTTPException(status_code=409, detail="Generation already running") from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GameRoundConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse(url=f"/rounds/{round_id}/generating", status_code=303)


async def _require_round_context(request: Request, round_id: str):
    """Load one durable round, mapping repository errors to route contracts."""

    try:
        return await request.app.state.game_round_service.get_round(round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _get_generation_status(request: Request, round_id: str) -> GenerationStatus:
    """Load a bounded persisted generation status for HTML or JSON callers."""

    try:
        return await request.app.state.game_round_service.get_generation_status(round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    except GameRoundValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _get_scene_round(request: Request, round_id: str, expected_state: GameState):
    """Load a round and reject stale scene access with HTTP 409."""

    record = await _require_round_context(request, round_id)
    if record.state is not expected_state:
        raise HTTPException(
            status_code=409,
            detail=f"Round is in {record.state.value}, not {expected_state.value}",
        )
    return record


def _challenge_for_round(request: Request, record):
    if record.challenge_id is None:
        raise HTTPException(status_code=409, detail="Round has no challenge")
    return _get_challenge(request, record.challenge_id)


def _generation_context(request: Request, record):
    if record.challenge_id is None or record.prompt is None or not record.prompt.strip():
        raise HTTPException(status_code=409, detail="Round has no generation context")
    return _get_challenge(request, record.challenge_id), record.prompt


def _get_challenge(request: Request, challenge_id: str):
    try:
        return request.app.state.catalog.get(challenge_id)
    except (KeyError, ChallengeNotFoundError) as error:
        raise HTTPException(status_code=404, detail="Challenge not found") from error
    except ChallengeRepositoryError as error:
        raise HTTPException(status_code=422, detail="Stored challenge is invalid") from error


# Publish only the controlled generated-artifact root, never the full data tree.
app.mount(
    "/generated",
    StaticFiles(directory=DEFAULT_GENERATED_ROOT, check_dir=False),
    name="generated-artifacts",
)
# Keep SSR routes above this generated-browser fallback.
app.mount("/", StaticFiles(directory=DIST_DIR, check_dir=False), name="assets")
