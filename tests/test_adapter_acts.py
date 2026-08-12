"""ACTS 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다.
`list.html`·`list_page2.html`은 `minjob-ingest snapshot`으로, 첨부가 달린 상세 표본
`detail_file.html`은 `--url .../bd_view.asp?no=2356&id=acts_csrd_guide`로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import acts
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "ACTS"
#: 실측: tbody tr 11 = 공지 1 + 공고 10.
_EXPECTED_POSTINGS: Final = 10
_NOTICE_TITLE: Final = "사역 공고 게시 방법"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="ACTS fixture 없음 — `minjob-ingest snapshot --source ACTS`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "ACTS")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return acts.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


def _row(*, no: str = "674", ident: str = "2375", title: str = "가", attach: str = "") -> str:
    """실측 칸 순서: 번호 | 구분 | 제목 | 작성자 | 첨부 | 등록일 | 조회수."""
    return (
        f"<tr><td>{no}</td><td>사역</td>"
        f'<td><a class="boardList__tit" href="bd_view.asp?no={ident}&amp;gotopage=1'
        f'&amp;id=acts_csrd_guide&amp;ca_no=1#a1">{title}</a></td>'
        f"<td>경력개발센터</td><td>{attach}</td><td>2026-08-01</td><td>99</td></tr>"
    )


def _list_html(*rows: str) -> str:
    return f'<table class="tbl--comm"><tbody>{"".join(rows)}</tbody></table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 10건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다 — 칸을 위치로 읽으므로 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "2375"
    assert first.title == "이한교회 청빙 공고"
    assert first.posted_on == date(2026, 8, 1)
    assert (
        first.url == "https://www.acts.ac.kr/modules/board/bd_view.asp?no=2375&id=acts_csrd_guide"
    )
    assert first.list_meta["display_no"] == "674"


def test_notice_icon_in_the_number_cell_is_the_only_marker(source: SourceConfig) -> None:
    """⚠️ 공지행에는 클래스가 없다 — 번호 자리에 `<img alt="공지">`가 들어가는 것이 유일한 표시다."""
    notice = _row(no='<img alt="공지" src="/images/icon/notice_orange.png"/>', ident="1627")
    assert len(acts.parse_list(_list_html(notice, _row()), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        acts.parse_list(_list_html(notice), source)


def test_broken_rows_are_rejected(source: SourceConfig) -> None:
    """날짜가 비면 `--months` 범위가 조용히 무의미해지고, 칸이 줄면 **다른 칸을 읽는다**.

    이 게시판은 칸에 클래스가 없어 위치로 읽으므로 둘 다 조용히 지나갈 수 있다.
    """
    with pytest.raises(ParseError, match="게시일 칸"):
        acts.parse_list(_list_html(_row().replace("<td>2026-08-01</td>", "<td></td>")), source)
    short = '<tr><td>674</td><td><a class="boardList__tit" href="x?no=1">가</a></td></tr>'
    with pytest.raises(ParseError, match="칸이"):
        acts.parse_list(_list_html(short), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_content_cell(refs: tuple[PostingRef, ...], detail_html: str) -> None:
    """본문은 `td.boardView__cont`다(실측 284자). 스킨의 숨은 라벨은 본문에서 뺀다."""
    raw = acts.parse_detail(detail_html, refs[0])
    assert raw.raw_text.startswith("1. 교회명")
    assert "게시글 본문" not in raw.raw_text
    assert "모집부서" in raw.raw_text


def test_body_links_are_not_attachments(refs: tuple[PostingRef, ...], detail_html: str) -> None:
    """⚠️ 이 게시판 본문에는 제출처 `mailto:`와 교회 홈페이지 링크가 있다(실측).

    첨부를 "본문 안의 모든 링크"로 잡으면 그것들이 첨부 파일로 저장돼 구조화가 엉뚱한 것을 읽고,
    이전글/다음글 링크까지 딸려 들어온다.
    """
    raw = acts.parse_detail(detail_html, refs[0])
    assert raw.attachments == ()
    assert raw.image_urls == ()


def test_attachment_is_the_download_link(source: SourceConfig) -> None:
    """실측 no=2356: **본문이 비었고 내용이 hwp 첨부에만** 있다 — 첨부를 놓치면 증거가 0이 된다."""
    page2 = _FIXTURES / "list_page2.html"
    detail = _FIXTURES / "detail_file.html"
    if not (page2.exists() and detail.exists()):
        pytest.skip("ACTS list_page2.html / detail_file.html 없음")
    refs = acts.parse_list(page2.read_text(encoding="utf-8"), source)
    with_file = next(ref for ref in refs if ref.list_meta["has_attachment"])
    assert with_file.external_id == "2356"
    raw = acts.parse_detail(detail.read_text(encoding="utf-8"), with_file)
    assert raw.raw_text == ""
    assert [attachment.name for attachment in raw.attachments] == ["개인정보동의서.hwp"]
    assert raw.attachments[0].url.startswith("https://www.acts.ac.kr/lib/download.asp?")


def test_listed_attachment_without_a_link_is_an_error(
    refs: tuple[PostingRef, ...], detail_html: str
) -> None:
    """목록이 첨부를 표시했는데 상세에서 못 찾으면 **조용히 0개**가 아니라 에러다."""
    marked = PostingRef(
        external_id=refs[0].external_id,
        url=refs[0].url,
        title=refs[0].title,
        list_meta={"has_attachment": True},
    )
    with pytest.raises(ParseError, match="첨부 표시"):
        acts.parse_detail(detail_html, marked)
