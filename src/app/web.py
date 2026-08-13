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
