# Enterprise Private Knowledge Base

私有知识库：**叙述 RAG** + **表数据 SQLite** + **ReAct Agent** + **HTTP API**。

## 能力

| 链路 | 说明 |
|------|------|
| RAG | txt/md/docx/文字 PDF → 切块 → 向量+BM25 → RRF → Rerank → 带引用回答 |
| SQLite | xlsx/csv → ingest → `query.db` 只读查询（校验+LIMIT+审计） |
| Hybrid | `HybridService`：一次调用内 SQL + RAG + 汇总（确定路径，非 Agent 多步） |
| **Chat** | **统一对话框**：`POST /v1/chat` 自动分流 sql/rag/hybrid/agent/chat；支持上传与粘贴代码块 |
| Agent | 沙箱内文件/终端 + `rag_Query` + `sqlite_Query`，模型自选工具 |
| API | FastAPI：`/v1/rag/*` `/v1/sqlite/*` `/v1/hybrid/ask` `/v1/agent/run` + 简易 UI |
| 错误 | `app.errors.AppError` 统一 `code/message/details`，API 映射 HTTP 状态 |
| 会话 | `session_id` 按调用者隔离多轮记忆，上传工作区与记忆互不可见，支持并发多用户会话文件 |

## 目录

```text
data/raw/          原件（叙述+表混放，按后缀分流）
data/chroma_db/    向量索引
data/bm25_index.json
data/sqlite/       query.db / logger.db
data/sessions/     各 session 对话记忆
data/chat_uploads/ 对话上传/粘贴拆分的附件工作区
data/locks/        跨进程文件锁
data/logs/app.log  滚动日志
data/backups/      备份产物
data/agent_sandbox/ Agent 默认工作目录

tests/              pytest 自动化测试（BM25 / RRF / LLM / 模型管理）
src/rag/           检索生成
src/sqlite_tool/   表 ingest + 只读网关
src/agent/         ReAct
src/app/           API · Service · Hybrid · Locks · Auth · Settings · CLI · UI
deploy/            nginx · systemd
scripts/           部署初始化 · 备份恢复 · 部署检查
```

## 环境与安装

- Python ≥ 3.14
- 依赖由 `uv` 管理，锁文件为 `uv.lock`
- pytest 是开发依赖，随 `dev` 依赖组安装

在项目根目录创建环境配置并填写必要密钥：

```powershell
Copy-Item .env.example .env
```

开发环境至少填写：

```env
DEEP_SEEK_KEY=你的模型服务密钥
KB_ENV=development
```

安装项目及开发依赖：

```bash
uv sync --group dev
```

生产环境必须设置 `KB_ENV=production`、`DEEP_SEEK_KEY` 和至少一把满足长度要求的 `KB_API_KEY_*`；生产模式始终强制 API Key 鉴权。完整可选项见 [`.env.example`](.env.example)。

### 配置参考

| 变量 | 默认 | 说明 |
|------|------|------|
| `DEEP_SEEK_KEY` | — | 模型服务密钥（LLM 生成必需） |
| `KB_ENV` | `development` | `development` / `production`；production 强制 API Key，关闭开发兜底 |
| `KB_REQUIRE_API_KEY` | 随 env | `1` 强制鉴权；`0` 仅本地开发兜底 |
| `KB_HOST` / `KB_PORT` | `127.0.0.1` / `8000` | 监听地址与端口（容器/裸机对外时 host 用 `0.0.0.0`） |
| `KB_WORKERS` | `1` | uvicorn worker 数（内存敏感，建议 1；多 worker 需同盘可写） |
| `KB_LOG_LEVEL` | `INFO` | 日志级别 |
| `KB_CORS_ORIGINS` | 空 | 浏览器跨域白名单（逗号分隔）；留空不启用 CORS |
| `KB_TRUST_PROXY` | `0` | 仅当位于已清洗 `X-Forwarded-For` 的可信反代之后开启 |
| `KB_RATE_LIMIT_ENABLED` | production 开 | 应用层限流总开关 |
| `KB_RATE_LIMIT_RPM` | `60` | 每分钟每键请求数 |
| `KB_RATE_LIMIT_BURST` | `30` | 每 60s 窗口内额外放行数 |
| `KB_API_KEY_ADMIN` 等 | — | 角色密钥（`ADMIN`/`USER`/`READONLY` 等，见下）；每把可配 `_OPERATOR`、`_EXPIRES_AT` |
| `OIDC_*` | 关 | 企业 OIDC 登录（启用后 Bearer 与 API Key 并存） |
| `KB_GOVERNANCE_DB` | `data/governance.db` | 动态 key / 变更 / 审计链数据库 |
| `KB_TEMP_FILE_TTL_MINUTES` | `60` | 临时文件保留时长 |
| `KB_TEMP_FILE_DIR` | 默认目录 | 临时文件落盘目录 |
| `KB_AUDIT_RETENTION_DAYS` | `365` | 审计与变更记录保留天数 |
| `KB_ACL_UNTAGGED_POLICY` | `public` | 未打标**文档**默认策略：`public` / `deny` |
| `KB_SQL_ACL_UNTAGGED_POLICY` | `public` | 未打标**表**默认策略：`public` / `deny` |
| `KB_SQL_TABLE_ACL_JSON` | 空 | 表级策略 JSON，见「数据访问控制」 |
| `KB_ACL_ADMIN_BYPASS` | `1` | admin 是否绕过 ACL（`0` 关闭） |

