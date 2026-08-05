"""HTUS(호남신대 미니스트리카페 청빙) 어댑터 — 자체 PHP 게시판(EUC-KR).

게시판 실측(2026-08-04 · fixture `tests/fixtures/HTUS/`):

```
목록  /board/board.php?b_id=ministry_009        2페이지 이상은 `&page={n}`
      div.bd_content table tr 16 = 헤더 1 + 공지 5 + 공고 10 (616페이지)
      칸: td.ltxt7(번호) td.left(링크) td(글쓴이) td.ltxt6(날짜) td.file(첨부) td.ltxt6(조회)
상세  /board/board.php?b_id=ministry_009&w_id={id}   본문 = div.board_view_content
      첨부는 `첨부파일` 행의 `td.file`에 `download.php?…&fno=N` 링크로 온다(실측 w_id=24330)
```

⚠️ **헤더 행이 `th`가 아니라 `td`다** — `rows_with_data`로는 걸러지지 않는다. 번호 칸이
숫자인지 보는 규칙이 헤더와 공지(`공지`)를 한꺼번에 걸러낸다.

⚠️ **날짜와 조회수가 같은 클래스(`td.ltxt6`)를 쓴다** — 셀렉터로 구분할 수 없어 순서로 읽는다.

⚠️ **첨부 파일명 뒤에 크기가 붙는다**(`이력서_양식.hwp (33.50 KB)` · 실측). 그대로 두면
`Attachment.is_image`가 확장자를 못 읽어 이미지 첨부를 텍스트로 취급한다 → 크기를 떼어낸다.
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
    require_attachment_evidence,
    require_date,
    require_one,
    require_some_kept,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "HTUS"

_LIST_TABLE: Final = "div.bd_content table"
_NUMBER_CELL: Final = "td.ltxt7"
_DETAIL_LINK: Final = "td.left a[href]"
#: 날짜·조회수 공용 클래스. 순서로 구분한다(모듈 docstring).
_SMALL_CELL: Final = "td.ltxt6"
_AUTHOR_CELL_INDEX: Final = 2
#: 첨부 표시 칸(목록) · 첨부 링크 칸(상세). 아이콘이 CSS라 **내용이 비어 있어도** 첨부다.
_FILE_CELL: Final = "td.file"
_PAGE_PARAM: Final = "page"
_BODY: Final = "div.board_view_content"
#: 상세 URL의 글번호. **`external_id_from_url`을 못 쓴다** — 목록 href는
#: `?b_id=…&page=1&w_id=24346&m=`처럼 `detail_pattern` 사이에 `page`가 끼어들어 접두사가 어긋난다.
_ID_IN_HREF: Final = re.compile(r"[?&]w_id=(\d+)")
#: 첨부 링크 텍스트 끝의 파일 크기(`… (33.50 KB)`). 파일명에서 떼어낸다.
_SIZE_SUFFIX: Final = re.compile(r"\s*\(\s*[\d.,]+\s*[KMG]?B\s*\)\s*$", re.IGNORECASE)


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(게시판 자신도 1페이지를 `page=1`로 부른다)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 헤더·고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = table.select("tr")
    refs = [_ref_from_row(row, source) for row in data_rows if _has_posting_number(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"번호 판정(`{_NUMBER_CELL}`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부."""
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = _attachments(soup.select_one(_FILE_CELL), base_url=ref.url)
    require_attachment_evidence(ref, source_key=SOURCE_KEY, selector=_FILE_CELL, found=files)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _attachments(cell: Tag | None, *, base_url: str) -> tuple[Attachment, ...]:
    """첨부파일 행의 다운로드 링크. 파일명에서 크기 접미사를 뗀다(모듈 docstring).

    `base.attachments_in`을 쓰지 않는 이유가 그 접미사다 — 링크 텍스트를 그대로 파일명으로
    쓰면 `.hwp (33.50 KB)`가 저장돼 확장자 판정이 어긋난다.
    """
    if cell is None:
        return ()
    return tuple(
        Attachment(name=name, url=urljoin(base_url, href))
        for link in cell.select("a[href]")
        if (href := str(link.get("href") or "").strip())
        and (name := _SIZE_SUFFIX.sub("", link.get_text(" ", strip=True)))
    )


def _has_posting_number(row: Tag) -> bool:
    """번호 칸이 숫자인 행만 공고다 — 헤더(`번호`)와 공지(`공지`)를 함께 걸러낸다."""
    return cell_text(row, _NUMBER_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = id_from_js(href, pattern=_ID_IN_HREF, source_key=SOURCE_KEY, what="목록 href")
    # 링크 텍스트만 제목이다 — 칸에는 `새글` 아이콘이 형제로 붙어 있다(실측).
    title = link.get_text(" ", strip=True)
    small = row.select(_SMALL_CELL)
    posted = small[0].get_text(" ", strip=True) if small else ""
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 `page`·`m` 파라미터가 붙어 있어 **정규형**으로 다시 만든다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(posted, source_key=SOURCE_KEY, cell=_SMALL_CELL),
        list_meta={
            "list_title": title,
            "list_date": posted or None,
            "author": _author(row),
            # 날짜와 같은 클래스라 **마지막** 칸이 조회수다(모듈 docstring).
            "views": as_int(small[-1].get_text(" ", strip=True)) if len(small) > 1 else None,
            "display_no": cell_text(row, _NUMBER_CELL) or None,
            # 상세에서 첨부 링크가 빗나갔는지 교차 확인하는 독립 신호. 아이콘이 CSS라
            # 칸이 비어 있어도 클래스만으로 판정한다(실측).
            "has_attachment": bool(row.select(_FILE_CELL)),
        },
    )


def _author(row: Tag) -> str | None:
    """글쓴이 칸에는 클래스가 없어 위치로 읽는다(실측: 번호·제목 다음)."""
    cells = row.find_all("td", recursive=False)
    if len(cells) <= _AUTHOR_CELL_INDEX:
        return None
    return cells[_AUTHOR_CELL_INDEX].get_text(" ", strip=True) or None
