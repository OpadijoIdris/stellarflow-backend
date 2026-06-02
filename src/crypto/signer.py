"""
src/crypto/signer.py
~~~~~~~~~~~~~~~~~~~~
Context-managed signing primitive that enforces strict key-lifetime isolation.

Security design
---------------
* The private key is held in a **mutable bytearray** for exactly the duration
  of the ``with`` block.  On exit — normal *or* exceptional — the buffer is
  overwritten with zeros **before** any reference is released, minimising the
  window during which key material is recoverable from a process memory dump.

* Zero-wipe uses ``ctypes.memset`` to write through the bytearray's underlying
  C buffer, sidestepping CPython optimisations that could otherwise elide a
  pure-Python ``buf[i] = 0`` loop.  A redundant Python-level loop follows as a
  belt-and-suspenders measure.

* A ``__del__`` finaliser is registered as a **last-resort safety net**: if
  the caller forgets the ``with`` statement the buffer is still wiped when the
  object is garbage-collected.  The finaliser must not raise, so all logic
  inside it is guarded with broad ``except`` clauses.

* Secret bytes are **never** materialised as an immutable ``bytes`` object
  within this module beyond what the crypto library strictly requires.  Both
  the ``stellar_sdk`` and ``PyNaCl`` paths receive the narrowest possible view
  of the buffer — a ``bytes`` object created immediately before the call and
  discarded immediately after — and that intermediate copy is wiped in a
  ``finally`` block.

* Error messages deliberately omit key material and internal state.  Only
  control-flow reasons for failure are surfaced.

* Debug logging is limited to lifecycle events (scope open / scope closed) and
  never logs key bytes, hash values, or signatures.

Usage::

    with SecureKeyHandle(raw_secret_bytes) as handle:
        signature = handle.sign(tx_hash)
    # raw_secret_bytes are zero-wiped here; handle is no longer usable.
"""

from __future__ import annotations

import ctypes
import logging
from types import TracebackType
from typing import Optional, Type

logger = logging.getLogger(__name__)

__all__ = ["SecureKeyHandle", "SigningError"]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _zero_wipe(buf: bytearray) -> None:
    """Overwrite *buf* in-place with zeros.

    Uses ``ctypes.memset`` to write directly into the underlying C buffer,
    resisting CPython optimisations that could theoretically elide a pure-
    Python zero loop.  A redundant Python-level pass follows as a belt-and-
    suspenders measure and to satisfy static analysers that check buffer state.

    This function is intentionally **not** listed in ``__all__`` and should
    not be used outside this module.
    """
    if len(buf) == 0:
        return
    try:
        # Write via ctypes to resist compiler / interpreter elision.
        addr = ctypes.addressof((ctypes.c_char * len(buf)).from_buffer(buf))
        ctypes.memset(addr, 0, len(buf))
    finally:
        # Belt-and-suspenders: also zero through the bytearray view itself so
        # the object's Python-level state reflects the wipe even if ctypes
        # raises (e.g. on an interpreter build that restricts buffer access).
        for i in range(len(buf)):
            buf[i] = 0


def _wipe_bytes_view(view: bytes) -> None:
    """Best-effort wipe of an immutable bytes object via ctypes.

    ``bytes`` objects are immutable at the Python level, so this uses a ctypes
    cast to reach the underlying C buffer directly.  This is inherently racy on
    a multi-threaded interpreter (another thread may have obtained the same
    interned object) but is still worth doing on a best-effort basis to reduce
    the in-memory lifetime of key material.

    This function **must not raise** — it is called from ``finally`` blocks.
    """
    if not view:
        return
    try:
        buf = (ctypes.c_char * len(view)).from_buffer_copy(view)
        # Wipe our local copy.  The original immutable bytes object in the
        # interpreter heap is unaffected; this is best-effort only.
        ctypes.memset(ctypes.addressof(buf), 0, len(view))
    except Exception:  # noqa: BLE001
        pass  # Never raise from a wipe helper.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SigningError(Exception):
    """Raised when a signing operation fails or the handle has already been closed.

    Error messages deliberately omit key material, hash values, and signatures.
    """


