"""Integration tests for the MCP server.

Tests the MCP server through the actual MCP protocol using the SDK's
in-memory transport.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from llm_mem.mcp.server import create_server


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a project directory with initialized database."""
    from llm_mem.core.database import Database

    llm_dir = tmp_path / ".llm-mem"
    llm_dir.mkdir()
    db = Database(llm_dir / "memory.db")
    db.initialize()
    return tmp_path


@pytest.fixture
def mcp_server(project_dir: Path) -> object:
    return create_server(project_dir)


class TestMCPToolsList:
    @pytest.mark.anyio
    async def test_list_tools(self, mcp_server: object) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(mcp_server) as client:
            result = await client.list_tools()
            tool_names = [t.name for t in result.tools]
            assert "mem_session_start" in tool_names
            assert "mem_session_end" in tool_names
            assert "mem_ingest" in tool_names
            assert "mem_search" in tool_names
            assert "mem_get_tasks" in tool_names
            assert "mem_pin" in tool_names

    @pytest.mark.anyio
    async def test_tool_descriptions(self, mcp_server: object) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(mcp_server) as client:
            result = await client.list_tools()
            for tool in result.tools:
                assert tool.description
                assert tool.inputSchema


class TestMCPToolCalls:
    @pytest.mark.anyio
    async def test_session_start(self, mcp_server: object) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(mcp_server) as client:
            result = await client.call_tool("mem_session_start", {
                "agent_name": "test",
            })
            text = result.content[0].text
            data = json.loads(text)
            assert "session_id" in data
            assert data["briefing"] == "Session started."

    @pytest.mark.anyio
    async def test_ingest_and_search(self, mcp_server: object) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(mcp_server) as client:
            await client.call_tool("mem_session_start", {})
            await client.call_tool("mem_ingest", {
                "events": [{"type": "decision", "content": "Use Redis for caching"}],
            })

            result = await client.call_tool("mem_search", {"query": "Redis caching"})
            data = json.loads(result.content[0].text)
            assert len(data["results"]) > 0

    @pytest.mark.anyio
    async def test_session_end(self, mcp_server: object) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(mcp_server) as client:
            await client.call_tool("mem_session_start", {})
            result = await client.call_tool("mem_session_end", {
                "note": "finished work",
            })
            data = json.loads(result.content[0].text)
            assert data["status"] == "ended"

    @pytest.mark.anyio
    async def test_get_tasks(self, mcp_server: object) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(mcp_server) as client:
            result = await client.call_tool("mem_get_tasks", {"status": "open"})
            data = json.loads(result.content[0].text)
            assert data["tasks"] == []

    @pytest.mark.anyio
    async def test_unknown_tool_returns_error(self, mcp_server: object) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(mcp_server) as client:
            result = await client.call_tool("nonexistent_tool", {})
            data = json.loads(result.content[0].text)
            assert "error" in data

    @pytest.mark.anyio
    async def test_session_end_without_start(self, mcp_server: object) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(mcp_server) as client:
            result = await client.call_tool("mem_session_end", {})
            data = json.loads(result.content[0].text)
            assert "error" in data
