"""원문 소멸 판정(`pipeline/gone`) — 네트워크는 MockTransport로 흉내낸다(진짜 요청 없음).

실측(2026-08-30)에서 겪은 함정이 곧 테스트다: 목록에서만 내려간 글(부산장신·침신대),
게시판 개편이 삭제로 보이는 것, 끌어올림 게시판의 이른 중단(고신대 63건 오판),
포스터 게시판의 빈 본문(칼빈대).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from minjob_ingest.domain import (
    Confidence,
    DenominationSource,
    IsChurchRecruitment,
    JobKind,
    Position,
    Region,
)
from minjob_ingest.fetch.client import SourceClient
from minjob_ingest.models import ReviewData, SourceData
from minjob_ingest.pipeline.dedup import dedup_all
from minjob_ingest.pipeline.gone import (
    Verdict,
    is_bulk_suspicious,
    min_pages,
    missing_from_listing,
    run_gone,
    sweep_source,
    verdict_of_detail,
    window_start,
)
from minjob_ingest.sources.adapters import csu
from minjob_ingest.sources.adapters.base import ListRequest, ParseError, PostingRef, RawPosting
from minjob_ingest.sources.adapters.registry import GoneProber
from minjob_ingest.sources.registry import SourceConfig, load_sources
from minjob_ingest.store.base import GoneTarget, JobAnchor
from minjob_ingest.store.json_store import JsonStore

_TODAY = date(2026, 9, 1)
_INSIDE = date(2026, 8, 20)  # 창(2개월) 안
_OUTSIDE = date(2026, 5, 1)  # 창 밖

#: fetch 층의 본문 길이 하한(200자)을 넘기는 패딩 — 목록·상세 응답 뒤에 붙인다.
_PAD = "\n<!-- " + "x" * 300 + " -->"


def _source() -> SourceConfig:
    return next(s for s in load_sources() if s.key == "YTUS")


def _target(external_id: str, *, published: bool = True) -> GoneTarget:
    return GoneTarget(
        review_data_id=uuid4(),
        published_job_id=uuid4() if published else None,
        source_key="YTUS",
        external_id=external_id,
        source_url=f"https://www.ytus.ac.kr/post/{external_id}",
        title="청빙 공고",
        posted_on=_INSIDE,
    )


def _raw(ref: PostingRef, *, text: str = "", images: tuple[str, ...] = ()) -> RawPosting:
    return RawPosting(ref=ref, raw_text=text, image_urls=images)


class _Board:
    """테스트 게시판. 페이지별 행과 글별 상세를 지정한다 — 어댑터 프로토콜을 만족한다.

    목록 응답 본문에 페이지 번호를 실어 보내고 `parse_list`가 그것을 읽는다. 상세는
    URL 끝의 번호로 찾는다. `details`에 없는 글은 개편된 게시판처럼 `ParseError`를 낸다.
    """

    NEEDS_DETAIL_REQUEST = True

    def __init__(
        self,
        pages: dict[int, list[tuple[str, date | None]]],
        details: dict[str, RawPosting | None] | None = None,
    ) -> None:
        self._pages = pages
        self._details = details or {}

    def list_request(self, source: SourceConfig, page: int) -> ListRequest:
        return ListRequest(url=f"{source.list_url}/list?page={page}")

    def parse_list(self, html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
        page = int(html.split("\n", 1)[0])
        return tuple(
            PostingRef(
                external_id=external_id,
                url=f"{source.list_url}/post/{external_id}",
                title="목록 행",
                posted_on=posted_on,
                list_meta={},
            )
            for external_id, posted_on in self._pages.get(page, [])
        )

    def parse_detail(self, html: str, ref: PostingRef) -> RawPosting:
        # 진짜 어댑터처럼 **응답 본문**을 읽는다 — 클라이언트가 첫 줄에 글 번호를 실어 준다.
        found = self._details.get(html.split("\n", 1)[0])
        if found is None:
            raise ParseError(f"셀렉터가 아무것도 찾지 못함 ({ref.external_id})")
        return found


def _client(source: SourceConfig, *, dead: frozenset[str] = frozenset()) -> SourceClient:
    """목록·상세에 항상 200을 주는 클라이언트. `dead`에 든 글의 상세만 404다."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404, text="")
        if "page" in request.url.params:  # 목록 — 본문 첫 줄에 페이지 번호를 실어 준다
            return httpx.Response(200, text=f"{request.url.params['page']}{_PAD}")
        external_id = path.rsplit("/", 1)[-1]
        if external_id in dead:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=f"{external_id}{_PAD}")

    return SourceClient(source, transport=httpx.MockTransport(handler), sleep=lambda _: None)


