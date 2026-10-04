# pg-mcp 开发环境搭建指南（WSL Ubuntu + 原生 PostgreSQL）

本文档描述在 Windows 上通过 WSL2 + Ubuntu 24.04 搭建 pg-mcp 完整开发环境的步骤，覆盖
Python 3.14、uv、PostgreSQL 16（apt 原生安装，非 Docker）、三个 fixture 测试库、多库配置、
服务运行与分层测试、指标与日志验证。

适用读者：需要在 Windows 环境下开发、调试、测试 pg-mcp 的工程师。

环境总览：

| 组件 | 版本 / 选型 | 说明 |
| --- | --- | --- |
| 宿主系统 | Windows 10（22H2+）/ Windows 11 | 需支持 WSL2 |
| Linux 子系统 | Ubuntu 24.04（WSL2） | 开发主环境 |
| Python | 3.14（uv 托管） | `pyproject.toml` 要求 `>=3.14` |
| 包管理 | uv | 依赖同步、虚拟环境、Python 安装 |
| 数据库 | PostgreSQL 16（apt 原生安装） | 非 Docker；承载 3 个 fixture 库 |
| LLM | OpenAI API（`sk-` 开头密钥） | SQL 生成与结果验证 |
| 测试 | pytest（unit / security / integration / e2e） | 覆盖率门禁 80% |

目录：

1. 前置条件
2. WSL Ubuntu 基础工具
3. 安装 uv
4. 安装 Python 3.14
5. 获取代码与依赖
6. 安装配置 PostgreSQL 16
7. 创建测试数据库
8. 配置 pg-mcp
9. 运行服务
10. 运行测试
11. 指标与日志验证
12. 故障排查
- 附录 A：常用命令速查
- 附录 B：参考文件

---

## 1. 前置条件

### 1.1 硬件与系统要求

- Windows 10（建议 22H2 / 19045 及以上）或 Windows 11，已启用 WSL2
- 至少 8 GB 内存（WSL2 默认可用宿主一半内存，可在 Windows 侧 `%UserProfile%\.wslconfig`
  中用 `[wsl2]` `memory=4GB` 调整）
- 约 10 GB 可用磁盘空间（Ubuntu + PostgreSQL + Python 3.14 + 依赖）
- 可访问外网（apt / astral.sh / PyPI / OpenAI API）

### 1.2 安装 WSL2 与 Ubuntu 24.04

以管理员身份打开 PowerShell，执行：

```powershell
wsl --install -d Ubuntu-24.04
```

若系统首次启用 WSL，安装完成后需重启 Windows。重启后 Ubuntu 会自动启动，并要求创建
Linux 用户（用户名建议全小写，例如 `dev`）与密码——后文所有 `sudo` 都使用该密码。

验证安装结果：

```powershell
wsl -l -v
```

预期输出（`VERSION` 列为 `2`）：

```text
  NAME            STATE           VERSION
* Ubuntu-24.04    Running         2
```

> 注意：请记住发行版名称。本文所有 `wsl.exe -d Ubuntu-24.04 ...` 均以 `Ubuntu-24.04`
> 为例；如果你的 `wsl -l -v` 显示为 `Ubuntu`，请相应替换。

### 1.3 VS Code WSL 扩展（可选，推荐）

在 Windows 侧 VS Code 中安装 **WSL**（ms-vscode-remote.remote-wsl）扩展后，在 WSL 终端中：

```bash
cd ~/workspace/pg-mcp && code .
```

VS Code 会以 Remote-WSL 模式打开仓库，语言服务、终端、调试器全部运行在 WSL 内，
避免跨文件系统访问 `/mnt/e` 的性能损耗（见第 12 节故障排查第 7 条）。

---

## 2. WSL Ubuntu 基础工具

进入 WSL（开始菜单打开 "Ubuntu 24.04 LTS"，或在 PowerShell 执行 `wsl -d Ubuntu-24.04`），
首先更新索引并安装基础工具链：

```bash
sudo apt update && sudo apt install -y build-essential libpq-dev make git curl
```

预期输出：`apt` 依次安装 gcc/g++/make（build-essential）、PostgreSQL 客户端开发头文件
（libpq-dev）、make、git、curl，最后无报错返回 shell。

为什么必须装 `build-essential` 和 `libpq-dev`：

