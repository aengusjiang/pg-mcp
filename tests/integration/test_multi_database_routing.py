"""Multi-database routing and per-database security integration tests.

Builds a real QueryOrchestrator over the three fixture PostgreSQL databases
(blog_small / ecommerce_medium / saas_crm_large) configured through a
temporary databases.json, with a deterministic stub standing in for the LLM
SQL generator. This exercises everything the unit tests mock out: real
connection pools, real schema introspection, real SQL execution against the
requested database, and real enforcement of per-database security overrides.

Because the stub replaces the LLM, these tests need the fixture databases
but not an OpenAI API key.
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.databases import load_databases_file
from pg_mcp.config.settings import (
    CacheConfig,
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    ValidationConfig,
)
from pg_mcp.db.pool import close_pools, create_pool
from pg_mcp.models.query import QueryRequest, QueryResponse, ReturnType
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import GenerationResult
from pg_mcp.services.sql_validator import SQLValidator
from tests.conftest import FIXTURE_DATABASES, fixture_connection_kwargs

pytestmark = pytest.mark.integration

# Marker -> SQL mapping used by the stub generator. Markers make the
# generated SQL deterministic per question without a real LLM call.
_STUB_SQL: dict[str, str] = {
    "[posts]": "SELECT COUNT(*) AS n FROM posts",
    "[orders]": "SELECT COUNT(*) AS n FROM orders",
    "[organizations]": "SELECT COUNT(*) AS n FROM organizations",
    "[sessions]": "SELECT COUNT(*) AS n FROM user_sessions",
    "[payments]": "SELECT COUNT(*) AS n FROM payments",
    "[user-email]": "SELECT email FROM users LIMIT 1",
    "[explain-organizations]": "EXPLAIN SELECT COUNT(*) FROM organizations",
    "[series]": "SELECT generate_series(1, 50000) AS n",
}


class StubSQLGenerator:
    """Deterministic stand-in for the LLM SQL generator.

    Returns canned SQL keyed by a marker embedded in the question and
    records every call so tests can assert on generation attempts.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(
        self,
        question: str,
        schema: Any,
        context: str | None = None,
        previous_attempt: str | None = None,
        error_feedback: str | None = None,
    ) -> GenerationResult:
        """Return the SQL mapped to the first marker found in the question."""
        self.calls.append(question)
        for marker, sql in _STUB_SQL.items():
            if marker in question:
                return GenerationResult(sql=sql, tokens_used=42)
        return GenerationResult(sql="SELECT 1 AS n", tokens_used=42)


