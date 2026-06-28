from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MIN_WORKERS: int = 4
MAX_WORKERS: int = 16

# Supervisor evaluates queue depth every N seconds.
_SUPERVISOR_INTERVAL: float = 2.0

# Scale-up when queue depth exceeds this threshold per active worker.
_SCALE_UP_RATIO: float = 2.0

# Scale-down when queue depth drops below this threshold per active worker.
_SCALE_DOWN_RATIO: float = 0.5

# How long an idle worker waits for an item before looping (keeps threads
# responsive to the stop event without busy-spinning).
_WORKER_TIMEOUT: float = 1.0


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolSnapshot:
    """Immutable view of pool state at a point in time."""

    active_workers: int
    queue_depth: int
    tasks_completed: int
    tasks_failed: int


@dataclass
class _PoolState:
    """Mutable internal counters — always accessed under _lock."""

    worker_count: int = MIN_WORKERS
    tasks_completed: int = 0
    tasks_failed: int = 0


# ---------------------------------------------------------------------------
# Worker function
# ---------------------------------------------------------------------------


def _worker(
    work_queue: queue.Queue,
    stop_event: threading.Event,
    state: _PoolState,
    lock: threading.Lock,
) -> None:
    """Main loop executed by each worker thread.

    Dequeues callables and runs them.  Increments completion/failure counters
    under *lock* so the supervisor can read consistent metrics.
    """
    while not stop_event.is_set():
        try:
            task: Callable = work_queue.get(timeout=_WORKER_TIMEOUT)
        except queue.Empty:
            continue

        try:
            task()
            with lock:
                state.tasks_completed += 1
        except Exception:
            logger.exception("Worker caught unhandled exception in task")
            with lock:
                state.tasks_failed += 1
        finally:
            work_queue.task_done()


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


