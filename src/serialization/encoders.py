from __future__ import annotations

"""
src/serialization/encoders.py
==============================
Binary layout encoder for StellarFlow telemetry bundles (Issue #496).

Converts high-frequency structural metrics arrays into dense binary byte
arrays using Python's native ``struct`` library, eliminating the CPU and
bandwidth overhead of JSON serialisation for local microservice communications.

Frame layout (little-endian, tightly-packed — no implicit C-struct padding):
┌─────────────┬────────┬──────────────────────────────────────────────────────┐
│ Field        │ Format │ Description                                          │
├─────────────┼────────┼──────────────────────────────────────────────────────┤
│ asset_id    │  8s    │ 8-byte ASCII asset pair (e.g. b"NGN/XLM\\x00")       │
│ price       │   q    │ int64 scaled price (fixed-point 10⁷)                 │
│ volume      │   Q    │ uint64 24-h rolling volume (scaled 10⁷)              │
│ timestamp   │   Q    │ uint64 Unix epoch milliseconds                       │
│ sequence    │   I    │ uint32 monotonic sequence / nonce counter             │
│ flags       │   H    │ uint16 status-flag bitmask                           │
│ feed_id     │   B    │ uint8  originating data-feed identifier               │
│ _reserved   │   B    │ uint8  reserved byte (always 0x00, for alignment)    │
└─────────────┴────────┴──────────────────────────────────────────────────────┘
Total frame size: 8 + 8 + 8 + 8 + 4 + 2 + 1 + 1 = 40 bytes
"""

import struct
from typing import NamedTuple, Sequence

# ---------------------------------------------------------------------------
# Format string & compile-time size
# ---------------------------------------------------------------------------
# '<'  — little-endian, no implicit padding (struct-pack semantics)
# '8s' — 8-byte fixed-length bytes field for asset identifier
# 'q'  — signed 64-bit integer: scaled price (10^7 fixed-point)
# 'Q'  — unsigned 64-bit integer: scaled volume (10^7 fixed-point)
# 'Q'  — unsigned 64-bit integer: Unix timestamp in milliseconds
# 'I'  — unsigned 32-bit integer: monotonic sequence counter
# 'H'  — unsigned 16-bit integer: status flags bitmask
# 'B'  — unsigned 8-bit integer:  data-feed identifier
# 'B'  — unsigned 8-bit integer:  reserved (padding to even word boundary)
_FRAME_FMT: str = "<8sqQQIHBB"
_FRAME_SIZE: int = struct.calcsize(_FRAME_FMT)  # 40 bytes

# ---------------------------------------------------------------------------
# Status-flag bitmask constants (uint16)
# ---------------------------------------------------------------------------
FLAG_LIVE: int = 0x0001       # feed is live / real-time
FLAG_STALE: int = 0x0002      # value has not refreshed within threshold
FLAG_ANOMALY: int = 0x0004    # anomaly-detection alert triggered
FLAG_SYNTHETIC: int = 0x0008  # value is interpolated / synthetic
FLAG_HALTED: int = 0x0010     # asset trading halted


# ---------------------------------------------------------------------------
# Typed data container
# ---------------------------------------------------------------------------
class TelemetryFrame(NamedTuple):
    """Immutable typed container for a single compacted telemetry record.

    All numeric fields use integer fixed-point representations to avoid
    floating-point non-determinism across microservice boundaries.

    Attributes:
        asset_id:  ASCII asset-pair identifier, at most 8 bytes
                   (e.g. ``b"NGN/XLM"``).  Shorter strings are zero-padded
                   during packing and right-stripped during unpacking.
        price:     Signed 64-bit scaled price (multiply by 10⁻⁷ for float).
        volume:    Unsigned 64-bit scaled 24-h rolling volume (×10⁻⁷).
        timestamp: Milliseconds since Unix epoch (uint64).
        sequence:  Monotonically incrementing frame counter (uint32).
        flags:     Status bitmask — combine FLAG_* constants (uint16).
        feed_id:   Originating data-feed identifier byte (uint8, 0–255).
    """

    asset_id: bytes   # at most 8 bytes; padded/stripped automatically
    price: int        # int64 — fixed-point scaled to 10^7
    volume: int       # uint64 — fixed-point scaled to 10^7
    timestamp: int    # uint64 — milliseconds since epoch
    sequence: int     # uint32 — monotonic counter
    flags: int        # uint16 — status bitmask
    feed_id: int      # uint8  — data-feed source identifier


