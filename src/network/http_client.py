"""network/http_client.py – Shared async HTTP client for the ingestion pipeline.

All external fetch requests are subject to a dynamically-tuned timeout window
enforced at the session level.  The initial hard baseline is 2 500 ms;
subsequent requests use a timeout derived from recent latency observations via
an exponential moving average (EMA) so that the window adapts to regional
network conditions without ever dropping below a safety floor.

Timeout handling contract
-------------------------
* ``httpx.TimeoutException`` / ``asyncio.TimeoutError`` are caught,
  logged with endpoint, duration, and UTC timestamp, then re-raised as
  ``FetchTimeoutError`` so callers can distinguish them from other errors.
* Non-timeout errors (connection refused, DNS failure, HTTP error status)
  propagate unchanged — this module never swallows them.
* Connections are always returned to the pool automatically — httpx manages
  this transparently via its internal connection pool.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple, Union

import httpx

from src.analytics.ema import RollingEMA


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timeout constants
# ---------------------------------------------------------------------------

#: Hard baseline (seconds) used when no latency history is available.
REQUEST_TIMEOUT_S: float = 2.5

#: Minimum timeout (seconds) – prevents the window from becoming too
#: aggressive during a run of unusually-fast responses.
_MIN_TIMEOUT_S: float = 1.0

#: Maximum timeout (seconds) – caps unbounded growth during congestion.
_MAX_TIMEOUT_S: float = 10.0

#: Multiplier applied to the EMA latency to compute the adaptive timeout.
#: A value of 3× gives comfortable headroom above the smoothed baseline.
_EMA_MULTIPLIER: float = 3.0

#: Human-readable label used in log messages so operators see milliseconds.
_TIMEOUT_LABEL_MS: int = int(REQUEST_TIMEOUT_S * 1000)

# ---------------------------------------------------------------------------
# Connection limits & HTTP/2
# ---------------------------------------------------------------------------

#: Keep one reusable connection pipe. With HTTP/2 enabled, concurrent ticker
#: requests share that socket as multiplexed streams instead of opening a new
#: TCP/TLS pipeline per asset.
_LIMITS = httpx.Limits(
    max_connections=1,
    max_keepalive_connections=1,
)


# ---------------------------------------------------------------------------
# Adaptive timeout
# ---------------------------------------------------------------------------


class AdaptiveTimeout:
    """Tracks recent request latency via EMA and derives a dynamic timeout.

    The timeout for the next request is ``max(_MIN_TIMEOUT_S,
    min(ema_latency * _EMA_MULTIPLIER, _MAX_TIMEOUT_S))``.  Before any
    latency samples have been recorded the baseline ``REQUEST_TIMEOUT_S`` is
    returned unchanged.

    Parameters
    ----------
    smoothing_period:
        Number of samples used to compute the EMA smoothing factor
        (α = 2 / (period + 1)).  A larger period means slower adaptation.
    """

    def __init__(self, smoothing_period: int = 10) -> None:
        self._ema = RollingEMA(smoothing_period=smoothing_period)

    def record(self, latency_s: float) -> None:
        """Feed a new latency observation (seconds) into the EMA."""
        self._ema.update(latency_s)

    @property
    def timeout_s(self) -> float:
        """Return the current adaptive timeout in seconds."""
        if self._ema.value is None:
            return REQUEST_TIMEOUT_S
        adaptive = self._ema.value * _EMA_MULTIPLIER
        return max(_MIN_TIMEOUT_S, min(adaptive, _MAX_TIMEOUT_S))

    def as_httpx_timeout(self) -> httpx.Timeout:
        """Return an ``httpx.Timeout`` built from the current adaptive value."""
        t = self.timeout_s
        return httpx.Timeout(connect=t, read=t, write=t, pool=t)


#: Module-level shared instance – all helpers use this by default so the EMA
#: accumulates across every outbound request in the process.
_adaptive_timeout: AdaptiveTimeout = AdaptiveTimeout()


# ---------------------------------------------------------------------------
# Typed error
# ---------------------------------------------------------------------------


class FetchTimeoutError(RuntimeError):
    """Raised when an outbound HTTP request exceeds the current adaptive timeout.

    Attributes
    ----------
    url : str
        The endpoint URL that timed out.
    timeout_ms : int
        The configured limit in milliseconds at the time of the failure.
    """

    def __init__(self, url: str, timeout_ms: int) -> None:
        self.url = url
        self.timeout_ms = timeout_ms
        super().__init__(
            f"[HttpClient] Request to {url!r} timed out after {timeout_ms} ms."
        )


MetricRequest = Union[
    str,
    Tuple[str, Optional[Mapping[str, str]]],
    Dict[str, Any],
]


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


def make_session(**kwargs: Any) -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` with HTTP/2 multiplexing enabled.

    The timeout is sourced from ``_adaptive_timeout`` at call time and will be
    updated per-request inside the fetch helpers — the session timeout serves
    only as a safety net for any request that bypasses the helpers.

    Parameters
    ----------
    **kwargs:
        Forwarded to ``httpx.AsyncClient``.  Supplying *timeout* or *limits*
        is silently discarded; the module-level values are authoritative.

    Returns
    -------
    httpx.AsyncClient
        A configured session ready for use.
    """
    kwargs["timeout"] = _adaptive_timeout.as_httpx_timeout()
    kwargs["limits"] = _LIMITS
    kwargs.setdefault("http2", True)
    return httpx.AsyncClient(**kwargs)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------


