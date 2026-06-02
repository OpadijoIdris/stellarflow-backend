from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from validation.liquidity import (
    DEFAULT_MINIMUM_24H_VOLUME,
    filter_record,
    filter_records,
    is_volume_sufficient,
)


def test_is_volume_sufficient_accepts_above_threshold():
    assert is_volume_sufficient(2_500_000.0, min_volume=1_000_000.0)


def test_is_volume_sufficient_rejects_below_threshold():
    assert not is_volume_sufficient(999_999, min_volume=1_000_000.0)


def test_is_volume_sufficient_rejects_invalid_values():
    assert not is_volume_sufficient(None, min_volume=1_000_000.0)
    assert not is_volume_sufficient("not-a-number", min_volume=1_000_000.0)


def test_filter_record_accepts_high_volume_record():
    record = {"price": 1.23, "volume_24h": "1000000"}
    assert filter_record(record, min_volume=1_000_000.0) == record


def test_filter_record_rejects_low_volume_record():
    record = {"price": 1.23, "volume_24h": 42}
    assert filter_record(record, min_volume=1_000_000.0) is None


def test_filter_record_rejects_missing_volume_key():
    record = {"price": 1.23}
    assert filter_record(record, min_volume=1_000_000.0) is None


def test_filter_records_keeps_only_sufficient_volume_records():
    records = [
        {"price": 1.23, "volume_24h": 2_000_000},
        {"price": 2.34, "volume_24h": 100},
        {"price": 3.45, "volume_24h": "1500000"},
    ]
    filtered = filter_records(records, min_volume=1_000_000.0)
    assert filtered == [records[0], records[2]]


def test_default_threshold_matches_constant():
    assert DEFAULT_MINIMUM_24H_VOLUME == 1_000_000.0
