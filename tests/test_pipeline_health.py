"""게시판 상태 기록·경보 판정 테스트(SPEC §7).

핵심은 **경보가 잡음이 되지 않는 것**이다. 조용한 게시판이 매일 울리면 아무도 안 보고,
정작 깨진 게시판이 그 속에 묻힌다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

from minjob_ingest.domain import SourceHealthStatus
from minjob_ingest.models import SourceHealth
from minjob_ingest.pipeline.collect import CollectReport
from minjob_ingest.pipeline.health import (
    EMPTY_RUNS_ALARM,
    FAILURES_ALARM,
    QUIET_DAYS_NOTICE,
    AlertKind,
    alerts_for,
    days_since_last_posting,
    record_failure,
    record_success,
    status_for,
)
from minjob_ingest.store.json_store import JsonStore

_NOW: Final = datetime(2026, 8, 4, 3, 0, tzinfo=UTC)
_TODAY: Final = _NOW.date()
_RUN: Final = UUID("11111111-2222-3333-4444-555555555555")


def _report(*, rows: int, saved: int = 0, newest: date | None = _TODAY) -> CollectReport:
    return CollectReport(
        source_key="YTUS",
        pages_read=1,
        rows=rows,
        fresh=saved,
        seen=rows - saved,
        stale=0,
        saved=saved,
        shifted=0,
        oldest=newest,
        newest=newest,
        samples=(),
        cutoff=date(2026, 5, 4),
    )


def _kinds(health: SourceHealth, *, today: date = _TODAY) -> set[AlertKind]:
    return {alert.kind for alert in alerts_for(health, today=today)}


# ── 상태 판정 ────────────────────────────────────────────────────


def test_rows_without_new_postings_is_still_ok() -> None:
    """⚠️ **신규 0건은 정상이다** — 원장이 이미 본 글을 걸러낸 결과다.

    이걸 EMPTY로 판정하면 조용한 게시판이 매일 소프트 실패로 기록된다.
    """
    assert status_for(_report(rows=18, saved=0)) is SourceHealthStatus.OK


def test_zero_rows_is_empty() -> None:
    """목록 자체를 못 읽은 것 — 셀렉터 깨짐·로그인벽 전환 신호다."""
    assert status_for(_report(rows=0, newest=None)) is SourceHealthStatus.EMPTY


# ── 기록 ─────────────────────────────────────────────────────────


def test_success_is_recorded_with_the_observed_window(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "data")
    health = record_success(store, _report(rows=258, saved=227), run_id=_RUN, at=_NOW)
    assert store.get_health("YTUS") == health
    assert health.last_cutoff == date(2026, 5, 4)  # 기간 없이는 행 수를 해석할 수 없다
    assert health.last_rows == 258
    assert health.last_new_count == 227
    assert health.last_run_id == _RUN


def test_failure_is_recorded_so_the_streak_can_be_counted(tmp_path: Path) -> None:
    """실패를 안 남기면 연속 실패를 셀 수 없어 §7 경보가 죽는다."""
    store = JsonStore(tmp_path / "data")
    record_success(store, _report(rows=18, saved=2), run_id=_RUN, at=_NOW)
    health = record_failure(
        store, "YTUS", run_id=_RUN, at=_NOW + timedelta(days=1), error="HTTP 500"
    )
    assert health.consecutive_failures == 1
    assert health.last_rows == 18  # 실패는 관측을 덮지 않는다
    assert store.get_health("YTUS") == health


def test_counters_accumulate_across_runs(tmp_path: Path) -> None:
    """직전 값을 store에서 읽지 않으면 매 실행 초기화돼 경보가 영구히 안 울린다."""
    store = JsonStore(tmp_path / "data")
    for day in range(EMPTY_RUNS_ALARM):
        record_success(
            store, _report(rows=0, newest=None), run_id=_RUN, at=_NOW + timedelta(days=day)
        )
    health = store.get_health("YTUS")
    assert health is not None
    assert health.consecutive_empty_runs == EMPTY_RUNS_ALARM


# ── 경보 판정 ────────────────────────────────────────────────────


def _advance(status: SourceHealthStatus, times: int, **extra: object) -> SourceHealth:
    health: SourceHealth | None = None
    for day in range(times):
        health = SourceHealth.advance(
            previous=health,
            source_key="YTUS",
            run_at=_NOW + timedelta(days=day),
            status=status,
            **extra,  # type: ignore[arg-type]
        )
    assert health is not None
    return health


def test_a_quiet_board_raises_nothing(tmp_path: Path) -> None:
    """목록은 읽히고 최신 글도 최근이면 신규가 0이어도 아무 알림이 없다."""
    store = JsonStore(tmp_path / "data")
    for day in range(5):
        health = record_success(
            store, _report(rows=18, saved=0), run_id=_RUN, at=_NOW + timedelta(days=day)
        )
    assert _kinds(health) == set()


def test_consecutive_empty_listings_warn() -> None:
    below = _advance(SourceHealthStatus.EMPTY, EMPTY_RUNS_ALARM - 1)
    assert AlertKind.LISTING_EMPTY not in _kinds(below)  # 임계값 아래는 조용하다
    at_threshold = _advance(SourceHealthStatus.EMPTY, EMPTY_RUNS_ALARM)
    assert AlertKind.LISTING_EMPTY in _kinds(at_threshold)
    assert AlertKind.LISTING_EMPTY.is_warning


def test_consecutive_failures_warn() -> None:
    below = _advance(SourceHealthStatus.FAIL, FAILURES_ALARM - 1, error="HTTP 500")
    assert AlertKind.FETCH_FAILING not in _kinds(below)
    at_threshold = _advance(SourceHealthStatus.FAIL, FAILURES_ALARM, error="HTTP 500")
    assert AlertKind.FETCH_FAILING in _kinds(at_threshold)


def test_an_old_latest_posting_is_information_not_a_warning() -> None:
    """방학처럼 실제로 조용한 시기가 있다 — 여기서 경보를 울리면 다시 잡음이 된다."""
    health = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=_NOW,
        status=SourceHealthStatus.OK,
        rows=18,
        posted_on=_TODAY - timedelta(days=QUIET_DAYS_NOTICE),
    )
    assert AlertKind.NO_RECENT_POSTINGS in _kinds(health)
    assert not AlertKind.NO_RECENT_POSTINGS.is_warning


def test_no_postings_inside_the_window_is_reported() -> None:
    """목록은 읽혔는데 컷오프 안에 글이 하나도 없던 경우 — 기간을 알아야 표현되는 상태다."""
    health = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=_NOW,
        status=SourceHealthStatus.OK,
        cutoff=date(2026, 5, 4),
        rows=258,  # 전부 범위 밖이었다
        posted_on=None,
    )
    assert AlertKind.NO_RECENT_POSTINGS in _kinds(health)
    assert days_since_last_posting(health, today=_TODAY) is None


def test_an_unreadable_listing_is_not_reported_as_quiet() -> None:
    """목록을 못 읽은 것은 "조용하다"가 아니다 — 두 사유가 겹치면 원인을 오해한다."""
    empty = _advance(SourceHealthStatus.EMPTY, EMPTY_RUNS_ALARM)
    assert AlertKind.NO_RECENT_POSTINGS not in _kinds(empty)


def test_warnings_come_before_information() -> None:
    """31곳을 훑은 요약에서 참고 정보가 경보를 밀어내면 안 된다."""
    health = SourceHealth.advance(
        previous=_advance(SourceHealthStatus.FAIL, FAILURES_ALARM, error="HTTP 500"),
        source_key="YTUS",
        run_at=_NOW + timedelta(days=9),
        status=SourceHealthStatus.OK,
        rows=18,
        posted_on=_TODAY - timedelta(days=QUIET_DAYS_NOTICE),
    )
    kinds = [alert.kind for alert in alerts_for(health, today=_TODAY)]
    assert kinds and all(kind.is_warning for kind in kinds[: len(kinds) - 1])
    assert kinds[-1] is AlertKind.NO_RECENT_POSTINGS
