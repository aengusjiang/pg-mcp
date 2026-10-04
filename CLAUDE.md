# CLAUDE.md - PostgreSQL MCP Server 开发指南

## 项目概述

pg-mcp 是基于 Model Context Protocol 的 PostgreSQL 自然语言查询服务器：MCP 客户端（Claude Desktop / MCP Inspector 等）调用 `query` 工具，服务端将自然语言问题交给 LLM 生成 SQL，经安全校验后在**指定数据库**上执行并返回结果。支持多数据库路由与每库独立安全策略。设计文档见 `specs/w6/`。

## 技术栈

- **Python**: 3.14（`requires-python = ">=3.14"`）
- **包管理**: uv（`uv sync` / `uv run`）
- **MCP SDK**: `mcp`（`from mcp.server.fastmcp import FastMCP`），stdio transport + lifespan
- **PostgreSQL Driver**: asyncpg（异步、连接池）
- **SQL 解析/校验**: sqlglot（`sqlglot.parse_one` / `exp` 节点遍历；**不是 pglast**）
- **LLM**: OpenAI SDK（`gpt-4o-mini` 默认，返回 `GenerationResult(sql, tokens_used)`；支持 `OPENAI_BASE_URL` 接入 OpenAI 兼容网关，此时 key 不要求 `sk-` 前缀）
- **配置**: pydantic-settings（嵌套节均带 `env_file='.env'`；多库走 `databases.json`）
- **可观测**: structlog + prometheus-client（/metrics HTTP）
- **测试**: pytest + pytest-asyncio（`asyncio_mode = "auto"`）+ pytest-cov

## 目录布局（真实）

```text
src/pg_mcp/
├── __main__.py              # python -m pg_mcp 入口
├── server.py                # FastMCP 服务器：lifespan 装配 + query 工具
├── config/
│   ├── settings.py          # Settings 及 8 个嵌套配置节（pydantic-settings）
│   └── databases.py         # databases.json 加载、DatabaseRegistry、env 回退
├── db/
│   ├── pool.py              # create_pool / close_pools
│   └── introspection.py     # SchemaIntrospector（系统目录内省）
├── cache/schema_cache.py    # Schema TTL 缓存 + LRU + 自动刷新
├── models/
│   ├── schema.py            # DatabaseSchema 等数据模型
│   ├── query.py             # QueryRequest/QueryResponse/QueryResult
│   └── errors.py            # ErrorCode 枚举 + 异常层次（PgMcpError 基类）
├── services/
│   ├── orchestrator.py      # 查询编排：路由/重试/退避/指标/request_id
│   ├── sql_generator.py     # LLM 调用 → GenerationResult
│   ├── sql_validator.py     # sqlglot 安全校验（白名单+黑名单+EXPLAIN 策略）
│   ├── sql_executor.py      # asyncpg 执行：只读事务 + 行限制下推
│   └── result_validator.py  # LLM 结果置信度评估（可关）
├── observability/
│   ├── logging.py           # structlog 配置 + 敏感信息过滤
│   ├── metrics.py           # MetricsCollector（Prometheus）
│   └── tracing.py           # request_id（contextvars）+ 日志注入
├── prompts/                 # LLM Prompt 模板
└── resilience/
    ├── rate_limiter.py      # MultiRateLimiter（信号量并发限制，非 QPS）
    └── circuit_breaker.py   # LLM 熔断器
```

## 核心机制（改代码前必读）

### 多数据库路由

- 配置来源：`DATABASES_FILE` 指向 `databases.json`（多库，见 `databases.json.example`）；未设置时回退 `.env` 的 `DATABASE_*` 单库。
- lifespan 为每个库建独立 pool/validator/executor，以逻辑名（`name` 字段）为 key；`QueryOrchestrator` 持有 `sql_validators: dict` / `sql_executors: dict`。
- 请求解析顺序：显式 `database` 参数 → `default_database` → 唯一库自动选择；未知名/歧义返回 `database_error`（details 带 `available_databases`）。

### 每库安全策略

`databases.json` 中 `security.*` 未设（null）时继承 `.env` 的 `SECURITY_*` 全局默认：

- `blocked_tables`：表黑名单（含别名场景）
- `blocked_columns`：列掩码，`"table.column"` 形式；裸列名、表别名（`u.email`）、`SELECT *`、`table.*` 均无法绕过（fail-closed）
- `allow_explain`：EXPLAIN 开关；即使开启，`ANALYZE` 变体仍被拒绝（会真实执行内层语句）；内层语句完整递归校验

### SQL 校验管线（sql_validator.py）

单条 SELECT 白名单 + 多层黑名单检查，顺序：解析 → 语句类型 → 多语句 → CTE 安全 → 子查询安全 → 阻止表 → 阻止列 → 阻止函数。任何解析失败即拒绝（fail-closed）。

### 执行安全（sql_executor.py）

- 只读事务 + `statement_timeout` + 安全 `search_path`
- 行限制下推：`SELECT * FROM (<sql>) AS _limited LIMIT max_rows + 1`（EXPLAIN 透传）
- 瞬时故障重试：`TRANSIENT_SQLSTATES`（08000/08003/08006/53300/40001/40P01 等）指数退避，上限 30s

### 错误信封

