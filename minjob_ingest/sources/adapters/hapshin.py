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

**첨부 실측(2026-08-05 · wr_id 15254·15246·15215 · fixture `detail_file.html` = 15254)**:
첨부는 머리 패널(`div.view-head`) 안의 `div.list-group`에 다운로드 링크로 온다.

```html
<div class="panel panel-default view-head">        <!-- 첨부 없으면 + no-attach -->
  <div class="panel-heading">…작성자·조회·날짜…</div>
  <div class="list-group font-12">
    <a class="… view_file_download …" href="…/bbs/download.php?bo_table=e03&wr_id=…&no=0">
      <span class="label … view-cnt">4</span><i class="fa fa-download"></i>
      이력서_자기소개서.hwp (93.5K)
      <span class="en font-11 text-muted"><i class="fa fa-clock-o"></i> 7일전</span></a>
  </div>
</div>
```

⚠️ **링크 텍스트를 그대로 파일명으로 쓸 수 없다** — 다운로드 횟수·크기·등록일이 섞여
`4 이력서_자기소개서.hwp (93.5K) 7일전`이 되고, 확장자가 끝에 오지 않아 `is_image`가 **항상
거짓**이 된다(KTS와 같은 결함). URL(`download.php?…&no=0`)에는 파일명이 없어 복원할 곳도
링크 텍스트뿐이다 → `_file_name`이 chrome을 걷어낸다.

