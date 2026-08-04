"""수집 판단 테스트 — 컷오프·페이지 종료·번호 충돌.

결정이 순수 함수라 네트워크·저장 없이 검증한다(가드레일 #7·#10).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import httpx
import pytest

from minjob_ingest.clock import utc_now
from minjob_ingest.domain import CrawlMode
from minjob_ingest.fetch.client import SourceClient
from minjob_ingest.models import SourceData
from minjob_ingest.paths import PROJECT_ROOT
from minjob_ingest.pipeline.collect import (
    CollectOptions,
    CollectReport,
    Conflict,
    LedgerConflict,
    Progress,
    ProgressSink,
    _require_dates_for_cutoff,  # 폭주 방지 가드
    collect_source,
    cutoff_date,
    plan_page,
    require_no_conflicts,
)
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.adapters.registry import find_adapter
from minjob_ingest.sources.registry import find_source, load_sources
from minjob_ingest.store.base import LedgerEntry
from minjob_ingest.store.json_store import JsonStore

_TODAY: Final = date(2026, 8, 4)


def _ref(external_id: str, *, title: str = "청빙 공고", on: date | None = _TODAY) -> PostingRef:
    return PostingRef(
        external_id=external_id,
        url=f"https://www.ytus.ac.kr/board/view/trXXR/{external_id}",
        title=title,
        posted_on=on,
    )


def _entry(ref: PostingRef) -> LedgerEntry:
    """그 참조를 그대로 저장했다면 남을 원장 항목."""
    return LedgerEntry(title=ref.title, posted_on=ref.posted_on)


# ── 컷오프 날짜 ──────────────────────────────────────────────────


def test_cutoff_is_calendar_months_not_ninety_days() -> None:
    # 90일 근사로 하면 달마다 범위가 달라진다.
    assert cutoff_date(3, today=date(2026, 8, 4)) == date(2026, 5, 4)


def test_cutoff_clamps_to_the_last_day_of_the_month() -> None:
    """3/31의 1개월 전은 2/31이 아니다 — 보정하지 않으면 `ValueError`로 죽는다."""
    assert cutoff_date(1, today=date(2026, 3, 31)) == date(2026, 2, 28)
    assert cutoff_date(3, today=date(2026, 5, 31)) == date(2026, 2, 28)


def test_cutoff_crosses_the_year_boundary() -> None:
    assert cutoff_date(3, today=date(2026, 1, 15)) == date(2025, 10, 15)


def test_cutoff_rejects_zero_months() -> None:
    with pytest.raises(ValueError, match="months"):
        cutoff_date(0, today=_TODAY)


# ── 행 분류 ──────────────────────────────────────────────────────


def test_empty_ledger_makes_every_row_fresh() -> None:
    refs = (_ref("1"), _ref("2"))
    plan = plan_page(refs, {}, cutoff=cutoff_date(3, today=_TODAY))
    assert plan.fresh == refs
    assert plan.seen == ()


def test_known_ids_are_skipped_without_a_detail_request() -> None:
    """원장에 있으면 상세를 요청하지 않는다 — 이게 증분의 전부다(가드레일 #7)."""
    known, new = _ref("1"), _ref("2")
    plan = plan_page((known, new), {known.external_id: _entry(known)}, cutoff=None)
    assert plan.fresh == (new,)
    assert plan.seen == (known,)


def test_known_row_does_not_stop_the_scan() -> None:
    """⚠️ "이미 본 글을 만나면 중단" 금지(SPEC §4).

    고정공지·끌어올림 때문에 위쪽에 아는 글이 섞인다 — 거기서 멈추면 아래 새 글을 놓친다.
    """
    known, new = _ref("1"), _ref("2")
    plan = plan_page((known, new), {known.external_id: _entry(known)}, cutoff=None)
    assert new in plan.fresh  # 아는 글 뒤에 있어도 잡힌다


def test_rows_older_than_the_cutoff_are_out_of_range() -> None:
    recent, old = _ref("1", on=date(2026, 8, 1)), _ref("2", on=date(2026, 1, 1))
    plan = plan_page((recent, old), {}, cutoff=cutoff_date(3, today=_TODAY))
    assert plan.fresh == (recent,)
    assert plan.stale == (old,)


def test_the_cutoff_day_itself_is_inside_the_range() -> None:
    """`--months 3`은 "3개월 전 그날부터"다.

    경계를 배타적으로 하면 정확히 그날 올라온 공고가 조용히 빠진다 — 경계는 명시해야 한다.
    """
    cutoff = cutoff_date(3, today=_TODAY)
    on_the_line = _ref("1", on=cutoff)
    one_day_before = _ref("2", on=date(cutoff.year, cutoff.month, cutoff.day - 1))
    plan = plan_page((on_the_line, one_day_before), {}, cutoff=cutoff)
    assert plan.fresh == (on_the_line,)
    assert plan.stale == (one_day_before,)


def test_rows_without_a_date_are_kept() -> None:
    """목록에 날짜가 없는 게시판이 있다 — 판단 근거가 없을 때 버리면 유실이다."""
    plan = plan_page((_ref("1", on=None),), {}, cutoff=cutoff_date(3, today=_TODAY))
    assert len(plan.fresh) == 1


def test_no_cutoff_keeps_everything() -> None:
    old = _ref("1", on=date(2020, 1, 1))
    plan = plan_page((old,), {}, cutoff=None)
    assert plan.fresh == (old,)


# ── 페이지 종료 ──────────────────────────────────────────────────


def test_scan_continues_while_rows_fall_inside_the_cutoff() -> None:
    plan = plan_page((_ref("1"),), {}, cutoff=cutoff_date(3, today=_TODAY))
    assert plan.has_more_pages


def test_scan_stops_when_the_whole_page_is_older_than_the_cutoff() -> None:
    # 게시판은 날짜 역순이라 그 아래는 더 오래된 글뿐이다.
    plan = plan_page((_ref("1", on=date(2020, 1, 1)),), {}, cutoff=cutoff_date(3, today=_TODAY))
    assert not plan.has_more_pages


def test_a_page_of_only_known_rows_still_continues() -> None:
    """ "새 글이 없으면 멈춘다"로 하면 안 된다.

    1개월 백필을 먼저 돌린 뒤 3개월로 다시 돌리면 앞 페이지가 전부 "이미 본 글"이라
    **더 오래된 미수집 공고에 도달하지 못한다**.
    """
    known = _ref("1")
    plan = plan_page((known,), {known.external_id: _entry(known)}, cutoff=None)
    assert plan.fresh == ()
    assert plan.has_more_pages


# ── 번호 충돌 ────────────────────────────────────────────────────


def test_same_number_with_a_different_posting_is_a_conflict() -> None:
    ref = _ref("100", title="안동도원교회 유년부전도사", on=date(2026, 8, 4))
    stored = LedgerEntry(title="○○교회 부목사 청빙", posted_on=date(2026, 5, 1))
    plan = plan_page((ref,), {ref.external_id: stored}, cutoff=None)
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].ref is ref


