"""Scripted MCP smoke test for pg-mcp.

Starts the server over stdio (same transport MCP Inspector and Claude
Desktop use), drives the documented smoke checklist against the fixture
databases, then captures the Prometheus metrics endpoint while the server
is still alive. Output is plain text designed to be readable directly or
captured into docs/images/ for the enhancement report.

Usage (from the repository root, with .env and databases.json in place):

    uv run python scripts/mcp_smoke.py
"""

import asyncio
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
METRICS_URL = "http://localhost:9090/metrics"
PER_CALL_TIMEOUT = 180.0

# (label, arguments, expected_error_code) triples implementing the smoke
# checklist from docs/DEVELOPMENT.md section 9.2 plus a default-database
# call. expected_error_code is None for cases that must succeed.
SMOKE_CASES: list[tuple[str, dict[str, str], str | None]] = [
    (
        "route to blog_small (default pool)",
        {"question": "统计每个用户的文章数", "database": "blog_small"},
        None,
    ),
    (
        "route to saas_crm_large (second pool)",
        {"question": "每个组织有多少个客户账户", "database": "saas_crm_large"},
        None,
    ),
    (
        "blocked table interception",
        {"question": "查询 user_sessions 表的全部数据", "database": "blog_small"},
        "security_violation",
    ),
    (
        "unknown database error",
        {"question": "一共有多少篇文章", "database": "nope"},
        "database_error",
    ),
    (
        "default database (omitted)",
        {"question": "一共有多少个用户"},
        None,
    ),
]

METRICS_WHITELIST = (
    "pg_mcp_query_requests_total",
    "pg_mcp_llm_calls_total",
    "pg_mcp_llm_tokens_used",
    "pg_mcp_sql_rejected_total",
    "pg_mcp_db_connections_active",
    "pg_mcp_schema_cache_age_seconds",
)


def _fmt_case(index: int, label: str, args: dict[str, str]) -> str:
    shown = dict(args)
    line = f'[{index}] {label}\n    query(question="{shown.get("question", "")}"'
    if "database" in shown:
        line += f', database="{shown["database"]}"'
    line += ")"
    return line


def _fmt_envelope(raw_text: str) -> str:
    try:
        payload: Any = json.loads(raw_text)
    except json.JSONDecodeError:
        return f"    ! non-JSON response: {raw_text[:200]}"
    if not isinstance(payload, dict):
        return f"    ! unexpected payload type: {type(payload).__name__}"
    lines: list[str] = []
    if payload.get("success"):
        data = payload.get("data") or {}
        lines.append(
            f"    -> success=true  rows={data.get('row_count')}  "
            f"time={data.get('execution_time_ms')}ms  "
            f"confidence={payload.get('confidence')}  tokens={payload.get('tokens_used')}"
        )
        sql = (payload.get("generated_sql") or "").replace("\n", " ")
        lines.append(f"    sql: {sql[:110]}{'…' if len(sql) > 110 else ''}")
        rows = data.get("rows") or []
        if rows:
            first = json.dumps(rows[0], ensure_ascii=False)
            lines.append(f"    first row: {first[:110]}{'…' if len(first) > 110 else ''}")
    else:
        err = payload.get("error") or {}
        lines.append(f"    -> success=false  code={err.get('code')}")
        msg = str(err.get("message", ""))[:110]
        lines.append(f"    message: {msg}")
        details = json.dumps(err.get("details") or {}, ensure_ascii=False)
        lines.append(f"    details: {details[:110]}{'…' if len(details) > 110 else ''}")
    request_id = payload.get("request_id")
    if request_id:
        lines.append(f"    request_id: {request_id}")
    return "\n".join(lines)


def _fetch_metrics() -> str:
    try:
        # S310: METRICS_URL is a fixed module-level http:// constant, not
        # externally controlled input.
        with urllib.request.urlopen(METRICS_URL, timeout=10) as response:  # noqa: S310
            body = response.read().decode("utf-8")
    except Exception as e:
        return f"metrics fetch failed: {e}"
    kept: list[str] = []
    for line in body.splitlines():
        if line.startswith("#") or not line.strip() or "_created" in line:
            continue
        if any(line.startswith(name) for name in METRICS_WHITELIST):
            kept.append(line)
    return "\n".join(kept) if kept else "(no matching metric lines)"


async def _run_cases(session: ClientSession) -> int:
    """Run the smoke checklist against one live session; return failure count."""
    failures = 0
    for index, (label, args, expected_code) in enumerate(SMOKE_CASES, start=1):
        print(_fmt_case(index, label, args))
        try:
            result = await asyncio.wait_for(
                session.call_tool("query", arguments=args), PER_CALL_TIMEOUT
            )
        except TimeoutError:
            failures += 1
            print(f"    ! timed out after {PER_CALL_TIMEOUT}s")
            print()
            continue
        text = result.content[0].text if result.content else ""
        print(_fmt_envelope(text))
        # Checklist expectation per case: success, or a specific error code.
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
        success = bool(payload.get("success"))
        actual_code = (payload.get("error") or {}).get("code")
        matched = success if expected_code is None else actual_code == expected_code
        if not matched:
            failures += 1
            expected = "success" if expected_code is None else expected_code
            print(f"    !! unexpected outcome (expected {expected}, got {actual_code})")
        print()
    return failures


async def main() -> int:
    server = StdioServerParameters(
        command="uv",
        args=["--directory", str(REPO_ROOT), "run", "python", "main.py"],
    )
    failures = 0
    completed = False
    print(
        f"pg-mcp smoke session  {datetime.now().strftime('%Y-%m-%d %H:%M')}  "
        f"(stdio transport, config from .env / databases.json)"
    )
    print()
    try:
        async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            print("connected: server initialized, tools listed")
            print()
            failures += await _run_cases(session)
            completed = True
            print(f"$ curl -s {METRICS_URL}   (server still running)")
            print(_fetch_metrics())
    except Exception as shutdown_noise:
        # The stdio transport occasionally races during teardown
        # (BrokenResourceError when the server process closes its streams
        # first). That is harmless once all cases have run; any exception
        # before that point is a real failure.
        if not completed:
            failures += 1
            print(
                f"! aborted by transport error: {type(shutdown_noise).__name__}: "
                f"{str(shutdown_noise)[:120]}"
            )
        else:
            print(f"(transport closed with {type(shutdown_noise).__name__} - expected on teardown)")
    print()
    print(f"smoke result: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
