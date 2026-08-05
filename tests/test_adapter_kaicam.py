"""KAICAM 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source KAICAM` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import kaicam
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "KAICAM"
#: 실측: tr.list 31 = 공지 1 + 공고 30(숨은 입력 `LISTLINE=30`과 일치).
_EXPECTED_POSTINGS: Final = 30
_NOTICE_TITLE: Final = "청빙ㆍ청원 게시판 사용안내"
#: 없는 글을 요청했을 때 오는 껍데기(실측 2,071바이트 · `<title>`만 있다).
_SOFT_200_SHELL: Final = "<html><head><title>청빙청원</title></head><body></body></html>"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="KAICAM fixture 없음 — `minjob-ingest snapshot --source KAICAM`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "KAICAM")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return kaicam.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


def _row(
    *,
    no: str = "730",
    ident: str = "436518",
    title: str = "가",
    date_text: str = "2026-08-04",
    notice: bool = False,
) -> str:
    """실측 마크업의 뼈대. 공지는 표시번호 자리에 `icon_notice.gif`가 온다."""
    number = '<img src="http://img.rh2.kr/solution/common/icon/icon_notice.gif"/>' if notice else no
    return (
        f'<tr class="list rwdnormal"><td class="lt">{number}</td>'
        f'<td><div class="BoardTdTitlesDiv" title="{title}"><div class="innerBoardTitles">'
        f'<a href="view.asp?boardid=D9537&amp;boardmasterseq=2726&amp;boarddetailseq={ident}">'
        f"<span>{title}</span></a></div>"
        f'<div class="innerBoardIcons">'
        f'<img src="http://img.rh2.kr/board/white2022/icon/icon_new.gif"/></div></div></td>'
        f'<td class="rwdhide ct"><div class="username">사수정</div></td>'
        f'<td class="rwdhide ct"><div class="date">{date_text}</div></td>'
        f'<td class="rwdhide ct">15</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table id="BOARD_white2022_list">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 30건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다 — `boardmasterseq=2726`은 config가 넣는다."""
    first = refs[0]
    assert first.external_id == "436518"
    assert first.title == "(안산) 임마누엘교회에서 청소년부'파트or준전임 을 모십니다,"
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == (
        "https://home.kaicam.org/webchon.layout/board/white2022/view.asp?"
        "boardid=D9537&boardmasterseq=2726&boarddetailseq=436518"
    )
    # 표시번호(730)와 원장 키(boarddetailseq)는 다르다.
    assert first.list_meta["display_no"] == "730"
    assert first.list_meta["author"] == "사수정"


def test_notice_icon_row_is_dropped(source: SourceConfig) -> None:
    """⚠️ 공지는 class가 아니라 **표시번호 칸의 아이콘**으로 구분된다(tr class는 공고와 같다)."""
    kept = kaicam.parse_list(_list_html(_row(notice=True), _row(ident="436514")), source)
    assert [ref.external_id for ref in kept] == ["436514"]
    with pytest.raises(ParseError, match="전부 걸러짐"):
        kaicam.parse_list(_list_html(_row(notice=True)), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        kaicam.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_posting_only(refs: tuple[PostingRef, ...], detail_html: str) -> None:
    """본문은 `div#contents`(실측 420자)다.

    ⚠️ 상세 페이지 아래에 목록 30건이 다시 그려진다 — 범위를 넓히면 남의 공고 제목이 이 공고의
    증거로 저장된다.
    """
    raw = kaicam.parse_detail(detail_html, refs[0])
    assert "2)교단: KAICAM(한국독립교회선교단체연합체)" in raw.raw_text
    assert "5)교회 주소:경기도 안산시 단원구 화정천동로2안길31" in raw.raw_text
    assert refs[1].title not in raw.raw_text
    # 첨부 영역을 실측하지 못해 수집하지 않는다(어댑터 docstring).
    assert raw.attachments == ()


def test_a_soft_200_shell_is_not_accepted(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 없는 글도 200을 준다(실측 2,071바이트 껍데기) — 상태코드로는 판정할 수 없다."""
    with pytest.raises(ParseError, match="상세 제목"):
        kaicam.parse_detail(_SOFT_200_SHELL, refs[0])


def test_a_different_posting_is_not_accepted(
    refs: tuple[PostingRef, ...], detail_html: str
) -> None:
    """엉뚱한 글을 받아왔으면 제목이 목록과 어긋난다 — 그걸 이 공고의 증거로 삼지 않는다."""
    with pytest.raises(ParseError, match="상세 제목이 목록과 다름"):
        kaicam.parse_detail(detail_html, refs[1])
