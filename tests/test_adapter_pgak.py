"""PGAK 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source PGAK` 으로 받는다. 첨부가 달린 상세 표본 `detail_file.html`은
`--url ".../view.asp?boarddetailseq=435901&boardid=B5FF8" --name detail_file.html`로 받는다.
"""

from __future__ import annotations

from datetime import date
from html import escape
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import pgak
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "PGAK"
#: 실측: tr.list 24 = 공고 12 + 모바일 중복행 12.
_EXPECTED_POSTINGS: Final = 12
#: 상세 페이지가 아래에 다시 그리는 목록에 들어 있는 **다른 공고**의 제목 조각.
_OTHER_POSTING: Final = "대림동 삼일교회"
#: 첨부 4개가 달린 실측 공고(`detail_file.html`). 1페이지 목록에 있어 `refs`로 대조할 수 있다.
_WITH_FILES: Final = "435901"
#: 첨부 파일 호스트 — CMS 벤더의 별도 도메인이다(게시판 호스트가 아니다 · 실측).
_FILE_HOST: Final = "https://pds.rh2.kr/pgak/"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="PGAK fixture 없음 — `minjob-ingest snapshot --source PGAK`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "PGAK")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return pgak.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


def _row(
    *, no: str = "4958", ident: str = "436517", title: str = "가", date_text: str = "2026-08-04"
) -> str:
    return (
        f'<tr class="list"><td>{no}</td>'
        f'<td class="title"><div class="title_in">'
        f'<a href="./view.asp?boarddetailseq={ident}&amp;boardid=B5FF8">{title}</a>'
        f'<div class="innerBoardIcons"><span class="icon_new ND">New</span></div></div></td>'
        f'<td class="user">사수정</td><td class="date">{date_text}</td>'
        f'<td class="hit">16</td></tr>'
    )


