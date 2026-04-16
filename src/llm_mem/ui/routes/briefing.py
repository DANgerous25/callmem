"""Briefing preview page."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/briefing")
async def briefing(request: Request) -> str:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    focus = request.query_params.get("focus")
    max_tokens = request.query_params.get("max_tokens")

    kwargs: dict[int | str | None, int | str | None] = {}
    if focus:
        kwargs["focus"] = focus
    if max_tokens:
        kwargs["max_tokens"] = int(max_tokens)

    briefing_data = engine.get_briefing(**kwargs)

    return render_template(
        request.app,
        "briefing.html",
        briefing=briefing_data,
        focus=focus or "",
    )


@router.post("/briefing/regenerate")
async def regenerate_briefing(request: Request) -> str:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    briefing_data = engine.get_briefing()
    return render_template(
        request.app,
        "briefing.html",
        briefing=briefing_data,
        focus="",
        partial=True,
    )