def test_an_edited_posting_is_not_a_conflict() -> None:
    """제목에 `[끌어올림]`을 붙이는 일이 흔하다 — 하나만 다르면 정상으로 본다."""
    ref = _ref("100", title="[끌어올림] 청빙 공고", on=_TODAY)
    stored = LedgerEntry(title="청빙 공고", posted_on=_TODAY)
    plan = plan_page((ref,), {ref.external_id: stored}, cutoff=None)
    assert plan.conflicts == ()


def test_conflicting_rows_are_still_counted_as_seen() -> None:
    """충돌해도 자동으로 새 글 취급하지 않는다 — 원장 키 체계를 코드가 몰래 바꾸면 안 된다."""
    ref = _ref("100", title="다른 글", on=date(2026, 8, 4))
    stored = LedgerEntry(title="원래 글", posted_on=date(2026, 5, 1))
    plan = plan_page((ref,), {ref.external_id: stored}, cutoff=None)
    assert plan.seen == (ref,)
    assert plan.fresh == ()


def test_no_conflicts_passes_quietly() -> None:
    require_no_conflicts("YTUS", ())


def test_conflicts_fail_the_source_with_both_values_named() -> None:
    """운영자가 로그만 보고 원인을 판단할 수 있어야 한다."""
    ref = _ref("100", title="목록의 글", on=date(2026, 8, 4))
    stored = LedgerEntry(title="저장된 글", posted_on=date(2026, 5, 1))
    with pytest.raises(LedgerConflict) as caught:
        require_no_conflicts("YTUS", (Conflict(ref=ref, stored=stored),))
    message = str(caught.value)
    assert "YTUS" in message
    assert "저장된 글" in message
    assert "목록의 글" in message
    assert "100" in message


# ── 어댑터 레지스트리 ────────────────────────────────────────────


