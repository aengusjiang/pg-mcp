"""FastMCP server for PostgreSQL natural language query interface.

This module implements the MCP server using FastMCP, exposing the query
functionality as an MCP tool. It includes complete lifespan management for
initializing and cleaning up all components, with per-database pools,
validators (merging per-database security overrides), and executors resolved
from the configured database registry.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from asyncpg import Pool
from mcp.server.fastmcp import FastMCP

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.databases import resolve_database_entries
from pg_mcp.config.settings import Settings
from pg_mcp.db.pool import close_pools, create_pool
from pg_mcp.models.errors import ErrorCode
from pg_mcp.models.query import QueryRequest, ReturnType
from pg_mcp.observability.logging import configure_logging, get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_logger(__name__)

# Global state for lifespan management. The circuit breaker lives inside the
# orchestrator (one per LLM dependency); only wiring-level state is here.
_settings: Settings | None = None
_pools: dict[str, Pool] | None = None
_schema_cache: SchemaCache | None = None
_orchestrator: QueryOrchestrator | None = None
_metrics: MetricsCollector | None = None
_rate_limiter: MultiRateLimiter | None = None


def _error_envelope(
    code: ErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact failure envelope matching QueryResponse.to_dict shape.

    Args:
        code: Error code from the ErrorCode enum.
        message: Human-readable error message.
        details: Optional error context (omitted when None).

    Returns:
        dict: Failure envelope with success/error/confidence/tokens_used keys.
    """
    error: dict[str, Any] = {"code": code.value, "message": message}
    if details is not None:
        error["details"] = details
    return {
        "success": False,
        "error": error,
        "confidence": 0,
        "tokens_used": 0,
    }


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
    """Lifespan context manager for server initialization and cleanup.

    This function manages the complete lifecycle of the MCP server:

    Startup:
        1. Load configuration from Settings
        2. Configure logging
        3. Resolve the database registry (databases.json or DATABASE_* fallback)
        4. Create one connection pool, validator, and executor per database
        5. Load schema cache for all databases
        6. Initialize metrics collector (and optional HTTP server)
        7. Create service components and resilience components
        8. Create query orchestrator wired for per-database routing

    Shutdown:
        1. Stop schema auto-refresh (if enabled)
        2. Close all database connection pools

    Yields:
        None
    """
    global _settings, _pools, _schema_cache, _orchestrator, _metrics, _rate_limiter

    logger.info("Starting PostgreSQL MCP Server initialization...")

    try:
        # 1. Load Settings
        logger.info("Loading configuration...")
        _settings = Settings()

        # 2. Configure logging
        logger.info("Configuring logging...")
        configure_logging(
            level=_settings.observability.log_level,
            log_format=_settings.observability.log_format,
            enable_sensitive_filter=True,
        )

        logger.info(
            "Configuration loaded",
            extra={
                "environment": _settings.environment,
                "log_level": _settings.observability.log_level,
            },
        )

        # 3. Resolve database registry (JSON file preferred, env fallback)
        registry = resolve_database_entries(_settings)
        logger.info(
            "Database registry resolved",
            extra={
                "databases": registry.names,
                "default_database": registry.default_database,
            },
        )

        # 4. Create per-database pools, validators, and executors
        logger.info("Creating database connection pools...")
        _pools = {}
        sql_validators: dict[str, SQLValidator] = {}
        sql_executors: dict[str, SQLExecutor] = {}

        for name, entry in registry.entries.items():
            db_config = entry.to_database_config()
            pool = await create_pool(db_config)
            _pools[name] = pool
            logger.info(
                f"Created connection pool for database '{name}'",
                extra={
                    "database": name,
                    "dsn": entry.safe_dsn,
                    "min_size": entry.pool.min_size,
                    "max_size": entry.pool.max_size,
                },
            )

            # Per-database security: unset overrides inherit the SECURITY_*
            # global defaults resolved into settings.security.
            overrides = entry.security
            sql_validators[name] = SQLValidator(
                config=_settings.security,
                blocked_tables=(
                    overrides.blocked_tables
                    if overrides.blocked_tables is not None
                    else _settings.security.blocked_tables
                ),
                blocked_columns=(
                    overrides.blocked_columns
                    if overrides.blocked_columns is not None
                    else _settings.security.blocked_columns
                ),
                allow_explain=(
                    overrides.allow_explain
                    if overrides.allow_explain is not None
                    else _settings.security.allow_explain
                ),
            )

            sql_executors[name] = SQLExecutor(
                pool=pool,
                security_config=_settings.security,
                db_config=db_config,
            )
            logger.info(f"Created validator and executor for database '{name}'")

        # 5. Load Schema cache for every configured database
        logger.info("Initializing schema cache...")
        _schema_cache = SchemaCache(_settings.cache)

        for db_name, pool in (_pools or {}).items():
            logger.info(f"Loading schema for database '{db_name}'...")
            schema = await _schema_cache.load(db_name, pool)
            logger.info(
                f"Schema loaded for '{db_name}'",
                extra={"database": db_name, "tables": len(schema.tables)},
            )

        # 6. Initialize metrics collector
        logger.info("Initializing metrics collector...")
        _metrics = MetricsCollector()

        if _settings.observability.metrics_enabled:
            _metrics.start_metrics_server(_settings.observability.metrics_port)
            logger.info(
                f"Metrics server started on port {_settings.observability.metrics_port}"
            )

        # 7. Create service components
        logger.info("Initializing service components...")

        sql_generator = SQLGenerator(_settings.openai)

        result_validator = ResultValidator(
            openai_config=_settings.openai,
            validation_config=_settings.validation,
        )

        # Resilience: concurrency limiters from configuration
        _rate_limiter = MultiRateLimiter(
            query_limit=_settings.resilience.query_concurrency,
            llm_limit=_settings.resilience.llm_concurrency,
        )

        # 8. Create QueryOrchestrator wired for per-database routing
        logger.info("Creating query orchestrator...")
        _orchestrator = QueryOrchestrator(
            sql_generator=sql_generator,
            sql_validators=sql_validators,
            sql_executors=sql_executors,
            result_validator=result_validator,
            schema_cache=_schema_cache,
            pools=_pools,
            resilience_config=_settings.resilience,
            validation_config=_settings.validation,
            default_database=registry.default_database,
            metrics=_metrics,
            rate_limiter=_rate_limiter,
        )

        logger.info("PostgreSQL MCP Server initialization complete!")
        logger.info(
            "Server ready to accept requests",
            extra={
                "databases": registry.names,
                "default_database": registry.default_database,
                "cache_enabled": _settings.cache.enabled,
                "metrics_enabled": _settings.observability.metrics_enabled,
            },
        )

        # Yield to run the server
        yield

    finally:
        # Shutdown sequence
        logger.info("Starting PostgreSQL MCP Server shutdown...")

        # Stop schema auto-refresh with timeout
        if _schema_cache is not None:
            try:
                await asyncio.wait_for(_schema_cache.stop_auto_refresh(), timeout=3.0)
                logger.info("Schema auto-refresh stopped")
            except TimeoutError:
                logger.warning("Schema auto-refresh stop timed out")
            except Exception as e:
                logger.warning(f"Error stopping schema auto-refresh: {e!s}")

        # Close database connection pools gracefully
        if _pools is not None:
            try:
                await close_pools(_pools)
                logger.info("Database connection pools closed")
            except Exception as e:
                logger.error(f"Error closing connection pools: {e!s}")

        logger.info("PostgreSQL MCP Server shutdown complete")


