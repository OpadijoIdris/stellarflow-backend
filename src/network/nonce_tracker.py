import logging
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class NonceTracker:
    """Thread-safe per-account nonce tracker for parallel transaction channels.

    Each account address owns an independent Lock, so concurrent transactions
    across different accounts proceed without contention while a single
    account's nonces remain strictly sequential and duplicate-free.

    Complexity
    ----------
    Time  : O(1) amortised per nonce acquisition, sync, or invalidation.
    Space : O(n) where n is the number of unique account addresses tracked.
    """

    _instance: Optional["NonceTracker"] = None
    _init_lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "NonceTracker":
        # Double-checked locking: fast path avoids acquiring _init_lock once
        # the singleton is fully constructed.
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._account_locks: Dict[str, threading.Lock] = {}
                    instance._nonces: Dict[str, int] = {}
                    # Protects _account_locks dict during lazy lock creation.
                    instance._map_lock = threading.Lock()
                    cls._instance = instance
        return cls._instance

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_lock(self, address: str) -> threading.Lock:
        """Return the per-account lock, creating it lazily on first access.

        Double-checked locking ensures _map_lock is acquired only on the
        initial creation, keeping the common path (lock already exists)
        entirely contention-free.
        """
        lock = self._account_locks.get(address)
        if lock is None:
            with self._map_lock:
                lock = self._account_locks.get(address)
                if lock is None:
                    lock = threading.Lock()
                    self._account_locks[address] = lock
        return lock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_next_nonce(self, address: str, seed: Optional[int] = None) -> int:
        """Return the next unique, monotonically-increasing nonce for *address*.

        On the first call for an account a *seed* (the current on-chain
        sequence number) must be supplied. Subsequent calls increment the
        cached value atomically without further network I/O.

        Args:
            address: Account identifier (e.g. a Stellar public key).
            seed:    Bootstrap nonce when no local cache exists. Required on
                     the first call; ignored once a value is cached.

        Returns:
            An integer nonce guaranteed to be unique and sequential for
            *address* across all concurrent callers.

        Raises:
            ValueError: If no cached nonce exists and no *seed* was supplied.
        """
        lock = self._get_lock(address)
        with lock:
            try:
                cached = self._nonces.get(address)
                if cached is None:
                    if seed is None:
                        raise ValueError(
                            f"No cached nonce for '{address}' and no seed supplied."
                        )
                    self._nonces[address] = seed
                    logger.info("[NonceTracker] Seeded nonce for %s → %d", address, seed)
                    return seed

                next_nonce = cached + 1
                self._nonces[address] = next_nonce
                return next_nonce
            except Exception:
                # Drop the cache on any error so the next caller is forced to
                # re-sync from the ledger instead of propagating a stale value.
                self._nonces.pop(address, None)
                raise

    def sync_nonce(self, address: str, nonce: int) -> None:
        """Overwrite the cached nonce with a known-good ledger value.
        
        Call this after a tx_bad_seq error to realign the local counter with
        the chain's authoritative sequence number.
        
        Time: O(1).
        """
        lock = self._get_lock(address)
        with lock:
            self._nonces[address] = nonce
            logger.info("[NonceTracker] Synced nonce for %s → %d", address, nonce)

    def get_nonce(self, address: str) -> Optional[int]:
        """Return the current cached nonce for *address*, if it exists.
        
        Time: O(1).
        """
        lock = self._get_lock(address)
        with lock:
            return self._nonces.get(address)

    def invalidate(self, address: Optional[str] = None) -> None:
        """Evict the cached nonce for *address*, or all accounts when omitted.

        The next call to get_next_nonce will require a seed or an external
        sync from the ledger.

        Implementation note: for a full clear, a snapshot of existing accounts
        is taken under _map_lock which is then released before acquiring
        individual per-account locks. This prevents a deadlock that would arise
        if _map_lock were held while waiting for per-account locks that other
        threads may already hold.

        Time: O(1) for a single address; O(n) for a full clear.
        """
        if address is not None:
            lock = self._get_lock(address)
            with lock:
                self._nonces.pop(address, None)
            logger.info(
                "[NonceTracker] Nonce invalidated for %s. Re-sync required.", address
            )
            return

        # Snapshot account locks without holding _map_lock during the clear.
        with self._map_lock:
            snapshot = list(self._account_locks.items())

        for addr, lock in snapshot:
            with lock:
                self._nonces.pop(addr, None)

        logger.info("[NonceTracker] All cached nonces cleared. Re-sync required.")


