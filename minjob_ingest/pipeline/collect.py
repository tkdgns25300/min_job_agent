"""수집 — 목록을 훑어 새 글만 `source_data`에 넣는다(SPEC §4).

**결정과 실행을 나눠 뒀다.** `cutoff_date`·`plan_page`·`require_no_conflicts`는 순수 함수라
게시판 없이 검증되고, `collect_source`가 그 판단대로 요청·저장한다.

요청 순서: 목록 1p → 원장 대조 → 새 글만 상세 → 저장 → (컷오프 안이면) 다음 페이지.
**상세는 새 글에만 요청한다** — 이미 본 글을 다시 긁지 않는 것이 증분의 전부다(가드레일 #7).
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from math import ceil
from typing import Final
from uuid import UUID

from minjob_ingest.clock import utc_now
from minjob_ingest.fetch.client import SourceClient
from minjob_ingest.models import SourceData
from minjob_ingest.sources.adapters.base import PostingRef, RawPosting
from minjob_ingest.sources.adapters.registry import Adapter
from minjob_ingest.sources.registry import SourceConfig
from minjob_ingest.store.base import LedgerEntry, Store

#: 목록 페이지 상한 기본값(SPEC §3). 실행 옵션으로 덮을 수 있다.
DEFAULT_MAX_PAGES: Final = 3

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
    """N개월 전 같은 날. 말일 보정을 한다(3/31에서 1개월 전 = 2/28).

    `--months`는 달 단위라 90일 근사로 하면 달마다 범위가 달라진다.
    """
    if months < 1:
        raise ValueError(f"months는 1 이상이어야 함 ({months})")
    total = today.year * 12 + (today.month - 1) - months
    year, month = divmod(total, 12)
    last_day = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(today.day, last_day))


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

    #: 게시일 컷오프. `None`이면 날짜로 자르지 않고 페이지 수로만 범위를 정한다.
    months: int | None = DEFAULT_MONTHS
    max_pages: int = DEFAULT_MAX_PAGES
    #: 저장하지 않고 무엇을 가져올지만 본다. 목록은 요청하고 **상세는 표본 1건만** 요청한다 —
    #: 상세 파싱이 되는지 확인해야 하고(목록만 보면 반쪽 검증), 게시판 부담은 1건이다.
    dry_run: bool = False


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
    #: `--dry-run`에서 상세 파싱을 확인한 표본(없으면 새 글이 없었다는 뜻).
    detail_sample: RawPosting | None = None
    #: 적용된 게시일 컷오프(`None`이면 날짜로 자르지 않았다).
    cutoff: date | None = None
    #: 페이지 상한.
    max_pages: int = DEFAULT_MAX_PAGES
    #: ⚠️ **컷오프에 도달하기 전에 페이지 상한에 걸려 멈췄는가.**
    #: 이걸 알려주지 않으면 "범위 밖 0"만 보고 3개월을 다 받은 줄 안다 — 조용한 미달이다.
    stopped_at_page_cap: bool = False

    @property
    def short_of_cutoff(self) -> bool:
        """요청한 범위를 채우지 못했는가. 채웠으면 마지막 페이지가 컷오프 밖이었을 것이다."""
        return self.stopped_at_page_cap and self.cutoff is not None

    def pages_needed_estimate(self) -> int | None:
        """컷오프까지 받으려면 몇 페이지가 필요한가(관측된 게시 속도로 추정).

        정확할 필요는 없다 — 운영자가 `--pages`를 얼마로 줄지 감을 잡는 용도다.
        """
        if self.cutoff is None or self.oldest is None or self.newest is None:
            return None
        covered = (self.newest - self.oldest).days + 1
        remaining = (self.oldest - self.cutoff).days
        if covered < 1 or remaining <= 0 or self.pages_read < 1:
            return None
        rows_per_page = self.rows / self.pages_read
        if rows_per_page < 1:
            return None
        rate = self.rows / covered
        return self.pages_read + ceil(rate * remaining / rows_per_page)


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
) -> CollectReport:
    """게시판 하나를 훑는다. 실패는 그대로 던진다 — 소스 격리는 호출자가 한다(SPEC §3).

    `run_id`는 `dry_run`이 아닐 때 필수다(저장되는 레코드가 참조한다).
    """
    if not options.dry_run and run_id is None:
        raise ValueError("run_id 없이 저장할 수 없다 — dry_run이 아니면 실행을 먼저 시작한다")
    cutoff = None if options.months is None else cutoff_date(options.months, today=today)
    tally = _Tally()

    capped = True  # for-else — break 없이 끝나면 상한에 걸린 것이다
    for page in range(1, options.max_pages + 1):
        listing = adapter.parse_list(client.get(adapter.list_page_url(source, page)).text, source)
        refs = tally.drop_already_scanned(listing)
        ledger = store.seen_postings(source.key, [ref.external_id for ref in refs])
        plan = plan_page(refs, ledger, cutoff=cutoff)
        require_no_conflicts(source.key, plan.conflicts)
        tally.add(plan, pages_read=page)

        for ref in plan.fresh:
            if options.dry_run:
                tally.sample_detail_once(adapter, client, ref)
                continue
            assert run_id is not None  # 위에서 검증
            tally.saved += int(
                store.save_source_data(_record(source, adapter, client, ref, run_id))
            )
        if not plan.has_more_pages:
            capped = False
            break

    return tally.report(
        source.key,
        dry_run=options.dry_run,
        cutoff=cutoff,
        max_pages=options.max_pages,
        stopped_at_page_cap=capped,
    )


def _record(
    source: SourceConfig, adapter: Adapter, client: SourceClient, ref: PostingRef, run_id: UUID
) -> SourceData:
    """상세를 받아 저장 레코드로. 여기가 원문 증거가 만들어지는 유일한 곳이다."""
    raw = adapter.parse_detail(client.get(ref.url).text, ref)
    return SourceData(
        source_key=source.key,
        external_id=ref.external_id,
        source_url=ref.url,
        title=ref.title,
        posted_on=ref.posted_on,
        run_id=run_id,
        fetched_at=utc_now(),
        raw_text=raw.raw_text,
        image_urls=raw.image_urls,
        attachments=raw.attachments,
        raw_meta=dict(ref.list_meta),
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

    def sample_detail_once(self, adapter: Adapter, client: SourceClient, ref: PostingRef) -> None:
        """`--dry-run`에서 상세 파싱을 한 번만 확인한다(요청 1건)."""
        if self.detail_sample is None:
            self.detail_sample = adapter.parse_detail(client.get(ref.url).text, ref)

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
            detail_sample=self.detail_sample if dry_run else None,
            cutoff=cutoff,
            max_pages=max_pages,
            stopped_at_page_cap=stopped_at_page_cap,
        )
