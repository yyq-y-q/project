"""一次性：给 .env 补生产密钥（保留已有 DEEP_SEEK_KEY）。"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
KEYS_OUT = ROOT / "data" / "local_api_keys.txt"


def upsert(text: str, key: str, value: str) -> str:
    pat = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    line = f"{key}={value}"
    if pat.search(text):
        return pat.sub(line, text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def main() -> None:
    if not ENV.exists():
        raise SystemExit(f"missing {ENV}")
    raw = ENV.read_text(encoding="utf-8")
    if "DEEP_SEEK_KEY=" not in raw or re.search(
        r"(?m)^DEEP_SEEK_KEY=\s*$", raw
    ):
        raise SystemExit("DEEP_SEEK_KEY empty — fill LLM key first")

    admin = "kb-admin-" + secrets.token_hex(24)
    user = "kb-user-" + secrets.token_hex(24)
    ro = "kb-ro-" + secrets.token_hex(24)

    for k, v in [
        ("KB_ENV", "production"),
        ("KB_REQUIRE_API_KEY", "1"),
        ("KB_HOST", "0.0.0.0"),
        ("KB_PORT", "8000"),
        ("KB_WORKERS", "1"),
        ("KB_LOG_LEVEL", "INFO"),
        ("KB_API_KEY_ADMIN", admin),
        ("KB_API_KEY_USER", user),
        ("KB_API_KEY_READONLY", ro),
    ]:
        raw = upsert(raw, k, v)

    ENV.write_text(raw, encoding="utf-8")
    KEYS_OUT.parent.mkdir(parents=True, exist_ok=True)
    KEYS_OUT.write_text(
        "# 本地 API Key 副本（data/ 已 gitignore）\n"
        f"KB_API_KEY_ADMIN={admin}\n"
        f"KB_API_KEY_USER={user}\n"
        f"KB_API_KEY_READONLY={ro}\n",
        encoding="utf-8",
    )
    print("ENV_KEYS_OK")
    print(f"wrote {ENV}")
    print(f"keys_copy {KEYS_OUT}")


if __name__ == "__main__":
    main()