角色与默认权限：`admin`=全部；`change_requester`（提交变更）、`approver`（审批）、`executor`（执行/回滚）、`auditor`（审计）、`user`（读写知识）、`readonly`（只读）。

## 自动化测试

测试由 pytest 管理，统一放在 `tests/`，按被测模块拆分文件。当前测试覆盖 BM25 索引的构建/加载/清理与检索、RRF 排名融合、LLM 响应和错误边界、模型实例惰性复用。测试使用临时目录及替身对象，不请求真实模型服务，也不下载推理模型。

```bash
# 运行全部测试
uv run pytest

# 运行单个测试模块
uv run pytest tests/test_bm25.py

# 运行单个测试用例
uv run pytest tests/test_fusion.py::test_fusion_returns_empty_list_for_empty_rank_lists
```

新增测试应遵循以下约定：

- 文件名使用 `test_*.py`，测试名使用 `test_*`，使 pytest 可自动发现。
- 使用 pytest fixture、`tmp_path` 和 `monkeypatch` 隔离文件系统、环境变量及外部依赖。
- 覆盖正常结果、边界输入和失败路径；不要依赖真实密钥、网络或运行时数据目录。
- 每个测试独立可重复运行，不依赖执行顺序；共享准备逻辑放入 fixture，而非测试间状态。
- 增加或修改依赖后使用 `uv add --dev <包名>` 或 `uv add <包名>`，并提交更新后的 `uv.lock`。

## 快速开始

```bash
# 一键：重建 RAG + 表 ingest + 冒烟
uv run mykb smoke

# 分步
uv run mykb rag-rebuild
uv run mykb sqlite-ingest
uv run mykb rag-smoke
uv run mykb sqlite-smoke

# 部署前自检（鉴权/密钥/目录，不启服务）
uv run mykb check-deploy

# 确定性混合问答（显式 SQL 或自动 SELECT）
uv run mykb hybrid -q "员工有多少人？什么是RAG？" --sql "SELECT COUNT(*) AS n FROM employees"
uv run mykb hybrid -q "研发部有谁？"

# 统一对话（自动分流；可带本地文件）
uv run mykb chat -m "什么是RAG？"
uv run mykb chat -m "研发部有多少人？"
uv run mykb chat -m "SELECT id, name FROM employees LIMIT 3"
uv run mykb chat -m "帮我看这个脚本" --file path/to/a.py

# API + 控制台 UI
uv run mykb serve
# 浏览器 http://127.0.0.1:8000/  统一对话框；文档 /docs

# Agent CLI（模型自己决定调工具）
uv run myagent data/agent_sandbox
uv run mykb agent -t "研发部有谁？什么是RAG？"
```

## 文件分流

| 后缀 | 去向 |
|------|------|
| .txt .md .docx .pdf(文字层) | RAG |
| .xlsx .csv | sqlite ingest |
| 扫描 PDF | 跳过（待 OCR） |

## API 摘要

| 方法 | 路径 | 角色 |
|------|------|------|
| GET | `/v1/health` | 公开（轻量存活） |
| GET | `/v1/ready` | 公开（就绪；生产缺配置 → 503） |
| GET | `/v1/metrics` | 任意有效角色 |
| POST | `/v1/rag/rebuild` | admin |
| POST | `/v1/rag/ask` | admin/user/readonly |
| POST | `/v1/sqlite/ingest` | admin |
| POST | `/v1/sqlite/query` | admin/user/readonly |
| POST | `/v1/hybrid/ask` | admin/user/readonly |
| POST | `/v1/agent/run` | admin/user |
| POST | `/v1/chat` | admin/user/readonly（multipart：message + files） |
| POST | `/v1/chat/json` | 同上，纯 JSON 无文件 |

