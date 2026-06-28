#!/usr/bin/env python3
"""
Database Connection Keep-Alive and Adaptive Timeout Controller
==============================================================
Two complementary components for robust DB write paths under load.

ConnectionKeepAlive
-------------------
Maintains long-lived relational connections during quiet, low-volume market
windows.

Serverless / autoscaled Postgres sinks (and intermediary poolers such as
PgBouncer) frequently drop idle TCP connections after a short timeout. When the
next price record arrives, the first write then stalls waiting for a fresh TCP
handshake and re-authentication. This module runs a low-overhead background
heartbeat (``SELECT 1;``) on a fixed interval to keep the channel warm so the
write path never pays the reconnect cost.

The keep-alive is connection-agnostic: it accepts any DB-API 2.0 connection
exposing ``cursor()``. It is meaningful for networked backends (e.g. PostgreSQL
via ``psycopg2``); for a local ``sqlite3`` connection there is no socket to keep
open, so the ping is harmless but inert.

AdaptiveTimeoutController
--------------------------
Dynamically calculates per-operation query timeout boundaries based on two
real-time engine signals:

  1. **Active connection count** — a pool under heavy concurrency needs wider
     timeouts so queued writers are not rejected before they even start.
  2. **Engine response latency (ms)** — a slow Postgres instance warrants more
     headroom; a fast one can be held to a tighter budget.

The formula is intentionally transparent and deterministic so operators can
reason about it without black-box tuning:

    timeout = BASE_TIMEOUT
              + LATENCY_COEFFICIENT  × latency_ms
              + CONNECTION_COEFFICIENT × active_connections

Both coefficients and the hard floor/ceiling are configurable at construction
time. The controller is thread-safe: the same instance can be shared across the
BatchSink flush thread, the HTTP handler pool, and any background worker.

Usage::

    controller = AdaptiveTimeoutController()
    timeout_s = controller.calculate_timeout(
        active_connections=pool.checked_out(),
        latency_ms=probe.last_rtt_ms(),
    )
    with db_op_with_timeout(timeout_s):
        ...

    # Optionally record observed latency samples so the controller can expose
    # a rolling average for monitoring:
    controller.record_latency(latency_ms=probe.last_rtt_ms())
    avg = controller.average_latency_ms()
"""

import logging
import threading
from typing import Any, Deque, Optional
from collections import deque

logger = logging.getLogger(__name__)
 
# ---------------------------------------------------------------------------
# ConnectionKeepAlive constants
# ---------------------------------------------------------------------------

# Default heartbeat cadence in seconds. Idle-connection timeouts on serverless
# Postgres / PgBouncer are commonly 60-300s, so a 30s ping keeps the channel
# warm with comfortable margin.
DEFAULT_PING_INTERVAL: float = 30.0
HEARTBEAT_QUERY: str = "SELECT 1;"

# ---------------------------------------------------------------------------
# AdaptiveTimeoutController constants
# ---------------------------------------------------------------------------

# Baseline timeout in seconds applied before any adjustment factors.
DEFAULT_BASE_TIMEOUT_S: float = 5.0

# Seconds of extra headroom added per millisecond of observed engine latency.
# At 100 ms RTT this adds 0.5 s; at 500 ms it adds 2.5 s.
DEFAULT_LATENCY_COEFFICIENT: float = 0.005

# Seconds of extra headroom added per active connection in the pool.
# 50 checked-out connections adds 2.5 s at the default coefficient.
DEFAULT_CONNECTION_COEFFICIENT: float = 0.05

# Hard lower bound: never issue a timeout shorter than this, regardless of
# how healthy the engine looks. Protects against accidental zero-timeout bugs.
DEFAULT_MIN_TIMEOUT_S: float = 2.0

# Hard upper bound: cap the timeout so a pathologically slow engine cannot
# stall a write thread indefinitely.
DEFAULT_MAX_TIMEOUT_S: float = 60.0

