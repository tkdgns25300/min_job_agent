"""WGST 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source WGST` 으로 받는다. 첨부가 달린 상세 표본 `detail_file.html`은
`--url ".../boardview.asp?key=6131&seq=670" --name detail_file.html`로 받는다(최근 60건에는
첨부가 없다 — 120페이지에서 3건 나왔다 · 어댑터 docstring).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final
from urllib.parse import parse_qsl, urlsplit

import pytest
from bs4 import BeautifulSoup

from minjob_ingest.sources.adapters import wgst
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "WGST"
#: 실측: li.item 12개(페이지당 12건) · 고정공지 없음.
_EXPECTED_POSTINGS: Final = 12
#: 첨부가 달린 실측 공고(`detail_file.html` · 120페이지에서 찾았다).
_WITH_FILE: Final = "670"
_ATTACHMENT_NAME: Final = "(02)성수교회와 함께 동역할 사역자 청빙(2016.12).hwp"
#: 목록 첨부칸의 실측 내용물. 아이콘 세트가 바뀌어도 "무언가 들어 있음"은 유지된다.
_FILE_ICON: Final = '<i aria-hidden="true" class="xi-file-text-o"></i>'
_DETAIL_URL: Final = "http://www.wgst.ac.kr/wgst_renew/board/boardview.asp?key=6131&seq=670"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="WGST fixture 없음 — `minjob-ingest snapshot --source WGST`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "WGST")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def list_html() -> str:
    return (_FIXTURES / "list.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def refs(source: SourceConfig, list_html: str) -> tuple[PostingRef, ...]:
    return wgst.parse_list(list_html, source)


def _row(
    *,
    num: str = "1676",
    href: str = "boardview.asp?key=6131&pageno=1&rowNo=1676&seq=6910&MCD=",
    label: str = '<font color="#2366b5">[제자들교회]</font>',
    title: str = "가",
    date_text: str = "2026.07.28",
    file_cell: str = "",
) -> str:
    """실측 마크업의 뼈대. 표가 아니라 `li` 리스트이고 날짜·조회수는 `dl.info` 안에 있다."""
    return (
        f'<li class="item"><span class="num">{num}</span><div class="content">'
        f'<strong class="subject"><a href="{href}" title="{title}">{label} {title}</a></strong>'
        f'<dl class="info"><dt>작성일</dt><dd>{date_text}</dd>'
        f'<dt class="fileTit">파일</dt><dd class="file">{file_cell}</dd>'
        f"<dt>조회수</dt><dd>33</dd></dl></div></li>"
    )


def _list_html(*rows: str) -> str:
    return f'<ul class="newsfeed_lst">{"".join(rows)}</ul>'


def _marked_ref(external_id: str) -> PostingRef:
    """목록이 "첨부 있음"이라고 표시한 참조. 그 표시가 상세 대조의 기준이다."""
    return PostingRef(
        external_id=external_id,
        url=_DETAIL_URL,
        title="가",
        list_meta={"has_attachment": True},
    )


def _file_item(name: str, *, stored: str = "20161215_stored.hwp") -> str:
    """첨부 한 줄. 다운로드는 href가 아니라 `fileDown` 호출이다(실측)."""
    return (
        f"<li><a href=\"javascript:fileDown('{name}','{stored}','2016.12.15')\">"
        f'<span class="filename">{name}</span><span class="down">다운로드</span></a></li>'
    )


def _detail_html(*, body: str = "", files: str = "") -> str:
    """상세 마크업의 뼈대. 첨부 상자는 **본문 상자 안**에 들어간다(실측)."""
    box = (
        f'<dl class="fileAttach_wrap"><dt>파일</dt><dd>'
        f'<ul class="file_attach">{files}</ul></dd></dl>'
        if files
        else ""
    )
    return (
        '<div class="newsfeed_view"><strong class="newsfeed_subject">가</strong>'
        '<div class="newsfeed_cnts_info typeTXT">작성일 2016.12.15</div>'
        f'<div class="newsfeed_cnts descript"><div class="description">{body}</div>'
        f"{box}</div></div>"
    )


# ── 목록 ─────────────────────────────────────────────────────────


def test_all_rows_are_postings(refs: tuple[PostingRef, ...]) -> None:
    """실측 1페이지는 12건 전부 공고다(고정공지 없음)."""
    assert len(refs) == _EXPECTED_POSTINGS


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다."""
    first = refs[0]
    assert first.external_id == "6910"
    assert first.title == "제자들교회(동탄)에서 중고등부 교역자를 모십니다."
    assert first.posted_on == date(2026, 7, 28)
    assert first.url == "http://www.wgst.ac.kr/wgst_renew/board/boardview.asp?key=6131&seq=6910"
    # 표시번호(rowNo)는 원장 키가 아니다 — 게시판이 다시 매기는 값이다.
    assert first.list_meta["display_no"] == "1676"
    assert first.list_meta["author"] == "제자들교회"


