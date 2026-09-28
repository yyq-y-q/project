#!/usr/bin/env bash
# 首次部署：目录 + 依赖 + 索引/表初始化
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> project: $ROOT"

if [[ ! -f .env ]]; then
  echo "缺少 .env：请 cp .env.example .env 并填写 DEEP_SEEK_KEY 与 API Key"
  exit 1
fi

# shellcheck disable=SC1091
set -a
source .env
set +a

if [[ "${KB_ENV:-}" == "production" || "${KB_ENV:-}" == "prod" ]]; then
  if [[ -z "${KB_API_KEY_ADMIN:-}${KB_API_KEY_USER:-}${KB_API_KEY_READONLY:-}" ]]; then
    echo "production 必须配置至少一把 KB_API_KEY_*"
    exit 1
  fi
fi

mkdir -p data/raw data/logs data/locks data/sqlite data/sessions data/chroma_db data/backups

if command -v uv >/dev/null 2>&1; then
  uv sync --frozen
  RUN=(uv run)
else
  echo "未找到 uv，尝试 .venv/bin/python"
  RUN=(.venv/bin/python -m)
fi

echo "==> deploy check"
"${RUN[@]}" mykb check-deploy || true

echo "==> rag rebuild"
"${RUN[@]}" mykb rag-rebuild

echo "==> sqlite ingest"
"${RUN[@]}" mykb sqlite-ingest

echo "==> final check"
"${RUN[@]}" mykb check-deploy

echo "OK: 初始化完成。启动: uv run mykb serve   或 docker compose up -d"
