"""network/horizon_pool.py - Synchronous shared connection pool for Horizon broadcasts.

This module provides a dedicated, synchronous connection pool controller for
Horizon interactions. By leveraging urllib3.PoolManager, it maintains active, 
warm TCP/TLS connections to significantly minimize latency and network overhead 
during critical, high-frequency transaction broadcast events.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import urllib3
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S: float = 2.5

_TIMEOUT = urllib3.Timeout(
    connect=REQUEST_TIMEOUT_S,
    read=REQUEST_TIMEOUT_S
)

_RETRY_STRATEGY = Retry(
    total=3,
    backoff_factor=0.2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"]
)

_POOL_MANAGER = urllib3.PoolManager(
    num_pools=5,
    maxsize=20,
    block=True,
    timeout=_TIMEOUT,
    retries=_RETRY_STRATEGY,
)

def broadcast_transaction(
    url: str, 
    payload: Dict[str, Any], 
    *, 
    headers: Optional[Dict[str, str]] = None
) -> urllib3.response.BaseHTTPResponse:
    """
    Broadcast a transaction to Horizon using a warm, pre-established connection.

    Parameters
    ----------
    url:
        Absolute endpoint URL for the Horizon broadcast (e.g., /transactions).
    payload:
        A dictionary representing the transaction payload (will be JSON-encoded).
    headers:
        Optional headers (e.g., custom User-Agent or API keys).

    Returns
    -------
    urllib3.response.BaseHTTPResponse
        The raw response object from urllib3.

    Raises
    ------
    urllib3.exceptions.MaxRetryError
        If the request exceeds the allowed retries.
    urllib3.exceptions.TimeoutError
        If the connect or read phases exceed REQUEST_TIMEOUT_S.
    """
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)

    encoded_payload = json.dumps(payload).encode("utf-8")

    try:
        response = _POOL_MANAGER.request(
            "POST",
            url,
            body=encoded_payload,
            headers=request_headers
        )
        return response
    except urllib3.exceptions.TimeoutError as exc:
        logger.warning(
            "[HorizonPool] Broadcast request timed out | endpoint=%s | timeout_s=%s", 
            url, 
            REQUEST_TIMEOUT_S
        )
        raise
    except Exception as exc:
        logger.error("[HorizonPool] Broadcast failed | endpoint=%s | error=%s", url, str(exc))
        raise

def get_pool_stats() -> Dict[str, int]:
    """
    Returns diagnostics on the current connection pool usage.
    Useful for health checks and backpressure monitoring.
    """
    stats = {}
    for pool_key, pool in _POOL_MANAGER.pools.items():
        host = pool_key.host
        stats[f"{host}_active"] = pool.num_connections
        stats[f"{host}_requests"] = pool.num_requests
    
    return stats