- 本项目要求 Python 3.14（`requires-python = ">=3.14"`），依赖 `asyncpg>=0.31.0`。
  **若 PyPI 上尚无 cp314 预编译 wheel，`uv sync` 会从源码编译 asyncpg 的 C 扩展**，
  编译需要 `build-essential` 提供的 gcc / make；缺工具链时报错形如
  `error: command 'gcc' failed` 或找不到 `Python.h`。
- `libpq-dev` 提供 PostgreSQL 客户端开发头文件，兼容需要链接 libpq 的驱动与工具，
  避免后续更换驱动（如 psycopg）时再次补装。
- `make` 供第 7 节 `fixtures/Makefile` 建库使用。

验证：

```bash
gcc --version && make --version | head -1 && git --version && curl --version | head -1
```

---

## 3. 安装 uv

uv 是本项目的包管理与 Python 版本管理工具：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

预期输出：安装脚本下载并安装 uv 到 `~/.local/bin`，末尾提示
`close and reopen your terminal`（或手动 source 配置文件）。

使 PATH 生效（安装脚本会自动向 `~/.bashrc` 追加 PATH，当前会话需手动加载）：

```bash
source ~/.bashrc     # 或 source ~/.profile，或直接关掉终端重开
uv --version
```

预期输出形如：`uv 0.9.x (home ... )`。

> 提示：uv 的自更新命令为 `uv self update`；若 `uv: command not found`，执行
> `export PATH="$HOME/.local/bin:$PATH"` 后重试，并确认该行已写入 `~/.bashrc`。

---

## 4. 安装 Python 3.14

```bash
uv python install 3.14
```

预期输出：下载 uv 托管的 CPython 3.14.x 并安装到 uv 的 Python 目录，无需系统权限。

验证：

```bash
uv python list --only-installed
```

预期输出包含 `- cpython-3.14.x-linux-gnu-x86_64-none`。

> 说明：无需 `sudo apt install python3`——uv 托管的 Python 与系统 Python 互不影响，
> 且自带开发头文件，供第 2 节工具链编译 C 扩展使用。

---

## 5. 获取代码与依赖

### 5.1 克隆仓库（推荐：WSL 原生文件系统）

```bash
mkdir -p ~/workspace
git clone https://github.com/aengusjiang/pg-mcp.git ~/workspace/pg-mcp
cd ~/workspace/pg-mcp
```

预期输出：`Cloning into '/home/<user>/workspace/pg-mcp'...`，随后检出默认分支。

若仓库已经存在于 Windows 侧（例如 `E:\workspace\pg-mcp`），也可以直接访问：

```bash
cd /mnt/e/workspace/pg-mcp
```

**性能差异（重要）**：`/mnt/e/...` 走 9P 协议跨 Windows/Linux 文件系统，`git status`、
`uv sync`、`pytest` 等大量小文件 I/O 操作明显偏慢（可达数倍到数十倍）。**推荐在 WSL
原生文件系统（如 `~/workspace`）克隆一份用于开发与测试**；Windows 侧可通过
`\\wsl$\Ubuntu-24.04\home\<user>\workspace\pg-mcp` 或 VS Code Remote-WSL 访问同一目录。

### 5.2 安装依赖

```bash
cd ~/workspace/pg-mcp
uv sync
```

预期输出：

```text
Resolved XX packages in ...
Installed XX packages in ...
```

`uv sync` 会按 `pyproject.toml` / `uv.lock` 在项目根创建 `.venv`（虚拟环境），并自动
选用满足 `requires-python >= 3.14` 的解释器（即第 4 节安装的 3.14）。

验证解释器与依赖：

```bash
uv run python -V          # 预期：Python 3.14.x
uv run python -c "import asyncpg, sqlglot, pydantic; print('deps ok')"
```

> 若此步出现 asyncpg 编译错误，回到第 2 节确认 `build-essential` 已安装，然后
> `uv cache clean asyncpg && uv sync` 重试；仍失败见第 12 节第 5 条。

---

## 6. 安装配置 PostgreSQL 16（原生 apt，非 Docker）

### 6.1 安装

```bash
sudo apt install -y postgresql postgresql-contrib
```

Ubuntu 24.04 的 apt 源提供 PostgreSQL 16。预期输出：安装 `postgresql-16` 等包，安装
过程自动创建 `16/main` 集群并尝试启动服务。

