"""Unit tests for QueryOrchestrator.

This module tests the orchestrator's coordination of the query pipeline:
per-database routing, retry logic with exponential backoff, metrics
instrumentation, request-scoped tracing, and error handling.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    LLMTimeoutError,
    LLMUnavailableError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    QueryRequest,
    ResultValidationResult,
    ReturnType,
)
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.resilience.circuit_breaker import CircuitState
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_generator import GenerationResult

# Fast retry configuration so backoff sleeps stay negligible in tests
# (retry_delay is validated >= 0.1, so keep the backoff factor flat).
FAST_RETRY = ResilienceConfig(max_retries=2, retry_delay=0.1, backoff_factor=1.0)


def _schema(database_name: str = "test_db") -> DatabaseSchema:
    """Build a minimal schema fixture."""
    return DatabaseSchema(
        database_name=database_name,
        tables=[
            TableInfo(
                schema_name="public",
                table_name="users",
                columns=[
                    ColumnInfo(
                        name="id",
                        data_type="integer",
                        is_nullable=False,
                        is_primary_key=True,
                    ),
                    ColumnInfo(
                        name="name",
                        data_type="varchar(255)",
                        is_nullable=False,
                    ),
                ],
            )
        ],
        version="15.0",
    )


def _null_context() -> Any:
    """Build an async context manager mock-compatible no-op."""

    @asynccontextmanager
    async def _ctx() -> AsyncIterator[None]:
        yield

    return _ctx()


def _rate_limiter_stub() -> MagicMock:
    """Build a MultiRateLimiter stub whose for_llm/for_queries are no-ops."""
    limiter = MagicMock(spec=MultiRateLimiter)
    limiter.for_llm = MagicMock(return_value=_null_context())
    limiter.for_queries = MagicMock(return_value=_null_context())
    return limiter


def _metrics_stub() -> MagicMock:
    """Build a MetricsCollector mock (avoids the prometheus singleton)."""
    return MagicMock(spec=MetricsCollector)


def _build_orchestrator(
    *,
    generator: Any = None,
    validators: dict[str, Any] | None = None,
    executors: dict[str, Any] | None = None,
    result_validator: Any = None,
    cache: Any = None,
    pools: dict[str, Any] | None = None,
    resilience: ResilienceConfig | None = None,
    validation: ValidationConfig | None = None,
    default_database: str | None = None,
    metrics: Any = None,
    rate_limiter: Any = None,
) -> QueryOrchestrator:
    """Build an orchestrator with mocked dependencies.

    Defaults every component to a MagicMock/AsyncMock wired to the single
    database "test_db" unless overridden.
    """
    generator = generator if generator is not None else AsyncMock()
    if isinstance(generator, AsyncMock) and not isinstance(
        generator.generate.return_value, GenerationResult
    ):
        generator.generate.return_value = GenerationResult("SELECT 1;", 10)

    validator = MagicMock()
    validator.validate_or_raise.return_value = None

    executors = executors if executors is not None else {"test_db": AsyncMock()}
    validators = validators if validators is not None else {"test_db": validator}

    return QueryOrchestrator(
        sql_generator=generator,
        sql_validators=validators,
        sql_executors=executors,
        result_validator=result_validator if result_validator is not None else MagicMock(),
        schema_cache=cache if cache is not None else MagicMock(),
        pools=pools if pools is not None else {"test_db": MagicMock()},
        resilience_config=resilience if resilience is not None else ResilienceConfig(),
        validation_config=validation if validation is not None else ValidationConfig(),
        default_database=default_database,
        metrics=metrics,
        rate_limiter=rate_limiter,
    )


class TestDatabaseResolution:
    """Test database name resolution logic."""

    @pytest.fixture
    def orchestrator(self) -> QueryOrchestrator:
        """Create orchestrator with two databases configured."""
        return _build_orchestrator(
            executors={"db1": AsyncMock(), "db2": AsyncMock()},
            validators={"db1": MagicMock(), "db2": MagicMock()},
            pools={"db1": MagicMock(), "db2": MagicMock()},
        )

    def test_resolve_database_specified_valid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified valid database."""
        result = orchestrator._resolve_database("db1")
        assert result == "db1"

    def test_resolve_database_specified_invalid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified but invalid database."""
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database("nonexistent")

        assert "not found" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]
        assert "db2" in exc_info.value.details["available_databases"]

    def test_resolve_database_uses_default_when_configured(self) -> None:
        """Test the default database is used when the request omits one."""
        orchestrator = _build_orchestrator(
            executors={"db1": AsyncMock(), "db2": AsyncMock()},
            validators={"db1": MagicMock(), "db2": MagicMock()},
            default_database="db2",
        )

        assert orchestrator._resolve_database(None) == "db2"

    def test_resolve_database_auto_select_single(self) -> None:
        """Test auto-selecting when only one database available."""
        orchestrator = _build_orchestrator(
            executors={"only_db": AsyncMock()},
            validators={"only_db": MagicMock()},
            pools={"only_db": MagicMock()},
        )

        result = orchestrator._resolve_database(None)
        assert result == "only_db"

    def test_resolve_database_auto_select_multiple_fails(
        self, orchestrator: QueryOrchestrator
    ) -> None:
        """Test that auto-select fails when multiple databases available."""
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "multiple databases" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]

    def test_resolve_database_no_databases(self) -> None:
        """Test error when no databases configured."""
        orchestrator = _build_orchestrator(executors={}, validators={})

        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "no databases configured" in str(exc_info.value).lower()


class TestPerDatabaseRouting:
    """Test that requests route to the validator/executor of their database."""

    @pytest.mark.asyncio
    async def test_executor_routes_to_requested_database(self) -> None:
        """Each request executes on the executor of its own database."""
        blog_executor = AsyncMock()
        crm_executor = AsyncMock()
        generator = AsyncMock()
        generator.generate.return_value = GenerationResult("SELECT 1;", 5)
        cache = MagicMock()
        cache.get.return_value = _schema()

        orchestrator = _build_orchestrator(
            generator=generator,
            executors={"blog": blog_executor, "crm": crm_executor},
            validators={"blog": MagicMock(), "crm": MagicMock()},
            cache=cache,
            default_database="blog",
        )

        await orchestrator.execute_query(
            QueryRequest(question="q", database="crm", return_type=ReturnType.RESULT)
        )

        crm_executor.execute.assert_called_once()
        blog_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_validator_routes_to_requested_database(self) -> None:
        """Validation uses the validator configured for the target database."""
        blog_validator = MagicMock()
        crm_validator = MagicMock()
        generator = AsyncMock()
        generator.generate.return_value = GenerationResult("SELECT 1;", 5)
        cache = MagicMock()
        cache.get.return_value = _schema()

        orchestrator = _build_orchestrator(
            generator=generator,
            executors={"blog": AsyncMock(), "crm": AsyncMock()},
            validators={"blog": blog_validator, "crm": crm_validator},
            cache=cache,
            default_database="blog",
        )

        await orchestrator.execute_query(
            QueryRequest(question="q", database="crm", return_type=ReturnType.SQL)
        )

        crm_validator.validate_or_raise.assert_called_once()
        blog_validator.validate_or_raise.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_database_returns_error_with_available_list(self) -> None:
        """A request for an unknown database lists the configured ones."""
        orchestrator = _build_orchestrator(
            executors={"blog": AsyncMock(), "crm": AsyncMock()},
            validators={"blog": MagicMock(), "crm": MagicMock()},
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="nope", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "database_error"
        details = response.error.details or {}
        assert set(details.get("available_databases", [])) == {"blog", "crm"}

    @pytest.mark.asyncio
    async def test_default_database_used_when_request_omits_database(self) -> None:
        """Requests without a database fall back to the configured default."""
        blog_validator = MagicMock()
        crm_validator = MagicMock()
        generator = AsyncMock()
        generator.generate.return_value = GenerationResult("SELECT 1;", 5)
        cache = MagicMock()
        cache.get.return_value = _schema()

        orchestrator = _build_orchestrator(
            generator=generator,
            executors={"blog": AsyncMock(), "crm": AsyncMock()},
            validators={"blog": blog_validator, "crm": crm_validator},
            cache=cache,
            default_database="crm",
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database=None, return_type=ReturnType.SQL)
        )

        assert response.success is True
        crm_validator.validate_or_raise.assert_called_once()
        blog_validator.validate_or_raise.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_executor_for_name_raises_database_error(self) -> None:
        """A configured database without an executor fails with a clear error."""
        generator = AsyncMock()
        generator.generate.return_value = GenerationResult("SELECT 1;", 5)
        cache = MagicMock()
        cache.get.return_value = _schema()

        orchestrator = _build_orchestrator(
            generator=generator,
            executors={"blog": AsyncMock()},
            validators={"blog": MagicMock(), "crm": MagicMock()},
            cache=cache,
            default_database="crm",
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database=None, return_type=ReturnType.RESULT)
        )

        assert response.success is False
        assert response.error is not None
        assert "has no SQL executor" in response.error.message


class TestSQLGenerationWithRetry:
    """Test SQL generation with retry logic."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return _schema()

    @pytest.mark.asyncio
    async def test_generate_sql_success_first_attempt(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test successful SQL generation on first attempt."""
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult(
            "SELECT * FROM users;", 120
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None  # No exception = valid

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            validators={"test_db": mock_validator},
            resilience=FAST_RETRY,
        )

        sql, validation_result, tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            request_id="test-123",
            database_name="test_db",
        )

        assert sql == "SELECT * FROM users;"
        assert validation_result.is_valid is True
        assert validation_result.is_select is True
        assert tokens == 120
        mock_generator.generate.assert_called_once()
        mock_validator.validate_or_raise.assert_called_once_with("SELECT * FROM users;")

    @pytest.mark.asyncio
    async def test_generate_sql_retry_on_validation_failure(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test retry logic when validation fails."""
        mock_generator = AsyncMock()
        mock_generator.generate.side_effect = [
            GenerationResult("SELECT * FROM user;", 100),  # wrong table name
            GenerationResult("SELECT * FROM users;", 80),  # correct
        ]

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = [
            SQLParseError('relation "user" does not exist'),
            None,  # Success on second attempt
        ]

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            validators={"test_db": mock_validator},
            resilience=FAST_RETRY,
        )

        sql, validation_result, tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            request_id="test-123",
            database_name="test_db",
        )

        assert sql == "SELECT * FROM users;"
        assert validation_result.is_valid is True
        # Token usage is summed across attempts
        assert tokens == 180
        assert mock_generator.generate.call_count == 2
        assert mock_validator.validate_or_raise.call_count == 2

        # Verify retry included error feedback
        second_call = mock_generator.generate.call_args_list[1]
        assert second_call.kwargs["previous_attempt"] == "SELECT * FROM user;"
        assert 'relation "user" does not exist' in second_call.kwargs["error_feedback"]

    @pytest.mark.asyncio
    async def test_generate_sql_fails_after_max_retries(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test failure after exhausting all retries (parse errors only)."""
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult("SELECT FROM WHERE;", 10)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SQLParseError(
            'syntax error at or near "WHERE"'
        )

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            validators={"test_db": mock_validator},
            resilience=ResilienceConfig(max_retries=2, retry_delay=0.1, backoff_factor=1.0),
        )

        with pytest.raises(SQLParseError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Broken query",
                schema=mock_schema,
                request_id="test-123",
                database_name="test_db",
            )

        assert 'syntax error at or near "WHERE"' in str(exc_info.value)
        # Should attempt max_retries + 1 times (initial + retries)
        assert mock_generator.generate.call_count == 3
        assert orchestrator.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_security_violation_fails_fast_without_retry(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Security policy rejections fail on first occurrence.

        Regression guard: feeding the policy text back to the LLM as retry
        feedback lets the model talk its way around the blocklist (e.g. an
        explanatory SELECT literal that passes validation), so a
        SecurityViolationError must abort immediately - no retry, and no
        charge against the LLM circuit breaker.
        """
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult("DELETE FROM users;", 10)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError(
            "DELETE statements are not allowed"
        )

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            validators={"test_db": mock_validator},
            resilience=ResilienceConfig(max_retries=2, retry_delay=0.1, backoff_factor=1.0),
        )

        with pytest.raises(SecurityViolationError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Delete all users",
                schema=mock_schema,
                request_id="test-123",
                database_name="test_db",
            )

        assert "DELETE statements are not allowed" in str(exc_info.value)
        # Single attempt: the policy rejection is final, not a retryable error
        assert mock_generator.generate.call_count == 1
        assert orchestrator.circuit_breaker.failure_count == 0

    @pytest.mark.asyncio
    async def test_transient_llm_errors_retried_with_backoff(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Transient LLM failures (timeout/unavailable) are retried with backoff."""
        mock_generator = AsyncMock()
        mock_generator.generate.side_effect = [
            LLMTimeoutError("timed out"),
            LLMUnavailableError("rate limited upstream"),
            GenerationResult("SELECT 1;", 42),
        ]

        mock_validator = MagicMock()

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            validators={"test_db": mock_validator},
            resilience=FAST_RETRY,
        )

        sql, _validation, tokens = await orchestrator._generate_sql_with_retry(
            question="q",
            schema=mock_schema,
            request_id="test-123",
            database_name="test_db",
        )

        assert sql == "SELECT 1;"
        assert tokens == 42
        assert mock_generator.generate.call_count == 3

    @pytest.mark.asyncio
    async def test_transient_llm_errors_exhaust_retries_raise(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Transient LLM failures exhausting the budget raise the last error."""
        mock_generator = AsyncMock()
        mock_generator.generate.side_effect = LLMTimeoutError("timed out")

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            resilience=FAST_RETRY,
        )

        with pytest.raises(LLMTimeoutError):
            await orchestrator._generate_sql_with_retry(
                question="q",
                schema=mock_schema,
                request_id="test-123",
                database_name="test_db",
            )

        # max_retries=2 -> 3 attempts total
        assert mock_generator.generate.call_count == 3
        assert orchestrator.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_non_transient_llm_error_propagates_immediately(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Non-transient errors are not retried."""
        mock_generator = AsyncMock()
        mock_generator.generate.side_effect = RuntimeError("Unexpected error")

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            resilience=FAST_RETRY,
        )

        with pytest.raises(RuntimeError, match="Unexpected error"):
            await orchestrator._generate_sql_with_retry(
                question="q",
                schema=mock_schema,
                request_id="test-123",
                database_name="test_db",
            )

        assert mock_generator.generate.call_count == 1

    @pytest.mark.asyncio
    async def test_generate_sql_circuit_breaker_open(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test that open circuit breaker prevents SQL generation."""
        orchestrator = _build_orchestrator(
            resilience=ResilienceConfig(circuit_breaker_threshold=1),
        )

        # Manually open the circuit breaker
        orchestrator.circuit_breaker._state = CircuitState.OPEN
        orchestrator.circuit_breaker._failure_count = 5

        from pg_mcp.models.errors import LLMError

        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                request_id="test-123",
                database_name="test_db",
            )

        assert "temporarily unavailable" in str(exc_info.value).lower()
        assert "circuit breaker" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_llm_calls_run_under_rate_limiter(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Each LLM call is guarded by the for_llm concurrency limiter."""
        rate_limiter = _rate_limiter_stub()
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult("SELECT 1;", 5)

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            resilience=ResilienceConfig(rate_limit_timeout=1.5),
            rate_limiter=rate_limiter,
        )

        await orchestrator._generate_sql_with_retry(
            question="q",
            schema=mock_schema,
            request_id="test-123",
            database_name="test_db",
        )

        rate_limiter.for_llm.assert_called_once_with(timeout=1.5)


class TestExecuteWithRetry:
    """Test SQL execution retry on transient database failures."""

    @pytest.mark.asyncio
    async def test_transient_sqlstate_retried_then_succeeds(self) -> None:
        """Transient connection failures are retried with backoff."""
        executor = AsyncMock()
        executor.execute.side_effect = [
            DatabaseError("connection failure", details={"error_code": "08006"}),
            ([{"id": 1}], 1),
        ]

        orchestrator = _build_orchestrator(executors={"test_db": executor}, resilience=FAST_RETRY)

        results, count = await orchestrator._execute_with_retry(executor, "SELECT 1", "req-1")

        assert results == [{"id": 1}]
        assert count == 1
        assert executor.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_transient_sqlstate_exhausted_raises(self) -> None:
        """Persistent transient failures raise the underlying DatabaseError."""
        executor = AsyncMock()
        executor.execute.side_effect = DatabaseError(
            "too many connections", details={"error_code": "53300"}
        )

        orchestrator = _build_orchestrator(executors={"test_db": executor}, resilience=FAST_RETRY)

        with pytest.raises(DatabaseError, match="too many connections"):
            await orchestrator._execute_with_retry(executor, "SELECT 1", "req-1")

        assert executor.execute.call_count == 3  # max_retries=2 -> 3 attempts

    @pytest.mark.asyncio
    async def test_non_transient_sqlstate_not_retried(self) -> None:
        """Permanent database errors fail fast without retry."""
        executor = AsyncMock()
        executor.execute.side_effect = DatabaseError(
            "syntax error", details={"error_code": "42601"}
        )

        orchestrator = _build_orchestrator(executors={"test_db": executor}, resilience=FAST_RETRY)

        with pytest.raises(DatabaseError, match="syntax error"):
            await orchestrator._execute_with_retry(executor, "SELECT bogus", "req-1")

        assert executor.execute.call_count == 1


class TestResultValidation:
    """Test result validation logic."""

    @pytest.mark.asyncio
    async def test_validate_results_success(self) -> None:
        """Test successful result validation."""
        mock_validator = AsyncMock()
        mock_validator.validate.return_value = ResultValidationResult(
            confidence=85,
            explanation="Results match the question well",
            suggestion=None,
            is_acceptable=True,
        )

        orchestrator = _build_orchestrator(
            result_validator=mock_validator,
            validation=ValidationConfig(enabled=True),
        )

        confidence = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 85
        mock_validator.validate.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_results_disabled(self) -> None:
        """Test that validation is skipped when disabled."""
        mock_validator = AsyncMock()

        orchestrator = _build_orchestrator(
            result_validator=mock_validator,
            validation=ValidationConfig(enabled=False),
        )

        confidence = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 100
        mock_validator.validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_results_failure_does_not_raise(self) -> None:
        """Test that validation failures don't raise exceptions."""
        mock_validator = AsyncMock()
        mock_validator.validate.side_effect = Exception("Validation failed")

        orchestrator = _build_orchestrator(
            result_validator=mock_validator,
            validation=ValidationConfig(enabled=True),
        )

        confidence = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 100


class TestMetricsWiring:
    """Test Prometheus metrics instrumentation of the request pipeline."""

    @pytest.mark.asyncio
    async def test_success_path_records_query_metrics(self) -> None:
        """Successful requests increment counters and observe durations."""
        metrics = _metrics_stub()
        generator = AsyncMock()
        generator.generate.return_value = GenerationResult("SELECT 1;", 30)
        cache = MagicMock()
        cache.get.return_value = _schema()

        orchestrator = _build_orchestrator(
            generator=generator,
            cache=cache,
            metrics=metrics,
            default_database="test_db",
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.SQL)
        )

        assert response.success is True
        metrics.increment_query_request.assert_called_once_with("success", "test_db")
        metrics.observe_query_duration.assert_called_once()
        metrics.increment_llm_call.assert_called_once_with("generate_sql")
        metrics.observe_llm_latency.assert_called_once()
        metrics.increment_llm_tokens.assert_called_once_with("generate_sql", 30)

    @pytest.mark.asyncio
    async def test_error_path_records_error_status(self) -> None:
        """Failed requests record the error status with the database label."""
        metrics = _metrics_stub()
        cache = MagicMock()
        cache.get.return_value = None
        cache.load = AsyncMock(side_effect=Exception("connection refused"))

        orchestrator = _build_orchestrator(
            cache=cache,
            metrics=metrics,
            default_database="test_db",
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.SQL)
        )

        assert response.success is False
        metrics.increment_query_request.assert_called_once_with("error", "test_db")

    @pytest.mark.asyncio
    async def test_validation_rejection_counts_sql_rejected(self) -> None:
        """Requests rejected by validation increment the rejection counter."""
        metrics = _metrics_stub()
        generator = AsyncMock()
        generator.generate.return_value = GenerationResult("DELETE FROM users;", 10)
        validator = MagicMock()
        validator.validate_or_raise.side_effect = SecurityViolationError("DELETE not allowed")
        cache = MagicMock()
        cache.get.return_value = _schema()

        orchestrator = _build_orchestrator(
            generator=generator,
            validators={"test_db": validator},
            cache=cache,
            resilience=ResilienceConfig(max_retries=1, retry_delay=0.1, backoff_factor=1.0),
            metrics=metrics,
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.SQL)
        )

        assert response.success is False
        metrics.increment_sql_rejected.assert_called_once_with("SecurityViolationError")

    @pytest.mark.asyncio
    async def test_execution_records_db_query_duration(self) -> None:
        """Database query duration is observed on execution."""
        metrics = _metrics_stub()
        generator = AsyncMock()
        generator.generate.return_value = GenerationResult("SELECT 1;", 5)
        executor = AsyncMock()
        executor.execute.return_value = ([{"id": 1}], 1)
        cache = MagicMock()
        cache.get.return_value = _schema()

        orchestrator = _build_orchestrator(
            generator=generator,
            executors={"test_db": executor},
            cache=cache,
            validation=ValidationConfig(enabled=False),
            metrics=metrics,
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.RESULT)
        )

        assert response.success is True
        metrics.observe_db_query_duration.assert_called_once()


