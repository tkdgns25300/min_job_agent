"""KWANGSHIN 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source KWANGSHIN` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import kwangshin
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "KWANGSHIN"
#: 실측: tr 10 = 전부 공고. 헤더 행(th)도 고정공지도 없다.
_EXPECTED_POSTINGS: Final = 10

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="KWANGSHIN fixture 없음 — `minjob-ingest snapshot --source KWANGSHIN`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "KWANGSHIN")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return kwangshin.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    no: str = "No.2264",
    href: str = "javascript:fView('42706')",
    date_text: str = "2026.08.04",
    views: str = "조회수 : 25",
    extra: str = "",
) -> str:
    return (
        f"<tr><td><span>{no}</span>"
        f'<p><a href="{href}">전주동부교회(예장합동) 구인광고</a></p>'
        f'<span class="writer">글쓴이 : 안상민</span></td>'
        f"<td><span>{date_text}</span></td><td><span>{views}</span></td>{extra}</tr>"
    )


def _list_html(*rows: str) -> str:
    return f'<table class="list_tb2">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_all_rows_are_postings(refs: tuple[PostingRef, ...]) -> None:
    """이 게시판에는 헤더 행도 고정공지도 없다 — 10행이 모두 공고다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·정규 URL이 실측과 같아야 한다."""
    first = refs[0]
    assert first.external_id == "42706"
    assert first.title == "전주동부교회(예장합동) 구인광고"
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == (
        "https://www.kwangshin.ac.kr/front/boardView.do?brd_mgrno=184&menu_no=467&brd_no=42706"
    )


def test_labels_are_stripped_from_the_meta_fields(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 값에 라벨이 붙어 온다(`No.2264` · `글쓴이 : ` · `조회수 : `).

    떼지 않으면 `views`가 늘 `None`이 되고 `display_no`에 접두어가 남는다.
    """
    meta = refs[0].list_meta
    assert meta["display_no"] == "2264"
    assert meta["author"] == "안상민"
    assert meta["views"] == 25
    assert refs[0].external_id != meta["display_no"]


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_href_without_the_js_call_is_rejected(source: SourceConfig) -> None:
    """⚠️ 상세 링크는 **href에 URL이 없다** — `javascript:fView('id')`뿐이다."""
    with pytest.raises(ParseError, match="글번호를 못 찾음"):
        kwangshin.parse_list(_list_html(_row(href="/front/boardView.do?brd_no=42706")), source)


def test_an_extra_cell_is_rejected_before_values_shift(source: SourceConfig) -> None:
    """⚠️ 칸에 클래스가 없어 **위치로** 읽는다 — 칸이 늘면 날짜 자리에 조회수가 온다.

    칸 수를 먼저 확인하지 않으면 그 조합이 `ParseError`가 아니라 **엉뚱한 날짜**로 흘러간다.
    """
    with pytest.raises(ParseError, match="칸이 2개"):
        kwangshin.parse_list(
            _list_html(_row().replace("<td><span>조회수 : 25</span></td>", "")), source
        )


def test_a_non_numbered_row_is_treated_as_a_notice(source: SourceConfig) -> None:
    """표시번호가 `No.<숫자>`가 아니면 공지로 본다 — 그것만 있으면 조용한 0건 대신 에러."""
    with pytest.raises(ParseError, match="전부 걸러짐"):
        kwangshin.parse_list(_list_html(_row(no="공지")), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        kwangshin.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_colspan_cell_only(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `td.details` 한 칸이다(실측 229자) — 표 전체를 잡으면 제목·작성자가 섞인다."""
    raw = kwangshin.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "교구 및 교육부서를 맡아 섬길 전임 사역자" in raw.raw_text
    assert "모집기한: 충원시까지" in raw.raw_text
    assert "글쓴이" not in raw.raw_text
    assert raw.image_urls == ()
    assert raw.attachments == ()


# ── 첨부 ─────────────────────────────────────────────────────────


def test_attachment_bearing_posting_is_measured(refs: tuple[PostingRef, ...]) -> None:
    """첨부 있는 공고(42680 · `.hwp` 1건)로 셀렉터와 **URL 변환**을 고정한다(2026-08-05 실측).

    ⚠️ 목록에 첨부 표시가 없어 교차 신호가 없다 — 이 못이 없으면 `td.file`이 바뀌어도
    "본문 있으니 정상"으로 통과하고, 첨부에만 내용이 있는 공고를 통째로 잃는다.
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("detail_file.html 없음 — `snapshot --url …&brd_no=42680`")
    raw = kwangshin.parse_detail(path.read_text(encoding="utf-8"), refs[0])
    assert [(a.name, a.url) for a in raw.attachments] == [
        (
            "260709- 지원자 서류양식.hwp",
            "https://www.kwangshin.ac.kr/common/download.do?file_no=18745",
        )
    ]
    # 파일명이 살아야 `is_image` 판정이 성립한다 — 여기서는 hwp라 False가 맞다.
    assert raw.attachments[0].is_image is False


def test_body_links_are_not_attachments(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 본문의 교회 홈페이지 링크가 첨부로 새면 안 된다(표본 4/7건이 그랬다 · 2026-08-05).

    첨부는 `td.file`에서만 온다. 본문에서 긁던 예전 코드의 회귀 방지.
    """
    body_link = (
        '<table class="view_tb"><tr><td class="details" colspan="3">'
        '<a href="http://osongch.kr/">http://osongch.kr</a> 지원 문의</td></tr></table>'
    )
    raw = kwangshin.parse_detail(body_link, refs[0])
    assert raw.attachments == ()
    assert "osongch" in raw.raw_text  # 본문 증거로는 남는다


def test_a_file_row_without_links_is_rejected(refs: tuple[PostingRef, ...]) -> None:
    """`td.file` 행이 있는데 링크가 없으면 셀렉터가 빗나간 것이다 — 조용한 0건으로 두지 않는다."""
    broken = (
        '<table class="view_tb"><tr><td class="details" colspan="3">본문</td></tr>'
        '<tr><td class="file" colspan="3">첨부파일 <span>양식.hwp</span></td></tr></table>'
    )
    with pytest.raises(ParseError, match="첨부 링크가 없음"):
        kwangshin.parse_detail(broken, refs[0])


def test_a_file_link_without_the_js_call_is_rejected(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 다운로드 URL도 href에 없다 — `javascript:download(N)`을 못 읽으면 실패해야 한다.

    조용히 `javascript:…`를 URL로 저장하면 구조화가 첨부를 열지 못한다(KTS 실측 계열 사고).
    """
    changed = (
        '<table class="view_tb"><tr><td class="details" colspan="3">본문</td></tr>'
        '<tr><td class="file" colspan="3">첨부파일'
        ' <span><a href="/common/other.do">양식.hwp</a></span></td></tr></table>'
    )
    with pytest.raises(ParseError, match="file_no를 못 뽑음"):
        kwangshin.parse_detail(changed, refs[0])
