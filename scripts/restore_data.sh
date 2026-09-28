#!/usr/bin/env bash
# 从 backup_data.sh 产物恢复到 ./data（会覆盖）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ARCHIVE="${1:-}"
if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "用法: $0 data/backups/kb_data_YYYYMMDD_HHMMSS.tar.gz"
  exit 1
fi

echo "恢复将覆盖 $ROOT/data 下内容。Ctrl+C 取消，5 秒后继续..."
sleep 5

mkdir -p data
tar -xzf "$ARCHIVE" -C "$ROOT"
echo "restored from $ARCHIVE"