def _mobile_row() -> str:
    """모바일 중복행 — 제목 링크가 없다(실측)."""
    return (
        '<tr class="list m_wrap"><td></td><td class="t_left">'
        '<span class="user">사수정</span> | <span class="data">2026-08-04</span></td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="board-list"><tbody>{"".join(rows)}</tbody></table>'


def _detail_html(*, body: str, files: str = "") -> str:
    """상세 마크업의 뼈대. **본문은 escape한다** — 실물도 textarea에 escape돼 들어 있다(실측).

    첨부가 없는 공고에는 `div.tag_box`가 아예 렌더되지 않는다(실측) — 그것까지 재현한다.
    """
    box = (
        f'<div class="tag_box"><p class="tit">첨부파일</p>'
        f'<div class="file-list-container">{files}</div></div>'
        if files
        else ""
    )
    return (
        '<div id="viewBoard"><div id="contents">'
        f'<textarea id="temp-raw-content">{escape(body)}</textarea>'
        f'<div id="contentWrap"></div></div>{box}</div>'
    )


# ── 목록 ─────────────────────────────────────────────────────────


def test_mobile_duplicate_rows_are_excluded(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ tr.list는 24개지만 공고는 12건이다 — 나머지 절반은 같은 공고의 모바일 행이다."""
    assert len(refs) == _EXPECTED_POSTINGS


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다."""
    first = refs[0]
    assert first.external_id == "436517"
    assert first.title == "(안산) 임마누엘교회에서 청소년부'파트or준전임 을 모십니다,"
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == (
        "https://pgak.net/sys-infra/components/board/view.asp?boarddetailseq=436517&boardid=B5FF8"
    )
    # 표시번호(4958)와 원장 키(boarddetailseq)는 다르다.
    assert first.list_meta["display_no"] == "4958"
    assert first.list_meta["author"] == "사수정"


def test_a_page_of_only_mobile_rows_is_an_error(source: SourceConfig) -> None:
    """반응형 레이아웃이 바뀌어 데이터 행이 전부 모바일 행이 되면 **조용한 0건**이 된다."""
    assert len(pgak.parse_list(_list_html(_row(), _mobile_row()), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        pgak.parse_list(_list_html(_mobile_row()), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        pgak.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_comes_from_the_hidden_textarea(
    refs: tuple[PostingRef, ...], detail_html: str
) -> None:
    """⚠️ 본문은 숨은 textarea의 HTML 문자열이다 — 꺼내 다시 파싱해야 글이 된다(실측 444자).

    다시 파싱하지 않으면 `<br />`가 글자로 남아 AI 입력이 태그로 오염된다.
    """
    raw = pgak.parse_detail(detail_html, refs[0])
    assert "2)교단: KAICAM(한국독립교회선교단체연합체)" in raw.raw_text
    assert "-이메일 접수: 0691cyj@hanmail.net" in raw.raw_text
    assert "<br" not in raw.raw_text


def test_the_body_is_not_the_relisted_board(refs: tuple[PostingRef, ...], detail_html: str) -> None:
    """⚠️ 상세 페이지 아래에 목록이 다시 그려진다 — 본문 범위를 넓히면 남의 공고가 증거가 된다."""
    raw = pgak.parse_detail(detail_html, refs[0])
    assert _OTHER_POSTING in detail_html
    assert _OTHER_POSTING not in raw.raw_text
    # 이 공고엔 첨부가 없다(`div.tag_box`가 아예 렌더되지 않는다 · 실측).
    assert raw.attachments == ()


def test_attachments_come_from_the_file_list_box(refs: tuple[PostingRef, ...]) -> None:
    """첨부가 달린 실제 공고(435901)로 셀렉터를 고정한다(2026-08-05 실측 · 표본 7건 중 1건).

    ⚠️ 목록에 첨부 표시 칸이 없어 `has_attachment` 대조 신호가 없다 — 셀렉터가 빗나가도
    "정상인데 첨부 0개"로 조용히 통과한다. 그래서 실측값을 여기 못으로 박는다.

    파일명은 **앵커 텍스트**에서 온다 — URL(`이슬람 소개1_ts1784808688052.hwpx`)에는 업로드
    타임스탬프가 끼어 있어 그대로 쓰면 운영자가 받은 파일과 이름이 달라진다.
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("PGAK detail_file.html 없음 — 모듈 docstring의 `--url`로 받는다")
    ref = next(found for found in refs if found.external_id == _WITH_FILES)
    raw = pgak.parse_detail(path.read_text(encoding="utf-8"), ref)
    assert [attachment.name for attachment in raw.attachments] == [
        "6하원칙으로 설득하기.hwp",
        "이슬람 소개1.hwpx",
        "이슬람은 어떻게 생겨났나.hwpx",
        "인격.hwp",
    ]
    assert all(found.url.startswith(_FILE_HOST) for found in raw.attachments)
    assert not any(attachment.is_image for attachment in raw.attachments)
    # ⚠️ 첨부 상자의 `<img>`는 확장자 아이콘(`hwp.gif`)이다 — 이미지로 저장하면 AI가 그걸 읽는다.
    assert raw.image_urls == ()


def test_a_body_link_is_not_an_attachment(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 첨부는 **첨부 상자에서만** 온다 — 본문까지 훑으면 공고에 적힌 교회 홈페이지가 첨부가 된다.

    공용 상자(`div.tag_box`)를 셀렉터로 쓰지 않는 이유도 여기 있다 — 그 상자는 태그 목록도 담는다
    (스킨 CSS `view.css`의 `.tag_box .tag span`).
    """
    html = _detail_html(
        body='교회 홈페이지 <a href="http://example-church.kr/notice.hwp">공고문</a>',
        files='<div class="file-item"><span class="file-name">'
        f'<a href="{_FILE_HOST}real_ts1.hwp">이력서양식.hwp</a></span></div>',
    )
    raw = pgak.parse_detail(html, refs[0])
    assert [found.name for found in raw.attachments] == ["이력서양식.hwp"]
    assert not any("example-church.kr" in found.url for found in raw.attachments)
    assert raw.raw_text == "교회 홈페이지 공고문"


def test_the_js_rendered_container_is_not_the_body(
    refs: tuple[PostingRef, ...], detail_html: str
) -> None:
    """정적 HTML에서 `div#contentWrap`은 항상 비어 있다 — 그걸 읽으면 전 공고가 빈 본문이 된다."""
    without_source = detail_html.replace('id="temp-raw-content"', 'id="was-here"')
    with pytest.raises(ParseError, match="상세 본문"):
        pgak.parse_detail(without_source, refs[0])
