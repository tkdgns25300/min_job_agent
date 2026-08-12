"""PCK 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source PCK` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import pck
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "PCK"
#: 실측: 제목 칸을 가진 tr 18 = 공지 2 + 공고 16(표에는 1px 구분선 tr이 23개 더 섞여 있다).
_EXPECTED_POSTINGS: Final = 16
_NOTICE_TITLES: Final = ("청빙 게시판 이용안내", "사기 꾼들을 조심하세요")

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="PCK fixture 없음 — `minjob-ingest snapshot --source PCK`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "PCK")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return pck.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    no: str = '<span class="mw_basic_list_num_new">1414</span>',
    ident: str = "5525",
    title: str = "가",
    posted: str = "2026-08-04",
    deadline: str = "",
) -> str:
    return (
        f'<tr><td class="media-no-text">{no}</td>'
        '<td class="mw_basic_list_hit media-no-text red">모집 중</td>'
        '<td class="mw_basic_list_subject">'
        f'<a href="/bbs/board.php?bo_table=SM05_05&amp;wr_id={ident}">{title}</a></td>'
        '<td class="mw_basic_list_name">홍길동</td>'
        f'<td class="mw_basic_list_datetime">{posted}</td>'
        f'<td class="mw_basic_list_hit">{deadline}</td>'
        '<td class="mw_basic_list_hit">7</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<form id="fboardlist"><table>{"".join(rows)}</table></form>'


#: 공지는 번호 대신 아이콘 이미지가 들어간다(실측) — 이 스킨엔 `tr.bo_notice`가 없다.
_NOTICE_ROW: Final = _row(no='<img src="../skin/board/calling/img/icon_notice.gif"/>')


# ── 목록 ─────────────────────────────────────────────────────────


def test_notices_are_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 2건은 제외하고 공고 16건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    titles = {ref.title for ref in refs}
    for notice in _NOTICE_TITLES:
        assert notice not in titles


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "5525"
    assert first.title == "인도네시아 열린교회 부교역자 및 견습선교사"
    assert first.posted_on == date(2026, 8, 4)
    # 목록 href에는 `:443`이 붙어 있다 — 저장되는 URL은 정규형이어야 한다.
    assert first.url == "https://pck.or.kr/bbs/board.php?bo_table=SM05_05&wr_id=5525"
    assert first.list_meta["display_no"] == "1414"


def test_unfilled_deadline_is_not_a_date(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 미입력 마감일이 빈 칸이 아니라 `0000-00-00`으로 온다(실측 3건) — 날짜로 흘리지 않는다."""
    deadlines = [ref.list_meta["deadline"] for ref in refs]
    assert "0000-00-00" not in deadlines
    assert deadlines[0] == "2027-11-30"
    assert refs[0].list_meta["status"] == "모집 중"


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_notice_icon_marks_the_pinned_rows(source: SourceConfig) -> None:
    """⚠️ 공지 표시가 **번호 칸의 아이콘**뿐이다 — `tr.bo_notice`를 보면 공지를 통째로 놓친다."""
    assert len(pck.parse_list(_list_html(_NOTICE_ROW, _row(ident="5524")), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        pck.parse_list(_list_html(_NOTICE_ROW), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        pck.parse_list(_list_html(_row(posted="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_carries_the_extra_fields_before_the_body(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 교회명·노회명은 본문이 아니라 **작성자 줄**에 있다 — 본문만 담으면 통째로 잃는다."""
    raw = pck.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert raw.raw_text.startswith("교회명 : 열린교회")
    assert "인도네시아 열린교회(예장통합)" in raw.raw_text
    assert "제출서류" in raw.raw_text


def test_attachment_url_comes_out_of_the_javascript_call(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 첨부 href가 `javascript:file_download('URL','0')`이다 — href를 그대로 쓰면 못 읽는다."""
    raw = pck.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert len(raw.attachments) == 1
    assert raw.attachments[0].name == "부교역자 및 견습선교사 청빙공고 - 2026.pdf"
    assert "/bbs/download.php?bo_table=SM05_05&wr_id=5525" in raw.attachments[0].url
    assert not raw.image_urls
