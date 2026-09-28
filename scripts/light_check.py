"""不 import rag 模型的轻量 check-deploy。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def main() -> int:
    from app.auth import auth_configured, resolve_principal
    from app.settings import get_settings

    s = get_settings()
    print("env", s.env)
    print("require_api_key", s.require_api_key)
    print("auth_configured", auth_configured())
    print("llm_key", bool(os.getenv("DEEP_SEEK_KEY", "").strip()))

    blockers = []
    if s.require_api_key and not auth_configured():
        blockers.append("auth_keys_missing")
    if not os.getenv("DEEP_SEEK_KEY", "").strip():
        blockers.append("llm_key_missing")

    lock = ROOT / "data" / "locks"
    lock.mkdir(parents=True, exist_ok=True)
    probe = lock / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        print("lock_dir_writable", True)
    except Exception as e:
        print("lock_dir_writable", False, e)
        blockers.append("lock_dir_not_writable")

    # 生产必须能解析 admin key
    admin = os.getenv("KB_API_KEY_ADMIN", "").strip()
    if admin:
        p = resolve_principal(admin)
        print("admin_role", p.role.value)
    else:
        blockers.append("no_admin_key")

    bm25 = (ROOT / "data" / "bm25_index.json").exists()
    chroma = (ROOT / "data" / "chroma_db").exists()
    qdb = (ROOT / "data" / "sqlite" / "query.db").exists()
    print("bm25", bm25, "chroma", chroma, "query_db", qdb)

    ready = not blockers
    print("ready", ready)
    print("blockers", blockers)
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
