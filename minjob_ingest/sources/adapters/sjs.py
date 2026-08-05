"""SJS(서울장신대 사역구인정보) 어댑터 — 자체 PHP 게시판(EUC-KR · charset 헤더 없음).

게시판 실측(2026-08-04 · fixture `tests/fixtures/SJS/`):

```
목록  /ht_ml/w_04ed/4600.php              2페이지 이상은 `?pageno={n}`
      table.board_table tr 16 = 헤더 1 + 공지 1(tr.notice) + 공고 14 (846페이지)
      칸: td.index(번호) td.board_tit(링크) td.date(YYYY-MM-DD) td.hit(조회)
상세  /ht_ml/w_04ed/4600.php?bbs_idx={id}&pagekind=c&bbsid=main4600
      본문 = td.view_content · 첨부 = `첨부파일` 행의 td.attached
```

⚠️ **목록에 작성자·첨부 칸이 없다** — 첨부 여부를 목록에서 알 수 없어 상세 교차 확인 신호가
없다(YTUS의 `has_attachment` 같은 것). 그래서 첨부는 `td.attached`로만 판정한다.

## 첨부 실측(2026-08-05 · 상세 5건: 49798·49823·49866·50043 + detail.html)

`td.attached`가 맞다(49798 = `.hwp` 1건 · 나머지 4건은 빈 칸). 셀렉터를 바꿀 이유가 없어
**어댑터는 그대로 둔다.** 확인한 것:

```
<td class="attached"><p><img src="/lms_bbs//img/hwp.gif">      ← 파일종류 아이콘(첨부가 아니다)
  <a href="/lms_bbs/dn.php?downloadname=담임목사 청빙 서류(1차).hwp
           &filename=20260719161635.hwp&bbs_idx=49798">담임목사 청빙 서류(1차).hwp</a></p></td>
```

- 파일명은 **앵커 텍스트**에서 온다 → 확장자가 살아 `is_image`가 성립한다.
- href에 **인코딩되지 않은 공백**이 있다. `httpx`가 요청 시 `%20`으로 바꾸므로 그대로 둔다 —
  여기서 미리 인코딩하면 이미 인코딩된 부분(`(1차)`)과 이중처리 위험이 생긴다.
- 첨부 칸의 `hwp.gif`는 파일종류 아이콘이다. 본문(`td.view_content`) 밖이라
  `image_urls`로 새지 않는다 — 이미지 수집 범위를 표로 넓히면 그 순간 섞인다.

⚠️ 상세 페이지의 작성자는 `mailto:` 링크다. 본문(`td.view_content`) 밖이라 첨부로 새지 않지만,
첨부 범위를 표 전체로 넓히면 그 메일 주소와 `링크` 행의 빈 앵커가 첨부가 된다.
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
    require_one,
    require_some_kept,
    rows_with_data,
    structural_html,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "SJS"

_LIST_TABLE: Final = "table.board_table"
_DETAIL_LINK: Final = "td.board_tit a[href]"
_NUMBER_CELL: Final = "td.index"
_DATE_CELL: Final = "td.date"
_VIEWS_CELL: Final = "td.hit"
#: 고정공지 — 두 신호가 독립적으로 있다(행 class + 번호 자리의 `공지` 아이콘).
_NOTICE_CLASS: Final = "notice"
_PAGE_PARAM: Final = "pageno"
_BODY: Final = "td.view_content"
#: `첨부파일` 행의 칸. 첨부가 없으면 **빈 칸으로 존재**한다(실측) — 셀렉터 확인에 쓸 수 있다.
_ATTACHED_CELL: Final = "td.attached"
#: 상세 URL의 글번호. **`external_id_from_url`을 못 쓴다** — 목록 href는
#: `?pagetype=&bbs_idx=50069&pageno=1&…`처럼 `detail_pattern` 앞에 `pagetype`이 끼어들어
#: 접두사가 어긋난다.
_ID_IN_HREF: Final = re.compile(r"[?&]bbs_idx=(\d+)")


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(쿼리 없이 1페이지가 나온다 · 실측)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 헤더·고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_skippable_row(row)]
    require_some_kept(
        refs,
        data_rows,
        source_key=SOURCE_KEY,
        filtered_by=f"공지 판정(`{_NOTICE_CLASS}`·{_NUMBER_CELL})",
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부."""
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    raw_html = structural_html(body)
    images = image_urls_in(body, base_url=ref.url)
    # 첨부는 `첨부파일` 행 안으로 제한한다 — 모듈 docstring의 `mailto:`·빈 앵커 때문에.
    files = attachments_in(soup.select_one(_ATTACHED_CELL), base_url=ref.url)
    return RawPosting(
        ref=ref, raw_text=raw_text, raw_html=raw_html, image_urls=images, attachments=files
    )


def _is_skippable_row(row: Tag) -> bool:
    """고정공지·헤더 행. 두 신호를 독립적으로 본다(행 class + 번호가 숫자인지)."""
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, _NUMBER_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = id_from_js(href, pattern=_ID_IN_HREF, source_key=SOURCE_KEY, what="목록 href")
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 검색·페이지 파라미터가 10개 넘게 붙어 있어 **정규형**으로 다시 만든다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NUMBER_CELL) or None,
        },
    )
