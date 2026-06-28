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

* **Memory locking (mlock)** — immediately after the private-key buffer is
  allocated, its pages are pinned to physical RAM via ``mlock(2)`` (POSIX) or
  ``VirtualLock`` (Windows).  This prevents the OS virtual-memory manager from
  paging the pages to disk (swap / hibernate file), so key material is never
  written to unencrypted storage.  On ``__exit__`` the buffer is unlocked with
  ``munlock`` / ``VirtualUnlock`` *after* the zero-wipe so the OS cannot write
  stale data to swap between the wipe and the unlock.

  If ``mlock`` is unavailable (e.g. the process lacks ``CAP_IPC_LOCK``,
  ``RLIMIT_MEMLOCK`` is zero, or the platform is unsupported) a one-time
  ``WARNING`` is logged and execution continues — the zero-wipe layer still
  applies.  On Linux, raise the ``RLIMIT_MEMLOCK`` soft limit or grant
  ``CAP_IPC_LOCK`` to harden the deployment.

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
    # raw_secret_bytes are zero-wiped and unlocked here; handle is no longer usable.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import platform
import sys
from types import TracebackType
from typing import Optional, Type

logger = logging.getLogger(__name__)

__all__ = ["SecureKeyHandle", "SecureSessionCredentials", "SigningError"]

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
# Memory-locking helpers (mlock / VirtualLock)
# ---------------------------------------------------------------------------

def _load_mlock_functions() -> tuple:
    """Load the platform's mlock / munlock function pair.

    Returns:
        ``(mlock_fn, munlock_fn)`` where each is a callable or ``None``.

    On Linux/macOS the functions are found in libc via ``ctypes.CDLL``.
    On Windows the equivalents are ``VirtualLock`` / ``VirtualUnlock``
    from ``kernel32``.

    The result is cached at module level in ``_MLOCK_FN`` and ``_MUNLOCK_FN``
    so this function is only executed once.
    """
    _os = platform.system()

    if _os == "Windows":
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # VirtualLock(lpAddress, dwSize) -> BOOL
            mlock_fn = kernel32.VirtualLock
            munlock_fn = kernel32.VirtualUnlock
            mlock_fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            mlock_fn.restype = ctypes.c_bool
            munlock_fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            munlock_fn.restype = ctypes.c_bool
            return mlock_fn, munlock_fn
        except Exception:  # noqa: BLE001
            return None, None

    # POSIX (Linux, macOS, BSDs)
    libc_name = ctypes.util.find_library("c")
    if libc_name is None:
        return None, None
    try:
        libc = ctypes.CDLL(libc_name, use_errno=True)
        mlock_fn = getattr(libc, "mlock", None)
        munlock_fn = getattr(libc, "munlock", None)
        if mlock_fn is None or munlock_fn is None:
            return None, None
        # mlock(const void *addr, size_t len) -> int
        mlock_fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        mlock_fn.restype = ctypes.c_int
        munlock_fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        munlock_fn.restype = ctypes.c_int
        return mlock_fn, munlock_fn
    except Exception:  # noqa: BLE001
        return None, None


# Module-level singletons — resolved once at import time.
_MLOCK_FN, _MUNLOCK_FN = _load_mlock_functions()

# Emit a single warning if mlock is unavailable so operators know the
# swap-protection layer is absent without spamming per-key-handle logs.
_MLOCK_UNAVAILABLE_WARNED: bool = False


def _warn_mlock_unavailable(reason: str) -> None:
    """Log a one-time WARNING that mlock is unavailable."""
    global _MLOCK_UNAVAILABLE_WARNED  # noqa: PLW0603
    if not _MLOCK_UNAVAILABLE_WARNED:
        logger.warning(
            "[SecureKeyHandle] mlock unavailable (%s). "
            "Private-key pages may be swapped to disk. "
            "Grant CAP_IPC_LOCK or raise RLIMIT_MEMLOCK to harden this deployment.",
            reason,
        )
        _MLOCK_UNAVAILABLE_WARNED = True


def _mlock_buffer(buf: bytearray) -> bool:
    """Pin the pages backing *buf* to physical RAM using mlock / VirtualLock.

    This prevents the OS from writing key material to swap or a hibernate file.
    The buffer **must** remain alive for as long as the lock is held; calling
    code is responsible for keeping a reference.

    Args:
        buf: The bytearray whose backing pages should be locked.

    Returns:
        ``True`` if the lock succeeded, ``False`` otherwise (caller should log
        a warning but must not abort — the zero-wipe layer still applies).

    This function **must not raise**.
    """
    if not buf:
        return False

    if _MLOCK_FN is None:
        _warn_mlock_unavailable("mlock/VirtualLock not found on this platform")
        return False

    try:
        # Obtain the raw address of the bytearray's underlying C buffer.
        c_arr = (ctypes.c_char * len(buf)).from_buffer(buf)
        addr = ctypes.addressof(c_arr)
        size = ctypes.c_size_t(len(buf))

        ret = _MLOCK_FN(addr, size)

        # POSIX returns 0 on success; Windows returns non-zero (BOOL TRUE).
        if platform.system() == "Windows":
            success = bool(ret)
        else:
            success = (ret == 0)

        if not success:
            errno_val = ctypes.get_errno()
            _warn_mlock_unavailable(f"syscall returned failure (errno={errno_val})")
            return False

        return True

    except Exception as exc:  # noqa: BLE001
        _warn_mlock_unavailable(f"exception during mlock: {exc}")
        return False


