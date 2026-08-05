"""BPU(부산장신대 청빙취업안내) 어댑터 — ASP.NET WebForms 게시판.

게시판 실측(2026-08-04 · fixture `tests/fixtures/BPU/`):

```
목록  /Board/BoardList.aspx?BoardMstNo=6&CategoryNo=1   2페이지 이상은 &PageNo={n}
      table.TableStyle03 tr 11 = 헤더 1(th) + 공지 1 + 공고 9
      칸: td(표시번호) td.Left(링크+아이콘) td(작성자) td(날짜 YYYY.MM.DD) td(조회)
      공지행의 표시번호 칸은 숫자가 아니라 `공지` 문자열이다 — class 표시가 없다.
상세  /Board/BoardView.aspx?BoardNo={id}&BoardMstNo=6&CategoryNo=1
```

⚠️ **`external_id`를 `external_id_from_url`로 뽑을 수 없다.** 목록 href의 쿼리 순서가
`?CategoryNo=…&PageNo=…&KeyWord=…&BoardNo=101350&BoardMstNo=6`으로 config의 `detail_pattern`
(`?BoardNo={id}&…`)과 다르다 — 접두사 매칭이 통째로 빗나간다. ASP.NET은 순서를 신경쓰지 않으므로
**쿼리 파라미터 이름으로** 뽑는다.

⚠️ 표시번호(3422)와 `BoardNo`(101350)는 다르다. 원장 키는 `BoardNo`다.

⚠️ **첨부 링크에 URL이 없다.** 다운로드 앵커는 `javascript:__doPostBack(…)`이라 `href`를 읽는
공용 `attachments_in`으로는 전량 유실된다. 실제 경로는 같은 `<span>`의 hidden input
`hdnFilePath`에 있다(`/Upload/Common/2026/8/….jpeg` — 직접 GET 200 image/jpeg 실측 2026-08-04).
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup, Tag

from minjob_ingest.models import Attachment
from minjob_ingest.sources.adapters.base import (
    ListRequest,
    ParseError,
    PostingRef,
    RawPosting,
    as_int,
    as_listing,
    cell_text,
    external_id_from_query,
    image_urls_in,
    normalized_text,
    parse_html,
    require_attachment_evidence,
    require_date,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "BPU"

_LIST_TABLE: Final = "table.TableStyle03"
_DETAIL_LINK: Final = "td.Left a[href]"
#: 칸에 클래스가 거의 없어 위치로 고른다(`td:nth-of-type(N)`) — 표시번호·작성자·날짜·조회 순.
_NO_CELL: Final = "td:nth-of-type(1)"
_AUTHOR_CELL: Final = "td:nth-of-type(3)"
_DATE_CELL: Final = "td:nth-of-type(4)"
_VIEWS_CELL: Final = "td:nth-of-type(5)"
#: 상세 URL이 담는 글번호의 쿼리 파라미터 이름(config `detail_pattern`과 같은 이름).
_ID_PARAM: Final = "BoardNo"
_PAGE_PARAM: Final = "PageNo"
#: 첨부가 있으면 제목 옆에 아이콘이 붙는다 — 상세 첨부 셀렉터 교차 확인용(실측 `alt="첨부파일"`).
_ATTACHMENT_ICON_ALT: Final = "첨부파일"
#: 본문(실측). ⚠️ 상세에는 게시판 이용안내(`p.tit_txt`)가 함께 있어 범위를 좁혀야 한다.
_BODY: Final = "div.contentDetail"
#: 첨부 한 건의 묶음. 이 안에 postback 앵커와 hidden input 3개가 함께 있다(실측).
_FILE_ENTRY: Final = "div.viewHead p.file span"
#: hidden input의 id 접미사 — ASP.NET이 앞에 컨트롤 경로를 붙이므로 부분 일치로 찾는다.
_FILE_PATH_INPUT: Final = 'input[id*="hdnFilePath"]'
_FILE_NAME_INPUT: Final = 'input[id*="hdnFileName"]'


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(쿼리 없이도 1페이지가 나온다 · 실측)."""
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
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NO_CELL}`이 숫자인가)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    본문이 비어 있어도 실패로 보지 않는다 — 내용을 첨부 이미지 한 장으로만 올리는 공고가 있고
    (실측: 양식 라벨만 남기고 포스터 jpeg에 다 넣는다) 그때는 그것이 유일한 증거다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = _attachments_from_postback(soup, base_url=ref.url)
    require_attachment_evidence(ref, source_key=SOURCE_KEY, selector=_FILE_ENTRY, found=files)
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _attachments_from_postback(soup: BeautifulSoup, *, base_url: str) -> tuple[Attachment, ...]:
    """hidden input에서 첨부 경로를 복원한다(모듈 docstring 참조).

    파일명은 `hdnFileName`을 쓰고 없으면 앵커 텍스트로 물러난다 — 둘 중 하나가 사라지는 개편에
    첨부가 통째로 유실되지 않게.
    """
    found = []
    for entry in soup.select(_FILE_ENTRY):
        path = _input_value(entry, _FILE_PATH_INPUT)
        if not path:
            continue
        name = _input_value(entry, _FILE_NAME_INPUT) or entry.get_text(" ", strip=True)
        found.append(Attachment(name=name or path, url=urljoin(base_url, quote(path))))
    return tuple(found)


def _input_value(entry: Tag, selector: str) -> str:
    found = entry.select_one(selector)
    return "" if found is None else str(found.get("value") or "").strip()


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 표시번호 칸이 숫자가 아니면(`공지`) 공지다 — class 표시가 없다(실측)."""
    return not cell_text(row, _NO_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = external_id_from_query(
        urljoin(source.list_url, href), param=_ID_PARAM, source_key=SOURCE_KEY
    )
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 `PageNo`·`KeyWord` 검색 상태가 붙어 있어 **정규형**으로 다시 만든다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, _AUTHOR_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NO_CELL) or None,
            "has_attachment": bool(row.select(f'img[alt="{_ATTACHMENT_ICON_ALT}"]')),
        },
    )