Header：`X-API-Key: ...`

### 统一对话 `/v1/chat`

自动分流：

| 信号 | mode |
|------|------|
| 纯 `SELECT ...` | sql |
| 表意图 / 上传 csv·xlsx | hybrid |
| 知识问题 | rag |
| 上传/粘贴代码或项目、编码任务 | agent |
| 寒暄 | chat |

multipart 字段：`message`、`session_id`、`force_mode`、`files`（可多文件）。

粘贴项目：消息里用 \`\`\`src/foo.py 代码 \`\`\` 会拆到 `data/chat_uploads/<session>/`。

Ask 示例：

```json
{"question":"什么是RAG？","session_id":"u1","use_memory":true}
```

Hybrid 示例：

```json
{
  "question": "员工有多少？政策里怎么定义加班？",
  "sql": "SELECT COUNT(*) AS n FROM employees",
  "auto_sql": false,
  "session_id": "h1"
}
```

业务错误响应（`AppError`）：

```json
{"ok": false, "error": {"code": "rag_index_not_ready", "message": "..."}}
```

### curl 调用示例

```bash
# 就绪探针（无需 key）
curl -s http://127.0.0.1:8000/v1/ready

# 知识问答（带 key）
curl -s -X POST http://127.0.0.1:8000/v1/rag/ask \
  -H "X-API-Key: $KB_API_KEY_USER" \
  -H "Content-Type: application/json" \
  -d '{"question": "什么是RAG？", "session_id": "u1"}'

# 只读 SQL（表级 ACL 按 key 身份生效）
curl -s -X POST http://127.0.0.1:8000/v1/sqlite/query \
  -H "X-API-Key: $KB_API_KEY_USER" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT COUNT(*) AS n FROM employees"}'

# 统一对话（自动分流）
curl -s -X POST http://127.0.0.1:8000/v1/chat/json \
  -H "X-API-Key: $KB_API_KEY_USER" \
  -H "Content-Type: application/json" \
  -d '{"message": "研发部有多少人？", "session_id": "c1"}'

# 管理操作（仅 admin）
curl -s -X POST http://127.0.0.1:8000/v1/sqlite/ingest \
  -H "X-API-Key: $KB_API_KEY_ADMIN"
```

### 错误码

| HTTP | code | 含义 |
|------|------|------|
| 401 | `auth_error` | 缺 key / key 无效 / 已过期 |
| 403 | `permission_denied` | 角色或权限不足（含 SQL 表被 ACL 拒绝） |
| 429 | `rate_limited` | 触发限流（带 `Retry-After`） |
| 422 | `validation_error` | 参数校验失败 |
| 503 | `rag_index_not_ready` / `db_not_ready` | 索引或表库未就绪 |
| 500 | `rag_index_build_failed` / `ingest_error` | 重建 / 摄取失败 |
| 500 | `llm_error` | 大模型调用失败（key 缺失 / 上游错误） |
| 400 | `sql_validation_error` / `sql_query_error` | SQL 校验或执行失败 |
| 500 | `agent_error` / `hybrid_error` / `rag_error` | 对应链路内部错误 |
| 500 | `audit_integrity_error` / `config_error` / `internal_error` | 审计链被篡改 / 配置错误 / 未知异常 |

## 数据访问控制（ACL）

ACL 在**重排、摘要、Prompt 拼接和模型调用之前**生效，受限内容不会进入回答；SQL 在**执行前**校验。

### 文档级（RAG）

文档元数据打标字段（由 `data/raw/` 下的 `_meta.json` 或文件名伴生元数据提供）：

| 字段 | 说明 |
|------|------|
| `visibility` | `public`（所有人可读）/ `private`（默认，仅 owner/允许者）/ `restricted` |
| `owner_key_id` / `owner_principal_id` / `owner_subject` / `owner_operator` | 所有者身份 |
| `allowed_key_ids` / `allowed_principal_ids` / `allowed_subjects` | 允许访问者白名单 |
| `allowed_groups` / `allowed_roles` | 按组 / 按角色开放 |

未打标文档遵循 `KB_ACL_UNTAGGED_POLICY`（默认 `public`，可设 `deny` 收紧）。

### 表级（SQLite）

用 `KB_SQL_TABLE_ACL_JSON` 配置，例如：

```json
{"sales": {"visibility": "public"},
 "hr":   {"visibility": "restricted", "allowed_roles": "admin"},
 "finance": {"allowed_key_ids": "kb-xxx"}}
