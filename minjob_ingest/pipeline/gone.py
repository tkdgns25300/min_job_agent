"""원문 소멸 감지(SPEC §4 gone 단계) — 목록에 없는 공고를 **두 번** 확인해 내린다.

게시판 목록이 정답지다: 창(2개월) 안 원장에 있는데 목록에 없으면 **후보**다. 그런데
목록 대조만 믿으면 오판한다(실측 2026-08-30: 후보 39건 중 4건이 살아 있었다 — 부산장신·
침신대는 오래된 글을 목록에서만 빼고 URL은 살려 둔다). 그래서 후보는 상세를 **직접 열어**
한 번 더 확인하고, 그때도 두 함정을 막는다:

- **게시판 개편·장애**: 셀렉터가 사라지면 살아있는 글도 전부 실패로 보인다 → **대조군**
  (방금 목록에서 본, 정의상 살아있는 글)을 먼저 찔러 그것이 실패하면 오늘은 판정하지 않는다.
- **덜 훑고 "없다"**: 끌어올림 게시판은 날짜 역순이 아니라 이른 중단이 대량 오판을 만든다
  (실측: 고신대 63건 오판) → 원장 건수로 **최소 페이지**를 계산해 그 전엔 멈추지 않고,
  그래도 후보가 비정상적으로 많으면 게시판째 보류한다.

⚠️ 여기는 **판정까지**다. `source_gone_at` 기록과 `jobs` 내리기는 저장 층이 하고(SPEC §8),
그 갱신은 `church_id IS NULL` 조건으로 DB가 지킨다 — 교회가 claim한 공고는 건드리지 않는다.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from minjob_ingest.clock import kst_now, months_before
from minjob_ingest.fetch.client import FetchError, SourceClient
from minjob_ingest.sources.adapters.base import ParseError, PostingRef, RawPosting
from minjob_ingest.sources.adapters.registry import (
    Adapter,
    GoneProber,
    find_adapter,
    needs_detail_request,
)
from minjob_ingest.sources.registry import SourceConfig
from minjob_ingest.store.base import GoneTarget, PublishTarget, Store

#: 창 = 이 달수 안의 원장만 판정한다. 목록을 이만큼 거슬러 훑을 수 있어야 성립하는 값이다 —
#: 창 밖 공고는 min_job의 노출 기한(게시 90일)이 곧 정리하므로 여기서 쫓지 않는다.
GONE_WINDOW_MONTHS: Final = 2

#: 목록 페이지 안전 상한. collect와 같은 성격(폭주 방지)이고 범위는 컷오프가 정한다.
MAX_LIST_PAGES: Final = 100

#: 후보가 대상의 이 비율을 넘으면 게시판째 보류한다 — 접속 장애·개편을 삭제로 읽지 않는다.
#: 실측 삭제율은 게시판당 2~13%였다(2026-08-30 · 창 안 2,180건 중 93건).
BULK_RATIO: Final = 0.30

#: 후보가 이만큼 이하면 비율 방어를 걸지 않는다 — 대상 몇 건뿐인 게시판에서 진짜 삭제
#: 한 건이 30%를 넘겨 영영 보류되는 것을 막는다(2건 중 1건 = 50%).
BULK_FREE: Final = 3

#: 대조군 크기. 방금 목록에서 본 글이라 정의상 살아 있다 — 둘 다 실패하면 게시판 문제다.
CONTROL_SAMPLE: Final = 2


class Verdict(StrEnum):
    """상세를 열어 본 결과. UNKNOWN은 아무것도 하지 않는다 — 모르는 것은 내리지 않는다."""

    GONE = "GONE"
    ALIVE = "ALIVE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SweepReport:
    """게시판 하나의 판정 결과. `skipped`가 있으면 그날은 아무것도 내리지 않은 것이다."""

    source_key: str
    targets: int
    pages: int = 0
    listed: int = 0
    #: 판정하지 않은 이유(사람이 읽는 문장). 값이 있으면 아래 세 묶음은 전부 비어 있다.
    skipped: str | None = None
    gone: tuple[GoneTarget, ...] = ()
    alive: tuple[GoneTarget, ...] = ()
    unknown: tuple[GoneTarget, ...] = ()


def window_start(today: date) -> date:
    """판정 창의 시작일. collect의 컷오프와 같은 셈법(`clock.months_before`)을 쓴다."""
    return months_before(today, GONE_WINDOW_MONTHS)


def min_pages(targets: int, page_size: int) -> int:
    """이 페이지 수를 읽기 전엔 컷오프 중단을 믿지 않는다.

    끌어올림 게시판은 날짜 역순이 아니라서 "한 페이지가 전부 컷오프 밖"이 일찍 올 수 있다 —
    원장에 있는 건수만큼은 목록 어딘가에 있어야 하므로, 그만큼 읽기 전의 중단은 덜 훑은 것이다
    (실측 2026-08-30: 이 검산이 없을 때 고신대 77건 중 63건을 삭제로 오판했다).
    """
    if page_size <= 0:
        return 1
    return max(1, math.ceil(targets / page_size))


def missing_from_listing(
    targets: Sequence[GoneTarget], listed_ids: frozenset[str]
) -> tuple[GoneTarget, ...]:
    """목록에 안 보인 대상 = 후보. **아직 삭제가 아니다** — 상세 확인이 남아 있다."""
    return tuple(target for target in targets if target.external_id not in listed_ids)


def is_bulk_suspicious(candidates: int, targets: int) -> bool:
    """후보가 이렇게 많으면 삭제가 아니라 게시판 쪽 문제다 — 오늘은 판정하지 않는다."""
    if candidates <= BULK_FREE:
        return False
    return candidates > targets * BULK_RATIO


def verdict_of_detail(raw: RawPosting) -> Verdict:
    """상세를 파싱한 결과 → 판정.

    ⚠️ `본문 0자`만으로 GONE이 아니다 — 포스터 게시판(칼빈대·기독공보)은 살아있는 글도
    본문이 비어 있다(실측 2026-08-30). 본문·그림·첨부가 **전부** 없어야 삭제다.
    """
    if raw.raw_text or raw.image_urls or raw.attachments:
        return Verdict.ALIVE
    return Verdict.GONE


def sweep_source(
    source: SourceConfig,
    adapter: Adapter,
    client: SourceClient,
    targets: Sequence[GoneTarget],
    *,
    today: date,
) -> SweepReport:
    """게시판 하나를 판정한다. 목록 실패는 그대로 던진다 — 소스 격리는 호출자가 한다(SPEC §3).

    삭제는 사라지지 않으므로 오늘 못 하면 내일 잡는다 — collect처럼 gap을 메울 필요가 없고,
    그래서 실패의 값이 싸다: 던지고 건너뛰는 것이 안전한 기본값이다.
    """
    if not targets:
        return SweepReport(source_key=source.key, targets=0, skipped="판정 대상 없음")
    prober = GoneProber.of(adapter)
    if prober is None and not needs_detail_request(adapter):
        return SweepReport(
            source_key=source.key,
            targets=len(targets),
            skipped="상세를 따로 받지 않는 게시판 — 2차 확인이 불가능해 판정하지 않는다",
        )

    listing = _read_listing(source, adapter, client, targets=len(targets), today=today)
    candidates = missing_from_listing(targets, listing.ids)
    report = SweepReport(
        source_key=source.key, targets=len(targets), pages=listing.pages, listed=len(listing.ids)
    )
    if not candidates:
        return report
    if is_bulk_suspicious(len(candidates), len(targets)):
        return _skipped(
            report,
            f"후보 {len(candidates)}건이 대상 {len(targets)}건의 {BULK_RATIO:.0%}를 넘는다"
            " — 게시판 장애·개편 의심, 오늘은 판정하지 않는다",
        )
    control = _control_failure(source, adapter, client, prober, listing.fresh_refs)
    if control is not None:
        return _skipped(report, control)

    gone: list[GoneTarget] = []
    alive: list[GoneTarget] = []
    unknown: list[GoneTarget] = []
    buckets = {Verdict.GONE: gone, Verdict.ALIVE: alive, Verdict.UNKNOWN: unknown}
    for target in candidates:
        verdict = _probe(source, adapter, client, prober, _ref_of(target), target.external_id)
        buckets[verdict].append(target)
    return SweepReport(
        source_key=report.source_key,
        targets=report.targets,
        pages=report.pages,
        listed=report.listed,
        gone=tuple(gone),
        alive=tuple(alive),
        unknown=tuple(unknown),
    )


@dataclass(frozen=True, slots=True)
class _Listing:
    """목록을 창까지 훑은 결과 — 보인 번호 전부 + 대조군으로 쓸 첫 페이지 행들."""

    ids: frozenset[str]
    fresh_refs: tuple[PostingRef, ...]
    pages: int


def _read_listing(
    source: SourceConfig,
    adapter: Adapter,
    client: SourceClient,
    *,
    targets: int,
    today: date,
) -> _Listing:
    """목록을 창(2개월)까지 훑어 **지금 보이는 번호 전부**를 모은다.

    중단 조건은 collect의 `has_more_pages`와 같은 뜻이다(한 페이지가 전부 컷오프 밖이면 그
    아래는 더 오래된 글뿐) — 다만 끌어올림 게시판을 위해 `min_pages` 검산을 더한다.
    한 페이지라도 실패하면 예외가 그대로 올라간다 — 부분 목록으로 "없다"를 판정하지 않는다.
    """
    cutoff = window_start(today)
    cap = min(MAX_LIST_PAGES, source.list_page_limit or MAX_LIST_PAGES)
    seen: set[str] = set()
    fresh: tuple[PostingRef, ...] = ()
    floor = 1
    pages = 0
    for page in range(1, cap + 1):
        refs = adapter.parse_list(_fetch_list(adapter, client, source, page), source)
        pages = page
        if not refs:
            break
        before = len(seen)
        seen.update(ref.external_id for ref in refs)
        if len(seen) == before:
            # 범위 밖 페이지에 마지막 페이지를 되돌려주는 게시판 — 새 번호가 없으면 끝이다.
            break
        if page == 1:
            fresh = tuple(refs)
            floor = max(floor, min_pages(targets, len(refs)))
        inside = sum(1 for ref in refs if ref.posted_on is None or ref.posted_on >= cutoff)
        if inside == 0 and page >= floor:
            break
    return _Listing(ids=frozenset(seen), fresh_refs=fresh, pages=pages)


def _fetch_list(adapter: Adapter, client: SourceClient, source: SourceConfig, page: int) -> str:
    request = adapter.list_request(source, page)
    if request.form is None:
        return client.get(request.url).text
    return client.post_form(request.url, request.form).text


def _control_failure(
    source: SourceConfig,
    adapter: Adapter,
    client: SourceClient,
    prober: GoneProber | None,
    fresh_refs: Sequence[PostingRef],
) -> str | None:
    """대조군이 살아있지 않으면 그 이유를 돌려준다 — 오늘 이 게시판은 판정하지 않는다.

    대조군은 방금 목록에서 본 글이라 **정의상 살아 있다**. 그것이 GONE으로 나온다면 삭제가
    아니라 게시판이 바뀐 것이고(셀렉터 소멸·세션 정책 변경), 후보의 GONE도 믿을 수 없다.
    """
    controls = fresh_refs[:CONTROL_SAMPLE]
    if not controls:
        return "대조군이 없다(목록이 비었다) — 판정하지 않는다"
    for ref in controls:
        verdict = _probe(source, adapter, client, prober, ref, ref.external_id)
        if verdict is not Verdict.ALIVE:
            return (
                f"대조군 {ref.external_id}(목록에 보이는 글)이 {verdict.value}"
                " — 게시판 개편·장애 의심, 오늘은 판정하지 않는다"
            )
    return None


def _probe(
    source: SourceConfig,
    adapter: Adapter,
    client: SourceClient,
    prober: GoneProber | None,
    ref: PostingRef,
    external_id: str,
) -> Verdict:
    """글 하나의 상세를 열어 본다. 전용 신호(prober)가 있으면 그쪽이 정확하다.

    ⚠️ FetchError·ParseError를 GONE으로 읽는 것은 **대조군이 살아 있을 때만** 안전하다 —
    호출자(`sweep_source`)가 그 순서를 지킨다.
    """
    if prober is not None:
        request = prober.request(source, external_id)
        if request.form is None:
            body = client.get(request.url).text
        else:
            body = client.post_form(request.url, request.form).text
        outcome = prober.parse(body)
        if outcome is None:
            return Verdict.UNKNOWN
        return Verdict.GONE if outcome else Verdict.ALIVE
    try:
        html = client.get(ref.url).text
    except FetchError:
        return Verdict.GONE
    try:
        raw = adapter.parse_detail(html, ref)
    except ParseError:
        return Verdict.GONE
    return verdict_of_detail(raw)


def _ref_of(target: GoneTarget) -> PostingRef:
    return PostingRef(
        external_id=target.external_id,
        url=target.source_url,
        title=target.title,
        posted_on=target.posted_on,
        list_meta={},
    )


def _skipped(report: SweepReport, reason: str) -> SweepReport:
    return SweepReport(
        source_key=report.source_key,
        targets=report.targets,
        pages=report.pages,
        listed=report.listed,
        skipped=reason,
    )


# ── 실행 조립 (publish_all과 같은 자리) ──────────────────────────


@dataclass(frozen=True, slots=True)
class GoneRunReport:
    """하루치 소멸 감지의 결과 — CLI가 그대로 그린다."""

    reports: tuple[SweepReport, ...]
    #: 게시판째 실패한 곳(목록·확인 요청이 죽었다). 삭제는 사라지지 않으니 내일 또 잡는다.
    failures: Mapping[str, str]
    #: `source_gone_at`을 새로 기록한 행 수.
    marked: int
    #: 내린 공고 수(`jobs.status=CLOSED`). 교회가 claim했거나 이미 닫힌 행은 세지 않는다.
    closed: int
    #: 마감이 지나 정리한 공고 수 — 소멸과 별개의 이유지만 같은 경로로 내린다.
    expired_closed: int

    @property
    def gone_total(self) -> int:
        return sum(len(report.gone) for report in self.reports)


def run_gone(
    store: Store,
    jobs: PublishTarget | None,
    sources: Sequence[SourceConfig],
    *,
    today: date,
    dry_run: bool = False,
    open_client: Callable[[SourceConfig], AbstractContextManager[SourceClient]] = SourceClient,
    adapter_of: Callable[[str], Adapter] = find_adapter,
) -> GoneRunReport:
    """모든 게시판의 소멸을 판정하고 확정분을 내린다. **에러는 소스 단위로 격리한다**(SPEC §3).

    `dry_run`이면 게시판 확인(요청)까지 하고 **저장·내리기만** 건너뛴다 — 유료 호출이 없어
    구조화의 `--dry-run`과 달리 몇 번을 돌려도 비용이 없고, "오늘 무엇을 내릴 것인가"를
    보는 것이 목적이다(운영 첫 며칠의 관례).

    ⚠️ `jobs`가 없으면(JSON 저장소) 관측 기록까지만 한다 — 내릴 공개 테이블이 없다.
    """
    by_key = _targets_by_source(store.gone_targets(since=window_start(today)))
    reports: list[SweepReport] = []
    failures: dict[str, str] = {}
    marked = 0
    closed = 0
    for source in sources:
        targets = by_key.pop(source.key, ())
        if not targets:
            continue
        if not source.enabled:
            failures[source.key] = "게시판이 비활성이다 — 수집도 멈춘 곳이라 확인할 수 없다"
            continue
        try:
            with open_client(source) as client:
                report = sweep_source(source, adapter_of(source.key), client, targets, today=today)
        # `ValueError`까지 잡는 이유는 collect와 같다 — 망가진 외부 입력이 표준 라이브러리
        # 예외로 나온다. 프로그래밍 실수(TypeError 등)는 그대로 터뜨린다.
        except (FetchError, ParseError, ValueError) as err:
            failures[source.key] = f"{type(err).__name__}: {err}"
            continue
        reports.append(report)
        if dry_run or not report.gone:
            continue
        marked += store.mark_gone([target.review_data_id for target in report.gone], at=kst_now())
        if jobs is not None:
            closed += sum(
                jobs.close_job(target.published_job_id)
                for target in report.gone
                if target.published_job_id is not None
            )
    for key in sorted(by_key):
        failures.setdefault(key, "어댑터·설정이 없는 게시판이다")
    expired_closed = 0 if dry_run else _close_expired(store, jobs, today=today)
    return GoneRunReport(
        reports=tuple(reports),
        failures=failures,
        marked=marked,
        closed=closed,
        expired_closed=expired_closed,
    )


def _close_expired(store: Store, jobs: PublishTarget | None, *, today: date) -> int:
    """마감이 지난 **우리** 공고를 내린다 — min_job은 화면에서만 가리고 DB 상태는 쌓인다.

    ⚠️ 후보(`expired_job_ids`)는 `church_id IS NULL`일 뿐이라 min_job이 만든 행이 섞일 수
    있다 — **우리 것 판별의 정본**(`published_job_ids` · SPEC §8)과 교집합을 낸 뒤 닫는다.
    """
    if jobs is None:
        return 0
    ours = store.published_job_ids()
    return sum(
        jobs.close_job(job_id) for job_id in jobs.expired_job_ids(today=today) if job_id in ours
    )


def _targets_by_source(targets: Sequence[GoneTarget]) -> dict[str, tuple[GoneTarget, ...]]:
    grouped: dict[str, list[GoneTarget]] = {}
    for target in targets:
        grouped.setdefault(target.source_key, []).append(target)
    return {key: tuple(members) for key, members in grouped.items()}
