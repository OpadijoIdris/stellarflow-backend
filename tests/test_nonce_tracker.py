from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from network.nonce_tracker import NonceWindow


# ---------------------------------------------------------------------------
# Basic acquire / seed behaviour
# ---------------------------------------------------------------------------


def test_acquire_requires_seed_on_first_call() -> None:
    window = NonceWindow()
    with pytest.raises(ValueError, match="unseeded"):
        window.acquire("GACC")


def test_acquire_returns_seed_then_increments() -> None:
    window = NonceWindow()
    assert window.acquire("GACC", seed=100) == 100
    assert window.acquire("GACC") == 101
    assert window.acquire("GACC") == 102


def test_seed_ignored_after_window_is_seeded() -> None:
    window = NonceWindow()
    window.acquire("GACC", seed=50)
    # A second seed value supplied here must be ignored.
    assert window.acquire("GACC", seed=999) == 51


# ---------------------------------------------------------------------------
# Window exhaustion and acknowledge-driven sliding
# ---------------------------------------------------------------------------


def test_exhaustion_raises_when_all_slots_in_flight() -> None:
    window = NonceWindow(window_size=4)
    for i in range(4):
        window.acquire("GACC", seed=0 if i == 0 else None)

    with pytest.raises(RuntimeError, match="exhausted"):
        window.acquire("GACC")


def test_acknowledge_opens_slot_after_exhaustion() -> None:
    window = NonceWindow(window_size=2)
    s0 = window.acquire("GACC", seed=10)
    s1 = window.acquire("GACC")

    with pytest.raises(RuntimeError):
        window.acquire("GACC")

    window.acknowledge("GACC", s0)
    # Window should have slid; one new slot is available.
    s2 = window.acquire("GACC")
    assert s2 == 12  # base slid to 11 then 11 was issued next


def test_window_slides_past_consecutive_leading_acknowledged_slots() -> None:
    window = NonceWindow(window_size=4)
    seqs = [window.acquire("GACC", seed=100 if i == 0 else None) for i in range(4)]

    # Acknowledge out of order: 101 first, then 100.
    window.acknowledge("GACC", seqs[1])  # 101 done; base stays at 100
    assert window.available_slots("GACC") == 0  # window still full

    window.acknowledge("GACC", seqs[0])  # 100 done; base slides past 100 and 101
    assert window.available_slots("GACC") == 2  # two slots freed (100 and 101)

    # The next sequences issued must continue from 104.
    assert window.acquire("GACC") == 104
    assert window.acquire("GACC") == 105


def test_full_window_drains_and_resets_base() -> None:
    window = NonceWindow(window_size=3)
    seqs = [window.acquire("GACC", seed=7 if i == 0 else None) for i in range(3)]

    for s in seqs:
        window.acknowledge("GACC", s)

    # All three acknowledged; base should now be at 10 with zero in-flight.
    assert window.available_slots("GACC") == 3
    assert window.acquire("GACC") == 10


# ---------------------------------------------------------------------------
# Available slots reporting
# ---------------------------------------------------------------------------


def test_available_slots_tracks_issued_count() -> None:
    window = NonceWindow(window_size=8)
    assert window.available_slots("GACC") == 0  # unseeded

    window.acquire("GACC", seed=1)
    assert window.available_slots("GACC") == 7

    window.acquire("GACC")
    assert window.available_slots("GACC") == 6


# ---------------------------------------------------------------------------
# Sync resets the window to a ledger-authoritative value
# ---------------------------------------------------------------------------


def test_sync_discards_pending_and_resets_base() -> None:
    window = NonceWindow(window_size=4)
    window.acquire("GACC", seed=50)
    window.acquire("GACC")

    window.sync("GACC", 200)

    assert window.available_slots("GACC") == 4
    assert window.acquire("GACC") == 200


# ---------------------------------------------------------------------------
# Invalidate clears one or all windows
# ---------------------------------------------------------------------------


def test_invalidate_single_account_requires_reseed() -> None:
    window = NonceWindow()
    window.acquire("GA", seed=10)
    window.acquire("GB", seed=20)

    window.invalidate("GA")

    with pytest.raises(ValueError, match="unseeded"):
        window.acquire("GA")

    # GB must be unaffected.
    assert window.acquire("GB") == 21