### 6.2 服务管理

Ubuntu on WSL 默认可能未启用 systemd，`service` 命令在两种情况下均可用：

```bash
sudo service postgresql status    # 预期：16/main (port 5432): online
sudo service postgresql start     # 启动
sudo service postgresql restart   # 重启（改配置后）
sudo service postgresql stop
```

备选方案一（`service` 不可用时，直接操作集群）：

```bash
pg_lsclusters                        # 查看所有集群及状态
sudo pg_ctlcluster 16 main start     # 启动 16/main
```

备选方案二（WSL 已启用 systemd 时）：

```bash
sudo systemctl enable --now postgresql
sudo systemctl status postgresql
```

> **WSL 特有注意**：WSL 发行版会随 Windows 关机/`wsl --shutdown` 而停止，PostgreSQL
> 不会像真机那样常驻。每次重新进入 WSL 后，先执行 `sudo service postgresql status`
> 确认状态，未启动则 `sudo service postgresql start`。可选：在 `~/.profile` 追加
> `sudo service postgresql status >/dev/null 2>&1 || sudo service postgresql start`。

验证监听：

```bash
pg_isready -h localhost -p 5432
```

预期输出：`/tmp:5432 - accepting connections`（或 `localhost:5432 - accepting connections`）。

### 6.3 认证方式说明（peer vs md5/scram）

Ubuntu 默认的 `pg_hba.conf`（位于 `/etc/postgresql/16/main/pg_hba.conf`）关键规则：

| 连接方式 | 默认认证 | 含义 |
| --- | --- | --- |
| `local`（Unix domain socket） | `peer` | 以 OS 用户名映射 PG 用户名，**无需密码**（`sudo -u postgres psql` 即利用此机制） |
| `host 127.0.0.1/32`、`host ::1/128` | `scram-sha-256` | TCP 连接需要**密码**认证 |

pg-mcp 通过 asyncpg 走 **TCP（host=localhost）**，命中 `scram-sha-256` 行，因此必须先给
`postgres` 用户设置密码（旧文档中的 `md5` 是历史默认，PG 14+ 默认升级为 scram-sha-256，
两者对使用者无差别，都是"用户名+密码"）。

设置密码（开发环境统一用 `postgres`）：

```bash
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'postgres';"
```

预期输出：`ALTER ROLE`。

验证 TCP 密码认证可用：

```bash
psql -h localhost -U postgres -c "SELECT version();"
```

按提示输入密码 `postgres` 后，应返回 `PostgreSQL 16.x ...` 一行。

### 6.4 （可选）允许远程连接

默认只监听 `localhost`，本开发流程无需修改。如需从 Windows 其他机器或局域网访问：

```bash
# 1) 监听所有地址
sudo sed -i "s/^#*listen_addresses.*/listen_addresses = '*'/" \
  /etc/postgresql/16/main/postgresql.conf

# 2) 允许局域网密码认证（按需收紧网段）
echo "host all all 0.0.0.0/0 scram-sha-256" | \
  sudo tee -a /etc/postgresql/16/main/pg_hba.conf

# 3) 重启生效
sudo service postgresql restart
```

> WSL2 默认把 WSL 内监听的 localhost 端口转发给 Windows（Windows 侧可直接连
> `localhost:5432`）。若使用镜像网络模式或需从其他物理机访问，还要在 Windows 侧配置
> `netsh interface portproxy` 或防火墙放行，此处不展开。

### 6.5 备选方案：无 sudo 权限时使用用户级 PostgreSQL cluster

如果当前 WSL 用户没有 sudo 权限（无法安装/管理系统级 PostgreSQL），可以用 `initdb`
在自己的主目录初始化一个**独立 cluster**，以当前用户为超级用户、监听非默认端口，
完全不需要 root：

```bash
# PostgreSQL 服务器工具（initdb/pg_ctl 已随 postgresql 客户端或服务器包提供）
export PATH=/usr/lib/postgresql/16/bin:$PATH

# 初始化用户级 cluster（-U 指定当前用户为该 cluster 的超级用户）
initdb -D ~/.pgtest-mcp -U $USER --auth=trust

# 启动（避开系统 5432 端口；socket 放 /tmp 避开 /var/run/postgresql 权限）
pg_ctl -D ~/.pgtest-mcp \
  -o '-p 5433 -c listen_addresses=localhost -c unix_socket_directories=/tmp' \
  -l ~/.pgtest-mcp.log -w start

# 验证（trust 认证：任意密码均可，用户名必须匹配 -U 的值）
psql -h localhost -p 5433 -U $USER -d postgres -c 'SELECT version();'
```

