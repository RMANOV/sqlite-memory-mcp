import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import parse_iso_datetime_for_compare


def test_parse_iso_datetime_for_compare_accepts_z_suffix():
    parsed = parse_iso_datetime_for_compare("2026-03-24T10:00:00Z")
    assert parsed == datetime(2026, 3, 24, 10, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_datetime_for_compare_normalizes_offsets():
    left = parse_iso_datetime_for_compare("2026-03-24T12:00:00+02:00")
    right = parse_iso_datetime_for_compare("2026-03-24T10:00:00Z")
    assert left == right


def test_parse_iso_datetime_for_compare_treats_invalid_as_minimum():
    invalid = parse_iso_datetime_for_compare("not-a-timestamp")
    valid = parse_iso_datetime_for_compare("2026-03-24T10:00:00")
    assert invalid < valid
