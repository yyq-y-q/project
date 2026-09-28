"""
轻量进程内限流（单节点/单 worker 适用）。

实现：滑动窗口计数 + 突发配额，按 (命名空间, 键) 计数，线程安全。
- 键优先取 API Key 的 SHA-256 摘要（不落明文）；
  未带 key 的请求（如 /auth/*）退化为客户端 IP。
- /v1/health、/v1/ready 探针豁免，避免探活被限流误伤。
- 多 worker 部署时各进程独立计数，配额按 worker 数分摊；
  更严格的全局限流请配合 deploy/nginx.conf 的 limit_req。

配置（.env）：
  KB_RATE_LIMIT_ENABLED=1|0   默认 production 开启
  KB_RATE_LIMIT_RPM=60        每分钟每键请求数
  KB_RATE_LIMIT_BURST=30      每 60 秒窗口内额外放行数
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque

DEFAULT_RPM = 60
DEFAULT_BURST = 30
WINDOW_SECONDS = 60.0
_MAX_BUCKETS = 8192


def key_digest(value: str) -> str:
    """对限流键做摘要，避免在内存中留存明文 API Key。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class RateLimiter:
    """按键滑动窗口限流器。"""

    def __init__(self, rpm: int = DEFAULT_RPM, burst: int = DEFAULT_BURST):
        self.rpm = max(1, int(rpm))
        self.burst = max(0, int(burst))
        self.window = WINDOW_SECONDS
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, namespace: str, key: str) -> tuple[bool, int, int]:
        """返回 (是否放行, 窗口剩余配额, 被限时建议重试秒数)。"""
        now = time.monotonic()
        bucket_key = (namespace, key)
        with self._lock:
            bucket = self._hits[bucket_key]
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            limit = self.rpm + self.burst
            if len(bucket) >= limit:
                retry = int(self.window - (now - bucket[0])) + 1
                return False, 0, retry
            bucket.append(now)
            if len(self._hits) > _MAX_BUCKETS:
                self._prune(now)
            return True, limit - len(bucket), 0

    def _prune(self, now: float) -> None:
        stale = [
            k
            for k, v in self._hits.items()
            if not v or now - v[-1] > max(self.window * 4, 300.0)
        ]
        for k in stale:
            del self._hits[k]

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