后续所有命令把连接参数换成该实例即可：

```bash
export PGHOST=localhost PGPORT=5433 PGUSER=$USER PGPASSWORD=any
cd ~/workspace/pg-mcp/fixtures && make create-all   # 建三个测试库
```

停止与删除：

```bash
pg_ctl -D ~/.pgtest-mcp stop
rm -rf ~/.pgtest-mcp ~/.pgtest-mcp.log
```

> 注：trust 认证意味着本机任何进程都可免密连接，仅限本地开发测试使用，不要暴露到
> 网络（默认只监听 localhost）。

---

## 7. 创建测试数据库

fixtures 目录提供三个规模的测试库（详见 `fixtures/README.md`）：

| 库 | 规模 | 表 | 视图 | 说明 |
| --- | --- | --- | --- | --- |
| `blog_small` | 小 | 7 | 3 | 博客：用户、文章、评论、标签 |
| `ecommerce_medium` | 中 | 25 | 6 | 电商：商品、订单、支付、库存 |
| `saas_crm_large` | 大 | 55+ | 10 | 多租户 SaaS CRM |

Makefile 通过 `psql -h localhost -U postgres` 走 TCP 连接，因此**需要密码**。先导出
`PGPASSWORD`（避免 make 过程中交互式密码提示卡住），再建库：

```bash
cd ~/workspace/pg-mcp/fixtures
export PGPASSWORD=postgres
make create-all
```

预期输出：

```text
Creating small database (blog_small)...
✓ Small database created
Creating medium database (ecommerce_medium)...
✓ Medium database created
Creating large database (saas_crm_large)...
✓ Large database created
✓ All test databases created successfully!
```

验证：

```bash
make list        # 列出已存在的测试库
make test-all    # 逐库输出 tables / views / enum_types / total_rows 统计
```

`make list` 预期输出三行：`- blog_small`、`- ecommerce_medium`、`- saas_crm_large`；
`make test-all` 中 blog_small 一行预期 `tables=7`、`views=3`、`enum_types=2`。

常见建库失败排查：

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| make 卡住不动 | 未 `export PGPASSWORD`，psql 在后台等待密码输入 | `export PGPASSWORD=postgres` 后重试 |
| `psql: error: connection refused` | PostgreSQL 未启动 | `sudo service postgresql start`（见 6.2） |
| `password authentication failed` | 未执行 6.3 的 `ALTER USER` 设密码 | 重新执行设密码命令 |
| 库已存在（重复执行 create） | SQL 文件含建库语句 | `make rebuild-all`（drop 后重建） |
| make 显示成功但库不存在 | Makefile 对 psql 输出做了 `grep` 过滤且 `\|\| true` 会吞掉部分错误 | 手动执行 `psql -h localhost -U postgres --set ON_ERROR_STOP=on -f 01_small_db.sql` 复现真实错误 |
| 权限不足（permission denied to create database） | 当前 PG 用户无 `CREATEDB` 权限 | `sudo -u postgres psql -c "ALTER USER <user> CREATEDB;"` |

---

## 8. 配置 pg-mcp

### 8.1 创建 .env

```bash
cd ~/workspace/pg-mcp
cp .env.example .env
```

编辑 `.env`（`nano .env`，或 VS Code Remote-WSL 中 `code .env`），**必填项只有一个**：

```bash
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
```

要求与说明：

- `OPENAI_API_KEY` **必须非空**；未设置 `OPENAI_BASE_URL` 时还**必须以 `sk-` 开头**，
  密钥为空或前缀不对，服务启动即失败（pydantic 校验 fail-fast）。OpenAI 官方密钥在
  <https://platform.openai.com/api-keys> 获取。
- **OpenAI 兼容网关（可选）**：设置 `OPENAI_BASE_URL` 指向任何兼容 Chat Completions
  协议的端点后，请求改发该网关，且 API key 不再要求 `sk-` 前缀、模型名换成网关提供的。
  例如智谱 BigModel（与其他 OpenAI 兼容供应商同理）：

  ```bash
  OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
  OPENAI_API_KEY=<智谱 API key>
  OPENAI_MODEL=glm-4.6
  ```

