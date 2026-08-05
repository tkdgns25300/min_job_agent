"""TTGU(횃불트리니티 Job Posting) 어댑터 — XpressEngine.

게시판 실측(2026-08-04 · fixture `tests/fixtures/TTGU/`):

```
목록  /index.php?mid=ttgu_board_03      2페이지 이상은 &page={n} (~113p)
      table.bd_lst tr 10 = 헤더 1 + 공지 1(tr.notice) + 공고 8
      칸: td.no(표시번호) td.title(a.hx + span.extraimages) td.time(YYYY.MM.DD) td.m_no(조회)
상세  같은 index.php의 &document_srl={id}
      본문 = div.rd_body(div.xe_content) · 첨부 = div.rd_file(제목 영역 · 실측)
```

⚠️ **2페이지 링크는 `&page=2&document_srl=…` 순서로 나온다**(실측). `detail_pattern`의 접두어
(`…&document_srl=`)로 URL을 뒤지면 2페이지 20건이 통째로 안 잡힌다 → **`document_srl`만 정규식으로
줍는다.** URL은 `detail_pattern`으로 다시 만들어 정규형을 유지한다.

⚠️ **고정공지가 모든 페이지에 다시 나온다**(실측: 1·2페이지 모두 `document_srl=1105532`).
`tr.notice`로 걸러내지 않으면 페이지마다 같은 id가 올라와 `as_listing`이 아니라 **원장 대조**에서
조용히 사라진다.
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
    require_attachment_evidence,
    require_date,
    require_one,
    require_some_kept,
    rows_with_data,
    structural_html,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "TTGU"

_LIST_TABLE: Final = "table.bd_lst"
#: 고정공지 — 두 신호가 독립적으로 있다(tr class + `td.no`가 "Notice"라 숫자가 아님).
_NOTICE_CLASS: Final = "notice"
_NUM_CELL: Final = "td.no"
_DETAIL_LINK: Final = "td.title a[href]"
_DATE_CELL: Final = "td.time"
#: 첨부 아이콘 자리. 상세에서 첨부 셀렉터가 빗나갔는지 교차 확인하는 **독립 신호**다.
_ATTACHMENT_ICON: Final = "span.extraimages img"
#: 목록 href에서 글번호만. 파라미터 순서에 의존하지 않는다(위 docstring 참조).
_DOCUMENT_SRL: Final = re.compile(r"document_srl=(\d+)")
_PAGE_PARAM: Final = "page"

#: 문서 본문. ⚠️ `div.xe_content`만 쓰면 **댓글 본문**(`div.fdb_lst`)까지 같은 클래스라 위험하다.
_BODY: Final = "div.rd_body"
#: 첨부 목록(파일명 + 다운로드 URL). 제목 영역(`div.rd_hd`) 안에 있고 본문 밖이다(실측).
_FILE_LIST: Final = "div.rd_file"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(쿼리에 이미 `mid`가 있다)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NOTICE_CLASS}`·td.no)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    본문이 비어도 실패로 보지 않는다 — 포스터 이미지·첨부만 올린 공고가 있고 그때는 그것이
    유일한 내용이다. 셋 다 없을 때만 파싱이 빗나간 것이다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    raw_html = structural_html(body)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(soup.select_one(_FILE_LIST), base_url=ref.url)
    require_attachment_evidence(ref, source_key=SOURCE_KEY, selector=_FILE_LIST, found=files)
    return RawPosting(
        ref=ref, raw_text=raw_text, raw_html=raw_html, image_urls=images, attachments=files
    )


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 두 신호를 독립적으로 본다(class + 표시번호 자리의 "Notice")."""
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, _NUM_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    external_id = id_from_js(
        str(link.get("href") or ""),
        pattern=_DOCUMENT_SRL,
        source_key=SOURCE_KEY,
        what="상세 링크(document_srl)",
    )
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href를 그대로 쓰지 않는다 — 2페이지 링크에는 `&page=2`가 섞여 있어 같은 글의
        # `source_url`이 페이지마다 달라진다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "views": as_int(cell_text(row, "td.m_no")),
            "display_no": cell_text(row, _NUM_CELL) or None,
            "has_attachment": bool(row.select(_ATTACHMENT_ICON)),
        },
    )
