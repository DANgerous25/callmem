"""Settings page with live briefing preview."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.responses import HTMLResponse

router = APIRouter()


def _config_path(engine: Any) -> Path:
    return engine.db.db_path.parent / "config.toml"


def _config_to_form(config: Any) -> dict[str, Any]:
    return {
        "briefing_max_tokens": config.briefing.max_tokens,
        "briefing_max_per_type": config.briefing.max_per_type,
        "briefing_include_last_session": config.briefing.include_last_session,
        "briefing_default_view": config.briefing.default_view,
        "briefing_auto_write": config.briefing.auto_write_session_summary,
        "llm_backend": config.llm.backend,
        "ollama_model": config.ollama.model,
        "ollama_endpoint": config.ollama.endpoint,
        "ui_port": config.ui.port,
        "ui_host": config.ui.host,
        "extraction_batch_size": config.extraction.batch_size,
    }


@router.get("/settings")
async def settings_page(request: Request) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    form = _config_to_form(engine.config)
    flash = request.query_params.get("flash")

    return render_template(
        request.app,
        "settings.html",
        form=form,
        flash=flash,
    )


@router.post("/settings")
async def save_settings(request: Request) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    form_data = await request.form()

    config_file = _config_path(engine)
    if config_file.exists():
        shutil.copy2(config_file, config_file.with_suffix(".toml.bak"))

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    existing = {}
    if config_file.exists():
        raw = config_file.read_text()
        try:
            existing = tomllib.loads(raw)
        except Exception:
            try:
                import json
                existing = json.loads(raw)
            except Exception:
                existing = {}

    max_tokens = int(form_data.get("briefing_max_tokens", 2000))
    existing.setdefault("briefing", {})["max_tokens"] = max_tokens
    existing["briefing"]["max_per_type"] = int(
        form_data.get("briefing_max_per_type", 20)
    )
    existing["briefing"]["include_last_session"] = (
        "briefing_include_last_session" in form_data
    )
    existing["briefing"]["default_view"] = form_data.get(
        "briefing_default_view", "key_points"
    )
    existing["briefing"]["auto_write_session_summary"] = (
        "briefing_auto_write" in form_data
    )

    existing.setdefault("llm", {})["backend"] = form_data.get(
        "llm_backend", "ollama"
    )
    existing.setdefault("ollama", {})["model"] = form_data.get(
        "ollama_model", "qwen3:8b"
    )
    existing["ollama"]["endpoint"] = form_data.get(
        "ollama_endpoint", "http://localhost:11434"
    )

    existing.setdefault("ui", {})["port"] = int(
        form_data.get("ui_port", 9090)
    )
    existing["ui"]["host"] = form_data.get("ui_host", "0.0.0.0")

    existing.setdefault("extraction", {})["batch_size"] = int(
        form_data.get("extraction_batch_size", 10)
    )

    try:
        import tomli_w
        config_file.write_text(tomli_w.dumps(existing))
    except ImportError:
        import json
        config_file.write_text(json.dumps(existing, indent=2))

    from llm_mem.models.config import Config
    engine.config = Config.from_dict(existing)

    form = _config_to_form(engine.config)
    return render_template(
        request.app,
        "settings.html",
        form=form,
        flash="saved",
    )


@router.get("/partials/briefing-preview")
async def briefing_preview(request: Request) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    max_tokens = request.query_params.get("max_tokens")
    kwargs: dict[str, Any] = {}
    if max_tokens:
        kwargs["max_tokens"] = int(max_tokens)

    briefing_data = engine.get_briefing(**kwargs)
    return render_template(
        request.app,
        "briefing_preview_partial.html",
        briefing=briefing_data,
    )
