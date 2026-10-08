"""Database connection and introspection utilities.

This package provides database connection pool management and schema
introspection capabilities for PostgreSQL.
"""

from pg_mcp.db.introspection import SchemaIntrospector
from pg_mcp.db.pool import close_pools, create_pool

__all__ = [
    "SchemaIntrospector",
    "create_pool",
    "close_pools",
]
