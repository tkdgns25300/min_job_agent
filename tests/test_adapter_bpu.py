"""BPU 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source BPU` 로 받는다. 첨부가 달린 상세(`detail_with_file.html`)는
`BoardNo=101347`을 `--url`로 받은 것이다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import bpu
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "BPU"
#: 실측: tr 11 = 헤더 1 + 공지 1 + 공고 9.
_EXPECTED_POSTINGS: Final = 9
_NOTICE_TITLE: Final = "[공지] 청빙/취업 게시판 게시글 작성 양식"
#: 첨부가 달린 공고(목록 2번째 행 · `alt="첨부파일"` 아이콘).
_WITH_FILE: Final = "detail_with_file.html"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="BPU fixture 없음 — `minjob-ingest snapshot --source BPU`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "BPU")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return bpu.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *, no: str = "3422", ident: str = "101350", title: str = "가", date_text: str = "2026.08.04"
) -> str:
    """실측 href 형태 — `BoardNo`가 **쿼리 중간**에 온다(순서가 config와 다르다)."""
    return (
        f"<tr><td>{no}</td>"
        f'<td class="Left"><a href="BoardView.aspx?CategoryNo=1&amp;PageNo=1&amp;KeyWord='
        f'&amp;KeyField=TITLE&amp;CategoryYN=N&amp;BoardNo={ident}&amp;BoardMstNo=6">{title}</a></td>'
        f"<td>홍길동</td><td>{date_text}</td><td>1</td></tr>"
    )


def _list_html(*rows: str) -> str:
    return f'<table class="TableStyle03">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 9건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·정규 URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "101350"
    assert first.title == "청소년찬양인도 전도사님"
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == (
        "https://www.bpu.ac.kr/Board/BoardView.aspx?BoardNo=101350&BoardMstNo=6&CategoryNo=1"
    )
    # 표시번호(3422)와 원장 키(101350)는 다르다 — 표시번호는 게시판이 다시 매긴다.
    assert first.list_meta["display_no"] == "3422"


def test_board_no_is_found_by_name_not_position(source: SourceConfig) -> None:
    """⚠️ 목록 href의 쿼리 순서가 config `detail_pattern`과 다르다.

    접두사(`?BoardNo=`)로 자르는 방식은 이 게시판에서 통째로 빗나간다 — 파라미터 **이름**으로
    찾아야 한다. 순서를 뒤집어도 같은 id가 나와야 한다.
    """
    reordered = _row().replace("CategoryNo=1&amp;PageNo=1", "PageNo=1&amp;CategoryNo=1")
    assert bpu.parse_list(_list_html(reordered), source)[0].external_id == "101350"


def test_notice_row_has_no_class_only_a_non_numeric_number(source: SourceConfig) -> None:
    """⚠️ 이 게시판의 공지행에는 class가 없다 — 표시번호 칸의 `공지` 문자열이 유일한 신호다."""
    notice = _row(no="공지", title=_NOTICE_TITLE)
    assert len(bpu.parse_list(_list_html(notice, _row(ident="101347")), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        bpu.parse_list(_list_html(notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        bpu.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_excludes_the_board_guide(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 상세에는 게시판 이용안내(`p.tit_txt`)가 함께 있다 — 본문 범위를 좁혀야 한다(실측 262자)."""
    raw = bpu.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "교단명 :통합" in raw.raw_text
    assert "근무부서 :청소년부" in raw.raw_text
    assert "작성 후 6개월이 지난 게시물은 삭제될 수 있습니다" not in raw.raw_text


@pytest.mark.skipif(not (_FIXTURES / _WITH_FILE).exists(), reason=f"BPU {_WITH_FILE} 없음")
def test_attachment_url_comes_from_the_hidden_input(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 다운로드 앵커는 `javascript:__doPostBack(…)`이라 href에 URL이 없다.

    hidden `hdnFilePath`에서 복원하지 않으면 첨부가 조용히 0개가 되고, 본문이 라벨뿐인 공고
    (실측 101347: 내용이 전부 포스터 jpeg에 있다)는 증거를 통째로 잃는다.
    """
    raw = bpu.parse_detail((_FIXTURES / _WITH_FILE).read_text(encoding="utf-8"), refs[1])
    assert len(raw.attachments) == 1
    attachment = raw.attachments[0]
    assert attachment.name == "삼성교회 담임목사 청빙.jpeg"
    assert attachment.url.startswith("https://www.bpu.ac.kr/Upload/Common/2026/8/")
    assert attachment.is_image
    assert "javascript" not in attachment.url


def test_attachment_icon_without_files_is_an_error(refs: tuple[PostingRef, ...]) -> None:
    """목록 아이콘은 첨부 셀렉터가 빗나갔는지 보는 **독립 신호**다 — 어긋나면 실패시킨다."""
    with pytest.raises(ParseError, match="첨부"):
        bpu.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[1])
