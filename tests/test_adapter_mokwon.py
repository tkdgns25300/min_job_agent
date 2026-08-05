"""MOKWON 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source MOKWON` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import mokwon
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "MOKWON"
#: 실측: tr 16 = 헤더 1 + 공지 1 + 공고 14.
_EXPECTED_POSTINGS: Final = 14
_NOTICE_TITLE: Final = "※ 사역지 정보 게시물 작성 방법 ※"
#: 실측 첫 행의 `no` — **32자리 hex**다(숫자가 아니다).
_FIRST_ID: Final = "501103573814a8ef882b3f885d1fb33b"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="MOKWON fixture 없음 — `minjob-ingest snapshot --source MOKWON`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "MOKWON")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return mokwon.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    no: str = "695",
    ident: str = _FIRST_ID,
    title: str = "가",
    date_text: str = "2026-05-18",
    row_class: str = "",
) -> str:
    attr = f' class="{row_class}"' if row_class else ""
    return (
        f'<tr{attr}><td class="ntt_no">{no}</td>'
        f'<td class="title"><a href="?mode=V&amp;no={ident}&amp;GotoPage=1">{title}</a></td>'
        f'<td class="wrt">윤**</td><td class="inq_cnt">88</td>'
        f'<td class="reg_date">{date_text}</td><td class="atch_nm"></td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="board_list">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 14건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·정규 URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == _FIRST_ID
    assert first.title == "[서울/중구용산] 일신교회 풀타임(수련목 가능) 전도사님을 모십니다."
    assert first.posted_on == date(2026, 5, 18)
    assert first.url == (f"https://mokwon.ac.kr/mt1954/html/sub06/0602.html?mode=V&no={_FIRST_ID}")
    # 목록 href의 `GotoPage=1`은 정규형에 남지 않는다 — 같은 글의 URL이 페이지마다 달라지면 안 된다.
    assert "GotoPage" not in first.url


def test_the_external_id_is_hex_not_a_number(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ `no`는 32자리 hex다 — 숫자 검사를 걸면 전 행이 탈락한다.

    숫자인 쪽은 표시번호(`td.ntt_no` = 695)이고 그건 게시판이 다시 매기는 값이라 원장 키가 아니다.
    """
    assert not refs[0].external_id.isdigit()
    assert len(refs[0].external_id) == 32
    assert refs[0].list_meta["display_no"] == "695"


def test_a_changed_id_shape_is_rejected(source: SourceConfig) -> None:
    """id 형태가 hex가 아니게 되면 링크 규칙이 바뀐 것이다 — 조용히 넘기지 않는다."""
    with pytest.raises(ParseError, match="hex"):
        mokwon.parse_list(_list_html(_row(ident="12345")), source)


def test_notice_row_is_detected_by_class_and_by_number(source: SourceConfig) -> None:
    """공지 신호가 둘이다(`tr.bbs_notice` · 표시번호 `공지`) — 하나가 바뀌어도 걸린다."""
    for notice in (_row(row_class="bbs_notice"), _row(no="공지")):
        with pytest.raises(ParseError, match="전부 걸러짐"):
            mokwon.parse_list(_list_html(notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        mokwon.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_posting_content_only(refs: tuple[PostingRef, ...]) -> None:
    """본문은 카드 안쪽 `div.bbs--view--content`다(실측 538자).

    상세 페이지에는 사이트 내비게이션·학과 메뉴가 함께 있어 범위를 좁혀야 한다.
    """
    raw = mokwon.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "기독교대한감리회 서울연회 중구용산지방 일신교회" in raw.raw_text
    assert "6. 제출서류" in raw.raw_text
    assert "학과안내" not in raw.raw_text
    # 이 공고는 본문만 있다(목록 `td.atch_nm`가 15행 전부 빈칸 · 실측).
    assert raw.attachments == ()
    assert raw.image_urls == ()
