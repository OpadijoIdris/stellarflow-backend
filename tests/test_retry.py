"""tests/test_retry.py - Comprehensive test suite for network retry controller.

Tests cover:
* Thread isolation and concurrent execution
* Exponential backoff calculation
* Full-jitter randomization
* Retry budget exhaustion
* Context manager behavior
* Decorator usage
* Edge cases and error conditions
"""

from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List
from unittest.mock import MagicMock, patch

import pytest

# Bootstrap path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from network.retry import (
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_INITIAL_DELAY,
    DEFAULT_MAX_DELAY,
    DEFAULT_MAX_RETRIES,
    RetryBudgetExhausted,
    RetryController,
    with_retry,
)


# ---------------------------------------------------------------------------
# Construction & Validation Tests
# ---------------------------------------------------------------------------


class TestConstruction:
    """Tests for RetryController construction and parameter validation."""
    
    def test_default_construction(self):
        """Verify default parameters are applied correctly."""
        controller = RetryController()
        assert controller.max_retries == DEFAULT_MAX_RETRIES
        assert controller.initial_delay == DEFAULT_INITIAL_DELAY
        assert controller.max_delay == DEFAULT_MAX_DELAY
        assert controller.backoff_factor == DEFAULT_BACKOFF_FACTOR
    
    def test_custom_parameters(self):
        """Verify custom parameters are stored correctly."""
        controller = RetryController(
            max_retries=5,
            initial_delay=2.0,
            max_delay=60.0,
            backoff_factor=3.0,
        )
        assert controller.max_retries == 5
        assert controller.initial_delay == 2.0
        assert controller.max_delay == 60.0
        assert controller.backoff_factor == 3.0
    
    def test_rejects_negative_max_retries(self):
        """Negative max_retries should raise ValueError."""
        with pytest.raises(ValueError, match="non-negative"):
            RetryController(max_retries=-1)
    
    def test_accepts_zero_max_retries(self):
        """Zero max_retries should be valid (no retries)."""
        controller = RetryController(max_retries=0)
        assert controller.max_retries == 0
    
    def test_rejects_negative_initial_delay(self):
        """Negative initial_delay should raise ValueError."""
        with pytest.raises(ValueError, match="positive"):
            RetryController(initial_delay=-0.5)
    
    def test_rejects_zero_initial_delay(self):
        """Zero initial_delay should raise ValueError."""
        with pytest.raises(ValueError, match="positive"):
            RetryController(initial_delay=0.0)
    
    def test_rejects_max_delay_less_than_initial_delay(self):
        """max_delay < initial_delay should raise ValueError."""
        with pytest.raises(ValueError, match="max_delay.*must be >= initial_delay"):
            RetryController(initial_delay=10.0, max_delay=5.0)
    
    def test_accepts_equal_delays(self):
        """max_delay == initial_delay should be valid."""
        controller = RetryController(initial_delay=5.0, max_delay=5.0)
        assert controller.initial_delay == 5.0
        assert controller.max_delay == 5.0
    
    def test_rejects_backoff_factor_less_than_one(self):
        """backoff_factor < 1.0 should raise ValueError."""
        with pytest.raises(ValueError, match="backoff_factor must be >= 1.0"):
            RetryController(backoff_factor=0.5)
    
    def test_accepts_backoff_factor_one(self):
        """backoff_factor == 1.0 should be valid (constant delay)."""
        controller = RetryController(backoff_factor=1.0)
        assert controller.backoff_factor == 1.0


# ---------------------------------------------------------------------------
# Basic Retry Logic Tests
# ---------------------------------------------------------------------------


