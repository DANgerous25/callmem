"""Search page."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/search")
async def search(request: Request) -> HTMLResponse:
    from callmem.ui.app import render_template

    engine = request.app.state.engine
    query = request.query_params.get("q", "")
    results = []

    if query:
        results = engine.search(query, limit=50)

    return render_template(
        request.app, "search.html", query=query, results=results
    )
