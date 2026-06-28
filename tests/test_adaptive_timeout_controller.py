"""
Tests for AdaptiveTimeoutController
====================================
Property-based and unit tests that verify the adaptive timeout calculation,
boundary enforcement, rolling latency tracking, and thread-safety guarantees.

Run with::

    pytest tests/test_adaptive_timeout_controller.py -v
"""
from __future__ import annotations

import os
import sys
import threading
from typing import List

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database.connection import (
    AdaptiveTimeoutController,
    DEFAULT_BASE_TIMEOUT_S,
    DEFAULT_CONNECTION_COEFFICIENT,
    DEFAULT_LATENCY_COEFFICIENT,
    DEFAULT_MAX_TIMEOUT_S,
    DEFAULT_MIN_TIMEOUT_S,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctrl(**kwargs) -> AdaptiveTimeoutController:
    """Return a controller, forwarding any keyword overrides."""
    return AdaptiveTimeoutController(**kwargs)


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_defaults_are_accepted(self):
        ctrl = AdaptiveTimeoutController()
        # spot-check that defaults survive construction
        assert ctrl.calculate_timeout(0, 0.0) == pytest.approx(
            max(DEFAULT_MIN_TIMEOUT_S, DEFAULT_BASE_TIMEOUT_S), rel=1e-6
        )

    @pytest.mark.parametrize(
        "kwargs,error_fragment",
        [
            ({"base_timeout_s": 0}, "base_timeout_s"),
            ({"base_timeout_s": -1}, "base_timeout_s"),
            ({"latency_coefficient": -0.1}, "latency_coefficient"),
            ({"connection_coefficient": -1.0}, "connection_coefficient"),
            ({"min_timeout_s": 0}, "min_timeout_s"),
            ({"min_timeout_s": -5}, "min_timeout_s"),
            ({"max_timeout_s": 0}, "max_timeout_s"),
            ({"min_timeout_s": 30.0, "max_timeout_s": 10.0}, "min_timeout_s"),
            ({"latency_window": 0}, "latency_window"),
            ({"latency_window": -1}, "latency_window"),
        ],
    )
    def test_invalid_construction_raises_value_error(self, kwargs, error_fragment):
        with pytest.raises(ValueError, match=error_fragment):
            AdaptiveTimeoutController(**kwargs)


# ---------------------------------------------------------------------------
# calculate_timeout: baseline behaviour
# ---------------------------------------------------------------------------


class TestCalculateTimeoutBaseline:
    def test_zero_load_returns_base_clamped_to_min(self):
        """With no connections and no latency the output equals the base (if
        above min) or min_timeout_s."""
        ctrl = _ctrl(base_timeout_s=5.0, min_timeout_s=2.0, max_timeout_s=60.0)
        result = ctrl.calculate_timeout(active_connections=0, latency_ms=0.0)
        assert result == pytest.approx(5.0, rel=1e-6)

    def test_base_below_min_is_raised_to_min(self):
        ctrl = _ctrl(
            base_timeout_s=1.0,
            min_timeout_s=3.0,
            max_timeout_s=60.0,
            latency_coefficient=0.0,
            connection_coefficient=0.0,
        )
        result = ctrl.calculate_timeout(active_connections=0, latency_ms=0.0)
        assert result == pytest.approx(3.0, rel=1e-6)

    def test_large_inputs_capped_at_max(self):
        ctrl = _ctrl(
            base_timeout_s=5.0,
            latency_coefficient=1.0,  # very aggressive scaling
            connection_coefficient=1.0,
            max_timeout_s=30.0,
        )
        # 5 + 1*10000 + 1*10000 would be 20005 — must be capped at 30
        result = ctrl.calculate_timeout(active_connections=10_000, latency_ms=10_000.0)
        assert result == pytest.approx(30.0, rel=1e-6)

    def test_result_always_within_bounds(self):
        """Property: output ∈ [min_timeout_s, max_timeout_s] for all inputs."""
        ctrl = _ctrl(min_timeout_s=2.0, max_timeout_s=45.0)
        test_cases = [
            (0, 0.0),
            (1, 10.0),
            (50, 100.0),
            (200, 500.0),
            (10_000, 50_000.0),
        ]
        for conns, lat in test_cases:
            t = ctrl.calculate_timeout(active_connections=conns, latency_ms=lat)
            assert 2.0 <= t <= 45.0, (
                f"timeout {t} out of bounds for conns={conns}, latency={lat}"
            )


# ---------------------------------------------------------------------------
# calculate_timeout: formula correctness
# ---------------------------------------------------------------------------


class TestCalculateTimeoutFormula:
    def test_latency_contribution_is_proportional(self):
        """Doubling latency doubles the latency contribution term."""
        ctrl = _ctrl(
            base_timeout_s=5.0,
            latency_coefficient=0.01,
            connection_coefficient=0.0,
            min_timeout_s=0.1,
            max_timeout_s=999.0,
        )
        t_100 = ctrl.calculate_timeout(active_connections=0, latency_ms=100.0)
        t_200 = ctrl.calculate_timeout(active_connections=0, latency_ms=200.0)
        # t_100 = 5 + 0.01*100 = 6.0
        # t_200 = 5 + 0.01*200 = 7.0
        # difference = latency_coefficient * (200 - 100) = 1.0
        assert (t_200 - t_100) == pytest.approx(0.01 * 100.0, rel=1e-6)

    def test_connection_contribution_is_proportional(self):
        """Doubling active connections doubles the connection contribution term."""
        ctrl = _ctrl(
            base_timeout_s=5.0,
            latency_coefficient=0.0,
            connection_coefficient=0.1,
            min_timeout_s=0.1,
            max_timeout_s=999.0,
        )
        t_10 = ctrl.calculate_timeout(active_connections=10, latency_ms=0.0)
        t_20 = ctrl.calculate_timeout(active_connections=20, latency_ms=0.0)
        assert (t_20 - t_10) == pytest.approx(0.1 * 10, rel=1e-6)

    def test_both_contributions_sum_correctly(self):
        ctrl = _ctrl(
            base_timeout_s=3.0,
            latency_coefficient=0.002,
            connection_coefficient=0.05,
            min_timeout_s=0.1,
            max_timeout_s=999.0,
        )
        # raw = 3.0 + 0.002*250 + 0.05*40 = 3.0 + 0.5 + 2.0 = 5.5
        result = ctrl.calculate_timeout(active_connections=40, latency_ms=250.0)
        assert result == pytest.approx(5.5, rel=1e-6)

    def test_zero_coefficients_always_returns_base(self):
        ctrl = _ctrl(
            base_timeout_s=7.0,
            latency_coefficient=0.0,
            connection_coefficient=0.0,
            min_timeout_s=1.0,
            max_timeout_s=100.0,
        )
        for conns, lat in [(0, 0.0), (100, 1000.0), (500, 5000.0)]:
            assert ctrl.calculate_timeout(conns, lat) == pytest.approx(7.0, rel=1e-6)


# ---------------------------------------------------------------------------
# calculate_timeout: negative input rejection
# ---------------------------------------------------------------------------


class TestCalculateTimeoutInputValidation:
    def test_negative_connections_raises(self):
        ctrl = AdaptiveTimeoutController()
        with pytest.raises(ValueError, match="active_connections"):
            ctrl.calculate_timeout(active_connections=-1, latency_ms=10.0)

    def test_negative_latency_raises(self):
        ctrl = AdaptiveTimeoutController()
        with pytest.raises(ValueError, match="latency_ms"):
            ctrl.calculate_timeout(active_connections=5, latency_ms=-0.1)

    def test_zero_inputs_are_valid(self):
        ctrl = AdaptiveTimeoutController()
        result = ctrl.calculate_timeout(active_connections=0, latency_ms=0.0)
        assert result >= DEFAULT_MIN_TIMEOUT_S


# ---------------------------------------------------------------------------
# Rolling latency tracking
# ---------------------------------------------------------------------------


class TestRollingLatencyTracking:
    def test_average_is_zero_before_any_samples(self):
        ctrl = AdaptiveTimeoutController()
        assert ctrl.average_latency_ms() == pytest.approx(0.0)

    def test_sample_count_zero_before_recording(self):
        ctrl = AdaptiveTimeoutController()
        assert ctrl.sample_count() == 0

    def test_single_sample_average_equals_sample(self):
        ctrl = AdaptiveTimeoutController()
        ctrl.record_latency(42.0)
        assert ctrl.average_latency_ms() == pytest.approx(42.0)
        assert ctrl.sample_count() == 1

    def test_average_across_multiple_samples(self):
        ctrl = AdaptiveTimeoutController()
        for v in [10.0, 20.0, 30.0]:
            ctrl.record_latency(v)
        assert ctrl.average_latency_ms() == pytest.approx(20.0)

    def test_window_evicts_oldest_when_full(self):
        """When the window is full, old samples are evicted so the average
        reflects only the most recent ``latency_window`` values."""
        ctrl = _ctrl(latency_window=3)
        for v in [100.0, 200.0, 300.0]:
            ctrl.record_latency(v)
        # window = [100, 200, 300]; avg = 200
        assert ctrl.average_latency_ms() == pytest.approx(200.0)

        ctrl.record_latency(400.0)
        # window = [200, 300, 400]; avg = 300
        assert ctrl.average_latency_ms() == pytest.approx(300.0)
        assert ctrl.sample_count() == 3  # still bounded at 3

    def test_negative_sample_raises(self):
        ctrl = AdaptiveTimeoutController()
        with pytest.raises(ValueError, match="latency_ms"):
            ctrl.record_latency(-1.0)

    def test_zero_latency_sample_accepted(self):
        ctrl = AdaptiveTimeoutController()
        ctrl.record_latency(0.0)
        assert ctrl.average_latency_ms() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# timeout_from_average convenience method
# ---------------------------------------------------------------------------


class TestTimeoutFromAverage:
    def test_no_samples_uses_zero_latency(self):
        """With no recorded samples average=0.0, so result == base clamped."""
        ctrl = _ctrl(
            base_timeout_s=5.0,
            latency_coefficient=0.01,
            connection_coefficient=0.0,
            min_timeout_s=2.0,
            max_timeout_s=60.0,
        )
        result = ctrl.timeout_from_average(active_connections=0)
        assert result == pytest.approx(5.0)

    def test_matches_manual_calculate_call(self):
        ctrl = AdaptiveTimeoutController()
        samples = [50.0, 100.0, 150.0]
        for s in samples:
            ctrl.record_latency(s)
        avg = ctrl.average_latency_ms()
        conns = 25
        expected = ctrl.calculate_timeout(active_connections=conns, latency_ms=avg)
        assert ctrl.timeout_from_average(active_connections=conns) == pytest.approx(expected)

    def test_result_within_bounds(self):
        ctrl = _ctrl(min_timeout_s=3.0, max_timeout_s=20.0)
        for lat in [0.0, 500.0, 5000.0]:
            ctrl.record_latency(lat)
        result = ctrl.timeout_from_average(active_connections=100)
        assert 3.0 <= result <= 20.0


# ---------------------------------------------------------------------------
# Thread-safety property tests
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_record_latency_does_not_raise_or_corrupt(self):
        """Multiple threads recording latency simultaneously must not raise or
        corrupt the sample deque."""
        ctrl = _ctrl(latency_window=200)
        errors: List[Exception] = []

        def worker(start_val: float) -> None:
            try:
                for i in range(50):
                    ctrl.record_latency(start_val + i)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(float(t * 100),)) for t in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors, f"Thread errors: {errors}"
        # 4 threads × 50 samples, window=200 → count exactly 200
        assert ctrl.sample_count() == 200

    def test_concurrent_calculate_timeout_is_consistent(self):
        """calculate_timeout must be callable from many threads simultaneously
        and always return a value within the configured bounds."""
        ctrl = _ctrl(min_timeout_s=2.0, max_timeout_s=30.0)
        results: List[float] = []
        lock = threading.Lock()

        def worker() -> None:
            for conns, lat in [(0, 0.0), (10, 50.0), (50, 200.0), (100, 800.0)]:
                t = ctrl.calculate_timeout(active_connections=conns, latency_ms=lat)
                with lock:
                    results.append(t)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        for t in results:
            assert 2.0 <= t <= 30.0, f"Out-of-bounds timeout: {t}"

    def test_mixed_record_and_average_from_multiple_threads(self):
        """Interleaved record_latency and average_latency_ms calls must not
        raise or deadlock."""
        ctrl = AdaptiveTimeoutController()
        errors: List[Exception] = []
        done = threading.Event()

        def recorder() -> None:
            try:
                for i in range(100):
                    ctrl.record_latency(float(i))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                done.set()

        def reader() -> None:
            while not done.is_set():
                try:
                    ctrl.average_latency_ms()
                    ctrl.sample_count()
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

        writer = threading.Thread(target=recorder)
        readers = [threading.Thread(target=reader) for _ in range(3)]
        for th in readers:
            th.start()
        writer.start()
        writer.join()
        for th in readers:
            th.join(timeout=2.0)

        assert not errors, f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# Integration: adaptive timeout responds to real load signals
