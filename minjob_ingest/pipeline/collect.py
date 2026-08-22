"""수집 — 목록을 훑어 새 글만 `source_data`에 넣는다(SPEC §4).

**결정과 실행을 나눠 뒀다.** `cutoff_date`·`plan_page`·`require_no_conflicts`는 순수 함수라
게시판 없이 검증되고, `collect_source`가 그 판단대로 요청·저장한다.

요청 순서: 목록 1p → 원장 대조 → 새 글만 상세 → 저장 → (컷오프 안이면) 다음 페이지.
**상세는 새 글에만 요청한다** — 이미 본 글을 다시 긁지 않는 것이 증분의 전부다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final
from uuid import UUID

from minjob_ingest.clock import kst_now, months_before
from minjob_ingest.fetch.client import FetchError, SourceClient
from minjob_ingest.models import SourceData
from minjob_ingest.sources.adapters.base import ParseError, PostingRef, RawPosting
from minjob_ingest.sources.adapters.registry import Adapter, needs_detail_request
from minjob_ingest.sources.registry import SourceConfig
from minjob_ingest.store.base import LedgerEntry, Store

_LOG = logging.getLogger(__name__)

#: 목록 페이지 **안전 상한**(폭주 방지용 · SPEC §3).
#:
#: ⚠️ 이건 **범위를 정하는 값이 아니다.** 범위는 게시일 컷오프(`--months`)가 정하고, 이 값은
#: "날짜 판정이 깨졌을 때 500페이지를 걷지 않게" 막는 역할만 한다. 예전엔 기본값이 3이어서
#: `--months 3`을 줘도 4주치만 가져왔다 — 운영자가 페이지 수를 계산해야 하는 건 설계 결함이다.
PAGE_SAFETY_CEILING: Final = 100

#: 백필 기본 범위(SPEC §4).
DEFAULT_MONTHS: Final = 3


class LedgerConflict(Exception):
    """같은 번호가 다른 글을 가리킨다 — 그 소스를 실패시킨다(SPEC §4).

    조용히 건너뛰면 그 공고를 영구히 놓친다. 원인은 둘이고 둘 다 사람이 봐야 한다:
    게시판이 번호를 재사용했거나, 사이트 개편으로 우리가 엉뚱한 칸을 읽기 시작했거나.
    """


@dataclass(frozen=True, slots=True)
class Conflict:
    """번호는 같은데 제목·게시일이 둘 다 다른 글."""

    ref: PostingRef
    stored: LedgerEntry

    def describe(self) -> str:
        return (
            f"{self.ref.external_id}: 저장된 것 {self.stored.title!r}({self.stored.posted_on})"
            f" ≠ 목록의 것 {self.ref.title!r}({self.ref.posted_on})"
        )


@dataclass(frozen=True, slots=True)
class PagePlan:
    """목록 한 페이지를 훑은 결과. 무엇을 요청할지와 다음 페이지로 갈지."""

    #: 원장에 없고 컷오프 안 — 상세를 요청할 것.
    fresh: tuple[PostingRef, ...]
    #: 원장에 이미 있음 — 건너뛴다(상세 요청 안 함).
    seen: tuple[PostingRef, ...]
    #: 컷오프보다 오래됨 — 이번 범위 밖.
    stale: tuple[PostingRef, ...]
    #: 번호가 다른 글을 가리킴 — 소스 실패 대상.
    conflicts: tuple[Conflict, ...]

    @property
    def rows(self) -> tuple[PostingRef, ...]:
        """분류 전 이 페이지의 모든 행(충돌은 `seen`에도 있어 중복 세지 않는다)."""
        return (*self.fresh, *self.seen, *self.stale)

    @property
    def dated(self) -> int:
        """게시일이 있는 행 수. 컷오프를 적용할 수 있는지 판단한다."""
        return sum(1 for ref in self.rows if ref.posted_on is not None)

    @property
    def within_cutoff(self) -> int:
        return len(self.fresh) + len(self.seen)

    @property
    def has_more_pages(self) -> bool:
        """다음 페이지로 갈까.

        기준은 **컷오프에 드는 행이 있었는가**다. "새 글이 없으면 멈춘다"로 하면 안 된다 —
        1개월 백필을 먼저 돌린 뒤 3개월로 다시 돌리면 앞 페이지가 전부 "이미 본 글"이어서
        더 오래된 미수집 공고에 **도달하지 못한다**. 게시판은 날짜 역순이므로 한 페이지가
        전부 컷오프 밖이면 그 아래는 더 오래된 글뿐이고, 그때 멈추는 것이 안전하다.
        """
        return self.within_cutoff > 0


def cutoff_date(months: int, *, today: date) -> date:
    """수집 범위의 시작일 = N개월 전 같은 날. 셈법은 `clock.months_before`가 갖는다."""
    return months_before(today, months)


def plan_page(
    refs: Sequence[PostingRef],
    ledger: Mapping[str, LedgerEntry],
    *,
    cutoff: date | None,
) -> PagePlan:
    """목록 행들을 [새 글 / 본 글 / 범위 밖 / 충돌]로 가른다.

    ⚠️ **"이미 본 글을 만나면 중단" 하지 않는다**(SPEC §4) — 고정공지·끌어올림 때문에 위쪽에
    아는 글이 섞이므로, 페이지를 다 훑고 **새 것만 고른다**.

    `cutoff`가 `None`이면 날짜로 자르지 않는다(목록에 날짜가 없는 게시판 — 페이지 수로 범위를
    정한다). 날짜가 없는 개별 행도 자르지 않는다 — 판단 근거가 없을 때 버리면 유실이다.
    """
    fresh: list[PostingRef] = []
    seen: list[PostingRef] = []
    stale: list[PostingRef] = []
    conflicts: list[Conflict] = []
    for ref in refs:
        if _is_stale(ref, cutoff):
            stale.append(ref)
            continue
        stored = ledger.get(ref.external_id)
        if stored is None:
            fresh.append(ref)
            continue
        seen.append(ref)
        if stored.points_to_another_posting(title=ref.title, posted_on=ref.posted_on):
            conflicts.append(Conflict(ref=ref, stored=stored))
    return PagePlan(
        fresh=tuple(fresh), seen=tuple(seen), stale=tuple(stale), conflicts=tuple(conflicts)
    )


def require_no_conflicts(source_key: str, conflicts: Sequence[Conflict]) -> None:
    """충돌이 있으면 소스를 실패시킨다. 호출자는 소스 단위로 격리한다(SPEC §3)."""
    if not conflicts:
        return
    detail = " · ".join(conflict.describe() for conflict in conflicts)
    raise LedgerConflict(
        f"{source_key}: 같은 번호가 다른 글을 가리킴 {len(conflicts)}건 — {detail}"
    )


def _is_stale(ref: PostingRef, cutoff: date | None) -> bool:
    if cutoff is None or ref.posted_on is None:
        return False
    return ref.posted_on < cutoff


@dataclass(frozen=True, slots=True)
class CollectOptions:
    """실행 옵션. 정책 기본값은 모듈 상단 상수이고 여기서 덮는다(CLAUDE.md Fetch)."""

    #: 게시일 컷오프. **범위는 이 값이 정한다**(운영자가 페이지 수를 계산하지 않는다 —
    #: 그래서 CLI에 페이지 옵션이 없다). `None`이면 날짜로 자르지 않으므로 그때만
    #: `max_pages`가 범위 역할을 한다(목록에 날짜가 없는 게시판용).
    months: int | None = DEFAULT_MONTHS
    #: 달 대신 일 단위 범위. 짧은 범위(2주)는 달로 표현할 수 없어서 있다 — 같은 "얼마나
    #: 과거까지"의 다른 표기이므로 **`months`와 함께 쓰지 않는다**.
    days: int | None = None
    #: 안전 상한. **CLI로 노출하지 않는다** — 폭주 방지용이라 운영자가 만질 값이 아니다.
    max_pages: int = PAGE_SAFETY_CEILING
    #: 저장하지 않고 무엇을 가져올지만 본다. 목록은 요청하고 **상세는 표본 1건만** 요청한다 —
    #: 상세 파싱이 되는지 확인해야 하고(목록만 보면 반쪽 검증), 게시판 부담은 1건이다.
    dry_run: bool = False

    def __post_init__(self) -> None:
        # 둘 다 받으면 어느 범위로 돌았는지 리포트만 보고 알 수 없다 — 조용한 오해를 막는다.
        if self.months is not None and self.days is not None:
            raise ValueError("months와 days는 함께 쓸 수 없다 — 범위는 하나로 정한다")
        if self.days is not None and self.days < 1:
            raise ValueError(f"days는 1 이상이어야 함 ({self.days})")
        # ⚠️ `months=0`은 만들 수 있는 상태였고 실패가 수집 도중(`cutoff_date`)에야 났다.
        #    경계에서 막는다 — 범위를 안 두려면 `None`이다.
        if self.months is not None and self.months < 1:
            raise ValueError(f"months는 1 이상이어야 함 ({self.months})")


@dataclass(frozen=True, slots=True)
class CollectReport:
    """한 게시판을 훑은 결과. `--dry-run`이 이걸 그대로 출력한다."""

    source_key: str
    pages_read: int
    rows: int
    fresh: int
    seen: int
    stale: int
    saved: int
    #: 페이지 경계에서 다시 나타난 글 수. 스캔 중 새 글이 올라와 밀린 것이며 **정상**이다 —
    #: 한 번만 수집한다(페이지 *안* 중복은 어댑터 책임이라 `as_listing`이 에러로 막는다).
    shifted: int
    oldest: date | None
    newest: date | None
    #: 사람이 눈으로 확인할 표본.
    samples: tuple[PostingRef, ...]
    #: 상세를 읽지 못한 글 수. ⚠️ **한 건이 그 게시판 전체를 멈추게 하지 않는다** — 800행
    #: 게시판에서 350번째가 이상하면 나머지 450건에 영구히 도달하지 못한다(원장이 이미 저장한
    #: 것만 건너뛰고 같은 자리에서 또 실패한다). 세어서 보고하고 계속 간다.
    #: 저장했지만 내용이 하나도 없던 글 수. **실패가 아니다** — 게시판에 내용 없이 올라온 글이
    #: 있다. 다만 개수가 크면 본문 셀렉터가 일부 스킨에서 빗나간 신호라 눈에 보이게 찍는다.
    empty: int = 0
    failed: int = 0
    #: 실패 사유 표본. 개수만으로는 무엇이 깨졌는지 알 수 없다.
    failure_samples: tuple[str, ...] = ()
    #: `--dry-run`에서 상세 파싱을 확인한 표본(없으면 새 글이 없었다는 뜻).
    detail_sample: RawPosting | None = None
    #: 적용된 게시일 컷오프(`None`이면 날짜로 자르지 않았다).
    cutoff: date | None = None
    #: 적용된 안전 상한.
    max_pages: int = PAGE_SAFETY_CEILING
    #: ⚠️ **컷오프에 도달하기 전에 안전 상한에 걸려 멈췄는가.**
    #: 상한이 넉넉해진 뒤로 이건 정상 상황이 아니다 — 날짜 판정이 깨졌거나 게시판이 예상보다
    #: 훨씬 활발하다는 신호다.
    stopped_at_page_cap: bool = False

    @property
    def short_of_cutoff(self) -> bool:
        """요청한 범위를 채우지 못했는가. 채웠으면 마지막 페이지가 컷오프 밖이었을 것이다."""
        return self.stopped_at_page_cap and self.cutoff is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class Progress:
    """지금까지 무엇을 했나 — 진행 표시용 스냅샷.

    **출력 형식은 여기서 정하지 않는다**(파이프라인은 콘솔을 모른다 · CLI가 그린다). 오래 걸리는
    구간이 무음이면 운영자는 멈춘 건지 도는 건지 알 수 없다 — 상세 227건이면 6분이다.
    """

    #: 지금 읽은 목록 페이지 번호.
    page: int
    #: 누적 목록 행 수.
    rows: int
    #: 누적 새 글 수(= 상세를 요청할 대상).
    fresh: int
    #: 누적 상세 요청 수.
    details_done: int
    #: 방금 처리한 글. 숫자만 움직이는 것보다 무엇을 받고 있는지 보이는 게 낫다.
    latest: PostingRef | None = None


#: 진행 알림 수신자. `None`이면 알리지 않는다(테스트·비대화형).
ProgressSink = Callable[[Progress], None]

#: 실패 사유를 몇 개까지 남길지. 전부 남기면 800건 실패 시 리포트가 로그가 된다.
_FAILURE_SAMPLE_SIZE: Final = 3

#: `--dry-run` 리포트에 넣을 표본 수.
_SAMPLE_SIZE: Final = 3


def collect_source(
    source: SourceConfig,
    adapter: Adapter,
    client: SourceClient,
    store: Store,
    *,
    run_id: UUID | None,
    options: CollectOptions,
    today: date,
    on_progress: ProgressSink | None = None,
) -> CollectReport:
    """게시판 하나를 훑는다. 실패는 그대로 던진다 — 소스 격리는 호출자가 한다(SPEC §3).

    `run_id`는 `dry_run`이 아닐 때 필수다(저장되는 레코드가 참조한다).
    """
    if not options.dry_run and run_id is None:
        raise ValueError("run_id 없이 저장할 수 없다 — dry_run이 아니면 실행을 먼저 시작한다")
    cutoff = _cutoff_for(source, options, today=today)
    tally = _Tally()

    page_limit = _page_limit_for(source, options)
    capped = True  # for-else — break 없이 끝나면 상한에 걸린 것이다
    for page in range(1, page_limit + 1):
        listing = adapter.parse_list(_fetch_list(adapter, client, source, page), source)
        refs = tally.drop_already_scanned(listing)
        ledger = store.seen_postings(source.key, [ref.external_id for ref in refs])
        plan = plan_page(refs, ledger, cutoff=cutoff)
        require_no_conflicts(source.key, plan.conflicts)
        _require_dates_for_cutoff(source.key, plan, cutoff=cutoff)
        tally.add(plan, pages_read=page)
        _notify(on_progress, tally)

        for ref in plan.fresh:
            # ⚠️ 글 하나의 실패로 게시판 전체를 포기하지 않는다(`CollectReport.failed` 참조).
            # 잡는 것은 **예상된 실패만**이다 — 그 밖의 예외는 버그라서 그대로 터뜨린다.
            try:
                if options.dry_run:
                    if not tally.sample_detail_once(adapter, client, ref):
                        continue
                else:
                    assert run_id is not None  # 위에서 검증
                    record = _record(source, adapter, client, ref, run_id, today=today)
                    tally.note_saved(record, inserted=store.save_source_data(record))
            # `ValueError`도 잡는다 — 표준 라이브러리가 **망가진 외부 입력**에 내는 예외다
            # (`urljoin`이 교회의 잘못된 주소에 `Invalid IPv6 URL`을 던져 수집 전체를 죽였다).
            # 프로그래밍 실수는 보통 TypeError·AttributeError·KeyError라 여전히 크래시한다.
            except (ParseError, FetchError, ValueError) as err:
                tally.note_failure(ref, err)
                continue
            _notify(on_progress, tally, latest=ref)
        if not plan.has_more_pages:
            capped = False
            break

    tally.require_some_saved(source.key, dry_run=options.dry_run)
    tally.require_some_content(source.key, dry_run=options.dry_run)
    return tally.report(
        source.key,
        dry_run=options.dry_run,
        cutoff=cutoff,
        max_pages=page_limit,
        stopped_at_page_cap=capped,
    )


def _page_limit_for(source: SourceConfig, options: CollectOptions) -> int:
    """이 소스에 적용할 페이지 상한.

    기본은 폭주 방지용 안전 상한이고, **config가 더 낮은 값을 적으면 그것을 쓴다** —
    날짜가 없어 컷오프를 만들 수 없는 게시판의 범위를 적어 두는 자리다(`list_page_limit`).
    실행 옵션(`max_pages`)이 더 낮으면 그것을 존중한다.
    """
    limits = [options.max_pages]
    if source.list_page_limit is not None:
        limits.append(source.list_page_limit)
    return min(limits)


def _cutoff_for(source: SourceConfig, options: CollectOptions, *, today: date) -> date | None:
    """이 소스에 적용할 게시일 컷오프.

    목록에 날짜가 없는 게시판(config `list_has_dates: false`)은 **컷오프를 만들지 않는다** —
    만들면 아무 행도 잘리지 않아 안전 상한까지 페이지를 넘기고, 그게 조용한 폭주다.
    그런 소스의 범위는 페이지 상한이 정한다. 눈에 보이게 로그를 남긴다.
    """
    if options.months is None and options.days is None:
        return None
    if not source.list_has_dates:
        _LOG.info(
            "%s 목록에 게시일이 없어 기간을 적용하지 않는다 — 페이지 상한이 범위다", source.key
        )
        return None
    if options.days is not None:
        return today - timedelta(days=options.days)
    assert options.months is not None  # 위에서 둘 다 None인 경우를 걸렀다
    return cutoff_date(options.months, today=today)


def _fetch_list(adapter: Adapter, client: SourceClient, source: SourceConfig, page: int) -> str:
    """목록 한 페이지를 받는다. 방식(GET/POST)은 어댑터가 정하고 전송은 fetch 층이 한다."""
    request = adapter.list_request(source, page)
    if request.form is None:
        return client.get(request.url).text
    return client.post_form(request.url, request.form).text


def _notify(sink: ProgressSink | None, tally: _Tally, *, latest: PostingRef | None = None) -> None:
    if sink is not None:
        sink(tally.progress(latest=latest))


def _require_dates_for_cutoff(source_key: str, plan: PagePlan, *, cutoff: date | None) -> None:
    """컷오프를 요청했는데 게시일이 하나도 없으면 실패시킨다.

    날짜가 없으면 `--months`가 아무 행도 자르지 못해 **안전 상한까지 계속 페이지를 넘긴다**
    (조용한 폭주). 목록에 날짜가 없는 게시판은 `--months 0`으로 돌린다(그때는 안전 상한이 범위다).
    """
    if cutoff is None or not plan.rows or plan.dated > 0:
        return
    raise ParseError(
        f"{source_key}: 목록에 게시일이 없어 `--months` 범위를 적용할 수 없다 —"
        f" 어댑터가 목록에서 게시일을 뽑도록 고친다(날짜가 진짜 없는 게시판이면 `--months 0`)"
    )


def _record(
    source: SourceConfig,
    adapter: Adapter,
    client: SourceClient,
    ref: PostingRef,
    run_id: UUID,
    *,
    today: date,
) -> SourceData:
    """상세를 받아 저장 레코드로. 여기가 원문 증거가 만들어지는 유일한 곳이다.

    ⚠️ `today`는 **넘겨받는다** — 실행이 하루를 넘길 수 있고, 시계를 안에서 읽으면 그 값이
    테스트에서도 벽시계를 타 어제 통과한 것이 오늘 깨진다(이 리포 관례 · `gnuboard_list_date`).
    """
    detail_html = client.get(ref.url).text if needs_detail_request(adapter) else ""
    raw = adapter.parse_detail(detail_html, ref)
    return SourceData(
        source_key=source.key,
        external_id=ref.external_id,
        source_url=ref.url,
        title=ref.title,
        # ⚠️ 어댑터가 채우는 것이 원칙이다(`PCKWORLD`는 썸네일 파일명에서 읽는다). 여기 오는
        #    것은 게시판이 날짜를 안 주고 어댑터도 못 찾은 경우뿐이라, **오늘 처음 봤다**는
        #    우리가 아는 유일한 사실을 쓴다 — 없는 과거 날짜를 지어내는 것보다 낫다.
        posted_on=ref.posted_on or today,
        run_id=run_id,
        fetched_at=kst_now(),
        raw_text=raw.raw_text,
        raw_html=raw.raw_html,
        image_urls=raw.image_urls,
        attachments=raw.attachments,
        # `_`로 시작하는 키는 **어댑터가 자기 `parse_detail`에 넘기는 내부 값**이다(HANIL은
        # 목록 JSON의 본문을 그렇게 전달한다) — 저장하면 `raw_text`와 그대로 중복된다.
        raw_meta={key: value for key, value in ref.list_meta.items() if not key.startswith("_")},
    )


@dataclass
class _Tally:
    """페이지를 넘기며 쌓이는 집계. 실행 하나 안에서만 산다."""

    pages_read: int = 0
    rows: int = 0
    fresh: int = 0
    seen: int = 0
    stale: int = 0
    saved: int = 0
    shifted: int = 0
    #: 상세를 실제로 요청한 횟수. `saved`와 다르다 — 이미 있는 행은 저장되지 않는다.
    details: int = 0
    #: 저장했지만 본문·이미지·첨부가 하나도 없던 글. 게시판에 실제로 있다(내용 없이 올린 글).
    empty: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)
    scanned: set[str] = field(default_factory=set)
    dates: list[date] = field(default_factory=list)
    samples: list[PostingRef] = field(default_factory=list)
    detail_sample: RawPosting | None = None

    def drop_already_scanned(self, refs: Sequence[PostingRef]) -> tuple[PostingRef, ...]:
        """이번 실행에서 이미 본 번호를 뺀다.

        스캔 중 새 글이 올라오면 글이 아래 페이지로 밀려 같은 번호가 두 번 나온다. 그대로 두면
        **한 실행에서 상세를 두 번 요청하고 두 번 구조화**한다(비용). 정상 현상이라 에러가 아니다.
        """
        kept = tuple(ref for ref in refs if ref.external_id not in self.scanned)
        self.shifted += len(refs) - len(kept)
        self.scanned.update(ref.external_id for ref in kept)
        return kept

    def add(self, plan: PagePlan, *, pages_read: int) -> None:
        self.pages_read = pages_read
        self.rows += len(plan.fresh) + len(plan.seen) + len(plan.stale)
        self.fresh += len(plan.fresh)
        self.seen += len(plan.seen)
        self.stale += len(plan.stale)
        self.dates.extend(
            ref.posted_on for ref in (*plan.fresh, *plan.seen) if ref.posted_on is not None
        )
        self.samples.extend(plan.fresh[: max(0, _SAMPLE_SIZE - len(self.samples))])

    def note_failure(self, ref: PostingRef, err: Exception) -> None:
        """상세 실패를 세고 사유 표본을 남긴다. **삼키는 것이 아니라 보고로 돌린다.**"""
        self.failed += 1
        if len(self.failures) < _FAILURE_SAMPLE_SIZE:
            self.failures.append(f"{ref.external_id}: {type(err).__name__}: {err}")

    def note_saved(self, record: SourceData, *, inserted: bool) -> None:
        """저장 결과를 센다. **내용이 없는 글도 저장한다** — 그것도 사실이다.

        ⚠️ 예전엔 어댑터가 "본문·이미지·첨부가 모두 없음"을 실패로 던졌다. 그런데 게시판에는
        내용 없이 올라온 글이 실제로 있고(YTUS 25309 = `<p>&nbsp;</p>` · 실측), 실패로 두면
        원장에 안 들어가 **매 실행 다시 받고 매번 "실패 1건"으로 보고된다**. 그 노이즈가
        진짜 실패를 가린다. 셀렉터가 빗나간 경우는 `require_one`(컨테이너 없음)과 아래
        `require_some_content`(전량 빈 내용)가 잡는다.
        """
        self.saved += int(inserted)
        self.details += 1
        if inserted and record.is_empty:
            self.empty += 1

    def require_some_content(self, source_key: str, *, dry_run: bool) -> None:
        """저장한 글이 **전부 비었으면** 소스를 실패시킨다.

        빈 글 하나는 게시판의 사실이지만, 전량이 비었으면 본문 셀렉터가 (컨테이너는 맞고
        내용은 다른 곳으로 옮겨간 식으로) 빗나간 것이다. 일부만 비는 경우는 실패로 만들지
        않고 **개수를 리포트에 찍는다** — 스킨이 두 가지인 게시판이 있어 그때는 사람이 봐야 한다.

        ⚠️ **폼으로만 받는 게시판이 오면 이 검사가 헛돈다** — 값이 본문이 아니라 게시판 필드
        (`raw_meta`)에 오는 곳이 있고(`CSU`), 그 게시판이 본문을 아예 안 쓰게 바뀌면 전량이
        `is_empty`가 되어 **정상인 소스를 실패시킨다**. 지금은 CSU도 대개 본문이 있어 터지지
        않는다(1주치 125건 중 12건만 빈 본문) — 터지면 이 검사가 `raw_meta`도 보게 고친다.
        """
        if dry_run or not self.saved:
            return
        if self.empty == self.saved:
            raise ParseError(
                f"{source_key}: 저장한 {self.saved}건이 **전부** 본문·이미지·첨부가 없음 —"
                f" 상세 본문 셀렉터가 내용을 못 집고 있다"
            )

    def require_some_saved(self, source_key: str, *, dry_run: bool) -> None:
        """실패만 있고 하나도 못 가져왔으면 **소스를 실패시킨다.**

        글 하나의 실패는 넘어가도 되지만 **전량 실패는 어댑터가 깨진 것**이다(상세 셀렉터가
        빗나갔거나 게시판이 차단했다). 그걸 "실패 800건·저장 0건"이라는 경고로 흘리면
        조용한 0건과 다르지 않다.
        """
        if not self.failed:
            return
        got = self.detail_sample is not None if dry_run else bool(self.saved)
        if not got:
            raise ParseError(
                f"{source_key}: 상세 {self.failed}건이 전부 실패하고 하나도 못 가져왔음 —"
                f" 어댑터·차단 확인 ({' · '.join(self.failures)})"
            )

    def sample_detail_once(self, adapter: Adapter, client: SourceClient, ref: PostingRef) -> bool:
        """`--dry-run`에서 상세 파싱을 한 번만 확인한다(요청 1건). 요청했으면 `True`."""
        if self.detail_sample is not None:
            return False
        # 목록에 본문이 든 게시판은 상세를 요청하지 않는다 — 저장 경로와 같은 규칙이어야
        # `--dry-run`이 실제 수집을 대표한다.
        detail_html = client.get(ref.url).text if needs_detail_request(adapter) else ""
        self.detail_sample = adapter.parse_detail(detail_html, ref)
        self.details += 1
        return True

    def progress(self, *, latest: PostingRef | None) -> Progress:
        return Progress(
            page=self.pages_read,
            rows=self.rows,
            fresh=self.fresh,
            details_done=self.details,
            latest=latest,
        )

    def report(
        self,
        source_key: str,
        *,
        dry_run: bool,
        cutoff: date | None,
        max_pages: int,
        stopped_at_page_cap: bool,
    ) -> CollectReport:
        return CollectReport(
            source_key=source_key,
            pages_read=self.pages_read,
            rows=self.rows,
            fresh=self.fresh,
            seen=self.seen,
            stale=self.stale,
            saved=self.saved,
            shifted=self.shifted,
            oldest=min(self.dates, default=None),
            newest=max(self.dates, default=None),
            samples=tuple(self.samples),
            empty=self.empty,
            failed=self.failed,
            failure_samples=tuple(self.failures),
            detail_sample=self.detail_sample if dry_run else None,
            cutoff=cutoff,
            max_pages=max_pages,
            stopped_at_page_cap=stopped_at_page_cap,
        )
