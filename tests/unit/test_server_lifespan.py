"""Unit tests for the server lifespan wiring in server.py.

These tests verify the startup assembly: the database registry loop
builds one pool, validator, and executor per configured database,
per-database security overrides merge with the global defaults, and the
orchestrator receives dict-based routing tables plus the configured
default database, metrics collector, and rate limiter.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

import pg_mcp.server as server_module
from pg_mcp.config.databases import (
    DatabaseEntry,
    DatabaseRegistry,
    DatabaseSecurityOverrides,
)
from pg_mcp.config.settings import (
    ResilienceConfig,
    SecurityConfig,
    ValidationConfig,
)


def _mock_settings() -> MagicMock:
    """Build a Settings mock with real config sections where it matters."""
    settings = MagicMock()
    settings.environment = "development"
    settings.security = SecurityConfig(blocked_tables=["global_blocked"])
    settings.resilience = ResilienceConfig()
    settings.validation = ValidationConfig(enabled=False)
    settings.observability.log_level = "INFO"
    settings.observability.log_format = "json"
    settings.observability.metrics_enabled = False
    settings.observability.metrics_port = 9090
    return settings


@pytest.fixture
def registry() -> DatabaseRegistry:
    """Two-database registry: one with security overrides, one without."""
    blog = DatabaseEntry(
        name="blog",
        database="blog_db",
        password=SecretStr("pw"),
    )
    crm = DatabaseEntry(
        name="crm",
        database="crm_db",
        password=SecretStr("pw"),
        security=DatabaseSecurityOverrides(
            blocked_tables=["payment_methods"],
            allow_explain=True,
        ),
    )
    return DatabaseRegistry(entries={"blog": blog, "crm": crm}, default_database="blog")


@pytest.fixture
def wired_lifespan(registry: DatabaseRegistry):
    """Patch all external collaborators of the lifespan function.

    Returns (run_lifespan, captures) where captures collects the
    constructor kwargs the orchestrator was built with.
    """
    from pg_mcp.cache.schema_cache import SchemaCache

    mock_schema = MagicMock()
    mock_schema.tables = []

    schema_cache_instance = MagicMock(spec=SchemaCache)
    schema_cache_instance.load = AsyncMock(return_value=mock_schema)

    orchestrator_captures: dict[str, object] = {}
    orchestrator_instance = MagicMock()

    def _orchestrator_ctor(**kwargs: object) -> MagicMock:
        orchestrator_captures.update(kwargs)
        return orchestrator_instance

    pool_counter = iter(f"pool-{i}" for i in range(10))

    @asynccontextmanager
    async def _run() -> AsyncIterator[None]:
        with (
            patch.object(server_module, "Settings", return_value=_mock_settings()),
            patch.object(server_module, "configure_logging"),
            patch.object(server_module, "resolve_database_entries", return_value=registry),
            patch.object(
                server_module,
                "create_pool",
                new=AsyncMock(side_effect=lambda _cfg: next(pool_counter)),
            ),
            patch.object(
                server_module, "SchemaCache", return_value=schema_cache_instance
            ),
            patch.object(server_module, "SQLGenerator"),
            patch.object(server_module, "SQLValidator") as mock_validator_cls,
            patch.object(server_module, "SQLExecutor"),
            patch.object(server_module, "ResultValidator"),
            patch.object(server_module, "MetricsCollector") as mock_metrics_cls,
            patch.object(
                server_module, "QueryOrchestrator", side_effect=_orchestrator_ctor
            ),
            patch.object(
                server_module, "close_pools", new=AsyncMock()
            ) as mock_close_pools,
        ):
            async with server_module.lifespan(MagicMock()):
                yield {
                    "validator_cls": mock_validator_cls,
                    "metrics_cls": mock_metrics_cls,
                    "close_pools": mock_close_pools,
                    "orchestrator_captures": orchestrator_captures,
                    "schema_cache": schema_cache_instance,
                }

    return _run


@pytest.fixture(autouse=True)
def _reset_server_globals() -> AsyncIterator[None]:
    """Clean the server module globals after each lifespan test."""
    yield
    server_module._pools = None
    server_module._schema_cache = None
    server_module._orchestrator = None
    server_module._metrics = None
    server_module._rate_limiter = None
    server_module._settings = None


class TestLifespanAssembly:
    @pytest.mark.asyncio
    async def test_one_pool_validator_executor_per_database(
        self, wired_lifespan, registry: DatabaseRegistry
    ) -> None:
        """The registry loop builds a pool/validator/executor per entry."""
        async with wired_lifespan() as harness:
            # Two pools (one per database)
            assert server_module.create_pool.call_count == 2

            # Two validators and two executors (one per database)
            assert harness["validator_cls"].call_count == 2

            # Schema loaded once per database
            assert harness["schema_cache"].load.call_count == 2
            loaded_names = [call.args[0] for call in harness["schema_cache"].load.call_args_list]
            assert set(loaded_names) == set(registry.names)

    @pytest.mark.asyncio
    async def test_security_overrides_merge_with_global_defaults(
        self, wired_lifespan
    ) -> None:
        """Per-database overrides replace only the unset global defaults."""
        async with wired_lifespan() as harness:
            validator_calls = {
                # Reconstruct the (name -> kwargs) mapping by call order
                i: call.kwargs
                for i, call in enumerate(harness["validator_cls"].call_args_list)
            }
            assert len(validator_calls) == 2

            all_kwargs = list(validator_calls.values())
            # blog: no overrides -> global blocked tables, explain off
            blog_kwargs = next(
                kw for kw in all_kwargs if kw.get("blocked_tables") == ["global_blocked"]
            )
            assert blog_kwargs["allow_explain"] is False

            # crm: overrides -> per-database blocked tables and explain on
            crm_kwargs = next(
                kw for kw in all_kwargs if kw.get("blocked_tables") == ["payment_methods"]
            )
            assert crm_kwargs["allow_explain"] is True

    @pytest.mark.asyncio
    async def test_orchestrator_receives_routing_tables(self, wired_lifespan) -> None:
        """The orchestrator is built with dict routing and default database."""
        async with wired_lifespan() as harness:
            captures: dict = harness["orchestrator_captures"]  # type: ignore[assignment]

            assert set(captures["sql_validators"]) == {"blog", "crm"}
            assert set(captures["sql_executors"]) == {"blog", "crm"}
            assert set(captures["pools"]) == {"blog", "crm"}
            assert captures["default_database"] == "blog"
            assert captures["metrics"] is not None
            assert captures["rate_limiter"] is not None

            limiter = captures["rate_limiter"]
            assert limiter.query_limiter.max_concurrent == ResilienceConfig().query_concurrency
            assert limiter.llm_limiter.max_concurrent == ResilienceConfig().llm_concurrency

    @pytest.mark.asyncio
    async def test_shutdown_closes_pools(self, wired_lifespan) -> None:
        """Shutdown closes every pool that was created."""
        async with wired_lifespan() as harness:
            pools_at_exit = dict(server_module._pools or {})
            assert set(pools_at_exit) == {"blog", "crm"}

        # After the lifespan exits, close_pools was called with the pools
        harness_close = harness["close_pools"]
        closed = harness_close.call_args[0][0]
        assert set(closed) == {"blog", "crm"}