def test_invalidate_all_clears_every_account() -> None:
    window = NonceWindow()
    window.acquire("GA", seed=1)
    window.acquire("GB", seed=2)

    window.invalidate()

    for addr in ("GA", "GB"):
        with pytest.raises(ValueError, match="unseeded"):
            window.acquire(addr)


# ---------------------------------------------------------------------------
# Account isolation
# ---------------------------------------------------------------------------


def test_independent_accounts_do_not_share_window_state() -> None:
    window = NonceWindow(window_size=4)
    window.acquire("GA", seed=100)  # GA → 100
    window.acquire("GB", seed=200)  # GB → 200
    window.acquire("GA")            # GA → 101

    assert window.acquire("GA") == 102  # GA: third call
    assert window.acquire("GB") == 201  # GB: second call


# ---------------------------------------------------------------------------
# Acknowledge edge cases
# ---------------------------------------------------------------------------


def test_acknowledge_unknown_sequence_is_a_no_op() -> None:
    window = NonceWindow()
    window.acquire("GACC", seed=5)
    # Should not raise; just logs a warning.
    window.acknowledge("GACC", 9999)
    assert window.available_slots("GACC") == NonceWindow.DEFAULT_WINDOW_SIZE - 1


# ---------------------------------------------------------------------------
# Thread-safety: concurrent acquire across workers
# ---------------------------------------------------------------------------


def test_concurrent_acquire_produces_unique_sequences() -> None:
    """All sequences issued under parallel pressure must be unique."""
    window = NonceWindow(window_size=64)
    window.acquire("GACC", seed=0)  # seed

    results: list[int] = []
    lock = threading.Lock()

    def worker(_: int) -> None:
        seq = window.acquire("GACC")
        with lock:
            results.append(seq)

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(worker, range(63)))  # 63 + 1 seed = 64 total

    assert len(results) == 63
    all_seqs = [0] + results
    assert len(set(all_seqs)) == 64, "Duplicate sequences detected"
    assert sorted(all_seqs) == list(range(64))


def test_concurrent_acquire_across_different_accounts_no_contention() -> None:
    """Workers on distinct accounts must never interfere."""
    window = NonceWindow(window_size=32)
    accounts = [f"G{i:04d}" for i in range(8)]

    # Seed each account deterministically before releasing workers.
    for i, acct in enumerate(accounts):
        window.acquire(acct, seed=1000 * (i + 1))

    collected: dict[str, list[int]] = {a: [] for a in accounts}
    collected_lock = threading.Lock()

    def worker(account: str) -> None:
        seq = window.acquire(account)
        with collected_lock:
            collected[account].append(seq)

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(worker, a) for a in accounts for _ in range(7)]
        for f in as_completed(futures):
            f.result()

    for acct, seqs in collected.items():
        assert len(set(seqs)) == len(seqs), f"Duplicates for {acct}"
        # Each account's sequences must form a contiguous range.
        assert set(seqs) == set(range(min(seqs), min(seqs) + len(seqs)))


def test_acknowledge_and_acquire_concurrent_no_data_race() -> None:
    """Mixed acquire/acknowledge calls under thread pressure must not corrupt state."""
    window = NonceWindow(window_size=8)
    issued: list[int] = []
    issued_lock = threading.Lock()
    errors: list[Exception] = []

    # Seed the window.
    window.acquire("GACC", seed=0)

    def acquirer(_: int) -> None:
        try:
            seq = window.acquire("GACC")
            with issued_lock:
                issued.append(seq)
        except RuntimeError:
            pass  # window full; acceptable under high concurrency

    def acknowledger(_: int) -> None:
        try:
            with issued_lock:
                if issued:
                    seq = issued[0]
            window.acknowledge("GACC", seq)
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [
            executor.submit(acquirer if i % 2 == 0 else acknowledger, i)
            for i in range(32)
        ]
        for f in as_completed(futures):
            f.result()

    assert not errors, f"Unexpected errors during concurrent test: {errors}"


# ---------------------------------------------------------------------------
# window_size property
# ---------------------------------------------------------------------------


def test_window_size_property_reflects_constructor_argument() -> None:
    assert NonceWindow(window_size=8).window_size == 8
    assert NonceWindow().window_size == NonceWindow.DEFAULT_WINDOW_SIZE


def test_invalid_window_size_raises() -> None:
    with pytest.raises(ValueError):
        NonceWindow(window_size=0)
    with pytest.raises(ValueError):
        NonceWindow(window_size=-1)