def _alive_detail(external_id: str) -> RawPosting:
    ref = PostingRef(
        external_id=external_id,
        url=f"https://www.ytus.ac.kr/post/{external_id}",
        title="목록 행",
        posted_on=_INSIDE,
        list_meta={},
    )
    return _raw(ref, text="살아있는 본문 " * 10)


# ── 순수 판정 ────────────────────────────────────────────────────


def test_the_window_is_two_calendar_months() -> None:
    assert window_start(date(2026, 9, 1)) == date(2026, 7, 1)


def test_min_pages_is_the_ledger_count_over_the_page_size() -> None:
    assert min_pages(77, 15) == 6  # 고신대 실측 — 1페이지에서 멈추면 63건 오판


def test_min_pages_survives_an_empty_page() -> None:
    assert min_pages(10, 0) == 1


def test_targets_absent_from_the_listing_become_candidates() -> None:
    kept, gone = _target("1"), _target("2")
    assert missing_from_listing([kept, gone], frozenset({"1"})) == (gone,)


def test_a_few_candidates_are_never_bulk_suspicious() -> None:
    # 대상 4건 중 3건(75%)이라도 소량이면 판정한다 — 작은 게시판의 진짜 삭제를 막지 않는다.
    assert not is_bulk_suspicious(3, 4)


def test_many_candidates_trip_the_bulk_guard() -> None:
    assert is_bulk_suspicious(40, 100)  # 40% — 장애·개편 의심


def test_an_empty_detail_is_gone_but_a_poster_is_alive() -> None:
    ref = PostingRef(
        external_id="1", url="https://x.test/1", title="t", posted_on=None, list_meta={}
    )
    assert verdict_of_detail(_raw(ref)) is Verdict.GONE
    # 칼빈대 실측 — 본문 0자여도 포스터가 있으면 살아 있다.
    assert verdict_of_detail(_raw(ref, images=("https://x.test/p.jpg",))) is Verdict.ALIVE


# ── sweep: 게시판 하나 ───────────────────────────────────────────


def test_no_targets_is_a_skip_without_any_request() -> None:
    report = sweep_source(_source(), _Board({}), _client(_source()), [], today=_TODAY)
    assert report.skipped == "판정 대상 없음"
    assert report.pages == 0


def test_a_board_without_detail_access_is_never_judged() -> None:
    """HANIL 실측 — 본문이 목록에 실려 와 상세가 없다. 2차 확인이 없으면 판정도 없다."""

    class _NoDetail(_Board):
        NEEDS_DETAIL_REQUEST = False

    board = _NoDetail({1: [("1", _INSIDE)]})
    report = sweep_source(_source(), board, _client(_source()), [_target("9")], today=_TODAY)
    assert report.skipped is not None and "2차 확인" in report.skipped
    assert report.gone == ()


def test_everything_listed_means_nothing_to_probe() -> None:
    board = _Board({1: [("1", _INSIDE), ("2", _OUTSIDE)]})
    report = sweep_source(
        _source(), board, _client(_source()), [_target("1"), _target("2")], today=_TODAY
    )
    assert report.skipped is None
    assert (report.gone, report.alive, report.unknown) == ((), (), ())


def test_a_candidate_whose_detail_is_broken_is_gone() -> None:
    """장신대 실측 — 삭제된 글은 본문 셀렉터가 아무것도 찾지 못한다."""
    board = _Board({1: [("1", _INSIDE)]}, details={"1": _alive_detail("1")})
    target = _target("9")
    report = sweep_source(_source(), board, _client(_source()), [target], today=_TODAY)
    assert report.gone == (target,)


def test_a_candidate_that_still_opens_is_alive_not_gone() -> None:
    """부산장신·침신대 실측 — 목록에서만 내려간 글. 내리면 안 된다."""
    board = _Board(
        {1: [("1", _INSIDE)]}, details={"1": _alive_detail("1"), "9": _alive_detail("9")}
    )
    target = _target("9")
    report = sweep_source(_source(), board, _client(_source()), [target], today=_TODAY)
    assert report.alive == (target,)
    assert report.gone == ()


