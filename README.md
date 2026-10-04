# PostgreSQL MCP 服务器

一个生产级的 [Model Context Protocol (MCP)](https://modelcontextprotocol.io) 服务器，使用户能够通过自然语言与 PostgreSQL 数据库进行交互。服务器将自然语言问题转换为安全的 SQL 查询，经多层安全校验后在**指定数据库**上执行并返回结果。支持多数据库路由与每库独立安全策略。一些参考文档：

- Python Postgres MCP 需求研究
: <https://gemini.google.com/share/c87a73f0969b>
- SQLGlot 深度研究方案
: <https://gemini.google.com/share/cc5e45c76c8f>

## 功能特性

- **自然语言转 SQL**：使用 OpenAI 模型（默认 `gpt-4o-mini`）将自然语言问题转换为优化的 PostgreSQL 查询
- **多数据库路由**：一份 `databases.json` 配置多个数据库，请求按库名路由到对应的连接池与安全策略
- **每库安全策略**：`blocked_tables` / `blocked_columns` / `allow_explain` 支持全局默认 + 每库覆盖
- **安全至上**：单条 SELECT 白名单、危险函数黑名单、SQL 注入防护、EXPLAIN ANALYZE 拦截、只读事务、查询超时与行数上限
- **弹性**：瞬时故障指数退避重试（LLM 与数据库两侧）、LLM 熔断器、并发限制（查询与 LLM 调用分别限流）
- **可观测**：Prometheus 指标、结构化日志、`request_id` 全链路贯穿、token 用量计入响应
- **Schema 智能化**：自动 Schema 缓存，基于 TTL 的刷新机制与 LRU 逐出
- **MCP 兼容**：支持 Claude Desktop 和任何 MCP 兼容客户端

## 快速开始

### 前置条件

- Python 3.14+
- PostgreSQL 12+
- OpenAI API 密钥
- UV 包管理器（推荐）或 pip

开发环境搭建（WSL Ubuntu 原生 PostgreSQL）完整教程见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

### 安装

```bash
# 克隆仓库
git clone <repository-url>
cd pg-mcp

# 安装依赖
uv sync

# 复制环境配置模板
cp .env.example .env

# 编辑 .env 并配置参数（API 密钥、数据库连接）
vi .env
```

### 配置

#### 单数据库模式（.env）

```bash
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=your_database
DATABASE_USER=your_user
DATABASE_PASSWORD=your_password

OPENAI_API_KEY=sk-your-api-key-here
```

#### 多数据库模式（databases.json，推荐）

```bash
cp databases.json.example databases.json
vi databases.json          # 每库连接 + 安全策略
```

在 `.env` 中启用：

```bash
DATABASES_FILE=databases.json
```

`databases.json` 示例（节选，完整见 `databases.json.example`）：

```json
{
  "default_database": "blog_small",
  "databases": [
    {
      "name": "blog_small",
      "database": "blog_small",
      "host": "localhost", "port": 5432,
      "user": "postgres", "password": "postgres",
      "pool": { "min_size": 2, "max_size": 10 },
      "security": {
        "blocked_tables": ["user_sessions"],
        "blocked_columns": ["users.email"],
        "allow_explain": false
      }
    },
    {
      "name": "saas_crm_large",
      "database": "saas_crm_large",
      "security": { "allow_explain": true }
    }
  ]
}
```

- `name`：逻辑名（请求中的 `database` 参数），`database`：物理库名（缺省取 `name`）
- `security.*` 省略或为 null 时继承 `.env` 的 `SECURITY_*` 全局默认
- 文件含密码：`databases.json` 已被 `.gitignore` 忽略，切勿提交

### 运行服务器

```bash
# 使用 UV
uv run python main.py

# 或使用 pip 安装后
python main.py
```

#### 与 Claude Desktop 集成

添加以下配置到 Claude Desktop MCP 设置文件：

**macOS/Linux**: `~/Library/Application Support/Claude/claude_desktop_config.json`

**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASES_FILE": "/absolute/path/to/pg-mcp/databases.json",
        "OPENAI_API_KEY": "sk-your-api-key-here"
      }
    }
  }
}
```

单库模式将 `DATABASES_FILE` 换成 `DATABASE_HOST/NAME/USER/PASSWORD` 等变量即可。

## 使用方法

### query 工具参数

| 参数             | 说明                                                           |
| ---------------- | ------------------------------------------------------------ |
| `question`       | 自然语言问题                                                   |
| `database`       | 目标数据库逻辑名（配置了 `default_database` 或仅一库时可省略） |
| `return_type`    | `result`（默认，执行查询）或 `sql`（只生成不执行）             |

### 示例查询

#### 简单查询

```text
How many tables are in the database?
→ SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'