- **嵌套配置节已支持从 `.env` 读取**：`DATABASE_*`、`OPENAI_*`、`SECURITY_*`、
  `VALIDATION_*`、`CACHE_*`、`RESILIENCE_*`、`OBSERVABILITY_*` 均可写入 `.env`。
- 配置优先级：**OS 环境变量 > `.env` > 代码默认值**（例如命令行
  `DATABASE_HOST=x uv run python main.py` 会覆盖 `.env` 中的同名项）。
- `OPENAI_MAX_TOKENS` 上限 **4096**（配置模型约束 `le=4096`）。示例默认 `2000`；
  写成旧文档中的 `32000` 会在启动时直接抛 pydantic 校验错误导致崩溃。

验证密钥有效（可选）：

```bash
source .env && curl -s https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY" | head -c 200
```

预期返回 JSON（含 `"data"` 列表）；返回 `{"error": ...}` 说明密钥无效或无额度。

### 8.2 创建 databases.json（多库模式，推荐）

```bash
cp databases.json.example databases.json
```

按需修改各库 `password` 等字段（本教程环境下 example 默认值即可直接使用）。然后在
`.env` 中启用：

```bash
DATABASES_FILE=databases.json
```

字段参考（完整示例见 `databases.json.example`，加载失败会 fail-fast 并带文件上下文）：

| 字段 | 类型 | 必填 | 缺省行为 | 说明 |
| --- | --- | --- | --- | --- |
| `default_database` | string | 是 | — | 请求未指定 `database` 参数时使用的逻辑库名，必须存在于 `databases` 列表中 |
| `databases[].name` | string | 是 | — | 逻辑库名：连接池 key、请求路由名、指标 label。须匹配 `^[a-zA-Z_][a-zA-Z0-9_-]{0,62}$`，且不可重复 |
| `databases[].database` | string | 否 | 取 `name` | 物理库名（PostgreSQL 中实际存在的 database） |
| `databases[].host` | string | 否 | `localhost` | 同 `DATABASE_HOST` |
| `databases[].port` | int | 否 | `5432` | 同 `DATABASE_PORT` |
| `databases[].user` / `password` | string | 否 | `postgres` / 空 | TCP 密码认证必须设置密码 |
| `databases[].pool.min_size` / `max_size` | int | 否 | `5` / `20` | 连接池大小；fixture 多库场景建议 `min_size=2` 控制启动连接成本 |
| `databases[].pool.timeout` / `command_timeout` | float | 否 | `30.0` | 取连接 / 执行超时（秒） |
| `databases[].security.blocked_tables` | list | 否 | `null` = 继承全局 `SECURITY_BLOCKED_TABLES` | 表黑名单，命中即 `security_violation` |
| `databases[].security.blocked_columns` | list | 否 | `null` = 继承全局 `SECURITY_BLOCKED_COLUMNS` | 列黑名单，元素支持 `table.column` 形式 |
| `databases[].security.allow_explain` | bool | 否 | `null` = 继承全局 `SECURITY_ALLOW_EXPLAIN` | 是否放行 EXPLAIN（ANALYZE 变体默认拒绝；即使放行，内层语句仍走完整校验） |

两种启动形态：

| 形态 | 触发条件 | 连接与安全配置来源 |
| --- | --- | --- |
| 多库模式 | `.env` 或环境变量设置了 `DATABASES_FILE`（且文件存在） | `databases.json`（安全字段缺省继承 `SECURITY_*` 全局默认） |
| 单库模式 | `DATABASES_FILE` 留空 / 未设置 | `.env` 中 `DATABASE_*`（向后兼容） |

> `databases.json` 含数据库密码，已被 `.gitignore` 忽略，只提交 `databases.json.example`。

---

## 9. 运行服务

### 9.1 stdio 模式（默认）

```bash
cd ~/workspace/pg-mcp
uv run python main.py
```

预期行为：进程启动后**没有交互式输出、保持挂起**——这是 stdio 传输的正常表现（MCP
客户端通过 stdin/stdout 通信）。日志（INFO 级别）输出在 stderr。`Ctrl+C` 退出。

