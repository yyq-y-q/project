"""
一键本地部署步骤（Windows 友好，不依赖 bash）：
  1) setup keys
  2) check-deploy
  3) rag-rebuild + sqlite-ingest（可用 --skip-index）
  4) 起 mykb serve
  5) 自签证书（HTTPS 需另开 nginx/docker profile）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], *, timeout: int | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True, timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-index", action="store_true", help="跳过 rebuild/ingest")
    ap.add_argument("--serve", action="store_true", help="前台启动 API")
    ap.add_argument("--no-cert", action="store_true", help="不生成自签证书")
    args = ap.parse_args()

    os.chdir(ROOT)
    py = [sys.executable]

    # 优先 uv run
    def uv_or_py(args_list: list[str]) -> list[str]:
        return ["uv", "run", *args_list]

    run(uv_or_py(["python", "scripts/setup_env_keys.py"]))
    run(uv_or_py(["mykb", "check-deploy"]))

    if not args.skip_index:
        # 索引可能很久（下载 embedding）
        run(uv_or_py(["mykb", "rag-rebuild"]), timeout=None)
        run(uv_or_py(["mykb", "sqlite-ingest"]), timeout=None)
        run(uv_or_py(["mykb", "check-deploy"]))

    if not args.no_cert:
        # 证书失败不阻断 API
        try:
            run(uv_or_py(["python", "scripts/gen_self_signed_cert.py"]))
        except subprocess.CalledProcessError:
            print("WARN: cert gen failed — API 仍可 HTTP 访问", flush=True)

    print("\n=== NEXT ===", flush=True)
    print("HTTP API:  uv run mykb serve", flush=True)
    print("Keys:      data/local_api_keys.txt", flush=True)
    print("HTTPS:     docker compose --profile with-proxy up -d --build", flush=True)
    print("           或本机 nginx 挂 deploy/certs + deploy/nginx-https.conf", flush=True)

    if args.serve:
        run(uv_or_py(["mykb", "serve"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