async def fetch_json(
    session: httpx.AsyncClient,
    url: str,
    *,
    params: Optional[Dict[str, str]] = None,
) -> Any:
    """Perform a GET request and return the parsed JSON body.

    Records the round-trip latency into the module-level
    :class:`AdaptiveTimeout` so the timeout for the next request is tuned to
    recent network conditions.

    Parameters
    ----------
    session:
        An ``httpx.AsyncClient`` created via :func:`make_session`.
    url:
        Absolute endpoint URL (no credentials / secret query params).
    params:
        Optional query parameters.

    Returns
    -------
    Any
        Parsed JSON payload.

    Raises
    ------
    FetchTimeoutError
        When the connect or read phase exceeds the current adaptive timeout.
    httpx.RequestError
        Propagated unchanged for non-timeout transport errors.
    """
    timeout = _adaptive_timeout.as_httpx_timeout()
    t0 = time.monotonic()
    try:
        resp = await session.get(url, params=params, timeout=timeout)
        _adaptive_timeout.record(time.monotonic() - t0)
        return resp.json()
    except httpx.TimeoutException as exc:
        _log_timeout(url, _adaptive_timeout.timeout_s)
        raise FetchTimeoutError(url, int(_adaptive_timeout.timeout_s * 1000)) from exc


async def fetch_json_many(
    session: httpx.AsyncClient,
    requests: Mapping[str, MetricRequest],
) -> Dict[str, Any]:
    """Fetch multiple JSON metric endpoints concurrently on one HTTP/2 session.

    ``requests`` maps each currency / metric key to either:

    * a URL string
    * ``(url, params)`` where params is a query-parameter mapping
    * ``{"url": url, "params": params}``

    All request tasks are scheduled before awaiting results, allowing httpx to
    multiplex them over the single connection configured in :func:`make_session`.
    """
    keys = list(requests.keys())
    tasks = []

    for key in keys:
        url, params = _normalise_metric_request(key, requests[key])
        tasks.append(asyncio.create_task(fetch_json(session, url, params=params)))

    results = await asyncio.gather(*tasks)
    return dict(zip(keys, results))


async def poll_json_metrics(requests: Mapping[str, MetricRequest]) -> Dict[str, Any]:
    """Create one HTTP/2 session and fetch distinct metric endpoints in parallel."""
    async with make_session() as session:
        return await fetch_json_many(session, requests)


