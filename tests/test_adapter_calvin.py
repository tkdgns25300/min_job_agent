"""CALVIN 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source CALVIN` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import calvin
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "CALVIN"
#: 실측: tr 11 = 헤더 1 + 공지 1 + 공고 9.
_EXPECTED_POSTINGS: Final = 9
_NOTICE_TITLE: Final = "사역자(목사, 전도사, 강도사)모집 게시요청 방법"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="CALVIN fixture 없음 — `minjob-ingest snapshot --source CALVIN`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "CALVIN")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return calvin.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(*, no: str = "4625", href: str = "javascript:fView('80031731')", cls: str = "") -> str:
    return (
        f'<tr{cls}><td class="one">{no}</td>'
        f'<td class="two left"><a href="{href}">[부성교회] 사역자 모집</a></td>'
        f'<td class="three">취창업지원센터</td><td class="four">2026.07.30</td>'
        f'<td class="five">24</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="tbl_basic_list">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """고정공지 1건은 제외하고 공고 9건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·정규 URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "80031731"
    assert first.title == "[부성교회] 사역자 모집"
    assert first.posted_on == date(2026, 7, 30)
    assert first.url == (
        "http://calvin.ac.kr/main/boardView.do?brd_mgrno=692&menu_no=2282&brd_no=80031731"
    )


def test_display_number_is_not_the_external_id(refs: tuple[PostingRef, ...]) -> None:
    """표시번호(4625)와 원장 키(brd_no 80031731)는 다르다 — 표시번호는 게시판이 다시 매긴다."""
    assert refs[0].list_meta["display_no"] == "4625"
    assert refs[0].external_id != refs[0].list_meta["display_no"]


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_href_without_the_js_call_is_rejected(source: SourceConfig) -> None:
    """⚠️ 이 게시판의 상세 링크는 **href에 URL이 없다** — `javascript:fView('id')`뿐이다.

    href를 URL로 믿는 코드는 `javascript:…`를 조용히 절대화해 쓰레기 id를 만든다.
    """
    with pytest.raises(ParseError, match="글번호를 못 찾음"):
        calvin.parse_list(_list_html(_row(href="/main/boardView.do?brd_no=80031731")), source)


def test_notice_row_class_and_non_numeric_number_both_filter(source: SourceConfig) -> None:
    """공지는 `tr.notice`이고 표시번호 칸에 "공지"가 온다 — 두 신호를 독립적으로 본다."""
    for notice in (_row(cls=' class="notice"'), _row(no="공지")):
        with pytest.raises(ParseError, match="전부 걸러짐"):
            calvin.parse_list(_list_html(notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        calvin.parse_list(_list_html(_row().replace(">2026.07.30<", "><")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_an_inline_base64_image(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 본문이 통째로 이미지다 — 실측 2건 모두 텍스트 0자 + `data:image/png;base64,…` 1개.

    빈 `raw_text`를 실패로 보면 이 게시판은 전량 탈락한다. 이미지가 유일한 증거다.
    """
    raw = calvin.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert raw.raw_text == ""
    assert len(raw.image_urls) == 1
    assert raw.image_urls[0].startswith("data:image/png;base64,")


def test_prev_next_links_are_not_attachments(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 상세 하단에 이전글/다음글 링크가 있다 — 첨부 범위를 본문 밖으로 넓히면 그게 첨부가 된다."""
    raw = calvin.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert raw.attachments == ()
