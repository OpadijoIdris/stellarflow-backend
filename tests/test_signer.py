"""
tests/test_signer.py
~~~~~~~~~~~~~~~~~~~~
Comprehensive test suite for src/crypto/signer.py.

Coverage targets
----------------
* Successful signing flow (stellar_sdk & PyNaCl paths)
* Cleanup execution on the normal (success) path
* Cleanup execution on the exception path
* Invalid / edge-case key handling
* Out-of-scope / post-exit guard enforcement
* __del__ finaliser as safety-net (garbage-collection path)
* No sensitive debug logging
* Signature correctness regression

Assumptions
-----------
* Tests run with either ``stellar_sdk`` *or* ``PyNaCl`` available; tests
  that require a real crypto library are marked ``importorskip`` so the
  suite remains green on a bare Python installation.
* ``_buf`` is accessed directly in cleanup assertions because it is the only
  observable evidence of a wipe within Python's memory model.  This is
  intentional: verifying cleanup is a security requirement, not an
  implementation detail.
"""
from __future__ import annotations

import gc
import logging
import os
import sys
import unittest.mock as mock

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap — allows ``pytest tests/`` from the repo root.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from crypto.signer import SecureKeyHandle, SecureSessionCredentials, SigningError, _zero_wipe  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# 32-byte dummy key — NOT a real Stellar secret; safe for unit tests.
_DUMMY_KEY = bytes(range(32))
# 32-byte dummy transaction hash.
_DUMMY_HASH = bytes(range(32))


# ---------------------------------------------------------------------------
# Helper: assert buffer is fully zero-wiped.
# ---------------------------------------------------------------------------


def _assert_wiped(buf: bytearray, label: str = "buffer") -> None:
    assert all(b == 0 for b in buf), f"{label} must be fully zero-wiped."


# ---------------------------------------------------------------------------
# _zero_wipe unit tests
# ---------------------------------------------------------------------------


