"""部署相关轻量自检（不加载 embedding/LLM 模型）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)


def main() -> int:
    # 自检必须与本机 .env、生产治理库隔离，避免测试读取真实 key 或动态 key。
    isolated_db = ROOT / "data" / "_deploy_verify_governance.db"
    isolated_db.unlink(missing_ok=True)
    os.environ["KB_GOVERNANCE_DB"] = str(isolated_db)
    for name in tuple(os.environ):
        if name.startswith("KB_API_KEY_"):
            os.environ.pop(name, None)

    from app.auth import resolve_principal
    from app.errors import AuthError
    from app.services import HealthService
    from app.settings import get_settings

    # 1) 开发模式可空 key
    os.environ.pop("KB_ENV", None)
    os.environ.pop("KB_REQUIRE_API_KEY", None)
    # 重新读 settings：模块级可能已缓存？settings 每次 get_settings() 现读 env
    s = get_settings()
    assert s.env == "development"
    p = resolve_principal(None)
    assert p.role.value == "admin", p

    # 2) 生产强制 key
    os.environ["KB_ENV"] = "production"
    os.environ.pop("KB_API_KEY_ADMIN", None)
    os.environ.pop("KB_API_KEY_USER", None)
    os.environ.pop("KB_API_KEY_READONLY", None)
    try:
        resolve_principal("x")
        print("FAIL: production should reject missing keys config")
        return 1
    except AuthError as e:
        print("prod_no_keys_ok", e.code)

    os.environ["KB_API_KEY_ADMIN"] = "secret-admin-key"
    p2 = resolve_principal("secret-admin-key")
    assert p2.role.value == "admin"
    try:
        resolve_principal("wrong")
        print("FAIL: wrong key")
        return 1
    except AuthError:
        print("prod_bad_key_ok")

    # 3) health 轻量
    os.environ["KB_ENV"] = "development"
    h = HealthService().status(deep=False)
    assert "ready" in h and "lock_dir_writable" in h
    print("health_ok", h.get("ready"), h.get("env"))

    # 4) 部署文件在位
    for rel in (
        "Dockerfile",
        "docker-compose.yml",
        "deploy/nginx.conf",
        "deploy/private-kb.service",
        "scripts/init_deploy.sh",
        "scripts/backup_data.sh",
        "scripts/restore_data.sh",
        ".env.example",
        "src/app/settings.py",
        "src/app/locks.py",
    ):
        path = ROOT / rel
        assert path.is_file(), rel
    print("files_ok")
    print("DEPLOY_PREP_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