若启动即退出，常见原因：`OPENAI_API_KEY` 缺失或非 `sk-` 开头、`OPENAI_MAX_TOKENS`
超过 4096、数据库连接失败（PostgreSQL 未启动）——对照第 12 节排查。

### 9.2 用 MCP inspector 手动测试（推荐）

先安装 Node.js（二选一）：

```bash
# 方式 A：apt（Ubuntu 24.04 提供 Node 18，满足 inspector 需求）
sudo apt install -y nodejs npm

# 方式 B：nvm（需要更新版本时）
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc && nvm install --lts
```

启动 inspector：

```bash
cd ~/workspace/pg-mcp
npx @modelcontextprotocol/inspector uv run python main.py
```

预期输出：inspector 打印本地访问地址（默认 `http://localhost:6274`，以终端实际输出
为准），并拉起 pg-mcp 子进程。浏览器打开该地址，点击 **Connect**。

冒烟清单（Connect 后在 Tools → query 中逐条验证）：

| 操作 | 预期结果 |
| --- | --- |
| `query(question="统计每个用户的文章数", database="blog_small")` | 正确路由到 blog_small，返回结果信封（`success=true`，含 `request_id`） |
| `query(question="每个组织有多少个客户账户", database="saas_crm_large")` | 路由到第二个连接池（saas_crm_large） |
| `query(question="查询 audit_log 全部数据", database="blog_small")` | `security_violation`：被 blog_small 的 `blocked_tables` 拦截 |
| `query(question="...", database="nope")` | `database_error`，错误信息附带可用数据库列表（available） |

除 inspector 手动测试外，仓库提供脚本化冒烟（stdio 客户端自动执行上述清单并抓取
`/metrics`，任一用例不符合预期以非零码退出）：

```bash
uv run python scripts/mcp_smoke.py
```

### 9.3 Claude Desktop（Windows 侧）接入 WSL 服务

Claude Desktop 运行在 Windows，配置文件位于 `%APPDATA%\Claude\claude_desktop_config.json`
（更多说明见 `CLAUDE_DESKTOP_SETUP.md`）。在 `mcpServers` 中加入（命令采用
`wsl.exe -d Ubuntu-24.04` 形式，让 Claude Desktop 进入 WSL 启动服务）：

```json
{
  "mcpServers": {
    "pg-mcp": {
      "command": "wsl.exe",
      "args": [
        "-d", "Ubuntu-24.04",
        "--", "bash", "-lc",
        "cd /home/dev/workspace/pg-mcp && uv run python main.py"
      ]
    }
  }
}
```

要点：

- `-d` 后的发行版名称须与 `wsl -l -v` 显示一致。
- `cd` 的路径是 **WSL 内的绝对路径**（把 `dev` 换成你的用户名）。
- 连接信息推荐放在仓库内 `.env` / `databases.json`（服务从工作目录读取），无需在
  JSON 里散落密码；如确需传递个别环境变量，可研究 `WSLENV` 机制。
- 修改配置后需**完全退出** Claude Desktop（托盘图标 Quit）再重启。
- 排查日志：Windows 侧 `%APPDATA%\Claude\logs\mcp*.log`。
- 先确保 WSL 内 PostgreSQL 已启动（第 6.2 节），否则服务启动时连库失败。

---

## 10. 运行测试

### 10.1 分层测试一览

| 层 | 命令 | 外部依赖 |
| --- | --- | --- |
| 单元 + 安全（离线） | `uv run pytest tests/unit tests/security -q` | 无（不需要 PG 与 API key） |
| 集成 | `PGPASSWORD=postgres uv run pytest -m integration` | PostgreSQL + 三个 fixture 库（LLM 用例另需有效 `OPENAI_API_KEY`） |
| 覆盖率门禁 | `uv run pytest --cov=pg_mcp` | 无（门禁 80%，`--cov-fail-under=80`） |
| Lint / 类型 | `uv run ruff check .`；`uv run mypy src` | 无 |

说明：`pyproject.toml` 的 pytest `addopts` 默认带 `-m 'not integration'` 与覆盖率门禁，
即**直接 `uv run pytest` 只跑离线测试并强制 80% 覆盖率**；集成测试需按上表显式加
`-m integration`（命令行 `-m` 覆盖默认过滤）。若只想看集成结果、不关心当次覆盖率
统计，可追加 `--no-cov`。

