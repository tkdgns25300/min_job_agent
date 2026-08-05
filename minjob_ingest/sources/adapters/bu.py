"""BU(백석대 대학원 정보나눔터) 어댑터 — Konnect(K2Web) CMS.

게시판 실측(2026-08-04 · fixture `tests/fixtures/BU/`):

```
목록  /graduateschool/3938/subview.do        2페이지 이상은 ?page={n} (GET으로 먹는다)
      table.artclTable tr 15 = 헤더 1(th) + 공지 4(tr.headline) + 공고 10
      칸: td._artclTdNum td._artclTdTitle td._artclTdWriter td._artclTdRdate td._artclTdAccess
      첨부 표시: 제목 칸의 span.attach_file("첨부파일이 N 개 있음")
상세  /bbs/graduateschool/1110/{artclNo}/artclView.do
      본문 div.artclView · 첨부 dd.artclInsert(div.artclItem.viewForm 안)
```

⚠️ **페이징은 `page_link()`가 `pageForm`을 POST하지만 우리는 GET `?page=N`을 쓴다.** 그 폼에는
페이지마다 새로 발급되는 `layout` 토큰이 들어 있어 `(source, page)`만 받는 계약으로 만들 수
없다. GET이 실제로 먹는 것을 실측했다(`_curPage`가 2로 바뀌고 표시번호가 2459→2449).

⚠️ **공지 4건은 모든 페이지에 반복 노출된다** — 걸러내지 않으면 페이지마다 같은 글을 다시
수집하고 external_id가 중복된다.

⚠️ `soft_200`(config): 없는 상세도 **HTTP 200 + 에러 쉘**을 준다. 그래서 상태코드가 아니라
본문 셀렉터로 판정한다 — 에러 쉘에는 `div.artclView`가 없어 `ParseError`가 난다.
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
    parse_html,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "BU"

_LIST_TABLE: Final = "table.artclTable"
_DETAIL_LINK: Final = "td._artclTdTitle a[href]"
_TITLE_CELL: Final = "td._artclTdTitle"
_NO_CELL: Final = "td._artclTdNum"
_DATE_CELL: Final = "td._artclTdRdate"
#: 고정공지 — 두 신호가 독립적으로 있다(tr class + 표시번호가 "일반공지").
_NOTICE_CLASS: Final = "headline"
#: ⚠️ 제목 앵커 안의 "새글" 배지. 떼지 않으면 제목에 `새글`이 붙어 **며칠 뒤 배지가 사라질 때
#: 같은 글의 제목이 달라진다**(원장 경보가 헛울린다 · SPEC §4).
_NEW_BADGE: Final = "span.newArtcl"
#: 목록의 첨부 표시. 상세 첨부 셀렉터가 빗나갔는지 교차 확인하는 **독립 신호**다.
_ATTACH_MARK: Final = "span.attach_file"
_PAGE_PARAM: Final = "page"
#: 본문. 제목(`artclViewTitleWrap`)·메타(`artclViewHead`)·이전다음글(`artclNavi`)의 형제다.
_BODY: Final = "div.artclView"
#: 첨부 목록. ⚠️ **본문을 첨부 컨테이너로 쓰면 안 된다** — 이 게시판 본문에는 이메일·홈페이지
#: 링크가 그대로 들어 있어(실측: `https://hwapyungsong@naver.com/`) 첨부로 저장된다.
_FILE_LIST: Final = "div.artclItem.viewForm dd.artclInsert"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `subview.do`에 쿼리가 없어 `?`로 붙인다(1페이지는 그대로)."""
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    if page == 1:
        return ListRequest(url=source.list_url)
    separator = "&" if "?" in source.list_url else "?"
    return ListRequest(url=f"{source.list_url}{separator}{_PAGE_PARAM}={page}")


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

    본문 셀렉터가 `soft_200` 판정을 겸한다 — 에러 쉘(HTTP 200)에는 `div.artclView`가 없다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(soup.select_one(_FILE_LIST), base_url=ref.url)
    _check_attachments_found(ref, files=files)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _check_attachments_found(ref: PostingRef, *, files: tuple[Attachment, ...]) -> None:
    """목록이 "첨부 있음"이라고 한 글에서 첨부가 0개면 에러.

    `_FILE_LIST`가 빗나가면 첨부가 **조용히** 0개가 되고, 본문이 있는 공고(대다수)는
    "정상인데 첨부 0개"로 통과한다. 목록에 이미 독립 신호가 있으니 쓴다.
    """
    if ref.list_meta.get("has_attachment") and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 목록은 첨부가 있다고 표시했는데 0개 —"
            f" 셀렉터 `{_FILE_LIST}` 확인"
        )


def _is_notice(row: Tag) -> bool:
    """고정공지 행. class와 표시번호를 독립적으로 본다(공지는 번호 대신 "일반공지")."""
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
        # 목록 href는 `/bbs` 프리픽스까지 이미 정확하지만 **정규형**으로 다시 만든다 —
        # 검색·페이지 파라미터가 붙는 개편에도 `source_url`이 흔들리지 않는다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, "td._artclTdWriter") or None,
            "views": as_int(cell_text(row, "td._artclTdAccess")),
            "display_no": cell_text(row, _NO_CELL) or None,
            "has_attachment": bool(row.select(f"{_TITLE_CELL} {_ATTACH_MARK}")),
        },
    )
