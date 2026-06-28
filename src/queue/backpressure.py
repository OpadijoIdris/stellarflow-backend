from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

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


# ---------------------------------------------------------------------------
# Redis-backed sliding window rate limiter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlidingWindowConfig:
    """Configuration for a SlidingWindowLimiter instance.

    Attributes:
        limit:         Maximum number of requests allowed inside *window_seconds*.
                       Defaults to 100 (matching the 100 req/s security spec).
        window_seconds: Rolling window duration in seconds. Default 1.0 s.
        key_prefix:    Redis key prefix; isolates limiters by use-case.
        mode:          Default policy when the limit is crossed.
                       ``"drop"`` returns immediately; ``"defer"`` spins until
                       a slot opens or *defer_timeout* elapses.
        defer_timeout: Maximum seconds to wait in defer mode before giving up.
    """

    limit: int = 100
    window_seconds: float = 1.0
    key_prefix: str = "rate_limit"
    mode: str = "drop"
    defer_timeout: float = 0.5


class RateLimitDecision(str, Enum):
    """Outcome of a single rate-limit check."""

    ALLOWED = "allowed"
    DROPPED = "dropped"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class RateLimitResult:
    """Result of a :meth:`SlidingWindowLimiter.check` call.

    Attributes:
        decision:      Whether the request was allowed, dropped, or deferred.
        endpoint:      The key that was evaluated.
        current_count: Requests recorded in the window after this check.
        limit:         The configured request ceiling.
        retry_after:   Seconds before a slot is likely to reopen (``None``
                       when the request was allowed).
    """

    decision: RateLimitDecision
    endpoint: str
    current_count: int
    limit: int
    retry_after: Optional[float]


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


class InMemorySlidingWindowBackend:
    """Thread-safe, process-local backend using a sorted list per key.

    Intended for unit testing and environments where Redis is unavailable.
    The check-and-record operation holds a single global lock, so it is
    correct but not optimised for extreme concurrency.

    Each stored entry is a ``(timestamp, unique_id)`` pair. Entries older
    than the configured window are pruned on every access.
    """

    def __init__(self) -> None:
        self._store: Dict[str, List[Tuple[float, str]]] = {}
        self._lock = threading.Lock()

    def check_and_record(
        self,
        key: str,
        now: float,
        window: float,
        limit: int,
        unique_id: str,
    ) -> Tuple[bool, int]:
        """Prune, count, and conditionally record in one atomic step.

        Returns:
            ``(allowed, count)`` — whether the request was admitted and the
            resulting entry count inside the window.
        """
        with self._lock:
            entries = self._store.setdefault(key, [])
            cutoff = now - window
            self._store[key] = [(t, uid) for t, uid in entries if t > cutoff]
            count = len(self._store[key])
            if count < limit:
                self._store[key].append((now, unique_id))
                return True, count + 1
            return False, count

    def current_count(self, key: str, now: float, window: float) -> int:
        """Return the live entry count without recording a new request."""
        with self._lock:
            entries = self._store.get(key, [])
            cutoff = now - window
            return sum(1 for t, _ in entries if t > cutoff)

    def reset(self, key: str) -> None:
        """Evict all entries for *key*."""
        with self._lock:
            self._store.pop(key, None)


class RedisSlidingWindowBackend:
    """Redis sorted-set backend with atomic Lua scripts.

    Each rate-limit key is stored as a Redis sorted set where the score is
    the Unix timestamp (float) of the request and the member is a unique
    request identifier. A single Lua script handles the full prune → count →
    conditionally-add cycle atomically, eliminating any TOCTOU race between
    reading the count and inserting the new entry.

    The key expires automatically ``ceil(window_seconds) + 1`` seconds after
    the last write, so stale keys do not accumulate in Redis.

    Args:
        redis_client: A ``redis.Redis`` (or compatible) client instance.
    """

    # Lua script: prune window, check count, conditionally insert.
    _LUA_CHECK_AND_RECORD = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, math.ceil(window) + 1)
    return {1, count + 1}
else
    return {0, count}
end
"""

    # Lua script: prune window and return current count (read-only view).
    _LUA_CURRENT_COUNT = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
return redis.call('ZCARD', key)
"""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client
        self._check_script = redis_client.register_script(
            self._LUA_CHECK_AND_RECORD
        )
        self._count_script = redis_client.register_script(
            self._LUA_CURRENT_COUNT
        )

    def check_and_record(
        self,
        key: str,
        now: float,
        window: float,
        limit: int,
        unique_id: str,
    ) -> Tuple[bool, int]:
        result = self._check_script(
            keys=[key],
            args=[f"{now:.9f}", str(window), str(limit), unique_id],
        )
        return bool(result[0]), int(result[1])

    def current_count(self, key: str, now: float, window: float) -> int:
        return int(
            self._count_script(keys=[key], args=[f"{now:.9f}", str(window)])
        )

    def reset(self, key: str) -> None:
        self._redis.delete(key)


# ---------------------------------------------------------------------------
# Main limiter interface
# ---------------------------------------------------------------------------

_id_counter = itertools.count(1)


