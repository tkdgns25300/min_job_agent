"""CALVIN(칼빈대 사역취업정보) 어댑터 — eGov 계열.

게시판 실측(2026-08-04 · fixture `tests/fixtures/CALVIN/`):

```
목록  /main/boardList.do?brd_mgrno=692&menu_no=2282   2페이지 이상은 &page_now={n}
      table.tbl_basic_list tr 11 = 헤더 1(th) + 공지 1(tr.notice) + 공고 9
      칸: td.one(표시번호) td.two.left(링크) td.three(작성자) td.four(YYYY.MM.DD) td.five(조회)
상세  /main/boardView.do?…&brd_no={id}
```

⚠️ **목록 행의 링크는 href에 URL이 없다.** `href="javascript:fView('80031731')"`이고 그 인자가
`detail_pattern`의 `brd_no`다. href를 그대로 읽는 코드는 `javascript:…`를 URL로 믿고
**조용히** 망가진다 — 그래서 `id_from_js`로 뽑는다. 원래 `fView(strRegNo, seqNo)`는 인자가
둘인데 목록은 하나만 넘긴다(실측) → 우리가 쓸 값은 첫 번째뿐이다.

⚠️ 상세는 **세션 쿠키를 요구**한다(config `needs_session` · cold 요청은 404). 그건 fetch 층이
흡수하므로 이 파일에는 흔적이 없다.
"""

from __future__ import annotations

import re
from typing import Final

from bs4 import Tag

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
    parse_html,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "CALVIN"

_LIST_TABLE: Final = "table.tbl_basic_list"
_DETAIL_LINK: Final = "td.two a[href]"
_NO_CELL: Final = "td.one"
_DATE_CELL: Final = "td.four"
#: 고정공지 — 두 신호가 독립적으로 있다(tr class + 표시번호가 "공지"). 하나가 바뀌어도 걸린다.
_NOTICE_CLASS: Final = "notice"
#: `javascript:fView('80031731')` → brd_no. 인용부호 안을 통째로 받고 숫자 검사는 따로 한다 —
#: `\d+`로 좁히면 "링크 형태가 바뀌었다"와 "함수가 사라졌다"가 같은 에러로 뭉친다.
_JS_ID: Final = re.compile(r"fView\(\s*'([^']*)'")
#: pagination은 쿼리 파라미터다(실측: `…&page_now=2`).
_PAGE_PARAM: Final = "page_now"
#: 본문 영역. 제목·작성자(`box_board_detailtop`)와 PREV/NEXT(`box_board_detailbtm`)를 **제외**한
#: 안쪽 칸이다 — 넓히면 이전글/다음글 링크가 첨부로 들어온다(실측).
_BODY: Final = "div.box_board_detailcont"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `list_url`에 이미 쿼리가 있어 `&`로 잇는다(1페이지는 그대로)."""
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    if page == 1:
        return ListRequest(url=source.list_url)
    separator = "&" if "?" in source.list_url else "?"
    return ListRequest(url=f"{source.list_url}{separator}{_PAGE_PARAM}={page}")


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs,
        data_rows,
        source_key=SOURCE_KEY,
        filtered_by=f"공지 판정(`tr.{_NOTICE_CLASS}`·{_NO_CELL})",
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    ⚠️ **이 게시판은 본문이 통째로 이미지다.** 실측 2건 모두 `raw_text`가 빈 문자열이고 내용은
    `<img src="data:image/png;base64,…">` 하나에 들어 있다(각 130~150KB) — 별도 첨부 영역이
    페이지에 아예 없다("첨부" 문자열 0회). 그래서 **빈 본문을 실패로 보지 않는다**. 셋 다
    없을 때만 파싱이 빗나간 것이다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    # ⚠️ 첨부 범위를 **본문 안으로** 제한한다 — 상세 하단(`box_board_detailbtm`)에 이전글/다음글
    # 링크가 있어 넓히면 그것이 첨부로 저장된다(실측).
    files = attachments_in(body, base_url=ref.url)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _is_notice(row: Tag) -> bool:
    """고정공지 행. class와 표시번호를 독립적으로 본다(공지는 번호 대신 "공지"가 온다)."""
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, _NO_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    external_id = _external_id_from(str(link.get("href") or ""))
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록에는 URL이 아예 없으므로 `detail_pattern`으로 **정규형**을 만든다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, "td.three") or None,
            "views": as_int(cell_text(row, "td.five")),
            "display_no": cell_text(row, _NO_CELL) or None,
        },
    )


def _external_id_from(href: str) -> str:
    """`javascript:fView('80031731')`의 인자 = `brd_no`. **표시번호(4625)가 아니다.**"""
    found = id_from_js(href, pattern=_JS_ID, source_key=SOURCE_KEY, what="목록 링크")
    return require_numeric_id(found, source_key=SOURCE_KEY)