Show me all users
→ SELECT * FROM users LIMIT 10000

What are the column names in the products table?
→ SELECT column_name, data_type FROM information_schema.columns
  WHERE table_name = 'products'
```

#### 多数据库查询

```text
问：How many posts does each author have?（database: blog_small）
→ 在 blog_small 库执行，路由到其连接池

问：What is the total revenue this month?（database: ecommerce_medium）
→ 在 ecommerce_medium 库执行，路由到其连接池

问：（不指定 database）
→ 使用 default_database；未配置默认且多库时返回 database_error 并列出可用库

问：查询 user_sessions 表（database: blog_small，该表已配置 blocked_tables）
→ security_violation，查询不会到达数据库
```

### 响应格式

#### 成功查询响应

```json
{
  "success": true,
  "generated_sql": "SELECT COUNT(*) FROM users",
  "data": {
    "columns": ["count"],
    "rows": [{"count": 1523}],
    "row_count": 1,
    "execution_time_ms": 23.4
  },
  "confidence": 95,
  "tokens_used": 234,
  "request_id": "f0c1a2b3-4d5e-6f7a-8b9c-0d1e2f3a4b5c"
}
```

#### 错误响应

```json
{
  "success": false,
  "error": {
    "code": "security_violation",
    "message": "Access to table 'user_sessions' is not allowed",
    "details": { "blocked_table": "user_sessions" }
  },
  "confidence": 0,
  "tokens_used": 180
}
```

错误码统一为小写字符串：`database_error`、`security_violation`、`sql_parse_error`、`llm_error`、`invalid_request`、`invalid_parameter`、`server_not_initialized`、`rate_limit_exceeded`、`execution_timeout`、`schema_load_error`、`internal_error`。`request_id` 可用于在日志中检索同一次请求的全部事件。

## 架构

### 核心组件

```text
┌─────────────────────────────────────────────────────────────┐
│                      MCP Server (FastMCP)                   │
│        lifespan: 逐库装配 pool / validator / executor       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Query Orchestrator                       │
│  - 按库名路由（sql_validators / sql_executors 字典）         │
│  - 退避重试（LLM 瞬时故障 + 数据库瞬时 sqlstate）            │
│  - 指标埋点 / request_id 贯穿 / token 记账                   │
└─────────────────────────────────────────────────────────────┘
           │                  │                  │
           ▼                  ▼                  ▼
    ┌───────────┐     ┌────────────┐     ┌──────────────────┐
    │   SQL     │     │    SQL     │     │     SQL          │
    │ Generator │────▶│ Validator  │────▶│  Executor        │
    │ (LLM)     │     │ (每库策略)  │     │ (每库连接池)      │
    └───────────┘     └────────────┘     └──────────────────┘
           │                                      │
           ▼                                      ▼
    ┌───────────┐                          ┌──────────────┐
    │  Schema   │                          │   Result     │
    │  Cache    │                          │  Validator   │
    │ (每库)    │                          │  (LLM, 可关) │
    └───────────┘                          └──────────────┘
