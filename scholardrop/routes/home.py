"""Landing page: pick a repository (DESIGN.md §6 step 1)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    registry = request.app.state.registry
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {"instances": list(registry.instances.values())},
    )


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Liveness probe -- also a quick way to confirm config actually loaded."""
    registry = request.app.state.registry
    return JSONResponse(
        {
            "status": "ok",
            "instances": sorted(registry.instances),
            "sessions": len(request.app.state.sessions),
        }
    )
