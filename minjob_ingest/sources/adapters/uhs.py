"""UHS(협성대 웨슬리 교역자청빙) 어댑터 — Konnect(K2Web) CMS, BU와 다른 스킨.

게시판 실측(2026-08-04 · fixture `tests/fixtures/UHS/`):

```
목록  /gsthe/2386/subview.do                 2페이지 이상은 ?page={n} (GET으로 먹는다)
      div.tableWrap table tr 12 = 헤더 1(th) + 공지 1(tr.key-notice) + 공고 10
      칸: td.TdNumber td.al-left(링크) td.TdWriter td.TdDate td.TdAccess td.TdAtchFile
상세  /bbs/gsthe/183/{artclNo}/artclView.do
      본문 div.dataView · 첨부 div.dataForm.fileWrap
```

⚠️ **목록 표에 class가 없다**(사이트 전체에 `table`이 하나뿐 · 실측) → 감싸는 `div.tableWrap`로
지목한다. `table`만 쓰면 개편으로 표가 하나 더 생기는 순간 아무 표나 집는다.

⚠️ **같은 CMS인데 BU와 클래스 이름이 다르다**(`dataView`↔`artclView` · `new-ba`↔`newArtcl` ·
`TdDate`↔`_artclTdRdate`). 스킨이 다르기 때문이다(`bbs_skin01` vs `bu_bbs_table`) — 그래서
Konnect 어댑터를 하나로 합치지 않았다.

⚠️ **`soft_200`이 config에 없는데 실제로는 그렇게 동작한다**: 잘못된 상세 경로가 HTTP 200 +
에러 쉘("알림메세지")로 온다(실측 — `/bbs` 프리픽스를 뺀 첫 fixture가 그것이었다). 본문
셀렉터가 그 판정을 겸한다.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urljoin

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
    external_id_from_url,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_attachment_evidence,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "UHS"

#: 표에 class가 없어 감싸는 div로 지목한다(모듈 상단 주석 참조).
_LIST_TABLE: Final = "div.tableWrap table"
_TITLE_CELL: Final = "td.al-left"
_DETAIL_LINK: Final = f"{_TITLE_CELL} a[href]"
_NO_CELL: Final = "td.TdNumber"
_DATE_CELL: Final = "td.TdDate"
#: 고정공지 — 두 신호가 독립적으로 있다(tr class + 표시번호 칸이 빈 `span.key-noti`).
_NOTICE_CLASS: Final = "key-notice"
#: ⚠️ 제목 앵커 안의 "새글" 배지. 떼지 않으면 배지가 사라질 때 **같은 글의 제목이 달라진다**.
_NEW_BADGE: Final = "span.new-ba"
#: 목록의 첨부 표시. 상세 첨부 셀렉터가 빗나갔는지 교차 확인하는 **독립 신호**다.
_ATTACH_MARK: Final = "td.TdAtchFile p.dwn-btn"
_PAGE_PARAM: Final = "page"
#: 본문. 제목·작성일(`div.infoWrap`)과 첨부(`div.fileWrap`)의 형제다.
_BODY: Final = "div.dataView"
#: 첨부 목록. ⚠️ 본문을 첨부 컨테이너로 쓰지 않는다 — 본문에 외부 링크가 섞여 있다(BU 실측).
#: 미리보기 버튼은 `<input>`이라 앵커만 읽는 `attachments_in`이 알아서 건너뛴다.
_FILE_LIST: Final = "div.dataForm.fileWrap dd"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `subview.do`에 쿼리가 없어 `?`로 붙인다(1페이지는 그대로).

    ⚠️ 사이트의 `page_link()`는 `pageForm`을 POST하지만 그 폼에는 페이지마다 새로 발급되는
    `layout` 토큰이 있어 `(source, page)`만 받는 계약으로 만들 수 없다. GET이 먹는 것을
    실측했다(BU와 같은 CMS · `_curPage`가 바뀐다).
    """
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 모든 페이지에 반복되는 고정공지는 제외한다."""
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

    본문 셀렉터가 soft-200 판정을 겸한다 — 에러 쉘에는 `div.dataView`가 없다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(soup.select_one(_FILE_LIST), base_url=ref.url)
    require_attachment_evidence(ref, source_key=SOURCE_KEY, selector=_FILE_LIST, found=files)
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _is_notice(row: Tag) -> bool:
    """고정공지 행. class와 표시번호를 독립적으로 본다(공지는 번호 칸이 빈 배지다)."""
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, _NO_CELL).isdigit()


def _title_of(link: Tag) -> str:
    """제목. "새글" 배지를 **떼고** 읽는다(모듈 상단 `_NEW_BADGE` 주석 참조)."""
    working = parse_html(str(link))
    for badge in working.select(_NEW_BADGE):
        badge.decompose()
    return working.get_text(" ", strip=True)


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    href = str(link.get("href") or "").strip()
    external_id = require_numeric_id(
        external_id_from_url(
            urljoin(source.list_url, href), detail_pattern=source.detail_pattern, what=SOURCE_KEY
        ),
        source_key=SOURCE_KEY,
    )
    title = _title_of(link)
    return PostingRef(
        external_id=external_id,
        # 목록 href도 이미 정확하지만 **정규형**으로 다시 만든다 — 검색·페이지 파라미터가
        # 붙는 개편에도 `source_url`이 흔들리지 않는다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, "td.TdWriter") or None,
            "views": as_int(cell_text(row, "td.TdAccess")),
            "display_no": cell_text(row, _NO_CELL) or None,
            "has_attachment": bool(row.select(_ATTACH_MARK)),
        },
    )