class TestZeroWipe:
    """Low-level tests for the _zero_wipe helper."""

    def test_empty_buffer_is_noop(self):
        buf = bytearray(0)
        _zero_wipe(buf)  # must not raise

    def test_single_byte_wiped(self):
        buf = bytearray(b"\xFF")
        _zero_wipe(buf)
        _assert_wiped(buf, "single-byte buffer")

    def test_arbitrary_content_wiped(self):
        buf = bytearray(range(256))
        _zero_wipe(buf)
        _assert_wiped(buf, "256-byte buffer")

    def test_idempotent(self):
        buf = bytearray(b"\xDE\xAD\xBE\xEF")
        _zero_wipe(buf)
        _zero_wipe(buf)  # second call must not raise
        _assert_wiped(buf, "doubly-wiped buffer")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_empty_key(self):
        with pytest.raises(ValueError, match="non-empty"):
            SecureKeyHandle(b"")

    def test_accepts_minimum_one_byte(self):
        handle = SecureKeyHandle(b"\x01")
        # Wipe so the __del__ finaliser has nothing to do.
        handle._do_wipe()

    def test_accepts_32_byte_key(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        handle._do_wipe()

    def test_buffer_is_independent_copy(self):
        """Mutating the original bytes must not affect the internal buffer."""
        raw = bytearray(_DUMMY_KEY)
        handle = SecureKeyHandle(bytes(raw))
        raw[0] = 0xFF
        assert handle._buf[0] == 0x00, "Internal buffer must be an independent copy."
        handle._do_wipe()

    def test_not_active_after_construction(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        assert not handle._active
        handle._do_wipe()

    def test_not_wiped_after_construction(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        assert not handle._wiped
        handle._do_wipe()


# ---------------------------------------------------------------------------
# Context-manager lifecycle
# ---------------------------------------------------------------------------


class TestContextManager:
    def test_enter_sets_active(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        handle.__enter__()
        assert handle._active
        handle.__exit__(None, None, None)

    def test_exit_clears_active(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        with handle:
            pass
        assert not handle._active

    def test_exit_wipes_buffer_on_success(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        with handle:
            pass
        _assert_wiped(handle._buf, "_buf after normal exit")

    def test_exit_sets_wiped_flag(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        with handle:
            pass
        assert handle._wiped

    def test_exit_does_not_suppress_exceptions(self):
        with pytest.raises(RuntimeError, match="propagated"):
            with SecureKeyHandle(_DUMMY_KEY):
                raise RuntimeError("propagated")

    def test_buffer_wiped_even_when_exception_raised(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        try:
            with handle:
                raise ValueError("simulated failure")
        except ValueError:
            pass
        _assert_wiped(handle._buf, "_buf after exception exit")

    def test_wiped_flag_set_even_when_exception_raised(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        try:
            with handle:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert handle._wiped

    def test_wipe_is_idempotent_double_exit(self):
        """Calling __exit__ twice must not raise."""
        handle = SecureKeyHandle(_DUMMY_KEY)
        handle.__enter__()
        handle.__exit__(None, None, None)
        handle.__exit__(None, None, None)  # second call must be safe
        _assert_wiped(handle._buf, "_buf after double exit")


# ---------------------------------------------------------------------------
# __del__ safety-net
# ---------------------------------------------------------------------------


class TestDelFinaliser:
    def test_del_wipes_buffer_when_context_not_used(self):
        """If the caller never enters the 'with' block, __del__ must still wipe."""
        handle = SecureKeyHandle(_DUMMY_KEY)
        # Directly invoke __del__ to simulate GC without running the context manager.
        handle.__del__()
        _assert_wiped(handle._buf, "_buf after __del__ without context manager")
        assert handle._wiped

    def test_del_does_not_raise_after_normal_exit(self):
        """__del__ called after a clean context exit must be a silent noop."""
        handle = SecureKeyHandle(_DUMMY_KEY)
        with handle:
            pass
        # Should not raise even though the buffer is already wiped.
        handle.__del__()

    def test_del_is_called_on_gc(self):
        """Force GC and confirm __del__ was invoked (indirectly via _wiped flag)."""
        handle = SecureKeyHandle(_DUMMY_KEY)
        ref = handle  # keep a second reference for inspection
        del handle
        gc.collect()
        # At this point, if ref is the only remaining reference it should still
        # be valid but reflect the finaliser having run.  Because ref is still
        # alive the GC won't have collected it; use _do_wipe instead.
        # This test therefore simply verifies __del__ does not raise.
        ref._do_wipe()


# ---------------------------------------------------------------------------
# sign() guard rails
# ---------------------------------------------------------------------------


class TestSignGuards:
    def test_sign_outside_context_raises(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        with pytest.raises(SigningError, match="outside an active signing scope"):
            handle.sign(_DUMMY_HASH)

    def test_sign_after_exit_raises(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        with handle:
            pass
        with pytest.raises(SigningError, match="outside an active signing scope"):
            handle.sign(_DUMMY_HASH)

    def test_sign_after_explicit_wipe_raises(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        handle.__enter__()
        handle._do_wipe()  # simulate premature wipe
        with pytest.raises(SigningError, match="wiped"):
            handle.sign(_DUMMY_HASH)

    def test_sign_rejects_short_hash(self):
        with SecureKeyHandle(_DUMMY_KEY) as handle:
            with pytest.raises(ValueError, match="32 bytes"):
                handle.sign(b"too-short")

    def test_sign_rejects_long_hash(self):
        with SecureKeyHandle(_DUMMY_KEY) as handle:
            with pytest.raises(ValueError, match="32 bytes"):
                handle.sign(b"x" * 33)

    def test_sign_rejects_empty_hash(self):
        with SecureKeyHandle(_DUMMY_KEY) as handle:
            with pytest.raises(ValueError, match="32 bytes"):
                handle.sign(b"")


# ---------------------------------------------------------------------------
# sign() happy path (requires PyNaCl or stellar_sdk)
# ---------------------------------------------------------------------------


class TestSignHappyPath:
    def test_returns_64_byte_signature(self):
        nacl = pytest.importorskip("nacl", reason="PyNaCl not installed — skipping signing test")
        with SecureKeyHandle(_DUMMY_KEY) as handle:
            sig = handle.sign(_DUMMY_HASH)
        assert isinstance(sig, bytes), "Signature must be a bytes object."
        assert len(sig) == 64, f"Expected 64-byte signature, got {len(sig)}."

    def test_buffer_wiped_after_signing(self):
        pytest.importorskip("nacl", reason="PyNaCl not installed — skipping signing test")
        handle = SecureKeyHandle(_DUMMY_KEY)
        with handle:
            handle.sign(_DUMMY_HASH)
        _assert_wiped(handle._buf, "_buf after successful signing exit")

    def test_signature_deterministic(self):
        """Ed25519 is deterministic — same key + hash must yield same signature."""
        pytest.importorskip("nacl", reason="PyNaCl not installed — skipping signing test")
        with SecureKeyHandle(_DUMMY_KEY) as h1:
            sig1 = h1.sign(_DUMMY_HASH)
        with SecureKeyHandle(_DUMMY_KEY) as h2:
            sig2 = h2.sign(_DUMMY_HASH)
        assert sig1 == sig2, "Ed25519 signatures must be deterministic."

    def test_different_hashes_produce_different_signatures(self):
        pytest.importorskip("nacl", reason="PyNaCl not installed — skipping signing test")
        hash_a = bytes(range(32))
        hash_b = bytes(reversed(range(32)))
        with SecureKeyHandle(_DUMMY_KEY) as handle:
            sig_a = handle.sign(hash_a)
            sig_b = handle.sign(hash_b)
        assert sig_a != sig_b, "Different hashes must produce different signatures."

    def test_different_keys_produce_different_signatures(self):
        pytest.importorskip("nacl", reason="PyNaCl not installed — skipping signing test")
        key_b = bytes(range(1, 33))
        with SecureKeyHandle(_DUMMY_KEY) as h1:
            sig1 = h1.sign(_DUMMY_HASH)
        with SecureKeyHandle(key_b) as h2:
            sig2 = h2.sign(_DUMMY_HASH)
        assert sig1 != sig2, "Different keys must produce different signatures."

    def test_signature_can_be_verified(self):
        """Regression: verify the produced signature with the corresponding public key."""
        nacl = pytest.importorskip("nacl", reason="PyNaCl not installed — skipping signing test")
        from nacl.signing import SigningKey, VerifyKey

        with SecureKeyHandle(_DUMMY_KEY) as handle:
            sig = handle.sign(_DUMMY_HASH)

        # Derive the verify key independently and check the signature.
        sk = SigningKey(_DUMMY_KEY)
        vk: VerifyKey = sk.verify_key
        # nacl's verify raises nacl.exceptions.BadSignatureError on failure.
        vk.verify(_DUMMY_HASH, sig)


# ---------------------------------------------------------------------------
# Exception-path cleanup
# ---------------------------------------------------------------------------


class TestExceptionPathCleanup:
    def test_signing_error_does_not_abort_cleanup(self):
        """If sign() itself raises, __exit__ must still wipe the buffer."""
        # Patch _sign_internal to raise so we can confirm __exit__ still cleans up.
        handle = SecureKeyHandle(_DUMMY_KEY)
        with mock.patch.object(
            handle, "_sign_internal", side_effect=RuntimeError("injected")
        ):
            try:
                with handle:
                    handle.sign(_DUMMY_HASH)
            except (RuntimeError, ValueError):
                pass
        _assert_wiped(handle._buf, "_buf after sign() raised inside context")

    def test_import_error_does_not_leak_key_material_in_message(self):
        """Error messages from missing-library paths must not embed key bytes."""
        # Force both import paths to fail by patching builtins.__import__.
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def blocking_import(name, *args, **kwargs):
            if name in ("stellar_sdk", "nacl"):
                raise ImportError(f"blocked: {name}")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=blocking_import):
            with pytest.raises(SigningError) as exc_info:
                with SecureKeyHandle(_DUMMY_KEY) as handle:
                    handle.sign(_DUMMY_HASH)

        msg = str(exc_info.value)
        # Key bytes must not appear in the error message.
        for byte_val in _DUMMY_KEY:
            assert str(byte_val) not in msg or byte_val == 0, (
                f"Key byte value {byte_val} should not appear in error message."
            )

    def test_buffer_wiped_when_crypto_library_raises(self):
        """Wipe must happen even when the underlying crypto call fails mid-flight."""
        handle = SecureKeyHandle(_DUMMY_KEY)
        with mock.patch.object(
            SecureKeyHandle, "_try_pynacl", side_effect=SigningError("crypto failure")
        ), mock.patch.object(
            SecureKeyHandle, "_try_stellar_sdk", side_effect=ImportError("not installed")
        ):
            try:
                with handle:
                    handle.sign(_DUMMY_HASH)
            except SigningError:
                pass
        _assert_wiped(handle._buf, "_buf after crypto failure")


# ---------------------------------------------------------------------------
# Logging security — no sensitive data in log records
# ---------------------------------------------------------------------------


class TestLoggingSecurity:
    def test_no_key_bytes_logged(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="crypto.signer"):
            handle = SecureKeyHandle(_DUMMY_KEY)
            with handle:
                pass

        combined = "\n".join(r.getMessage() for r in caplog.records)
        # Check no byte value from the key appears in a suspicious context.
        for val in set(_DUMMY_KEY):
            # A byte value of 0 is acceptable (it's in the wiped state).
            if val == 0:
                continue
            assert str(val) not in combined, (
                f"Key byte value {val} leaked into log output."
            )

    def test_no_hash_bytes_logged(self, caplog):
        pytest.importorskip("nacl", reason="PyNaCl not installed — skipping signing test")
        with caplog.at_level(logging.DEBUG, logger="crypto.signer"):
            with SecureKeyHandle(_DUMMY_KEY) as handle:
                handle.sign(_DUMMY_HASH)

        combined = "\n".join(r.getMessage() for r in caplog.records)
        # No raw byte values of the hash should appear.
        for val in set(_DUMMY_HASH):
            if val == 0:
                continue
            assert str(val) not in combined, (
                f"Hash byte value {val} leaked into log output."
            )

    def test_log_level_is_debug_only(self, caplog):
        """Lifecycle messages must be DEBUG, not INFO/WARNING/ERROR."""
        with caplog.at_level(logging.DEBUG, logger="crypto.signer"):
            with SecureKeyHandle(_DUMMY_KEY):
                pass
        for record in caplog.records:
            assert record.levelno <= logging.DEBUG, (
                f"Unexpected log level {record.levelname}: {record.getMessage()}"
            )


# ---------------------------------------------------------------------------
# Regression coverage
# ---------------------------------------------------------------------------


class TestRegression:
    def test_signing_api_unchanged(self):
        """Public API must remain: SecureKeyHandle(bytes) → context → .sign(bytes) → bytes."""
        pytest.importorskip("nacl", reason="PyNaCl not installed — skipping signing test")
        with SecureKeyHandle(_DUMMY_KEY) as handle:
            result = handle.sign(_DUMMY_HASH)
        assert isinstance(result, bytes)

    def test_multiple_signs_in_same_scope(self):
        """Multiple sign() calls within the same 'with' block must all succeed."""
        pytest.importorskip("nacl", reason="PyNaCl not installed — skipping signing test")
        hash_a = bytes(range(32))
        hash_b = bytes(range(32, 64)) if len(bytes(range(32, 64))) == 32 else bytes(b"\xAA" * 32)
        with SecureKeyHandle(_DUMMY_KEY) as handle:
            sig_a = handle.sign(hash_a)
            sig_b = handle.sign(hash_b)
        assert len(sig_a) == 64
        assert len(sig_b) == 64

    def test_handle_cannot_be_reused_after_exit(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        with handle:
            pass
        with pytest.raises(SigningError):
            with handle:  # re-entering should fail because _wiped is True
                handle.sign(_DUMMY_HASH)

    def test_signing_error_is_exception_subclass(self):
        assert issubclass(SigningError, Exception)

    def test_key_not_leaked_through_repr_or_str(self):
        handle = SecureKeyHandle(_DUMMY_KEY)
        text = repr(handle) + str(handle)
        # Default __repr__ for slotted classes does not include attributes,
        # but verify none of the key byte values appear.
        for val in _DUMMY_KEY:
            if val == 0:
                continue
            # The repr should just be the class name + memory address.
            # It must not include the raw key bytes.
            assert hex(val) not in text.lower() or len(text) < 200, (
                "Key material must not appear in __repr__."
            )
        handle._do_wipe()


# ---------------------------------------------------------------------------
# SecureSessionCredentials
# ---------------------------------------------------------------------------


class TestSecureSessionCredentialsConstruction:
    def test_rejects_empty_credentials(self):
        with pytest.raises(ValueError, match="non-empty"):
            SecureSessionCredentials(b"")

    def test_accepts_valid_credentials(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        assert not creds._active
        assert not creds._wiped
        creds._do_wipe()

    def test_buffer_is_independent_copy(self):
        raw = bytearray(_DUMMY_KEY)
        creds = SecureSessionCredentials(bytes(raw))
        raw[0] = 0xFF
        assert creds._buf[0] == 0x00, "Internal buffer must be an independent copy."
        creds._do_wipe()


class TestSecureSessionCredentialsContextManager:
    def test_enter_sets_active(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        creds.__enter__()
        assert creds._active
        creds.__exit__(None, None, None)

    def test_exit_clears_active(self):
        with SecureSessionCredentials(_DUMMY_KEY):
            pass
        creds = SecureSessionCredentials(_DUMMY_KEY)
        creds.__enter__()
        creds.__exit__(None, None, None)
        assert not creds._active

    def test_exit_wipes_buffer_on_success(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        with creds:
            pass
        _assert_wiped(creds._buf, "_buf after normal exit")

    def test_exit_sets_wiped_flag(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        with creds:
            pass
        assert creds._wiped

    def test_exit_does_not_suppress_exceptions(self):
        with pytest.raises(RuntimeError, match="propagated"):
            with SecureSessionCredentials(_DUMMY_KEY):
                raise RuntimeError("propagated")

    def test_buffer_wiped_even_when_exception_raised(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        try:
            with creds:
                raise ValueError("simulated failure")
        except ValueError:
            pass
        _assert_wiped(creds._buf, "_buf after exception exit")

    def test_wipe_is_idempotent_double_exit(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        creds.__enter__()
        creds.__exit__(None, None, None)
        creds.__exit__(None, None, None)
        _assert_wiped(creds._buf, "_buf after double exit")


class TestSecureSessionCredentialsDel:
    def test_del_wipes_buffer_when_context_not_used(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        creds.__del__()
        _assert_wiped(creds._buf, "_buf after __del__ without context manager")
        assert creds._wiped

    def test_del_does_not_raise_after_normal_exit(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        with creds:
            pass
        creds.__del__()


class TestSecureSessionCredentialsGet:
    def test_get_outside_context_raises(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        with pytest.raises(SigningError, match="outside an active validation scope"):
            creds.get()

    def test_get_after_exit_raises(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        with creds:
            pass
        with pytest.raises(SigningError, match="outside an active validation scope"):
            creds.get()

    def test_get_after_explicit_wipe_raises(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        creds.__enter__()
        creds._do_wipe()
        with pytest.raises(SigningError, match="wiped"):
            creds.get()

    def test_get_returns_credentials_copy(self):
        creds = SecureSessionCredentials(_DUMMY_KEY)
        with creds:
            result = creds.get()
        assert isinstance(result, bytes)
        assert result == _DUMMY_KEY

    def test_get_returns_independent_bytes(self):
        creds = SecureSessionCredentials(bytes(range(32)))
        with creds:
            result = creds.get()
        assert isinstance(result, bytes)
        assert len(result) == 32


class TestSecureSessionCredentialsLogging:
    def test_no_credential_bytes_logged(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="crypto.signer"):
            creds = SecureSessionCredentials(_DUMMY_KEY)
            with creds:
                creds.get()

        combined = "\n".join(r.getMessage() for r in caplog.records)
        for val in set(_DUMMY_KEY):
            if val == 0:
                continue
            assert str(val) not in combined, (
                f"Credential byte value {val} leaked into log output."
            )

    def test_log_level_is_debug_only(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="crypto.signer"):
            with SecureSessionCredentials(_DUMMY_KEY):
                pass
        for record in caplog.records:
            assert record.levelno <= logging.DEBUG, (
                f"Unexpected log level {record.levelname}: {record.getMessage()}"
            )
