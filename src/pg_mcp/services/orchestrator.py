"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation. It implements per-database routing, retry logic with exponential
backoff, metrics instrumentation, and request-scoped tracing.
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    LLMTimeoutError,
    LLMUnavailableError,
    PgMcpError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.logging import get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import generate_request_id, request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

if TYPE_CHECKING:
    from pg_mcp.models.schema import DatabaseSchema

logger = get_logger(__name__)

# PostgreSQL sqlstate codes treated as transient: connection exceptions,
# too many connections, serialization failures, deadlocks, and lock/admin
# events. Everything else fails fast (the same query would fail again).
TRANSIENT_SQLSTATES: frozenset[str] = frozenset(
    {
        "08000",  # connection_exception
        "08001",  # sqlclient_unable_to_establish_sqlconnection
        "08003",  # connection_does_not_exist
        "08004",  # sqlserver_rejected_establishment_of_sqlconnection
        "08006",  # connection_failure
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "53300",  # too_many_connections
        "55P03",  # lock_not_available
        "57P01",  # admin_shutdown
    }
)

# Upper bound for a single backoff sleep, keeping pathological configs
# (e.g. retry_delay=10 * factor=10 ** attempt) from stalling a request forever.
MAX_BACKOFF_SECONDS = 30.0


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation with per-database routing: validators and executors are
    injected per logical database name, and every request resolves to the
    matching pair. It implements retry logic with exponential backoff for
    transient LLM and database failures, a circuit breaker for LLM calls,
    Prometheus metrics instrumentation, and request-scoped tracing.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validators={"blog": blog_validator, "crm": crm_validator},
        ...     sql_executors={"blog": blog_executor, "crm": crm_executor},
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"blog": pool1, "crm": pool2},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ...     default_database="blog",
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="crm"
        ... ))
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validators: dict[str, SQLValidator],
        sql_executors: dict[str, SQLExecutor],
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        default_database: str | None = None,
        metrics: MetricsCollector | None = None,
        rate_limiter: MultiRateLimiter | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validators: Validators keyed by logical database name.
            sql_executors: Executors keyed by logical database name.
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Dictionary mapping database names to connection pools.
            resilience_config: Resilience configuration for retries and circuit breaker.
            validation_config: Validation configuration including thresholds.
            default_database: Database used when a request omits the
                database parameter (None requires explicit selection unless
                exactly one database is configured).
            metrics: Optional metrics collector for instrumentation.
            rate_limiter: Optional rate limiter guarding LLM concurrency.
        """
        self.sql_generator = sql_generator
        self.sql_validators = sql_validators
        self.sql_executors = sql_executors
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.default_database = default_database
        self._metrics = metrics
        self._rate_limiter = rate_limiter

        # Circuit breaker for LLM calls
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow from question to results.

        This method orchestrates the entire pipeline under a request
        context (so the request_id propagates to logs and the response):
        1. Resolve and validate database name
        2. Load schema from cache
        3. Generate and validate SQL with retry logic
        4. Execute SQL (if return_type == RESULT)
        5. Validate results (optional)
        6. Return structured response

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.
        """
        async with request_context(generate_request_id()) as request_id:
            started = time.perf_counter()
            response = await self._process_request(request, request_id)
            if self._metrics is not None:
                self._metrics.observe_query_duration(time.perf_counter() - started)
                self._metrics.increment_query_request(
                    "success" if response.success else "error",
                    request.database or self.default_database or "unknown",
                )
            return response

    async def _process_request(self, request: QueryRequest, request_id: str) -> QueryResponse:
        """Run the query pipeline; never raises, always returns a response."""
        # Single-element sink so tokens consumed by failed generation
        # attempts survive the exception path into the error response.
        tokens_sink: list[int] = [0]

        try:
            # Step 1: Resolve database name
            database_name = self._resolve_database(request.database)
            logger.debug(
                "Resolved database",
                extra={"request_id": request_id, "database": database_name},
            )

            # Step 2: Get schema from cache
            schema = self.schema_cache.get(database_name)
            if schema is None:
                # Schema not in cache, load it
                pool = self.pools.get(database_name)
                if pool is None:
                    raise DatabaseError(
                        message=f"No connection pool available for database '{database_name}'",
                        details={"database": database_name},
                    )
                try:
                    schema = await self.schema_cache.load(database_name, pool)
                except Exception as e:
                    raise SchemaLoadError(
                        message=f"Failed to load schema for database '{database_name}': {e!s}",
                        details={"database": database_name, "error": str(e)},
                    ) from e

            logger.debug(
                "Schema loaded",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "tables": len(schema.tables),
                },
            )

            # Step 3: Generate and validate SQL with retry logic
            generated_sql, validation_result, tokens_used = await self._generate_sql_with_retry(
                question=request.question,
                schema=schema,
                request_id=request_id,
                database_name=database_name,
                tokens_sink=tokens_sink,
            )

            # Step 4: If return_type is SQL, return early
            if request.return_type == ReturnType.SQL:
                logger.info(
                    "Returning SQL only",
                    extra={"request_id": request_id, "sql_length": len(generated_sql)},
                )
                return QueryResponse(
                    success=True,
                    generated_sql=generated_sql,
                    validation=validation_result,
                    data=None,
                    error=None,
                    confidence=100,
                    tokens_used=tokens_used,
                    request_id=request_id,
                )

            # Step 5: Execute SQL against the resolved database
            executor = self._get_executor(database_name)
            logger.debug(
                "Executing SQL",
                extra={"request_id": request_id, "database": database_name},
            )
            start_time = self._get_current_time_ms()

            results, total_count = await self._execute_with_retry(
                executor, generated_sql, request_id
            )

            execution_time_ms = self._get_current_time_ms() - start_time
            logger.info(
                "SQL executed successfully",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "row_count": total_count,
                    "execution_time_ms": execution_time_ms,
                },
            )

            # Step 6: Validate results (non-blocking, failures don't fail the request)
            result_confidence = await self._validate_results_safely(
                question=request.question,
                sql=generated_sql,
                results=results,
                row_count=total_count,
                request_id=request_id,
            )

            # Step 7: Build successful response
            query_result = QueryResult(
                columns=list(results[0].keys()) if results else [],
                rows=results,
                row_count=len(results),  # Limited row count (after max_rows applied)
                execution_time_ms=execution_time_ms,
            )

            return QueryResponse(
                success=True,
                generated_sql=generated_sql,
                validation=validation_result,
                data=query_result,
                error=None,
                confidence=result_confidence,
                tokens_used=tokens_used,
                request_id=request_id,
            )

        except PgMcpError as e:
            # Handle known application errors
            logger.warning(
                "Query execution failed with known error",
                extra={
                    "request_id": request_id,
                    "error_code": e.code,
                    "error_message": str(e),
                },
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=e.code.value,
                    message=e.message,
                    details=e.details,
                ),
                confidence=0,
                tokens_used=tokens_sink[0],
                request_id=request_id,
            )
        except Exception as e:
            # Handle unexpected errors
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    message=f"Internal server error: {e!s}",
                    details={"error_type": type(e).__name__},
                ),
                confidence=0,
                tokens_used=tokens_sink[0],
                request_id=request_id,
            )

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request, default, or auto-select.

        If database is specified, validate it exists. If omitted, use the
        configured default database, or auto-select when exactly one
        database is configured.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.sql_executors:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": sorted(self.sql_executors),
                    },
                )
            return database

        if self.default_database is not None:
            if self.default_database not in self.sql_executors:
                raise DatabaseError(
                    message=(
                        f"Configured default database '{self.default_database}' "
                        "has no SQL executor"
                    ),
                    details={
                        "default_database": self.default_database,
                        "available_databases": sorted(self.sql_executors),
                    },
                )
            return self.default_database

        # Auto-select if only one database available
        available_dbs = sorted(self.sql_executors)
        if len(available_dbs) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available_dbs) == 1:
            return available_dbs[0]

        # Multiple databases, must specify
        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    def _get_validator(self, database_name: str) -> SQLValidator:
        """Get the SQL validator for the resolved database.

        Args:
            database_name: Logical database name.

        Returns:
            SQLValidator: The validator configured for this database.

        Raises:
            DatabaseError: If no validator is configured for the name.
        """
        try:
            return self.sql_validators[database_name]
        except KeyError:
            raise DatabaseError(
                message=f"No SQL validator configured for database '{database_name}'",
                details={
                    "database": database_name,
                    "available_databases": sorted(self.sql_validators),
                },
            ) from None

    def _get_executor(self, database_name: str) -> SQLExecutor:
        """Get the SQL executor for the resolved database.

        Args:
            database_name: Logical database name.

        Returns:
            SQLExecutor: The executor bound to this database's pool.

        Raises:
            DatabaseError: If no executor is configured for the name.
        """
        try:
            return self.sql_executors[database_name]
        except KeyError:
            raise DatabaseError(
                message=f"No SQL executor configured for database '{database_name}'",
                details={
                    "database": database_name,
                    "available_databases": sorted(self.sql_executors),
                },
            ) from None

    def _backoff_delay(self, attempt: int) -> float:
        """Compute the exponential backoff delay for a retry attempt.

        Args:
            attempt: Zero-based attempt number that just failed.

        Returns:
            float: Delay in seconds (capped at MAX_BACKOFF_SECONDS).
        """
        delay = self.resilience_config.retry_delay * (
            self.resilience_config.backoff_factor**attempt
        )
        return min(delay, MAX_BACKOFF_SECONDS)

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: "DatabaseSchema",
        request_id: str,
        database_name: str,
        tokens_sink: list[int] | None = None,
    ) -> tuple[str, ValidationResult, int]:
        """Generate and validate SQL with retry logic.

        A single attempt budget (``max_retries``) is shared between
        transient LLM failures (timeout / unavailable, retried with
        exponential backoff) and validation failures (retried with error
        feedback). Every LLM call runs under the ``for_llm`` concurrency
        limiter when one is injected, and records latency/token metrics.

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            request_id: Request ID for tracking.
            database_name: Database whose validator should check the SQL.
            tokens_sink: Optional single-element list that mirrors the
                tokens consumed so far, so callers building error responses
                after an exception still observe the usage.

        Returns:
            tuple: (generated_sql, validation_result, tokens_used)

        Raises:
            LLMError: If circuit breaker is open or generation fails.
            SecurityViolationError: If SQL violates the security policy
                (raised on first occurrence, without retry).
            SQLParseError: If SQL cannot be parsed after all retries.
        """
        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        validator = self._get_validator(database_name)
        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        tokens_used = 0

        for attempt in range(max_retries + 1):
            # --- LLM call with transient-failure retry ---
            try:
                generation = await self._call_llm(
                    question=question,
                    schema=schema,
                    previous_attempt=previous_sql,
                    error_feedback=error_feedback,
                    request_id=request_id,
                    attempt=attempt,
                )
            except (LLMTimeoutError, LLMUnavailableError) as e:
                if attempt < max_retries:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "Transient LLM failure, retrying with backoff",
                        extra={
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "delay_seconds": delay,
                            "error": str(e),
                        },
                    )
                    await asyncio.sleep(delay)
                    continue
                self.circuit_breaker.record_failure()
                raise

            tokens_used += generation.tokens_used or 0
            if tokens_sink is not None:
                tokens_sink[0] = tokens_used

            # --- Validation ---
            try:
                validator.validate_or_raise(generation.sql)
            except SecurityViolationError as violation:
                # Policy rejections are final. Retrying with feedback would
                # hand the policy text back to the model and invite it to
                # talk its way around the blocklist (e.g. an explanatory
                # SELECT literal that passes validation), so fail fast:
                # no retry, and no charge against the LLM circuit breaker.
                if self._metrics is not None:
                    self._metrics.increment_sql_rejected("SecurityViolationError")
                logger.warning(
                    "SQL rejected by security policy (no retry)",
                    extra={
                        "request_id": request_id,
                        "database": database_name,
                        "error": str(violation),
                    },
                )
                raise
            except SQLParseError as validation_error:
                if attempt < max_retries:
                    # Retry with feedback
                    logger.warning(
                        "SQL validation failed, retrying with feedback",
                        extra={
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "error": str(validation_error),
                        },
                    )
                    previous_sql = generation.sql
                    error_feedback = str(validation_error)
                    continue
                # Out of retries
                self.circuit_breaker.record_failure()
                if self._metrics is not None:
                    self._metrics.increment_sql_rejected(type(validation_error).__name__)
                logger.error(
                    "SQL validation failed after all retries",
                    extra={
                        "request_id": request_id,
                        "attempts": attempt + 1,
                        "error": str(validation_error),
                    },
                )
                raise

            # Validation successful
            self.circuit_breaker.record_success()
            logger.info(
                "SQL generated and validated successfully",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "attempts": attempt + 1,
                },
            )

            # Build validation result
            validation_result = ValidationResult(
                is_valid=True,
                is_select=True,
                allows_data_modification=False,
                uses_blocked_functions=[],
                error_message=None,
            )

            return generation.sql, validation_result, tokens_used

        # Should not reach here, but just in case
        self.circuit_breaker.record_failure()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _call_llm(
        self,
        question: str,
        schema: "DatabaseSchema",
        previous_attempt: str | None,
        error_feedback: str | None,
        request_id: str,
        attempt: int,
    ) -> Any:
        """Perform one LLM generation call under the concurrency limiter.

        Records LLM call/latency/token metrics when a collector is injected.

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            previous_attempt: Previously failed SQL (for feedback).
            error_feedback: Error message from the previous attempt.
            request_id: Request ID for tracking.
            attempt: Zero-based attempt number (for logging).

        Returns:
            GenerationResult from the generator.

        Raises:
            LLMTimeoutError: On transient API timeout.
            LLMUnavailableError: On transient API unavailability.
            LLMError: On non-transient generation failures.
        """
        logger.debug(
            "Generating SQL",
            extra={"request_id": request_id, "attempt": attempt + 1},
        )
        started = time.perf_counter()
        try:
            if self._rate_limiter is not None:
                async with self._rate_limiter.for_llm(
                    timeout=self.resilience_config.rate_limit_timeout
                ):
                    generation = await self.sql_generator.generate(
                        question=question,
                        schema=schema,
                        previous_attempt=previous_attempt,
                        error_feedback=error_feedback,
                    )
            else:
                generation = await self.sql_generator.generate(
                    question=question,
                    schema=schema,
                    previous_attempt=previous_attempt,
                    error_feedback=error_feedback,
                )
        finally:
            if self._metrics is not None:
                self._metrics.increment_llm_call("generate_sql")
                self._metrics.observe_llm_latency(
                    "generate_sql", time.perf_counter() - started
                )
        if self._metrics is not None and generation.tokens_used:
            self._metrics.increment_llm_tokens("generate_sql", generation.tokens_used)
        return generation

    async def _execute_with_retry(
        self,
        executor: SQLExecutor,
        sql: str,
        request_id: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Execute SQL with retry on transient database failures.

        Args:
            executor: Executor bound to the resolved database.
            sql: Validated SQL statement.
            request_id: Request ID for tracking.

        Returns:
            tuple: (results, total_row_count) from the executor.

        Raises:
            DatabaseError: On non-transient failure or after exhausting retries.
            ExecutionTimeoutError: When execution exceeds the timeout.
        """
        max_retries = self.resilience_config.max_retries

        for attempt in range(max_retries + 1):
            try:
                started = time.perf_counter()
                results, total_count = await executor.execute(sql)
                if self._metrics is not None:
                    self._metrics.observe_db_query_duration(time.perf_counter() - started)
                return results, total_count
            except DatabaseError as e:
                sqlstate = str(e.details.get("error_code") or "")
                if sqlstate in TRANSIENT_SQLSTATES and attempt < max_retries:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "Transient database error, retrying with backoff",
                        extra={
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "sqlstate": sqlstate,
                            "delay_seconds": delay,
                        },
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

        raise DatabaseError(
            message="Query execution failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        """Validate query results with error handling (non-blocking).

        This method attempts to validate results using LLM, but failures
        don't cause the overall query to fail. Returns a confidence score.

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            int: Confidence score (0-100). Returns 100 if validation disabled/fails.
        """
        if not self.validation_config.enabled:
            return 100

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            validation_result = await self.result_validator.validate(
                question=question,
                sql=sql,
                results=results,
                row_count=row_count,
            )

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                },
            )

            return validation_result.confidence

        except Exception as e:
            # Log but don't fail the query
            logger.warning(
                "Result validation failed, continuing with default confidence",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                },
            )
            return 100  # Default to high confidence if validation fails

    @staticmethod
    def _get_current_time_ms() -> float:
        """Get current time in milliseconds.

        Returns:
            float: Current time in milliseconds since epoch.
        """
        return time.time() * 1000
