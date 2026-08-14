"""PCKWORLD 어댑터 — 공고가 포스터 이미지 한 장인 게시판.

구조적 검사는 `test_adapter_conformance.py`가 한다. 여기엔 실측값과 이 게시판만의 함정만 둔다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import pckworld
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "PCKWORLD"
#: 실측: 그리드가 페이지당 12건.
_PER_PAGE: Final = 12

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(), reason="PCKWORLD fixture 없음"
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "PCKWORLD")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return pckworld.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def test_postings_are_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    assert len(refs) == _PER_PAGE
    first = refs[0]
    assert first.external_id == "1551"
    assert first.title == "담임목사청빙(평강교회)"
    assert first.url == "https://pckworld.com/adsearch/ad_view.php?aid=1551"


def test_the_id_comes_from_a_javascript_call(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 링크가 `javascript:adview('1551',1761,822)`다 — href를 그대로 읽으면 id가 없다."""
    assert all(ref.external_id.isdigit() for ref in refs)


def test_a_link_without_the_adview_call_is_an_error(source: SourceConfig) -> None:
    html = (
        '<ul class="grid"><li class="grid-item"><a href="/somewhere"><span>가</span></a></li></ul>'
    )
    with pytest.raises(ParseError, match="글번호를 못 찾음"):
        pckworld.parse_list(html, source)


def test_the_date_comes_from_the_thumbnail_filename(
    refs: tuple[PostingRef, ...], source: SourceConfig
) -> None:
    """⚠️ 목록에 날짜 칸이 없다 — 썸네일 파일명(`20260729171107.jpg`)이 유일한 근거다.

    ⚠️ **그래도 `list_has_dates`는 `false`다.** 파일명 관례는 조용히 바뀔 수 있어서 이 값이
    백필 컷오프를 움직이면 공고가 조용히 잘린다. 컷오프는 페이지 상한이 정하고, 여기서 읽은
    날짜는 `posted_at`으로만 쓴다(SPEC §9 자동 만료).
    """
    assert source.list_has_dates is False
    assert refs[0].posted_on == date(2026, 7, 29)
    assert refs[0].list_meta["thumbnail"] == "/upimg/adsearch/20260729171107.jpg"
    assert all(ref.posted_on is not None for ref in refs)


@pytest.mark.parametrize(
    "source_path",
    [None, "/upimg/adsearch/thumb.jpg", "/upimg/adsearch/20261332171107.jpg", "/upimg/x/1234.jpg"],
    ids=["없음", "날짜아님", "없는날짜", "짧은숫자"],
)
def test_an_unreadable_filename_gives_no_date(source_path: str | None) -> None:
    """파일명 관례가 바뀌면 **지어내지 않고** 비운다 — 부르는 쪽이 오늘로 둔다."""
    assert pckworld.uploaded_on(source_path) is None


def test_detail_is_a_poster_image_with_no_text(refs: tuple[PostingRef, ...]) -> None:
    """빈 `raw_text`가 정상이다(config `image_only`) — 내용은 이미지에만 있다."""
    raw = pckworld.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert raw.raw_text == ""
    assert raw.image_urls == ("https://pckworld.com/upimg/adsearch/20260729171107.jpg",)


def test_a_detail_without_an_image_is_an_error(refs: tuple[PostingRef, ...]) -> None:
    """텍스트도 이미지도 없으면 증거가 하나도 없는 레코드가 된다."""
    with pytest.raises(ParseError, match="포스터 이미지가 없음"):
        pckworld.parse_detail("<html><body><a>창닫기</a></body></html>", refs[0])


def test_an_icon_before_the_poster_does_not_become_the_date(source: SourceConfig) -> None:
    """⚠️ 항목 안 **첫 `img` 아무거나**를 잡으면 게시판이 아이콘·뱃지를 넣는 순간 날짜가
    엉뚱한 파일에서 나온다 — 숫자 14자리를 우연히 담으면 **조용히 틀린 날짜**가 된다.

    포스터는 경로로 알아본다(상세 파서가 쓰는 것과 같은 셀렉터).
    """
    html = (
        "<ul class='grid'><li class='grid-item'>"
        "<img src='/img/icon/new_20991231235959.gif'/>"
        "<a href=\"javascript:adview('1545',800,600)\">"
        "<img src='/upimg/adsearch/20260729171107.jpg'/><span>담임목사청빙</span></a>"
        "</li></ul>"
    )

    refs = pckworld.parse_list(html, source)

    assert refs[0].posted_on == date(2026, 7, 29)
    assert refs[0].list_meta["thumbnail"] == "/upimg/adsearch/20260729171107.jpg"
