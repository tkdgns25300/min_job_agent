"""HTUS 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11).
첨부가 달린 상세 표본 `detail_file.html`은 `--url ...&w_id=24330`으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import htus
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "HTUS"
#: 실측: tr 16 = 헤더 1(td!) + 공지 5 + 공고 10.
_EXPECTED_POSTINGS: Final = 10
_NOTICE_TITLES: Final = (
    "청빙게시판 글쓰기시 저장이 안되는경우 참고하세요",
    "게시판 성격에 맞지 않는 글은 삭제됩니다.",
)

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="HTUS fixture 없음 — `minjob-ingest snapshot --source HTUS`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "HTUS")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return htus.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


def _row(
    *, no: str = "9227", ident: str = "24346", title: str = "가", file_cell: str = '""'
) -> str:
    """실측 칸: 번호 | 제목 | 글쓴이 | 날짜 | 첨부 | 조회. 날짜와 조회가 같은 클래스다."""
    return (
        f'<tr><td class="ltxt7">{no}</td>'
        f'<td class="left"><a href="/board/board.php?b_id=ministry_009&amp;page=1'
        f'&amp;w_id={ident}&amp;m=">{title}</a> <img alt="새글" src="icon_new.gif"/></td>'
        f'<td>윤형순</td><td class="ltxt6">2026-08-04</td>'
        f'<td class={file_cell}></td><td class="ltxt6">32</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<div class="bd_content"><table>{"".join(rows)}</table></div>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notices_are_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 5건과 헤더를 빼고 공고 10건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    titles = {ref.title for ref in refs}
    for notice in _NOTICE_TITLES:
        assert notice not in titles


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다."""
    first = refs[0]
    assert first.external_id == "24346"
    assert first.title == "군산동부교회에서 함께 사역하실 중고등부 준전임 사역자를 청빙합니다."
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == "https://ministry.htus.ac.kr/board/board.php?b_id=ministry_009&w_id=24346"
    assert first.list_meta["display_no"] == "9227"


def test_date_and_views_share_a_class(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 날짜·조회수가 둘 다 `td.ltxt6`다 — 셀렉터로 집으면 조회수를 날짜로 읽는다."""
    assert refs[0].list_meta["list_date"] == "2026-08-04"
    assert refs[0].list_meta["views"] == 32
    assert refs[0].list_meta["author"] == "윤형순"


def test_header_row_uses_td_not_th(source: SourceConfig) -> None:
    """⚠️ 헤더 행이 `th`가 아니라 `td`다 — `rows_with_data`류로는 안 걸러진다."""
    header = (
        "<tr><td>번호</td><td>제목</td><td>글쓴이</td><td>날짜</td><td>파일</td><td>조회</td></tr>"
    )
    notice = _row(no="공지", ident="1967")
    assert len(htus.parse_list(_list_html(header, notice, _row()), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        htus.parse_list(_list_html(header, notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        htus.parse_list(_list_html(_row().replace(">2026-08-04<", "><")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_view_content(refs: tuple[PostingRef, ...], detail_html: str) -> None:
    """본문은 `div.board_view_content`다(실측 330자 · 교단이 본문에 명시된다)."""
    raw = htus.parse_detail(detail_html, refs[0])
    assert "교단명 : 대한예수교장로회(통합)" in raw.raw_text
    assert "모집인원" in raw.raw_text
    assert raw.attachments == ()


def test_attachment_name_drops_the_size_suffix(source: SourceConfig) -> None:
    """⚠️ 링크 텍스트가 `이력서_양식(양동제일교회).hwp (33.50 KB)`다(실측 w_id=24330).

    크기를 안 떼면 `Attachment.is_image`가 확장자를 못 읽어 이미지 첨부를 텍스트로 취급한다.
    """
    page2 = _FIXTURES / "list_page2.html"
    detail = _FIXTURES / "detail_file.html"
    if not (page2.exists() and detail.exists()):
        pytest.skip("HTUS list_page2.html / detail_file.html 없음")
    with_file = next(
        ref
        for ref in htus.parse_list(page2.read_text(encoding="utf-8"), source)
        if ref.external_id == "24330"
    )
    assert with_file.list_meta["has_attachment"] is True
    raw = htus.parse_detail(detail.read_text(encoding="utf-8"), with_file)
    assert [attachment.name for attachment in raw.attachments] == ["이력서_양식(양동제일교회).hwp"]
    assert raw.attachments[0].url.endswith("/board/download.php?b_id=ministry_009&w_id=24330&fno=0")


def test_listed_attachment_without_a_link_is_an_error(
    refs: tuple[PostingRef, ...], detail_html: str
) -> None:
    """목록 첨부 칸(`td.file`)은 아이콘이 CSS라 **비어 있어도** 첨부다 — 신호를 버리지 않는다."""
    marked = PostingRef(
        external_id=refs[0].external_id,
        url=refs[0].url,
        title=refs[0].title,
        list_meta={"has_attachment": True},
    )
    with pytest.raises(ParseError, match="첨부 표시"):
        htus.parse_detail(detail_html, marked)
