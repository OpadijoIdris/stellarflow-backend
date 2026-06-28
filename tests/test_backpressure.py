from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

# Insert src before any stdlib import so our src/queue package takes precedence
# over the stdlib `queue` module. Purge any cached reference too.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.modules.pop("queue", None)

from queue.backpressure import (
    InMemorySlidingWindowBackend,
    RateLimitDecision,
    RateLimitResult,
    SlidingWindowConfig,
    SlidingWindowLimiter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_limiter(
    limit: int = 5,
    window_seconds: float = 1.0,
    mode: str = "drop",
    defer_timeout: float = 0.5,
) -> SlidingWindowLimiter:
    config = SlidingWindowConfig(
        limit=limit,
        window_seconds=window_seconds,
        mode=mode,
        defer_timeout=defer_timeout,
    )
    return SlidingWindowLimiter(InMemorySlidingWindowBackend(), config)


# ---------------------------------------------------------------------------
# Basic allow / drop
# ---------------------------------------------------------------------------


def test_requests_within_limit_are_allowed() -> None:
    limiter = make_limiter(limit=3)
    for _ in range(3):
        result = limiter.check("ep")
        assert result.decision is RateLimitDecision.ALLOWED


def test_request_at_limit_boundary_is_dropped() -> None:
    limiter = make_limiter(limit=3)
    for _ in range(3):
        limiter.check("ep")

    over = limiter.check("ep")
    assert over.decision is RateLimitDecision.DROPPED
    assert over.retry_after is not None and over.retry_after > 0


def test_result_carries_correct_endpoint_and_limit() -> None:
    limiter = make_limiter(limit=10)
    result = limiter.check("my-gateway")
    assert result.endpoint == "my-gateway"
    assert result.limit == 10
    assert result.current_count == 1
    assert result.retry_after is None


def test_current_count_increases_with_each_allowed_request() -> None:
    limiter = make_limiter(limit=5)
    for expected in range(1, 6):
        result = limiter.check("ep")
        assert result.current_count == expected


# ---------------------------------------------------------------------------
# 100 req/s default security limit
# ---------------------------------------------------------------------------


def test_default_config_enforces_100_requests_per_second() -> None:
    config = SlidingWindowConfig()
    assert config.limit == 100
    assert config.window_seconds == 1.0

    limiter = SlidingWindowLimiter(InMemorySlidingWindowBackend())
    allowed = sum(
        1
        for _ in range(110)
        if limiter.check("gw").decision is RateLimitDecision.ALLOWED
    )
    assert allowed == 100


# ---------------------------------------------------------------------------
# Sliding window expiry
# ---------------------------------------------------------------------------


def test_old_requests_expire_and_open_new_slots() -> None:
    limiter = make_limiter(limit=3, window_seconds=0.1)

    for _ in range(3):
        limiter.check("ep")

    # Window is full — next request must be dropped.
    assert limiter.check("ep").decision is RateLimitDecision.DROPPED

    # Wait for the window to roll past the initial 3 entries.
    time.sleep(0.15)

    fresh = limiter.check("ep")
    assert fresh.decision is RateLimitDecision.ALLOWED
    assert fresh.current_count == 1


def test_current_count_reflects_live_window_only() -> None:
    limiter = make_limiter(limit=5, window_seconds=0.1)
    for _ in range(5):
        limiter.check("ep")

    time.sleep(0.15)  # let the window expire

    assert limiter.current_count("ep") == 0


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_window_and_allows_fresh_requests() -> None:
    limiter = make_limiter(limit=2)
    limiter.check("ep")
    limiter.check("ep")

    assert limiter.check("ep").decision is RateLimitDecision.DROPPED

    limiter.reset("ep")

    assert limiter.check("ep").decision is RateLimitDecision.ALLOWED
    assert limiter.current_count("ep") == 1


def test_reset_does_not_affect_other_endpoints() -> None:
    limiter = make_limiter(limit=2)
    limiter.check("a")
    limiter.check("a")
    limiter.check("b")

    limiter.reset("a")

    assert limiter.check("a").decision is RateLimitDecision.ALLOWED
    # "b" still has one request recorded; second is allowed, third is dropped.
    assert limiter.check("b").decision is RateLimitDecision.ALLOWED
    assert limiter.check("b").decision is RateLimitDecision.DROPPED


# ---------------------------------------------------------------------------
# Endpoint isolation
# ---------------------------------------------------------------------------


def test_different_endpoints_have_independent_windows() -> None:
    limiter = make_limiter(limit=2)
    limiter.check("alpha")
    limiter.check("alpha")

    # "alpha" is at limit; "beta" is untouched.
    assert limiter.check("alpha").decision is RateLimitDecision.DROPPED
    assert limiter.check("beta").decision is RateLimitDecision.ALLOWED


def test_key_prefix_isolates_separate_limiter_instances() -> None:
    cfg_a = SlidingWindowConfig(limit=2, key_prefix="zone_a")
    cfg_b = SlidingWindowConfig(limit=2, key_prefix="zone_b")
    backend = InMemorySlidingWindowBackend()

    limiter_a = SlidingWindowLimiter(backend, cfg_a)
    limiter_b = SlidingWindowLimiter(backend, cfg_b)

    limiter_a.check("gw")
    limiter_a.check("gw")

    # zone_a/gw is full; zone_b/gw is untouched.
    assert limiter_a.check("gw").decision is RateLimitDecision.DROPPED
    assert limiter_b.check("gw").decision is RateLimitDecision.ALLOWED


# ---------------------------------------------------------------------------
# Defer mode
# ---------------------------------------------------------------------------


def test_defer_mode_returns_allowed_once_slot_opens() -> None:
    limiter = make_limiter(limit=2, window_seconds=0.1, mode="defer", defer_timeout=0.5)
    limiter.check("ep")
    limiter.check("ep")

    # Over limit; defer should spin through the 0.1 s window and succeed.
    result = limiter.check("ep")
    assert result.decision is RateLimitDecision.ALLOWED


def test_defer_mode_drops_when_timeout_exhausted() -> None:
    # Use a long window so the defer timeout elapses before any slot opens.
    limiter = make_limiter(
        limit=2, window_seconds=5.0, mode="defer", defer_timeout=0.05
    )
    limiter.check("ep")
    limiter.check("ep")

    result = limiter.check("ep")
    assert result.decision is RateLimitDecision.DROPPED
    assert result.retry_after is not None


def test_drop_mode_per_call_override() -> None:
    limiter = make_limiter(limit=1, mode="defer", defer_timeout=5.0)
    limiter.check("ep")

    # Override to drop even though the instance default is defer.
    result = limiter.check("ep", mode="drop")
    assert result.decision is RateLimitDecision.DROPPED


def test_defer_timeout_per_call_override_drops_fast() -> None:
    limiter = make_limiter(limit=2, window_seconds=5.0, mode="defer", defer_timeout=5.0)
    limiter.check("ep")
    limiter.check("ep")

    start = time.monotonic()
    result = limiter.check("ep", defer_timeout=0.05)
    elapsed = time.monotonic() - start

    assert result.decision is RateLimitDecision.DROPPED
    assert elapsed < 1.0  # confirmed fast failure


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_requests_respect_limit_under_pressure() -> None:
    limit = 50
    limiter = make_limiter(limit=limit)
    decisions: list[RateLimitDecision] = []
    lock = threading.Lock()

    def worker(_: int) -> None:
        result = limiter.check("shared-gateway")
        with lock:
            decisions.append(result.decision)

    with ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(worker, range(100)))

    allowed = decisions.count(RateLimitDecision.ALLOWED)
    dropped = decisions.count(RateLimitDecision.DROPPED)

    assert allowed == limit, f"Expected exactly {limit} allowed, got {allowed}"
    assert dropped == 100 - limit