def test_church_label_is_split_off_from_the_title(
    refs: tuple[PostingRef, ...], list_html: str
) -> None:
    """⚠️ 제목 앞 `<font>[교회명]</font>`을 떼야 제목이 교회명을 두 번 담지 않는다.

    이 게시판은 `a[title]`에 순수 제목을 들고 있어 **독립 신호**로 대조할 수 있다 — 대괄호를
    정규식으로 자르는 구현은 `[청빙중]`·`[용인]`처럼 제목에 든 대괄호까지 먹어 여기서 걸린다.
    """
    subjects = [
        str(link.get("title"))
        for link in BeautifulSoup(list_html, "lxml").select(
            "ul.newsfeed_lst li.item strong.subject a"
        )
    ]
    assert [ref.title for ref in refs] == subjects


def test_seq_is_taken_by_name_not_by_url_prefix(source: SourceConfig) -> None:
    """⚠️ 목록 href의 파라미터 순서가 `detail_pattern`과 다르다 — 접두사 매칭은 실패한다."""
    reordered = _row(href="boardview.asp?seq=6910&key=6131&rowNo=1676")
    assert wgst.parse_list(_list_html(reordered), source)[0].external_id == "6910"
    with pytest.raises(ParseError, match="seq"):
        wgst.parse_list(_list_html(_row(href="boardview.asp?key=6131&rowNo=1676")), source)


def test_notice_rows_are_dropped(source: SourceConfig) -> None:
    """공지가 생기면 표시번호 칸에 숫자가 아닌 값이 온다 — 그 행은 제외한다."""
    kept = wgst.parse_list(_list_html(_row(num="공지"), _row(num="1675")), source)
    assert [ref.list_meta["display_no"] for ref in kept] == ["1675"]
    with pytest.raises(ParseError, match="전부 걸러짐"):
        wgst.parse_list(_list_html(_row(num="공지")), source)