def test_a_dead_control_skips_the_whole_board() -> None:
    """게시판이 개편되면 살아있는 글(대조군)도 실패한다 — 그날은 아무것도 내리지 않는다."""
    board = _Board({1: [("1", _INSIDE)]}, details={})  # 상세가 전부 ParseError
    report = sweep_source(_source(), board, _client(_source()), [_target("9")], today=_TODAY)
    assert report.skipped is not None and "대조군" in report.skipped
    assert report.gone == ()


def test_a_mass_disappearance_is_held_not_judged() -> None:
    targets = [_target(str(n)) for n in range(10)]
    board = _Board({1: [("100", _INSIDE)]}, details={"100": _alive_detail("100")})
    report = sweep_source(_source(), board, _client(_source()), targets, today=_TODAY)
    assert report.skipped is not None and "게시판 장애" in report.skipped


def test_the_scan_reads_past_bumped_old_rows_until_the_floor() -> None:
    """고신대 실측 — 끌어올림으로 1페이지가 전부 옛 날짜여도 원장 건수만큼은 읽는다."""
    board = _Board(
        {1: [("a", _OUTSIDE)], 2: [("b", _INSIDE), ("9", _INSIDE)]},
        details={"a": _alive_detail("a"), "b": _alive_detail("b")},
    )
    targets = [_target("9"), _target("b")]  # floor = ceil(2/1) = 2페이지
    report = sweep_source(_source(), board, _client(_source()), targets, today=_TODAY)
    # 1페이지(전부 컷오프 밖)에서 멈추지 않고, 2페이지가 창 안이라 3페이지(빈 목록)까지 간다.
    assert report.pages == 3
    assert report.gone == ()  # 둘 다 목록에 있었다 — 오판 없음


def test_a_repeating_last_page_ends_the_scan() -> None:
    """범위 밖 페이지에 마지막 페이지를 되돌려주는 게시판 — 같은 번호만 나오면 끝이다."""
    board = _Board(
        {page: [("1", _INSIDE)] for page in range(1, 50)}, details={"1": _alive_detail("1")}
    )
    report = sweep_source(_source(), board, _client(_source()), [_target("1")], today=_TODAY)
    assert report.pages == 2  # 1페이지 + 반복 확인 1페이지


# ── 전용 신호(CSU) ───────────────────────────────────────────────


def test_the_csu_probe_reads_the_deleted_code() -> None:
    assert csu.parse_gone(json.dumps({"code": 42004, "message": "삭제된 게시물입니다."})) is True
    assert csu.parse_gone(json.dumps({"code": 10000, "body": {}})) is False


def test_an_unknown_csu_answer_never_takes_a_posting_down() -> None:
    assert csu.parse_gone("<html>점검 중</html>") is None
    assert csu.parse_gone(json.dumps({"code": 22000})) is None
    assert csu.parse_gone(json.dumps(["not", "a", "dict"])) is None


def test_the_csu_adapter_declares_the_prober_pair() -> None:
    prober = GoneProber.of(csu)
    assert prober is not None
    request = prober.request(next(s for s in load_sources() if s.key == "CSU"), "1118421")
    assert request.form == {"id": "1118421"}
    assert request.url.endswith("/api/board/getBoardContent")


def test_a_plain_adapter_has_no_prober() -> None:
    assert GoneProber.of(_Board({})) is None


def test_half_a_prober_pair_fails_loudly() -> None:
    class _Half(_Board):
        @staticmethod
        def gone_request(source: SourceConfig, external_id: str) -> ListRequest:
            return ListRequest(url=f"{source.list_url}/content/{external_id}")

    with pytest.raises(TypeError, match="짝"):
        GoneProber.of(_Half({}))


# ── 실행 조립: 판정 → 기록 → 내리기 ──────────────────────────────