class TestBasicRetry:
    """Tests for basic retry iteration and attempt counting."""
    
    def test_first_attempt_is_zero(self):
        """First attempt should be numbered 0."""
        controller = RetryController(max_retries=0)
        attempts = []
        
        try:
            for attempt in controller.attempts():
                attempts.append(attempt)
        except RetryBudgetExhausted:
            pass
        
        assert len(attempts) == 1
        assert attempts[0] == 0
    
    def test_retry_count_matches_max_retries(self):
        """Should generate max_retries + 1 total attempts."""
        controller = RetryController(max_retries=3)
        attempts = []
        
        try:
            for attempt in controller.attempts():
                attempts.append(attempt)
        except RetryBudgetExhausted:
            pass
        
        # 3 retries + 1 initial attempt = 4 total
        assert attempts == [0, 1, 2, 3]
    
    def test_exhausted_raises_exception(self):
        """Should raise RetryBudgetExhausted when retries depleted."""
        controller = RetryController(max_retries=0)
        
        with pytest.raises(RetryBudgetExhausted, match="Exhausted"):
            for _ in controller.attempts():
                pass  # Will raise after first attempt
    
    def test_early_break_does_not_raise(self):
        """Breaking out early should not raise RetryBudgetExhausted."""
        controller = RetryController(max_retries=5)
        
        count = 0
        for attempt in controller.attempts():
            count += 1
            if attempt == 2:
                break
        
        assert count == 3  # Attempts 0, 1, 2


# ---------------------------------------------------------------------------
# Delay & Backoff Tests
# ---------------------------------------------------------------------------


class TestDelayCalculation:
    """Tests for exponential backoff and jitter calculation."""
    
    def test_no_delay_on_first_attempt(self):
        """First attempt (0) should have no delay."""
        controller = RetryController(initial_delay=1.0)
        start = time.time()
        
        for attempt in controller.attempts():
            if attempt == 0:
                elapsed = time.time() - start
                # Should be nearly instant (< 100ms)
                assert elapsed < 0.1
                break
    
    def test_delay_increases_exponentially(self):
        """Delays should follow exponential backoff pattern."""
        controller = RetryController(
            max_retries=3,
            initial_delay=0.1,
            max_delay=10.0,
            backoff_factor=2.0,
        )
        
        delays: List[float] = []
        prev_time = time.time()
        
        try:
            for attempt in controller.attempts():
                if attempt > 0:
                    current_time = time.time()
                    delay = current_time - prev_time
                    delays.append(delay)
                    prev_time = current_time
                
                if attempt == 3:
                    break
        except RetryBudgetExhausted:
            pass
        
        # Each delay should be roughly >= previous delay (with jitter variance)
        # At minimum, the maximum delay should be larger than the first
        assert len(delays) >= 2
        assert max(delays) > min(delays)
    
    def test_delay_respects_max_cap(self):
        """Delays should never exceed max_delay."""
        controller = RetryController(
            max_retries=10,
            initial_delay=1.0,
            max_delay=2.0,
            backoff_factor=10.0,  # Aggressive growth
        )
        
        # Manually test delay calculation
        for attempt in range(1, 11):
            delay = controller._calculate_delay(attempt)
            assert delay <= 2.0, f"Delay {delay} exceeds max_delay on attempt {attempt}"
    
    def test_jitter_produces_varied_delays(self):
        """Full-jitter should produce different delays for same attempt."""
        controller = RetryController(initial_delay=1.0, max_delay=5.0)
        
        delays = [controller._calculate_delay(1) for _ in range(100)]
        
        # With full-jitter, we should see variety in delays
        unique_delays = len(set(delays))
        assert unique_delays > 50  # Expect most to be unique
    
    def test_jitter_stays_within_bounds(self):
        """Jittered delays should be in [0, capped_delay] range."""
        controller = RetryController(
            initial_delay=1.0,
            max_delay=10.0,
            backoff_factor=2.0,
        )
        
        for attempt in range(1, 6):
            for _ in range(20):  # Test multiple times
                delay = controller._calculate_delay(attempt)
                
                # Calculate expected upper bound
                base_delay = 1.0 * (2.0 ** (attempt - 1))
                capped_delay = min(base_delay, 10.0)
                
                assert 0.0 <= delay <= capped_delay


# ---------------------------------------------------------------------------
# Thread Isolation Tests
# ---------------------------------------------------------------------------