async def fetch_text(
    session: httpx.AsyncClient,
    url: str,
    *,
    params: Optional[Dict[str, str]] = None,
) -> str:
    """Perform a GET request and return the raw response text.

    Identical adaptive-timeout semantics to :func:`fetch_json`.

    Parameters
    ----------
    session:
        Session created via :func:`make_session`.
    url:
        Absolute endpoint URL (no credentials / secret params).
    params:
        Optional query parameters.

    Returns
    -------
    str
        Decoded response body.

    Raises
    ------
    FetchTimeoutError
        On connect or read timeout.
    httpx.RequestError
        Propagated unchanged for non-timeout transport errors.
    """
    timeout = _adaptive_timeout.as_httpx_timeout()
    t0 = time.monotonic()
    try:
        resp = await session.get(url, params=params, timeout=timeout)
        _adaptive_timeout.record(time.monotonic() - t0)
        return resp.text
    except httpx.TimeoutException as exc:
        _log_timeout(url, _adaptive_timeout.timeout_s)
        raise FetchTimeoutError(url, int(_adaptive_timeout.timeout_s * 1000)) from exc


async def post_json(
    session: httpx.AsyncClient,
    url: str,
    payload: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    """Perform a POST request with a JSON body and return parsed JSON.

    Identical adaptive-timeout semantics to :func:`fetch_json`.

    Parameters
    ----------
    session:
        Session created via :func:`make_session`.
    url:
        Absolute endpoint URL (no credentials in the URL).
    payload:
        JSON-serialisable object sent as the request body.
    headers:
        Optional additional request headers.

    Returns
    -------
    Any
        Parsed JSON response body.

    Raises
    ------
    FetchTimeoutError
        On connect or read timeout.
    httpx.RequestError
        Propagated unchanged for non-timeout transport errors.
    """
    timeout = _adaptive_timeout.as_httpx_timeout()
    t0 = time.monotonic()
    try:
        resp = await session.post(url, json=payload, headers=headers, timeout=timeout)
        _adaptive_timeout.record(time.monotonic() - t0)
        return resp.json()
    except httpx.TimeoutException as exc:
        _log_timeout(url, _adaptive_timeout.timeout_s)
        raise FetchTimeoutError(url, int(_adaptive_timeout.timeout_s * 1000)) from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_metric_request(
    key: str,
    request: MetricRequest,
) -> Tuple[str, Optional[Dict[str, str]]]:
    if isinstance(request, str):
        return request, None

    if isinstance(request, tuple):
        if len(request) != 2:
            raise ValueError(f"Metric request {key!r} must be a (url, params) tuple.")
        url, params = request
    elif isinstance(request, dict):
        url = request.get("url")
        params = request.get("params")
    else:
        raise TypeError(f"Metric request {key!r} must be a URL, tuple, or dict.")

    if not isinstance(url, str) or not url:
        raise ValueError(f"Metric request {key!r} must include a non-empty URL.")
    if params is None:
        return url, None
    if not isinstance(params, Mapping):
        raise TypeError(f"Metric request {key!r} params must be a mapping.")

    return url, dict(params)


def _log_timeout(url: str) -> None:
    """Emit a structured warning for a timed-out request.

    Always logs:
    * ``endpoint`` – the URL that stalled (never includes auth headers/tokens)
    * ``timeout_ms`` – the configured hard limit
    * ``timestamp`` – ISO-8601 UTC moment when expiration was detected

    Never logs authentication headers, bearer tokens, or secret query
    parameters — those must be kept out of *url* by callers.
    """
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    logger.warning(
        "[HttpClient] Request timed out | endpoint=%s | timeout_ms=%d | timestamp=%s",
        url,
        int(timeout_s * 1000),
        timestamp,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "REQUEST_TIMEOUT_S",
    "AdaptiveTimeout",
    "FetchTimeoutError",
    "MetricRequest",
    "make_session",
    "fetch_json",
    "fetch_json_many",
    "poll_json_metrics",
    "fetch_text",
    "post_json",
]
