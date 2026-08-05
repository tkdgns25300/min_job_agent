"""DAESHIN(대신대 취업정보) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/DAESHIN/`):

```
목록  /html/05_community/03.php          2페이지 이상은 ?b_page={n}
      table.board tr 18 = 헤더 1(th) + 공지 2 + 공고 15
      칸: td.No td.Title td.Name td.Date(YYYY.MM.DD) td.Hits
      공지행은 **각 칸에 `.notice` 클래스**가 붙는다(tr에는 표시가 없다).
상세  같은 .php의 ?AT=V&b_id={id}        목록/상세가 한 파일이고 AT 파라미터로 갈린다
```

⚠️ **취업(일반)과 사역(청빙)이 한 게시판에 섞여 있다.** 어댑터가 걸러내지 않는다 — 제목만 보고
자르면 진짜 청빙을 조용히 잃고, 그게 비용보다 나쁘다(운영자 결정 2026-08-04). 판정은 게이트1이
한다(SPEC §5).
"""

from __future__ import annotations

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
    attachments_in,
    cell_text,
    external_id_from_url,
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

SOURCE_KEY: Final = "DAESHIN"

_LIST_TABLE: Final = "table.board"
_DETAIL_LINK: Final = "td.Title a[href]"
_DATE_CELL: Final = "td.Date"
#: 공지 표시는 **칸**에 붙는다 — `tr`에는 아무 클래스도 없다(실측).
_NOTICE_CELL: Final = "td.notice"
_PAGE_PARAM: Final = "b_page"
#: 본문은 표 안의 한 칸이다(`class="Cont last"` · 실측). 그 안에 모집요강이 표로 들어 있다.
_BODY: Final = "td.Cont"
#: ⚠️ 첨부는 **본문과 다른 행**(`td.last`)에 있고, 그 칸도 `last` 클래스를 공유해 셀렉터로는
#: 본문·이전글/다음글과 구분되지 않는다. 그래서 **업로드 경로**로 판정한다(2026-08-05 실측).
#: 푸터의 사이트 공용 파일은 `/upfile/data/`라 경로가 달라 자연히 빠진다.
_ATTACHMENT_LINK: Final = 'a[href*="/upfile/board/"]'
#: 첨부 링크를 찾을 범위. 상세 본문 표 전체를 본다(푸터는 이 표 밖이다).
_DETAIL_TABLE: Final = "table.table6"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(쿼리 없이도 1페이지가 나온다 · 실측)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NOTICE_CELL}`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    ⚠️ 첨부를 **본문에서 찾지 않는다.** 본문에는 교회 홈페이지 링크가 흔하고, 그것을 첨부로
    저장하면 구조화가 엉뚱한 주소를 파일로 연다. 실제로 그 상태에서 `http://www.daechun.or.kr]/`
    (교회가 `]`를 잘못 넣은 주소)가 `urljoin`을 터뜨려 수집 전체가 중단됐다(2026-08-05).
    → 업로드 경로(`_ATTACHMENT_LINK`)로 판정한다.

    ⚠️ 목록에 첨부 표시 칸이 없어 교차 확인 신호가 없다 — 셀렉터가 빗나가면 조용히 0개가 되므로
    `detail_file.html` fixture 테스트가 유일한 방어다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    raw_html = structural_html(body)
    images = image_urls_in(body, base_url=ref.url)
    files = _attachments(soup, base_url=ref.url)
    return RawPosting(
        ref=ref, raw_text=raw_text, raw_html=raw_html, image_urls=images, attachments=files
    )


def _attachments(soup: Tag, *, base_url: str) -> tuple[Attachment, ...]:
    """업로드 경로를 가진 링크만 첨부로 본다(모듈 상단 `_ATTACHMENT_LINK` 참조)."""
    table = soup.select_one(_DETAIL_TABLE)
    if table is None:
        return ()
    links = table.select(_ATTACHMENT_LINK)
    if not links:
        return ()
    holder = parse_html("".join(str(link) for link in links))
    return attachments_in(holder, base_url=base_url)


def _is_notice(row: Tag) -> bool:
    return bool(row.select(_NOTICE_CELL))


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    external_id = external_id_from_url(
        urljoin(source.list_url, href), detail_pattern=source.detail_pattern, what=SOURCE_KEY
    )
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 링크에는 검색·페이지 파라미터가 붙어 있어 **정규형**으로 다시 만든다 —
        # 같은 글을 다른 페이지에서 만났을 때 `source_url`이 달라지면 안 된다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, "td.Name") or None,
            "views": as_int(cell_text(row, "td.Hits")),
            "display_no": cell_text(row, "td.No") or None,
        },
    )
