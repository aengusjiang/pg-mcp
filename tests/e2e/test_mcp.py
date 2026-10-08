"""End-to-end tests for the MCP server tool contract.

These run the real server module, calling the FastMCP tool function
directly (the stdio transport layer is exercised manually with the MCP
inspector — see docs/DEVELOPMENT.md). Environment-dependent tests use the
blog_environment guard and skip without the fixture databases or a valid
OPENAI_API_KEY.
"""

import pytest

import pg_mcp.server as server_module
from pg_mcp.server import lifespan, mcp, query

pytestmark = pytest.mark.integration


async def test_query_before_initialization_returns_server_not_initialized() -> None:
    """Calling the tool outside a lifespan yields server_not_initialized.

    This path needs no database or LLM, so it runs unguarded.
    """
    original = server_module._orchestrator
    server_module._orchestrator = None
    try:
        result = await query(question="SELECT 1", return_type="sql")
    finally:
        server_module._orchestrator = original

    assert result["success"] is False
    assert result["error"]["code"] == "server_not_initialized"


class TestMCPServerTool:
    """Tool contract over a real server lifespan."""

    @pytest.fixture(autouse=True)
    def _environment(self, blog_environment: None) -> None:
        """Both guards (databases reachable, LLM key present) are required."""

    async def test_lifespan_starts_and_stops(self) -> None:
        """Entering and exiting the lifespan initializes and cleans up."""
        async with lifespan(mcp):
            assert server_module._orchestrator is not None
            assert server_module._pools is not None

    async def test_successful_query_response_shape(self) -> None:
        """A successful response carries the full contract fields."""
        async with lifespan(mcp):
            result = await query(
                question="How many users are there?",
                return_type="result",
            )

        assert result["success"] is True, result.get("error")
        for key in ("success", "generated_sql", "data", "confidence", "tokens_used",
                    "request_id"):
            assert key in result, f"missing field: {key}"
        assert isinstance(result["data"]["rows"], list)
        assert isinstance(result["data"]["row_count"], int)

    async def test_sql_only_response_has_no_data(self) -> None:
        """return_type='sql' omits the data field entirely."""
        async with lifespan(mcp):
            result = await query(question="How many posts exist?", return_type="sql")

        assert result["success"] is True, result.get("error")
        assert result["generated_sql"]
        assert result.get("data") is None

    async def test_explicit_database_parameter(self) -> None:
        """The database parameter is accepted and routed."""
        async with lifespan(mcp):
            result = await query(
                question="SELECT 1 as one",
                database="blog_small",
                return_type="sql",
            )

        assert result["success"] is True, result.get("error")

    async def test_invalid_return_type(self) -> None:
        """An unsupported return_type yields invalid_parameter."""
        async with lifespan(mcp):
            result = await query(question="SELECT 1", return_type="cursor")

        assert result["success"] is False
        assert result["error"]["code"] == "invalid_parameter"

    async def test_empty_question_rejected(self) -> None:
        """An empty question yields invalid_request."""
        async with lifespan(mcp):
            result = await query(question="", return_type="result")

        assert result["success"] is False
        assert result["error"]["code"] == "invalid_request"

    async def test_unknown_database_error_envelope(self) -> None:
        """An unknown database yields database_error with the available list."""
        async with lifespan(mcp):
            result = await query(
                question="SELECT 1",
                database="missing_db",
                return_type="sql",
            )

        assert result["success"] is False
        assert result["error"]["code"] == "database_error"
        assert result["error"]["details"]["available_databases"] == ["blog_small"]