```

### 安全特性

1. **只读强制执行**：仅允许单条 SELECT（含 CTE/UNION/子查询安全审查），写操作与多语句一律拒绝
2. **每库表/列黑名单**：`blocked_tables` 含别名场景；`blocked_columns` 拦截裸列名、表别名（`u.email`）、`SELECT *` 与 `table.*` 投影（fail-closed）
3. **EXPLAIN 策略**：默认拒绝；`allow_explain` 开启后仍拒绝 `ANALYZE` 变体（会真实执行内层语句），内层语句完整递归校验
4. **危险函数黑名单**：pg_sleep、文件 I/O、大对象操作等
5. **资源限制**：行数上限下推为 SQL 包装（`LIMIT max_rows + 1`，截断可检测）、语句超时、连接池上限
6. **事务隔离**：所有查询在只读事务中运行

### 弹性特性

- **熔断器**：LLM 连续失败后快速失败，超时后半开恢复
- **并发限制**：查询与 LLM 调用各自独立的信号量并发上限，等待超时返回 `rate_limit_exceeded`
- **重试逻辑**：LLM 瞬时故障（超时/不可用）与数据库瞬时错误（连接类/序列化冲突/死锁等 sqlstate）指数退避重试；校验失败带错误反馈重试
- **连接池**：每库独立 asyncpg 连接池
- **Schema 缓存**：TTL + LRU，支持后台自动刷新

## 配置参考

### 多数据库设置

| 变量                 | 描述                                             | 默认值   |
| -------------------- | ----------------------------------------------- | -------- |
| `DATABASES_FILE`     | databases.json 路径；未设置时回退单库 DATABASE_* | 无       |

### 数据库设置（单库模式）

| 变量                         | 描述              | 默认值        |
| ---------------------------- | ----------------- | ------------- |
| `DATABASE_HOST`              | PostgreSQL 主机   | `localhost`   |
| `DATABASE_PORT`              | PostgreSQL 端口   | `5432`        |
| `DATABASE_NAME`              | 数据库名称        | `postgres`    |
| `DATABASE_USER`              | 数据库用户        | `postgres`    |
| `DATABASE_PASSWORD`          | 数据库密码        | 空            |
| `DATABASE_MIN_POOL_SIZE`     | 池中最小连接数    | `5`           |
| `DATABASE_MAX_POOL_SIZE`     | 池中最大连接数    | `20`          |
| `DATABASE_COMMAND_TIMEOUT`   | 查询超时（秒）    | `30`          |

### OpenAI 设置

| 变量                   | 描述                      | 默认值           |
| `OPENAI_API_KEY`     | OpenAI API 密钥         | 必需            |
|----------------------|-------------------------|-----------------|
| `OPENAI_BASE_URL`    | OpenAI 兼容网关地址     | 无（官方端点）  |
| `OPENAI_MODEL`       | 使用的模型              | `gpt-4o-mini`   |
| `OPENAI_MAX_TOKENS`  | 每次请求的最大 token 数 | `2000`（≤4096） |
| `OPENAI_TEMPERATURE` | 模型温度                | `0.0`           |
| `OPENAI_TIMEOUT`     | API 超时（秒）          | `30`            |

> **OpenAI 兼容网关**：设置 `OPENAI_BASE_URL` 后请求改发该端点，API key 不再要求 `sk-` 前缀（适配智谱等自有 key 格式的供应商），模型名也随之换成网关提供的（如 `glm-4.6`）。

### 安全设置（全局默认，可被每库 security 覆盖）

| 变量                          | 描述                                  | 默认值              |
| ----------------------------- | ------------------------------------- | ------------------- |
| `SECURITY_BLOCKED_FUNCTIONS`  | 逗号分隔的函数黑名单                  | 参考 .env.example   |
| `SECURITY_BLOCKED_TABLES`     | 逗号分隔的表黑名单                    | 空                  |
| `SECURITY_BLOCKED_COLUMNS`    | 逗号分隔的列黑名单（table.column）    | 空                  |
| `SECURITY_ALLOW_EXPLAIN`      | 是否允许 EXPLAIN                      | `false`             |
| `SECURITY_MAX_ROWS`           | 每个查询的最大行数                    | `10000`             |
| `SECURITY_MAX_EXECUTION_TIME` | 查询超时（秒）                        | `30`                |
| `SECURITY_READONLY_ROLE`      | 执行前切换的只读角色                  | 无                  |

### 缓存设置

| 变量                 | 描述                  | 默认值   |
| -------------------- | --------------------- | -------- |
| `CACHE_ENABLED`      | 启用 Schema 缓存      | `true`   |
| `CACHE_SCHEMA_TTL`   | Schema 缓存 TTL（秒） | `3600`   |
| `CACHE_MAX_SIZE`     | 最大缓存 Schema 数    | `100`    |

### 弹性设置

| 变量                                     | 描述                              | 默认值   |
| ---------------------------------------- | --------------------------------- | -------- |
| `RESILIENCE_MAX_RETRIES`                 | 最大重试次数                      | `3`      |
| `RESILIENCE_RETRY_DELAY`                 | 初始重试延迟（秒）                | `1.0`    |
| `RESILIENCE_BACKOFF_FACTOR`              | 指数退避倍数                      | `2.0`    |
| `RESILIENCE_CIRCUIT_BREAKER_THRESHOLD`   | 熔断前的失败数                    | `5`      |
| `RESILIENCE_CIRCUIT_BREAKER_TIMEOUT`     | 熔断器超时（秒）                  | `60`     |
| `RESILIENCE_QUERY_CONCURRENCY`           | 最大并发查询数（信号量）          | `10`     |
| `RESILIENCE_LLM_CONCURRENCY`             | 最大并发 LLM 调用数（信号量）     | `5`      |
| `RESILIENCE_RATE_LIMIT_TIMEOUT`          | 并发槽等待超时（秒）              | `30`     |

### 可观测性设置

| 变量                              | 描述                   | 默认值   |
| --------------------------------- | ---------------------- | -------- |
| `OBSERVABILITY_METRICS_ENABLED`   | 启用 Prometheus 指标   | `true`   |
| `OBSERVABILITY_METRICS_PORT`      | 指标 HTTP 端口         | `9090`   |
| `OBSERVABILITY_LOG_LEVEL`         | 日志级别               | `INFO`   |
| `OBSERVABILITY_LOG_FORMAT`        | 日志格式（json/text）  | `json`   |

## 开发

### 设置开发环境

```bash
# 安装开发依赖
uv sync --all-extras
```

完整开发环境教程（WSL Ubuntu 原生 PostgreSQL、fixture 数据库、MCP Inspector 冒烟）见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

### 运行测试

```bash
# 默认（排除 integration 标记，含覆盖率门禁 80%）
uv run pytest