def test_registered_adapter_is_found() -> None:
    from minjob_ingest.sources.adapters import ytus
    from minjob_ingest.sources.adapters.registry import find_adapter

    assert find_adapter("YTUS") is ytus


def test_unregistered_board_fails_loudly() -> None:
    """어댑터가 없는 게시판을 조용히 건너뛰면 "게시판이 조용하네"로 오해한다."""
    from minjob_ingest.sources.adapters.registry import AdapterMissing, find_adapter

    with pytest.raises(AdapterMissing, match="PUTS"):
        find_adapter("PUTS")


def test_every_registered_key_exists_in_the_source_config() -> None:
    """레지스트리 키가 `config/sources.json`에 없으면 오타다."""
    from minjob_ingest.sources.adapters.registry import implemented_keys
    from minjob_ingest.sources.registry import load_sources

    configured = {source.key for source in load_sources(None)}
    assert set(implemented_keys()) <= configured


# ── 실행 루프 (네트워크 없음 · MockTransport) ────────────────────


@pytest.fixture(scope="module")
def board_html() -> tuple[str, str]:
    fixtures = PROJECT_ROOT / "tests" / "fixtures" / "YTUS"
    return (
        (fixtures / "list.html").read_text(encoding="utf-8"),
        (fixtures / "detail_with_image.html").read_text(encoding="utf-8"),
    )


class _Board:
    """게시판 대역. 요청 경로를 기록해 "상세를 몇 번 요청했나"를 검증한다."""

    def __init__(self, list_html: str, detail_html: str) -> None:
        self.paths: list[str] = []
        self._list = list_html
        self._detail = detail_html

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if "/board/view/" in request.url.path:
            return httpx.Response(200, text=self._detail)
        return httpx.Response(200, text=self._list)

    @property
    def detail_requests(self) -> int:
        return sum(1 for path in self.paths if "/board/view/" in path)

    @property
    def list_requests(self) -> int:
        return sum(1 for path in self.paths if "/board/list" in path)


def _collect(
    board: _Board,
    store: JsonStore,
    options: CollectOptions,
    *,
    today: date = _TODAY,
    on_progress: ProgressSink | None = None,
) -> CollectReport:
    source = find_source(load_sources(None), "YTUS")
    assert source is not None
    ticks = iter(range(100_000))
    run = None if options.dry_run else store.start_run(CrawlMode.BACKFILL)
    with SourceClient(
        source,
        transport=httpx.MockTransport(board.handler),
        sleep=lambda _seconds: None,
        monotonic=lambda: float(next(ticks)),
    ) as client:
        return collect_source(
            source,
            find_adapter("YTUS"),
            client,
            store,
            run_id=None if run is None else run.id,
            options=options,
            today=today,
            on_progress=on_progress,
        )


def test_first_run_collects_every_posting(board_html: tuple[str, str], tmp_path: Path) -> None:
    board = _Board(*board_html)
    report = _collect(board, JsonStore(tmp_path / "data"), CollectOptions(max_pages=1))
    assert report.rows == 18
    assert report.saved == 18
    assert board.detail_requests == 18


def test_second_run_requests_no_details(board_html: tuple[str, str], tmp_path: Path) -> None:
    """증분의 전부 — 이미 본 글의 상세를 다시 요청하지 않는다(가드레일 #7)."""
    store = JsonStore(tmp_path / "data")
    _collect(_Board(*board_html), store, CollectOptions(max_pages=1))
    board = _Board(*board_html)
    report = _collect(board, store, CollectOptions(max_pages=1))
    assert report.fresh == 0
    assert report.seen == 18
    assert report.saved == 0
    assert board.detail_requests == 0


def test_dry_run_saves_nothing_and_samples_one_detail(
    board_html: tuple[str, str], tmp_path: Path
) -> None:
    """목록만 보면 상세 파싱이 검증되지 않는다 → 표본 1건만 요청한다."""
    store = JsonStore(tmp_path / "data")
    board = _Board(*board_html)
    report = _collect(board, store, CollectOptions(max_pages=1, dry_run=True))
    assert report.fresh == 18
    assert report.saved == 0
    assert board.detail_requests == 1
    assert report.detail_sample is not None
    assert len(report.detail_sample.attachments) == 1
    assert store.list_unstructured(limit=1) == ()  # 아무것도 저장되지 않았다


def test_dry_run_leaves_no_run_record(board_html: tuple[str, str], tmp_path: Path) -> None:
    """`--dry-run`은 실행 기록조차 남기지 않는다 — 대시보드가 유령 실행을 보면 안 된다."""
    data_dir = tmp_path / "data"
    _collect(_Board(*board_html), JsonStore(data_dir), CollectOptions(max_pages=1, dry_run=True))
    assert not (data_dir / "crawl_run.json").exists()


