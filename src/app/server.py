"""FastAPI entry point for the first visible Photo Prompt checkpoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import DEMO_ROUND_ID, DIST_DIR
from .web import render_level_selection, render_ready


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

    if round_id != DEMO_ROUND_ID:
        raise HTTPException(status_code=404, detail="Round not found")
    return render_level_selection(request, round_id)


# Keep SSR routes above this generated-browser fallback.
app.mount("/", StaticFiles(directory=DIST_DIR, check_dir=False), name="assets")