class TestThreadIsolation:
    """Tests for thread-local state isolation."""
    
    def test_different_threads_have_independent_state(self):
        """Each thread should maintain separate retry state."""
        controller = RetryController(max_retries=5)
        results = {}
        
        def worker(thread_id: int) -> None:
            controller.reset()
            count = 0
            
            for attempt in controller.attempts():
                count += 1
                if attempt == thread_id:  # Each thread stops at different point
                    break
            
            results[thread_id] = count
        
        threads = []
        for i in range(3):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join()
        
        # Each thread should have stopped at different attempt counts
        assert results[0] == 1  # Stopped at attempt 0
        assert results[1] == 2  # Stopped at attempt 1
        assert results[2] == 3  # Stopped at attempt 2
    
    def test_concurrent_retry_loops_do_not_interfere(self):
        """Multiple threads running full retry loops should not interfere."""
        controller = RetryController(max_retries=2)
        success_counts = []
        
        def worker() -> None:
            count = 0
            try:
                for _ in controller.attempts():
                    count += 1
            except RetryBudgetExhausted:
                pass
            success_counts.append(count)
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker) for _ in range(10)]
            for future in as_completed(futures):
                future.result()
        
        # All threads should have completed same number of attempts
        assert len(success_counts) == 10
        assert all(count == 3 for count in success_counts)  # 2 retries + 1 initial = 3
    
    def test_reset_only_affects_current_thread(self):
        """reset() should only clear state for calling thread."""
        controller = RetryController(max_retries=3)
        barrier = threading.Barrier(2)
        results = {}
        
        def thread1() -> None:
            # Advance to attempt 2
            for attempt in controller.attempts():
                if attempt == 2:
                    results["t1_before_reset"] = controller.get_stats()["attempt"]
                    barrier.wait()  # Sync with thread2
                    barrier.wait()  # Wait for thread2 to check
                    break
        
        def thread2() -> None:
            # Start iteration
            for attempt in controller.attempts():
                if attempt == 1:
                    barrier.wait()  # Sync with thread1
                    # Thread1 is at attempt 2, we're at attempt 1
                    results["t2_before_reset"] = controller.get_stats()["attempt"]
                    controller.reset()
                    results["t2_after_reset"] = controller.get_stats()["attempt"]
                    barrier.wait()  # Signal thread1
                    break
        
        t1 = threading.Thread(target=thread1)
        t2 = threading.Thread(target=thread2)
        
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        # Thread1 should be at attempt 3 (index 2 + 1 after yield)
        assert results["t1_before_reset"] == 3
        # Thread2 should be at attempt 2 before reset
        assert results["t2_before_reset"] == 2
        # Thread2 should be at attempt 0 after reset
        assert results["t2_after_reset"] == 0


# ---------------------------------------------------------------------------
# should_retry Tests
# ---------------------------------------------------------------------------


class TestShouldRetry:
    """Tests for should_retry logic."""
    
    def test_should_retry_returns_true_by_default(self):
        """Default should_retry should return True for any exception."""
        controller = RetryController()
        
        assert controller.should_retry(ValueError("test"))
        assert controller.should_retry(RuntimeError("test"))
        assert controller.should_retry(Exception("test"))
    
    def test_should_retry_returns_false_when_exhausted(self):
        """should_retry should return False when budget exhausted."""
        controller = RetryController(max_retries=1)
        
        try:
            for _ in controller.attempts():
                pass
        except RetryBudgetExhausted:
            pass
        
        # Budget is now exhausted
        assert not controller.should_retry(Exception("test"))
    
    def test_should_retry_respects_max_retries(self):
        """should_retry should return False after max_retries exceeded."""
        controller = RetryController(max_retries=2)
        
        # Manually advance attempt counter past limit
        state = controller._get_state()
        state.attempt = 3  # Beyond max_retries
        
        assert not controller.should_retry(Exception("test"))


# ---------------------------------------------------------------------------
# Statistics & Observability Tests
# ---------------------------------------------------------------------------