class _Jobs:
    """`PublishTarget`의 소멸 감지 부분만 실제로 움직이는 가짜 — 나머지는 부르면 버그다."""

    def __init__(self, *, expired: tuple[UUID, ...] = ()) -> None:
        self.closed: list[UUID] = []
        self._expired = expired

    def check_jobs_columns(self) -> None:
        raise AssertionError("gone 경로가 부를 일이 없다")

    def visible_anchors(
        self, *, today: date, exclude: frozenset[UUID] = frozenset()
    ) -> tuple[JobAnchor, ...]:
        raise AssertionError(f"gone 경로가 부를 일이 없다 ({today}, {exclude})")

    def reserve_publication(self, review_data_id: UUID) -> UUID:
        raise AssertionError(f"gone 경로가 부를 일이 없다 ({review_data_id})")

    def publish(self, draft: ReviewData, *, job_id: UUID, posted_at: date) -> None:
        raise AssertionError(f"gone 경로가 부를 일이 없다 ({draft.id}, {job_id}, {posted_at})")

    def bump_posted_at(self, job_id: UUID, posted_at: date) -> bool:
        raise AssertionError(f"gone 경로가 부를 일이 없다 ({job_id}, {posted_at})")

    def expired_job_ids(self, *, today: date) -> tuple[UUID, ...]:
        assert today == _TODAY
        return self._expired

    def close_job(self, job_id: UUID) -> bool:
        self.closed.append(job_id)
        return True

    def published_state(self, job_ids: Sequence[UUID]) -> Mapping[UUID, date]:
        raise AssertionError(f"gone 경로가 부를 일이 없다 ({list(job_ids)})")

    def count_jobs(self) -> int:
        raise AssertionError("gone 경로가 부를 일이 없다")

    def release_publication(self, review_data_id: UUID, job_id: UUID) -> None:
        raise AssertionError(f"gone 경로가 부를 일이 없다 ({review_data_id}, {job_id})")


def _seeded_store(tmp_path: Path, *, external_id: str, published: UUID | None) -> JsonStore:
    """원자료 + 초안 한 쌍을 넣은 로컬 저장소. 게시일은 창 안이다."""
    store = JsonStore(tmp_path / "data")
    origin = SourceData(
        source_key="YTUS",
        external_id=external_id,
        source_url=f"https://www.ytus.ac.kr/post/{external_id}",
        title=f"공고 {external_id}",
        posted_on=_INSIDE,
        run_id=uuid4(),
        fetched_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        raw_text="본문",
    )
    store.save_source_data(origin)
    done = origin.with_verdict_recorded()
    store.update_structure_state(done)
    store.upsert_review_data(
        ReviewData(
            posted_at=_INSIDE,
            source_url=origin.source_url,
            source_data_id=done.id,
            run_id=uuid4(),
            is_church_recruitment=IsChurchRecruitment.YES,
            confidence=Confidence.HIGH,
            denomination_source=DenominationSource.UNKNOWN,
            published_job_id=published,
        )
    )
    return store


def test_run_gone_marks_and_closes_a_confirmed_deletion(tmp_path: Path) -> None:
    job_id = uuid4()
    store = _seeded_store(tmp_path, external_id="9", published=job_id)
    jobs = _Jobs()
    board = _Board({1: [("1", _INSIDE)]}, details={"1": _alive_detail("1")})  # 9는 목록·상세에 없다

    report = run_gone(
        store,
        jobs,
        [_source()],
        today=_TODAY,
        open_client=lambda source: _client(source),
        adapter_of={"YTUS": board}.__getitem__,
    )

    assert (report.marked, report.closed) == (1, 1)
    assert jobs.closed == [job_id]
    assert store.gone_targets(since=_INSIDE) == ()  # 관측된 행은 대상에서 빠졌다


def test_a_dry_run_probes_but_never_writes(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path, external_id="9", published=uuid4())
    jobs = _Jobs()
    board = _Board({1: [("1", _INSIDE)]}, details={"1": _alive_detail("1")})

    report = run_gone(
        store,
        jobs,
        [_source()],
        today=_TODAY,
        dry_run=True,
        open_client=lambda source: _client(source),
        adapter_of={"YTUS": board}.__getitem__,
    )

    assert report.gone_total == 1  # 확인은 했다
    assert (report.marked, report.closed, report.expired_closed) == (0, 0, 0)
    assert jobs.closed == []


def test_a_board_failure_is_isolated_not_fatal(tmp_path: Path) -> None:
    """게시판이 통째로 죽어도 실행은 끝난다 — 삭제는 사라지지 않으니 내일 또 잡는다."""
    store = _seeded_store(tmp_path, external_id="9", published=uuid4())

    def broken_client(source: SourceConfig) -> SourceClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404, text="")
            return httpx.Response(500, text="boom")

        return SourceClient(source, transport=httpx.MockTransport(handler), sleep=lambda _: None)

    report = run_gone(
        store,
        _Jobs(),
        [_source()],
        today=_TODAY,
        open_client=broken_client,
        adapter_of={"YTUS": _Board({})}.__getitem__,
    )

    assert "YTUS" in report.failures
    assert report.marked == 0