### 10.2 离线快速反馈（改代码后最常用）

```bash
cd ~/workspace/pg-mcp
uv run pytest tests/unit tests/security -q
```

预期输出末尾形如：`XXX passed, Y deselected in Zs`（deselected 的是 integration 用例）。

### 10.3 集成测试（需要数据库与 API key）

前置：PostgreSQL 已启动、`make create-all` 已执行、`.env` 中 `OPENAI_API_KEY` 有效。

集成测试的守卫通过 `PG*` 环境变量定位 PostgreSQL 实例（默认
`localhost:5432 / postgres / postgres`；使用 6.5 的用户级 cluster 时导出
`PGPORT=5433 PGUSER=$USER` 等）：

```bash
# 系统级 PostgreSQL（第 6 节安装的实例）
PGPASSWORD=postgres uv run pytest -m integration -v

# 或用户级 cluster（6.5 节）
PGHOST=localhost PGPORT=5433 PGUSER=$USER PGPASSWORD=any \
  uv run pytest -m integration -v
```

说明：

- `tests/integration/test_multi_database_routing.py`（多库路由 + 每库安全）用 stub
  替代 LLM，**只需要数据库、不需要 API key**
- `tests/integration/test_full_flow.py` 与 `tests/e2e/test_mcp.py` 走真实 LLM 全链路，
  两个守卫（数据库可达 + `OPENAI_API_KEY` 有效）任一不满足即 `SKIPPED`
- 多库路由测试内部会生成临时 databases.json（含每库安全规则），不需要手工准备
  `databases.json`

预期输出：集成用例全部 `PASSED`。若出现 `SKIPPED`（skip 守卫触发），说明前置条件
未满足（数据库不可达或 API key 缺失），按第 12 节排查后重跑。

### 10.4 覆盖率与静态检查（提交前门禁）

```bash
uv run pytest --cov=pg_mcp --cov-report=term-missing   # 总覆盖率 >= 80%
uv run ruff check .                                     # lint
uv run mypy src                                         # 类型检查（strict）
```

预期：覆盖率报告末尾 `TOTAL ... 8x%`；ruff `All checks passed!`；mypy
`Success: no issues found in ... source files`。

---

## 11. 指标与日志验证

### 11.1 Prometheus 指标

前置：`.env` 中 `OBSERVABILITY_METRICS_ENABLED=true`（默认即 true）、
`OBSERVABILITY_METRICS_PORT=9090`。

服务运行期间（例如 inspector 会话保持连接时），另开一个 WSL 终端：

```bash
curl -s http://localhost:9090/metrics | \
  grep -E 'pg_mcp_query_requests_total|pg_mcp_llm_calls_total|pg_mcp_sql_rejected_total'
```

预期输出（发起过若干查询后，计数应 **大于 0**）：

```text
pg_mcp_query_requests_total{...} 3.0
pg_mcp_llm_calls_total{...} 4.0
pg_mcp_sql_rejected_total{...} 1.0
```

说明：

- 完整指标列表可通过 `curl -s http://localhost:9090/metrics | grep '^# HELP pg_mcp'` 查看；
- 指标带 `database` 等 label，可区分多库流量；
- 若第 9.2 节冒烟中执行过 blocked_tables 拦截用例，`pg_mcp_sql_rejected_total` 应非零。

### 11.2 request_id 贯穿验证

每次查询会生成唯一 `request_id`，贯穿"生成 → 校验 → 执行 → 响应"全链路日志，并出现在
响应信封中。验证方法：

```bash
# 终端 1：启动服务并把 stderr 日志重定向到文件
cd ~/workspace/pg-mcp
uv run python main.py 2>server.log

# 终端 2（或 inspector）：发起一次查询，从响应 JSON 中记下 request_id 字段值，
# 例如 "3f2a1b8c-...."

# 回到终端 1 所在会话，检索该 id：
grep "3f2a1b8c" server.log
```

预期：同一 `request_id` 出现在该请求的多条日志行中（SQL 生成、校验、执行、完成），
证明链路贯穿。`.env` 设置 `OBSERVABILITY_LOG_FORMAT=json` 时每行日志为 JSON 对象，
`request_id` 是其中一个字段；`text` 格式同样附带。

---

## 12. 故障排查

