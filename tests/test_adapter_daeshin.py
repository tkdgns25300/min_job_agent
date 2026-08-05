"""DAESHIN 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source DAESHIN` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import daeshin
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "DAESHIN"
#: 실측: tr 18 = 헤더 1 + 공지 2 + 공고 15.
_EXPECTED_POSTINGS: Final = 15
_NOTICE_TITLES: Final = (
    "저희 게시판은 끌어올림 기능이 없습니다.",
    "취업게시판 이용안내 및 관계법령",
)

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="DAESHIN fixture 없음 — `minjob-ingest snapshot --source DAESHIN`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "DAESHIN")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return daeshin.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *, no: str = "11350", ident: str = "0001540699999999", title: str = "가", cells: str = ""
) -> str:
    return (
        f'<tr><td class="No">{no}</td>'
        f'<td class="Title">'
        f'<a href="/html/05_community/03.php?AT=V&amp;b_id={ident}">{title}</a></td>'
        f'<td class="Name">홍길동</td><td class="Date">2026.08.04</td>'
        f'<td class="Hits">1</td>{cells}</tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="board">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notices_are_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 2건은 제외하고 공고 15건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    titles = {ref.title for ref in refs}
    for notice in _NOTICE_TITLES:
        assert notice not in titles


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "0001540699999999"
    assert first.title == "경산중앙교회에서 함께 할 동역자를 모십니다."
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == (
        "https://daeshin.ac.kr/html/05_community/03.php?AT=V&b_id=0001540699999999"
    )


def test_display_number_is_not_the_external_id(refs: tuple[PostingRef, ...]) -> None:
    """표시번호(11350)와 원장 키(b_id)는 다르다 — 표시번호는 게시판이 다시 매긴다."""
    assert refs[0].list_meta["display_no"] == "11350"
    assert refs[0].external_id != refs[0].list_meta["display_no"]


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_notice_class_on_cells_not_rows(source: SourceConfig) -> None:
    """⚠️ 이 게시판은 공지 표시를 **칸**에 붙인다 — `tr`을 보면 공지를 통째로 놓친다."""
    notice = _row(no="공지", cells="").replace('class="No"', 'class="No notice"')
    assert daeshin.parse_list(_list_html(notice, _row(ident="0001539299999999")), source)
    only_notice = _list_html(notice)
    with pytest.raises(ParseError, match="전부 걸러짐"):
        daeshin.parse_list(only_notice, source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        daeshin.parse_list(_list_html(_row().replace(">2026.08.04<", "><")), source)


def test_missing_link_is_rejected(source: SourceConfig) -> None:
    with pytest.raises(ParseError, match="상세 링크"):
        daeshin.parse_list(
            _list_html('<tr><td class="No">1</td><td class="Date">2026.08.04</td></tr>'), source
        )


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_content_cell(refs: tuple[PostingRef, ...]) -> None:
    """본문은 표 안의 `td.Cont` 칸이다 — 모집요강이 그 안에 표로 들어 있다(실측 281자)."""
    raw = daeshin.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "경산중앙교회에서 2027년 동역자를 모십니다." in raw.raw_text
    assert "제자훈련" in raw.raw_text
    assert len(raw.image_urls) == 1


def test_site_wide_footer_file_is_not_an_attachment(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 페이지 푸터에 사이트 공용 `.hwp`가 있고 상세 표에는 PREV/NEXT 링크가 있다.

    첨부 범위를 본문 밖으로 넓히면 그것들이 첨부로 저장돼, 구조화가 엉뚱한 파일을 읽는다.
    """
    raw = daeshin.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert raw.attachments == ()
    assert all("장학금기탁서" not in url for url in raw.image_urls)