def test_months_none_disables_the_date_cutoff(board_html: tuple[str, str], tmp_path: Path) -> None:
    """`--months 0`은 "날짜로 자르지 않음"이다(목록에 날짜가 없는 게시판·전량 수집용).

    ⚠️ 오늘 날짜를 **한참 뒤로** 준다. 가까운 날짜로 확인하면 기본 컷오프(3개월)로도 아무것도
    걸러지지 않아, `None`이 무시되고 기본값이 적용돼도 테스트가 통과한다.
    """
    board = _Board(*board_html)
    report = _collect(
        board,
        JsonStore(tmp_path / "data"),
        CollectOptions(months=None, max_pages=1),
        today=date(2027, 12, 31),  # 3개월 컷오프라면 18건 전부 범위 밖이 된다
    )
    assert report.stale == 0
    assert report.fresh == 18


def test_loop_applies_the_cutoff(board_html: tuple[str, str], tmp_path: Path) -> None:
    """컷오프가 루프에 실제로 전달되는지 — fixture 게시일 2026-07-31~08-04 기준."""
    board = _Board(*board_html)
    # 2026-11-01에서 3개월 전 = 2026-08-01 → 07-31자 행들은 범위 밖.
    report = _collect(
        board,
        JsonStore(tmp_path / "data"),
        CollectOptions(months=3, max_pages=1),
        today=date(2026, 11, 1),
    )
    assert report.stale > 0
    assert report.fresh + report.stale == 18
    assert board.detail_requests == report.fresh  # 범위 밖은 상세를 요청하지 않는다


def test_loop_fails_the_source_on_a_ledger_conflict(
    board_html: tuple[str, str], tmp_path: Path
) -> None:
    """같은 번호가 다른 글을 가리키면 그 소스를 세운다 — 조용히 건너뛰면 공고를 잃는다."""
    store = JsonStore(tmp_path / "data")
    run = store.start_run(CrawlMode.BACKFILL)
    store.save_source_data(
        SourceData(
            source_key="YTUS",
            external_id="25581",  # fixture 최신 글의 번호
            source_url="https://www.ytus.ac.kr/board/view/trXXR/25581",
            title="○○교회 부목사 청빙",  # 제목·게시일이 둘 다 다르다
            posted_on=date(2026, 5, 1),
            run_id=run.id,
            fetched_at=utc_now(),
            raw_text="예전 글",
        )
    )
    with pytest.raises(LedgerConflict, match="25581"):
        _collect(_Board(*board_html), store, CollectOptions(max_pages=1))


def test_saving_requires_a_run_id(board_html: tuple[str, str], tmp_path: Path) -> None:
    """레코드가 참조할 실행이 없는데 저장하면 원장에 고아 행이 생긴다."""
    source = find_source(load_sources(None), "YTUS")
    assert source is not None
    board = _Board(*board_html)
    with (
        SourceClient(source, transport=httpx.MockTransport(board.handler)) as client,
        pytest.raises(ValueError, match="run_id"),
    ):
        collect_source(
            source,
            find_adapter("YTUS"),
            client,
            JsonStore(tmp_path / "data"),
            run_id=None,
            options=CollectOptions(dry_run=False),
            today=_TODAY,
        )


def test_a_posting_shifted_across_pages_is_collected_once(
    board_html: tuple[str, str], tmp_path: Path
) -> None:
    """스캔 중 새 글이 올라오면 같은 글이 두 페이지에 나온다 — 상세를 두 번 요청하면 비용이다.

    페이지 *안* 중복은 어댑터 버그라 `as_listing`이 에러로 막고, 페이지 *간* 중복은 정상이므로
    조용히 한 번만 수집한다.
    """
    board = _Board(*board_html)  # 모든 페이지가 같은 목록을 준다
    report = _collect(board, JsonStore(tmp_path / "data"), CollectOptions(max_pages=3))
    assert report.saved == 18  # 같은 글이 여러 페이지에 나왔지만 한 번만
    assert board.detail_requests == 18
    assert report.shifted == 18  # 2페이지의 18건이 이미 스캔된 것으로 걸러졌다
    # 2페이지가 전부 걸러져 컷오프에 드는 행이 0 → 거기서 멈춘다(같은 내용의 반복이므로 맞다).
    assert report.pages_read == 2


