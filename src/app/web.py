"""Jinja rendering helpers for the visible kiosk scenes."""

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .config import DEMO_ROUND_ID, TEMPLATE_DIR

templates = Jinja2Templates(directory=TEMPLATE_DIR)
templates.env.auto_reload = True


def render_ready(request: Request):
    """Render the attract screen for the next player."""

    return templates.TemplateResponse(
        request=request,
        name="ready.html",
        context={"demo_round_id": DEMO_ROUND_ID},
    )


def render_level_selection(request: Request, round_id: str):
    """Render level selection for the temporary demo round seam."""

    return templates.TemplateResponse(
        request=request,
        name="level.html",
        context={"round_id": round_id},
    )


def render_challenge_reveal(request: Request, round_id: str, challenge):
    """Render the selected challenge image without exposing its design brief."""

    return templates.TemplateResponse(
        request=request,
        name="challenge.html",
        context={"round_id": round_id, "challenge": challenge},
    )


def render_prompt_entry(request: Request, round_id: str, challenge):
    """Render Prompt Entry with the selected challenge as a visual-only reference."""

    return templates.TemplateResponse(
        request=request,
        name="prompt.html",
        context={"round_id": round_id, "challenge": challenge},
    )
