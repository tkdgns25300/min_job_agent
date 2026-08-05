"""HAPSHIN(합신대 교역자초빙) 어댑터 — 그누보드 + 부트스트랩 커스텀 스킨.

게시판 실측(2026-08-04 · fixture `tests/fixtures/HAPSHIN/`):

```
목록  /bbs/board.php?bo_table=e03        2페이지 이상은 &page={n}
      table.list-pc · tr 17 = 헤더 1 + 공지 1 + 공고 15 (2페이지는 공지 없이 15)
      칸: 번호 | 제목(td.list-subject) | 작성자 | 등록일 | 조회 — 등록일·조회 칸의 클래스가
      같아서(`text-center en font-11`) **위치로만** 구분된다.
상세  같은 board.php의 &wr_id={id}       본문 = div.view-content
```

⚠️ **`fetch_note`·`tr.bo_notice` 가정과 다르다**(2026-08-04 재실측): 공지는 6건이 아니라
**1건**이고, 표시는 `tr.bo_notice`가 아니라 `td.list-subject.notice`(+ `tr.active`)다.

⚠️ **목록 게시일에 연도가 없다** — 오늘 글은 `15:36`, 그 이전 글은 `08.02`(점 구분!)다.
연도 복원은 KTS의 `gnuboard_list_date`를 함께 쓴다(그누보드 계열 공용 · base.py 공용화 후보).

⚠️ 도메인은 `hapdong.ac.kr`이지만 교단은 예장**합신**이다(합동 아님 · config `fetch_note`).
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
    external_id_from_url,
    gnuboard_list_date,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "HAPSHIN"

_LIST_TABLE: Final = "table.list-pc"
#: 고정공지 — 제목 칸의 `notice` 클래스와 번호 아이콘(`span.wr-notice`)이 함께 붙는다(실측).
#: `tr.active`도 공지에만 있으나 부트스트랩 공용 클래스라 판정 근거로 쓰지 않는다.
_NOTICE_CELL: Final = "td.list-subject.notice"
_NOTICE_ICON: Final = "span.wr-notice"
_DETAIL_LINK: Final = "td.list-subject a[href]"
_NO_CELL: Final = "td:nth-of-type(1)"
_AUTHOR_CELL: Final = "td:nth-of-type(3)"
_DATE_CELL: Final = "td:nth-of-type(4)"
_VIEWS_CELL: Final = "td:nth-of-type(5)"
_PAGE_PARAM: Final = "page"

_VIEW: Final = "div.view-wrap"
_BODY: Final = "div.view-content"
#: 첨부 유무를 알려주는 **독립 신호**. 첨부가 없으면 머리 패널에 `no-attach`가 붙는다(실측).
_VIEW_HEAD: Final = "div.view-head"
_NO_ATTACH_CLASS: Final = "no-attach"
#: 첨부 링크는 스킨 클래스가 아니라 **그누보드 공용 다운로드 경로**로 찾는다 — 첨부가 달린
#: 공고를 실측하지 못했고(1페이지 15건 전부 `no-attach`), 경로는 스킨이 바뀌어도 남는다.
_FILE_LINK: Final = 'a[href*="download.php"]'
_FILE_HREF: Final = re.compile(r"(?:https?://[^\s'\"]+|/)[^\s'\"]*download\.php[^\s'\"]*")
_UNNAMED_FILE: Final = "attachment"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(쿼리 없이 1페이지가 나온다 · 실측)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NOTICE_CELL}`·빈 번호)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부."""
    soup = parse_html(html)
    view = require_one(soup, _VIEW, what=f"{SOURCE_KEY} 상세")
    body = require_one(view, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = _attachments(view, base_url=ref.url)
    _check_attachments_found(view, ref, files=files)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _attachments(view: Tag, *, base_url: str) -> tuple[Attachment, ...]:
    """첨부 = 상세 안의 다운로드 링크. **범위를 상세로 제한**한다(푸터의 사이트 공용 파일 배제).

    href가 `javascript:` 래퍼일 수도 있어(같은 그누보드인 PCK가 그렇다) 문자열에서 실제 경로를
    꺼낸다. 못 꺼내면 조용히 버리지 않고 실패한다 — 첨부 유실은 사후에 알 수 없다.
    """
    found: list[Attachment] = []
    for link in view.select(_FILE_LINK):
        href = str(link.get("href") or "")
        matched = _FILE_HREF.search(href)
        if matched is None:
            raise ParseError(f"{SOURCE_KEY}: 첨부 링크에서 URL을 못 뽑음 ({href[:60]!r})")
        name = link.get_text(" ", strip=True) or _UNNAMED_FILE
        found.append(Attachment(name=name, url=urljoin(base_url, matched.group(0))))
    return tuple(found)


def _check_attachments_found(view: Tag, ref: PostingRef, *, files: tuple[Attachment, ...]) -> None:
    """첨부가 있다는 **독립 신호**(머리 패널에 `no-attach`가 없음)와 대조한다.

    이 게시판은 첨부 있는 공고를 실측하지 못했다 — 교차 확인이 없으면 첨부 셀렉터가 처음부터
    틀렸어도 "본문 있으니 정상"으로 통과해 아무도 모른다.
    """
    head = view.select_one(_VIEW_HEAD)
    if head is None:
        raise ParseError(f"{SOURCE_KEY} {ref.external_id}: 셀렉터 `{_VIEW_HEAD}` 없음 — 개편 의심")
    classes: list[str] = head.get_attribute_list("class")
    if _NO_ATTACH_CLASS not in classes and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 첨부가 있다고 표시됐는데 목록이 비었음 —"
            f" 셀렉터 `{_FILE_LINK}` 확인"
        )


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 제목 칸 클래스와 번호 칸 아이콘을 독립적으로 본다(실측 — 번호가 빈다)."""
    return bool(row.select(_NOTICE_CELL) or row.select(_NOTICE_ICON)) or not cell_text(
        row, _NO_CELL
    )


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    href = urljoin(source.list_url, str(link.get("href") or "").strip())
    external_id = require_numeric_id(
        external_id_from_url(href, detail_pattern=source.detail_pattern, what=SOURCE_KEY),
        source_key=SOURCE_KEY,
    )
    title = link.get_text(" ", strip=True)
    shown_date = cell_text(row, _DATE_CELL)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 `:443`이 붙어 있다(실측) — 저장 URL은 정규형으로 통일한다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=gnuboard_list_date(shown_date, source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            # 연도 없는 원문을 그대로 남긴다 — 복원한 연도가 틀렸을 때 대조할 근거가 된다.
            "list_date": shown_date or None,
            "author": cell_text(row, _AUTHOR_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NO_CELL) or None,
        },
    )