⚠️ **이미지 첨부는 본문의 형제 상자(`div.view-img`)에 온다** — 실측 4건 전부 비어 있었지만
스킨은 그 상자를 **항상 렌더**하고 `a.view_image` 팝업 핸들러도 함께 내려온다(그누보드5
`#bo_v_img` 계열). KTS는 이 상자를 빼먹어 이미지 첨부 하나를 통째로 잃었다 — 같은 계열이므로
여기서도 함께 읽는다. 빈 상자는 아무것도 만들지 않으므로 오염 위험이 없다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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
    gnuboard_list_date,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
    structural_html,
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
#: 첨부 목록도 이 패널 안에 있어(위 docstring) 그대로 첨부 범위가 된다 — 범위를 상세 전체로
#: 넓히면 댓글에 붙은 링크까지 첨부가 된다.
_VIEW_HEAD: Final = "div.view-head"
_NO_ATTACH_CLASS: Final = "no-attach"
#: 이미지 첨부 상자 — 본문의 **형제**다(위 docstring · KTS `#bo_v_img`와 같은 자리).
_IMAGE_BOX: Final = "div.view-img"
#: 첨부 링크는 스킨 클래스가 아니라 **그누보드 공용 다운로드 경로**로 찾는다 —
#: 경로는 스킨이 바뀌어도 남는다(`a.view_file_download` 클래스는 스킨 것이다).
_FILE_LINK: Final = 'a[href*="download.php"]'
#: ⚠️ 그누보드의 **관련 링크**(`wr_link`). 작성자가 파일이 아니라 **URL**을 붙인 것이고,
#: 그것도 `no-attach`를 떼어낸다(실측 2026-08-05 · 15242 = `cafe.daum.net/peace5851`).
#: 이걸 모르면 "첨부 있다고 표시됐는데 없다"로 판정해 **정상 공고를 버린다**(전량 수집에서 7건).
_RELATED_LINK: Final = 'a[href*="link.php"]'
_FILE_HREF: Final = re.compile(r"(?:https?://[^\s'\"]+|/)[^\s'\"]*download\.php[^\s'\"]*")
_UNNAMED_FILE: Final = "attachment"
#: 링크 텍스트에 섞인 chrome — 다운로드 횟수 배지(`span.view-cnt`)·등록일(`span.text-muted`)·
#: 폰트 아이콘. 파일명은 이 사이의 **맨 텍스트 노드**다(위 docstring).
_NAME_NOISE: Final = "span.view-cnt, span.text-muted, i"
#: 파일명 뒤에 붙는 크기 표기 `(93.5K)`·`(768.2K)`.
_SIZE_SUFFIX: Final = re.compile(r"\s*\(\s*\d+(?:\.\d+)?\s*[KMGT]?B?\s*\)\s*$", re.IGNORECASE)


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
    """상세 HTML → 본문 + 이미지 + 첨부.

    첨부는 **두 상자**에 나뉘어 있다 — 파일 목록(머리 패널)과 이미지 첨부 상자(본문의 형제).
    모듈 docstring의 실측을 참조.
    """
    soup = parse_html(html)
    view = require_one(soup, _VIEW, what=f"{SOURCE_KEY} 상세")
    body = require_one(view, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    image_box = view.select_one(_IMAGE_BOX)
    raw_text = normalized_text(body)
    raw_html = structural_html(body)
    box_images = image_urls_in(image_box, base_url=ref.url)
    images = image_urls_in(body, base_url=ref.url) + box_images
    files = _attachments(view, base_url=ref.url) + attachments_in(image_box, base_url=ref.url)
    # 대조에 세는 것은 **첨부 영역에서 온 것만**이다 — 본문 이미지는 첨부의 증거가 아니다.
    _check_attachments_found(view, ref, found=(*files, *box_images))
    return RawPosting(
        ref=ref, raw_text=raw_text, raw_html=raw_html, image_urls=images, attachments=files
    )


def _attachments(view: Tag, *, base_url: str) -> tuple[Attachment, ...]:
    """파일 첨부 = 머리 패널 안의 다운로드 링크(모듈 docstring의 실측).

    범위를 머리 패널로 제한한다 — 상세 전체로 넓히면 댓글·푸터의 링크가 첨부가 된다.
    href가 `javascript:` 래퍼일 수도 있어(같은 그누보드인 PCK가 그렇다) 문자열에서 실제 경로를
    꺼낸다. 못 꺼내면 조용히 버리지 않고 실패한다 — 첨부 유실은 사후에 알 수 없다.
    """
    head = view.select_one(_VIEW_HEAD)
    found: list[Attachment] = []
    for link in () if head is None else head.select(_FILE_LINK):
        href = str(link.get("href") or "")
        matched = _FILE_HREF.search(href)
        if matched is None:
            raise ParseError(f"{SOURCE_KEY}: 첨부 링크에서 URL을 못 뽑음 ({href[:60]!r})")
        found.append(Attachment(name=_file_name(link), url=urljoin(base_url, matched.group(0))))
    return tuple(found)


def _file_name(link: Tag) -> str:
    """링크 텍스트에서 **파일명만** 뽑는다(모듈 docstring의 chrome 실측).

    다운로드 횟수·아이콘·등록일을 걷어내고 뒤에 붙은 크기 표기를 떼면 파일명이 남는다.
    URL에는 파일명이 없으므로(`download.php?…&no=0`) 여기가 유일한 출처다 — 비면 이름 없는
    첨부로 남기고 버리지 않는다.
    """
    working = parse_html(str(link))
    for noise in working.select(_NAME_NOISE):
        noise.decompose()
    return _SIZE_SUFFIX.sub("", working.get_text(" ", strip=True)).strip() or _UNNAMED_FILE


def _check_attachments_found(view: Tag, ref: PostingRef, *, found: Sequence[object]) -> None:
    """첨부가 있다는 **독립 신호**(머리 패널에 `no-attach`가 없음)와 대조한다.

    목록에 첨부 아이콘이 없는 게시판이라 이것이 유일한 교차 확인이다 — 없으면 첨부 셀렉터가
    빗나갔어도 "본문 있으니 정상"으로 통과해 아무도 모른다(2026-08-05 실측 전까지 그랬다).
    """
    head = view.select_one(_VIEW_HEAD)
    if head is None:
        raise ParseError(f"{SOURCE_KEY} {ref.external_id}: 셀렉터 `{_VIEW_HEAD}` 없음 — 개편 의심")
    classes: list[str] = head.get_attribute_list("class")
    if _NO_ATTACH_CLASS in classes or found:
        return
    # ⚠️ `no-attach`는 **파일도 링크도 없을 때만** 붙는다. 작성자가 URL만 붙인 글에서는
    # 파일이 없는 것이 정상이므로 실패로 보면 안 된다(교회 카페·홈페이지 주소가 흔하다).
    if head.select(_RELATED_LINK):
        return
    raise ParseError(
        f"{SOURCE_KEY} {ref.external_id}: 첨부가 있다고 표시됐는데 목록이 비었음 —"
        f" 셀렉터 `{_FILE_LINK}`·`{_IMAGE_BOX}` 확인"
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