# Module-level singleton – import and use directly.
nonce_tracker = NonceTracker()


# ---------------------------------------------------------------------------
# Sliding-window sequence tracker
# ---------------------------------------------------------------------------


class _AccountWindow:
    """Per-account mutable state for NonceWindow."""

    __slots__ = ("lock", "base", "next_index", "pending")

    def __init__(self) -> None:
        self.lock: threading.Lock = threading.Lock()
        self.base: Optional[int] = None
        self.next_index: int = 0
        self.pending: set = set()


class NonceWindow:
    """Thread-safe sliding window of pre-allocated Stellar sequence numbers.

    Unlike a simple sequential counter, ``NonceWindow`` pre-allocates
    *window_size* consecutive sequence numbers per account upfront.  Each
    ``acquire()`` call returns a unique slot from the current batch with only
    a brief critical section (a counter increment), so multiple parallel
    broadcast workers obtain their sequences almost simultaneously and can
    then sign and dispatch concurrently without serialising on the tracking
    layer.

    Sliding behaviour
    -----------------
    The window covers the integer range ``[base, base + window_size)``.
    ``acquire()`` hands out slots in order; ``acknowledge()`` marks a slot as
    finished.  Whenever the *lowest* in-flight sequence is acknowledged the
    base advances past every contiguous run of completed leading slots,
    opening fresh capacity for new ``acquire()`` calls.

    Example with window_size=4 and seed=100
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    acquire() → 100   pending={100}        slots used: 1/4
    acquire() → 101   pending={100,101}    slots used: 2/4
    acquire() → 102   pending={100,101,102} slots used: 3/4
    acknowledge(101)  pending={100,102}    base stays at 100 (100 still live)
    acknowledge(100)  pending={102}        base slides to 102 (101 already done)
    acquire() → 103   pending={102,103}    slots used: 2/4  (one slot re-opened)

    Complexity
    ----------
    acquire     : O(1) – lock held for a counter increment only.
    acknowledge : O(W) worst-case (reverse-order completions); O(1) typical.
    Space       : O(W × A) where W = window_size and A = number of accounts.
    """

    DEFAULT_WINDOW_SIZE: int = 16

    def __init__(self, window_size: int = DEFAULT_WINDOW_SIZE) -> None:
        if window_size < 1:
            raise ValueError("window_size must be a positive integer.")
        self._window_size = window_size
        self._accounts: Dict[str, _AccountWindow] = {}
        self._map_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_account(self, address: str) -> _AccountWindow:
        acct = self._accounts.get(address)
        if acct is None:
            with self._map_lock:
                acct = self._accounts.get(address)
                if acct is None:
                    acct = _AccountWindow()
                    self._accounts[address] = acct
        return acct

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, address: str, seed: Optional[int] = None) -> int:
        """Return the next pre-allocated sequence number for *address*.

        The call is O(1) and holds the per-account lock only for a counter
        increment, so concurrent workers on the same account contend for
        nanoseconds rather than the full sign-and-dispatch duration.

        Args:
            address : Stellar account public key.
            seed    : On-chain sequence number; required on the first call
                      for each account (ignored once the window is seeded).

        Returns:
            A unique sequence integer that will not repeat for *address*
            while any prior sequences for that account are still pending.

        Raises:
            ValueError  : Window is unseeded and no *seed* was supplied.
            RuntimeError: All window slots are in-flight; call
                          ``acknowledge()`` to release completed sequences
                          before acquiring more.
        """
        acct = self._get_account(address)
        with acct.lock:
            if acct.base is None:
                if seed is None:
                    raise ValueError(
                        f"NonceWindow for '{address}' is unseeded; supply a seed."
                    )
                acct.base = int(seed)
                acct.next_index = 0
                acct.pending.clear()
                logger.info(
                    "[NonceWindow] Seeded window for %s → %d (size=%d)",
                    address,
                    acct.base,
                    self._window_size,
                )

            if acct.next_index >= self._window_size:
                raise RuntimeError(
                    f"NonceWindow for '{address}' is exhausted: all "
                    f"{self._window_size} slots are in-flight. "
                    "Call acknowledge() to release completed sequences."
                )

            seq = acct.base + acct.next_index
            acct.next_index += 1
            acct.pending.add(seq)

            logger.debug(
                "[NonceWindow] Issued seq %d for %s (slot %d/%d)",
                seq,
                address,
                acct.next_index,
                self._window_size,
            )
            return seq

    def acknowledge(self, address: str, sequence: int) -> None:
        """Mark *sequence* as complete and advance the window base if possible.

        After removing the sequence from the pending set the method slides
        the window base forward past every contiguous run of leading
        acknowledged slots, opening capacity for fresh ``acquire()`` calls.

        Args:
            address  : Stellar account public key.
            sequence : Sequence number returned by a prior ``acquire()`` call.
        """
        acct = self._get_account(address)
        with acct.lock:
            if sequence not in acct.pending:
                logger.warning(
                    "[NonceWindow] acknowledge(%s, %d) – sequence not tracked; "
                    "ignoring.",
                    address,
                    sequence,
                )
                return

            acct.pending.discard(sequence)

            # Advance the base past every leading slot that has been both
            # issued (next_index > 0 guarantees base was issued) and
            # acknowledged (base is absent from pending).
            while acct.next_index > 0 and acct.base not in acct.pending:
                acct.base += 1
                acct.next_index -= 1
                logger.debug(
                    "[NonceWindow] Window slid for %s → base=%d in-flight=%d",
                    address,
                    acct.base,
                    acct.next_index,
                )

    def sync(self, address: str, sequence: int) -> None:
        """Realign the window to a known-good on-chain sequence.

        Drops all pending in-flight slots and resets the base to *sequence*.
        Use this after a ``tx_bad_seq`` error to resynchronise local tracking
        with the ledger's authoritative value.

        Args:
            address  : Stellar account public key.
            sequence : Authoritative sequence number from the ledger.
        """
        acct = self._get_account(address)
        with acct.lock:
            acct.base = int(sequence)
            acct.next_index = 0
            acct.pending.clear()
            logger.info(
                "[NonceWindow] Synced window for %s → %d", address, sequence
            )

    def invalidate(self, address: Optional[str] = None) -> None:
        """Evict the window for *address*, or all windows when omitted.

        The next ``acquire()`` will require a seed.

        Implementation note: for a full clear a snapshot of existing accounts
        is taken under ``_map_lock``, which is released before acquiring
        individual per-account locks to prevent the same deadlock risk
        described in ``NonceTracker.invalidate``.

        Time: O(1) for a single address; O(A) for a full clear.
        """
        if address is not None:
            acct = self._get_account(address)
            with acct.lock:
                acct.base = None
                acct.next_index = 0
                acct.pending.clear()
            logger.info(
                "[NonceWindow] Invalidated window for %s. Re-seed required.",
                address,
            )
            return

        with self._map_lock:
            snapshot = list(self._accounts.items())

        for _addr, acct in snapshot:
            with acct.lock:
                acct.base = None
                acct.next_index = 0
                acct.pending.clear()

        logger.info("[NonceWindow] All windows invalidated. Re-seed required.")

    @property
    def window_size(self) -> int:
        """The number of sequences pre-allocated per window batch."""
        return self._window_size

    def available_slots(self, address: str) -> int:
        """Return how many sequences can still be acquired in the current window.

        Returns 0 when the window is unseeded or fully exhausted.
        """
        acct = self._get_account(address)
        with acct.lock:
            if acct.base is None:
                return 0
            return self._window_size - acct.next_index


# Module-level default window – import and use directly.
nonce_window = NonceWindow()

__all__ = [
    "NonceTracker",
    "NonceWindow",
    "nonce_tracker",
    "nonce_window",
]
