from __future__ import annotations

import unittest

from ingestion.parser import build_telemetry_segments, flatten_telemetry_frames


class ParserTests(unittest.TestCase):
    def test_flatten_telemetry_frames_collapses_nested_websocket_batches(self) -> None:
        payloads = [
            {
                "data": {
                    "tickers": [
                        {
                            "symbol": "xlm-usd",
                            "last_price": "0.1025",
                            "event_time": "1710000000123",
                            "seq": "101",
                        },
                        {
                            "ticker": {
                                "asset_id": "btc-usd",
                                "price": 64000.25,
                                "timestamp": 1710000000456,
                                "flags": 3,
                            }
                        },
                    ]
                }
            },
            {
                "frames": [
                    {
                        "pair": "eth-usd",
                        "value": "3150.75",
                        "ts": 1710000000999,
                        "nonce": 7,
                        "flag_bits": "2",
                    }
                ]
            },
        ]

        self.assertEqual(
            flatten_telemetry_frames(payloads),
            (
                ("XLM-USD", 0.1025, 1710000000123, 101, 0),
                ("BTC-USD", 64000.25, 1710000000456, 0, 3),
                ("ETH-USD", 3150.75, 1710000000999, 7, 2),
            ),
        )

    def test_build_telemetry_segments_groups_frames_into_fixed_size_batches(
        self,
    ) -> None:
        payloads = [
            [
                {"asset": "xlm-usd", "price": 0.11, "timestamp": 1},
                {"asset": "btc-usd", "price": 1.22, "timestamp": 2},
                {"asset": "eth-usd", "price": 2.33, "timestamp": 3},
            ]
        ]

        self.assertEqual(
            build_telemetry_segments(payloads, segment_size=2),
            (
                (("XLM-USD", 0.11, 1, 0, 0), ("BTC-USD", 1.22, 2, 0, 0)),
                (("ETH-USD", 2.33, 3, 0, 0),),
            ),
        )

    def test_flatten_telemetry_frames_can_skip_invalid_payloads(self) -> None:
        payloads = [
            {"asset": "xlm-usd", "price": 0.11, "timestamp": 1},
            {"asset": "bad-frame", "timestamp": 2},
            {"payload": {"ticker": {"asset": "eth-usd", "price": "3.50", "time": "3"}}},
            "ignored",
        ]

        self.assertEqual(
            flatten_telemetry_frames(payloads, drop_invalid=True),
            (
                ("XLM-USD", 0.11, 1, 0, 0),
                ("ETH-USD", 3.5, 3, 0, 0),
            ),
        )

    def test_build_telemetry_segments_rejects_non_positive_segment_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "segment_size"):
            _ = build_telemetry_segments([], segment_size=0)


if __name__ == "__main__":
    _ = unittest.main()