@pytest.fixture
async def routing_stack(
    fixture_databases: dict[str, object],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Assemble the real three-database stack with a stub LLM generator.

    Writes a temporary databases.json carrying per-database security
    overrides, points DATABASES_FILE at it, then builds pools, validators,
    executors, and the schema cache exactly the way the server lifespan
    does. Yields a namespace with the orchestrator and the stub generator.
    """
    conn = fixture_connection_kwargs()

    databases = [
        {
            "name": "blog_small",
            "database": "blog_small",
            "pool": {"min_size": 1, "max_size": 5},
            "security": {
                "blocked_tables": ["user_sessions"],
                "blocked_columns": ["users.email"],
            },
        },
        {
            "name": "ecommerce_medium",
            "database": "ecommerce_medium",
            "pool": {"min_size": 1, "max_size": 5},
            "security": {"blocked_tables": ["payments"]},
        },
        {
            "name": "saas_crm_large",
            "database": "saas_crm_large",
            "pool": {"min_size": 1, "max_size": 5},
            "security": {"allow_explain": True},
        },
    ]
    for entry in databases:
        entry.update(
            host=conn["host"],
            port=conn["port"],
            user=conn["user"],
            password=conn["password"],
        )

    db_file = tmp_path / "databases.json"
    db_file.write_text(
        json.dumps({"default_database": "blog_small", "databases": databases}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASES_FILE", str(db_file))

    # Load the registry directly (not via Settings) so these tests do not
    # require an OPENAI_API_KEY just to construct the settings object.
    databases_file = load_databases_file(db_file)
    entries = {entry.name: entry for entry in databases_file.databases}
    default_database = databases_file.default_database

    security = SecurityConfig()
    pools: dict[str, Any] = {}
    validators: dict[str, SQLValidator] = {}
    executors: dict[str, SQLExecutor] = {}
    schema_cache = SchemaCache(CacheConfig())

    for name, entry in entries.items():
        db_config = entry.to_database_config()
        pool = await create_pool(db_config)
        pools[name] = pool

        # Merge overrides with global defaults the same way the lifespan does
        overrides = entry.security
        validators[name] = SQLValidator(
            config=security,
            blocked_tables=(
                overrides.blocked_tables
                if overrides.blocked_tables is not None
                else security.blocked_tables
            ),
            blocked_columns=(
                overrides.blocked_columns
                if overrides.blocked_columns is not None
                else security.blocked_columns
            ),
            allow_explain=(
                overrides.allow_explain
                if overrides.allow_explain is not None
                else security.allow_explain
            ),
        )
        executors[name] = SQLExecutor(
            pool=pool, security_config=security, db_config=db_config
        )
        await schema_cache.load(name, pool)

    generator = StubSQLGenerator()
    orchestrator = QueryOrchestrator(
        sql_generator=generator,
        sql_validators=validators,
        sql_executors=executors,
        result_validator=ResultValidator(
            # Dummy key satisfies format validation; never called because
            # validation is disabled below.
            openai_config=OpenAIConfig(api_key=SecretStr("sk-integration-stub")),
            validation_config=ValidationConfig(enabled=False),
        ),
        schema_cache=schema_cache,
        pools=pools,
        # High circuit-breaker threshold: several tests intentionally fail
        # validation, and the breaker must not open mid-suite.
        resilience_config=ResilienceConfig(
            max_retries=1,
            retry_delay=0.1,
            backoff_factor=1.0,
            circuit_breaker_threshold=100,
        ),
        validation_config=ValidationConfig(enabled=False),
        default_database=default_database,
        rate_limiter=MultiRateLimiter(query_limit=10, llm_limit=10),
    )

    try:
        yield SimpleNamespace(orchestrator=orchestrator, generator=generator)
    finally:
        await close_pools(pools)


async def _query(
    stack: SimpleNamespace,
    question: str,
    database: str | None = None,
    return_type: str = "result",
) -> QueryResponse:
    """Run one orchestrator request and return the QueryResponse."""
    request = QueryRequest(
        question=question,
        database=database,
        return_type=ReturnType(return_type),
    )
    return await stack.orchestrator.execute_query(request)


class TestMultiDatabaseRouting:
    """Each request executes against the database it resolved to."""

    async def test_each_database_serves_its_own_tables(self, routing_stack: Any) -> None:
        """The same COUNT(*) shape runs on each database's own tables."""
        cases = [
            ("blog_small", "[posts]"),
            ("ecommerce_medium", "[orders]"),
            ("saas_crm_large", "[organizations]"),
        ]
        for database, marker in cases:
            response = await _query(routing_stack, marker, database=database)
            assert response.success, f"{database}: {response.error}"
            assert response.data is not None
            assert response.data.row_count == 1  # COUNT(*) returns a single row

    async def test_execution_is_isolated_to_the_requested_database(
        self, routing_stack: Any
    ) -> None:
        """A blog-only table cannot be reached through the CRM database."""
        response = await _query(routing_stack, "[posts]", database="saas_crm_large")
        assert response.success is False
        assert response.error is not None
        assert response.error.code == "database_error"
        # 42P01 = undefined_table: the query really ran on saas_crm_large,
        # which has no posts relation.
        assert response.error.details["error_code"] == "42P01"

    async def test_default_database_used_when_omitted(self, routing_stack: Any) -> None:
        """Omitting the database parameter routes to the configured default."""
        response = await _query(routing_stack, "[posts]")
        assert response.success, response.error
        assert response.data is not None
        assert response.data.rows[0]["n"] == 10  # blog_small ships 10 posts

    async def test_unknown_database_lists_available_databases(
        self, routing_stack: Any
    ) -> None:
        """An unknown name fails with the available databases in details."""
        response = await _query(routing_stack, "[posts]", database="warehouse")
        assert response.success is False
        assert response.error is not None
        assert response.error.code == "database_error"
        assert sorted(response.error.details["available_databases"]) == sorted(
            FIXTURE_DATABASES
        )


class TestPerDatabaseSecurity:
    """Security overrides from databases.json are enforced per database."""

    async def test_blocked_table_enforced(self, routing_stack: Any) -> None:
        """payments is blocked on ecommerce_medium by its entry."""
        response = await _query(routing_stack, "[payments]", database="ecommerce_medium")
        assert response.success is False
        assert response.error is not None
        assert response.error.code == "security_violation"

    async def test_blocked_table_on_default_database(self, routing_stack: Any) -> None:
        """user_sessions exists on blog_small but is blocked there."""
        response = await _query(routing_stack, "[sessions]", database="blog_small")
        assert response.success is False
        assert response.error is not None
        assert response.error.code == "security_violation"

    async def test_blocked_column_enforced(self, routing_stack: Any) -> None:
        """users.email is masked on blog_small via blocked_columns."""
        response = await _query(routing_stack, "[user-email]", database="blog_small")
        assert response.success is False
        assert response.error is not None
        assert response.error.code == "security_violation"

    async def test_allow_explain_only_where_enabled(self, routing_stack: Any) -> None:
        """EXPLAIN runs on saas_crm_large (override) but not blog_small."""
        allowed = await _query(
            routing_stack, "[explain-organizations]", database="saas_crm_large"
        )
        assert allowed.success, allowed.error
        assert allowed.data is not None
        assert "QUERY PLAN" in allowed.data.columns

        rejected = await _query(
            routing_stack, "[explain-organizations]", database="blog_small"
        )
        assert rejected.success is False
        assert rejected.error is not None
        assert rejected.error.code == "security_violation"


class TestExecutionBehavior:
    """Executor-level guarantees hold over real connections."""

    async def test_row_limit_pushdown_caps_large_result_sets(
        self, routing_stack: Any
    ) -> None:
        """generate_series(1, 50000) is capped at max_rows by the wrapper."""
        response = await _query(routing_stack, "[series]", database="blog_small")
        assert response.success, response.error
        assert response.data is not None
        assert response.data.row_count == SecurityConfig().max_rows

    async def test_response_carries_request_id_and_tokens(
        self, routing_stack: Any
    ) -> None:
        """Tracing and token accounting are wired through the response."""
        response = await _query(routing_stack, "[posts]", database="blog_small")
        assert response.success
        assert response.request_id
        assert response.tokens_used == 42  # the stub reports 42 per attempt
