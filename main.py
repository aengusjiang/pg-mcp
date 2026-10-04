"""Entry point shim for the PostgreSQL MCP server.

Delegates to the package entry point so `python main.py`, `uv run python
main.py`, and the `pg-mcp` console script all run the same code path.
"""

from pg_mcp.__main__ import main

if __name__ == "__main__":
    main()