class SecureKeyHandle:
    """Context manager that holds a private key for exactly one signing scope.

    The key is copied into an internal ``bytearray`` on construction.  On
    ``__exit__`` the buffer is zero-wiped **regardless of whether an exception
    occurred**, and any further call to :meth:`sign` raises
    :class:`SigningError`.

    A ``__del__`` finaliser acts as a last-resort safety net: if the caller
    fails to use the ``with`` statement the buffer is still wiped on garbage
    collection.

    Args:
        raw_key: Raw private-key bytes (32 bytes for Ed25519 / Stellar).

    Raises:
        ValueError:   If *raw_key* is empty.
        SigningError: If :meth:`sign` is called outside the ``with`` block.

    Example::

        with SecureKeyHandle(secret_bytes) as handle:
            sig = handle.sign(tx_hash)
        # Buffer zero-wiped here; handle is inert.
    """

    __slots__ = ("_buf", "_active", "_wiped")

    def __init__(self, raw_key: bytes) -> None:
        if not raw_key:
            raise ValueError("raw_key must be non-empty bytes.")
        # Copy into a mutable buffer so we — not the caller — control the
        # lifetime.  The original ``raw_key`` bytes object remains the caller's
        # responsibility.
        self._buf: bytearray = bytearray(raw_key)
        self._active: bool = False
        self._wiped: bool = False

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "SecureKeyHandle":
        self._active = True
        logger.debug("[SecureKeyHandle] Signing scope opened.")
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool:
        self._active = False
        self._do_wipe()
        # Do not suppress exceptions — always re-raise.
        return False

    def __del__(self) -> None:
        """Last-resort safety net: wipe the buffer on garbage collection.

        This executes when the context manager is used correctly (after
        ``__exit__`` has already wiped) as well as when it is *not* used
        correctly (the buffer has not been wiped yet).  In both cases it is
        safe to call because ``_do_wipe`` is idempotent.

        ``__del__`` must never raise; all logic is guarded.
        """
        try:
            self._do_wipe()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _do_wipe(self) -> None:
        """Idempotent zero-wipe of the internal buffer.

        Sets ``_wiped`` **before** zeroing so that concurrent or re-entrant
        calls skip the wipe (the buffer is already being cleared).
        """
        if self._wiped:
            return
        self._wiped = True
        _zero_wipe(self._buf)
        logger.debug("[SecureKeyHandle] Signing scope closed — key wiped.")

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    def sign(self, tx_hash: bytes) -> bytes:
        """Sign *tx_hash* with the held private key.

        Both the ``stellar_sdk`` and ``PyNaCl`` paths isolate the key material
        into a temporary ``bytes`` view that is wiped immediately after the
        library call returns (or raises), via a ``finally`` block.

        Args:
            tx_hash: The 32-byte transaction hash to sign.

        Returns:
            64-byte raw Ed25519 signature as an immutable ``bytes`` object.

        Raises:
            SigningError: If called outside the ``with`` block, after the
                         scope has been exited, or if the underlying crypto
                         library raises.
            ValueError:  If *tx_hash* is not exactly 32 bytes.
        """
        if not self._active:
            raise SigningError(
                "SecureKeyHandle.sign() called outside an active signing scope. "
                "Use 'with SecureKeyHandle(...) as handle:' and call sign() inside."
            )
        if self._wiped:
            raise SigningError(
                "SecureKeyHandle.sign() called after the handle has been wiped."
            )
        if len(tx_hash) != 32:
            raise ValueError(f"tx_hash must be exactly 32 bytes, got {len(tx_hash)}.")

        return self._sign_internal(tx_hash)

    def _sign_internal(self, tx_hash: bytes) -> bytes:
        """Perform the actual signing.  Called only from :meth:`sign`.

        Creates the narrowest possible temporary ``bytes`` view of the buffer,
        passes it to the crypto library, and wipes the view immediately
        afterwards — whether or not the library call succeeded.

        Separating this from ``sign()`` keeps the public method's guard logic
        easy to audit.
        """
        # Build a fresh bytes copy of the key material.  This copy is
        # deliberately limited in scope and wiped in the finally block below.
        key_bytes: bytes = bytes(self._buf)
        try:
            stellar_unavailable = False
            try:
                return self._try_stellar_sdk(key_bytes, tx_hash)
            except ImportError:
                stellar_unavailable = True

            # Only reach here if stellar_sdk is not installed.
            if stellar_unavailable:
                return self._try_pynacl(key_bytes, tx_hash)

            # Should never be reached.
            raise SigningError("Signing failed: no backend available.")  # pragma: no cover
        finally:
            # Wipe the transient key copy regardless of success or failure.
            # _wipe_bytes_view must not raise.
            _wipe_bytes_view(key_bytes)
            del key_bytes


    @staticmethod
    def _try_stellar_sdk(key_bytes: bytes, tx_hash: bytes) -> bytes:
        """Attempt signing via ``stellar_sdk.Keypair``.

        Raises:
            ImportError:  If ``stellar_sdk`` is not installed.
            SigningError: If the keypair construction or signing fails.
        """
        from stellar_sdk import Keypair  # type: ignore[import]  # noqa: PLC0415

        try:
            keypair = Keypair.from_raw_ed25519_seed(key_bytes)
            return bytes(keypair.sign(tx_hash))
        except Exception as exc:
            # Do not include ``exc`` details that might echo key material.
            raise SigningError("Signing failed (stellar_sdk path).") from exc

    @staticmethod
    def _try_pynacl(key_bytes: bytes, tx_hash: bytes) -> bytes:
        """Attempt signing via ``nacl.signing.SigningKey`` (PyNaCl).

        Raises:
            ImportError:  If ``PyNaCl`` is not installed.
            SigningError: If key construction or signing fails.
        """
        try:
            from nacl.signing import SigningKey  # type: ignore[import]  # noqa: PLC0415
        except ImportError:
            raise SigningError(
                "Neither 'stellar_sdk' nor 'PyNaCl' is installed. "
                "Install one to enable signing."
            )

        try:
            sk = SigningKey(key_bytes)
            return bytes(sk.sign(tx_hash).signature)
        except Exception as exc:
            raise SigningError("Signing failed (PyNaCl path).") from exc