# Rolling window size for the internal latency sample buffer used by
# record_latency() / average_latency_ms().
DEFAULT_LATENCY_WINDOW: int = 100
 
 
class ConnectionKeepAlive:
    """Background heartbeat that keeps a relational connection channel alive.
 
    A daemon thread wakes every ``interval`` seconds and issues a lightweight
    ``SELECT 1;`` against the supplied connection. The thread is interruptible:
    ``stop()`` signals it via an :class:`threading.Event`, so shutdown does not
    wait out the full interval.
    """
 
    def __init__(
        self,
        connection: Any,
        interval: float = DEFAULT_PING_INTERVAL,
        query: str = HEARTBEAT_QUERY,
    ) -> None:
        if connection is None:
            raise ValueError("connection must not be None")
        if interval <= 0:
            raise ValueError("interval must be a positive number of seconds")
 
        self._conn = connection
        self._interval = interval
        self._query = query
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
 
    @property
    def is_running(self) -> bool:
        """True while the background heartbeat thread is alive."""
        return self._thread is not None and self._thread.is_alive()
 
    def start(self) -> None:
        """Start the background heartbeat thread.
 
        Calling ``start`` on an already-running keep-alive is a no-op.
        """
        if self.is_running:
            logger.debug("ConnectionKeepAlive already running; start() ignored")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="ConnectionKeepAlive",
        )
        self._thread.start()
        logger.info(
            "ConnectionKeepAlive started; pinging every %.1f seconds", self._interval
        )
 
    def ping(self) -> bool:
        """Issue a single heartbeat query.
 
        Returns ``True`` if the ping succeeded, ``False`` if it raised. Failures
        are logged and swallowed so a transient drop never takes down the
        background loop; the next tick simply tries again.
        """
        try:
            with self._lock:
                cursor = self._conn.cursor()
                try:
                    cursor.execute(self._query)
                    cursor.fetchone()
                finally:
                    close = getattr(cursor, "close", None)
                    if callable(close):
                        close()
            logger.debug("Heartbeat ping succeeded")
            return True
        except Exception:
            logger.warning("Heartbeat ping failed; will retry next interval", exc_info=True)
            return False
 
    def _run(self) -> None:
        """Background worker loop.
 
        ``Event.wait`` returns ``True`` when ``stop()`` has been signalled and
        ``False`` on timeout, so the loop ticks once per interval and exits
        promptly on shutdown.
        """
        while not self._stop_event.wait(self._interval):
            self.ping()
 
    def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Signal the background thread to stop and wait for it to exit."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        logger.info("ConnectionKeepAlive stopped")
 