所有错误统一 `{success: false, error: {code, message[, details]}, confidence: 0, tokens_used: N}`。`ErrorCode` 枚举值为**小写字符串**（`database_error`、`security_violation`、`invalid_request`、`invalid_parameter`、`server_not_initialized`、`rate_limit_exceeded` 等）。`request_id` 贯穿日志与响应。

## Python 规范

1. **类型注解**：所有公开 API 完整注解（mypy strict 通过是门禁）
2. **docstring**：公开类/函数 Google style
3. **错误处理**：抛 `PgMcpError` 子类（带 `ErrorCode`），不裸 `except`
4. **日志脱敏**：绝不在日志/指标中输出密码、PII（DSN 用 `safe_dsn`）
5. **资源管理**：pool/连接用 context manager；`close_pools` 超时强杀
6. **配置校验**：所有边界输入（databases.json、question 等）schema 校验、fail-fast

```bash
# Lint + 格式化（ruff，规则见 pyproject.toml）
uv run ruff check --fix .
uv run ruff format .

# 类型检查（strict）
uv run mypy src
```

### 安全编码红线

```python
# 永远不要拼接 SQL
# BAD
sql = f"SELECT * FROM {table_name}"
# GOOD — 行限制包裹仅用于已过校验的内部 SQL（见 _wrap_with_limit 的 noqa 说明）

# 校验所有外部输入（pydantic Field 约束 + validator）

# 新增拦截规则时：先加测试（tests/security/），再改 sql_validator.py
```

## 测试

### 结构

```text
tests/
├── conftest.py                  # 全局 fixture + 集成守卫（fixture_databases / require_llm_environment / blog_environment）
├── unit/                        # 离线单测（mock 协作者）
│   ├── test_databases_config.py # databases.json 加载/校验/env 回退
│   ├── test_orchestrator.py     # 路由/重试/指标/request_id（含 TestPerDatabaseRouting）
│   ├── test_sql_validator.py    # 校验管线全分支
│   ├── test_sql_executor.py     # 行限制下推/序列化/会话参数
│   ├── test_sql_generator.py    # GenerationResult + token 提取
│   ├── test_server_lifespan.py  # lifespan 装配（registry 循环/override 合并）
│   ├── test_server_tool.py      # query 工具信封/限流超时
│   ├── test_db_pool.py          # create_pool/close_pools
│   └── ...
├── security/                    # 安全矩阵（CLAUDE.md 强制要求）
│   └── test_security_matrix.py  # 双库矩阵/注入载荷/EXPLAIN 绕过/列掩码绕过
├── integration/                 # 真实 PostgreSQL（fixtures/ 三库）
│   ├── test_multi_database_routing.py  # 多库路由+每库安全（stub LLM，无需 key）
│   └── test_full_flow.py               # 真实 LLM 全链路（需 key）
└── e2e/
    └── test_mcp.py              # MCP 工具契约（信封/错误码）
```

### 运行

```bash
# 默认（排除 integration 标记，含覆盖率门禁 80%）
uv run pytest

# 只跑安全测试
uv run pytest tests/security -v

# 集成测试（需要 fixtures 三库；PG* 变量指向实例；无 OPENAI_API_KEY 时 LLM 用例自动 skip）
PGHOST=localhost PGPORT=5432 PGUSER=postgres PGPASSWORD=postgres \
  uv run pytest -m integration --no-cov

# 覆盖率报告
uv run pytest --cov=pg_mcp --cov-report=html
```

### 覆盖率要求

- **总体 ≥ 80%**（pyproject `--cov-fail-under=80`，门禁强制）
- **核心安全模块 ≥ 95%**（sql_validator 分支需全覆盖；安全相关改动必须先加 tests/security 用例）

### 测试编写要点

- 集成守卫 fixture 模式：环境不可达 → `pytest.skip` 而非失败（见 tests/conftest.py）
- pydantic-settings 校验边界（如 `retry_delay >= 0.1`）需绕开时用 `SimpleNamespace` stub
- 异步 mock 计数断言前 `await asyncio.sleep(0)` 让 create_task 中的递减落地
- LLM 依赖测试用 stub generator（marker → 固定 SQL，见 test_multi_database_routing.py）

## 常用命令

```bash
uv sync                       # 安装依赖
uv run python main.py         # 运行服务（stdio MCP）
uv run pytest                 # 测试门禁
uv run mypy src               # 类型检查
uv run ruff check .           # Lint

# 建测试库（fixtures/ 三个规模的库）
cd fixtures && make create-all

# 指标验证（服务运行时）
curl -s localhost:9090/metrics | grep -E 'pg_mcp_query_requests_total|pg_mcp_llm_calls_total|pg_mcp_sql_rejected_total'

# 脚本化 MCP 冒烟（stdio 客户端跑冒烟清单并抓取 /metrics）
uv run python scripts/mcp_smoke.py
```

## Git 提交规范

```text
feat: 新功能
fix: Bug 修复
docs: 文档更新
refactor: 重构（不改变功能）
test: 测试相关
perf: 性能优化
security: 安全相关修复
```

## 提交前检查清单

- [ ] `uv run pytest` 全绿（含覆盖率 ≥ 80%）
- [ ] `uv run mypy src` 零问题
- [ ] `uv run ruff check .` 通过
- [ ] 安全测试覆盖新增代码路径（tests/security/）
- [ ] 敏感信息未出现在日志/指标/测试输出
- [ ] 文档已更新（如适用）
