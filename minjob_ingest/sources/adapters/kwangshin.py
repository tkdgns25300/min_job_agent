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
      table.view_tb tr = 머리(tr.tbg) + 본문(td.details) + **첨부가 있으면** td.file
```

⚠️ **칸에 클래스가 없어 위치로 읽는다.** 그래서 칸 수를 먼저 확인한다 — 칸이 하나 늘면
날짜와 조회수가 밀려 `require_date`가 조회수를 날짜로 파싱하려 든다.

⚠️ **값에 라벨이 붙어 있다**(`No.2264` · `글쓴이 : ` · `조회수 : `). 라벨을 떼지 않으면
`views`가 늘 `None`이 되고 `display_no`에 접두어가 남는다.

⚠️ 상세 링크는 href가 아니라 `javascript:fView('42706')`이고 그 인자가 `brd_no`다 —
href를 그대로 URL로 믿는 코드는 조용히 망가진다.

## 첨부 실측(2026-08-05 · 7건 표본: 42680·42688·42694·42700·42702·42705 + detail.html)

첨부 영역은 **`td.file`** 이고 첨부가 있는 공고에만 그 행이 생긴다(42680 = `.hwp` 1건).
표본 7건 중 그 한 건뿐이라 목록에도 첨부 표시가 없어(아이콘 0개) 교차 신호가 없다 —
그래서 **`td.file` 행이 있는데 링크를 못 뽑으면 실패**시켜 셀렉터 이상을 드러낸다.

⚠️ 첨부 다운로드도 href가 아니라 `javascript:download(18745)`다. 실제 경로는
`/common/download.do?file_no=18745`(`/js/n1Util.js`의 `download()` 실측) — 변환하지 않으면
`javascript:…` 문자열이 URL로 저장돼 구조화가 첨부를 열 수 없다. 파일명은 링크 텍스트뿐이다.

⚠️ **본문 안 링크는 첨부가 아니다.** 예전 코드가 `td.details`에서 첨부를 긁어 교회 홈페이지
(`http://osongch.kr/`·`https://www.ygdch.com/`)가 첨부로 저장됐다(표본 4/7건) — 첨부는
`td.file`에서만 온다. 본문에 붙여넣은 이미지는 `/userfiles/image/…`로 렌더되므로
`image_urls`가 이미 담는다(42700 실측 — 본문이 "첨부파일로 참조바랍니다"라고만 쓰인 공고).

⚠️ 형제 보드 CALVIN처럼 본문을 `data:image/png;base64` 하나로 내려주는 사례는 **없었다**
(표본 7건 모두 `data:image` 0회 · 본문 텍스트 229~978자). 같은 `fView` 계열이지만 상세
스킨이 다르다(CALVIN은 `div.box_board_detailcont`).
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urljoin

from bs4 import Tag

from minjob_ingest.models import Attachment
from minjob_ingest.sources.adapters.base import (
    ListRequest,
    ParseError,
    PostingRef,
    RawPosting,
    as_int,
    as_listing,
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
    structural_html,
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
#: 첨부 영역. **첨부가 있는 공고에만 이 행이 생긴다**(모듈 상단 첨부 실측 참조).
_FILE_CELL: Final = "td.file"
_FILE_LINK: Final = "a[href]"
#: `javascript:download(18745)` → `file_no`.
_FILE_NO: Final = re.compile(r"download\(\s*(\d+)\s*\)")
#: 실제 다운로드 경로(`/js/n1Util.js`의 `download()` 실측).
_DOWNLOAD_PATH: Final = "/common/download.do?file_no="


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
    raw_html = structural_html(body)
    images = image_urls_in(body, base_url=ref.url)
    # ⚠️ 첨부는 `td.file`에서만 온다 — 본문에서 긁으면 교회 홈페이지가 첨부가 된다(모듈 상단).
    files = _attachments(soup.select_one(_FILE_CELL), base_url=ref.url)
    return RawPosting(
        ref=ref, raw_text=raw_text, raw_html=raw_html, image_urls=images, attachments=files
    )


def _attachments(cell: Tag | None, *, base_url: str) -> tuple[Attachment, ...]:
    """`td.file` 안의 다운로드 링크 → (파일명, 절대 URL).

    URL이 href에 없다 — `javascript:download(<file_no>)`를 실제 경로로 바꾼다(모듈 상단).
    행이 있는데 링크를 하나도 못 뽑으면 **실패시킨다**: 목록에 첨부 표시가 없어 교차 신호가
    없으므로, 조용히 빈 목록을 돌려주면 첨부만 있는 공고를 통째로 잃고도 아무도 모른다.
    """
    if cell is None:
        return ()
    found: list[Attachment] = []
    for link in cell.select(_FILE_LINK):
        href = str(link.get("href") or "")
        matched = _FILE_NO.search(href)
        if matched is None:
            raise ParseError(f"{SOURCE_KEY}: 첨부 링크에서 file_no를 못 뽑음 ({href[:60]!r})")
        name = link.get_text(" ", strip=True) or f"file_{matched.group(1)}"
        found.append(
            Attachment(name=name, url=urljoin(base_url, _DOWNLOAD_PATH + matched.group(1)))
        )
    if not found:
        raise ParseError(
            f"{SOURCE_KEY}: `{_FILE_CELL}` 행은 있는데 첨부 링크가 없음 —"
            f" 셀렉터 `{_FILE_LINK}` 확인(사이트 개편 의심)"
        )
    return tuple(found)


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
