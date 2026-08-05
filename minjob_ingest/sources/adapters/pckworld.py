"""PCKWORLD(한국기독공보 광고검색) 어댑터 — **공고가 포스터 이미지 한 장이다.**

게시판 실측(2026-08-04 · fixture `tests/fixtures/PCKWORLD/`):

```
목록  /adsearch/            2페이지 이상은 ?page={n}
      ul.grid > li.grid-item 12건 · 각 항목 = 썸네일 img + span(제목)
      링크는 href가 아니라 javascript:adview('{aid}',w,h)
상세  /adsearch/ad_view.php?aid={id}   3.3KB · **텍스트 3자("창닫기")** + 포스터 img 1장
```

⚠️ **목록에 게시일이 없다** → config `list_has_dates: false`. 그래서 `--months`가 아니라 페이지
상한이 범위를 정한다(`collect._cutoff_for`).

썸네일 파일명이 `20260729171107.jpg`처럼 업로드 시각을 담고 있어 날짜를 **추론할 수는 있다**.
하지만 하지 않는다 — 발행된 데이터가 아니라 파일명 관례라서 조용히 바뀔 수 있고, 그 값이
백필 컷오프를 움직이면 공고가 조용히 잘려 나간다. 게시판이 날짜를 노출하면 그때 켠다.

⚠️ **빈 `raw_text`가 정상이다**(config `image_only`). 내용은 포스터 이미지에만 있고, 구조화가
Gemini 멀티모달로 읽는다(SPEC §3·§5).
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
    as_listing,
    id_from_js,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_one,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "PCKWORLD"

_GRID: Final = "ul.grid"
_ITEM: Final = "li.grid-item"
_LINK: Final = "a[href]"
_TITLE: Final = "span"
_PAGE_PARAM: Final = "page"
#: `javascript:adview('1551',1761,822)` — 첫 인자가 광고 번호(`aid`)다.
_ADVIEW_ID: Final = re.compile(r"adview\(\s*'(\d+)'")
#: 상세는 포스터 이미지 한 장뿐이라 본문 컨테이너가 따로 없다.
_DETAIL_IMAGE: Final = "img[src*='/upimg/adsearch/']"
_CLOSE_BUTTON_TEXT: Final = "창닫기"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들.

    ⚠️ 고정공지가 없는 게시판이라(광고 그리드) **걸러내는 것이 없다** — 항목 하나마다 참조
    하나가 나오고 못 읽으면 그 자리에서 실패한다. 그래서 `require_some_kept`(전량 필터 감지)를
    두지 않는다. 발동할 수 없는 가드는 검사하는 척만 한다.

    그리드가 비어 있는 것(마지막 페이지)은 여기서 에러로 보지 않는다 — 페이징이 그걸 필요로
    한다. 1페이지가 비는 이상 상황은 fixture 테스트와 `source_health`의 목록 0행 경보가 잡는다.
    """
    grid = require_one(parse_html(html), _GRID, what=f"{SOURCE_KEY} 목록")
    refs = [_ref_from_item(item, source) for item in grid.select(_ITEM)]
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 포스터 이미지. **텍스트는 사실상 없다**(닫기 버튼뿐).

    이미지가 없으면 실패다 — 그때는 증거가 하나도 없는 레코드가 된다.
    """
    soup = parse_html(html)
    images = image_urls_in(soup, base_url=ref.url)
    if not images:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 포스터 이미지가 없음 — 셀렉터 `{_DETAIL_IMAGE}` 확인"
        )
    return RawPosting(ref=ref, raw_text=_body_text(soup), image_urls=images)


def _body_text(soup: Tag) -> str:
    """닫기 버튼 같은 상투 문구는 본문으로 보지 않는다 — 빈 본문이 이 게시판의 정상이다."""
    text = normalized_text(soup)
    return "" if text.strip() == _CLOSE_BUTTON_TEXT else text


def _ref_from_item(item: Tag, source: SourceConfig) -> PostingRef:
    link = item.select_one(_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 항목에 링크가 없음 — 셀렉터 `{_LINK}` 확인")
    external_id = id_from_js(
        str(link.get("href") or ""), pattern=_ADVIEW_ID, source_key=SOURCE_KEY, what="adview 링크"
    )
    title_node = item.select_one(_TITLE)
    title = (title_node or link).get_text(" ", strip=True)
    if not title:
        raise ParseError(f"{SOURCE_KEY} {external_id}: 제목이 비었음 — 셀렉터 `{_TITLE}` 확인")
    thumbnail = item.select_one("img")
    return PostingRef(
        external_id=external_id,
        url=detail_url(source, external_id),
        title=title,
        # ⚠️ 날짜를 넣지 않는다(모듈 docstring 참조 · config `list_has_dates: false`).
        posted_on=None,
        list_meta={
            "list_title": title,
            "list_date": None,
            # 썸네일. 파일명에 업로드 시각이 들어 있어 나중에 날짜 근거가 필요하면 여기서 본다.
            "thumbnail": str(thumbnail.get("src")) if thumbnail is not None else None,
        },
    )
