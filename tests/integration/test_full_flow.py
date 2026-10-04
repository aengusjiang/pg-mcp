"""Integration tests for the full query flow with real services.

These exercise the complete pipeline through the MCP server tool — real
settings, a real connection pool, real schema introspection, real LLM SQL
generation, real validation, and real execution — against the blog_small
fixture database. They require both the fixture databases and a valid
OPENAI_API_KEY, and skip cleanly when either is missing.
"""

import pytest

from pg_mcp.server import lifespan, mcp, query

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _environment(blog_environment: None) -> None:
    """Both guards (databases reachable, LLM key present) are required."""


class TestFullQueryFlow:
    """Complete question-to-result flow through the server tool."""

    async def test_natural_language_to_executed_result(self) -> None:
        """A natural-language question returns executed rows."""
        async with lifespan(mcp):
            result = await query(
                question="How many posts are in the database?",
                return_type="result",
            )

        assert result["success"] is True, result.get("error")
        data = result["data"]
        assert data is not None
        assert data["row_count"] >= 1
        assert isinstance(data["rows"], list)
        assert "SELECT" in result["generated_sql"].upper()

    async def test_sql_only_mode_returns_sql_without_data(self) -> None:
        """return_type='sql' returns the statement without executing it."""
        async with lifespan(mcp):
            result = await query(
                question="How many users are registered?",
                return_type="sql",
            )

        assert result["success"] is True, result.get("error")
        assert result["generated_sql"]
        assert result.get("data") is None

    async def test_response_carries_request_id_and_tokens(self) -> None:
        """Tracing and token accounting survive the full round trip."""
        async with lifespan(mcp):
            result = await query(
                question="How many comments exist?",
                return_type="result",
            )

        assert result["success"] is True, result.get("error")
        assert isinstance(result["request_id"], str) and result["request_id"]
        assert isinstance(result["tokens_used"], int) and result["tokens_used"] >= 0


class TestFullFlowErrorPaths:
    """Deterministic error envelopes through the real server tool."""

    async def test_unknown_database_reports_available_databases(self) -> None:
        """An unknown database fails with the available list in details."""
        async with lifespan(mcp):
            result = await query(
                question="How many posts are there?",
                database="nonexistent_database_12345",
                return_type="result",
            )

        assert result["success"] is False
        assert result["error"]["code"] == "database_error"
        # Single-database fallback mode: blog_small is the only entry
        assert result["error"]["details"]["available_databases"] == ["blog_small"]

    async def test_empty_question_is_rejected(self) -> None:
        """An empty question fails request validation."""
        async with lifespan(mcp):
            result = await query(question="", return_type="result")

        assert result["success"] is False
        assert result["error"]["code"] == "invalid_request"

    async def test_invalid_return_type_is_rejected(self) -> None:
        """An unsupported return_type fails parameter validation."""
        async with lifespan(mcp):
            result = await query(question="How many posts?", return_type="cursor")

        assert result["success"] is False
        assert result["error"]["code"] == "invalid_parameter"
