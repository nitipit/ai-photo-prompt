"""FastAPI entry point for the first visible Photo Prompt checkpoint."""

import asyncio
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

from .ai.generated_artifacts import GeneratedArtifactStore
from .ai.pi_pipeline import PiAIPipeline
from .config import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_GENERATED_ROOT,
    DEFAULT_PI_EXECUTABLE,
    DIST_DIR,
    ConfigError,
    RuntimeConfig,
    load_config,
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
    AIPipelineRunner,
    GameRoundConflictError,
    GameRoundDeadlineError,
    GameRoundService,
    GameRoundValidationError,
    GenerationStatus,
)
from .services.staff import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    SESSION_PATH,
    StaffAuth,
    StaffCooldownError,
    StaffCSRFError,
    StaffLoginError,
    StaffUnavailableError,
    artifact_is_available,
    search_completed_rounds,
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
    render_staff_login,
    render_staff_search,
)

_DEFAULT_ARTIFACT_RECONCILIATION_MAX_ENTRIES = 10_000


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Own the one-process local runtime dependencies for the application."""

    application.state.staff_auth = StaffAuth()
    injected_pipeline = getattr(application.state, "ai_pipeline", None)
    runtime_config: RuntimeConfig | None = getattr(application.state, "runtime_config", None)
    if runtime_config is None and injected_pipeline is None:
        try:
            runtime_config = load_config()
        except ConfigError as error:
            raise RuntimeError(str(error)) from error
    application.state.runtime_config = runtime_config

    config_paths = runtime_config.paths if runtime_config is not None else None
    catalog_path = Path(
        getattr(
            application.state,
            "catalog_path",
            config_paths.catalog_path if config_paths else DEFAULT_CATALOG_PATH,
        )
    )
    db_path = Path(
        getattr(
            application.state,
            "db_path",
            config_paths.db_path if config_paths else DEFAULT_DB_PATH,
        )
    )
    configured_generated_root = Path(
        getattr(
            application.state,
            "generated_root",
            config_paths.generated_root if config_paths else DEFAULT_GENERATED_ROOT,
        )
    )
    pipeline, artifact_store, provider_timeout = _build_ai_pipeline(
        application,
        configured_generated_root,
        runtime_config,
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
            seconds=float(
                getattr(
                    application.state,
                    "claim_lease_seconds",
                    runtime_config.pi.claim_lease_seconds if runtime_config else 30.0,
                )
            )
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
                seconds=float(
                    getattr(
                        application.state,
                        "claim_heartbeat_seconds",
                        runtime_config.pi.claim_heartbeat_seconds if runtime_config else 5.0,
                    )
                )
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
            "pi" if isinstance(pipeline, PiAIPipeline) else "injected"
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
                    "runtime_config",
                    "staff_auth",
                ):
                    if hasattr(application.state, name):
                        delattr(application.state, name)


def _artifact_reconciliation_limit(application: FastAPI) -> int:
    runtime_config = getattr(application.state, "runtime_config", None)
    value = getattr(
        application.state,
        "artifact_reconciliation_max_entries",
        runtime_config.pi.reconciliation_max_entries
        if runtime_config is not None
        else _DEFAULT_ARTIFACT_RECONCILIATION_MAX_ENTRIES,
    )
    if type(value) is not int or value <= 0:
        raise ValueError("artifact_reconciliation_max_entries must be a positive integer")
    return value


def _build_ai_pipeline(
    application: FastAPI,
    generated_root: Path,
    runtime_config: RuntimeConfig | None,
) -> tuple[AIPipelineRunner, GeneratedArtifactStore | None, float]:
    """Build production Pi from the validated config or use explicit DI in tests."""

    injected = getattr(application.state, "ai_pipeline", None)
    if injected is not None:
        if not hasattr(injected, "run") or not hasattr(injected, "rollback_attempt"):
            raise RuntimeError("injected AI pipeline does not implement the pipeline boundary")
        return (
            injected,
            getattr(application.state, "artifact_store", None),
            float(getattr(application.state, "provider_timeout", 10.0)),
        )
    if runtime_config is None:
        raise RuntimeError("runtime configuration is required when no AI pipeline is injected")

    pi = runtime_config.pi
    resolved_executable = shutil.which(DEFAULT_PI_EXECUTABLE)
    if resolved_executable is None:
        raise RuntimeError("Pi AI provider is configured but the pi executable is unavailable")
    bridge_path = runtime_config.paths.pi_bridge_path
    if not bridge_path.is_file():
        raise RuntimeError("Pi AI provider is configured but the Codex bridge is unavailable")

    workspace_root = runtime_config.paths.pi_workspace_root
    artifact_store = GeneratedArtifactStore(
        workspace_root,
        generated_root,
        max_bytes=pi.max_artifact_bytes,
        max_width=pi.max_artifact_width,
        max_height=pi.max_artifact_height,
    )
    workspace_root = _prepare_runtime_directory(
        artifact_store.private_root,
        "Pi workspace root",
    )
    generated_root = _prepare_runtime_directory(
        artifact_store.published_root,
        "generated artifact root",
    )
    image_argv = _pi_rpc_argv(
        resolved_executable,
        pi.provider,
        pi.model,
        pi.image_thinking,
    ) + (
        "--extension",
        str(bridge_path),
        "--no-tools",
        "--tools",
        "codex_imagegen",
    )
    evaluator_argv = _pi_rpc_argv(
        resolved_executable,
        pi.provider,
        pi.model,
        pi.evaluator_thinking,
    ) + ("--no-tools",)
    pipeline = PiAIPipeline(
        image_argv=image_argv,
        evaluator_argv=evaluator_argv,
        target_static_root=runtime_config.paths.target_static_root,
        artifact_store=artifact_store,
        max_stdout_bytes=pi.max_stdout_bytes,
        max_stderr_bytes=pi.max_stderr_bytes,
        rpc_cwd=workspace_root,
        max_concurrent_attempts=pi.max_concurrent_attempts,
    )
    return pipeline, artifact_store, pi.timeout_seconds


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


@app.get("/health")
async def health(request: Request):
    """Expose readiness and active generation count, without player data."""

    service = getattr(request.app.state, "game_round_service", None)
    if service is None:
        return JSONResponse(status_code=503, content={"ready": False, "active_generation_count": 0})
    return {
        "ready": True,
        "active_generation_count": service.active_generation_count,
    }


@app.get("/staff/login", response_class=HTMLResponse)
async def staff_login_page(request: Request):
    """Show the staff PIN form only when a valid secret is configured."""

    auth = _staff_auth(request)
    if not auth.available:
        raise HTTPException(status_code=404, detail="Staff search is unavailable")
    token = auth.issue_login_csrf()
    if token is None:
        raise HTTPException(status_code=404, detail="Staff search is unavailable")
    response = render_staff_login(request, csrf_token=token)
    _mark_staff_no_store(response)
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        max_age=900,
        secure=True,
        httponly=False,
        samesite="strict",
        path=SESSION_PATH,
    )
    return response


@app.post("/staff/login", status_code=303)
async def staff_login(
    request: Request,
    pin: str = Form(default=""),
    csrf: str = Form(default=""),
):
    """Authenticate a staff member without reflecting secrets or player data."""

    auth = _staff_auth(request)
    if not auth.available:
        raise HTTPException(status_code=404, detail="Staff search is unavailable")
    client_key = request.client.host if request.client is not None else "unknown"
    try:
        token = auth.verify_login(
            pin,
            csrf,
            request.cookies.get(LOGIN_CSRF_COOKIE),
            client_key,
        )
    except StaffUnavailableError as error:
        raise HTTPException(status_code=404, detail="Staff search is unavailable") from error
    except StaffCSRFError as error:
        raise HTTPException(status_code=403, detail="Invalid CSRF token") from error
    except StaffCooldownError:
        response = render_staff_login(request, csrf_token=csrf, error="ลองใหม่อีกครั้งภายหลัง")
        return _staff_response(response, status_code=429)
    except StaffLoginError:
        response = render_staff_login(request, csrf_token=csrf, error="PIN ไม่ถูกต้อง")
        return _staff_response(response, status_code=401)
    response = RedirectResponse(url="/staff/search", status_code=303)
    _mark_staff_no_store(response)
    _set_staff_session_cookie(response, token)
    return response


@app.get("/staff/search", response_class=HTMLResponse)
async def staff_search_page(request: Request, page: str = "1"):
    """Render the private latest/search result page; the URL contains page only."""

    session = _require_staff_session(request)
    requested_page = _safe_page(page)
    session.page = requested_page
    result = _staff_search_projection(request, session.search_term, requested_page)
    response = render_staff_search(request, page=result, csrf_token=session.csrf_token)
    _mark_staff_no_store(response)
    return response


@app.post("/staff/search", status_code=303)
async def staff_search_submit(
    request: Request,
    query: str = Form(default=""),
    csrf: str = Form(default=""),
):
    """Store a bounded private search term in the opaque session state."""

    session = _require_staff_session(request)
    _require_staff_csrf(request, session, csrf)
    session.search_term = query.strip()[:50]
    session.page = 1
    response = RedirectResponse(url="/staff/search?page=1", status_code=303)
    _mark_staff_no_store(response)
    return response


@app.post("/staff/search/clear", status_code=303)
async def staff_search_clear(request: Request, csrf: str = Form(default="")):
    """Reset private search state to newest completed rounds."""

    session = _require_staff_session(request)
    _require_staff_csrf(request, session, csrf)
    session.search_term = ""
    session.page = 1
    response = RedirectResponse(url="/staff/search?page=1", status_code=303)
    _mark_staff_no_store(response)
    return response


@app.post("/staff/logout", status_code=303)
async def staff_logout(request: Request, csrf: str = Form(default="")):
    """Invalidate the opaque session and return to the kiosk Ready scene."""

    session = _require_staff_session(request)
    _require_staff_csrf(request, session, csrf)
    auth = _staff_auth(request)
    auth.logout(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse(url="/", status_code=303)
    _mark_staff_no_store(response)
    response.delete_cookie(SESSION_COOKIE, path=SESSION_PATH)
    return response


@app.get("/staff/rounds/{round_id}/photo-print", response_class=HTMLResponse)
async def staff_photo_print(request: Request, round_id: str, page: str = ""):
    """Render the existing A5 print projection for an authenticated staff member."""

    session = _require_staff_session(request)
    requested_page = _safe_page(page) if page else session.page
    session.page = requested_page
    try:
        record = await asyncio.to_thread(request.app.state.round_repository.get, round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error
    if record.terminal_disposition is not TerminalDisposition.COMPLETED:
        raise HTTPException(status_code=409, detail="Photo Print requires a completed round")
    if record.generated_artifact is None or record.score is None or record.level is None:
        raise HTTPException(status_code=422, detail="Photo Print data is incomplete")
    request.state.round_display_name = record.display_name
    generated_root = _staff_generated_root(request)
    if not artifact_is_available(record.generated_artifact.url, generated_root):
        raise HTTPException(status_code=409, detail="Image is unavailable")
    response = render_photo_print(
        request,
        round_id,
        display_name=record.display_name,
        level=record.level.value if record.level else "",
        generated_artifact=record.generated_artifact,
        score=record.score,
        return_url=f"/staff/search?page={requested_page}",
        print_available=True,
    )
    _mark_staff_no_store(response)
    return response


@app.post("/rounds", status_code=303)
async def start_round(
    request: Request,
    display_name: str = Form(...),
) -> RedirectResponse:
    """Create a durable named round and open level selection."""

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
    except GameRoundDeadlineError:
        # A browser timer may fire a fraction before the server-owned deadline.
        # Preserve the state-machine guard and return to the reveal scene instead
        # of exposing a transient timing error to the player.
        return RedirectResponse(
            url=f"/rounds/{round_id}/generating", status_code=303
        )
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


def _staff_auth(request: Request) -> StaffAuth:
    auth = getattr(request.app.state, "staff_auth", None)
    if not isinstance(auth, StaffAuth):
        raise HTTPException(status_code=404, detail="Staff search is unavailable")
    return auth


def _require_staff_session(request: Request):
    auth = _staff_auth(request)
    if not auth.available:
        raise HTTPException(status_code=404, detail="Staff search is unavailable")
    session = auth.session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(
            status_code=303,
            detail="Staff login required",
            headers={"Location": "/staff/login"},
        )
    return session


def _require_staff_csrf(request: Request, session, csrf: str) -> None:
    if not _staff_auth(request).verify_csrf(session, csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _safe_page(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return min(max(parsed, 1), 1_000_000)


def _staff_generated_root(request: Request) -> Path:
    store = getattr(request.app.state, "artifact_store", None)
    if isinstance(store, GeneratedArtifactStore):
        return store.published_root
    return Path(getattr(request.app.state, "generated_root", DEFAULT_GENERATED_ROOT)).resolve()


def _staff_search_projection(request: Request, term: str, page: int):
    return search_completed_rounds(
        request.app.state.round_repository,
        _staff_generated_root(request),
        term=term,
        page=page,
    )


def _mark_staff_no_store(response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _staff_response(response, *, status_code: int):
    response.status_code = status_code
    _mark_staff_no_store(response)
    return response


def _set_staff_session_cookie(response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="strict",
        path=SESSION_PATH,
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
        record = await request.app.state.game_round_service.get_round(round_id)
        request.state.round_display_name = record.display_name
        return record
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
