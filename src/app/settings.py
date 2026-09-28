"""
运行时环境开关（部署前配置面）

  KB_ENV=development|production   默认 development
  KB_REQUIRE_API_KEY=0|1          强制 API Key（production 默认开）
  KB_HOST / KB_PORT               serve 默认绑定
  KB_WORKERS                      uvicorn worker 数（建议 1~2；多 worker 靠文件锁）
  KB_LOG_LEVEL                    INFO|DEBUG|WARNING
  KB_CORS_ORIGINS                 逗号分隔；空=不启用 CORS
  KB_RATE_LIMIT_ENABLED           0|1（production 默认开，development 默认关）
  KB_RATE_LIMIT_RPM               每分钟每键请求数（默认 60）
  KB_RATE_LIMIT_BURST             窗口内额外突发配额（默认 30）
  OIDC_*                          企业 SSO 登录配置（可选）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _truthy(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OIDCConfig:
    enabled: bool
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str
    role_claim: str
    role_mapping: dict[str, str]
    session_secret: str


@dataclass(frozen=True)
class Settings:
    env: str
    require_api_key: bool
    host: str
    port: int
    workers: int
    log_level: str
    cors_origins: tuple[str, ...]
    trust_proxy: bool
    rate_limit_enabled: bool
    rate_limit_rpm: int
    rate_limit_burst: int
    project_root: Path
    oidc: OIDCConfig | None

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def _get_oidc_config() -> OIDCConfig | None:
    if not _truthy("OIDC_ENABLED"):
        return None
    issuer = os.getenv("OIDC_ISSUER", "").strip()
    client_id = os.getenv("OIDC_CLIENT_ID", "").strip()
    client_secret = os.getenv("OIDC_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("OIDC_REDIRECT_URI", "").strip()
    missing = [
        name
        for name, value in (
            ("OIDC_ISSUER", issuer),
            ("OIDC_CLIENT_ID", client_id),
            ("OIDC_CLIENT_SECRET", client_secret),
            ("OIDC_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"OIDC_ENABLED is set but required settings are missing: {', '.join(missing)}")
    scope = os.getenv("OIDC_SCOPE", "openid profile email").strip()
    role_claim = os.getenv("OIDC_ROLE_CLAIM", "groups").strip()
    mapping_raw = os.getenv("OIDC_ROLE_MAPPING", "{}").strip()
    try:
        role_mapping = json.loads(mapping_raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("OIDC_ROLE_MAPPING must be a JSON object") from exc
    if not isinstance(role_mapping, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in role_mapping.items()
    ):
        raise ValueError("OIDC_ROLE_MAPPING must map string group names to string roles")
    session_secret = os.getenv("OIDC_SESSION_SECRET", "").strip()
    if len(session_secret) < 32:
        raise ValueError("OIDC_SESSION_SECRET must contain at least 32 characters")
    return OIDCConfig(
        enabled=True,
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scope,
        role_claim=role_claim,
        role_mapping=role_mapping,
        session_secret=session_secret,
    )


def _integer_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def get_settings() -> Settings:
    raw_env = (os.getenv("KB_ENV") or "development").strip().lower()
    if raw_env in {"dev", "development"}:
        env = "development"
    elif raw_env in {"prod", "production"}:
        env = "production"
    else:
        raise ValueError("KB_ENV must be development or production")

    # 开发态允许显式关闭鉴权；生产态始终强制鉴权。
    if env == "production":
        require = True
    elif os.getenv("KB_REQUIRE_API_KEY") is not None:
        require = _truthy("KB_REQUIRE_API_KEY")
    else:
        require = False

    cors_raw = (os.getenv("KB_CORS_ORIGINS") or "").strip()
    origins = tuple(x.strip() for x in cors_raw.split(",") if x.strip())
    port = _integer_env("KB_PORT", 8000, minimum=1, maximum=65535)
    workers = _integer_env("KB_WORKERS", 1, minimum=1, maximum=8)
    log_level = (os.getenv("KB_LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("KB_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

    return Settings(
        env=env,
        require_api_key=require,
        host=(os.getenv("KB_HOST") or "127.0.0.1").strip(),
        port=port,
        workers=workers,
        log_level=log_level,
        cors_origins=origins,
        trust_proxy=_truthy("KB_TRUST_PROXY"),
        rate_limit_enabled=_truthy("KB_RATE_LIMIT_ENABLED", "1" if env == "production" else "0"),
        rate_limit_rpm=_integer_env("KB_RATE_LIMIT_RPM", 60, minimum=1, maximum=100000),
        rate_limit_burst=_integer_env("KB_RATE_LIMIT_BURST", 30, minimum=0, maximum=100000),
        project_root=PROJECT_ROOT,
        oidc=_get_oidc_config(),
    )


def validate_runtime_settings(settings: Settings, *, auth_configured: bool) -> tuple[str, ...]:
    """Return safe-to-display production blockers without including secret values."""
    issues: list[str] = []
    if not settings.host:
        issues.append("KB_HOST must not be empty")

    for origin in settings.cors_origins:
        parsed = urlparse(origin)
        if origin != "*" and (parsed.scheme not in {"http", "https"} or not parsed.netloc):
            issues.append("KB_CORS_ORIGINS entries must be absolute HTTP(S) origins")
            break
    if settings.is_production and "*" in settings.cors_origins:
        issues.append("KB_CORS_ORIGINS must not contain * in production")

    if settings.oidc:
        issuer = urlparse(settings.oidc.issuer)
        redirect = urlparse(settings.oidc.redirect_uri)
        if issuer.scheme not in {"http", "https"} or not issuer.netloc:
            issues.append("OIDC_ISSUER must be an absolute HTTP(S) URL")
        if redirect.scheme not in {"http", "https"} or not redirect.netloc:
            issues.append("OIDC_REDIRECT_URI must be an absolute HTTP(S) URL")
        if settings.is_production and (issuer.scheme != "https" or redirect.scheme != "https"):
            issues.append("OIDC_ISSUER and OIDC_REDIRECT_URI must use HTTPS in production")

    if settings.is_production:
        if not auth_configured:
            issues.append("production requires at least one configured API key")
        for name in (
            "KB_API_KEY_ADMIN",
            "KB_API_KEY_CHANGE_REQUESTER",
            "KB_API_KEY_APPROVER",
            "KB_API_KEY_EXECUTOR",
            "KB_API_KEY_AUDITOR",
            "KB_API_KEY_USER",
            "KB_API_KEY_READONLY",
        ):
            value = os.getenv(name, "").strip()
            if value and (len(value) < 32 or value.lower().startswith("replace-with")):
                issues.append(f"{name} must be a non-placeholder secret of at least 32 characters")
        if not os.getenv("DEEP_SEEK_KEY", "").strip():
            issues.append("DEEP_SEEK_KEY is required in production")

    return tuple(issues)
