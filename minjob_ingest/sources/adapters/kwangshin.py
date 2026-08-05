"""KWANGSHIN(광신대 구인게시판) 어댑터 — eGov 계열(CALVIN과 같은 `fView`·`page_now`).

게시판 실측(2026-08-04 · fixture `tests/fixtures/KWANGSHIN/`):

```
목록  /front/boardList.do?brd_mgrno=184&menu_no=467   2페이지 이상은 &page_now={n}
      table.list_tb2 tr 10 = 전부 공고(헤더 행도 공지 행도 없다)
      칸이 **클래스가 없다**: td|td|td
        td1  <span>No.2264</span> <p><a href="javascript:fView('42706')">제목</a></p>
             <span class="writer">글쓴이 : 안상민</span>
        td2  <span>2026.08.04</span>
        td3  <span>조회수 : 25</span>
상세  /front/boardView.do?…&brd_no={id}
```

⚠️ **칸에 클래스가 없어 위치로 읽는다.** 그래서 칸 수를 먼저 확인한다 — 칸이 하나 늘면
날짜와 조회수가 밀려 `require_date`가 조회수를 날짜로 파싱하려 든다.

⚠️ **값에 라벨이 붙어 있다**(`No.2264` · `글쓴이 : ` · `조회수 : `). 라벨을 떼지 않으면
`views`가 늘 `None`이 되고 `display_no`에 접두어가 남는다.

⚠️ 상세 링크는 href가 아니라 `javascript:fView('42706')`이고 그 인자가 `brd_no`다 —
href를 그대로 URL로 믿는 코드는 조용히 망가진다.
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
    page_query_request,
    parse_html,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "KWANGSHIN"

_LIST_TABLE: Final = "table.list_tb2"
_DETAIL_LINK: Final = "a[href]"
_WRITER: Final = "span.writer"
#: 칸이 무클래스라 위치로 읽는다. 순서: 제목묶음 · 게시일 · 조회수.
_CELLS_PER_ROW: Final = 3
_TITLE_CELL: Final = 0
_DATE_CELL_INDEX: Final = 1
_VIEWS_CELL: Final = 2
#: 값에 붙어 있는 라벨(실측). 떼지 않으면 숫자 변환이 전부 실패한다.
_NO_PREFIX: Final = "No."
_WRITER_LABEL: Final = "글쓴이"
_VIEWS_LABEL: Final = "조회수"
#: `javascript:fView('42706')` → brd_no. 숫자 검사는 따로 해서 "함수가 사라졌다"와
#: "id 형태가 바뀌었다"를 다른 에러로 남긴다.
_JS_ID: Final = re.compile(r"fView\(\s*'([^']*)'")
_PAGE_PARAM: Final = "page_now"
#: 본문. 상세도 표 하나(`table.view_tb`)이고 제목·작성자 행 아래의 `colspan` 칸이 본문이다.
#: 표 전체를 잡으면 제목·작성자·조회수가 `raw_text`에 섞여 구조화가 본문으로 오독한다.
_BODY: Final = "td.details"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `list_url`에 이미 쿼리가 있어 `&`로 잇는다(1페이지는 그대로)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들.

    이 fixture에는 고정공지가 없다(표시번호가 10행 모두 연속) — 그래도 공지가 생기면 표시번호
    자리에 숫자가 아닌 값이 오는 계열이므로(CALVIN 실측) 그것을 기준으로 걸러낸다.
    """
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NO_PREFIX}<숫자>`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    본문이 비어 있어도 실패로 보지 않는다 — 내용을 이미지로만 올린 공고가 있고 그때는 그것이
    유일한 증거다. 셋 다 없을 때만 파싱이 빗나간 것이다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    # ⚠️ 첨부 범위를 **본문 안으로** 제한한다 — 별도 첨부 영역이 없는 게시판이라(실측) 밖으로
    # 넓히면 페이지의 사이트 공용 링크가 첨부로 저장된다(DAESHIN 실측).
    files = attachments_in(body, base_url=ref.url)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _cells(row: Tag) -> list[Tag]:
    """행의 칸들. **칸 수가 맞는지 먼저 확인한다** — 위치로 읽으므로 하나 밀리면 값이 뒤바뀐다."""
    found = row.find_all("td", recursive=False)
    if len(found) < _CELLS_PER_ROW:
        raise ParseError(
            f"{SOURCE_KEY}: 목록 행의 칸이 {len(found)}개 —"
            f" {_CELLS_PER_ROW}개를 기대한다(칸이 무클래스라 위치로 읽는다)"
        )
    return found


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 표시번호가 `No.<숫자>` 형태가 아니면 공지로 본다."""
    return not _display_no(_cells(row)[_TITLE_CELL]).isdigit()


def _display_no(title_cell: Tag) -> str:
    """`No.2264` → `2264`. 첫 `<span>`이다(`span.writer`는 뒤에 온다 · 실측)."""
    for span in title_cell.select("span"):
        text = span.get_text(" ", strip=True)
        if text.startswith(_NO_PREFIX):
            return text.removeprefix(_NO_PREFIX).strip()
    return ""


def _value_after(text: str, label: str) -> str:
    """`글쓴이 : 안상민` → `안상민`.

    라벨이 없으면 **원문을 그대로** 돌려준다 — 이 값들은 `list_meta`(부수 정보)에만 들어가므로
    라벨 문구가 바뀌었다고 수집을 세우지 않는다. 대신 원문이 남아 사후에 알아볼 수 있다.
    """
    trimmed = text.strip()
    if not trimmed.startswith(label):
        return trimmed
    # 라벨과 값 사이는 `글쓴이 : `처럼 공백이 섞이고 게시판이 `&nbsp;`를 쓰기도 한다 →
    # 콜론 하나만 명시적으로 떼고 나머지는 공백 처리에 맡긴다.
    return trimmed.removeprefix(label).lstrip().removeprefix(":").strip()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    cells = _cells(row)
    title_cell = cells[_TITLE_CELL]
    link = title_cell.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    external_id = _external_id_from(str(link.get("href") or ""))
    # 제목 앵커 안에 "새글" 아이콘(`list_new.jpg`)이 들어 있다 — `get_text`가 alt를 읽지 않아
    # 제목에는 섞이지 않는다(실측).
    title = link.get_text(" ", strip=True)
    date_text = cells[_DATE_CELL_INDEX].get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록에는 URL이 아예 없으므로 `detail_pattern`으로 **정규형**을 만든다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(date_text, source_key=SOURCE_KEY, cell=f"td[{_DATE_CELL_INDEX}]"),
        list_meta={
            "list_title": title,
            "list_date": date_text or None,
            "author": _value_after(cell_text(title_cell, _WRITER), _WRITER_LABEL) or None,
            "views": as_int(
                _value_after(cells[_VIEWS_CELL].get_text(" ", strip=True), _VIEWS_LABEL)
            ),
            "display_no": _display_no(title_cell) or None,
        },
    )


def _external_id_from(href: str) -> str:
    """`javascript:fView('42706')`의 인자 = `brd_no`. **표시번호(2264)가 아니다.**"""
    found = id_from_js(href, pattern=_JS_ID, source_key=SOURCE_KEY, what="목록 링크")
    return require_numeric_id(found, source_key=SOURCE_KEY)