# Create FastMCP server instance with lifespan
mcp = FastMCP("pg-mcp", lifespan=lifespan)


@mcp.tool()
async def query(
    question: str,
    database: str | None = None,
    return_type: str = "result",
) -> dict[str, Any]:
    """Execute a natural language query against PostgreSQL database.

    This tool converts natural language questions into SQL queries and executes
    them against the specified PostgreSQL database. It includes comprehensive
    security validation, result verification, and error handling.

    Args:
        question: Natural language description of the query.
            Examples:
                - "How many users registered in the last 30 days?"
                - "Show me the top 10 products by revenue"
                - "What is the average order value by country?"

        database: Target database name (optional when a default database is
            configured or only one database is available).

        return_type: Type of result to return.
            Options:
                - "sql": Return only the generated SQL query without executing it
                - "result": Execute the query and return results (default)

    Returns:
        dict: Query response containing:
            - success (bool): Whether the query succeeded
            - generated_sql (str): The generated SQL query
            - data (dict): Query results if executed (columns, rows, row_count, etc.)
            - error (dict): Error information if query failed
            - confidence (int): Confidence score (0-100) for result quality
            - tokens_used (int): Number of LLM tokens consumed
            - request_id (str): Identifier for tracing this request in logs

    Security:
        - Only SELECT queries are allowed (no INSERT, UPDATE, DELETE, DROP, etc.)
        - Dangerous PostgreSQL functions are blocked (pg_sleep, file operations, etc.)
        - Per-database blocked tables/columns are enforced during validation
        - Query execution timeout is enforced
        - Row count limits prevent memory exhaustion
        - All queries run in read-only transactions
        - Concurrent queries are bounded by the configured concurrency limiter
    """
    if _orchestrator is None:
        return _error_envelope(
            ErrorCode.SERVER_NOT_INITIALIZED,
            "Server not initialized properly",
        )

    # Validate return_type
    if return_type not in ("sql", "result"):
        return _error_envelope(
            ErrorCode.INVALID_PARAMETER,
            f"Invalid return_type: '{return_type}'. Must be 'sql' or 'result'.",
            details={"return_type": return_type},
        )

    # Build request
    try:
        request = QueryRequest(
            question=question,
            database=database,
            return_type=ReturnType(return_type),
        )
    except Exception as e:
        return _error_envelope(
            ErrorCode.INVALID_REQUEST,
            f"Invalid request parameters: {e!s}",
            details={"error": str(e)},
        )

    # Execute query through orchestrator, bounded by the query concurrency
    # limiter. A timeout while waiting for a slot is reported as
    # rate_limit_exceeded rather than queuing indefinitely.
    limiter = _rate_limiter
    try:
        if limiter is not None:
            async with limiter.for_queries(timeout=_rate_limit_timeout()):
                response = await _orchestrator.execute_query(request)
        else:
            response = await _orchestrator.execute_query(request)
    except TimeoutError:
        logger.warning("Query rejected: concurrency limiter wait timed out")
        return _error_envelope(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            "Too many concurrent queries; timed out waiting for a slot",
            details={"timeout_seconds": _rate_limit_timeout()},
        )
    except Exception as e:
        logger.exception("Unexpected error in query tool")
        return _error_envelope(
            ErrorCode.INTERNAL_ERROR,
            f"Internal server error: {e!s}",
            details={"error_type": type(e).__name__},
        )

    return response.to_dict()


def _rate_limit_timeout() -> float:
    """Get the concurrency limiter wait timeout from settings.

    Returns:
        float: Timeout in seconds (0 means fail fast when no slot is free).
    """
    if _settings is None:
        return 0.0
    return _settings.resilience.rate_limit_timeout


if __name__ == "__main__":
    """Run the server when executed directly."""
    import anyio

    anyio.run(mcp.run_stdio_async)
