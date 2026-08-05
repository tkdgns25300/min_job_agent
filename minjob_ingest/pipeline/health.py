"""게시판 상태 기록·경보 판정(SPEC §7).

`collect`가 게시판 하나를 끝낼 때마다 결과를 `source_health` **한 행**에 접어 넣는다. 실행 기록
(`crawl_run`)은 전체 합계만 담아서 **어느 게시판이 조용해졌는지 알 수 없다** — 그 구분이 여기 있다.

⚠️ **판정 기준은 "목록 행 0"이지 "신규 0건"이 아니다.** 원장 증분이라 조용한 게시판은 신규가
며칠씩 0이고 그게 정상이다. 그걸 경보로 세면 31곳 중 절반이 매일 울려 **경보가 잡음이 되고 정작
깨진 게시판이 묻힌다**(`SourceHealthStatus` 참조).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from minjob_ingest.domain import SourceHealthStatus
from minjob_ingest.models import SourceHealth
from minjob_ingest.pipeline.collect import CollectReport
from minjob_ingest.store.base import Store

#: 목록 행 0이 이만큼 연속되면 경보. 1회로 울리지 않는 이유는 게시판이 첫 페이지를 잠깐 비우는
#: 일이 있어서다 — 다만 셀렉터 깨짐은 회복되지 않으므로 2회면 충분히 확정적이다.
EMPTY_RUNS_ALARM: Final = 2
#: 연속 실패 경보 임계값(1회는 일시적 네트워크 장애일 수 있다).
FAILURES_ALARM: Final = 2
#: 최신 글이 이만큼 오래됐으면 알린다. **경보가 아니라 정보**다 — 방학처럼 실제로 조용한 시기가
#: 있고, 여기서 경보를 울리면 다시 잡음이 된다.
QUIET_DAYS_NOTICE: Final = 60


class AlertKind(StrEnum):
    """운영자에게 알릴 사유. **문장은 여기서 만들지 않는다**(출력은 CLI 몫)."""

    #: 응답은 오는데 목록 행이 계속 0 — 셀렉터 깨짐·로그인벽 전환.
    LISTING_EMPTY = "LISTING_EMPTY"
    #: 연속 실패 — 접속 불가·차단.
    FETCH_FAILING = "FETCH_FAILING"
    #: 목록은 읽히는데 최근 글이 없다 — 게시판이 조용한 것일 수 있다(정보).
    NO_RECENT_POSTINGS = "NO_RECENT_POSTINGS"

    @property
    def is_warning(self) -> bool:
        """사람이 손을 써야 하는가. `False`면 참고 정보다."""
        return self is not AlertKind.NO_RECENT_POSTINGS


@dataclass(frozen=True, slots=True)
class Alert:
    """게시판 하나에 대한 알림. 수치는 `health`에서 읽는다(중복 보관하지 않는다)."""

    kind: AlertKind
    health: SourceHealth

    @property
    def source_key(self) -> str:
        return self.health.source_key


def status_for(report: CollectReport) -> SourceHealthStatus:
    """목록을 읽었나로 판정한다.

    ⚠️ `report.saved`(신규 건수)를 보지 않는다 — 신규 0건은 정상이다.
    """
    return SourceHealthStatus.OK if report.rows > 0 else SourceHealthStatus.EMPTY


def record_success(
    store: Store, report: CollectReport, *, run_id: UUID | None, at: datetime
) -> SourceHealth:
    """수집 결과를 상태 한 행에 접어 넣는다. 직전 값은 store에서 읽어 누적을 잇는다."""
    health = SourceHealth.advance(
        previous=store.get_health(report.source_key),
        source_key=report.source_key,
        run_at=at,
        status=status_for(report),
        run_id=run_id,
        cutoff=report.cutoff,
        rows=report.rows,
        new_count=report.saved,
        posted_on=report.newest,
    )
    store.upsert_health(health)
    return health


def record_failure(
    store: Store, source_key: str, *, run_id: UUID | None, at: datetime, error: str
) -> SourceHealth:
    """실패도 반드시 남긴다 — 안 남기면 연속 실패를 셀 수 없어 §7 경보가 죽는다.

    관측값(기간·행 수·최신 게시일)은 넘기지 않는다. `advance`가 직전 관측을 보존한다.
    """
    health = SourceHealth.advance(
        previous=store.get_health(source_key),
        source_key=source_key,
        run_at=at,
        status=SourceHealthStatus.FAIL,
        run_id=run_id,
        error=error,
    )
    store.upsert_health(health)
    return health


def alerts_for(health: SourceHealth, *, today: date) -> tuple[Alert, ...]:
    """이 상태가 알릴 만한가. 경보를 먼저, 참고 정보를 뒤에 둔다."""
    kinds: list[AlertKind] = []
    if health.consecutive_failures >= FAILURES_ALARM:
        kinds.append(AlertKind.FETCH_FAILING)
    if health.consecutive_empty_runs >= EMPTY_RUNS_ALARM:
        kinds.append(AlertKind.LISTING_EMPTY)
    if _has_no_recent_postings(health, today=today):
        kinds.append(AlertKind.NO_RECENT_POSTINGS)
    return tuple(Alert(kind=kind, health=health) for kind in kinds)


def days_since_last_posting(health: SourceHealth, *, today: date) -> int | None:
    """최신 글이 며칠 전인가(모르면 `None`). 출력에 쓰라고 여기서 계산한다."""
    if health.last_posted_on is None:
        return None
    return (today - health.last_posted_on).days


def _has_no_recent_postings(health: SourceHealth, *, today: date) -> bool:
    """목록은 읽혔는데 최근 글이 없나.

    두 모습이 있다: 훑은 기간 안에 아무 글도 없었거나(`last_posted_on`이 없음), 최신 글이
    한참 전이거나. **목록을 못 읽은 경우는 여기가 아니라 `LISTING_EMPTY`다.**
    """
    if health.last_rows == 0:
        return False
    elapsed = days_since_last_posting(health, today=today)
    if elapsed is None:
        # 게시일을 모른다. ⚠️ 이유가 둘이고 뜻이 반대다:
        # ① 컷오프를 적용했는데 그 안에 글이 없었다 → 조용한 게시판(경보 맞다)
        # ② 게시판이 **애초에 게시일을 주지 않는다**(config `list_has_dates: false`) → 판정
        #    불가. `PCKWORLD`가 그렇고, 구분하지 않으면 60건을 잘 받아도 **매 실행 오경보**가
        #    뜬다(실측 2026-08-05). 상시 뜨는 경보는 아무도 안 보게 된다.
        return health.last_cutoff is not None
    return elapsed >= QUIET_DAYS_NOTICE
