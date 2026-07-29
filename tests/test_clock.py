"""시각 헬퍼 테스트 — timestamptz에 naive/로컬시간이 새지 않는지."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from minjob_agent.clock import (
    ensure_utc,
    parse_iso,
    parse_iso_date,
    require_plain_date,
    to_iso,
    to_iso_date,
    utc_now,
)


def test_utc_now_is_timezone_aware_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_ensure_utc_rejects_naive_datetime() -> None:
    # naive를 통과시키면 Supabase가 서버 로컬시간으로 해석해 조용히 어긋난다.
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 7, 29, 12, 0))  # noqa: DTZ001


def test_ensure_utc_converts_other_offsets() -> None:
    seoul = datetime(2026, 7, 29, 21, 0, tzinfo=timezone(timedelta(hours=9)))
    assert ensure_utc(seoul) == datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def test_to_iso_uses_z_suffix() -> None:
    value = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    assert to_iso(value) == "2026-07-29T12:00:00Z"


def test_to_iso_normalizes_offset_to_utc() -> None:
    seoul = datetime(2026, 7, 29, 21, 0, tzinfo=timezone(timedelta(hours=9)))
    assert to_iso(seoul) == "2026-07-29T12:00:00Z"


def test_iso_roundtrip_preserves_instant() -> None:
    original = utc_now()
    assert parse_iso(to_iso(original)) == original


def test_parse_iso_accepts_offset_form() -> None:
    assert parse_iso("2026-07-29T12:00:00+00:00") == datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def test_parse_iso_converts_non_utc_offset() -> None:
    assert parse_iso("2026-07-29T21:00:00+09:00") == datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def test_parse_iso_rejects_naive_text() -> None:
    with pytest.raises(ValueError, match="naive"):
        parse_iso("2026-07-29T12:00:00")


def test_parse_iso_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="ISO8601"):
        parse_iso("어제")


# ── date 컬럼(posted_at·deadline) ─────────────────────────────────


def test_to_iso_date_formats_yyyy_mm_dd() -> None:
    assert to_iso_date(date(2026, 7, 29)) == "2026-07-29"


def test_to_iso_date_rejects_datetime() -> None:
    # datetime은 date의 서브클래스라 통과해버린다 → date 컬럼에 시각이 섞인다.
    with pytest.raises(ValueError, match="datetime"):
        to_iso_date(datetime(2026, 7, 29, 12, 0, tzinfo=UTC))


def test_parse_iso_date_roundtrip() -> None:
    original = date(2026, 7, 22)
    assert parse_iso_date(to_iso_date(original)) == original


def test_parse_iso_date_rejects_datetime_text() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_iso_date("2026-07-29T12:00:00Z")


def test_require_plain_date_returns_value() -> None:
    value = date(2026, 7, 29)
    assert require_plain_date(value) is value


# ── 정밀도·경계 ──────────────────────────────────────────────────


def test_to_iso_preserves_microseconds() -> None:
    value = datetime(2026, 7, 29, 12, 0, 0, 123456, tzinfo=UTC)
    assert to_iso(value) == "2026-07-29T12:00:00.123456Z"


def test_parse_iso_accepts_fractional_z() -> None:
    assert parse_iso("2026-07-29T12:00:00.123456Z") == datetime(
        2026, 7, 29, 12, 0, 0, 123456, tzinfo=UTC
    )


def test_to_iso_rejects_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_iso(datetime(2026, 7, 29, 12, 0))  # noqa: DTZ001


def test_parse_iso_rejects_date_only() -> None:
    with pytest.raises(ValueError, match="naive"):
        parse_iso("2026-07-29")


def test_parse_iso_rejects_empty() -> None:
    with pytest.raises(ValueError, match="ISO8601"):
        parse_iso("")