# ── 진행 알림 ────────────────────────────────────────────────────


def test_progress_is_reported_as_work_happens(board_html: tuple[str, str], tmp_path: Path) -> None:
    """⚠️ **다 끝나고 한 번에 나오면 안 된다** — 상세 227건이면 6분간 무음이다.

    운영자는 멈춘 건지 도는 건지 알 수 없다. 페이지마다 한 번 + 상세마다 한 번 알린다.
    """
    seen: list[Progress] = []
    board = _Board(*board_html)
    report = _collect(
        board, JsonStore(tmp_path / "data"), CollectOptions(max_pages=1), on_progress=seen.append
    )
    assert len(seen) == 1 + report.saved  # 페이지 1회 + 상세 18회
    assert seen[0].details_done == 0  # 목록만 읽은 시점
    assert [p.details_done for p in seen[1:]] == list(range(1, report.saved + 1))
    assert all(p.fresh == 18 and p.rows == 18 and p.page == 1 for p in seen)


def test_progress_names_the_posting_being_fetched(
    board_html: tuple[str, str], tmp_path: Path
) -> None:
    """숫자만 움직이는 것보다 무엇을 받고 있는지 보이는 게 낫다(멈춘 지점도 알 수 있다)."""
    seen: list[Progress] = []
    _collect(
        _Board(*board_html),
        JsonStore(tmp_path / "data"),
        CollectOptions(max_pages=1),
        on_progress=seen.append,
    )
    assert seen[0].latest is None  # 목록 단계에는 대상이 없다
    assert seen[1].latest is not None
    assert seen[1].latest.external_id == "25581"


def test_dry_run_reports_progress_for_the_one_sampled_detail(
    board_html: tuple[str, str], tmp_path: Path
) -> None:
    """`--dry-run`은 상세를 1건만 요청한다 — 나머지 17건까지 진행이 오르면 거짓 보고다."""
    seen: list[Progress] = []
    _collect(
        _Board(*board_html),
        JsonStore(tmp_path / "data"),
        CollectOptions(max_pages=1, dry_run=True),
        on_progress=seen.append,
    )
    assert max(p.details_done for p in seen) == 1


def test_collect_runs_without_a_progress_sink(board_html: tuple[str, str], tmp_path: Path) -> None:
    """알림은 선택이다 — 테스트·비대화형에서 콘솔 없이 돌아야 한다."""
    report = _collect(
        _Board(*board_html), JsonStore(tmp_path / "data"), CollectOptions(max_pages=1)
    )
    assert report.saved == 18


# ── 범위는 --months 가 정한다 ────────────────────────────────────


class _AgingBoard(_Board):
    """페이지가 깊어질수록 오래된 글을 주는 게시판 대역.

    `_Board`는 모든 페이지에 같은 목록을 줘서 "컷오프에 닿아 멈춘다"를 확인할 수 없다.
    여기선 페이지마다 번호를 바꾸고 게시일을 30일씩 밀어, **컷오프가 종료를 결정**하게 한다.
    """

    _DAYS_PER_PAGE: Final = 30

    def _page_of(self, url: httpx.URL) -> int:
        """2페이지 이상은 목록 URL 뒤에 `/page/N`이 붙는다(어댑터 `list_page_url`)."""
        found = re.search(r"/page/(\d+)", url.path)
        return int(found[1]) if found else 1

    def _aged(self, page: int) -> str:
        shift = timedelta(days=self._DAYS_PER_PAGE * (page - 1))
        offset = 1000 * (page - 1)
        html = re.sub(
            r"20\d\d-\d\d-\d\d",
            lambda m: str(date.fromisoformat(m.group()) - shift),
            self._list,
        )
        return re.sub(r"(trXXR/)(\d+)", lambda m: f"{m[1]}{int(m[2]) + offset}", html)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if "/board/view/" in request.url.path:
            return httpx.Response(200, text=self._detail)
        return httpx.Response(200, text=self._aged(self._page_of(request.url)))