# 安全测试套件
uv run pytest tests/security -v

# 集成测试（需要 fixtures 数据库，见 docs/DEVELOPMENT.md；环境缺失时自动 skip）
uv run pytest -m integration --no-cov

# 覆盖率报告
uv run pytest --cov=pg_mcp --cov-report=html
```

### 代码质量

```bash
uv run mypy src
uv run ruff check --fix .
uv run ruff format .
```

### 项目结构

```text
pg-mcp/
├── src/pg_mcp/
│   ├── cache/              # Schema 缓存
│   ├── config/             # 配置管理（settings + databases.json）
│   ├── db/                 # 连接池与内省
│   ├── models/             # 数据模型
│   ├── observability/      # 日志、指标、追踪
│   ├── prompts/            # LLM Prompt 模板
│   ├── resilience/         # 熔断器、并发限制器
│   ├── services/           # 核心业务逻辑
│   │   ├── orchestrator.py      # 查询编排（路由/重试/指标）
│   │   ├── sql_generator.py     # 基于 LLM 的 SQL 生成
│   │   ├── sql_validator.py     # 安全验证
│   │   ├── sql_executor.py      # 查询执行
│   │   └── result_validator.py  # 结果验证
│   └── server.py           # FastMCP 服务器
├── tests/
│   ├── unit/               # 单元测试
│   ├── security/           # 安全测试矩阵
│   ├── integration/        # 集成测试（真实数据库）
│   └── e2e/                # 端到端测试
├── fixtures/               # 测试数据库 fixture（三个规模的库）
├── docs/                   # 开发文档
├── specs/                  # 设计文档与增强报告
├── databases.json.example  # 多库配置模板
├── .env.example            # 环境模板
└── pyproject.toml          # 项目配置
```

## Docker 部署

### 构建镜像

```bash
docker build -t pg-mcp:latest .
```

### 运行容器

```bash
docker run -d \
  --name pg-mcp \
  -e DATABASE_HOST=your-db-host \
  -e DATABASE_NAME=your-db \
  -e DATABASE_USER=your-user \
  -e DATABASE_PASSWORD=your-password \
  -e OPENAI_API_KEY=sk-your-key \
  -p 9090:9090 \
  pg-mcp:latest
```

多库模式挂载配置文件：`-v $(pwd)/databases.json:/app/databases.json -e DATABASES_FILE=/app/databases.json`。

### Docker Compose

```bash
docker-compose up -d
docker-compose logs -f pg-mcp
docker-compose down
```

详细配置参考 `docker-compose.yml`。

## 监控

### 指标

服务器在端口 9090（可配置）上暴露 Prometheus 指标：

```bash
curl -s http://localhost:9090/metrics | \
  grep -E 'pg_mcp_query_requests_total|pg_mcp_llm_calls_total|pg_mcp_sql_rejected_total'
