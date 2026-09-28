"""限流器回归测试：配额、突发、窗口滑动与键摘要。"""
from __future__ import annotations

import types

import pytest

from app.ratelimit import RateLimiter, key_digest


@pytest.fixture
def fake_clock(monkeypatch):
    """用可控时钟替换 time.monotonic，避免真实等待。"""
    now = {"t": 1000.0}
    fake_time = types.SimpleNamespace(monotonic=lambda: now["t"])
    monkeypatch.setattr("app.ratelimit.time", fake_time)
    return now


def test_allow_within_budget(fake_clock):
    limiter = RateLimiter(rpm=3, burst=0)
    for _ in range(3):
        ok, remaining, retry = limiter.allow("api", "key-1")
        assert ok is True
        assert remaining >= 0
        assert retry == 0


def test_blocked_when_exceeded(fake_clock):
    limiter = RateLimiter(rpm=3, burst=0)
    for _ in range(3):
        assert limiter.allow("api", "key-1")[0] is True
    ok, remaining, retry = limiter.allow("api", "key-1")
    assert ok is False
    assert remaining == 0
    assert retry > 0


def test_burst_extends_budget(fake_clock):
    limiter = RateLimiter(rpm=2, burst=2)
    for _ in range(4):
        assert limiter.allow("api", "key-1")[0] is True
    assert limiter.allow("api", "key-1")[0] is False


def test_window_slides_and_recovers(fake_clock):
    limiter = RateLimiter(rpm=2, burst=0)
    assert limiter.allow("api", "key-1")[0] is True
    assert limiter.allow("api", "key-1")[0] is True
    assert limiter.allow("api", "key-1")[0] is False

    # 滑动窗口推进到第一个请求滑出窗口（窗口 60s，需严格大于）
    fake_clock["t"] += 60.1
    assert limiter.allow("api", "key-1")[0] is True
    # 只剩一个在窗口内，再次请求应放行（旧的已滑出）后超限
    assert limiter.allow("api", "key-1")[0] is True
    assert limiter.allow("api", "key-1")[0] is False


def test_keys_are_namespaced(fake_clock):
    limiter = RateLimiter(rpm=1, burst=0)
    assert limiter.allow("api", "a")[0] is True
    # 不同键互不影响
    assert limiter.allow("api", "b")[0] is True
    # 不同命名空间互不影响
    assert limiter.allow("auth", "a")[0] is True


def test_key_digest_does_not_contain_raw(fake_clock):
    raw = "kb-very-secret-api-key-value"
    digest = key_digest(raw)
    assert raw not in digest
    assert digest == key_digest(raw)
    assert len(digest) == 16
