"""Unit tests for the MCP query tool wrapper in server.py.

These tests verify the tool-level envelope contract: error codes for
common failure paths, tokens_used always present, and enforcement of the
query concurrency limiter.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import pg_mcp.server as server_module
from pg_mcp.models.query import QueryRequest, QueryResponse, ReturnType
from pg_mcp.resilience.rate_limiter import MultiRateLimiter


def _mock_orchestrator() -> MagicMock:
    """Build an orchestrator mock whose queries always succeed."""
    orchestrator = MagicMock()
    response = QueryResponse(
        success=True,
        generated_sql="SELECT 1;",
        confidence=100,
        tokens_used=7,
        request_id="req-test-1",
    )
    orchestrator.execute_query = AsyncMock(return_value=response)
    return orchestrator


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MultiRateLimiter]:
    """Wire the server module with mock orchestrator and a 1-slot limiter.

    The settings stub uses plain namespaces so the sub-second limiter
    timeout isn't rejected by ResilienceConfig's >= 1.0 validation.
    """
    orchestrator = _mock_orchestrator()
    limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
    settings = SimpleNamespace(
        resilience=SimpleNamespace(rate_limit_timeout=0.2),
    )

    monkeypatch.setattr(server_module, "_orchestrator", orchestrator)
    monkeypatch.setattr(server_module, "_rate_limiter", limiter)
    monkeypatch.setattr(server_module, "_settings", settings)
    return orchestrator, limiter


class TestServerNotInitialized:
    @pytest.mark.asyncio
    async def test_returns_server_not_initialized_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before lifespan startup the tool reports server_not_initialized."""
        monkeypatch.setattr(server_module, "_orchestrator", None)

        result = await server_module.query(question="anything")

        assert result["success"] is False
        assert result["error"]["code"] == "server_not_initialized"
        assert result["tokens_used"] == 0


class TestParameterValidation:
    @pytest.mark.asyncio
    async def test_invalid_return_type(
        self, wired: tuple[MagicMock, MultiRateLimiter]
    ) -> None:
        """An unknown return_type is rejected with invalid_parameter."""
        orchestrator, _limiter = wired

        result = await server_module.query(question="q", return_type="csv")

        assert result["success"] is False
        assert result["error"]["code"] == "invalid_parameter"
        assert "return_type" in result["error"]["details"]
        orchestrator.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_question(
        self, wired: tuple[MagicMock, MultiRateLimiter]
    ) -> None:
        """An empty question fails QueryRequest validation with invalid_request."""
        orchestrator, _limiter = wired

        result = await server_module.query(question="")

        assert result["success"] is False
        assert result["error"]["code"] == "invalid_request"
        orchestrator.execute_query.assert_not_called()


class TestSuccessEnvelope:
    @pytest.mark.asyncio
    async def test_success_envelope_carries_tokens_used(
        self, wired: tuple[MagicMock, MultiRateLimiter]
    ) -> None:
        """The success envelope always includes tokens_used."""
        result = await server_module.query(question="How many users?")

        assert result["success"] is True
        assert result["generated_sql"] == "SELECT 1;"
        assert result["tokens_used"] == 7
        assert result["request_id"] == "req-test-1"

    @pytest.mark.asyncio
    async def test_request_forwarded_to_orchestrator(
        self, wired: tuple[MagicMock, MultiRateLimiter]
    ) -> None:
        """The tool builds a QueryRequest and forwards it unchanged."""
        orchestrator, _limiter = wired

        await server_module.query(
            question="q", database="blog_small", return_type="sql"
        )

        orchestrator.execute_query.assert_called_once()
        forwarded = orchestrator.execute_query.call_args[0][0]
        assert isinstance(forwarded, QueryRequest)
        assert forwarded.question == "q"
        assert forwarded.database == "blog_small"
        assert forwarded.return_type == ReturnType.SQL


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limit_exceeded_when_slots_exhausted(
        self, wired: tuple[MagicMock, MultiRateLimiter]
    ) -> None:
        """With all query slots held, the tool reports rate_limit_exceeded."""
        orchestrator, limiter = wired

        # Occupy the single query slot
        acquired = await limiter.query_limiter.acquire()
        assert acquired

        try:
            result = await server_module.query(question="q")

            assert result["success"] is False
            assert result["error"]["code"] == "rate_limit_exceeded"
            assert result["tokens_used"] == 0
            orchestrator.execute_query.assert_not_called()
        finally:
            limiter.query_limiter.release()

    @pytest.mark.asyncio
    async def test_slot_released_after_request(
        self, wired: tuple[MagicMock, MultiRateLimiter]
    ) -> None:
        """A completed request releases its slot for the next caller."""
        _orchestrator, limiter = wired

        await server_module.query(question="first")
        result = await server_module.query(question="second")

        assert result["success"] is True
        # release() decrements the active counter from a scheduled task;
        # yield once so it runs before asserting.
        await asyncio.sleep(0)
        assert limiter.query_limiter.active_count == 0
