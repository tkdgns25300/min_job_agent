"""SUNGKYUL(예수교대한성결교회 총회 구인/청빙) 어댑터 — NOS-Board.

게시판 실측(2026-08-04 · fixture `tests/fixtures/SUNGKYUL/`):

```
목록  /NOS-Board/bbs.php?idx=com9        2페이지 이상은 &page={n}
      table.boardlist01 tr 18 = 헤더 1(th) + 공지 2 + 공고 15
      칸: 1 번호(td.nope) · 2 제목(td.board-tit) · 3 작성자 · 4 작성일(YYYY.MM.DD) · 5 조회
상세  https://www.sungkyul.org/NOS-Board/bbs.php?uid={id}&idx=com9&retype=view
      본문 = div.board_cont · 첨부 = 상세 표의 "첨부파일" 행
```

⚠️ **`uid`가 id다** — 목록 표시번호(4854)·href의 `articlenum`(4853)과 **셋 다 다르다**(실측).
원장 키가 되어야 하는 것은 DB 고유값인 `uid`(8185)다.

⚠️ **목록은 apex(`sungkyul.org`), 상세는 `www.` 절대 URL이다**(config `detail_pattern`).
목록 href는 상대 경로라 그대로 절대화하면 apex가 붙는다 → 같은 글이 호스트 두 개로 갈린다.
그래서 URL은 항상 `detail_url()`로 만든다.

⚠️ **없는 `uid`를 요청하면 200 + 본문 없는 껍데기 페이지를 준다**(실측). 그래서 본문 셀렉터가
없으면 실패로 다뤄야 한다 — `require_one`이 그 역할을 한다.

**첨부 실측(2026-08-05 · uid 8155·8178·8167 조사 · fixture `detail_file.html` = 8155)**:
`첨부파일` 행의 `<td>`에 `<a class="a3" href="./down.php?idx=com9&uid={id}&num=N">파일명</a>`이
` | `로 이어진다(8155는 2건 · 8178은 1건 · 8167은 빈 칸). **파일명은 링크 텍스트에만** 있다 —
다운로드 URL은 `num=1`뿐이라 URL에서는 확장자를 알 수 없고 `is_image` 판정이 깨진다.
그래서 `attachments_in`(링크 텍스트 우선)이 맞는 도구다. 이 행은 **형식과 무관하게** 파일을
나열하고(txt·hwpx·pdf 실측), 상세 HTML 어디에도 `file`·`attach`를 담은 class·id가 없다 —
KTS의 `#bo_v_img` 같은 **별도 이미지 미리보기 상자가 이 스킨에는 없다**(실측 4건 전부 —
8185·8167·8155·8178). 이미지 첨부가 달린 공고는 표본에서 만나지 못했다.
"""

from __future__ import annotations

import re
from typing import Final

from bs4 import BeautifulSoup, Tag

from minjob_ingest.sources.adapters.base import (
    ListRequest,
    ParseError,
    PostingRef,
    RawPosting,
    as_int,
    as_listing,
    attachments_in,
    cell_text,
    id_from_js,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_date,
    require_one,
    require_some_kept,
    rows_with_data,
    structural_html,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "SUNGKYUL"

_LIST_TABLE: Final = "table.boardlist01"
#: 번호·조회수 둘 다 `td.nope`라 class로는 못 가른다 → 위치로 읽는다.
_NUM_CELL: Final = "td:nth-of-type(1)"
_WRITER_CELL: Final = "td:nth-of-type(3)"
_DATE_CELL: Final = "td:nth-of-type(4)"
_VIEWS_CELL: Final = "td:nth-of-type(5)"
_DETAIL_LINK: Final = "td.board-tit a[href]"
#: 고정공지 — 두 신호가 독립적으로 있다(번호 자리가 "공지" + href의 `articlenum=공지`).
_NOTICE_MARKER: Final = "공지"
_NOTICE_IN_HREF: Final = f"articlenum={_NOTICE_MARKER}"
#: 목록 href의 `uid=8185`. 표시번호·articlenum이 아니라 이 값이 원장 키다(위 docstring).
_UID: Final = re.compile(r"uid=(\d+)")
_PAGE_PARAM: Final = "page"

_BODY: Final = "div.board_cont"
#: 상세 표(제목·첨부가 머리글 있는 행으로 들어 있다). **표 전체를 첨부 범위로 쓰지 않는다.**
_VIEW_TABLE: Final = "table.board-view"
_ATTACHMENT_HEADER: Final = "첨부파일"
_TITLE_HEADER: Final = "제목"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(쿼리에 이미 `idx`가 있다)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NOTICE_MARKER}`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    본문 컨테이너가 없으면 실패다 — 없는 글도 200을 주는 게시판이라(위 docstring) 빈 결과로
    흘리면 껍데기 페이지가 정상 레코드로 저장된다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = "\n\n".join(part for part in (_full_title(soup), normalized_text(body)) if part)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(_cell_after_header(soup, _ATTACHMENT_HEADER), base_url=ref.url)
    return RawPosting(
        ref=ref,
        raw_text=raw_text,
        # 제목은 `ref.title`에 이미 있으므로 구조는 본문만 담는다.
        raw_html=structural_html(body),
        image_urls=images,
        attachments=files,
    )


def _full_title(soup: BeautifulSoup) -> str:
    """상세 표의 제목. 없으면 빈 문자열(본문만으로도 레코드는 성립한다).

    ⚠️ **목록 제목은 26자에서 잘린다**(실측: `…청빙합니...`). 전체 제목이 남는 곳은 여기뿐이라
    본문 앞에 붙여 보존한다 — 구조화가 읽는 것은 `raw_text`다.
    """
    cell = _cell_after_header(soup, _TITLE_HEADER)
    if cell is None:
        return ""
    for span in cell.select("span"):  # 게시일이 제목과 **같은 칸**에 들어 있다(실측)
        span.decompose()
    return cell.get_text(" ", strip=True)


def _cell_after_header(soup: BeautifulSoup, header_text: str) -> Tag | None:
    """상세 표에서 머리글이 `header_text`인 행의 값 칸. 없으면 `None`.

    ⚠️ 표 전체를 첨부 범위로 쓰면 **이전글·다음글 링크가 첨부로 저장된다**(실측: 두 행이 같은
    표 안에 있다). 그래서 머리글로 그 행만 집어낸다.
    """
    table = soup.select_one(_VIEW_TABLE)
    if table is None:
        return None
    for row in table.select("tr"):
        header = row.select_one("th")
        if header is not None and header.get_text(" ", strip=True) == header_text:
            return row.select_one("td")
    return None


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 번호 자리와 링크의 `articlenum`을 독립적으로 본다."""
    link = row.select_one(_DETAIL_LINK)
    href = "" if link is None else str(link.get("href") or "")
    return cell_text(row, _NUM_CELL) == _NOTICE_MARKER or _NOTICE_IN_HREF in href


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    external_id = id_from_js(
        str(link.get("href") or ""), pattern=_UID, source_key=SOURCE_KEY, what="상세 링크(uid)"
    )
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href를 절대화하면 apex 호스트가 붙는다 — 상세는 www가 정본이다(위 docstring).
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, _WRITER_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NUM_CELL) or None,
        },
    )