def test_concurrent_requests_on_independent_endpoints_no_interference() -> None:
    limit = 10
    limiter = make_limiter(limit=limit)
    results: dict[str, list[RateLimitDecision]] = {}
    results_lock = threading.Lock()

    endpoints = [f"ep-{i}" for i in range(5)]

    def worker(endpoint: str) -> None:
        decision = limiter.check(endpoint).decision
        with results_lock:
            results.setdefault(endpoint, []).append(decision)

    tasks = [ep for ep in endpoints for _ in range(15)]
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(worker, ep) for ep in tasks]
        for f in as_completed(futures):
            f.result()

    for ep, decs in results.items():
        allowed = decs.count(RateLimitDecision.ALLOWED)
        assert allowed == limit, f"{ep}: expected {limit} allowed, got {allowed}"


# ---------------------------------------------------------------------------
# InMemorySlidingWindowBackend unit tests
# ---------------------------------------------------------------------------


def test_in_memory_backend_check_and_record_allows_up_to_limit() -> None:
    backend = InMemorySlidingWindowBackend()
    now = time.time()

    for i in range(5):
        allowed, count = backend.check_and_record("k", now, 1.0, 5, str(i))
        assert allowed is True
        assert count == i + 1

    allowed, count = backend.check_and_record("k", now, 1.0, 5, "over")
    assert allowed is False
    assert count == 5


def test_in_memory_backend_prunes_expired_entries() -> None:
    backend = InMemorySlidingWindowBackend()
    old = time.time() - 2.0  # 2 seconds ago

    backend.check_and_record("k", old, 1.0, 5, "old-1")
    backend.check_and_record("k", old, 1.0, 5, "old-2")

    now = time.time()
    allowed, count = backend.check_and_record("k", now, 1.0, 5, "new-1")
    assert allowed is True
    assert count == 1  # old entries pruned; only the new one remains


def test_in_memory_backend_current_count_excludes_expired() -> None:
    backend = InMemorySlidingWindowBackend()
    old = time.time() - 2.0

    backend.check_and_record("k", old, 1.0, 10, "a")
    backend.check_and_record("k", old, 1.0, 10, "b")

    count = backend.current_count("k", time.time(), 1.0)
    assert count == 0


def test_in_memory_backend_reset_clears_key() -> None:
    backend = InMemorySlidingWindowBackend()
    now = time.time()

    for i in range(3):
        backend.check_and_record("k", now, 1.0, 5, str(i))

    backend.reset("k")

    assert backend.current_count("k", now, 1.0) == 0
    allowed, _ = backend.check_and_record("k", now, 1.0, 5, "fresh")
    assert allowed is True


# ---------------------------------------------------------------------------
# SlidingWindowConfig defaults
# ---------------------------------------------------------------------------


def test_sliding_window_config_defaults_match_security_spec() -> None:
    cfg = SlidingWindowConfig()
    assert cfg.limit == 100
    assert cfg.window_seconds == 1.0
    assert cfg.mode == "drop"
    assert cfg.key_prefix == "rate_limit"
