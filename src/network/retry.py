"""network/retry.py – Thread-isolated retry controller with exponential backoff and full-jitter.

This module implements a sophisticated retry mechanism designed to prevent IP bans
and minimize external server congestion during outages by staggering network
connection retries using exponential backoff augmented with randomized full-jitter.

Key Features
------------
* Thread-isolated retry state management via threading.local()
* Exponential backoff with configurable base delay and maximum delay
* Full-jitter randomization to prevent thundering herd problem
* Per-thread retry budget tracking to prevent infinite loops
* Comprehensive logging with structured metadata
* Type-safe implementation with full type hints
* Zero external dependencies beyond standard library

Retry Algorithm
---------------
The retry delay is calculated using exponential backoff with full-jitter:

    base_delay = initial_delay * (backoff_factor ^ attempt)
    capped_delay = min(base_delay, max_delay)
    actual_delay = random.uniform(0, capped_delay)

Full-jitter ensures that concurrent retry attempts are maximally spread out,
preventing synchronized thundering herds that can trigger rate limiters or
worsen server congestion.

Thread Safety
-------------
All retry state is stored in thread-local storage, ensuring complete isolation
between threads. Multiple threads can safely use the same RetryController
instance without coordination or locking overhead.

Usage Examples
--------------
Basic usage with default settings::

    controller = RetryController()
    
    for attempt in controller.attempts():
        try:
            response = requests.get("https://api.example.com/data")
            response.raise_for_status()
            break  # Success - exit retry loop
        except requests.RequestException as exc:
            if not controller.should_retry(exc):
                raise  # Fatal error - propagate immediately

Custom configuration::

    controller = RetryController(
        max_retries=5,
        initial_delay=2.0,
        max_delay=60.0,
        backoff_factor=3.0
    )

Context manager pattern::

    with RetryController() as controller:
        for attempt in controller.attempts():
            try:
                result = perform_operation()
                break
            except RetryableError as exc:
                if not controller.should_retry(exc):
                    raise

Thread-Isolation Guarantees
----------------------------
Each thread maintains its own independent retry state including:
* Current attempt count
* Accumulated delay time
* Retry exhaustion status

This ensures that retry budgets in one thread do not affect other threads,
and allows fine-grained per-request retry control in multi-threaded applications.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable, Iterator, Optional, Type, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants & Default Configuration
# ---------------------------------------------------------------------------

#: Default maximum number of retry attempts before giving up.
DEFAULT_MAX_RETRIES: int = 3

#: Default initial delay in seconds before the first retry.
DEFAULT_INITIAL_DELAY: float = 1.0

#: Default maximum delay in seconds - caps exponential backoff growth.
DEFAULT_MAX_DELAY: float = 32.0

#: Default backoff multiplication factor - 2.0 means delays double each retry.
DEFAULT_BACKOFF_FACTOR: float = 2.0


# ---------------------------------------------------------------------------
# Thread-Local State Container
# ---------------------------------------------------------------------------


class _RetryState:
    """Thread-local storage for retry state.
    
    Attributes
    ----------
    attempt : int
        Current attempt number (0-indexed, 0 = first attempt).
    total_delay : float
        Cumulative delay time spent waiting between retries in seconds.
    exhausted : bool
        Flag indicating whether the retry budget has been depleted.
    """
    
    __slots__ = ("attempt", "total_delay", "exhausted")
    
    def __init__(self) -> None:
        self.attempt: int = 0
        self.total_delay: float = 0.0
        self.exhausted: bool = False
    
    def reset(self) -> None:
        """Reset all state fields to initial values."""
        self.attempt = 0
        self.total_delay = 0.0
        self.exhausted = False


# ---------------------------------------------------------------------------
# Retry Controller
# ---------------------------------------------------------------------------


class RetryController:
    """Thread-isolated retry controller with exponential backoff and full-jitter.
    
    This class orchestrates retry attempts with sophisticated backoff logic to
    prevent IP bans and minimize server congestion. Each thread using the
    controller maintains completely independent retry state.
    
    Parameters
    ----------
    max_retries : int, optional
        Maximum number of retry attempts (default: 3).
        Must be non-negative. A value of 0 means no retries.
    initial_delay : float, optional
        Initial delay in seconds before first retry (default: 1.0).
        Must be positive.
    max_delay : float, optional
        Maximum delay cap in seconds (default: 32.0).
        Prevents exponential backoff from growing unbounded.
        Must be >= initial_delay.
    backoff_factor : float, optional
        Exponential backoff multiplier (default: 2.0).
        Each retry delay is multiplied by this factor.
        Must be >= 1.0.
    
    Raises
    ------
    ValueError
        If any parameter violates its constraints.
    
    Attributes
    ----------
    max_retries : int
        Configured maximum retry attempts.
    initial_delay : float
        Configured initial retry delay in seconds.
    max_delay : float
        Configured maximum retry delay cap in seconds.
    backoff_factor : float
        Configured exponential backoff multiplier.
    
    Examples
    --------
    Basic retry loop::
    
        controller = RetryController(max_retries=5)
        
        for attempt in controller.attempts():
            try:
                result = fetch_data()
                break
            except NetworkError as exc:
                if not controller.should_retry(exc):
                    raise
    
    Context manager usage::
    
        with RetryController() as ctrl:
            for attempt in ctrl.attempts():
                try:
                    process_request()
                    break
                except RetryableError:
                    pass  # will retry
    
    Notes
    -----
    Thread Safety:
        Fully thread-safe. Multiple threads can share a single controller
        instance without any synchronization overhead. Each thread maintains
        independent retry state via threading.local().
    
    Time Complexity:
        O(1) for all operations.
    
    Space Complexity:
        O(T) where T is the number of threads using the controller.
        Each thread allocates a small _RetryState object.
    """
    
    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_delay: float = DEFAULT_INITIAL_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        # Validate parameters
        if max_retries < 0:
            raise ValueError(
                f"max_retries must be non-negative, got {max_retries}"
            )
        if initial_delay <= 0:
            raise ValueError(
                f"initial_delay must be positive, got {initial_delay}"
            )
        if max_delay < initial_delay:
            raise ValueError(
                f"max_delay ({max_delay}) must be >= initial_delay ({initial_delay})"
            )
        if backoff_factor < 1.0:
            raise ValueError(
                f"backoff_factor must be >= 1.0, got {backoff_factor}"
            )
        
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        
        # Thread-local storage for retry state
        self._local = threading.local()
        
        logger.debug(
            "[RetryController] Initialized | max_retries=%d | initial_delay=%.2fs | "
            "max_delay=%.2fs | backoff_factor=%.2f",
            max_retries,
            initial_delay,
            max_delay,
            backoff_factor,
        )
    
    def _get_state(self) -> _RetryState:
        """Get or create thread-local retry state.
        
        Returns
        -------
        _RetryState
            Current thread's retry state object.
        """
        if not hasattr(self._local, "state"):
            self._local.state = _RetryState()
        return self._local.state
    
    def reset(self) -> None:
        """Reset retry state for the current thread.
        
        This clears the attempt counter, total delay, and exhausted flag,
        allowing a fresh retry sequence to begin.
        
        Notes
        -----
        Only affects the calling thread's state. Other threads are unaffected.
        """
        state = self._get_state()
        state.reset()
        logger.debug("[RetryController] State reset for thread %s", threading.current_thread().name)
    
    def attempts(self) -> Iterator[int]:
        """Generate retry attempt sequence with automatic delay injection.
        
        Yields attempt numbers (0-indexed) and sleeps for the appropriate
        jittered exponential backoff delay before each retry (after attempt 0).
        
        Yields
        ------
        int
            Current attempt number (0 = first attempt, 1 = first retry, etc.).
        
        Raises
        ------
        RetryBudgetExhausted
            When all retry attempts have been consumed.
        
        Examples
        --------
        ::
        
            for attempt in controller.attempts():
                try:
                    result = risky_operation()
                    break  # Success - exit loop
                except RetryableError:
                    pass  # Continue to next retry
        
        Notes
        -----
        The iterator automatically:
        * Tracks the current attempt number
        * Calculates exponential backoff delays with full-jitter
        * Sleeps for the calculated delay before yielding retry attempts
        * Logs structured retry metadata
        * Raises RetryBudgetExhausted when retries are depleted
        
        Time Complexity: O(1) per iteration
        Space Complexity: O(1)
        """
        state = self._get_state()
        state.reset()
        
        while state.attempt <= self.max_retries:
            current_attempt = state.attempt
            
            # Sleep with exponential backoff + full-jitter before retries
            if current_attempt > 0:
                delay = self._calculate_delay(current_attempt)
                state.total_delay += delay
                
                logger.info(
                    "[RetryController] Retry attempt %d/%d | delay=%.3fs | "
                    "total_delay=%.3fs | thread=%s",
                    current_attempt,
                    self.max_retries,
                    delay,
                    state.total_delay,
                    threading.current_thread().name,
                )
                
                time.sleep(delay)
            
            state.attempt += 1
            yield current_attempt
        
        # Retry budget exhausted
        state.exhausted = True
        logger.warning(
            "[RetryController] Retry budget exhausted | attempts=%d | "
            "total_delay=%.3fs | thread=%s",
            state.attempt,
            state.total_delay,
            threading.current_thread().name,
        )
        raise RetryBudgetExhausted(
            f"Exhausted {self.max_retries} retry attempts "
            f"after {state.total_delay:.2f}s total delay"
        )
    
    def _calculate_delay(self, attempt: int) -> float:
        """Calculate jittered exponential backoff delay for a given attempt.
        
        Uses full-jitter: the actual delay is uniformly distributed between
        0 and the exponential backoff value, maximizing retry dispersion.
        
        Parameters
        ----------
        attempt : int
            Current attempt number (1-indexed for this calculation).
        
        Returns
        -------
        float
            Delay in seconds to wait before this retry attempt.
        
        Notes
        -----
        Formula:
            base_delay = initial_delay * (backoff_factor ^ (attempt - 1))
            capped_delay = min(base_delay, max_delay)
            actual_delay = random.uniform(0, capped_delay)
        
        Time Complexity: O(1)
        Space Complexity: O(1)
        """
        # Exponential backoff: initial_delay * (backoff_factor ^ (attempt - 1))
        base_delay = self.initial_delay * (self.backoff_factor ** (attempt - 1))
        
        # Cap at max_delay to prevent unbounded growth
        capped_delay = min(base_delay, self.max_delay)
        
        # Apply full-jitter: uniform random in [0, capped_delay]
        jittered_delay = random.uniform(0, capped_delay)
        
        return jittered_delay
    
    def should_retry(self, exception: Exception) -> bool:
        """Determine if an exception is retryable.
        
        This method provides a hook for custom retry logic based on exception
        types or attributes. The default implementation considers most exceptions
        retryable, but subclasses can override to implement specific policies.
        
        Parameters
        ----------
        exception : Exception
            The exception that occurred during an attempt.
        
        Returns
        -------
        bool
            True if the operation should be retried, False to propagate immediately.
        
        Examples
        --------
        ::
        
            for attempt in controller.attempts():
                try:
                    result = perform_request()
                    break
                except NetworkError as exc:
                    if not controller.should_retry(exc):
                        raise  # Non-retryable, propagate
                    # Otherwise continue to next retry
        
        Notes
        -----
        Common patterns for retry decisions:
        * Retry on transient network errors (timeouts, connection refused)
        * Retry on HTTP 429 (rate limited), 500, 502, 503, 504
        * Do NOT retry on HTTP 400, 401, 403, 404 (client errors)
        * Do NOT retry on authentication/authorization failures
        
        Override this method to implement application-specific retry policies.
        """
        state = self._get_state()
        
        # Check if retry budget is exhausted
        if state.exhausted or state.attempt > self.max_retries:
            return False
        
        # Default: most exceptions are retryable
        # Subclasses can override for specific logic
        return True
    
    def get_stats(self) -> dict[str, Any]:
        """Get current retry statistics for the calling thread.
        
        Returns
        -------
        dict
            Dictionary containing:
            * attempt (int): Current attempt number
            * total_delay (float): Total time spent in retry delays
            * exhausted (bool): Whether retry budget is depleted
            * max_retries (int): Configured maximum retries
            * thread_name (str): Name of the current thread
        
        Examples
        --------
        ::
        
            stats = controller.get_stats()
            print(f"Attempt {stats['attempt']}/{stats['max_retries']}")
            print(f"Total delay: {stats['total_delay']:.2f}s")
        """
        state = self._get_state()
        return {
            "attempt": state.attempt,
            "total_delay": state.total_delay,
            "exhausted": state.exhausted,
            "max_retries": self.max_retries,
            "thread_name": threading.current_thread().name,
        }
    
    def __enter__(self) -> RetryController:
        """Enter context manager - resets retry state."""
        self.reset()
        return self
    
    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> bool:
        """Exit context manager - does not suppress exceptions."""
        # Log final stats on exit
        stats = self.get_stats()
        logger.debug(
            "[RetryController] Context exit | final_attempt=%d | "
            "total_delay=%.3fs | thread=%s",
            stats["attempt"],
            stats["total_delay"],
            stats["thread_name"],
        )
        return False  # Do not suppress exceptions


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RetryBudgetExhausted(RuntimeError):
    """Raised when all retry attempts have been consumed.
    
    This exception is raised by the attempts() iterator when the retry budget
    is depleted. It includes details about the number of attempts made and
    total time spent in retry delays.
    """
    pass


# ---------------------------------------------------------------------------
# Convenience Decorator
# ---------------------------------------------------------------------------


def with_retry(
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    retryable_exceptions: tuple[Type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to add automatic retry logic to functions.
    
    Parameters
    ----------
    max_retries : int
        Maximum number of retry attempts.
    initial_delay : float
        Initial delay in seconds before first retry.
    max_delay : float
        Maximum delay cap in seconds.
    backoff_factor : float
        Exponential backoff multiplier.
    retryable_exceptions : tuple of Exception types
        Exception types that should trigger retries (default: all exceptions).
    
    Returns
    -------
    Callable
        Decorated function with automatic retry logic.
    
    Examples
    --------
    ::
    
        @with_retry(max_retries=5, initial_delay=2.0)
        def fetch_data():
            response = requests.get("https://api.example.com/data")
            response.raise_for_status()
            return response.json()
        
        # Automatically retries up to 5 times on any exception
        data = fetch_data()
    
    ::
    
        @with_retry(
            max_retries=3,
            retryable_exceptions=(requests.RequestException,)
        )
        def upload_file(path):
            with open(path, "rb") as f:
                requests.post("https://api.example.com/upload", files={"file": f})
        
        # Only retries on requests.RequestException
        upload_file("data.csv")
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            controller = RetryController(
                max_retries=max_retries,
                initial_delay=initial_delay,
                max_delay=max_delay,
                backoff_factor=backoff_factor,
            )
            
            last_exception: Optional[Exception] = None
            
            for attempt in controller.attempts():
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exception = exc
                    if not controller.should_retry(exc):
                        raise
                    # Continue to next retry
            
            # If we get here, retries are exhausted
            if last_exception:
                raise last_exception
            raise RetryBudgetExhausted("Retry budget exhausted without exception")
        
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper
    
    return decorator


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "RetryController",
    "RetryBudgetExhausted",
    "with_retry",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_INITIAL_DELAY",
    "DEFAULT_MAX_DELAY",
    "DEFAULT_BACKOFF_FACTOR",
]
