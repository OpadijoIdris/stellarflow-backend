from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenBucketConfig:
    max_tokens: float
    refill_rate: float
    refill_interval: float = 1.0


@dataclass(frozen=True)
class TokenBucketSnapshot:
    current_tokens: float
    max_tokens: float
    fill_ratio: float
    is_throttled: bool


class TokenBucket:
    __slots__ = ("_config", "_tokens", "_last_refill", "_lock")

    def __init__(self, config: TokenBucketConfig) -> None:
        self._config = config
        self._tokens: float = config.max_tokens
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed >= self._config.refill_interval:
            tokens_to_add = elapsed * self._config.refill_rate
            if tokens_to_add > 0:
                self._tokens = min(
                    self._config.max_tokens, self._tokens + tokens_to_add
                )
            self._last_refill = now

    def try_consume(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def consume_or_wait(
        self, tokens: float = 1.0, timeout: Optional[float] = None
    ) -> bool:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            if self.try_consume(tokens):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(max(0.001, self._config.refill_interval / 100))

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def snapshot(self) -> TokenBucketSnapshot:
        with self._lock:
            self._refill()
            return TokenBucketSnapshot(
                current_tokens=round(self._tokens, 4),
                max_tokens=self._config.max_tokens,
                fill_ratio=round(self._tokens / self._config.max_tokens, 4),
                is_throttled=self._tokens < 1.0,
            )

    def reset(self) -> None:
        with self._lock:
            self._tokens = self._config.max_tokens
            self._last_refill = time.monotonic()

    def update_config(self, config: TokenBucketConfig) -> None:
        with self._lock:
            self._config = config
            if self._tokens > config.max_tokens:
                self._tokens = config.max_tokens


class TokenBucketController:
    __slots__ = ("_buckets", "_map_lock", "_default_config")

    def __init__(
        self, default_config: Optional[TokenBucketConfig] = None
    ) -> None:
        self._default_config = default_config or TokenBucketConfig(
            max_tokens=100,
            refill_rate=10.0,
            refill_interval=1.0,
        )
        self._buckets: Dict[str, TokenBucket] = {}
        self._map_lock = threading.Lock()

    def _get_or_create(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            with self._map_lock:
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = TokenBucket(self._default_config)
                    self._buckets[key] = bucket
        return bucket

    def try_consume(self, key: str, tokens: float = 1.0) -> bool:
        return self._get_or_create(key).try_consume(tokens)

    def consume_or_wait(
        self, key: str, tokens: float = 1.0, timeout: Optional[float] = None
    ) -> bool:
        return self._get_or_create(key).consume_or_wait(tokens, timeout)

    def snapshot(self, key: str) -> TokenBucketSnapshot:
        return self._get_or_create(key).snapshot()

    def configure(
        self, key: str, config: TokenBucketConfig
    ) -> None:
        self._get_or_create(key).update_config(config)

    def reset(self, key: Optional[str] = None) -> None:
        if key is not None:
            self._get_or_create(key).reset()
        else:
            with self._map_lock:
                for bucket in self._buckets.values():
                    bucket.reset()

    def snapshot_all(self) -> Dict[str, TokenBucketSnapshot]:
        return {k: v.snapshot() for k, v in self._buckets.items()}


token_bucket_controller = TokenBucketController()

__all__ = [
    "TokenBucketConfig",
    "TokenBucketSnapshot",
    "TokenBucket",
    "TokenBucketController",
    "token_bucket_controller",
]
