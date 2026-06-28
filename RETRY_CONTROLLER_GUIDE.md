# Retry Controller Implementation Guide

## Overview

The `RetryController` class in `src/network/retry.py` provides a thread-isolated retry mechanism with exponential backoff and full-jitter randomization. This implementation prevents IP bans and minimizes external server congestion during outages by intelligently staggering network connection retries.

## Key Features

### 1. **Thread Isolation**
- Each thread maintains completely independent retry state
- Zero synchronization overhead between threads
- Safe for use in multi-threaded applications
- Thread-local storage via `threading.local()`

### 2. **Exponential Backoff with Full-Jitter**
- Prevents thundering herd problem
- Maximizes retry attempt dispersion
- Configurable backoff parameters
- Automatic delay capping to prevent unbounded growth

### 3. **Comprehensive Observability**
- Structured logging with metadata
- Retry statistics tracking
- Thread identification in logs
- Delay time accumulation

### 4. **Type-Safe & Well-Documented**
- Full type hints throughout
- Extensive docstrings
- Parameter validation
- Clear exception handling

## Algorithm Details

### Delay Calculation

The retry delay uses exponential backoff with full-jitter:

```
base_delay = initial_delay * (backoff_factor ^ (attempt - 1))
capped_delay = min(base_delay, max_delay)
actual_delay = random.uniform(0, capped_delay)
```

**Why Full-Jitter?**
- **Equal Jitter**: `base_delay / 2 + random.uniform(0, base_delay / 2)`
- **Full-Jitter**: `random.uniform(0, base_delay)` ✓ (Used here)

Full-jitter provides maximum dispersion, preventing synchronized retry waves that can trigger rate limiters or worsen server congestion during outages.

## Usage Examples

### Basic Usage

```python
from network.retry import RetryController
import requests

controller = RetryController(max_retries=3)

for attempt in controller.attempts():
    try:
        response = requests.get("https://api.exchange.com/rates")
        response.raise_for_status()
        data = response.json()
        break  # Success - exit retry loop
    except requests.RequestException as exc:
        if not controller.should_retry(exc):
            raise  # Non-retryable error - propagate immediately
        # Log and continue to next retry
        print(f"Attempt {attempt} failed: {exc}")
```

### Custom Configuration

```python
# Aggressive retry for critical operations
controller = RetryController(
    max_retries=5,           # 5 retry attempts
    initial_delay=2.0,       # Start with 2 second delay
    max_delay=60.0,          # Cap at 60 seconds
    backoff_factor=3.0       # Triple delay each retry
)

for attempt in controller.attempts():
    try:
        result = critical_exchange_operation()
        break
    except TransientError as exc:
        if not controller.should_retry(exc):
            raise
```

### Context Manager Pattern

```python
with RetryController(max_retries=3) as controller:
    for attempt in controller.attempts():
        try:
            data = fetch_exchange_rates()
            process_data(data)
            break
        except NetworkError:
            pass  # Will retry automatically
```

### Decorator Usage

```python
from network.retry import with_retry
import requests

@with_retry(
    max_retries=5,
    initial_delay=1.0,
    max_delay=30.0,
    retryable_exceptions=(requests.RequestException, TimeoutError)
)
def fetch_market_data(symbol: str):
    """Fetch market data with automatic retry."""
    response = requests.get(f"https://api.exchange.com/market/{symbol}")
    response.raise_for_status()
    return response.json()

# Automatically retries up to 5 times on network errors
data = fetch_market_data("BTC-USD")
```

### Integration with Existing Code

```python
# Example: Retrofit existing exchange fetcher
from network.retry import RetryController
import httpx

class ExchangeRateFetcher:
    def __init__(self):
        self.retry_controller = RetryController(
            max_retries=3,
            initial_delay=1.0,
            max_delay=10.0,
            backoff_factor=2.0
        )
    
    async def fetch_rate(self, base: str, quote: str) -> float:
        """Fetch exchange rate with retry logic."""
        async with httpx.AsyncClient() as client:
            for attempt in self.retry_controller.attempts():
                try:
                    response = await client.get(
                        f"https://api.exchange.com/rate",
                        params={"base": base, "quote": quote},
                        timeout=5.0
                    )
                    response.raise_for_status()
                    data = response.json()
                    return data["rate"]
                
                except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    if not self.retry_controller.should_retry(exc):
                        raise
                    
                    # Log retry metadata
                    stats = self.retry_controller.get_stats()
                    logger.warning(
                        f"Fetch failed on attempt {stats['attempt']}, "
                        f"retrying... (total_delay={stats['total_delay']:.2f}s)"
                    )
```

