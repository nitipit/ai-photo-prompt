"""FastAPI entry point for the first visible Photo Prompt checkpoint."""

import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from .ai import FakeAIPipeline
from .config import DEFAULT_CATALOG_PATH, DEFAULT_DB_PATH, DIST_DIR
from .content.repository import ChallengeCatalog
from .domain.models import ChallengeSpec, GameState, PromptSubmissionReason
from .persistence import (
    GenerationAlreadyRunningError,
    RoundNotFoundError,
    ShelfDbGenerationClaims,
    ShelfDbRoundRepository,
)
from .services import (
    GameRoundConflictError,
    GameRoundDeadlineError,
    GameRoundService,
    GameRoundValidationError,
)
from .web import (
    render_challenge_reveal,
    render_generating,
    render_leaderboard,
    render_level_selection,
    render_prompt_entry,
    render_ready,
    render_result,
)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Own the one-process local runtime dependencies for the application."""

    catalog_path = Path(getattr(application.state, "catalog_path", DEFAULT_CATALOG_PATH))
    db_path = Path(getattr(application.state, "db_path", DEFAULT_DB_PATH))
    db: DB | None = None
    try:
        catalog = ChallengeCatalog.load(catalog_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = DB(str(db_path))
        round_repository = ShelfDbRoundRepository(db)
        generation_claims = ShelfDbGenerationClaims(db)
        game_round_service = GameRoundService(
            round_repository,
            catalog,
            _select_challenge,
            _utc_now,
            generation_claims=generation_claims,
            pipeline=FakeAIPipeline(),
            owner_instance=str(uuid4()),
        )
        application.state.db = db
        application.state.catalog = catalog
        application.state.round_repository = round_repository
        application.state.generation_claims = generation_claims
        application.state.game_round_service = game_round_service
        yield
    finally:
        try:
            if db is not None:
                db.close()
        finally:
            for name in (
                "db",
                "catalog",
                "round_repository",
                "generation_claims",
                "game_round_service",
            ):
                if hasattr(application.state, name):
                    delattr(application.state, name)


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

    if not prompt.strip() and reason is PromptSubmissionReason.TIMEOUT:
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
        return render_generating(
            request,
            round_id,
            challenge,
            prompt,
            state=record.state,
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
        return render_generating(
            request,
            round_id,
            challenge,
            prompt,
            state=record.state,
            generated_artifact=record.generated_artifact,
            score=record.score,
            reveal_deadline=record.reveal_deadline,
        )
    raise HTTPException(
        status_code=409,
        detail=f"Round is in {record.state.value}, not a generating state",
    )


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
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Challenge not found") from error


# Keep SSR routes above this generated-browser fallback.
app.mount("/", StaticFiles(directory=DIST_DIR, check_dir=False), name="assets")