def test_a_disabled_board_is_reported_not_probed(tmp_path: Path) -> None:
    """대신대 실측 — 러너 IP가 막혀 수집이 꺼진 곳. 확인 요청 자체를 보내지 않는다."""
    store = _seeded_store(tmp_path, external_id="9", published=uuid4())
    disabled = replace(_source(), enabled=False)

    report = run_gone(
        store,
        _Jobs(),
        [disabled],
        today=_TODAY,
        open_client=lambda source: _client(source),
        adapter_of={"YTUS": _Board({})}.__getitem__,
    )

    assert "비활성" in report.failures["YTUS"]


def test_expired_cleanup_only_touches_our_own_jobs(tmp_path: Path) -> None:
    """마감 정리는 `published_job_ids`(우리 것 판별의 정본)와의 교집합만 닫는다(SPEC §8)."""
    ours = uuid4()
    theirs = uuid4()
    store = _seeded_store(tmp_path, external_id="1", published=ours)
    jobs = _Jobs(expired=(ours, theirs))
    board = _Board({1: [("1", _INSIDE)]}, details={"1": _alive_detail("1")})  # 삭제 없음

    report = run_gone(
        store,
        jobs,
        [_source()],
        today=_TODAY,
        open_client=lambda source: _client(source),
        adapter_of={"YTUS": board}.__getitem__,
    )

    assert report.expired_closed == 1
    assert jobs.closed == [ours]  # min_job이 만든 행(theirs)은 건드리지 않았다


def test_a_gone_row_counts_as_settled_not_as_human_work(tmp_path: Path) -> None:
    """사라진 행이 dedup 리포트의 "판단 못 함(사람이 본다)"에 세어지면 안 된다 —
    삭제가 쌓일수록 그 숫자가 부풀어 검수자가 헛짚는다."""
    store = JsonStore(tmp_path / "data")
    origin = SourceData(
        source_key="YTUS",
        external_id="1",
        source_url="https://www.ytus.ac.kr/post/1",
        title="공고 1",
        posted_on=_INSIDE,
        run_id=uuid4(),
        fetched_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        raw_text="본문",
    )
    store.save_source_data(origin)
    done = origin.with_verdict_recorded()
    store.update_structure_state(done)
    draft = ReviewData(
        posted_at=_INSIDE,
        source_url=origin.source_url,
        source_data_id=done.id,
        run_id=uuid4(),
        is_church_recruitment=IsChurchRecruitment.YES,
        confidence=Confidence.HIGH,
        denomination_source=DenominationSource.UNKNOWN,
        church_name="오천중앙교회",
        region=Region.GYEONGBUK,
        job_kind=(JobKind.MINISTRY,),
        position=(Position.ASSOCIATE_PASTOR,),
        published_job_id=uuid4(),
    )
    store.upsert_review_data(draft)
    store.mark_gone([draft.id], at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC))

    report = dedup_all(store, None, dry_run=True)

    assert (report.settled, report.unjudged) == (1, 0)


def test_an_expired_row_counts_as_settled_not_as_human_work(tmp_path: Path) -> None:
    """마감이 지난 행도 다툼에서 빠진다(SPEC §4.1 · 2026-09-02) — 소멸 행과 같이 "이미 결론"에
    세어야 한다. 사람 몫에 세면 마감이 쌓일수록 숫자가 부풀어 검수자가 헛짚는다."""
    store = JsonStore(tmp_path / "data")
    origin = SourceData(
        source_key="YTUS",
        external_id="1",
        source_url="https://www.ytus.ac.kr/post/1",
        title="공고 1",
        posted_on=_INSIDE,
        run_id=uuid4(),
        fetched_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        raw_text="본문",
    )
    store.save_source_data(origin)
    done = origin.with_verdict_recorded()
    store.update_structure_state(done)
    draft = ReviewData(
        posted_at=_INSIDE,
        source_url=origin.source_url,
        source_data_id=done.id,
        run_id=uuid4(),
        is_church_recruitment=IsChurchRecruitment.YES,
        confidence=Confidence.HIGH,
        denomination_source=DenominationSource.UNKNOWN,
        church_name="오천중앙교회",
        region=Region.GYEONGBUK,
        job_kind=(JobKind.MINISTRY,),
        position=(Position.ASSOCIATE_PASTOR,),
        published_job_id=uuid4(),
        deadline=date(2026, 1, 1),  # 오래전에 지났다 — 벽시계가 어디 있어도 판정이 같다
    )
    store.upsert_review_data(draft)

    report = dedup_all(store, None, dry_run=True)

    assert (report.settled, report.unjudged) == (1, 0)