## Advanced Patterns

### Custom Retry Logic

```python
class SmartRetryController(RetryController):
    """Custom retry controller with application-specific logic."""
    
    def should_retry(self, exception: Exception) -> bool:
        """Only retry on specific transient errors."""
        # Check budget first
        if not super().should_retry(exception):
            return False
        
        # Application-specific retry decisions
        if isinstance(exception, requests.HTTPError):
            status_code = exception.response.status_code
            
            # Retry on rate limits and server errors
            if status_code in (429, 500, 502, 503, 504):
                return True
            
            # Don't retry client errors
            if 400 <= status_code < 500:
                return False
        
        # Retry on network/timeout errors
        if isinstance(exception, (requests.Timeout, requests.ConnectionError)):
            return True
        
        return False

# Usage
controller = SmartRetryController(max_retries=5)
```

### Multi-Threaded Exchange Rate Fetching

```python
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from network.retry import RetryController

# Single controller instance shared across threads
controller = RetryController(max_retries=3)

def fetch_rate_with_retry(pair: str) -> dict:
    """Fetch rate for a currency pair with retry (thread-safe)."""
    for attempt in controller.attempts():
        try:
            response = requests.get(f"https://api.exchange.com/rate/{pair}")
            response.raise_for_status()
            return {
                "pair": pair,
                "rate": response.json()["rate"],
                "thread": threading.current_thread().name
            }
        except requests.RequestException:
            if not controller.should_retry(exception):
                raise

# Fetch multiple pairs concurrently
pairs = ["BTC-USD", "ETH-USD", "XLM-USD", "USDC-NGN"]

with ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(fetch_rate_with_retry, pair) for pair in pairs]
    
    results = []
    for future in as_completed(futures):
        try:
            result = future.result()
            results.append(result)
        except Exception as exc:
            print(f"Failed to fetch rate: {exc}")

print(f"Successfully fetched {len(results)} rates")
```

### Observability & Monitoring

```python
from network.retry import RetryController
import logging

logger = logging.getLogger(__name__)

controller = RetryController(max_retries=5)

for attempt in controller.attempts():
    try:
        result = risky_operation()
        
        # Log success with retry metadata
        stats = controller.get_stats()
        if stats['attempt'] > 0:
            logger.info(
                f"Operation succeeded after {stats['attempt']} retries, "
                f"total_delay={stats['total_delay']:.2f}s"
            )
        break
        
    except RetryableError as exc:
        if not controller.should_retry(exc):
            # Log final failure
            stats = controller.get_stats()
            logger.error(
                f"Operation failed after {stats['attempt']} attempts, "
                f"total_delay={stats['total_delay']:.2f}s, "
                f"error={exc}"
            )
            raise
```

## Configuration Guidelines

### Conservative (Default)
```python
RetryController(
    max_retries=3,      # 3 retries (4 total attempts)
    initial_delay=1.0,  # 1 second initial delay
    max_delay=32.0,     # Cap at 32 seconds
    backoff_factor=2.0  # Double each time
)
# Retry delays: ~0.5s, ~1s, ~2s (avg with jitter)
# Total time: ~3.5s average
```

### Aggressive (High-Priority Operations)
```python
RetryController(
    max_retries=7,      # 7 retries (8 total attempts)
    initial_delay=0.5,  # 500ms initial delay
    max_delay=60.0,     # Cap at 60 seconds
    backoff_factor=2.0  # Double each time
)
# Retry delays: ~0.25s, ~0.5s, ~1s, ~2s, ~4s, ~8s, ~16s
# Total time: ~31.75s average
```

### Patient (Low-Priority Background Jobs)
```python
RetryController(
    max_retries=10,     # 10 retries
    initial_delay=5.0,  # 5 second initial delay
    max_delay=300.0,    # Cap at 5 minutes
    backoff_factor=2.0  # Double each time
)
# Will retry for extended period with long delays
```

