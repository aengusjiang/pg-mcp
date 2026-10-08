"""Golden tests for the MCP tool response envelope.

These tests lock the exact shape of the dict returned by the ``query``
MCP tool (``QueryResponse.to_dict``): compact ``exclude_none`` form with
``tokens_used`` always present. Any intentional envelope change must be
reflected here deliberately.
"""

from pg_mcp.models.errors import ErrorDetail
from pg_mcp.models.query import QueryResponse, QueryResult


class TestSuccessEnvelope:
    """Golden shape of a successful response."""

    def test_success_envelope_keys(self) -> None:
        """Executed success envelope contains exactly the expected keys."""
        response = QueryResponse(
            success=True,
            generated_sql="SELECT COUNT(*) FROM users",
            data=QueryResult(columns=["count"], rows=[{"count": 10}], row_count=1),
            confidence=95,
            tokens_used=1234,
            request_id="req-abc",
        )
        envelope = response.to_dict()
        assert set(envelope) == {
            "success",
            "generated_sql",
            "data",
            "confidence",
            "tokens_used",
            "request_id",
        }
        assert envelope["tokens_used"] == 1234
        assert envelope["request_id"] == "req-abc"

    def test_sql_only_envelope_drops_unset_optionals(self) -> None:
        """Unset optional fields are omitted (compact exclude_none shape)."""
        response = QueryResponse(
            success=True,
            generated_sql="SELECT 1",
            confidence=90,
        )
        envelope = response.to_dict()
        assert set(envelope) == {"success", "generated_sql", "confidence", "tokens_used"}

    def test_data_nested_shape(self) -> None:
        """The data payload carries columns/rows/row_count/execution_time_ms."""
        response = QueryResponse(
            success=True,
            generated_sql="SELECT 1",
            data=QueryResult(columns=["one"], rows=[{"one": 1}], row_count=1),
        )
        envelope = response.to_dict()
        assert set(envelope["data"]) == {"columns", "rows", "row_count", "execution_time_ms"}


class TestFailureEnvelope:
    """Golden shape of an error response."""

    def test_failure_envelope_keys(self) -> None:
        """Failure envelope contains success/error/confidence plus tokens_used.

        confidence has a non-None default (100) so it is always present in
        the exclude_none envelope.
        """
        response = QueryResponse(
            success=False,
            error=ErrorDetail(code="security_violation", message="blocked table"),
        )
        envelope = response.to_dict()
        assert set(envelope) == {"success", "error", "confidence", "tokens_used"}
        assert envelope["tokens_used"] == 0
        assert envelope["confidence"] == 100

    def test_error_details_omitted_when_none(self) -> None:
        """error.details is omitted when unset."""
        response = QueryResponse(
            success=False,
            error=ErrorDetail(code="database_error", message="boom"),
        )
        envelope = response.to_dict()
        assert set(envelope["error"]) == {"code", "message"}

    def test_error_details_included_when_set(self) -> None:
        """error.details is carried into the envelope when provided."""
        response = QueryResponse(
            success=False,
            error=ErrorDetail(
                code="database_error",
                message="unknown database",
                details={"available": ["blog_small"]},
            ),
        )
        envelope = response.to_dict()
        assert envelope["error"]["details"] == {"available": ["blog_small"]}


class TestTokensUsedFloor:
    """tokens_used is always an int in the envelope."""

    def test_tokens_used_defaults_to_zero(self) -> None:
        """A response without token accounting reports tokens_used=0."""
        response = QueryResponse(
            success=True,
            generated_sql="SELECT 1",
        )
        envelope = response.to_dict()
        assert envelope["tokens_used"] == 0
        assert isinstance(envelope["tokens_used"], int)

    def test_tokens_used_preserved_when_set(self) -> None:
        """A real token count round-trips unchanged."""
        response = QueryResponse(success=True, generated_sql="SELECT 1", tokens_used=42)
        assert response.to_dict()["tokens_used"] == 42
