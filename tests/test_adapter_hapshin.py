"""HAPSHIN 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source HAPSHIN` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.clock import board_today
from minjob_ingest.sources.adapters import hapshin
from minjob_ingest.sources.adapters.base import ParseError, PostingRef

# 그누보드 계열 공용 헬퍼는 지금 kts.py에 있다(base.py 공용화 후보).
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "HAPSHIN"
#: 실측: tr 17 = 헤더 1 + 공지 1 + 공고 15.
_EXPECTED_POSTINGS: Final = 15
_NOTICE_TITLE_PART: Final = "홈페이지 아이디 및 비밀번호 분실"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="HAPSHIN fixture 없음 — `minjob-ingest snapshot --source HAPSHIN`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "HAPSHIN")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return hapshin.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


def _row(
    *, no: str = "10972", ident: str = "15264", title: str = "가", posted: str = "08.02"
) -> str:
    return (
        f'<tr><td class="text-center font-11"><span class="en">{no}</span></td>'
        '<td class="list-subject">'
        f'<a href="https://hapdong.ac.kr:443/bbs/board.php?bo_table=e03&amp;wr_id={ident}">'
        f'<span class="wr-icon wr-new"></span> {title}</a></td>'
        '<td><b><span class="sv_member">아무개</span></b></td>'
        f'<td class="text-center en font-11">{posted}</td>'
        '<td class="text-center en font-11">7</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="table div-table list-pc bg-white">{"".join(rows)}</table>'


#: 공지는 제목 칸에 `notice` 클래스가 붙고 번호 대신 아이콘이 들어간다(실측).
_NOTICE_ROW: Final = (
    _row(no='<span class="wr-icon wr-notice"></span>', ident="9486")
    .replace('class="list-subject"', 'class="list-subject notice"')
    .replace('<span class="en">', "")
    .replace("</span></td>", "</td>", 1)
)


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 15건만 남는다(실측 · `fetch_note`의 "공지 6개"는 옛 값)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert all(_NOTICE_TITLE_PART not in ref.title for ref in refs)


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "15264"
    assert (
        first.title
        == "안양일심교회(예장고신, 경기중부노회)에서 파트로 사역할 여전도사님을 모십니다."
    )
    # 목록 href에는 `:443`이 붙어 있다 — 저장되는 URL은 정규형이어야 한다.
    assert first.url == "https://hapdong.ac.kr/bbs/board.php?bo_table=e03&wr_id=15264"
    assert first.list_meta["display_no"] == "10972"
    assert first.list_meta["author"] == "찬솔아빠"


def test_dotted_month_day_is_restored_to_a_real_date(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 이 게시판의 연도 없는 날짜는 **점 구분**(`08.02`)이다 — 하이픈만 받으면 통째로 실패한다.

    오늘 글은 시각만(`15:36`) 나온다.
    """
    assert refs[0].list_meta["list_date"] == "15:36"
    assert refs[0].posted_on == board_today()
    assert refs[2].list_meta["list_date"] == "08.02"
    assert refs[2].posted_on == date(2026, 8, 2)


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_notice_is_marked_on_the_subject_cell(source: SourceConfig) -> None:
    """⚠️ 공지 표시가 `tr.bo_notice`가 아니라 **제목 칸의 `notice` 클래스**다(실측)."""
    assert len(hapshin.parse_list(_list_html(_NOTICE_ROW, _row(ident="15263")), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        hapshin.parse_list(_list_html(_NOTICE_ROW), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        hapshin.parse_list(_list_html(_row(posted="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_view_content(refs: tuple[PostingRef, ...], detail_html: str) -> None:
    """본문은 `div.view-content`다(실측 575자 · HWP에서 붙인 `<span>` 더미)."""
    raw = hapshin.parse_detail(detail_html, refs[0])
    assert "파트타임 여전도사 또는 기관 사역자" in raw.raw_text
    assert "부임시기 2027년 1월" in raw.raw_text
    assert raw.attachments == ()


def test_missing_no_attach_marker_demands_attachments(
    refs: tuple[PostingRef, ...], detail_html: str
) -> None:
    """⚠️ 첨부 있는 공고를 실측하지 못했다 — 머리 패널의 `no-attach`가 유일한 교차 신호다.

    그 표시가 없는데 첨부가 0건이면 다운로드 링크 셀렉터가 빗나간 것이다.
    """
    with_attachment = detail_html.replace(" no-attach", "", 1)
    with pytest.raises(ParseError, match="첨부가 있다고 표시됐는데"):
        hapshin.parse_detail(with_attachment, refs[0])
