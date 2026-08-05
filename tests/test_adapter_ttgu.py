"""TTGU 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source TTGU` 로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import ttgu
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "TTGU"
#: 실측: tr 10 = 헤더 1 + 공지 1 + 공고 8.
_EXPECTED_POSTINGS: Final = 8
_NOTICE_TITLE: Final = "(필독) 취업정보 게시 이용 방법 - 꼭 읽어주세요."
#: 공지 글번호. **1·2페이지에 모두** 다시 나온다(실측).
_NOTICE_ID: Final = "1105532"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="TTGU fixture 없음 — `minjob-ingest snapshot --source TTGU`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "TTGU")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return ttgu.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(*, no: str = "1128", href: str = "", date_text: str = "2026.07.29") -> str:
    link = href or "https://www.ttgu.ac.kr/index.php?mid=ttgu_board_03&document_srl=1107457"
    return (
        f'<tr><td class="no">{no}</td>'
        f'<td class="title"><a class="hx" href="{link}">가</a>'
        f'<span class="extraimages"></span></td>'
        f'<td class="time">{date_text}</td><td class="m_no">1</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="bd_lst bd_tb_lst bd_tb">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건(`tr.notice`)은 제외하고 공고 8건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}
    assert _NOTICE_ID not in {ref.external_id for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "1107457"
    assert first.title == "온누리교회 회복사역본부에서 파트교역자님을 초빙합니다."
    assert first.posted_on == date(2026, 7, 29)
    assert first.url == "https://www.ttgu.ac.kr/index.php?mid=ttgu_board_03&document_srl=1107457"
    assert first.list_meta["display_no"] == "1128"
    assert first.external_id != first.list_meta["display_no"]


@pytest.mark.skipif(
    not (_FIXTURES / "list_page2.html").exists(), reason="TTGU list_page2.html 없음"
)
def test_page_two_ids_come_from_document_srl_not_the_page_param(source: SourceConfig) -> None:
    """⚠️ 2페이지 href는 `&page=2&document_srl=…` 순서다(실측).

    `detail_pattern` 접두어로 URL을 뒤지면 이 9건이 통째로 안 잡힌다. 공지는 여기서도 다시
    나오므로(같은 1105532) 제외되는지도 같이 본다.
    """
    page2 = ttgu.parse_list((_FIXTURES / "list_page2.html").read_text(encoding="utf-8"), source)
    assert [ref.external_id for ref in page2][:3] == ["1107374", "1107086", "1107077"]
    assert _NOTICE_ID not in {ref.external_id for ref in page2}
    assert all("page=" not in ref.url for ref in page2)


def test_a_link_without_document_srl_is_rejected(source: SourceConfig) -> None:
    """XE가 링크 형태를 바꾸면 id를 조용히 잃는 대신 실패해야 한다."""
    with pytest.raises(ParseError, match="글번호를 못 찾음"):
        ttgu.parse_list(_list_html(_row(href="/index.php?mid=ttgu_board_03")), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        ttgu.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_document_not_the_comments(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `div.rd_body`다 — 댓글도 `div.xe_content`라서 그것만 쓰면 섞인다(실측 727자)."""
    raw = ttgu.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "1. 교회명 : 온누리교회" in raw.raw_text
    assert "2. 교단명 : 대한예수교장로회 통합" in raw.raw_text
    assert "Write a comment" not in raw.raw_text
    assert raw.attachments == ()


def test_attachment_icon_without_a_file_list_is_an_error(refs: tuple[PostingRef, ...]) -> None:
    """목록 아이콘은 첨부 셀렉터가 빗나갔는지 보는 **독립 신호**다 — 버리지 않는다."""
    flagged = PostingRef(
        external_id=refs[0].external_id,
        url=refs[0].url,
        title=refs[0].title,
        list_meta={"has_attachment": True},
    )
    with pytest.raises(ParseError, match="첨부 아이콘"):
        ttgu.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), flagged)


def test_all_rows_filtered_as_notices_is_an_error(source: SourceConfig) -> None:
    """⚠️ 공지 판정 기준이 어긋나면 공고 전량이 조용히 사라진다 — 그때는 에러여야 한다.

    (변이 검증에서 이 경로가 어떤 테스트에도 걸리지 않아 추가했다.)
    """
    notice_only = (
        '<table class="bd_lst"><tbody>'
        '<tr class="notice"><td class="no">공지</td>'
        '<td class="title"><a href="/index.php?mid=ttgu_board_03&document_srl=1">가</a></td>'
        '<td class="time">2026-07-29</td></tr>'
        "</tbody></table>"
    )
    with pytest.raises(ParseError, match="전부 걸러짐"):
        ttgu.parse_list(notice_only, source)