def test_months_alone_decides_how_deep_to_page(board_html: tuple[str, str], tmp_path: Path) -> None:
    """⚠️ **`--months 3`이면 3개월치를 가져온다** — 페이지 수를 운영자가 계산하지 않는다.

    예전엔 페이지 상한 기본값이 3이라 이게 `--months`보다 먼저 걸렸다. 활발한 게시판에서
    `--months 3`을 줘도 4주치만 오고, 리포트가 "11페이지로 다시 하세요"라고 했다. 지금은
    **컷오프가 종료를 정하고 상한은 폭주 방지용**이며 CLI에 옵션조차 없다.

    대역: 페이지당 30일 · 게시일 07-31~08-04 → 4페이지에서 컷오프(05-04)를 걸치고 5페이지는
    전부 범위 밖 → 거기서 멈춘다. 옛 기본값(3p)이면 4·5페이지를 못 본다.
    """
    board = _AgingBoard(*board_html)
    report = _collect(
        board,
        JsonStore(tmp_path / "data"),
        CollectOptions(months=3),  # ← 페이지 상한을 주지 않는다(기본 = 안전 상한)
    )
    assert report.pages_read == 5
    assert not report.stopped_at_page_cap  # 컷오프에 닿아 스스로 멈췄다
    assert report.oldest is not None
    assert report.oldest >= cutoff_date(3, today=_TODAY)  # 범위 밖은 수집하지 않았다


def test_the_default_ceiling_does_not_bind_a_three_month_range() -> None:
    """기본 상한은 **범위를 정하는 값이 아니다** — 3개월을 못 채울 정도로 낮으면 안 된다."""
    assert CollectOptions().max_pages >= 100


def test_a_cutoff_without_any_list_dates_fails_loudly() -> None:
    """날짜가 없으면 컷오프가 아무 행도 자르지 못해 **안전 상한까지 걷는다**(조용한 폭주).

    목록에 날짜가 없는 게시판은 `--months 0`을 써야 한다 — 그렇게 말해 준다.
    """
    plan = plan_page((_ref("1", on=None), _ref("2", on=None)), {}, cutoff=_TODAY)
    assert plan.dated == 0
    with pytest.raises(ParseError, match="게시일이 없어"):
        _require_dates_for_cutoff("YTUS", plan, cutoff=_TODAY)


def test_a_page_with_some_dates_still_applies_the_cutoff() -> None:
    """일부 행에만 날짜가 없는 건 정상이다 — 그걸로 소스를 세우면 안 된다."""
    plan = plan_page((_ref("1", on=None), _ref("2", on=_TODAY)), {}, cutoff=_TODAY)
    _require_dates_for_cutoff("YTUS", plan, cutoff=_TODAY)


# ── 안전 상한 미달 보고 ──────────────────────────────────────────


def _capped_report(**overrides: object) -> CollectReport:
    base: dict[str, object] = {
        "source_key": "YTUS",
        "pages_read": 3,
        "rows": 58,
        "fresh": 58,
        "seen": 0,
        "stale": 0,
        "saved": 0,
        "shifted": 0,
        "oldest": date(2026, 7, 10),
        "newest": date(2026, 8, 4),
        "samples": (),
        "cutoff": date(2026, 5, 4),
        "max_pages": 3,
        "stopped_at_page_cap": True,
    }
    return CollectReport(**{**base, **overrides})  # type: ignore[arg-type]


def test_hitting_the_page_cap_before_the_cutoff_is_reported() -> None:
    """이걸 알려주지 않으면 "범위 밖 0"만 보고 3개월을 다 받은 줄 안다 — 조용한 미달이다."""
    assert _capped_report().short_of_cutoff


def test_reaching_the_cutoff_is_not_a_shortfall() -> None:
    assert not _capped_report(stopped_at_page_cap=False).short_of_cutoff


def test_no_cutoff_cannot_be_short() -> None:
    """`--months 0`은 범위를 날짜로 정하지 않으므로 미달이라는 개념이 없다."""
    assert not _capped_report(cutoff=None).short_of_cutoff


def test_the_loop_reports_the_cap(board_html: tuple[str, str], tmp_path: Path) -> None:
    """루프가 상한 도달을 실제로 기록하는지 — fixture는 컷오프 안이라 3p까지 간다."""
    board = _Board(*board_html)
    report = _collect(board, JsonStore(tmp_path / "data"), CollectOptions(max_pages=1))
    assert report.stopped_at_page_cap
    assert report.max_pages == 1


def test_the_loop_marks_a_natural_stop(board_html: tuple[str, str], tmp_path: Path) -> None:
    """컷오프 밖에서 멈추면 상한 도달이 아니다 — 요청 범위를 다 받았다는 뜻이다."""
    board = _Board(*board_html)
    report = _collect(
        board,
        JsonStore(tmp_path / "data"),
        CollectOptions(months=3, max_pages=5),
        today=date(2027, 1, 1),  # fixture 전체가 컷오프 밖
    )
    assert not report.stopped_at_page_cap
    assert report.stale == 18
