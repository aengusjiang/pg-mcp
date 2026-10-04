"""Security test suite for per-database policies and attack payloads.

These tests verify the security guarantees of the SQL validation layer:
the per-database security matrix (the same query may be legal on one
database and blocked on another) and rejection of common attack payloads
(injection, multi-statement, UNION-based data theft, write operations,
dangerous functions, and EXPLAIN-based bypass attempts).
"""

import pytest

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError
from pg_mcp.services.sql_validator import SQLValidator


def make_validator(
    blocked_tables: list[str] | None = None,
    blocked_columns: list[str] | None = None,
    allow_explain: bool = False,
) -> SQLValidator:
    """Build a validator with an otherwise-default security config."""
    return SQLValidator(
        config=SecurityConfig(),
        blocked_tables=blocked_tables,
        blocked_columns=blocked_columns,
        allow_explain=allow_explain,
    )


class TestDualDatabaseSecurityMatrix:
    """The same SQL must pass or fail per the target database's policy."""

    def test_blocked_table_enforced_only_where_configured(self) -> None:
        """audit_log is blocked on blog_small but allowed on saas_crm_large."""
        blog_validator = make_validator(blocked_tables=["audit_log"])
        crm_validator = make_validator()

        sql = "SELECT * FROM audit_log"

        with pytest.raises(SecurityViolationError, match="audit_log"):
            blog_validator.validate_or_raise(sql)

        is_valid, error = crm_validator.validate(sql)
        assert is_valid
        assert error is None

    def test_blocked_column_enforced_only_where_configured(self) -> None:
        """users.email is masked on one database and readable on another."""
        masked_validator = make_validator(blocked_columns=["users.email"])
        open_validator = make_validator()

        sql = "SELECT users.email FROM users"

        with pytest.raises(SecurityViolationError, match="email"):
            masked_validator.validate_or_raise(sql)

        is_valid, error = open_validator.validate(sql)
        assert is_valid
        assert error is None

    def test_blocked_column_bypass_attempts(self) -> None:
        """Masked columns cannot be reached by spelling tricks."""
        validator = make_validator(blocked_columns=["users.email"])

        bypass_attempts = [
            "SELECT email FROM users",  # bare column name
            "SELECT u.email FROM users u",  # table alias
            "SELECT * FROM users",  # star projection
            "SELECT users.* FROM users",  # qualified star projection
            "SELECT u.* FROM users AS u",  # aliased star projection
            "SELECT USERS.EMAIL FROM USERS",  # case variation
        ]
        for sql in bypass_attempts:
            is_valid, error = validator.validate(sql)
            assert not is_valid, f"bypass succeeded: {sql}"
            assert error is not None

    def test_blocked_column_unaffected_queries_still_pass(self) -> None:
        """Masking one column does not lock down the whole table."""
        validator = make_validator(blocked_columns=["users.email"])

        allowed_queries = [
            "SELECT username FROM users",
            "SELECT email FROM customers",  # same column name, other table
            "SELECT COUNT(*) FROM users",
        ]
        for sql in allowed_queries:
            is_valid, error = validator.validate(sql)
            assert is_valid, f"false rejection: {sql} ({error})"
            assert error is None

    def test_allow_explain_matrix(self) -> None:
        """EXPLAIN is permitted only on the database that opted in."""
        explain_validator = make_validator(allow_explain=True)
        default_validator = make_validator(allow_explain=False)

        sql = "EXPLAIN SELECT * FROM users"

        is_valid, error = explain_validator.validate(sql)
        assert is_valid
        assert error is None

        with pytest.raises(SecurityViolationError):
            default_validator.validate_or_raise(sql)

    def test_blocked_function_shared_across_databases(self) -> None:
        """Built-in dangerous functions are blocked regardless of database."""
        for validator in (make_validator(), make_validator(blocked_tables=["x"])):
            with pytest.raises(SecurityViolationError, match="pg_sleep"):
                validator.validate_or_raise("SELECT pg_sleep(100)")