def test_missing_date_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        wgst.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_posting_only(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `div.newsfeed_cnts`(실측 388자) — 교단이 본문에 명시된 양식형 공고다.

    ⚠️ 페이지 푸터에 사이트 공용 PDF(`2022_대학안전관리계획.pdf`)가 있다. 첨부·본문 범위를
    넓히면 그것이 공고의 증거로 저장된다.
    """
    raw = wgst.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "모집교회 : 제자들교회(대한예수교장로회 통합)" in raw.raw_text
    assert "사역시작일 : 2026년 9월부터(협의 가능)" in raw.raw_text
    assert "대학안전관리계획" not in raw.raw_text
    # 첨부가 없는 공고에는 `dl.fileAttach_wrap`이 아예 렌더되지 않는다(실측).
    assert raw.attachments == ()
    assert raw.image_urls == ()


# ── 첨부 ─────────────────────────────────────────────────────────


def test_the_list_marks_rows_that_carry_a_file(source: SourceConfig) -> None:
    """첨부칸(`dd.file`)의 아이콘이 **상세와 독립된** 첨부 신호다 — 셀렉터 대조의 근거다."""
    rows = _list_html(
        _row(href="boardview.asp?key=6131&seq=670", file_cell=_FILE_ICON),
        _row(href="boardview.asp?key=6131&seq=6910"),
    )
    kept = wgst.parse_list(rows, source)
    assert [ref.external_id for ref in kept] == ["670", "6910"]
    assert [ref.list_meta["has_attachment"] for ref in kept] == [True, False]


def test_attachment_url_is_rebuilt_from_the_js_call() -> None:
    """실측 seq=670: **본문이 비어 있고 내용 전부가 hwp에** 있다 — 첨부를 놓치면 증거가 0이 된다.

    다운로드는 href에 없다(`javascript:fileDown(...)`) — 숨은 폼이 GET으로 보내는
    `/common/download.asp?rfilename=…&filename=…&regdate=…`로 되살린다(어댑터 docstring).
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("WGST detail_file.html 없음 — 모듈 docstring의 `--url`로 받는다")
    raw = wgst.parse_detail(path.read_text(encoding="utf-8"), _marked_ref(_WITH_FILE))
    assert raw.raw_text == ""
    assert raw.image_urls == ()
    assert [found.name for found in raw.attachments] == [_ATTACHMENT_NAME]
    url = raw.attachments[0].url
    assert url.startswith("http://www.wgst.ac.kr/common/download.asp?")
    assert dict(parse_qsl(urlsplit(url).query)) == {
        # 원본명·저장명이 다르다 — 저장명에만 업로드 타임스탬프가 붙는다.
        "rfilename": _ATTACHMENT_NAME,
        "filename": f"2016121514537_{_ATTACHMENT_NAME}",
        "regdate": "2016.12.15",
    }


def test_the_file_name_drops_the_download_label() -> None:
    """앵커에 `<span class="down">다운로드</span>`가 붙어 있다 — 이름에 섞이면 안 된다.

    확장자가 끝에 없으면 `Attachment.is_image`가 이미지 첨부를 못 알아보고 구조화가 Gemini에
    보내지 않는다(KTS에서 실제로 그랬다).
    """
    raw = wgst.parse_detail(_detail_html(files=_file_item("포스터.png")), _marked_ref("1"))
    assert [found.name for found in raw.attachments] == ["포스터.png"]
    assert raw.attachments[0].is_image


def test_the_file_box_is_taken_out_of_the_body() -> None:
    """⚠️ 첨부 상자는 **본문 상자 안**에 있다 — 빼내지 않으면 파일명이 공고 본문으로 저장된다."""
    raw = wgst.parse_detail(
        _detail_html(body="사역자를 모십니다", files=_file_item("이력서양식.hwp")),
        _marked_ref("1"),
    )
    assert raw.raw_text == "사역자를 모십니다"
    assert [found.name for found in raw.attachments] == ["이력서양식.hwp"]


def test_a_body_link_is_not_an_attachment() -> None:
    """첨부는 `dl.fileAttach_wrap`에서만 온다 — 본문·메뉴까지 훑으면 교회 홈페이지와 사이트 공용
    PDF(`2022_대학안전관리계획.pdf` · 좌측 메뉴에 있다)가 첨부가 된다."""
    unmarked = PostingRef(external_id="1", url=_DETAIL_URL, title="가")
    body = '<a href="/wgst_renew/upfile/gong/2022_대학안전관리계획.pdf">안내</a>'
    raw = wgst.parse_detail(_detail_html(body=body), unmarked)
    assert raw.raw_text == "안내"
    assert raw.attachments == ()


def test_a_listed_attachment_that_cannot_be_found_is_an_error() -> None:
    """목록이 첨부를 표시했는데 상세에서 못 찾으면 **조용히 0개**가 아니라 에러다."""
    with pytest.raises(ParseError, match="첨부 표시"):
        wgst.parse_detail(_detail_html(body="본문만 있다"), _marked_ref("1"))


def test_an_unknown_download_call_is_an_error() -> None:
    """다운로드 방식이 바뀌면 `javascript:…` 문자열이 URL로 저장된다 — 그전에 실패시킨다."""
    broken = _file_item("이력서양식.hwp").replace("javascript:fileDown(", "javascript:newFileDown(")
    with pytest.raises(ParseError, match="fileDown"):
        wgst.parse_detail(_detail_html(files=broken), _marked_ref("1"))


def test_an_empty_file_box_is_an_error() -> None:
    """상자만 있고 링크가 없으면 셀렉터가 빗나간 것이다 — 빈 목록으로 흘리지 않는다."""
    with pytest.raises(ParseError, match="첨부 링크가 없음"):
        wgst.parse_detail(_detail_html(files="<li>이력서양식.hwp</li>"), _marked_ref("1"))