```

- 未出现在 JSON 中的表遵循 `KB_SQL_ACL_UNTAGGED_POLICY`（默认 `public`）
- `sqlite_master` 等系统表仅 admin 可查
- 表名不区分大小写；SQL 引用的**所有**表都通过才放行

### 生效位置与旁路

- RAG：两路召回、RRF 融合、重排前 `filter_chunks_by_acl`
- SQL：`TableService.query` 与 Agent 的 `sqlite_Query` 工具在执行前解析表名并校验
- Hybrid / Chat / Agent 全链路把调用者 `principal` 一路透传，**不存在绕过 ACL 的暗门**
- admin 默认绕过全部 ACL（`KB_ACL_ADMIN_BYPASS=0` 可关闭）

## 会话隔离与限流

- **会话按调用者隔离**：API 层所有 `session_id` 经 `scoped_session_id(principal, sid)` 转为 `"{key_id}:{sid}"` 后再落盘；不同 key 传入相同 `session_id` 也不会读到对方的 RAG 记忆或上传工作区（`data/chat_uploads/<scoped>/`）
- **应用层限流**：进程内滑动窗口，键为 `X-API-Key` 摘要或客户端 IP；超限返回 `429` + `Retry-After` + `X-RateLimit-Remaining`；`/v1/health`、`/v1/ready` 豁免
- **nginx 层限流**：`deploy/*.conf` 内置按 IP `120r/m` 限速 + 单 IP 20 连接上限，多 worker 时兜底

## 安全（当前级别）

- Agent 文件/终端限制在工作目录；危险命令黑名单；命令超时
- SQL 只读网关 + 自动 LIMIT；**表级 ACL 按调用者身份执行**（API / Hybrid / Agent 工具统一走 `principal`）
- API Key → 角色；`KB_ENV=production` **强制配置 key，关闭开发兜底**
- **会话按调用者隔离**：所有 `session_id` 经 `scoped_session_id` 拼接 key 身份，上传工作区与 RAG 记忆互不可见
- **全局限流**：应用层滑动窗口（`KB_RATE_LIMIT_*`，生产默认开启），nginx 层 IP 限速/限连接
- 跨进程文件锁：`data/locks/`（RAG 重建 / SQLite 写 / session 记忆）
- 密钥与 TLS 私钥仅 `.env` / `deploy/certs/`，已 gitignore

## 部署

### 清单（上线前）

1. `.env`：`KB_ENV=production` + `DEEP_SEEK_KEY` + 至少一把 `KB_API_KEY_*`；按需配置 `KB_RATE_LIMIT_*`、`KB_ACL_UNTAGGED_POLICY`、`KB_SQL_TABLE_ACL_JSON`
2. TLS 证书：`deploy/certs/fullchain.pem` + `privkey.pem`（公网用 certbot，内网用 `uv run python scripts/gen_self_signed_cert.py`，见 `deploy/certs/README.md`）；**私钥勿进仓库**
3. `uv run mykb check-deploy` → `ready: true`
4. 原件进 `data/raw/`，执行 init 或分步 rebuild/ingest
5. 反代 HTTPS（裸机 `deploy/nginx.conf`；docker compose 用 `--profile with-proxy` 挂载 `deploy/nginx-https.conf`）
6. 定期 `scripts/backup_data.sh`

### 裸机

```bash
cp .env.example .env   # 编辑生产值
chmod +x scripts/*.sh
./scripts/init_deploy.sh
# 生成证书（无现成证书时）
uv run python scripts/gen_self_signed_cert.py
# 复制 nginx 配置并启动（已含 HTTP→HTTPS 跳转 + 限流 + 安全头）
sudo cp deploy/nginx.conf /etc/nginx/conf.d/kb.conf
sudo nginx -t && sudo systemctl reload nginx
uv run mykb serve
# 或 systemd：
# sudo cp deploy/private-kb.service /etc/systemd/system/
# sudo systemctl enable --now private-kb
```

### Docker

```bash
cp .env.example .env   # KB_ENV=production + keys
# 先生成证书（无现成证书时）
uv run python scripts/gen_self_signed_cert.py
docker compose up -d --build
# 带 nginx（HTTPS）：docker compose --profile with-proxy up -d --build
curl -s http://127.0.0.1:8000/v1/ready
# HTTPS 入口：https://127.0.0.1:8443/v1/ready
```

### 备份 / 恢复

```bash
./scripts/backup_data.sh              # → data/backups/kb_data_*.tar.gz
./scripts/restore_data.sh data/backups/kb_data_YYYYMMDD_HHMMSS.tar.gz
```

### 进程与锁

- 默认 `KB_WORKERS=1`；多 worker 时 `data/` 必须同盘可写（索引/表/session 文件锁）
- 日志：`data/logs/app.log`；指标：`/v1/metrics`（需 API Key）
- 勿把 `data/`、`.env`、`deploy/certs/` 进仓库
- 应用层限流为进程内计数：多 worker 时按进程独立计算，建议同时保留 nginx 层限流兜底（已在 `deploy/*.conf` 内置）

## 运维与监控

### 日志

- 应用日志：`data/logs/app.log`（滚动，保留策略见 `logging_setup.py`）
- 排查入口：`tail -f data/logs/app.log`；错误统一带 `stage` 与 `error.code`，可 grep 定位
- 审计日志：`data/sqlite/logger.db`（SQL 执行审计、变更留痕）

### 监控

- `GET /v1/health`：轻量存活探针（k8s/systemd 用，不加载模型）
- `GET /v1/ready`：就绪探针；生产缺配置返回 `503` 且 `prod_blockers` 列出原因
- `GET /v1/metrics`：进程指标（需有效 API Key）

### 发布更新流程

```bash
cd /path/to/project
git pull origin main          # 拉取新版本
uv sync --frozen              # 同步依赖（有锁文件）
./scripts/backup_data.sh      # 更新前先备份数据
uv run mykb check-deploy      # 自检通过再重启
sudo systemctl restart private-kb   # 或 docker compose up -d --build
```

### 证书续期

- Let's Encrypt：`sudo certbot renew`（建议 cron 每周）；续期后无需重启 nginx
- 自签证书：到期前重跑 `uv run python scripts/gen_self_signed_cert.py`

### 数据备份

- `./scripts/backup_data.sh` → `data/backups/kb_data_*.tar.gz`（含 raw 原件、索引、表库、会话、治理库）
- 建议 cron 每日备份 + 异地留存一份

## 常见问题排查

| 现象 | 原因与处理 |
|------|-----------|
| `/v1/ready` 返回 503 | 看 `prod_blockers`：缺 API Key / 缺 `DEEP_SEEK_KEY` / 锁目录不可写 → 补配置后重启 |
| 请求返回 401 | `X-API-Key` 缺失 / 错误 / 过期 → 检查 key 与 `_EXPIRES_AT` |
| 返回 403 `permission_denied` | 角色权限不足，或 SQL 引用了无权的表 → 换高角色 key，或调整 `KB_SQL_TABLE_ACL_JSON` |
| 返回 429 | 触发限流 → 降低调用频率，或调大 `KB_RATE_LIMIT_RPM/BURST` |
| `rag_index_not_ready` | 索引未构建 → `uv run mykb rag-rebuild`；确认 `data/raw/` 有文档 |
| `db_not_ready` | 表库缺失 → `uv run mykb sqlite-ingest` |
| `llm_error` | `DEEP_SEEK_KEY` 缺失或上游超时 → 检查密钥与网络，看日志定位 stage |
| 服务起不来（`ImportError`） | 依赖未装或代码不完整 → `uv sync --frozen`，确认是从仓库完整拉取 |
| HTTPS 打不开 | 证书缺失 → 先 `uv run python scripts/gen_self_signed_cert.py` 或放置正式证书，再 `nginx -t && systemctl reload nginx` |

## 阶段说明

- Phase1 RAG 基线 ✅
- Phase2 docx/PDF ✅
- Phase3 表一键 ingest ✅
- Phase4 Agent 沙箱/路由/session ✅
- Phase5 API/Service/CLI/UI/日志 ✅
- Phase6 并发锁 + 部署包（Docker/nginx/systemd/备份/生产鉴权）✅
- 更后：OCR、增量索引、JWT/OIDC、完整 RBAC、前端工程化

日常：`mykb check-deploy` / `mykb smoke` / `mykb serve`。
