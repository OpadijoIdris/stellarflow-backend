#!/usr/bin/env python3
"""Example: Using RetryController for exchange rate fetching.

This example demonstrates how to use the RetryController class to handle
transient network failures when fetching exchange rates from external APIs.

Run this example:
    python examples/retry_controller_example.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from network.retry import RetryController, RetryBudgetExhausted, with_retry

# Configure logging to see retry attempts
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Example 1: Basic Retry with Manual Loop
# ---------------------------------------------------------------------------


def example_basic_retry():
    """Demonstrate basic retry loop with RetryController."""
    print("\n" + "=" * 70)
    print("Example 1: Basic Retry Loop")
    print("=" * 70)
    
    controller = RetryController(
        max_retries=3,
        initial_delay=0.5,
        max_delay=5.0,
        backoff_factor=2.0,
    )
    
    # Simulate a flaky API that fails first 2 times
    attempt_count = 0
    
    def flaky_api_call():
        nonlocal attempt_count
        attempt_count += 1
        
        if attempt_count < 3:
            logger.info(f"API call #{attempt_count} - Simulating failure")
            raise ConnectionError(f"Simulated network error #{attempt_count}")
        
        logger.info(f"API call #{attempt_count} - SUCCESS!")
        return {"rate": 0.95, "currency": "USD/EUR"}
    
    try:
        for attempt in controller.attempts():
            try:
                result = flaky_api_call()
                logger.info(f"✓ Received result: {result}")
                break  # Success - exit retry loop
            except ConnectionError as exc:
                if not controller.should_retry(exc):
                    raise
                logger.warning(f"Attempt {attempt} failed: {exc}")
        
        # Log final statistics
        stats = controller.get_stats()
        print(f"\n✓ Success after {stats['attempt'] - 1} retries")
        print(f"  Total delay: {stats['total_delay']:.2f}s")
        
    except RetryBudgetExhausted:
        print(f"\n✗ Failed after exhausting all retry attempts")


# ---------------------------------------------------------------------------
# Example 2: Context Manager Pattern
# ---------------------------------------------------------------------------


def example_context_manager():
    """Demonstrate context manager pattern."""
    print("\n" + "=" * 70)
    print("Example 2: Context Manager Pattern")
    print("=" * 70)
    
    attempt_count = 0
    
    def another_flaky_operation():
        nonlocal attempt_count
        attempt_count += 1
        
        if attempt_count <= 2:
            logger.info(f"Operation #{attempt_count} - Simulating timeout")
            raise TimeoutError(f"Request timed out #{attempt_count}")
        
        logger.info(f"Operation #{attempt_count} - SUCCESS!")
        return "Data successfully retrieved"
    
    with RetryController(max_retries=4, initial_delay=0.3) as controller:
        try:
            for attempt in controller.attempts():
                try:
                    data = another_flaky_operation()
                    logger.info(f"✓ Got data: {data}")
                    break
                except TimeoutError as exc:
                    if not controller.should_retry(exc):
                        raise
                    logger.warning(f"Attempt {attempt} timed out")
            
            stats = controller.get_stats()
            print(f"\n✓ Success after {stats['attempt'] - 1} retries")
            
        except RetryBudgetExhausted:
            print(f"\n✗ Failed after exhausting retries")


# ---------------------------------------------------------------------------
# Example 3: Decorator Usage
# ---------------------------------------------------------------------------


@with_retry(
    max_retries=3,
    initial_delay=0.4,
    max_delay=3.0,
    retryable_exceptions=(ConnectionError, TimeoutError),
)
def fetch_exchange_rate(base: str, quote: str) -> float:
    """Fetch exchange rate with automatic retry.
    
    This function will automatically retry up to 3 times on
    ConnectionError or TimeoutError.
    """
    # Simulate occasional failures
    import random
    
    if random.random() < 0.6:  # 60% failure rate
        error_type = random.choice([ConnectionError, TimeoutError])
        raise error_type(f"Simulated {error_type.__name__}")
    
    # Success case
    rate = random.uniform(0.8, 1.2)
    logger.info(f"✓ Fetched rate for {base}/{quote}: {rate:.4f}")
    return rate


def example_decorator():
    """Demonstrate decorator usage."""
    print("\n" + "=" * 70)
    print("Example 3: Decorator Pattern")
    print("=" * 70)
    
    try:
        rate = fetch_exchange_rate("USD", "EUR")
        print(f"\n✓ Final rate: {rate:.4f}")
    except (ConnectionError, TimeoutError) as exc:
        print(f"\n✗ Failed to fetch rate: {exc}")


# ---------------------------------------------------------------------------
# Example 4: Custom Retry Logic
# ---------------------------------------------------------------------------


class SmartRetryController(RetryController):
    """Custom retry controller with intelligent retry decisions."""
    
    def should_retry(self, exception: Exception) -> bool:
        """Only retry on specific transient errors."""
        # Check if we still have retry budget
        if not super().should_retry(exception):
            return False
        
        # Application-specific retry logic
        if isinstance(exception, ValueError):
            # Don't retry validation errors
            logger.info("Validation error detected - NOT retrying")
            return False
        
        if isinstance(exception, (ConnectionError, TimeoutError)):
            # Always retry network errors
            logger.info("Network error detected - WILL retry")
            return True
        
        # Default: retry other exceptions
        return True


def example_custom_retry_logic():
    """Demonstrate custom retry logic."""
    print("\n" + "=" * 70)
    print("Example 4: Custom Retry Logic")
    print("=" * 70)
    
    controller = SmartRetryController(max_retries=3, initial_delay=0.3)
    
    # Test 1: Network error (should retry)
    print("\nTest 1: Network error (should retry)")
    attempt_count = 0
    
    for attempt in controller.attempts():
        try:
            attempt_count += 1
            if attempt_count < 2:
                raise ConnectionError("Network issue")
            logger.info("✓ Network operation succeeded")
            break
        except ConnectionError as exc:
            if not controller.should_retry(exc):
                raise
    
    # Test 2: Validation error (should NOT retry)
    print("\nTest 2: Validation error (should NOT retry)")
    controller.reset()  # Reset for new sequence
    
    try:
        for attempt in controller.attempts():
            try:
                raise ValueError("Invalid input format")
            except ValueError as exc:
                if not controller.should_retry(exc):
                    logger.info("✓ Correctly NOT retrying validation error")
                    raise
    except ValueError:
        pass  # Expected


# ---------------------------------------------------------------------------
# Example 5: Multi-Threaded Usage
# ---------------------------------------------------------------------------


def example_thread_safety():
    """Demonstrate thread-safe concurrent usage."""
    print("\n" + "=" * 70)
    print("Example 5: Multi-Threaded Usage")
    print("=" * 70)
    
    import threading
    import random
    
    # Single controller shared across threads (safe!)
    controller = RetryController(max_retries=2, initial_delay=0.2)
    
    results = []
    
    def worker(thread_id: int) -> None:
        """Simulate fetching data in a thread."""
        attempt_count = 0
        
        try:
            for attempt in controller.attempts():
                try:
                    attempt_count += 1
                    
                    # Simulate random failures
                    if random.random() < 0.5:
                        raise ConnectionError(f"Thread {thread_id} attempt {attempt_count} failed")
                    
                    # Success
                    result = {
                        "thread_id": thread_id,
                        "attempts": attempt_count,
                        "thread_name": threading.current_thread().name,
                    }
                    logger.info(f"Thread {thread_id} succeeded after {attempt_count} attempts")
                    results.append(result)
                    break
                    
                except ConnectionError as exc:
                    if not controller.should_retry(exc):
                        raise
        except RetryBudgetExhausted as exc:
            logger.warning(f"Thread {thread_id} exhausted retries")
    
    # Run multiple threads concurrently
    print(f"\nStarting 5 concurrent workers...")
    
    threads = []
    for i in range(5):
        thread = threading.Thread(target=worker, args=(i,))
        threads.append(thread)
        thread.start()
    
    for thread in threads:
        thread.join()
    
    print(f"\n✓ {len(results)}/5 workers succeeded")
    for result in results:
        print(f"  Thread {result['thread_id']}: {result['attempts']} attempts")


# ---------------------------------------------------------------------------
# Example 6: Statistics & Monitoring
# ---------------------------------------------------------------------------


def example_statistics():
    """Demonstrate statistics tracking and monitoring."""
    print("\n" + "=" * 70)
    print("Example 6: Statistics & Monitoring")
    print("=" * 70)
    
    controller = RetryController(max_retries=4, initial_delay=0.3)
    
    attempt_count = 0
    
    for attempt in controller.attempts():
        try:
            attempt_count += 1
            
            # Fail first 3 times
            if attempt_count < 4:
                raise ConnectionError(f"Attempt {attempt_count} failed")
            
            # Success on 4th attempt
            stats = controller.get_stats()
            print(f"\n✓ Operation succeeded!")
            print(f"  Attempt number: {stats['attempt']}")
            print(f"  Total delay: {stats['total_delay']:.3f}s")
            print(f"  Exhausted: {stats['exhausted']}")
            print(f"  Max retries: {stats['max_retries']}")
            print(f"  Thread: {stats['thread_name']}")
            break
            
        except ConnectionError as exc:
            if not controller.should_retry(exc):
                raise
            
            # Log intermediate statistics
            stats = controller.get_stats()
            print(f"Attempt {attempt} failed:")
            print(f"  Current attempt: {stats['attempt']}")
            print(f"  Accumulated delay: {stats['total_delay']:.3f}s")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    """Run all examples."""
    print("\n" + "=" * 70)
    print("RETRY CONTROLLER EXAMPLES")
    print("=" * 70)
    print("\nDemonstrating various usage patterns of the RetryController class.")
    print("Watch for INFO logs showing retry attempts with delays.\n")
    
    try:
        example_basic_retry()
        example_context_manager()
        example_decorator()
        example_custom_retry_logic()
        example_thread_safety()
        example_statistics()
        
        print("\n" + "=" * 70)
        print("✓ All examples completed successfully!")
        print("=" * 70 + "\n")
        
    except Exception as exc:
        logger.exception("Example failed with exception")
        print(f"\n✗ Example failed: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
