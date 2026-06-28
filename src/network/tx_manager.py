"""Transaction broadcast ordering for transport-layer workers.

The manager in this module keeps sequence assignment, payload signing, and
network dispatch inside the same per-source-account critical section. That
prevents parallel broadcast workers from signing valid sequential payloads and
then sending them to the Stellar network out of order.
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, MutableMapping, Optional, Protocol
from .nonce_tracker import nonce_tracker

logger = logging.getLogger(__name__)

Payload = MutableMapping[str, Any]
Signer = Callable[[Payload], Payload]
Dispatcher = Callable[[Payload], Any]


class TxPayloadSigner(Protocol):
    def __call__(self, payload: Payload) -> Payload:
        """Sign and return a transaction payload."""


class TxPayloadDispatcher(Protocol):
    def __call__(self, payload: Payload) -> Any:
        """Dispatch a signed transaction payload."""


@dataclass
class BroadcastResult:
    """Result wrapper that exposes the assigned sequence for tracking."""

    account_id: str
    sequence: int
    payload: Payload
    dispatch_result: Any



@dataclass
class _AccountState:
    lock: threading.Lock


class TxManager:
    """Serialize signing and dispatch by account using atomic sequence counters."""

    def __init__(self, sequence_field: str = "sequence") -> None:
        self.sequence_field = sequence_field
        self._states: Dict[str, _AccountState] = {}
        self._states_lock = threading.Lock()

    def broadcast(
        self,
        account_id: str,
        payload: Payload,
        *,
        signer: Signer,
        dispatcher: Dispatcher,
        seed_sequence: Optional[int] = None,
    ) -> BroadcastResult:
        """Assign a sequence, sign the payload, and dispatch it in order.

        Args:
            account_id: Source account whose transaction sequence is tracked.
            payload: Transaction payload. It is deep-copied before mutation.
            signer: Callable that signs the sequenced payload and returns it.
            dispatcher: Callable that sends the signed payload to the network.
            seed_sequence: Required on first use for an account.

        Returns:
            BroadcastResult containing the assigned sequence, signed payload,
            and dispatcher response.
        """

        if not account_id:
            raise ValueError("account_id is required.")

        state = self._get_state(account_id)

        # Keep assignment, signing, and dispatch together. If signing is slow in
        # one worker, a later sequence cannot leapfrog it on the wire.
        with state.lock:
            sequence = nonce_tracker.get_next_nonce(account_id, seed_sequence)
            sequenced_payload = self._with_sequence(payload, sequence)
            signed_payload = signer(sequenced_payload)
            self._assert_signed_sequence(signed_payload, sequence)
            dispatch_result = dispatcher(signed_payload)

        logger.info(
            "[TxManager] Dispatched transaction for %s with sequence %d",
            account_id,
            sequence,
        )

        return BroadcastResult(
            account_id=account_id,
            sequence=sequence,
            payload=signed_payload,
            dispatch_result=dispatch_result,
        )

    def sync_sequence(self, account_id: str, sequence: int) -> None:
        """Set an account counter to a known-good sequence value."""

        nonce_tracker.sync_nonce(account_id, sequence)
        logger.info("[TxManager] Synced sequence for %s to %d", account_id, sequence)

    def invalidate(self, account_id: Optional[str] = None) -> None:
        """Clear one account sequence or all tracked account sequences."""

        if account_id is not None:
            nonce_tracker.invalidate(account_id)
            logger.info("[TxManager] Invalidated sequence for %s", account_id)
            return

        nonce_tracker.invalidate()

        logger.info("[TxManager] Invalidated all tracked sequences")

    def current_sequence(self, account_id: str) -> Optional[int]:
        """Return the cached sequence for an account, if seeded."""

        return nonce_tracker.get_nonce(account_id)

    def _get_state(self, account_id: str) -> _AccountState:
        state = self._states.get(account_id)
        if state is not None:
            return state

        with self._states_lock:
            state = self._states.get(account_id)
            if state is None:
                state = _AccountState(
                    lock=threading.Lock(),
                )
                self._states[account_id] = state
            return state

    def _with_sequence(self, payload: Payload, sequence: int) -> Payload:
        sequenced_payload = copy.deepcopy(dict(payload))
        sequenced_payload[self.sequence_field] = sequence
        return sequenced_payload

    def _assert_signed_sequence(self, payload: Payload, sequence: int) -> None:
        if payload.get(self.sequence_field) != sequence:
            raise ValueError(
                "Signer returned a payload with a mismatched sequence value."
            )


tx_manager = TxManager()

__all__ = [
    "BroadcastResult",
    "TxManager",
    "tx_manager",
]
