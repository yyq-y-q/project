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
| 会话 | `session_id` 隔离多轮记忆，支持并发多用户会话文件 |

## 目录

```text
data/raw/          原件（叙述+表混放，按后缀分流）
data/chroma_db/    向量索引
data/bm25_index.json
data/sqlite/       query.db / logger.db
data/sessions/     各 session 对话记忆
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

## 阶段说明

- Phase1 RAG 基线 ✅
- Phase2 docx/PDF ✅
- Phase3 表一键 ingest ✅
- Phase4 Agent 沙箱/路由/session ✅
- Phase5 API/Service/CLI/UI/日志 ✅
- Phase6 并发锁 + 部署包（Docker/nginx/systemd/备份/生产鉴权）✅
- 更后：OCR、增量索引、JWT/OIDC、完整 RBAC、前端工程化

日常：`mykb check-deploy` / `mykb smoke` / `mykb serve`。