class TestRequestContext:
    """Test request_id propagation into responses."""

    @pytest.mark.asyncio
    async def test_success_response_carries_request_id(self) -> None:
        """Every successful response includes a generated request_id."""
        generator = AsyncMock()
        generator.generate.return_value = GenerationResult("SELECT 1;", 5)
        cache = MagicMock()
        cache.get.return_value = _schema()

        orchestrator = _build_orchestrator(generator=generator, cache=cache)

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.SQL)
        )

        assert response.success is True
        assert response.request_id
        assert isinstance(response.request_id, str)

    @pytest.mark.asyncio
    async def test_error_response_carries_request_id(self) -> None:
        """Error responses also include the request_id for tracing."""
        orchestrator = _build_orchestrator(
            executors={"blog": AsyncMock()},
            validators={"blog": MagicMock()},
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="nope", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.request_id


class TestExecuteQueryFlow:
    """Test complete query execution flow."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return _schema()

    @pytest.mark.asyncio
    async def test_execute_query_sql_only(self, mock_schema: DatabaseSchema) -> None:
        """Test executing query with return_type=SQL."""
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult("SELECT * FROM users;", 50)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            validators={"test_db": mock_validator},
            cache=mock_cache,
        )

        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        assert response.generated_sql == "SELECT * FROM users;"
        assert response.validation is not None
        assert response.validation.is_valid is True
        assert response.data is None  # No execution for SQL-only
        assert response.error is None
        assert response.tokens_used == 50

    @pytest.mark.asyncio
    async def test_execute_query_with_results(self, mock_schema: DatabaseSchema) -> None:
        """Test executing query with return_type=RESULT."""
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult(
            "SELECT id, name FROM users;", 60
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.execute.return_value = (
            [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
            ],
            2,  # total count
        )

        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=90,
            explanation="Good results",
            suggestion=None,
            is_acceptable=True,
        )

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            validators={"test_db": mock_validator},
            executors={"test_db": mock_executor},
            result_validator=mock_result_validator,
            cache=mock_cache,
            validation=ValidationConfig(enabled=True),
        )

        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        assert response.generated_sql == "SELECT id, name FROM users;"
        assert response.data is not None
        assert response.data.row_count == 2
        assert len(response.data.rows) == 2
        assert response.data.columns == ["id", "name"]
        assert response.confidence == 90
        assert response.error is None
        assert response.tokens_used == 60

    @pytest.mark.asyncio
    async def test_execute_query_schema_not_cached(self) -> None:
        """Test loading schema when not in cache."""
        mock_schema = DatabaseSchema(
            database_name="test_db",
            tables=[],
            version="15.0",
        )

        mock_cache = MagicMock()
        mock_cache.get.return_value = None  # Not in cache
        mock_cache.load = AsyncMock(return_value=mock_schema)

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult("SELECT 1;", 5)

        mock_pool = MagicMock()

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            cache=mock_cache,
            pools={"test_db": mock_pool},
        )

        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        mock_cache.load.assert_called_once_with("test_db", mock_pool)
        assert response.success is True

    @pytest.mark.asyncio
    async def test_execute_query_schema_load_fails(self) -> None:
        """Test handling of schema load failure."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache.load = AsyncMock(side_effect=Exception("DB connection failed"))

        orchestrator = _build_orchestrator(cache=mock_cache)

        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert "schema" in response.error.message.lower()
        assert response.generated_sql is None

    @pytest.mark.asyncio
    async def test_execute_query_validation_error(self) -> None:
        """Test handling of SQL validation errors."""
        mock_schema = DatabaseSchema(
            database_name="test_db",
            tables=[],
            version="15.0",
        )

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult("DELETE FROM users;", 15)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError(
            "DELETE not allowed"
        )

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            validators={"test_db": mock_validator},
            cache=mock_cache,
            resilience=ResilienceConfig(max_retries=1, retry_delay=0.1, backoff_factor=1.0),
        )

        request = QueryRequest(
            question="Delete all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert "DELETE not allowed" in response.error.message
        assert response.error.code == "security_violation"
        # Tokens consumed by the single (policy-rejected) attempt are reported;
        # security violations fail fast and are not retried
        assert response.tokens_used == 15

    @pytest.mark.asyncio
    async def test_execute_query_execution_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of SQL execution errors."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult("SELECT * FROM users;", 5)

        mock_executor = AsyncMock()
        mock_executor.execute.side_effect = DatabaseError("Query execution failed")

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            executors={"test_db": mock_executor},
            cache=mock_cache,
        )

        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert "execution failed" in response.error.message.lower()
        assert response.error.code == "database_error"

    @pytest.mark.asyncio
    async def test_execute_query_unexpected_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of unexpected errors."""
        mock_cache = MagicMock()
        mock_cache.get.side_effect = RuntimeError("Unexpected error")

        orchestrator = _build_orchestrator(cache=mock_cache)

        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "internal_error"
        assert "internal server error" in response.error.message.lower()

    @pytest.mark.asyncio
    async def test_execute_query_auto_select_database(self, mock_schema: DatabaseSchema) -> None:
        """Test auto-selecting database when only one available."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = GenerationResult("SELECT 1;", 5)

        orchestrator = _build_orchestrator(
            generator=mock_generator,
            executors={"only_db": AsyncMock()},
            validators={"only_db": MagicMock()},
            cache=mock_cache,
            pools={"only_db": MagicMock()},  # Only one database
        )

        request = QueryRequest(
            question="Test query",
            database=None,  # No database specified
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        mock_cache.get.assert_called_once_with("only_db")
