"""Files tab — browse the observation timeline per file."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/files")
async def files_page(request: Request) -> HTMLResponse:
    from callmem.ui.app import render_template

    engine = request.app.state.engine
    files = engine.repo.list_files_with_observations(engine.project_id)
    stats = engine.file_context_stats()

    return render_template(
        request.app,
        "files.html",
        files=files,
        stats=stats,
    )


@router.get("/files/timeline")
async def file_timeline(request: Request) -> HTMLResponse:
    from callmem.ui.app import render_template

    engine = request.app.state.engine
    path = request.query_params.get("path", "")
    context = engine.get_file_context(path) if path else None

    return render_template(
        request.app,
        "file_timeline.html",
        path=path,
        context=context,
    )