| # | 现象 | 可能原因 | 解决 |
| --- | --- | --- | --- |
| 1 | `connection refused` / `ECONNREFUSED ...:5432`（psql、make、服务启动、集成测试均可能报） | PostgreSQL 未启动，或监听端口非 5432 | `sudo service postgresql status` 查看；未启动则 `sudo service postgresql start`；`pg_isready -h localhost -p 5432` 与 `ss -ltnp \| grep 5432` 确认监听 |
| 2 | `password authentication failed for user "postgres"`，或 psql 本地能连、TCP 连不上 | TCP 走 scram-sha-256 但未设密码 / 密码不对；或 `pg_hba.conf` 的 host 行被改成 `peer` | 执行 `sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'postgres';"`；检查 `/etc/postgresql/16/main/pg_hba.conf` 中 `host 127.0.0.1/32` 行应为 `scram-sha-256`（或 `md5`），修改后 `sudo service postgresql restart` |
| 3 | locale 警告（如 `warning: setlocale(LC_ALL...)`、集群 locale 不一致提示） | WSL 环境变量 locale 与数据库集群 locale 不匹配 | 通常无害可忽略；需消除时 `sudo locale-gen en_US.UTF-8` 并 `sudo update-locale`，或重建集群时显式 `--locale=C.UTF-8` |
| 4 | metrics 服务启动失败：`address already in use`（9090 端口占用） | 其他进程（或残留的服务实例）占用 9090 | `ss -ltnp \| grep 9090` 找到占用进程并结束；或修改 `.env` 中 `OBSERVABILITY_METRICS_PORT` 为其他空闲端口 |
| 5 | `uv sync` 时 asyncpg 编译失败：`error: command 'gcc' failed` / 找不到 `Python.h` | Python 3.14 无 cp314 wheel 触发源码编译，而缺编译工具链 | `sudo apt install -y build-essential libpq-dev`，然后 `uv cache clean asyncpg && uv sync` |
| 6 | 启动即崩：`OpenAI API key must start with 'sk-'` 或 `max_tokens` 相关 ValidationError | `.env` 中 API key 为空 / 前缀不对；或 `OPENAI_MAX_TOKENS` 大于上限 4096（如旧值 32000） | 确认 `OPENAI_API_KEY=sk-...`；将 `OPENAI_MAX_TOKENS` 改回 `2000`（上限 4096） |
| 7 | 在 `/mnt/e/...` 下 `git` / `uv sync` / `pytest` 极慢 | 9P 协议跨 Windows/Linux 文件系统 I/O 慢 | 把仓库克隆到 WSL 原生路径 `~/workspace` 开发；Windows 侧用 `\\wsl$` 共享或 VS Code Remote-WSL 访问 |

---

## 附录 A：常用命令速查

```bash
# --- 每日开发循环 ---
sudo service postgresql start                     # 进入 WSL 后先确认 PG（status/start）
cd ~/workspace/pg-mcp
uv run pytest tests/unit tests/security -q        # 离线快速测试
uv run ruff check . && uv run mypy src            # lint + 类型
uv run python main.py                             # 启动服务（stdio）

# --- 数据库维护（fixtures）---
cd ~/workspace/pg-mcp/fixtures
export PGPASSWORD=postgres
make create-all        # 首次建库（blog_small / ecommerce_medium / saas_crm_large）
make list              # 查看已建库
make test-all          # 每库统计
make rebuild-all       # 数据脏了，drop 后重建

# --- 集成测试 ---
cd ~/workspace/pg-mcp
DATABASES_FILE=databases.json uv run pytest -m integration

# --- 可观测性 ---
curl -s http://localhost:9090/metrics | grep '^# HELP pg_mcp'   # 指标一览
```

## 附录 B：参考文件

| 文件 | 内容 |
| --- | --- |
| `.env.example` | 全部环境变量及注释（复制为 `.env` 使用） |
| `databases.json.example` | 多库配置 JSON 示例（复制为 `databases.json` 使用） |
| `fixtures/README.md` | 三个测试库的 schema、数据与查询示例详解 |
| `fixtures/Makefile` | 建库 / 删库 / 重建 / 统计等全部 make 目标 |
| `CLAUDE_DESKTOP_SETUP.md` | Claude Desktop 接入配置详解（含各平台路径与排错） |
| `pyproject.toml` | 依赖版本、pytest / ruff / mypy / coverage 配置 |