class SlidingWindowLimiter:
    """Redis-backed sliding window rate limiter for network gateway endpoints.

    Protects internal messaging pipelines from unregulated third-party data
    surges by enforcing a maximum request rate per endpoint key. The default
    configuration enforces 100 requests per second, matching the security
    specification for incoming telemetry gateways.

    Requests that exceed the limit are handled according to *mode*:

    * ``"drop"``   — Return a ``DROPPED`` result immediately. The caller is
                     responsible for discarding the packet.
    * ``"defer"``  — Spin until either a slot opens inside the current window
                     or *defer_timeout* seconds elapse. If the timeout is
                     reached the request is dropped. This is useful for
                     low-priority telemetry that can tolerate a brief delay
                     rather than being silently discarded.

    Backend selection
    -----------------
    Pass a :class:`RedisSlidingWindowBackend` for production deployments.
    Pass an :class:`InMemorySlidingWindowBackend` for local testing or
    environments without Redis.

    Complexity
    ----------
    ``check()``         : O(log N + P) on the Redis side (N = window entries,
                          P = entries pruned). O(1) amortised.
    ``current_count()`` : Same as ``check()`` without the insert.
    Space               : O(L) per endpoint where L = config.limit.

    Example::

        backend = RedisSlidingWindowBackend(redis.Redis())
        limiter = SlidingWindowLimiter(backend)
        result  = limiter.check("us-east-gateway")

        if result.decision is RateLimitDecision.DROPPED:
            drop_packet(packet)
    """

    DEFAULT_LIMIT: int = 100
    DEFAULT_WINDOW: float = 1.0

    def __init__(
        self,
        backend: Any,
        config: Optional[SlidingWindowConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or SlidingWindowConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        endpoint: str,
        *,
        mode: Optional[str] = None,
        defer_timeout: Optional[float] = None,
    ) -> RateLimitResult:
        """Evaluate whether a request from *endpoint* should be allowed.

        Args:
            endpoint:      Opaque identifier for the request source — an IP
                           address, service name, regional gateway tag, etc.
            mode:          Per-call override for the drop/defer policy.
            defer_timeout: Per-call override for the defer timeout (seconds).

        Returns:
            :class:`RateLimitResult` with the decision, live count, and
            a ``retry_after`` hint when the request is dropped.
        """
        effective_mode = mode if mode is not None else self._config.mode
        effective_timeout = (
            defer_timeout if defer_timeout is not None else self._config.defer_timeout
        )
        key = f"{self._config.key_prefix}:{endpoint}"

        allowed, count = self._attempt(key)
        if allowed:
            return RateLimitResult(
                decision=RateLimitDecision.ALLOWED,
                endpoint=endpoint,
                current_count=count,
                limit=self._config.limit,
                retry_after=None,
            )

        if effective_mode != "defer":
            logger.warning(
                "[SlidingWindowLimiter] DROPPED %s – %d/%d req in %.2fs window",
                endpoint,
                count,
                self._config.limit,
                self._config.window_seconds,
            )
            return RateLimitResult(
                decision=RateLimitDecision.DROPPED,
                endpoint=endpoint,
                current_count=count,
                limit=self._config.limit,
                retry_after=self._config.window_seconds,
            )

        # Defer mode: spin until a slot opens or timeout elapses.
        # Cap each sleep to the remaining time so large step values cannot
        # overshoot a short defer_timeout.
        sleep_step = self._config.window_seconds / max(self._config.limit, 1)
        deadline = time.monotonic() + effective_timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(sleep_step, remaining))
            allowed, count = self._attempt(key)
            if allowed:
                logger.info(
                    "[SlidingWindowLimiter] DEFERRED→ALLOWED %s after brief wait",
                    endpoint,
                )
                return RateLimitResult(
                    decision=RateLimitDecision.ALLOWED,
                    endpoint=endpoint,
                    current_count=count,
                    limit=self._config.limit,
                    retry_after=None,
                )

        logger.warning(
            "[SlidingWindowLimiter] DROPPED (defer timeout) %s – %d/%d",
            endpoint,
            count,
            self._config.limit,
        )
        return RateLimitResult(
            decision=RateLimitDecision.DROPPED,
            endpoint=endpoint,
            current_count=count,
            limit=self._config.limit,
            retry_after=self._config.window_seconds,
        )

    def current_count(self, endpoint: str) -> int:
        """Return the number of requests in the active window for *endpoint*."""
        key = f"{self._config.key_prefix}:{endpoint}"
        return self._backend.current_count(
            key, time.time(), self._config.window_seconds
        )

    def reset(self, endpoint: str) -> None:
        """Evict all tracked requests for *endpoint* from the window."""
        key = f"{self._config.key_prefix}:{endpoint}"
        self._backend.reset(key)
        logger.info("[SlidingWindowLimiter] Reset window for %s", endpoint)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _attempt(self, key: str) -> Tuple[bool, int]:
        uid = f"{time.time():.9f}-{next(_id_counter)}"
        return self._backend.check_and_record(
            key,
            time.time(),
            self._config.window_seconds,
            self._config.limit,
            uid,
        )


# Module-level singleton backed by InMemory for zero-config imports.
# Replace with RedisSlidingWindowBackend in production via dependency injection.
sliding_window_limiter = SlidingWindowLimiter(InMemorySlidingWindowBackend())

__all__ = [
    "TokenBucketConfig",
    "TokenBucketSnapshot",
    "TokenBucket",
    "TokenBucketController",
    "token_bucket_controller",
    "SlidingWindowConfig",
    "RateLimitDecision",
    "RateLimitResult",
    "InMemorySlidingWindowBackend",
    "RedisSlidingWindowBackend",
    "SlidingWindowLimiter",
    "sliding_window_limiter",
]