class TestStatistics:
    """Tests for get_stats and observability features."""
    
    def test_get_stats_initial_state(self):
        """Stats should reflect initial state before any attempts."""
        controller = RetryController(max_retries=5)
        stats = controller.get_stats()
        
        assert stats["attempt"] == 0
        assert stats["total_delay"] == 0.0
        assert stats["exhausted"] is False
        assert stats["max_retries"] == 5
        assert "thread_name" in stats
    
    def test_get_stats_tracks_attempts(self):
        """Stats should update as attempts progress."""
        controller = RetryController(max_retries=3, initial_delay=0.01)
        
        for attempt in controller.attempts():
            stats = controller.get_stats()
            # attempt field increments after yield
            if attempt == 1:
                assert stats["attempt"] == 2
                break
    
    def test_get_stats_tracks_total_delay(self):
        """Stats should accumulate total delay time."""
        controller = RetryController(
            max_retries=2,
            initial_delay=0.05,
            max_delay=0.1,
        )
        
        for attempt in controller.attempts():
            if attempt == 2:
                stats = controller.get_stats()
                # Should have accumulated some delay
                assert stats["total_delay"] > 0
                break
    
    def test_get_stats_marks_exhausted(self):
        """Stats should reflect exhausted state after budget depleted."""
        controller = RetryController(max_retries=0)
        
        try:
            for _ in controller.attempts():
                pass
        except RetryBudgetExhausted:
            pass
        
        stats = controller.get_stats()
        assert stats["exhausted"] is True


# ---------------------------------------------------------------------------
# Context Manager Tests
# ---------------------------------------------------------------------------


