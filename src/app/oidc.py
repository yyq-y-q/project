"""
OIDC 登录与会话管理（企业 SSO）。

支持 Authorization Code Flow + PKCE，与现有 API Key 鉴权并存。
未配置或 discovery 失败时，OIDC 路由返回明确错误，不影响 API Key 通道。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.auth import Principal, Role
from app.errors import AuthError
from app.logging_setup import get_logger
from app.settings import OIDCConfig, get_settings

logger = get_logger(__name__)

_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_DISCOVERY_TTL = 3600.0


def _fetch_discovery(issuer: str) -> dict[str, Any]:
    now = time.time()
    cached = _discovery_cache.get(issuer)
    if cached and (now - cached[0]) < _DISCOVERY_TTL:
        return cached[1]
    well_known = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(well_known)
            resp.raise_for_status()
            doc = resp.json()
            if not isinstance(doc, dict) or doc.get("issuer") != issuer:
                raise ValueError("OIDC discovery issuer does not match configured issuer")
            _discovery_cache[issuer] = (now, doc)
            logger.info("OIDC discovery 已更新: %s", issuer)
            return doc
    except Exception as e:
        logger.error("OIDC discovery 失败 %s: %s", issuer, e)
        raise AuthError(
            "OIDC 配置不可用",
            stage="oidc.discovery",
            details={"issuer": issuer},
            cause=e,
        ) from e


def _fetch_jwks(jwks_uri: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(jwks_uri)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("OIDC JWKS 获取失败 %s: %s", jwks_uri, e)
        raise AuthError(
            "OIDC 密钥集不可用",
            stage="oidc.jwks",
            details={"jwks_uri": jwks_uri},
            cause=e,
        ) from e


def get_oidc_config() -> OIDCConfig:
    settings = get_settings()
    if not settings.oidc or not settings.oidc.enabled:
        raise AuthError("OIDC 未启用", stage="oidc.config")
    return settings.oidc


def build_auth_url(state: str, nonce: str, code_challenge: str) -> str:
    cfg = get_oidc_config()
    discovery = _fetch_discovery(cfg.issuer)
    auth_endpoint = discovery.get("authorization_endpoint")
    if not auth_endpoint:
        raise AuthError("OIDC discovery 缺少 authorization_endpoint", stage="oidc.config")
    params = {
        "client_id": cfg.client_id,
        "response_type": "code",
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{auth_endpoint}?{urlencode(params)}"


def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    cfg = get_oidc_config()
    discovery = _fetch_discovery(cfg.issuer)
    token_endpoint = discovery.get("token_endpoint")
    if not token_endpoint:
        raise AuthError("OIDC discovery 缺少 token_endpoint", stage="oidc.config")
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "code_verifier": code_verifier,
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(token_endpoint, data=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("OIDC token 交换失败: %s", e)
        raise AuthError(
            "OIDC 授权码交换失败",
            stage="oidc.token",
            cause=e,
        ) from e


def verify_id_token(id_token: str, expected_nonce: str) -> dict[str, Any]:
    cfg = get_oidc_config()
    discovery = _fetch_discovery(cfg.issuer)
    jwks_uri = discovery.get("jwks_uri")
    if not jwks_uri:
        raise AuthError("OIDC discovery 缺少 jwks_uri", stage="oidc.config")
    jwks = _fetch_jwks(jwks_uri)
    try:
        header = jwt.get_unverified_header(id_token)
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not kid:
            raise AuthError("ID token 签名算法或 kid 无效", stage="oidc.verify")
        key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if not key_data:
            raise AuthError("未找到匹配 JWKS key", stage="oidc.verify", details={"kid": kid})
        from jwt.algorithms import RSAAlgorithm
        public_key = RSAAlgorithm.from_jwk(key_data)
        claims = jwt.decode(
            id_token,
            public_key,
            algorithms=["RS256"],
            audience=cfg.client_id,
            issuer=cfg.issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not secrets.compare_digest(nonce, expected_nonce):
            raise AuthError("ID token nonce 不匹配", stage="oidc.verify")
        return claims
    except AuthError:
        raise
    except jwt.ExpiredSignatureError as e:
        raise AuthError("ID token 已过期", stage="oidc.verify", cause=e) from e
    except jwt.InvalidTokenError as e:
        raise AuthError("ID token 验证失败", stage="oidc.verify", cause=e) from e
    except Exception as e:
        logger.exception("ID token 验证异常")
        raise AuthError("ID token 验证失败", stage="oidc.verify", cause=e) from e


def claims_to_principal(claims: dict[str, Any]) -> Principal:
    cfg = get_oidc_config()
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise AuthError("ID token 缺少有效 sub", stage="oidc.claims")
    email = claims.get("email")
    operator = email.strip() if isinstance(email, str) and email.strip() else sub
    role_candidates = claims.get(cfg.role_claim, [])
    if isinstance(role_candidates, str):
        role_candidates = [role_candidates]
    if not isinstance(role_candidates, list):
        role_candidates = []
    role_str = "user"
    for candidate in role_candidates:
        if not isinstance(candidate, str):
            continue
        mapped = cfg.role_mapping.get(candidate)
        if mapped:
            role_str = mapped
            break
    try:
        role = Role(role_str)
    except ValueError:
        role = Role.USER
    from app.auth import DEFAULT_PERMISSIONS
    permissions = DEFAULT_PERMISSIONS[role]
    exp = claims["exp"]
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    return Principal(
        key_id=f"oidc:{sub}",
        role=role,
        operator=operator,
        permissions=permissions,
        expires_at=expires_at,
        source="oidc",
        subject=sub,
        groups=frozenset(item for item in role_candidates if isinstance(item, str)),
    )


def create_session_token(principal: Principal) -> str:
    cfg = get_oidc_config()
    serializer = URLSafeTimedSerializer(cfg.session_secret, salt="oidc-session")
    payload = {
        "key_id": principal.key_id,
        "role": principal.role.value,
        "operator": principal.operator,
        "permissions": list(principal.permissions),
        "expires_at": principal.expires_at,
        "source": principal.source,
        "subject": principal.subject,
        "groups": list(principal.groups),
    }
    return serializer.dumps(payload)


def verify_session_token(token: str, max_age: int = 86400) -> Principal:
    cfg = get_oidc_config()
    serializer = URLSafeTimedSerializer(cfg.session_secret, salt="oidc-session")
    try:
        payload = serializer.loads(token, max_age=max_age)
        role = Role(payload["role"])
        expires_at = payload.get("expires_at")
        if expires_at:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                raise AuthError("会话所对应的 ID token 已过期", stage="oidc.session")
        return Principal(
            key_id=payload["key_id"],
            role=role,
            operator=payload["operator"],
            permissions=frozenset(payload["permissions"]),
            expires_at=payload.get("expires_at"),
            source=payload.get("source", "oidc"),
            subject=payload.get("subject"),
            groups=frozenset(payload.get("groups") or []),
        )
    except (BadSignature, SignatureExpired) as e:
        message = "会话已过期" if isinstance(e, SignatureExpired) else "会话签名无效"
        raise AuthError(message, stage="oidc.session", cause=e) from e
    except AuthError:
        raise
    except Exception as e:
        raise AuthError("会话验证失败", stage="oidc.session", cause=e) from e


def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(challenge).decode("ascii").rstrip("=")