def _supervisor(
    work_queue: queue.Queue,
    threads: list[threading.Thread],
    stop_event: threading.Event,
    state: _PoolState,
    lock: threading.Lock,
    thread_factory: Callable[[], threading.Thread],
) -> None:
    """Background supervisor that adjusts worker count based on queue depth.

    Scale-up rule: ``queue_depth / active_workers > SCALE_UP_RATIO``
    Scale-down rule: ``queue_depth / active_workers < SCALE_DOWN_RATIO``

    Worker count is clamped to [MIN_WORKERS, MAX_WORKERS].
    """
    while not stop_event.is_set():
        time.sleep(_SUPERVISOR_INTERVAL)

        with lock:
            depth = work_queue.qsize()
            current = state.worker_count

        if current == 0:
            ratio = float("inf")
        else:
            ratio = depth / current

        if ratio > _SCALE_UP_RATIO and current < MAX_WORKERS:
            # Add one worker per supervisor tick to avoid overshooting.
            new_thread = thread_factory()
            new_thread.start()
            with lock:
                threads.append(new_thread)
                state.worker_count += 1
            logger.info(
                "ThreadingPool: scaled UP to %d workers (queue depth %d)",
                state.worker_count,
                depth,
            )

        elif ratio < _SCALE_DOWN_RATIO and current > MIN_WORKERS:
            # Signal one idle worker to exit on its next empty-queue loop by
            # enqueuing a sentinel None value that workers check for.
            work_queue.put(None)  # handled below — see _worker_with_sentinel
            with lock:
                state.worker_count -= 1
            logger.info(
                "ThreadingPool: scaled DOWN to %d workers (queue depth %d)",
                state.worker_count,
                depth,
            )


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class DynamicThreadingPool:
    """Automated worker-scaling thread pool.

    Starts with :data:`MIN_WORKERS` threads and adjusts dynamically between
    :data:`MIN_WORKERS` and :data:`MAX_WORKERS` depending on real-time queue
    depth.

    Usage::

        pool = DynamicThreadingPool()
        pool.start()

        pool.submit(my_callable)
        pool.submit(lambda: process(item))

        pool.stop()

    The pool is also usable as a context manager::

        with DynamicThreadingPool() as pool:
            pool.submit(my_callable)
    """

    def __init__(
        self,
        min_workers: int = MIN_WORKERS,
        max_workers: int = MAX_WORKERS,
        supervisor_interval: float = _SUPERVISOR_INTERVAL,
    ) -> None:
        if min_workers < 1:
            raise ValueError("min_workers must be >= 1")
        if max_workers < min_workers:
            raise ValueError("max_workers must be >= min_workers")

        self._min_workers = min_workers
        self._max_workers = max_workers
        self._supervisor_interval = supervisor_interval

        self._work_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._state = _PoolState(worker_count=min_workers)
        self._threads: list[threading.Thread] = []
        self._supervisor_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_worker_thread(self) -> threading.Thread:
        """Create (but do not start) a daemon worker thread."""
        return threading.Thread(
            target=_worker_with_sentinel,
            args=(
                self._work_queue,
                self._stop_event,
                self._state,
                self._lock,
            ),
            daemon=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn initial workers and the supervisor thread."""
        if self._supervisor_thread is not None:
            raise RuntimeError("Pool is already running")

        for _ in range(self._min_workers):
            t = self._make_worker_thread()
            t.start()
            self._threads.append(t)

        self._supervisor_thread = threading.Thread(
            target=_supervisor,
            args=(
                self._work_queue,
                self._threads,
                self._stop_event,
                self._state,
                self._lock,
                self._make_worker_thread,
            ),
            daemon=True,
            name="ThreadingPool-Supervisor",
        )
        self._supervisor_thread.start()
        logger.info(
            "ThreadingPool: started with %d workers (min=%d, max=%d)",
            self._min_workers,
            self._min_workers,
            self._max_workers,
        )

    def submit(self, task: Callable) -> None:
        """Enqueue *task* for execution by a worker thread.

        Raises ``RuntimeError`` if the pool has been stopped.
        """
        if self._stop_event.is_set():
            raise RuntimeError("Cannot submit tasks to a stopped pool")
        self._work_queue.put(task)

    def stop(self, wait: bool = True, timeout: Optional[float] = None) -> None:
        """Signal all workers and the supervisor to stop.

        Parameters
        ----------
        wait:
            If ``True`` (default), block until all threads have exited.
        timeout:
            Optional per-thread join timeout in seconds.
        """
        self._stop_event.set()

        if wait:
            if self._supervisor_thread is not None:
                self._supervisor_thread.join(timeout=timeout)
            for t in self._threads:
                t.join(timeout=timeout)

        logger.info("ThreadingPool: stopped")

    def snapshot(self) -> PoolSnapshot:
        """Return an immutable snapshot of current pool metrics."""
        with self._lock:
            return PoolSnapshot(
                active_workers=self._state.worker_count,
                queue_depth=self._work_queue.qsize(),
                tasks_completed=self._state.tasks_completed,
                tasks_failed=self._state.tasks_failed,
            )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "DynamicThreadingPool":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.stop()
        return False


# ---------------------------------------------------------------------------
# Worker variant that supports scale-down sentinel
# ---------------------------------------------------------------------------


def _worker_with_sentinel(
    work_queue: queue.Queue,
    stop_event: threading.Event,
    state: _PoolState,
    lock: threading.Lock,
) -> None:
    """Worker loop that also handles the ``None`` scale-down sentinel.

    When the supervisor wants to remove a worker it enqueues ``None``.  The
    first worker to dequeue it exits cleanly, reducing the active count by one.
    """
    while not stop_event.is_set():
        try:
            task = work_queue.get(timeout=_WORKER_TIMEOUT)
        except queue.Empty:
            continue

        # Scale-down sentinel — exit gracefully.
        if task is None:
            work_queue.task_done()
            break

        try:
            task()
            with lock:
                state.tasks_completed += 1
        except Exception:
            logger.exception("Worker caught unhandled exception in task")
            with lock:
                state.tasks_failed += 1
        finally:
            work_queue.task_done()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Shared pool instance; call ``threading_pool.start()`` to activate.
threading_pool = DynamicThreadingPool(
    min_workers=MIN_WORKERS,
    max_workers=MAX_WORKERS,
)

__all__ = [
    "MIN_WORKERS",
    "MAX_WORKERS",
    "PoolSnapshot",
    "DynamicThreadingPool",
    "threading_pool",
]
