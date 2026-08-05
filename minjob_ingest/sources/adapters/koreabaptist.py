"""KOREABAPTIST(기독교한국침례회총회 목회자청빙) 어댑터 — Dimode 게시판.

게시판 실측(2026-08-04 · fixture `tests/fixtures/KOREABAPTIST/`):

```
목록  /Board/Index/21317          2페이지 이상은 ?page={n} (28p)
      ⚠️ 21317은 **게시판 식별자**다(글번호가 아니다).
      table.table-hover tr 16 = 헤더 1 + 공고 15 (공지 없음)
      칸: td.document-number td.document-title(a + span.file) td.document-writer
          td.document-regdate(YYYY-MM-DD) td.document-readedcount
상세  /Board/Detail/21317/{id}    본문 = div.detail-content
```

⚠️ **상세 페이지가 목록을 다시 품고 있다**(`div.list-in-detail`에 같은 `table.table-hover`).
본문·첨부 범위를 `div.detail-content` 밖으로 넓히면 그 15행의 링크가 통째로 첨부로 들어온다.

⚠️ **목록의 `span.file`(💾 아이콘)은 별도 첨부함이 아니라 본문에 박힌 이미지를 뜻한다**(실측:
580912는 `div.detail-content > img` 한 장, 첨부 목록 컨테이너 자체가 없다). 그래서 첨부 교차
확인은 `attachments`가 아니라 **이미지까지 함께** 본다.

⚠️ **2페이지 링크에는 `?page=2`가 붙는다**(실측: `/Board/Detail/21317/560567?page=2`).
id 추출은 `?`에서 멈추고 URL은 `detail_pattern`으로 다시 만들어 정규형을 유지한다.
"""

from __future__ import annotations

from typing import Final

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
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "KOREABAPTIST"

_LIST_TABLE: Final = "table.table-hover"
_NUM_CELL: Final = "td.document-number"
_DETAIL_LINK: Final = "td.document-title a[href]"
_DATE_CELL: Final = "td.document-regdate"
#: 첨부(=본문 이미지) 표시. 상세에서 이미지 셀렉터가 빗나갔는지 보는 **독립 신호**다.
_ATTACHMENT_ICON: Final = "td.document-title span.file"
#: 실측 확인(2026-08-04): `?page=2` → 선택된 페이지가 2로 바뀌고 다른 15건이 나온다.
_PAGE_PARAM: Final = "page"

#: 본문. ⚠️ 상세 페이지에 목록이 또 있으므로 범위를 여기서 넓히지 않는다(위 docstring).
_BODY: Final = "div.detail-content"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(경로 끝의 21317은 게시판 식별자다)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들.

    ⚠️ 실측한 1·2페이지에는 고정공지가 없었다. 그래도 **번호 칸이 숫자가 아닌 행**은 걸러낸다 —
    이 테마가 공지를 붙이면 번호 자리에 "공지"가 들어오고(31곳 공통 관습), 그때 조용히 섞이는
    것보다 걸러내는 편이 안전하다. 걸러서 0건이 되면 `require_some_kept`가 실패로 알린다.
    """
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NUM_CELL}`이 숫자인가)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    본문이 비어도 실패로 보지 않는다 — **이 게시판은 포스터 이미지 한 장이 공고 전체인 경우가
    많다**(fetch_note). 그때 이미지가 유일한 증거이고 구조화가 멀티모달로 읽는다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(body, base_url=ref.url)
    _check_files_found(ref, images=images, files=files)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _check_files_found(
    ref: PostingRef, *, images: tuple[str, ...], files: tuple[Attachment, ...]
) -> None:
    """목록의 💾 아이콘과 대조한다.

    이미지가 공고 내용의 전부인 게시판이라 이미지 셀렉터가 빗나가면 **내용이 통째로 비는데도**
    "본문 짧은 공고"로 통과한다. 목록이 이미 알려주는 신호를 버리지 않는다.
    """
    if ref.list_meta.get("has_attachment") and not (images or files):
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 목록에 첨부 표시가 있는데 이미지·첨부가 비었음 —"
            f" 셀렉터 `{_BODY}` 확인"
        )


def _is_notice(row: Tag) -> bool:
    """번호 칸이 숫자가 아닌 행(= 고정공지 관습)."""
    return not cell_text(row, _NUM_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    external_id = require_numeric_id(
        external_id_from_url(
            str(link.get("href") or ""), detail_pattern=source.detail_pattern, what=SOURCE_KEY
        ),
        source_key=SOURCE_KEY,
    )
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href를 그대로 쓰지 않는다 — 2페이지 링크에는 `?page=2`가 붙어 같은 글의
        # `source_url`이 페이지마다 달라진다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, "td.document-writer") or None,
            "views": as_int(cell_text(row, "td.document-readedcount")),
            "display_no": cell_text(row, _NUM_CELL) or None,
            "has_attachment": bool(row.select(_ATTACHMENT_ICON)),
        },
    )
