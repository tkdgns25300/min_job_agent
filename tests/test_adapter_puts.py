"""PUTS 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source PUTS` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import puts
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "PUTS"
#: 실측: tr 54 = 공지 4(tr.ntc) + 공고 50. 공고 수는 `pagesize=50`이 고정한다.
_EXPECTED_POSTINGS: Final = 50
_NOTICE_TITLES: Final = (
    "구글에 개인정보 노출시 삭제 요청하는 방법 안내",
    "초빙완료시 [초빙공고] -> [초빙완료]로 수정 요청",
    "초빙게시판 기능 업데이트 안내 (2024.03)",
)
#: 다른 게시판(`fetch_note`의 `jnotice02`) 행이 섞였을 때 버려야 하는 값.
_FOREIGN_BOARD: Final = "jnotice02"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="PUTS fixture 없음 — `minjob-ingest snapshot --source PUTS`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "PUTS")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return puts.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    seq: str = "157666",
    num: str = "157666",
    title: str = "가",
    board: str = "jangshin_jboard04",
    date_text: str = "2026.08.04",
    notice: bool = False,
    file_icon: bool = False,
) -> str:
    """실측 마크업의 뼈대. 칸이 2개뿐이고 나머지 필드는 `span`으로 들어 있다."""
    icon = (
        '<div class="file"><a href="#" title="첨부파일다운로드">'
        '<img alt="첨부파일" src="/main/_common/ic_file1.png"/></a></div>'
        if file_icon
        else ""
    )
    return (
        f"<tr{' class="ntc"' if notice else ''}>"
        f'<td class="c1"><span class="num">{num}</span>{icon}</td>'
        f'<td class="c2"><div class="tit">'
        f'<a href="view.general.asp?seq={seq}&amp;skin=type2&amp;bd_name={board}&amp;page=1">'
        f'<span class="grp">초빙공고</span> {title}</a></div>'
        f'<span class="name">향기나는교회/김호윤</span>'
        f'<span class="date">{date_text}</span>'
        f'<div class="usbox"><span class="count" title="조회">7</span></div></td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<div class="BoardListTy1"><table>{"".join(rows)}</table></div>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notices_are_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 4건(`tr.ntc`)은 제외하고 공고 50건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    titles = {ref.title for ref in refs}
    for notice in _NOTICE_TITLES:
        assert notice not in titles


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다 — span이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "157666"
    assert first.title == "교회 시설 관리집사님을 청빙합니다."
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == (
        "https://puts.ac.kr/www/board/view.general.asp?seq=157666&bd_name=jangshin_jboard04"
    )
    # 분류 배지는 제목에서 떼어 meta로 간다(제목이 `초빙공고 ...`로 시작하면 안 된다).
    assert first.list_meta["category"] == "초빙공고"
    assert first.list_meta["author"] == "부천노회/향기나는교회"


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_foreign_board_rows_are_dropped(source: SourceConfig) -> None:
    """⚠️ 이 사이트의 `seq`는 게시판을 가로질러 매겨진다 — 섞인 행을 받으면 남의 글이 원장에 든다."""
    mine, foreign = _row(seq="157666"), _row(seq="149165", board=_FOREIGN_BOARD)
    assert [ref.external_id for ref in puts.parse_list(_list_html(mine, foreign), source)] == [
        "157666"
    ]
    with pytest.raises(ParseError, match="전부 걸러짐"):
        puts.parse_list(_list_html(foreign), source)


def test_attachment_icon_is_not_mistaken_for_the_detail_link(source: SourceConfig) -> None:
    """⚠️ 첨부가 있는 행은 `div.file`의 `a href="#"`이 제목 링크보다 **먼저** 온다(실측 9/50행).

    셀렉터를 `td a[href]`로 느슨하게 두면 그 행들만 `#`을 상세 URL로 쓰거나 통째로 걸러진다.
    """
    refs = puts.parse_list(_list_html(_row(file_icon=True)), source)
    assert refs[0].external_id == "157666"
    assert refs[0].list_meta["has_attachment"] is True


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        puts.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_keeps_the_form_and_drops_site_boilerplate(
    refs: tuple[PostingRef, ...],
) -> None:
    """본문은 `div.cont` — 교단이 명시된 양식 + 자유 서술이다(실측 375자).

    ⚠️ 사이트 안내문(`notesBox2`·`notesBox3`)은 모든 글에 붙는 240자 상용구라 떼어낸다.
    """
    raw = puts.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "예장 통합 / 부천노회" in raw.raw_text
    assert "향기나는교회에서 관리집사님을 청빙합니다." in raw.raw_text
    assert "핸드폰번호 대체 문자표시" not in raw.raw_text
    assert "검색 결과 삭제 요청" not in raw.raw_text


def test_attachment_is_taken_from_the_file_box(refs: tuple[PostingRef, ...]) -> None:
    """첨부는 본문 밖 `div.file`에 있다 — 본문에서 찾으면 지도·주소복사 링크가 첨부가 된다."""
    raw = puts.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert [attachment.name for attachment in raw.attachments] == [
        "향기나는교회에서 관리집사님을 청빙합니다 (2026년 8월).hwp"
    ]
    assert raw.image_urls == ()


def test_attachment_flagged_in_the_list_must_appear_in_the_detail(
    refs: tuple[PostingRef, ...],
) -> None:
    """목록 아이콘은 독립 신호다 — `div.file`이 사라지면 첨부가 조용히 0개가 되는 것을 막는다."""
    html = (_FIXTURES / "detail.html").read_text(encoding="utf-8")
    assert refs[0].list_meta["has_attachment"] is True
    with pytest.raises(ParseError, match="첨부"):
        puts.parse_detail(html.replace('<div class="file">', '<div class="gone">'), refs[0])
