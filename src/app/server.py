"""FastAPI entry point for the first visible Photo Prompt checkpoint."""

import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from .ai import FakeAIPipeline
from .config import DEFAULT_CATALOG_PATH, DEFAULT_DB_PATH, DIST_DIR
from .content.repository import ChallengeCatalog
from .domain.models import ChallengeSpec, GameState, LevelGroup, PromptSubmissionReason
from .persistence import RoundNotFoundError, ShelfDbGenerationClaims, ShelfDbRoundRepository
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

DEMO_SCORE = 82
DEMO_FEEDBACK = (
    ("strength", "บอกองค์ประกอบสำคัญได้ชัดเจน"),
    ("strength", "เลือกสีและบรรยากาศได้ตรงโจทย์"),
    ("improvement", "ลองเพิ่มรายละเอียดตำแหน่งของสิ่งต่าง ๆ"),
)
DEMO_CURRENT_RANK = 2
# Fixed rows keep this visible checkpoint deterministic; no ranking is computed here.
DEMO_LEADERBOARD_ROWS = (
    {
        "rank": 1,
        "name": "น้องมะลิ",
        "score": 96,
        "prompt": "กระต่ายเชฟยืนทำอาหารในครัวสีสดใส",
        "is_current": False,
    },
    {"rank": 2, "name": "รอบนี้", "score": DEMO_SCORE, "prompt": "", "is_current": True},
    {
        "rank": 2,
        "name": "น้องต้นกล้า",
        "score": 82,
        "prompt": "แพนเค้กยักษ์ลอยอยู่เหนือโต๊ะอาหาร",
        "is_current": False,
    },
    {
        "rank": 4,
        "name": "น้องพริม",
        "score": 74,
        "prompt": "ห้องครัวมีแสงอุ่นและดาวประกายรอบ ๆ",
        "is_current": False,
    },
)


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
    """Persist prompt submission before handing off to temporary Generating."""

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
    return RedirectResponse(
        url=_generating_url(round_id, record.challenge_id, record.prompt),
        status_code=303,
    )


@app.get("/rounds/{round_id}/generating", response_class=HTMLResponse)
async def generating_scene(
    request: Request,
    round_id: str,
    challenge_id: str | None = None,
    prompt: str | None = None,
    failure: str | None = None,
):
    """Render the temporary Generating scene from persisted setup context."""

    record = await _get_scene_round(request, round_id, GameState.GENERATING)
    challenge, stored_prompt = _generation_context(request, record)
    return render_generating(
        request,
        round_id,
        challenge,
        stored_prompt,
        failure=failure == "1",
    )


@app.post("/rounds/{round_id}/generating/retry", status_code=303)
async def retry_demo_generation(request: Request, round_id: str) -> RedirectResponse:
    """Retry the temporary Generating scene using stored challenge and prompt."""

    record = await _get_scene_round(request, round_id, GameState.GENERATING)
    challenge, prompt = _generation_context(request, record)
    return RedirectResponse(
        url=_generating_url(round_id, challenge.id, prompt),
        status_code=303,
    )


@app.post("/rounds/{round_id}/generating/exit", status_code=303)
async def exit_demo_generation(request: Request, round_id: str) -> RedirectResponse:
    """Exit the temporary failure state without creating a result or score."""

    record = await _get_scene_round(request, round_id, GameState.GENERATING)
    _generation_context(request, record)
    return RedirectResponse(url="/", status_code=303)


@app.post("/rounds/{round_id}/generating/continue", status_code=303)
async def continue_demo_generation(request: Request, round_id: str) -> RedirectResponse:
    """Redirect to the temporary Result scene with stored generation context."""

    record = await _get_scene_round(request, round_id, GameState.GENERATING)
    challenge, prompt = _generation_context(request, record)
    return RedirectResponse(
        url=_result_url(round_id, challenge.id, prompt),
        status_code=303,
    )


