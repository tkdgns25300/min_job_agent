"""어댑터 공용 도구(`base.py`) 자체 테스트.

⚠️ **여기 없으면 30곳이 공유하는 코드가 검증되지 않는다.** 어댑터 테스트를 통해 간접적으로
지나가긴 하지만, 그건 그 게시판이 쓰는 경로만 덮는다 — 실제로 변이 검증에서
`page == 1` 분기·구분자 선택·숫자 검증을 아무 테스트도 잡지 못했다(2026-08-05).

공용 함수는 한 곳을 고치면 30곳이 함께 움직이므로, 여기서 **경계와 실패**를 직접 고정한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Final

import pytest

from minjob_ingest.sources.adapters.base import (
    ParseError,
    PostingRef,
    external_id_from_query,
    page_query_request,
    require_attachment_evidence,
    require_date,
    require_numeric_id,
    require_some_kept,
)
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_PARAM: Final = "page"


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "YTUS")
    assert found is not None
    return found


# ── 쿼리 페이징 (24곳 공유) ──────────────────────────────────────


def test_first_page_is_the_bare_list_url(source: SourceConfig) -> None:
    """1페이지는 `list_url` 그대로다 — 파라미터를 붙이면 게시판마다 결과가 달라질 수 있다."""
    assert page_query_request(source, 1, param=_PARAM).url == source.list_url


def test_later_pages_append_the_parameter(source: SourceConfig) -> None:
    plain = replace(source, list_url="https://x.test/list")
    assert page_query_request(plain, 3, param=_PARAM).url == "https://x.test/list?page=3"


def test_an_existing_query_is_extended_with_an_ampersand() -> None:
    """⚠️ `?`를 또 붙이면 `...?a=1?page=2`가 되어 서버가 페이지를 무시한다.

    31곳 중 대부분의 `list_url`에 이미 쿼리가 있어서 이 분기가 기본 경로다.
    """
    found = find_source(load_sources(None), "DAESHIN")
    assert found is not None
    with_query = replace(found, list_url="https://x.test/list.php?board=5")
    assert page_query_request(with_query, 2, param="b_page").url == (
        "https://x.test/list.php?board=5&b_page=2"
    )


def test_always_include_puts_the_parameter_on_page_one() -> None:
    """기본 페이지가 무엇인지 서버 구현에 맡기지 않는 게시판이 있다(WGST·PUTS·PGAK·KAICAM)."""
    found = find_source(load_sources(None), "WGST")
    assert found is not None
    request = page_query_request(found, 1, param="pageno", always_include=True)
    assert request.url.endswith("pageno=1")


def test_page_zero_is_rejected(source: SourceConfig) -> None:
    with pytest.raises(ValueError, match="page"):
        page_query_request(source, 0, param=_PARAM)


def test_the_request_is_a_get(source: SourceConfig) -> None:
    """쿼리 페이징은 GET이다 — form이 붙으면 `collect`가 POST로 보낸다."""
    assert page_query_request(source, 2, param=_PARAM).form is None


# ── 쿼리 파라미터로 id 추출 (3곳 공유) ───────────────────────────


def test_the_id_is_found_by_name_not_by_position() -> None:
    """⚠️ 2페이지부터 상세 href에 페이지 파라미터가 끼어든다 — 순서에 의존하면 전 행이 깨진다.

    KOSIN_TH(`pg`)·MTU(`page`) 실측. 이름으로 찾으면 파라미터 순서·개수와 무관해진다.
    """
    shifted = "https://x.test/view?pCode=MN6&pg=2&mode=view&idx=304339"
    assert external_id_from_query(shifted, param="idx", source_key="T") == "304339"


def test_a_missing_parameter_is_an_error() -> None:
    with pytest.raises(ParseError, match="idx"):
        external_id_from_query("https://x.test/view?mode=view", param="idx", source_key="T")


def test_an_empty_parameter_is_an_error() -> None:
    with pytest.raises(ParseError, match="idx"):
        external_id_from_query("https://x.test/view?idx=", param="idx", source_key="T")


def test_a_non_numeric_id_is_rejected_by_default() -> None:
    """숫자가 아니면 링크 형태가 바뀐 것이다 — 조용히 통과시키면 엉뚱한 값이 원장 키가 된다."""
    with pytest.raises(ParseError, match="숫자가 아님"):
        external_id_from_query("https://x.test/view?idx=abc", param="idx", source_key="T")


def test_non_numeric_ids_are_allowed_when_asked() -> None:
    """32자리 hex(MOKWON)·복합키처럼 숫자가 아닌 게시판이 있다."""
    ident = "501103573814a8ef882b3f885d1fb33b"
    assert (
        external_id_from_query(
            f"https://x.test/v?no={ident}", param="no", source_key="T", numeric=False
        )
        == ident
    )


def test_require_numeric_id_rejects_mixed_text() -> None:
    with pytest.raises(ParseError, match="숫자가 아님"):
        require_numeric_id("12a", source_key="T")


# ── 첨부 교차확인 (11곳 공유) ────────────────────────────────────


def _ref(*, marked: bool) -> PostingRef:
    return PostingRef(
        external_id="1",
        url="https://x.test/1",
        title="가",
        list_meta={"has_attachment": marked},
    )


def test_a_marked_posting_with_nothing_found_is_an_error() -> None:
    """⚠️ 이 대조가 없으면 첨부 셀렉터가 빗나가도 본문 있는 공고는 그냥 통과한다."""
    with pytest.raises(ParseError, match="첨부 표시가 있는데"):
        require_attachment_evidence(
            _ref(marked=True), source_key="T", selector="div.file", found=()
        )


def test_a_marked_posting_with_something_found_passes() -> None:
    require_attachment_evidence(
        _ref(marked=True), source_key="T", selector="div.file", found=("파일",)
    )


def test_an_unmarked_posting_with_nothing_found_passes() -> None:
    """첨부 없는 공고가 대다수다 — 표시가 없으면 아무것도 못 찾은 게 정상이다."""
    require_attachment_evidence(_ref(marked=False), source_key="T", selector="div.file", found=())


# ── 그 밖의 공용 규칙 ────────────────────────────────────────────


@pytest.mark.parametrize("text", ["2026-08-05", "2026.08.05", "2026/08/05"])
def test_date_separators_are_interchangeable(text: str) -> None:
    """구분자는 게시판마다 갈린다 — 30곳이 각자 파서를 들 이유가 없다."""
    assert require_date(text, source_key="T", cell="td.date") == date(2026, 8, 5)


def test_an_empty_date_cell_is_an_error() -> None:
    with pytest.raises(ParseError, match="게시일 칸"):
        require_date("   ", source_key="T", cell="td.date")


def test_an_unparseable_date_is_an_error() -> None:
    with pytest.raises(ParseError, match="게시일 형식"):
        require_date("2026년 8월 5일", source_key="T", cell="td.date")


def test_all_rows_filtered_is_an_error() -> None:
    with pytest.raises(ParseError, match="전부 걸러짐"):
        require_some_kept([], [object()], source_key="T", filtered_by="공지 판정")


def test_no_rows_at_all_is_not_an_error() -> None:
    """마지막 페이지는 정상적으로 비어 있다 — 페이징이 그걸 필요로 한다."""
    require_some_kept([], [], source_key="T", filtered_by="공지 판정")
