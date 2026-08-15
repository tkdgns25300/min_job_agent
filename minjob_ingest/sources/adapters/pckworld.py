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

게시일은 **썸네일 파일명**에서 읽는다(`20260729171107.jpg` → 2026-07-29 · 실측 60/60).

⚠️ 전에는 읽지 않았다 — 파일명 관례라 조용히 바뀔 수 있고, 그 값이 백필 컷오프를 움직이면
공고가 조용히 잘려 나가기 때문이었다. **그 이유는 지금도 유효하고, 그래서 `list_has_dates`는
계속 `false`다** — 컷오프는 이 날짜를 보지 않고 페이지 상한이 범위를 정한다. 여기서 읽는 값은
`posted_at`으로만 쓰인다(게시일 기준 자동 만료 · SPEC §9).

⚠️ 파일명을 못 읽으면 **여기서 채우지 않고 비운다**. 어댑터가 오늘로 메꾸면 관례가 바뀐 사실이
어디에도 안 드러난다 — 적합성 테스트도, `source_health`의 최신 게시일 경보도 통과해버린다.
폴백은 `collect`가 저장 직전에 한 번만 한다.

⚠️ **빈 `raw_text`가 정상이다**(config `image_only`). 내용은 포스터 이미지에만 있고, 구조화가
Gemini 멀티모달로 읽는다(SPEC §3·§5).
"""

from __future__ import annotations

import re
from datetime import date
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

#: 썸네일 파일명의 업로드 시각 — `/upimg/adsearch/20260729171107.jpg`. 앞 8자리가 날짜다.
_UPLOAD_STAMP: Final = re.compile(r"/(\d{4})(\d{2})(\d{2})\d{6}\.")

#: 파일명에서 읽은 값을 게시일로 인정할 연도 범위. 밖이면 파일명 관례가 바뀐 것이다.
_PLAUSIBLE_YEARS: Final = range(2000, 2101)
#: 상세는 포스터 이미지 한 장뿐이라 본문 컨테이너가 따로 없다.
#: 포스터가 올라가는 경로. 목록 썸네일도 상세 본문도 같은 곳을 쓴다 — 게시판이 아이콘·뱃지
#: `img`를 넣어도 그 파일이 **게시일이 되거나 공고 내용으로 실리는 일**이 없게 이걸로 가른다.
_POSTER_PATH: Final = "/upimg/adsearch/"
_POSTER_IMAGE: Final = f"img[src*='{_POSTER_PATH}']"
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
    # ⚠️ **포스터인지 경로로 확인한다** — `img` 아무거나 세면 아이콘·뱃지 한 장짜리 페이지가
    #    포스터가 있는 것으로 통과한다. 이 게시판은 그림이 곧 공고라 그때 내용이 통째로 빈다.
    images = tuple(url for url in image_urls_in(soup, base_url=ref.url) if _POSTER_PATH in url)
    if not images:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 포스터 이미지가 없음 — 경로 `{_POSTER_PATH}` 확인"
        )
    # ⚠️ `raw_html`을 담지 않는다 — 이 게시판 상세는 포스터 `<img>` 한 장뿐이고 본문
    # 컨테이너가 없다(내용은 이미지에만 있다 · config `image_only`). 페이지 전체를
    # 담으면 껍데기만 저장된다.
    return RawPosting(ref=ref, raw_text=_body_text(soup), image_urls=images)


def uploaded_on(source_path: str | None) -> date | None:
    """썸네일 파일명이 말하는 업로드 날짜. 읽을 수 없으면 `None`(부르는 쪽이 오늘로 둔다)."""
    if source_path is None:
        return None
    found = _UPLOAD_STAMP.search(source_path)
    if found is None:
        return None
    year, month, day = (int(part) for part in found.groups())
    if year not in _PLAUSIBLE_YEARS:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        # 파일명이 날짜꼴이지만 없는 날이다(`20261332…`) — 관례가 바뀐 신호로 보고 버린다.
        return None


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
    thumbnail = item.select_one(_POSTER_IMAGE)
    source_path = None if thumbnail is None else str(thumbnail.get("src"))
    return PostingRef(
        external_id=external_id,
        url=detail_url(source, external_id),
        title=title,
        # ⚠️ 못 읽으면 **비운다** — 여기서 오늘로 채우면 파일명 관례가 바뀐 사실을 아무도
        #    모르게 된다(테스트도 `source_health`의 최신 게시일 경보도 통과해버린다).
        #    폴백은 `collect`가 저장 직전에 한 번만 한다.
        posted_on=uploaded_on(source_path),
        list_meta={
            "list_title": title,
            "list_date": None,
            # 썸네일. 게시일의 근거라서 남긴다 — 파일명이 바뀌면 여기서 확인한다.
            "thumbnail": source_path,
        },
    )