## Best Practices

### 1. **Choose Appropriate max_retries**
- **3-5 retries**: Most API calls, transient network issues
- **7-10 retries**: Critical operations, known flaky endpoints
- **0-1 retries**: User-facing operations (avoid long waits)

### 2. **Set Reasonable max_delay**
- **10-30s**: Interactive operations
- **60s**: Background jobs
- **300s+**: Batch processing, non-urgent tasks

### 3. **Use Specific Exception Types**
```python
# ✓ Good - specific exception handling
except (requests.Timeout, requests.ConnectionError) as exc:
    if not controller.should_retry(exc):
        raise

# ✗ Bad - catching all exceptions
except Exception:
    pass  # Might hide non-retryable errors
```

### 4. **Log Retry Attempts**
```python
for attempt in controller.attempts():
    try:
        result = operation()
        break
    except RetryableError as exc:
        if not controller.should_retry(exc):
            raise
        logger.warning(f"Retry {attempt}: {exc}")
```

### 5. **Always Break on Success**
```python
# ✓ Good - explicit break
for attempt in controller.attempts():
    try:
        result = operation()
        break  # Essential!
    except RetryableError:
        pass

# ✗ Bad - no break (will exhaust retries even on success)
for attempt in controller.attempts():
    try:
        result = operation()
    except RetryableError:
        pass
```

## Performance Characteristics

### Time Complexity
- Construction: `O(1)`
- Per retry attempt: `O(1)`
- State reset: `O(1)`
- Statistics retrieval: `O(1)`

### Space Complexity
- Per controller instance: `O(1)`
- Per thread using controller: `O(1)` (thread-local state)
- Total for T threads: `O(T)`

### Thread Safety
- **Read operations**: Lock-free
- **Write operations**: Isolated per thread
- **Shared controller**: Safe for concurrent use

## Testing

Run the comprehensive test suite:

```bash
# Run all retry controller tests
python -m pytest tests/test_retry.py -v

# Run specific test class
python -m pytest tests/test_retry.py::TestThreadIsolation -v

# Run with coverage
python -m pytest tests/test_retry.py --cov=src/network/retry --cov-report=html
```

**Test Coverage**: 45 tests covering:
- Construction & validation
- Basic retry logic
- Delay calculation & jitter
- Thread isolation
- Statistics & observability
- Context manager behavior
- Decorator functionality
- Edge cases & stress testing

## Migration from Existing Retry Logic

### Before (Manual Retry)
```python
max_retries = 3
delay = 1.0

for attempt in range(max_retries + 1):
    try:
        result = fetch_data()
        break
    except Exception as exc:
        if attempt < max_retries:
            time.sleep(delay)
            delay *= 2
        else:
            raise
```

### After (RetryController)
```python
from network.retry import RetryController

controller = RetryController(max_retries=3, initial_delay=1.0)

for attempt in controller.attempts():
    try:
        result = fetch_data()
        break
    except Exception as exc:
        if not controller.should_retry(exc):
            raise
```

## Troubleshooting

### Issue: Retries taking too long
**Solution**: Reduce `max_retries` or `max_delay`
```python
controller = RetryController(max_retries=2, max_delay=10.0)
```

### Issue: Still getting IP bans
**Solution**: Increase jitter by reducing `backoff_factor` or use longer initial delays
```python
controller = RetryController(backoff_factor=1.5, initial_delay=2.0)
```

### Issue: Thread state not resetting
**Solution**: Explicitly call `reset()` between retry sequences
```python
controller.reset()  # Clear state before new sequence
```

### Issue: Want to see retry attempts in logs
**Solution**: Configure logging level
```python
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("network.retry")
logger.setLevel(logging.INFO)
```

## Related Documentation

- `src/network/retry.py` - Implementation source code
- `tests/test_retry.py` - Comprehensive test suite
- `RETRY_IMPLEMENTATION.md` - TypeScript retry utilities
- `src/network/http_client.py` - HTTP client with timeout handling

## Support

For issues or questions about the retry controller:
1. Check the comprehensive docstrings in `src/network/retry.py`
2. Review test cases in `tests/test_retry.py` for usage examples
3. Enable debug logging: `logging.getLogger("network.retry").setLevel(logging.DEBUG)`
