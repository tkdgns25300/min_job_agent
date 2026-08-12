"""MTU 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source MTU` 로 받는다. 첨부가 달린 상세(`detail_with_file.html`)는
`brdIdx=20687`을 `--url`로 받은 것이다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import mtu
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "MTU"
#: 실측: tr 22 = 헤더 1 + 공지 1 + 공고 20.
_EXPECTED_POSTINGS: Final = 20
_NOTICE_TITLE_HEAD: Final = "취업게시판 구인 양식"
#: 첨부(HWP)가 달린 공고. 목록 `td.file`에 아이콘이 있다.
_WITH_FILE: Final = "detail_with_file.html"
_WITH_FILE_ID: Final = "20687"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="MTU fixture 없음 — `minjob-ingest snapshot --source MTU`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "MTU")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return mtu.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    no: str = "10965",
    ident: str = "20694",
    title: str = "가",
    date_text: str = "2026-07-31",
    row_class: str = "",
) -> str:
    attr = f' class="{row_class}"' if row_class else ""
    return (
        f'<tr{attr}><td class="number">{no}</td>'
        f'<td class="tltle left"><a class="fn_btn_view" href="view.do?mId=162&amp;brdIdx={ident}"'
        f">{title}</a></td>"
        f'<td class="file"></td><td class="writer">관리자</td>'
        f'<td class="date">{date_text}</td><td class="hit">92</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="tbListA">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 20건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert all(not ref.title.startswith(_NOTICE_TITLE_HEAD) for ref in refs)


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·정규 URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "20694"
    assert first.title == "중부/부천남] 성은교회 파트타임 사역자를 모집합니다."
    assert first.posted_on == date(2026, 7, 31)
    # ⚠️ `mId=162`가 빠지면 상세가 게시판을 특정하지 못한다(config `detail_pattern`이 들고 있다).
    assert first.url == "https://www.mtu.ac.kr/mtu/board/view.do?mId=162&brdIdx=20694"
    # 표시번호(10965)와 원장 키(brdIdx)는 다르다 — 표시번호는 게시판이 다시 매긴다.
    assert first.list_meta["display_no"] == "10965"


def test_the_title_cell_class_is_misspelled_on_purpose(source: SourceConfig) -> None:
    """⚠️ 제목 칸 class가 `tltle`이다(게시판 원본의 오타).

    `title`로 고쳐 쓰면 상세 링크를 하나도 못 찾는다 — 실수로 "정상화"하는 것을 막는다.
    """
    with pytest.raises(ParseError, match="상세 링크"):
        mtu.parse_list(
            _list_html(_row().replace('class="tltle left"', 'class="title left"')), source
        )


def test_notice_row_is_detected_by_class_and_by_number(source: SourceConfig) -> None:
    """공지 신호가 둘이다(`tr.notice` · 번호 칸이 `공지사항` 이미지라 숫자가 없다)."""
    by_class = _row(row_class="notice")
    by_icon = _row(no='<img alt="공지사항" src="/assets/img/board/icon_notice@2x.png"/>')
    for notice in (by_class, by_icon):
        with pytest.raises(ParseError, match="전부 걸러짐"):
            mtu.parse_list(_list_html(notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        mtu.parse_list(_list_html(_row(date_text="")), source)


def test_page_two_inserts_page_into_the_detail_href(source: SourceConfig) -> None:
    """⚠️ 2페이지부터 상세 href가 `view.do?mId=162&page=2&brdIdx=…`가 된다(실측).

    config `detail_pattern` 접두사(`?mId=162&brdIdx=`)로 자르면 **2페이지 이후 전 행이 탈락**한다 —
    1페이지만 보면 절대 드러나지 않는다. 파라미터 이름으로 뽑으니 `page`가 끼어도 같아야 한다.
    """
    paged = _row().replace("mId=162&amp;brdIdx=", "mId=162&amp;page=2&amp;brdIdx=")
    refs = mtu.parse_list(_list_html(paged), source)
    assert refs[0].external_id == "20694"
    assert "page=2" not in refs[0].url  # 정규형에는 페이지 상태가 남지 않는다


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_survives_the_hwp_paste(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `<pre>` 안 HWP 붙여넣기다 — span 조각이 붙어 한 줄로 뭉치면 안 된다(실측 444자)."""
    raw = mtu.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert (
        "중부연회 부천남지방 성은교회(담임:박승남목사)에서 파트타임 사역자를 모집합니다."
        in raw.raw_text
    )
    assert "- 감리교 계통의 신학대학/대학원 재학생 및 졸업자" in raw.raw_text
    assert "취업게시판" not in raw.raw_text  # 사이트 내비게이션이 섞이지 않았다


@pytest.mark.skipif(not (_FIXTURES / _WITH_FILE).exists(), reason=f"MTU {_WITH_FILE} 없음")
def test_attachment_comes_from_the_file_dl(refs: tuple[PostingRef, ...]) -> None:
    """첨부는 본문 밖 `dl.attached-file-wrapper`에 있다(실측 HWP 1건).

    본문만 훑으면 이력서 양식 같은 첨부를 통째로 잃는다. 목록의 첨부 아이콘은 그 셀렉터가
    빗나갔는지 보는 **독립 신호**이므로, 어긋나면 실패해야 한다.
    """
    with_file = next(ref for ref in refs if ref.external_id == _WITH_FILE_ID)
    raw = mtu.parse_detail((_FIXTURES / _WITH_FILE).read_text(encoding="utf-8"), with_file)
    assert len(raw.attachments) == 1
    attachment = raw.attachments[0]
    assert attachment.name == "광림교회 이력서.hwp"
    assert attachment.url == (
        "https://www.mtu.ac.kr/mtu/board/download.do?mId=162&brdIdx=20687&fidx=1&itId=file"
    )
    assert not attachment.is_image  # HWP도 첨부로 남긴다(이미지만 모으지 않는다)
    with pytest.raises(ParseError, match="첨부 표시가 있는데"):
        mtu.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), with_file)
