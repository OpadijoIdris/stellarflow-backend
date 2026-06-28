# Retry Controller - Quick Reference

## Import

```python
from network.retry import RetryController, with_retry, RetryBudgetExhausted
```

## Basic Usage

```python
controller = RetryController(max_retries=3)

for attempt in controller.attempts():
    try:
        result = risky_operation()
        break  # ← IMPORTANT: Always break on success!
    except NetworkError as exc:
        if not controller.should_retry(exc):
            raise
```

## Common Patterns

### Pattern 1: Simple Retry
```python
controller = RetryController(max_retries=3)
for attempt in controller.attempts():
    try:
        data = fetch_api()
        break
    except requests.RequestException:
        pass  # Automatically retries
```

### Pattern 2: Context Manager
```python
with RetryController(max_retries=3) as controller:
    for attempt in controller.attempts():
        try:
            process_data()
            break
        except TransientError:
            pass
```

### Pattern 3: Decorator
```python
@with_retry(max_retries=5, initial_delay=1.0)
def fetch_exchange_rate():
    response = requests.get("https://api.example.com/rate")
    response.raise_for_status()
    return response.json()
```

### Pattern 4: Selective Retry
```python
for attempt in controller.attempts():
    try:
        result = operation()
        break
    except (TimeoutError, ConnectionError) as exc:
        if not controller.should_retry(exc):
            raise
        logger.warning(f"Retry {attempt}: {exc}")
```

## Configuration Presets

### Conservative (Default)
```python
RetryController(
    max_retries=3,      # 3 retries
    initial_delay=1.0,  # 1 second
    max_delay=32.0,     # Cap at 32s
    backoff_factor=2.0  # Double each time
)
```

### Aggressive
```python
RetryController(
    max_retries=7,
    initial_delay=0.5,
    max_delay=60.0,
    backoff_factor=2.0
)
```

### Patient
```python
RetryController(
    max_retries=10,
    initial_delay=5.0,
    max_delay=300.0,
    backoff_factor=2.0
)
```

## Statistics

```python
stats = controller.get_stats()
print(f"Attempt: {stats['attempt']}")
print(f"Total delay: {stats['total_delay']:.2f}s")
print(f"Exhausted: {stats['exhausted']}")
```

## Custom Retry Logic

```python
class SmartRetryController(RetryController):
    def should_retry(self, exception):
        if not super().should_retry(exception):
            return False
        
        # Don't retry 4xx client errors
        if isinstance(exception, requests.HTTPError):
            if 400 <= exception.response.status_code < 500:
                return False
        
        # Retry network errors
        return isinstance(exception, (requests.Timeout, requests.ConnectionError))
```

## Thread Safety

```python
# Safe: Single controller shared across threads
controller = RetryController(max_retries=3)

def worker(thread_id):
    for attempt in controller.attempts():
        try:
            result = fetch_data(thread_id)
            break
        except NetworkError:
            pass

threads = [Thread(target=worker, args=(i,)) for i in range(10)]
```

## Common Mistakes

### ❌ Forgetting to break
```python
for attempt in controller.attempts():
    try:
        result = operation()
        # Missing break! Will exhaust retries even on success
    except Error:
        pass
```

### ✅ Correct
```python
for attempt in controller.attempts():
    try:
        result = operation()
        break  # Essential!
    except Error:
        pass
```

### ❌ Catching all exceptions silently
```python
for attempt in controller.attempts():
    try:
        result = operation()
        break
    except Exception:
        pass  # Might hide non-retryable errors
```

### ✅ Correct
```python
for attempt in controller.attempts():
    try:
        result = operation()
        break
    except (NetworkError, TimeoutError) as exc:
        if not controller.should_retry(exc):
            raise
```

## Error Handling

```python
try:
    for attempt in controller.attempts():
        try:
            result = operation()
            break
        except RetryableError:
            pass
except RetryBudgetExhausted:
    logger.error("All retry attempts exhausted")
    raise
```

## Delay Timing Examples

**Config:** `initial_delay=1.0, backoff_factor=2.0, max_delay=32.0`

| Attempt | Base Delay | Jittered Range (avg) |
|---------|-----------|---------------------|
| 1       | 1s        | 0-1s (~0.5s)       |
| 2       | 2s        | 0-2s (~1.0s)       |
| 3       | 4s        | 0-4s (~2.0s)       |
| 4       | 8s        | 0-8s (~4.0s)       |
| 5       | 16s       | 0-16s (~8.0s)      |
| 6       | 32s       | 0-32s (~16.0s)     |
| 7+      | 32s       | 0-32s (~16.0s)     |

## Logging

```python
import logging
logging.basicConfig(level=logging.INFO)

# Enable retry controller debug logs
logging.getLogger("network.retry").setLevel(logging.DEBUG)
```

## Testing

```bash
# Run tests
python -m pytest tests/test_retry.py -v

# Run specific test class
python -m pytest tests/test_retry.py::TestThreadIsolation -v

# Run examples
python examples/retry_controller_example.py
```

## Documentation

- Full guide: `RETRY_CONTROLLER_GUIDE.md`
- Implementation: `src/network/retry.py`
- Tests: `tests/test_retry.py`
- Examples: `examples/retry_controller_example.py`

## Key Features

✅ Thread-isolated (safe for concurrent use)
✅ Exponential backoff with full-jitter
✅ Configurable parameters
✅ Context manager support
✅ Decorator pattern
✅ Statistics tracking
✅ Structured logging
✅ Zero external dependencies
