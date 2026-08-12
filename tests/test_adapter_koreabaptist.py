"""KOREABAPTIST 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source KOREABAPTIST` 로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import koreabaptist
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "KOREABAPTIST"
#: 실측: tr 16 = 헤더 1 + 공고 15(고정공지 없음).
_EXPECTED_POSTINGS: Final = 15

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="KOREABAPTIST fixture 없음 — `minjob-ingest snapshot --source KOREABAPTIST`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "KOREABAPTIST")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return koreabaptist.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(*, no: str = "420", ident: str = "581752", date_text: str = "2026-08-03") -> str:
    return (
        f'<tr><td class="document-number">{no}</td>'
        f'<td class="document-title"><a href="/Board/Detail/21317/{ident}">가</a></td>'
        f'<td class="document-writer">홍길동</td>'
        f'<td class="document-regdate">{date_text}</td>'
        f'<td class="document-readedcount">1</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="table table-hover">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_all_rows_are_postings_on_this_board(refs: tuple[PostingRef, ...]) -> None:
    """실측 1페이지에는 고정공지가 없다 — 15건이 전부 공고다."""
    assert len(refs) == _EXPECTED_POSTINGS


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 21317은 게시판 식별자다 — id로 오인하면 15행이 전부 같은 값을 받는다."""
    first = refs[0]
    assert first.external_id == "581752"
    assert first.title == "이스탄불한인교회 담임목사 청빙"
    assert first.posted_on == date(2026, 8, 3)
    assert first.url == "https://koreabaptist.or.kr/Board/Detail/21317/581752"
    assert first.list_meta["display_no"] == "420"


def test_a_notice_row_would_be_filtered(source: SourceConfig) -> None:
    """번호 칸이 숫자가 아니면(=공지 관습) 제외하고, 전부 그렇게 되면 실패로 알린다."""
    notice = _row(no="공지", ident="34526")
    assert len(koreabaptist.parse_list(_list_html(notice, _row()), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        koreabaptist.parse_list(_list_html(notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        koreabaptist.parse_list(_list_html(_row(date_text="")), source)


@pytest.mark.skipif(
    not (_FIXTURES / "list_page2.html").exists(), reason="KOREABAPTIST list_page2.html 없음"
)
def test_page_two_links_carry_a_page_query_that_must_not_reach_the_url(
    source: SourceConfig,
) -> None:
    """⚠️ 2페이지 상세 링크는 `/Board/Detail/21317/560567?page=2`다(실측).

    id는 `?`에서 멈춰야 하고, 저장되는 URL에는 페이지가 섞이면 안 된다 —
    같은 글이 페이지마다 다른 `source_url`을 갖게 된다.
    """
    page2 = koreabaptist.parse_list(
        (_FIXTURES / "list_page2.html").read_text(encoding="utf-8"), source
    )
    assert [ref.external_id for ref in page2][:3] == ["560567", "560202", "552854"]
    assert all("page=" not in ref.url for ref in page2)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_excludes_the_list_embedded_in_the_page(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 상세 페이지가 목록 15행을 또 품고 있다(`div.list-in-detail`).

    범위를 `div.detail-content` 밖으로 넓히면 그 링크들이 첨부로 저장된다(실측 1542자).
    """
    raw = koreabaptist.parse_detail(
        (_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0]
    )
    assert "이스탄불한인교회(초교파)는 튀르키예 이스탄불에 1989년 설립되어" in raw.raw_text
    assert "서천 지원침례교회 후임 담임목사 청빙" not in raw.raw_text  # 아래 목록의 다른 글
    assert raw.attachments == ()


def test_the_list_file_icon_must_be_backed_by_an_image(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 이 게시판의 💾 아이콘은 별도 첨부함이 아니라 **본문 이미지**를 뜻한다(실측).

    이미지가 공고 내용의 전부인 공고가 많아, 셀렉터가 빗나가면 빈 레코드가 조용히 통과한다.
    """
    flagged = PostingRef(
        external_id=refs[0].external_id,
        url=refs[0].url,
        title=refs[0].title,
        list_meta={"has_attachment": True},
    )
    with pytest.raises(ParseError, match="첨부 표시"):
        koreabaptist.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), flagged)


def test_attachment_bearing_posting_is_measured(
    refs: tuple[PostingRef, ...],
) -> None:
    """첨부가 달린 실제 공고로 셀렉터를 고정한다(2026-08-05 실측 · `detail_file.html`).

    ⚠️ 표본 공고에 첨부가 없으면 셀렉터가 틀려도 "정상인데 첨부 0개"로 통과한다 —
    그래서 첨부 있는 공고를 따로 받아 여기서 못을 박는다.
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("detail_file.html 없음")
    marked = [ref for ref in refs if ref.list_meta.get("has_attachment")]
    assert marked, "목록에 첨부 표시된 공고가 없다 — 대조 신호가 사라졌다"
    raw = koreabaptist.parse_detail(path.read_text(encoding="utf-8"), marked[0])
    assert raw.attachments or raw.image_urls, "첨부·이미지를 하나도 못 찾았다"
