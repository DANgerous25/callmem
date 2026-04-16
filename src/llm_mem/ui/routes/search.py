"""Search page."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/search")
async def search(request: Request) -> str:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    query = request.query_params.get("q", "")
    results = []

    if query:
        results = engine.search(query, limit=50)

    return render_template(
        request.app, "search.html", query=query, results=results
    )
