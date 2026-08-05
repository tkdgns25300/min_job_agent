"""시각 헬퍼 테스트 — timestamptz에 naive/로컬시간이 새지 않는지."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from minjob_ingest.clock import (
    ensure_kst,
    kst_now,
    parse_iso,
    parse_iso_date,
    require_plain_date,
    to_iso,
    to_iso_date,
)


def test_now_is_timezone_aware_kst() -> None:
    """저장값은 KST다(운영자 결정 2026-08-05). **오프셋이 반드시 있어야 한다** — naive KST는
    DB가 서버 시간대로 해석해 9시간 어긋난다."""
    now = kst_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=9)


def test_ensure_utc_rejects_naive_datetime() -> None:
    # naive를 통과시키면 Supabase가 서버 로컬시간으로 해석해 조용히 어긋난다.
    with pytest.raises(ValueError, match="naive"):
        ensure_kst(datetime(2026, 7, 29, 12, 0))  # noqa: DTZ001


def test_ensure_utc_converts_other_offsets() -> None:
    seoul = datetime(2026, 7, 29, 21, 0, tzinfo=timezone(timedelta(hours=9)))
    assert ensure_kst(seoul) == datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def test_to_iso_uses_the_kst_offset() -> None:
    """`Z`가 아니라 `+09:00`으로 적는다 — 사람이 파일을 열었을 때 한국 시간으로 읽힌다."""
    value = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    assert to_iso(value) == "2026-07-29T21:00:00+09:00"


def test_to_iso_normalizes_any_offset_to_kst() -> None:
    """다른 오프셋으로 들어와도 같은 순간의 KST 표기로 통일한다(포맷 드리프트 방지)."""
    newyork = datetime(2026, 7, 29, 8, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert to_iso(newyork) == "2026-07-29T21:00:00+09:00"


def test_an_old_utc_value_reads_back_as_the_same_instant() -> None:
    """⚠️ 이관 전에 저장한 `...Z` 값이 그대로 읽혀야 한다 — 같은 순간이므로 KST로 정규화된다."""
    assert parse_iso("2026-08-05T09:33:08.632854Z") == parse_iso("2026-08-05T18:33:08.632854+09:00")


def test_iso_roundtrip_preserves_instant() -> None:
    original = kst_now()
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
    assert to_iso(value) == "2026-07-29T21:00:00.123456+09:00"


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