```

**可用指标：**

- `pg_mcp_query_requests_total{status, database}` - 查询请求数（按状态与数据库）
- `pg_mcp_query_duration_seconds` - 查询全链路耗时直方图
- `pg_mcp_llm_calls_total{operation}` - LLM 调用数
- `pg_mcp_llm_latency_seconds{operation}` - LLM 调用耗时直方图
- `pg_mcp_llm_tokens_used{operation}` - LLM token 用量
- `pg_mcp_sql_rejected_total{reason}` - 安全拦截数（按原因）
- `pg_mcp_db_connections_active{database}` - 活跃连接数
- `pg_mcp_db_query_duration_seconds` - 数据库执行耗时直方图
- `pg_mcp_schema_cache_age_seconds{database}` - Schema 缓存年龄

### 日志

结构化 JSON 日志（或文本格式）输出到标准输出，每条带 `request_id`：

```json
{
  "timestamp": "2026-10-04T10:30:00.123Z",
  "level": "INFO",
  "message": "SQL executed successfully",
  "request_id": "f0c1a2b3-4d5e-6f7a-8b9c-0d1e2f3a4b5c",
  "database": "blog_small",
  "row_count": 42,
  "execution_time_ms": 23.4
}
```

## 故障排查

### 常见问题

#### 连接被拒绝

```
Error: Connection to database failed
```

**解决方案**：验证 PostgreSQL 正在运行且凭证正确：

```bash
psql -h $DATABASE_HOST -U $DATABASE_USER -d $DATABASE_NAME
```

#### database_error 且 details 含 available_databases

请求的 `database` 逻辑名不在配置中，或未指定且没有默认库。按 `available_databases` 列表中的名字重试。

#### OpenAI API 错误

1. 检查 API 密钥是否有效且有额度
2. 验证网络连接
3. 如果请求超时，检查 `OPENAI_TIMEOUT` 设置；连续失败触发熔断时等待 `RESILIENCE_CIRCUIT_BREAKER_TIMEOUT` 后重试

#### rate_limit_exceeded

并发超过 `RESILIENCE_QUERY_CONCURRENCY` / `RESILIENCE_LLM_CONCURRENCY` 且等待超过 `RESILIENCE_RATE_LIMIT_TIMEOUT`。降低客户端并发或调大上限。

#### 查询超时

1. 增加 `SECURITY_MAX_EXECUTION_TIME`
2. 优化数据库（添加索引、VACUUM）
3. 简化查询或添加过滤条件

### 调试模式

```bash
export OBSERVABILITY_LOG_LEVEL=DEBUG
uv run python main.py
```

## 安全考虑

### 生产环境部署

1. **使用只读数据库用户**：创建专用 PostgreSQL 用户，仅具有 SELECT 权限：

```sql
CREATE USER pg_mcp_readonly WITH PASSWORD 'secure-password';
GRANT CONNECT ON DATABASE your_database TO pg_mcp_readonly;
GRANT USAGE ON SCHEMA public TO pg_mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pg_mcp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO pg_mcp_readonly;
```

（或在 `.env` 中配置 `SECURITY_READONLY_ROLE=pg_mcp_readonly`，执行前自动切换角色。）

2. **保护 API 密钥**：使用环境变量或秘密管理系统，切勿提交到版本控制；`databases.json` 含密码，已被 `.gitignore` 忽略

3. **网络隔离**：在隔离网络中运行服务器，通过 IP 限制数据库访问

4. **监控使用**：启用指标并为异常模式设置告警（关注 `pg_mcp_sql_rejected_total` 突增）

5. **最小化数据暴露**：为承载敏感列的库配置 `SECURITY_BLOCKED_COLUMNS` 或每库 `blocked_columns`

## 许可证

[您的许可证信息]

## 贡献

欢迎贡献！请参阅 CONTRIBUTING.md 了解指南。

## 支持

如有问题和疑问：

- GitHub Issues：[repository-url]/issues
- 文档：`docs/DEVELOPMENT.md` 与 `specs/` 目录（设计文档与增强报告）

## 致谢

- 基于 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)（FastMCP）构建
- SQL 解析由 [sqlglot](https://github.com/tobymao/sqlglot) 提供
- 数据库驱动：[asyncpg](https://github.com/MagicStack/asyncpg)
