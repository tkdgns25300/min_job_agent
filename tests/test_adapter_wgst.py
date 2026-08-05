"""WGST 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source WGST` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest
from bs4 import BeautifulSoup

from minjob_ingest.sources.adapters import wgst
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "WGST"
#: 실측: li.item 12개(페이지당 12건) · 고정공지 없음.
_EXPECTED_POSTINGS: Final = 12

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="WGST fixture 없음 — `minjob-ingest snapshot --source WGST`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "WGST")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def list_html() -> str:
    return (_FIXTURES / "list.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def refs(source: SourceConfig, list_html: str) -> tuple[PostingRef, ...]:
    return wgst.parse_list(list_html, source)


def _row(
    *,
    num: str = "1676",
    href: str = "boardview.asp?key=6131&pageno=1&rowNo=1676&seq=6910&MCD=",
    label: str = '<font color="#2366b5">[제자들교회]</font>',
    title: str = "가",
    date_text: str = "2026.07.28",
) -> str:
    """실측 마크업의 뼈대. 표가 아니라 `li` 리스트이고 날짜·조회수는 `dl.info` 안에 있다."""
    return (
        f'<li class="item"><span class="num">{num}</span><div class="content">'
        f'<strong class="subject"><a href="{href}" title="{title}">{label} {title}</a></strong>'
        f'<dl class="info"><dt>작성일</dt><dd>{date_text}</dd>'
        f'<dt class="fileTit">파일</dt><dd class="file"></dd>'
        f"<dt>조회수</dt><dd>33</dd></dl></div></li>"
    )


def _list_html(*rows: str) -> str:
    return f'<ul class="newsfeed_lst">{"".join(rows)}</ul>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_all_rows_are_postings(refs: tuple[PostingRef, ...]) -> None:
    """실측 1페이지는 12건 전부 공고다(고정공지 없음)."""
    assert len(refs) == _EXPECTED_POSTINGS


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다."""
    first = refs[0]
    assert first.external_id == "6910"
    assert first.title == "제자들교회(동탄)에서 중고등부 교역자를 모십니다."
    assert first.posted_on == date(2026, 7, 28)
    assert first.url == "http://www.wgst.ac.kr/wgst_renew/board/boardview.asp?key=6131&seq=6910"
    # 표시번호(rowNo)는 원장 키가 아니다 — 게시판이 다시 매기는 값이다.
    assert first.list_meta["display_no"] == "1676"
    assert first.list_meta["author"] == "제자들교회"


def test_church_label_is_split_off_from_the_title(
    refs: tuple[PostingRef, ...], list_html: str
) -> None:
    """⚠️ 제목 앞 `<font>[교회명]</font>`을 떼야 제목이 교회명을 두 번 담지 않는다.

    이 게시판은 `a[title]`에 순수 제목을 들고 있어 **독립 신호**로 대조할 수 있다 — 대괄호를
    정규식으로 자르는 구현은 `[청빙중]`·`[용인]`처럼 제목에 든 대괄호까지 먹어 여기서 걸린다.
    """
    subjects = [
        str(link.get("title"))
        for link in BeautifulSoup(list_html, "lxml").select(
            "ul.newsfeed_lst li.item strong.subject a"
        )
    ]
    assert [ref.title for ref in refs] == subjects


def test_seq_is_taken_by_name_not_by_url_prefix(source: SourceConfig) -> None:
    """⚠️ 목록 href의 파라미터 순서가 `detail_pattern`과 다르다 — 접두사 매칭은 실패한다."""
    reordered = _row(href="boardview.asp?seq=6910&key=6131&rowNo=1676")
    assert wgst.parse_list(_list_html(reordered), source)[0].external_id == "6910"
    with pytest.raises(ParseError, match="seq"):
        wgst.parse_list(_list_html(_row(href="boardview.asp?key=6131&rowNo=1676")), source)


def test_notice_rows_are_dropped(source: SourceConfig) -> None:
    """공지가 생기면 표시번호 칸에 숫자가 아닌 값이 온다 — 그 행은 제외한다."""
    kept = wgst.parse_list(_list_html(_row(num="공지"), _row(num="1675")), source)
    assert [ref.list_meta["display_no"] for ref in kept] == ["1675"]
    with pytest.raises(ParseError, match="전부 걸러짐"):
        wgst.parse_list(_list_html(_row(num="공지")), source)


def test_missing_date_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        wgst.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_posting_only(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `div.newsfeed_cnts`(실측 388자) — 교단이 본문에 명시된 양식형 공고다.

    ⚠️ 페이지 푸터에 사이트 공용 PDF(`2022_대학안전관리계획.pdf`)가 있다. 첨부·본문 범위를
    넓히면 그것이 공고의 증거로 저장된다.
    """
    raw = wgst.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "모집교회 : 제자들교회(대한예수교장로회 통합)" in raw.raw_text
    assert "사역시작일 : 2026년 9월부터(협의 가능)" in raw.raw_text
    assert "대학안전관리계획" not in raw.raw_text
    # 첨부 영역을 실측하지 못해 수집하지 않는다(어댑터 docstring) — 추측 셀렉터를 넣지 않았다.
    assert raw.attachments == ()
    assert raw.image_urls == ()
