#!/usr/bin/env bash
# 备份运行时数据（索引 / sqlite / sessions），不含 .env
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${1:-data/backups}"
mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/kb_data_${STAMP}.tar.gz"

# 只打 data 下持久化；locks 可重建
tar -czf "$ARCHIVE" \
  --exclude='data/backups' \
  --exclude='data/locks' \
  --exclude='data/logs' \
  --exclude='data/agent_sandbox' \
  --exclude='data/chat_uploads' \
  -C "$ROOT" data

echo "backup: $ARCHIVE"
ls -lh "$ARCHIVE"