# ---------------------------------------------------------------------------
# Single-frame codec
# ---------------------------------------------------------------------------
def pack_frame(frame: TelemetryFrame) -> bytes:
    """Serialise one :class:`TelemetryFrame` into a compact 40-byte buffer.

    The output is a raw binary byte-string with no delimiters, no length
    prefix, and no JSON overhead — ready for direct socket/queue transmission.

    Args:
        frame: A populated :class:`TelemetryFrame` instance.

    Returns:
        A 40-byte ``bytes`` object representing the packed frame.

    Raises:
        struct.error: If any field value is out of range for its C-type.
    """
    # Guarantee exactly 8 bytes for asset_id: truncate then zero-pad.
    asset_bytes: bytes = frame.asset_id[:8].ljust(8, b"\x00")
    return struct.pack(
        _FRAME_FMT,
        asset_bytes,
        frame.price,
        frame.volume,
        frame.timestamp,
        frame.sequence,
        frame.flags,
        frame.feed_id,
        0x00,  # reserved byte — always zero
    )


def unpack_frame(data: bytes) -> TelemetryFrame:
    """Deserialise a 40-byte buffer back into a :class:`TelemetryFrame`.

    Only the first ``FRAME_SIZE`` bytes are consumed; trailing bytes are
    silently ignored, which allows safe slicing from a larger buffer.

    Args:
        data: Raw bytes produced by :func:`pack_frame`.  Must be at least
              ``FRAME_SIZE`` (40) bytes long.

    Returns:
        A :class:`TelemetryFrame` with ``asset_id`` right-stripped of
        null-padding bytes.

    Raises:
        struct.error: If ``data`` is shorter than ``FRAME_SIZE``.
    """
    asset_id, price, volume, timestamp, sequence, flags, feed_id, _ = struct.unpack(
        _FRAME_FMT, data[:_FRAME_SIZE]
    )
    return TelemetryFrame(
        asset_id=asset_id.rstrip(b"\x00"),
        price=price,
        volume=volume,
        timestamp=timestamp,
        sequence=sequence,
        flags=flags,
        feed_id=feed_id,
    )


# ---------------------------------------------------------------------------
# Batch codec
# ---------------------------------------------------------------------------
def pack_bundle(frames: Sequence[TelemetryFrame]) -> bytes:
    """Pack a batch of telemetry frames into a single contiguous byte array.

    The bundle has no header or length prefix — it is a simple concatenation
    of fixed-size frame buffers.  Use :func:`unpack_bundle` to reverse.

    Args:
        frames: An ordered sequence of :class:`TelemetryFrame` instances.

    Returns:
        A ``bytes`` object of length ``len(frames) * FRAME_SIZE``.
    """
    return b"".join(pack_frame(f) for f in frames)


def unpack_bundle(data: bytes) -> list[TelemetryFrame]:
    """Unpack a contiguous byte array produced by :func:`pack_bundle`.

    Trailing bytes that do not constitute a complete frame are silently
    discarded.

    Args:
        data: Raw bytes produced by :func:`pack_bundle`.

    Returns:
        A list of :class:`TelemetryFrame` objects in original order.
    """
    return [
        unpack_frame(data[offset : offset + _FRAME_SIZE])
        for offset in range(0, len(data), _FRAME_SIZE)
        if len(data) - offset >= _FRAME_SIZE
    ]


def bundle_frame_count(data: bytes) -> int:
    """Return the number of complete frames present in a raw bundle buffer.

    Args:
        data: Raw bundle bytes.

    Returns:
        Integer count of decodable frames.
    """
    return len(data) // _FRAME_SIZE


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def encode_asset_id(symbol: str) -> bytes:
    """Encode a human-readable asset-pair symbol into a fixed 8-byte field.

    Args:
        symbol: ASCII string such as ``"NGN/XLM"`` (max 8 chars).

    Returns:
        An 8-byte ``bytes`` object — truncated and zero-padded as needed.
    """
    return symbol.encode("ascii", errors="replace")[:8].ljust(8, b"\x00")


def decode_asset_id(asset_bytes: bytes) -> str:
    """Decode an 8-byte asset-id field back into a readable string.

    Args:
        asset_bytes: Raw bytes from a :class:`TelemetryFrame`.

    Returns:
        ASCII string with null bytes stripped.
    """
    return asset_bytes.rstrip(b"\x00").decode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__ = [
    # Types
    "TelemetryFrame",
    # Flag constants
    "FLAG_LIVE",
    "FLAG_STALE",
    "FLAG_ANOMALY",
    "FLAG_SYNTHETIC",
    "FLAG_HALTED",
    # Single-frame codec
    "pack_frame",
    "unpack_frame",
    # Batch codec
    "pack_bundle",
    "unpack_bundle",
    "bundle_frame_count",
    # Helpers
    "encode_asset_id",
    "decode_asset_id",
    # Size constant
    "FRAME_SIZE",
]

#: Exported constant — size in bytes of one packed telemetry frame.
FRAME_SIZE: int = _FRAME_SIZE