class TestInjectionPayloads:
    """Malicious SQL payloads must all be rejected."""

    @pytest.mark.parametrize(
        "sql",
        [
            # Stacked statements
            "SELECT * FROM users; DROP TABLE users;--",
            "SELECT 1; SELECT 2",
            # UNION-based data theft against a blocked table
            "SELECT id, name FROM users UNION SELECT * FROM audit_log",
            "SELECT 1 UNION ALL SELECT * FROM secrets",
            # Write operations
            "INSERT INTO logs VALUES (1, 'x')",
            "UPDATE users SET is_admin = true",
            "DELETE FROM users WHERE id = 1",
            "TRUNCATE TABLE audit_log",
            "DROP TABLE users",
            "ALTER TABLE users ADD COLUMN backdoor text",
            "CREATE TABLE evil (id int)",
            "GRANT ALL ON users TO public",
            # Dangerous server-side functions
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT dblink('host=evil dbname=x', 'SELECT 1')",
            "SELECT lo_import('/etc/shadow')",
            "SELECT pg_terminate_backend(42)",
            # Session/system manipulation
            "SET statement_timeout = 0",
        ],
    )
    def test_attack_payloads_rejected(self, sql: str) -> None:
        """Every attack payload is rejected by a default-config validator."""
        validator = make_validator(blocked_tables=["audit_log", "secrets"])

        is_valid, error = validator.validate(sql)
        assert not is_valid
        assert error is not None

    @pytest.mark.parametrize(
        "sql",
        [
            # Data-modifying CTE smuggled inside a SELECT
            "WITH del AS (DELETE FROM users RETURNING *) SELECT * FROM del",
            "WITH ins AS (INSERT INTO logs VALUES (1) RETURNING *) SELECT * FROM ins",
            # Write operations nested in subqueries
            "SELECT * FROM (DELETE FROM users RETURNING *) AS doomed",
        ],
    )
    def test_write_operations_hidden_in_cte_and_subqueries_rejected(
        self, sql: str
    ) -> None:
        """Write operations smuggled through CTEs or subqueries are rejected.

        sqlglot currently rejects data-modifying CTEs at parse time
        (SQLParseError); if a future parser accepts them, the CTE safety
        check rejects them with SecurityViolationError. Either way the
        query must not validate.
        """
        validator = make_validator()

        is_valid, error = validator.validate(sql)
        assert not is_valid
        assert error is not None


class TestExplainBypassAttempts:
    """EXPLAIN must not become a side door around the security policy."""

    @pytest.mark.parametrize(
        "sql",
        [
            "EXPLAIN ANALYZE DELETE FROM users",
            "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM audit_log",
            "EXPLAIN ANALYZE SELECT * FROM audit_log",
            "EXPLAIN ANALYSE SELECT pg_sleep(100)",
        ],
    )
    def test_explain_analyze_bypass_attempts_rejected(self, sql: str) -> None:
        """ANALYZE executes the inner statement and is always rejected."""
        validator = make_validator(blocked_tables=["audit_log"], allow_explain=True)

        is_valid, error = validator.validate(sql)
        assert not is_valid
        assert error is not None

    def test_explain_cannot_reach_blocked_table(self) -> None:
        """Plain EXPLAIN over a blocked table is still rejected."""
        validator = make_validator(blocked_tables=["payment_methods"], allow_explain=True)

        with pytest.raises(SecurityViolationError, match="payment_methods"):
            validator.validate_or_raise("EXPLAIN SELECT * FROM payment_methods")

    def test_explain_analyze_rejected_even_when_explain_disabled(self) -> None:
        """With EXPLAIN fully disabled, every EXPLAIN form is rejected."""
        validator = make_validator(allow_explain=False)

        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise("EXPLAIN ANALYZE SELECT 1")