# ---------------------------------------------------------------------------


class TestIntegrationLoadResponse:
    def test_timeout_grows_with_higher_connection_count(self):
        """Under increasing connection counts the timeout monotonically increases
        (until the ceiling is reached)."""
        ctrl = _ctrl(
            base_timeout_s=5.0,
            connection_coefficient=0.1,
            latency_coefficient=0.0,
            min_timeout_s=2.0,
            max_timeout_s=999.0,
        )
        previous = ctrl.calculate_timeout(active_connections=0, latency_ms=0.0)
        for conns in [10, 25, 50, 100, 200]:
            current = ctrl.calculate_timeout(active_connections=conns, latency_ms=0.0)
            assert current >= previous, (
                f"Timeout did not grow at conns={conns}: {current} < {previous}"
            )
            previous = current

    def test_timeout_grows_with_higher_latency(self):
        """Under increasing engine latency the timeout monotonically increases
        (until the ceiling is reached)."""
        ctrl = _ctrl(
            base_timeout_s=5.0,
            latency_coefficient=0.005,
            connection_coefficient=0.0,
            min_timeout_s=2.0,
            max_timeout_s=999.0,
        )
        previous = ctrl.calculate_timeout(active_connections=0, latency_ms=0.0)
        for lat in [50.0, 100.0, 250.0, 500.0, 1000.0]:
            current = ctrl.calculate_timeout(active_connections=0, latency_ms=lat)
            assert current >= previous, (
                f"Timeout did not grow at latency={lat}: {current} < {previous}"
            )
            previous = current

    def test_high_load_scenario_stays_below_ceiling(self):
        """Simulates a heavy analytical batch: 80 connections, 400ms latency."""
        ctrl = AdaptiveTimeoutController()  # default config
        timeout = ctrl.calculate_timeout(active_connections=80, latency_ms=400.0)
        assert timeout <= DEFAULT_MAX_TIMEOUT_S
        assert timeout >= DEFAULT_MIN_TIMEOUT_S

    def test_rolling_average_drives_timeout_up_under_load(self):
        """Recording progressively worse latency samples drives timeout_from_average up."""
        ctrl = _ctrl(
            base_timeout_s=5.0,
            latency_coefficient=0.01,
            connection_coefficient=0.0,
            min_timeout_s=2.0,
            max_timeout_s=999.0,
            latency_window=10,
        )
        # Record a batch of high-latency samples
        for _ in range(10):
            ctrl.record_latency(1000.0)  # 1000ms → adds 10s each

        high_load_timeout = ctrl.timeout_from_average(active_connections=0)
        # Expected: 5 + 0.01*1000 = 15.0
        assert high_load_timeout == pytest.approx(15.0, rel=1e-6)

        # Now replace with low-latency samples
        for _ in range(10):
            ctrl.record_latency(10.0)  # 10ms → adds 0.1s each

        low_load_timeout = ctrl.timeout_from_average(active_connections=0)
        # Expected: 5 + 0.01*10 = 5.1
        assert low_load_timeout < high_load_timeout
        assert low_load_timeout == pytest.approx(5.1, rel=1e-6)