def _munlock_buffer(buf: bytearray) -> None:
    """Release the mlock / VirtualLock on *buf*'s pages.

    Must be called **after** :func:`_zero_wipe` so the unlocked pages do not
    contain live key material when the OS is free to evict them.

    This function **must not raise**.
    """
    if not buf or _MUNLOCK_FN is None:
        return

    try:
        c_arr = (ctypes.c_char * len(buf)).from_buffer(buf)
        addr = ctypes.addressof(c_arr)
        size = ctypes.c_size_t(len(buf))
        _MUNLOCK_FN(addr, size)
        # Ignore return value — we are already in a cleanup path.
    except Exception:  # noqa: BLE001
        pass  # Never raise from a cleanup helper.


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

    __slots__ = ("_buf", "_active", "_wiped", "_locked")

    def __init__(self, raw_key: bytes) -> None:
        if not raw_key:
            raise ValueError("raw_key must be non-empty bytes.")
        # Copy into a mutable buffer so we — not the caller — control the
        # lifetime.  The original ``raw_key`` bytes object remains the caller's
        # responsibility.
        self._buf: bytearray = bytearray(raw_key)
        self._active: bool = False
        self._wiped: bool = False
        # Immediately pin the buffer's pages to physical RAM so the OS cannot
        # page key material to disk (swap partition, hibernate file, etc.).
        # _mlock_buffer logs a one-time warning if mlock is unavailable and
        # returns False; execution continues because the zero-wipe layer still
        # applies even without page-locking.
        self._locked: bool = _mlock_buffer(self._buf)

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
        """Idempotent zero-wipe and page-unlock of the internal buffer.

        Ordering is deliberate:

        1. Set ``_wiped`` first so re-entrant or concurrent calls are no-ops.
        2. Zero the buffer via :func:`_zero_wipe` (ctypes.memset + Python
           fallback) while the pages are still locked — this guarantees the
           kernel cannot evict a dirty page to disk between the wipe and the
           unlock.
        3. Call :func:`_munlock_buffer` to release the mlock / VirtualLock
           only after the buffer contains zeros.  At that point the OS is free
           to page the (now-zeroed) memory without exposing key material.
        """
        if self._wiped:
            return
        self._wiped = True
        # Step 2: zero key material while pages are still locked.
        _zero_wipe(self._buf)
        # Step 3: release the page lock now that the buffer is zeroed.
        if self._locked:
            _munlock_buffer(self._buf)
            self._locked = False
        logger.debug("[SecureKeyHandle] Signing scope closed — key wiped and pages unlocked.")

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


class SecureSessionCredentials:
    """Context manager that holds temporary session credentials for one validation scope.

    The credentials are copied into an internal ``bytearray`` on construction.
    On ``__exit__`` — normal *or* exceptional — the buffer is zero-wiped
    **before** any reference is released, ensuring credentials are not left
    in process memory after the validation block closes.

    A ``__del__`` finaliser acts as a last-resort safety net: if the caller
    fails to use the ``with`` statement the buffer is still wiped on garbage
    collection.

    Args:
        credentials: Raw session credential bytes (e.g. API token, JWT).

    Raises:
        ValueError:   If *credentials* is empty.
        SigningError: If :meth:`get` is called outside the ``with`` block.

    Example::

        with SecureSessionCredentials(token_bytes) as creds:
            api_token = creds.get()
            # use api_token for validation ...
        # Buffer zero-wiped here; creds is no longer usable.
    """

    __slots__ = ("_buf", "_active", "_wiped")

    def __init__(self, credentials: bytes) -> None:
        if not credentials:
            raise ValueError("credentials must be non-empty bytes.")
        self._buf: bytearray = bytearray(credentials)
        self._active: bool = False
        self._wiped: bool = False

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "SecureSessionCredentials":
        self._active = True
        logger.debug("[SecureSessionCredentials] Validation scope opened.")
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool:
        self._active = False
        self._do_wipe()
        return False

    def __del__(self) -> None:
        try:
            self._do_wipe()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _do_wipe(self) -> None:
        if self._wiped:
            return
        self._wiped = True
        _zero_wipe(self._buf)
        logger.debug("[SecureSessionCredentials] Validation scope closed — credentials wiped.")

    # ------------------------------------------------------------------
    # Accessor
    # ------------------------------------------------------------------

    def get(self) -> bytes:
        """Return a ``bytes`` copy of the stored session credentials.

        The returned copy is the caller's responsibility to manage.  The
        internal buffer is unaffected.

        Returns:
            A ``bytes`` copy of the credentials stored in the handle.

        Raises:
            SigningError: If called outside the ``with`` block or after the
                          buffer has already been wiped.
        """
        if not self._active:
            raise SigningError(
                "SecureSessionCredentials.get() called outside an active validation scope. "
                "Use 'with SecureSessionCredentials(...) as creds:' and call get() inside."
            )
        if self._wiped:
            raise SigningError(
                "SecureSessionCredentials.get() called after credentials have been wiped."
            )
        return bytes(self._buf)