class TestContextManager:
    """Tests for context manager protocol."""
    
    def test_context_manager_resets_on_enter(self):
        """Entering context should reset state."""
        controller = RetryController(max_retries=1)
        
        # Advance state
        for attempt in controller.attempts():
            if attempt == 1:
                break
        
        assert controller.get_stats()["attempt"] > 0
        
        # Enter new context
        with controller:
            stats = controller.get_stats()
            assert stats["attempt"] == 0
            assert stats["total_delay"] == 0.0
    
    def test_context_manager_does_not_suppress_exceptions(self):
        """Context manager should not suppress exceptions."""
        controller = RetryController()
        
        with pytest.raises(ValueError, match="test error"):
            with controller:
                raise ValueError("test error")
    
    def test_context_manager_logs_final_stats(self, caplog):
        """Context exit should log final statistics."""
        import logging
        
        controller = RetryController(max_retries=1, initial_delay=0.01)
        
        with caplog.at_level(logging.DEBUG, logger="network.retry"):
            with controller:
                for attempt in controller.attempts():
                    if attempt == 1:
                        break
        
        # Should have logged context exit
        assert any("Context exit" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Decorator Tests
# ---------------------------------------------------------------------------


class TestDecorator:
    """Tests for @with_retry decorator."""
    
    def test_decorator_retries_on_failure(self):
        """Decorated function should retry on exceptions."""
        call_count = 0
        
        @with_retry(max_retries=3, initial_delay=0.01)
        def flaky_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("transient error")
            return "success"
        
        result = flaky_function()
        assert result == "success"
        assert call_count == 3
    
    def test_decorator_respects_retry_limit(self):
        """Decorator should raise after exhausting retries."""
        call_count = 0
        
        @with_retry(max_retries=2, initial_delay=0.01)
        def always_fails():
            nonlocal call_count
            call_count += 1
            raise ValueError("persistent error")
        
        with pytest.raises(ValueError, match="persistent error"):
            always_fails()
        
        # 2 retries + 1 initial = 3 calls
        assert call_count == 3
    
    def test_decorator_only_retries_specified_exceptions(self):
        """Decorator should only retry listed exception types."""
        call_count = 0
        
        @with_retry(
            max_retries=3,
            initial_delay=0.01,
            retryable_exceptions=(ValueError,)
        )
        def mixed_failures():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("retryable")
            raise TypeError("not retryable")
        
        with pytest.raises(TypeError, match="not retryable"):
            mixed_failures()
        
        # Should have made 2 calls: 1st raised ValueError (retried), 2nd raised TypeError (not retried)
        assert call_count == 2
    
    def test_decorator_preserves_function_metadata(self):
        """Decorator should preserve original function name and docstring."""
        @with_retry()
        def my_function():
            """This is a docstring."""
            pass
        
        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "This is a docstring."


# ---------------------------------------------------------------------------
# Edge Cases & Integration Tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and unusual scenarios."""
    
    def test_zero_max_retries_only_tries_once(self):
        """max_retries=0 should only allow one attempt."""
        controller = RetryController(max_retries=0)
        count = 0
        
        try:
            for _ in controller.attempts():
                count += 1
        except RetryBudgetExhausted:
            pass
        
        assert count == 1
    
    def test_very_large_max_retries(self):
        """Should handle very large max_retries values."""
        controller = RetryController(max_retries=1000)
        
        # Just verify construction and early break works
        for attempt in controller.attempts():
            if attempt == 5:
                break
        
        assert controller.get_stats()["attempt"] == 6
    
    def test_backoff_factor_one_produces_constant_delay(self):
        """backoff_factor=1.0 should produce roughly constant delays."""
        controller = RetryController(
            max_retries=5,
            initial_delay=0.5,
            max_delay=0.5,
            backoff_factor=1.0,
        )
        
        # All delays should be uniform [0, 0.5] with same distribution
        delays = [controller._calculate_delay(i) for i in range(1, 6)]
        
        # All should be within [0, 0.5]
        assert all(0 <= d <= 0.5 for d in delays)
    
    def test_multiple_sequential_retry_sequences(self):
        """Should support multiple retry sequences in same thread."""
        controller = RetryController(max_retries=2, initial_delay=0.01)
        
        # First sequence
        count1 = 0
        for attempt in controller.attempts():
            count1 += 1
            if attempt == 1:
                break
        
        # Reset for second sequence
        controller.reset()
        
        # Second sequence
        count2 = 0
        for attempt in controller.attempts():
            count2 += 1
            if attempt == 1:
                break
        
        assert count1 == 2
        assert count2 == 2
    
    def test_handles_extremely_short_delays(self):
        """Should handle very short delay values without errors."""
        controller = RetryController(
            max_retries=3,
            initial_delay=0.001,
            max_delay=0.01,
        )
        
        count = 0
        for attempt in controller.attempts():
            count += 1
            if attempt == 2:
                break
        
        assert count == 3


# ---------------------------------------------------------------------------
# Performance & Stress Tests
# ---------------------------------------------------------------------------


class TestPerformance:
    """Performance and stress tests."""
    
    def test_high_concurrency_stress(self):
        """Should handle many concurrent threads without errors."""
        controller = RetryController(max_retries=3, initial_delay=0.001)
        results = []
        
        def worker() -> int:
            count = 0
            try:
                for _ in controller.attempts():
                    count += 1
            except RetryBudgetExhausted:
                pass
            return count
        
        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(worker) for _ in range(100)]
            for future in as_completed(futures):
                results.append(future.result())
        
        # All threads should complete successfully
        assert len(results) == 100
        assert all(r == 4 for r in results)  # 3 retries + 1 initial
    
    def test_rapid_reset_cycles(self):
        """Should handle rapid reset() calls without issues."""
        controller = RetryController(max_retries=2)
        
        for _ in range(100):
            controller.reset()
            stats = controller.get_stats()
            assert stats["attempt"] == 0
            assert stats["total_delay"] == 0.0
            assert stats["exhausted"] is False


# ---------------------------------------------------------------------------
# Logging Tests
# ---------------------------------------------------------------------------


class TestLogging:
    """Tests for logging behavior."""
    
    def test_logs_retry_attempts(self, caplog):
        """Should log each retry attempt with metadata."""
        import logging
        
        controller = RetryController(max_retries=2, initial_delay=0.01)
        
        with caplog.at_level(logging.INFO, logger="network.retry"):
            for attempt in controller.attempts():
                if attempt == 1:
                    break
        
        # Should have logged the retry
        retry_logs = [r for r in caplog.records if "Retry attempt" in r.message]
        assert len(retry_logs) == 1
        assert "delay=" in retry_logs[0].message
    
    def test_logs_budget_exhaustion(self, caplog):
        """Should log when retry budget is exhausted."""
        import logging
        
        controller = RetryController(max_retries=1, initial_delay=0.01)
        
        with caplog.at_level(logging.WARNING, logger="network.retry"):
            try:
                for _ in controller.attempts():
                    pass
            except RetryBudgetExhausted:
                pass
        
        # Should have logged exhaustion
        exhausted_logs = [r for r in caplog.records if "exhausted" in r.message.lower()]
        assert len(exhausted_logs) > 0
