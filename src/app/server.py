"""FastAPI entry point for the first visible Photo Prompt checkpoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import DEMO_ROUND_ID, DIST_DIR
from .content.repository import CatalogValidationError, ChallengeCatalog
from .domain.models import LevelGroup, PromptSubmissionReason
from .web import (
    render_challenge_reveal,
    render_level_selection,
    render_prompt_entry,
    render_ready,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Reserve the application lifecycle seam for later round services."""

    yield


app = FastAPI(title="Photo Prompt", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def ready(request: Request):
    """Render the global Ready scene."""

    return render_ready(request)


@app.post("/rounds", status_code=303)
async def start_demo_round() -> RedirectResponse:
    """Start the visible checkpoint without creating fake persistence."""

    return RedirectResponse(
        url=f"/rounds/{DEMO_ROUND_ID}/level",
        status_code=303,
    )


@app.get("/rounds/{round_id}/level", response_class=HTMLResponse)
async def level_selection(request: Request, round_id: str):
    """Render Level Selection for the explicitly temporary demo round only."""

    _require_demo_round(round_id)
    return render_level_selection(request, round_id)


@app.post("/rounds/{round_id}/level", status_code=303)
async def configure_demo_round(
    round_id: str,
    display_name: str = Form(default=""),
    level: str = Form(...),
) -> RedirectResponse:
    """Select the first approved challenge for the submitted level."""

    _require_demo_round(round_id)
    try:
        selected_level = LevelGroup(level)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Unknown level") from error

    challenges = _load_challenge_catalog().for_level(selected_level)
    if not challenges:
        raise HTTPException(status_code=500, detail="No challenge is available for this level")

    # ``display_name`` is accepted at this temporary seam but has no persistence owner yet.
    _ = display_name
    challenge = challenges[0]
    return RedirectResponse(
        url=f"/rounds/{round_id}/challenge?challenge_id={challenge.id}",
        status_code=303,
    )


@app.get("/rounds/{round_id}/challenge", response_class=HTMLResponse)
async def challenge_reveal(request: Request, round_id: str, challenge_id: str):
    """Render the selected challenge image from the generated runtime catalog."""

    _require_demo_round(round_id)
    try:
        challenge = _load_challenge_catalog().get(challenge_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Challenge not found") from error
    return render_challenge_reveal(request, round_id, challenge)


@app.post("/rounds/{round_id}/challenge/continue", status_code=303)
async def continue_demo_challenge(
    round_id: str,
    challenge_id: str = Form(...),
) -> RedirectResponse:
    """Carry the selected challenge into the temporary Prompt Entry scene."""

    _require_demo_round(round_id)
    _get_challenge(challenge_id)
    return RedirectResponse(
        url=f"/rounds/{round_id}/prompt?challenge_id={challenge_id}",
        status_code=303,
    )


@app.get("/rounds/{round_id}/prompt", response_class=HTMLResponse)
async def prompt_entry(request: Request, round_id: str, challenge_id: str):
    """Render Prompt Entry with only the selected challenge reference image."""

    _require_demo_round(round_id)
    return render_prompt_entry(request, round_id, _get_challenge(challenge_id))


@app.post("/rounds/{round_id}/prompt")
async def submit_demo_prompt(
    round_id: str,
    challenge_id: str = Form(...),
    prompt: str = Form(default=""),
    submission_reason: str = Form(...),
):
    """Validate prompt input before the deliberately unimplemented Generating seam."""

    _require_demo_round(round_id)
    _get_challenge(challenge_id)
    if len(prompt) > 1000:
        raise HTTPException(status_code=422, detail="Prompt must be 1000 characters or fewer")
    try:
        reason = PromptSubmissionReason(submission_reason)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Unknown prompt submission reason") from error

    if not prompt.strip():
        if reason is PromptSubmissionReason.TIMEOUT:
            return RedirectResponse(url="/", status_code=303)
        raise HTTPException(status_code=422, detail="Prompt cannot be blank")

    raise HTTPException(
        status_code=501,
        detail="Generating scene is not implemented in this temporary seam",
    )


def _require_demo_round(round_id: str) -> None:
    if round_id != DEMO_ROUND_ID:
        raise HTTPException(status_code=404, detail="Round not found")


def _load_challenge_catalog() -> ChallengeCatalog:
    try:
        return ChallengeCatalog.load(DIST_DIR / "catalog.json")
    except CatalogValidationError as error:
        raise HTTPException(status_code=500, detail="Challenge catalog is unavailable") from error


def _get_challenge(challenge_id: str):
    try:
        return _load_challenge_catalog().get(challenge_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Challenge not found") from error


# Keep SSR routes above this generated-browser fallback.
app.mount("/", StaticFiles(directory=DIST_DIR, check_dir=False), name="assets")
