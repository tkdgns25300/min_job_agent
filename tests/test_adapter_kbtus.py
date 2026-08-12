"""KBTUS 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source KBTUS` 로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import kbtus
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "KBTUS"
#: 실측: tr 20 = 헤더 1 + 공지 1 + 공고 18.
_EXPECTED_POSTINGS: Final = 18
_NOTICE_TITLE: Final = "공지 [사역자 채용 공지 안내 및 2025년 이전 자료 삭제 안내]"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="KBTUS fixture 없음 — `minjob-ingest snapshot --source KBTUS`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "KBTUS")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return kbtus.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    no: str = "18",
    ident: str = "37449",
    href: str | None = None,
    date_text: str = "2026-08-04",
) -> str:
    # ⚠️ href를 **홑따옴표**로 감싼다 — JS 인자가 겹따옴표라 실제 게시판도 그렇다(실측).
    link = f'javascript:URL_encode("?mCode=MN014&mode=view&mgr_seq=91&board_seq={ident}");'
    return (
        f'<tr><td class="num">{no}</td><td class="cate">[파트]</td>'
        f"<td class=\"subject\"><a href='{href if href is not None else link}'>가</a></td>"
        f'<td class="writer">홍길동</td><td class="date">{date_text}</td>'
        f'<td class="cnt">1</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="board-list-table">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건(`tr.isnotice`)은 제외하고 공고 18건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "37449"
    assert first.title == "[대전]태평중앙교회에서 찬양인도 사역자(파트)를 모십니다."
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == (
        "https://job.kbtus.ac.kr/job/CMS/Board/Board.do"
        "?mCode=MN014&mode=view&mgr_seq=91&board_seq=37449"
    )
    # 고용형태 구분은 게시판이 직접 붙인 값이라 구조화의 근거가 된다.
    assert first.list_meta["category"] == "[파트]"


def test_the_id_comes_from_the_js_call_not_the_href(source: SourceConfig) -> None:
    """⚠️ href가 `javascript:URL_encode(...)`다 — href를 URL로 믿으면 id를 통째로 놓친다."""
    with pytest.raises(ParseError, match="글번호를 못 찾음"):
        kbtus.parse_list(_list_html(_row(href="javascript:void(0);")), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        kbtus.parse_list(_list_html(_row(date_text="")), source)


def test_the_boards_own_empty_page_marker_is_not_an_error(source: SourceConfig) -> None:
    """⚠️ 실측(`&page=2`): 게시판이 `td.no-data` 안내 행 하나를 준다.

    공고 행으로 다루면 "상세 링크 없음"으로 터지고, 공지로 걸러내면 "전부 걸러짐"으로 터진다 —
    둘 다 오진이다. 게시판이 스스로 글 없음을 말한 것이므로 빈 결과가 정답이다.
    """
    empty = _list_html('<tr><td class="no-data" colspan="6">등록된 게시글이 없습니다.</td></tr>')
    assert kbtus.parse_list(empty, source) == ()


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_drops_the_cms_boilerplate(refs: tuple[PostingRef, ...]) -> None:
    """본문 맨 앞의 저작권 안내는 모든 공고에 붙는 CMS 고정 문구라 남기지 않는다(실측 701자)."""
    raw = kbtus.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "가. 교회명 : 태평중앙교회" in raw.raw_text
    assert "나. 교단 : 예장 합동" in raw.raw_text
    assert "저작권 등 다른 사람의 권리를 침해하거나" not in raw.raw_text


def test_detail_has_no_attachment_for_a_text_only_posting(refs: tuple[PostingRef, ...]) -> None:
    """실측 공고는 본문만 있다 — 상세 표의 이전/다음글 링크가 첨부로 새면 여기서 드러난다."""
    raw = kbtus.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert raw.attachments == ()
    assert raw.image_urls == ()


def test_attachment_bearing_posting_is_measured(refs: tuple[PostingRef, ...]) -> None:
    """첨부가 달린 실제 공고로 셀렉터를 고정한다(2026-08-05 실측 · `detail_file.html` = 37416).

    ⚠️ 표본 공고에 첨부가 없으면 셀렉터가 틀려도 "정상인데 첨부 0개"로 통과한다 —
    그래서 첨부 있는 공고를 따로 받아 여기서 못을 박는다.

    ⚠️ **앵커 텍스트가 `파일명.hwp (57KB)`다.** 크기 표기를 떼지 않으면 파일명이 확장자로
    끝나지 않아 이미지 첨부의 `is_image`가 거짓이 되고 구조화가 Gemini에 안 보낸다.
    `(목)` 같은 파일명 안의 괄호는 남아야 하므로 **끝의 크기만** 지운다.
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("detail_file.html 없음 — `--url ...&board_seq=37416`")
    marked = [ref for ref in refs if ref.list_meta.get("has_attachment")]
    assert len(marked) == 8, "목록 첨부 아이콘(img.isFileIcon) 대조 신호가 사라졌다"
    ref = next(found for found in marked if found.external_id == "37416")
    raw = kbtus.parse_detail(path.read_text(encoding="utf-8"), ref)
    assert len(raw.attachments) == 1
    only = raw.attachments[0]
    assert only.name == "26년목사청빙공고최종 7.23(목).hwp"
    assert only.url == (
        "https://job.kbtus.ac.kr/job/ajx_json/UploadMgr/downloadRun.do?qcode=Qm9hcmQsNTcxNjYsWQ=="
    )
    assert only.is_image is False


def test_attachment_mark_without_a_file_is_an_error(refs: tuple[PostingRef, ...]) -> None:
    """목록이 첨부 있다고 했는데 상세에서 못 찾으면 **에러**여야 한다.

    이 대조가 없으면 셀렉터가 빗나가도 본문 있는 공고는 조용히 통과한다 — 내용이 첨부에만
    있는 공고를 통째로 잃는다.
    """
    flagged = PostingRef(
        external_id=refs[0].external_id,
        url=refs[0].url,
        title=refs[0].title,
        posted_on=refs[0].posted_on,
        list_meta={"has_attachment": True},
    )
    with pytest.raises(ParseError, match="첨부 표시가 있는데"):
        kbtus.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), flagged)


def test_all_rows_filtered_as_notices_is_an_error(source: SourceConfig) -> None:
    """⚠️ 공지 판정 기준이 어긋나면 공고 전량이 조용히 사라진다 — 그때는 에러여야 한다.

    (변이 검증에서 이 경로가 어떤 테스트에도 걸리지 않아 추가했다.)
    """
    notice_only = (
        '<table class="board-list-table"><tbody>'
        '<tr class="isnotice"><td class="num">공지</td>'
        '<td class="subject"><a href="javascript:URL_encode(\'board_seq=1\')">가</a></td>'
        '<td class="date">2026-08-04</td></tr>'
        "</tbody></table>"
    )
    with pytest.raises(ParseError, match="전부 걸러짐"):
        kbtus.parse_list(notice_only, source)