class AdaptiveTimeoutController:
    """Dynamically calculates database query timeout boundaries at runtime.

    The timeout for any single write operation is composed of three additive
    terms:

        timeout_s = base_timeout_s
                  + latency_coefficient   × latency_ms
                  + connection_coefficient × active_connections

    The result is then clamped to ``[min_timeout_s, max_timeout_s]``.

    Rationale
    ---------
    Hard-coded timeouts are optimised for a single point on the
    load/latency curve.  Under heavy analytical batch writes the engine
    response time rises and the connection pool fills up, making a static
    budget too tight and causing valid telemetry updates to be dropped.
    This controller adjusts the budget proportionally so writes succeed
    when they legitimately need more time, while still bounding the wait
    during a genuine outage.

    Thread safety
    -------------
    All public methods are thread-safe. ``record_latency`` and
    ``average_latency_ms`` share a ``threading.Lock`` protecting the
    internal sample deque. ``calculate_timeout`` is stateless with
    respect to the deque and is therefore lock-free.

    Parameters
    ----------
    base_timeout_s:
        Baseline query timeout in seconds.
    latency_coefficient:
        Seconds added per ms of observed engine response latency.
    connection_coefficient:
        Seconds added per active (checked-out) connection.
    min_timeout_s:
        Hard floor; the returned value is never less than this.
    max_timeout_s:
        Hard ceiling; the returned value is never greater than this.
    latency_window:
        Number of recent latency samples retained for ``average_latency_ms``.

    Raises
    ------
    ValueError
        If any boundary argument violates basic sanity (e.g. min > max,
        non-positive base, negative coefficients).
    """

    def __init__(
        self,
        base_timeout_s: float = DEFAULT_BASE_TIMEOUT_S,
        latency_coefficient: float = DEFAULT_LATENCY_COEFFICIENT,
        connection_coefficient: float = DEFAULT_CONNECTION_COEFFICIENT,
        min_timeout_s: float = DEFAULT_MIN_TIMEOUT_S,
        max_timeout_s: float = DEFAULT_MAX_TIMEOUT_S,
        latency_window: int = DEFAULT_LATENCY_WINDOW,
    ) -> None:
        if base_timeout_s <= 0:
            raise ValueError("base_timeout_s must be positive")
        if latency_coefficient < 0:
            raise ValueError("latency_coefficient must be non-negative")
        if connection_coefficient < 0:
            raise ValueError("connection_coefficient must be non-negative")
        if min_timeout_s <= 0:
            raise ValueError("min_timeout_s must be positive")
        if max_timeout_s <= 0:
            raise ValueError("max_timeout_s must be positive")
        if min_timeout_s > max_timeout_s:
            raise ValueError(
                f"min_timeout_s ({min_timeout_s}) must not exceed max_timeout_s ({max_timeout_s})"
            )
        if latency_window < 1:
            raise ValueError("latency_window must be at least 1")

        self._base = base_timeout_s
        self._latency_coeff = latency_coefficient
        self._conn_coeff = connection_coefficient
        self._min = min_timeout_s
        self._max = max_timeout_s

        self._samples: Deque[float] = deque(maxlen=latency_window)
        self._lock = threading.Lock()

        logger.info(
            "AdaptiveTimeoutController initialised: base=%.1fs "
            "latency_coeff=%.4f conn_coeff=%.4f "
            "bounds=[%.1f, %.1f]s window=%d",
            self._base,
            self._latency_coeff,
            self._conn_coeff,
            self._min,
            self._max,
            latency_window,
        )

    # ------------------------------------------------------------------
    # Core calculation
    # ------------------------------------------------------------------

    def calculate_timeout(
        self,
        active_connections: int,
        latency_ms: float,
    ) -> float:
        """Return the adaptive timeout in seconds for the current engine state.

        Parameters
        ----------
        active_connections:
            Number of connections currently checked out from the pool (or
            the total open connection count if the driver does not distinguish
            checked-out vs idle).  Must be >= 0.
        latency_ms:
            Most recent round-trip engine latency in milliseconds (e.g. from a
            ``SELECT 1`` probe or the last successful write duration).
            Must be >= 0.

        Returns
        -------
        float
            Adaptive timeout in seconds, clamped to [min_timeout_s, max_timeout_s].

        Raises
        ------
        ValueError
            If ``active_connections`` or ``latency_ms`` is negative.
        """
        if active_connections < 0:
            raise ValueError("active_connections must be >= 0")
        if latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")

        raw = (
            self._base
            + self._latency_coeff * latency_ms
            + self._conn_coeff * active_connections
        )
        timeout = max(self._min, min(self._max, raw))

        logger.debug(
            "AdaptiveTimeoutController: conns=%d latency_ms=%.1f "
            "raw=%.3fs → timeout=%.3fs",
            active_connections,
            latency_ms,
            raw,
            timeout,
        )
        return timeout

    # ------------------------------------------------------------------
    # Rolling latency tracking
    # ------------------------------------------------------------------

    def record_latency(self, latency_ms: float) -> None:
        """Record an observed engine latency sample.

        Samples are kept in a bounded rolling window so callers can pass the
        result of ``average_latency_ms()`` into ``calculate_timeout`` without
        needing to maintain their own running average.

        Parameters
        ----------
        latency_ms:
            Observed round-trip latency in milliseconds.  Must be >= 0.

        Raises
        ------
        ValueError
            If ``latency_ms`` is negative.
        """
        if latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")
        with self._lock:
            self._samples.append(latency_ms)
        logger.debug("AdaptiveTimeoutController: recorded latency sample %.1f ms", latency_ms)

    def average_latency_ms(self) -> float:
        """Return the rolling average of recorded latency samples.

        Returns 0.0 if no samples have been recorded yet, so callers can
        pass the result directly into ``calculate_timeout`` without a
        special-case check.

        Returns
        -------
        float
            Average latency in milliseconds over the current window.
        """
        with self._lock:
            if not self._samples:
                return 0.0
            return sum(self._samples) / len(self._samples)

    def sample_count(self) -> int:
        """Return the number of latency samples currently in the rolling window."""
        with self._lock:
            return len(self._samples)

    # ------------------------------------------------------------------
    # Convenience: timeout from internally tracked average
    # ------------------------------------------------------------------

    def timeout_from_average(self, active_connections: int) -> float:
        """Calculate timeout using the internally tracked average latency.

        Combines ``average_latency_ms()`` and ``calculate_timeout()`` in one
        call for callers that continuously record samples and want to derive
        a timeout without separately querying the average.

        Parameters
        ----------
        active_connections:
            Number of connections currently checked out from the pool.

        Returns
        -------
        float
            Adaptive timeout in seconds.
        """
        return self.calculate_timeout(
            active_connections=active_connections,
            latency_ms=self.average_latency_ms(),
        )