@app.get("/rounds/{round_id}/result", response_class=HTMLResponse)
async def result_scene(
    request: Request,
    round_id: str,
    challenge_id: str,
    prompt: str,
):
    """Render deterministic demo feedback for one existing round context."""

    await _require_round_context(request, round_id)
    challenge = _get_challenge(request, challenge_id)
    _validate_prompt(prompt)
    return render_result(
        request,
        round_id,
        challenge,
        prompt,
        score=DEMO_SCORE,
        feedback=DEMO_FEEDBACK,
    )


@app.post("/rounds/{round_id}/result/leaderboard", status_code=303)
async def continue_demo_leaderboard(
    request: Request,
    round_id: str,
    challenge_id: str = Form(...),
    prompt: str = Form(...),
    score: int = Form(...),
    level: str | None = Form(default=None),
) -> RedirectResponse:
    """Validate the fixed demo result before the temporary Leaderboard scene."""

    await _require_round_context(request, round_id)
    challenge = _get_challenge(request, challenge_id)
    selected_level = _validate_leaderboard_context(challenge, prompt, score, level)
    return RedirectResponse(
        url=_leaderboard_url(round_id, challenge_id, prompt, score, selected_level.value),
        status_code=303,
    )


@app.get("/rounds/{round_id}/leaderboard", response_class=HTMLResponse)
async def leaderboard_scene(
    request: Request,
    round_id: str,
    challenge_id: str,
    prompt: str,
    score: int,
    level: str,
):
    """Render the deterministic current-level leaderboard checkpoint."""

    await _require_round_context(request, round_id)
    challenge = _get_challenge(request, challenge_id)
    selected_level = _validate_leaderboard_context(challenge, prompt, score, level)
    rows = tuple(
        {
            **entry,
            "prompt": prompt if entry["is_current"] else entry["prompt"],
            "image_url": challenge.target_asset_url,
        }
        for entry in DEMO_LEADERBOARD_ROWS
    )
    return render_leaderboard(
        request,
        round_id,
        challenge,
        prompt,
        score=score,
        level=selected_level.value,
        current_rank=DEMO_CURRENT_RANK,
        rows=rows,
    )


def _validate_leaderboard_context(
    challenge,
    prompt: str,
    score: int,
    level: str | None,
) -> LevelGroup:
    _validate_prompt(prompt)
    if score != DEMO_SCORE:
        raise HTTPException(status_code=422, detail="Score does not match the demo result")
    selected_level = challenge.level
    if level is None:
        return selected_level
    try:
        requested_level = LevelGroup(level)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Unknown leaderboard level") from error
    if requested_level != selected_level:
        raise HTTPException(status_code=422, detail="Leaderboard context does not match the demo")
    return selected_level


def _validate_prompt(prompt: str) -> None:
    if len(prompt) > 1000:
        raise HTTPException(status_code=422, detail="Prompt must be 1000 characters or fewer")
    if not prompt.strip():
        raise HTTPException(status_code=422, detail="Prompt cannot be blank")


def _generating_url(round_id: str, challenge_id: str, prompt: str) -> str:
    """Build the explicitly temporary, URL-encoded visible-slice handoff."""

    query = urlencode({"challenge_id": challenge_id, "prompt": prompt})
    return f"/rounds/{round_id}/generating?{query}"


def _result_url(round_id: str, challenge_id: str, prompt: str) -> str:
    """Build the URL-encoded handoff into the temporary Result scene."""

    query = urlencode({"challenge_id": challenge_id, "prompt": prompt})
    return f"/rounds/{round_id}/result?{query}"


def _leaderboard_url(
    round_id: str,
    challenge_id: str,
    prompt: str,
    score: int,
    level: str,
) -> str:
    """Build the URL-encoded handoff into the temporary Leaderboard scene."""

    query = urlencode(
        {
            "challenge_id": challenge_id,
            "prompt": prompt,
            "score": score,
            "level": level,
        }
    )
    return f"/rounds/{round_id}/leaderboard?{query}"


async def _require_round_context(request: Request, round_id: str):
    """Load one durable round, mapping repository absence to HTTP 404."""

    try:
        return await request.app.state.game_round_service.get_round(round_id)
    except RoundNotFoundError as error:
        raise HTTPException(status_code=404, detail="Round not found") from error


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
