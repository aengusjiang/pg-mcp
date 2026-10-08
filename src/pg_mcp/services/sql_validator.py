"""SQL Security Validator using SQLGlot.

This module provides SQL validation and security checking using SQLGlot parser.
It ensures that only safe, read-only queries are executed and blocks potentially
dangerous operations.
"""

from typing import ClassVar

import sqlglot
from sqlglot import exp

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError, SQLParseError


class SQLValidator:
    """SQL security validator using SQLGlot for parsing and validation.

    This validator ensures queries are safe by:
    - Allowing only SELECT statements
    - Blocking dangerous functions (pg_sleep, file operations, etc.)
    - Preventing access to blocked tables and columns
    - Rejecting multi-statement queries
    - Validating subquery safety
    """

    # Allowed statement types at the top level (including set operations)
    ALLOWED_STATEMENT_TYPES: ClassVar = {
        exp.Select, exp.Union, exp.Intersect, exp.Except
    }

    # Allowed top-level expressions (including CTEs)
    ALLOWED_TOP_LEVEL: ClassVar = {
        exp.Select, exp.Union, exp.Intersect, exp.Except, exp.With, exp.Subquery
    }

    # Forbidden statement types
    FORBIDDEN_STATEMENT_TYPES: ClassVar = {
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Grant,
        exp.Revoke,
        exp.Set,
        exp.Command,
        exp.Use,
        exp.Merge,
    }

    # Built-in dangerous PostgreSQL functions
    BUILTIN_DANGEROUS_FUNCTIONS: ClassVar = {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_open",
        "pg_write_file",
        "pg_execute_sql",
        "copy_from",
        "copy_to",
    }

    # EXPLAIN option keywords that may precede the explained statement.
    # ANALYZE/ANALYSE actually execute the inner statement and are always
    # rejected (even with allow_explain=True); the rest are plan-only options.
    EXPLAIN_OPTIONS: ClassVar = {
        "ANALYZE",
        "ANALYSE",
        "VERBOSE",
        "COSTS",
        "BUFFERS",
        "TIMING",
        "SUMMARY",
    }
    EXPLAIN_VALUE_OPTIONS: ClassVar = {"FORMAT"}
    EXPLAIN_BOOLEAN_VALUES: ClassVar = {"ON", "OFF", "TRUE", "FALSE"}

    def __init__(
        self,
        config: SecurityConfig,
        blocked_tables: list[str] | None = None,
        blocked_columns: list[str] | None = None,
        allow_explain: bool = False,
    ) -> None:
        """Initialize SQL validator.

        Args:
            config: Security configuration containing blocked functions and settings.
            blocked_tables: Optional list of table names to block access to.
            blocked_columns: Optional list of column names to block access to.
            allow_explain: Whether to allow EXPLAIN statements.
        """
        self.config = config
        self.blocked_tables = {t.lower() for t in (blocked_tables or [])}
        self.blocked_columns = {c.lower() for c in (blocked_columns or [])}
        self.allow_explain = allow_explain

        # Combine built-in dangerous functions with custom blocked functions
        self.blocked_functions = self.BUILTIN_DANGEROUS_FUNCTIONS | {
            f.lower() for f in config.blocked_functions
        }

    def validate(self, sql: str) -> tuple[bool, str | None]:
        """Validate SQL query for security compliance.

        Args:
            sql: SQL query string to validate.

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        try:
            self.validate_or_raise(sql)
            return (True, None)
        except (SecurityViolationError, SQLParseError) as e:
            return (False, str(e))

    def validate_or_raise(self, sql: str) -> None:
        """Validate SQL query and raise exception on violation.

        Args:
            sql: SQL query string to validate.

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: If SQL violates security constraints.
        """
        # Check for empty or whitespace-only SQL
        if not sql or not sql.strip():
            raise SQLParseError("SQL query cannot be empty")

        # Parse SQL using SQLGlot
        try:
            parsed = sqlglot.parse(sql, read="postgres")
        except Exception as e:
            raise SQLParseError(f"Failed to parse SQL: {e}") from e

        # Check for multiple statements
        if len(parsed) > 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted."
            )

        if not parsed:
            raise SQLParseError("No valid SQL statement found")

        statement = parsed[0]

        # Check for null or empty statement (e.g., comment-only SQL)
        if statement is None or isinstance(statement, type(None)):
            raise SQLParseError("No valid SQL statement found")

        # Handle EXPLAIN statements (parsed as Command in sqlglot 28.5.0)
        if isinstance(statement, exp.Command):
            # Check if it's an EXPLAIN command
            cmd_name = str(statement.this).upper() if statement.this else ""
            if cmd_name == "EXPLAIN":
                self._validate_explain(statement)
                return None
            else:
                # Other commands are not allowed
                raise SecurityViolationError(
                    f"Command '{cmd_name}' is not allowed. Only SELECT queries are permitted."
                )

        # Handle CTE (WITH) statements - extract the main query
        if isinstance(statement, exp.With):
            # WITH statements are allowed, but we need to validate the main query
            if statement.this:
                main_query = statement.this
            else:
                raise SQLParseError("WITH statement has no main query")
        else:
            main_query = statement

        # Perform security checks
        if error := self._check_statement_type(main_query):
            raise SecurityViolationError(error)

        if error := self._check_dangerous_functions(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_tables(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_columns(statement):
            raise SecurityViolationError(error)

        if error := self._check_cte_safety(statement):
            raise SecurityViolationError(error)

        if error := self._check_subquery_safety(statement):
            raise SecurityViolationError(error)

    def _validate_explain(self, statement: exp.Command) -> None:
        """Validate an EXPLAIN statement by validating its inner query.

        sqlglot 28.5.0 parses ``EXPLAIN ...`` as a Command whose expression
        literal holds the remainder of the statement. Policy:

        - ``allow_explain`` disabled -> reject.
        - ``ANALYZE``/``ANALYSE`` option present -> reject even when enabled
          (it executes the inner statement; a separate opt-in may be added
          in the future, but none exists today).
        - Otherwise strip the plan options and run the inner statement
          through the full validation pipeline (statement type, dangerous
          functions, blocked tables/columns, subquery safety).
        - If the inner statement cannot be extracted or parsed -> reject
          (fail-closed).

        Args:
            statement: The parsed EXPLAIN Command node.

        Raises:
            SecurityViolationError: When EXPLAIN is disabled, ANALYZE is
                requested, or the inner statement violates security rules.
            SQLParseError: When the inner statement cannot be parsed.
        """
        if not self.allow_explain:
            raise SecurityViolationError("EXPLAIN statements are not allowed")

        remainder = ""
        if isinstance(statement.expression, exp.Literal):
            remainder = str(statement.expression.this)

        inner_sql, has_analyze = self._strip_explain_options(remainder)
        if has_analyze:
            raise SecurityViolationError(
                "EXPLAIN ANALYZE is not allowed: ANALYZE executes the inner statement "
                "(no separate opt-in exists; plain EXPLAIN is permitted when enabled)"
            )
        if not inner_sql.strip():
            raise SQLParseError("EXPLAIN statement has no inner query to validate")

        # Fail-closed: the inner statement goes through the complete pipeline.
        self.validate_or_raise(inner_sql)

    def _strip_explain_options(self, remainder: str) -> tuple[str, bool]:
        """Strip leading EXPLAIN option modifiers from the statement text.

        Args:
            remainder: Text following the EXPLAIN keyword (e.g.
                "ANALYZE SELECT * FROM users" or "(ANALYZE, BUFFERS) SELECT 1").

        Returns:
            Tuple of (inner_sql, has_analyze): the statement with option
            modifiers removed, and whether an ANALYZE option was seen.
        """
        text = remainder.strip()
        has_analyze = False

        while text:
            # Parenthesized option list: (ANALYZE, BUFFERS, ...)
            if text.startswith("("):
                close = text.find(")")
                if close == -1:
                    break  # malformed; left for the parser to fail closed
                for option in text[1:close].split(","):
                    if option.strip().upper() in ("ANALYZE", "ANALYSE"):
                        has_analyze = True
                text = text[close + 1 :].lstrip()
                continue

            first, _, rest = text.partition(" ")
            keyword = first.upper()

            if keyword in ("ANALYZE", "ANALYSE"):
                has_analyze = True
                text = rest.lstrip()
                continue
            if keyword in self.EXPLAIN_OPTIONS:
                text = rest.lstrip()
                # TIMING/SUMMARY may carry an ON/OFF value
                if text.partition(" ")[0].upper() in self.EXPLAIN_BOOLEAN_VALUES:
                    text = text.partition(" ")[2].lstrip()
                continue
            if keyword in self.EXPLAIN_VALUE_OPTIONS:
                # e.g. FORMAT JSON - consume the option and its value
                text = rest.partition(" ")[2].lstrip()
                continue
            break

        return text, has_analyze

    def _check_statement_type(self, statement: exp.Expression) -> str | None:
        """Check if statement type is allowed.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Check for forbidden statement types
        for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
            if isinstance(statement, forbidden_type):
                stmt_name = forbidden_type.__name__.upper()
                return f"{stmt_name} statements are not allowed. Only SELECT queries are permitted."

        # Ensure statement is an allowed type (SELECT or set operations)
        if not isinstance(statement, tuple(self.ALLOWED_STATEMENT_TYPES)):
            stmt_type = type(statement).__name__
            return f"Statement type {stmt_type} is not allowed. Only SELECT queries are permitted."

        return None

    def _check_dangerous_functions(self, statement: exp.Expression) -> str | None:
        """Check for use of blocked/dangerous functions.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all function calls in the query
        for func in statement.find_all(exp.Func):
            func_name = func.name.lower() if func.name else ""

            if func_name in self.blocked_functions:
                return f"Function '{func_name}' is blocked for security reasons"

        return None

    def _check_blocked_tables(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked tables.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_tables:
            return None

        # Find all table references
        for table in statement.find_all(exp.Table):
            table_name = table.name.lower() if table.name else ""

            if table_name in self.blocked_tables:
                return f"Access to table '{table_name}' is not allowed"

        return None

    def _check_blocked_columns(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked columns.

        Blocked columns are configured as bare names ("email") or qualified
        names ("users.email"). A reference is rejected when any of its
        spellings resolves to a blocked entry:

        - bare name matching a bare entry ("email" vs "email")
        - qualified name matching a qualified entry ("users.email")
        - qualified name whose table part is an alias that resolves to a
          blocked table ("u.email" with "FROM users u" vs "users.email")
        - bare name whose statement reads a table holding that blocked
          column ("email" with "FROM users" vs "users.email") — closed on
          ambiguity because the server cannot know which table the user
          meant
        - a ``*`` or ``table.*`` projection over a table that has any
          blocked column

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_columns:
            return None

        # Split entries into bare names and table-column pairs. Entries with
        # a schema prefix ("public.users.email") keep their last two parts.
        blocked_bare: set[str] = set()
        blocked_by_column: dict[str, set[str]] = {}
        for entry in self.blocked_columns:
            parts = entry.lower().split(".")
            if len(parts) >= 2:
                blocked_by_column.setdefault(parts[-1], set()).add(parts[-2])
            else:
                blocked_bare.add(parts[0])

        # Alias resolution: alias and bare table name -> real table name.
        table_names: set[str] = set()
        alias_to_table: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            real = table.name.lower()
            table_names.add(real)
            alias_to_table[real] = real
            if table.alias:
                alias_to_table[table.alias.lower()] = real

        for column in statement.find_all(exp.Column):
            column_name = column.name.lower() if column.name else ""
            column_table = column.table.lower() if column.table else ""

            if isinstance(column.this, exp.Star):
                # "table.*" projection: reject when that table has any
                # blocked column.
                source = alias_to_table.get(column_table, column_table)
                if source and any(
                    source in tables for tables in blocked_by_column.values()
                ):
                    return f"Access to all columns of '{source}' is not allowed"
                continue

            if not column_name:
                continue
            if column_name in blocked_bare:
                return f"Access to column '{column_name}' is not allowed"

            blocked_tables = blocked_by_column.get(column_name)
            if not blocked_tables:
                continue
            if column_table:
                source = alias_to_table.get(column_table, column_table)
                if source in blocked_tables:
                    return f"Access to column '{source}.{column_name}' is not allowed"
            else:
                # Bare reference: fail closed when the statement reads a
                # table whose copy of this column is blocked.
                overlap = blocked_tables & table_names
                if overlap:
                    owner = sorted(overlap)[0]
                    return f"Access to column '{owner}.{column_name}' is not allowed"

        # Bare "*" projection: fail closed when any table read by this
        # statement has blocked columns at all. Only stars in the SELECT
        # projection list leak columns — COUNT(*) and similar aggregates
        # (star inside a function) project nothing and are left alone.
        for star in statement.find_all(exp.Star):
            if not isinstance(star.parent, exp.Select):
                continue  # table.* handled above; COUNT(*) projects nothing
            for referenced in table_names:
                if any(referenced in tables for tables in blocked_by_column.values()):
                    return (
                        f"Access to all columns of '{referenced}' is not allowed "
                        "(some columns are blocked); list columns explicitly"
                    )

        return None

    def _check_cte_safety(self, statement: exp.Expression) -> str | None:
        """Check that CTE definitions only contain SELECT statements.

        PostgreSQL allows data-modifying CTEs (``WITH del AS (DELETE ...)
        SELECT ...``); sqlglot currently rejects them at parse time, but if
        a future parser version accepts them this check keeps them blocked
        (defense in depth).

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        for cte in statement.find_all(exp.CTE):
            inner = cte.this
            if inner is None:
                continue

            for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
                if isinstance(inner, forbidden_type):
                    stmt_name = forbidden_type.__name__.upper()
                    return f"{stmt_name} statements in CTEs are not allowed"

            if not isinstance(
                inner, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.With)
            ):
                return "CTEs must contain only SELECT statements"

        return None

    def _check_subquery_safety(self, statement: exp.Expression) -> str | None:
        """Check that all subqueries only contain SELECT statements.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all subqueries
        for subquery in statement.find_all(exp.Subquery):
            if subquery.this:
                inner_stmt = subquery.this

                # Check if the inner statement is a forbidden type
                for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
                    if isinstance(inner_stmt, forbidden_type):
                        stmt_name = forbidden_type.__name__.upper()
                        return f"{stmt_name} statements in subqueries are not allowed"

                # Ensure it's a SELECT
                if not isinstance(inner_stmt, (exp.Select, exp.With)):
                    return "Subqueries must contain only SELECT statements"

        return None

    def normalize_sql(self, sql: str) -> str:
        """Normalize SQL query to a canonical form.

        This removes extra whitespace, standardizes formatting, and makes
        queries easier to compare or cache.

        Args:
            sql: SQL query string to normalize.

        Returns:
            Normalized SQL string.

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            # Generate normalized SQL
            return parsed.sql(dialect="postgres", pretty=False)
        except Exception as e:
            raise SQLParseError(f"Failed to normalize SQL: {e}") from e

    def extract_tables(self, sql: str) -> list[str]:
        """Extract all table names referenced in the SQL query.

        Args:
            sql: SQL query string.

        Returns:
            List of table names (in lowercase).

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            tables = []

            for table in parsed.find_all(exp.Table):
                if table.name:
                    tables.append(table.name.lower())

            return sorted(set(tables))
        except Exception as e:
            raise SQLParseError(f"Failed to extract tables: {e}") from e
