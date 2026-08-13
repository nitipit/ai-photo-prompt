"""Jinja rendering helpers for the visible kiosk scenes."""

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .config import TEMPLATE_DIR
from .domain.models import FailureDetail, GameState, ImageArtifact, ScoreResult

templates = Jinja2Templates(directory=TEMPLATE_DIR)
templates.env.auto_reload = True


def render_ready(request: Request):
    """Render the attract screen for the next player."""

    return templates.TemplateResponse(request=request, name="ready.html", context={})


def render_level_selection(request: Request, round_id: str):
    """Render level selection for a durable round in setup."""

    return templates.TemplateResponse(
        request=request,
        name="level.html",
        context={"round_id": round_id},
    )


def render_leaderboard(
    request: Request,
    round_id: str,
    challenge,
    prompt: str,
    *,
    score: int,
    level: str,
    current_rank: int,
    rows,
):
    """Render the deterministic current-level leaderboard without persistence."""

    return templates.TemplateResponse(
        request=request,
        name="leaderboard.html",
        context={
            "round_id": round_id,
            "challenge": challenge,
            "prompt": prompt,
            "score": score,
            "level": level,
            "current_rank": current_rank,
            "rows": rows,
        },
    )


def render_challenge_reveal(request: Request, round_id: str, challenge):
    """Render the selected challenge image without exposing its design brief."""

    return templates.TemplateResponse(
        request=request,
        name="challenge.html",
        context={"round_id": round_id, "challenge": challenge},
    )


def render_prompt_entry(
    request: Request,
    round_id: str,
    challenge,
    *,
    prompt_deadline: str,
):
    """Render prompt entry with stored challenge and deadline context."""

    return templates.TemplateResponse(
        request=request,
        name="prompt.html",
        context={
            "round_id": round_id,
            "challenge": challenge,
            "prompt_deadline": prompt_deadline,
        },
    )


def render_generating(
    request: Request,
    round_id: str,
    challenge,
    prompt: str,
    *,
    state: GameState,
    failure: FailureDetail | None = None,
    generated_artifact: ImageArtifact | None = None,
    score: ScoreResult | None = None,
    reveal_deadline: str | None = None,
):
    """Render waiting, persisted failure, or persisted generated reveal state."""

    return templates.TemplateResponse(
        request=request,
        name="generating.html",
        context={
            "round_id": round_id,
            "challenge": challenge,
            "prompt": prompt,
            "generating_state": state.value,
            "failure": failure,
            "generated_artifact": generated_artifact,
            "score": score,
            "reveal_deadline": reveal_deadline,
        },
    )


def render_result(
    request: Request,
    round_id: str,
    challenge,
    prompt: str,
    *,
    score: int,
    feedback: tuple[tuple[str, str], ...],
):
    """Render deterministic demo feedback without implying real AI scoring."""

    return templates.TemplateResponse(
        request=request,
        name="result.html",
        context={
            "round_id": round_id,
            "challenge": challenge,
            "prompt": prompt,
            "score": score,
            "feedback": feedback,
        },
    )
