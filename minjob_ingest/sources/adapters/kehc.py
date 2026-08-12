"""KEHC(기독교대한성결교회 총회 성결광장 구인) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/KEHC/`):

```
목록  /home/recruit/view_list/page/0
      ⚠️ **`page/N`은 페이지 번호가 아니라 행 offset이다** — 2페이지는 `page/50`(실측: 페이징
      링크가 0·50·100…7950). 한 페이지 50행.
      div.sub_text02.board table tr 51 = 헤더 1(th만) + 공지 2 + 공고 48
      칸에 class가 없다(제목만 `td.ellipsis`) → 위치로 읽는다:
      1 번호 · 2 상태(진행중/완료) · 3 분류(목사/전도사/음악사역자/기타) · 4 제목 ·
      5 작성자 · 6 날짜(**YY.MM.DD**) · 7 조회
상세  /home/recruit/read_post/{id}    본문 = div#data_content
```

⚠️ **날짜가 2자리 연도다**(`26.08.04`). 그대로는 ISO가 아니라 `require_date`가 거부한다 →
세기를 붙여 넘긴다.

⚠️ **비밀글(`ico_lock.gif`)은 목록에서 제외한다.** 실측: 잠긴 글(27561)의 상세는 200을 주면서
`<meta charset>` 하나뿐인 **빈 페이지**를 돌려준다(같은 세션에서 잠기지 않은 27558은 정상).
비공개 글이므로 우회하지 않고 아예 목록에 담지 않는다 — 담으면 매 실행마다
상세 파싱이 실패해 게시판 전체 수집이 멈춘다.

⚠️ **상세 페이지가 목록 50행을 다시 품고 있다**(`table.read_post_align`). 본문·첨부 범위를
`div#data_content` 밖으로 넓히면 그 링크들이 통째로 섞인다.

**첨부 실측(2026-08-05)**: 27468(총회본부 직원 공개채용)에 HWP 1개가 `ul#attachFileList`로
렌더된 것을 확인했다(`tests/fixtures/KEHC/detail_file.html`). 첨부가 없는 27537·27558은 그 `ul`
자체가 없다. ⚠️ **목록에 첨부 표시 칸이 없어** `require_attachment_evidence` 대조를 걸 수 없다 —
그래서 이 셀렉터는 fixture 테스트가 유일한 방어선이다.
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
    as_int,
    as_listing,
    attachments_in,
    cell_text,
    id_from_js,
    image_urls_in,
    normalized_text,
    parse_html,
    require_date,
    require_one,
    require_some_kept,
    rows_with_data,
    structural_html,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "KEHC"

#: 목록 테이블에는 class·id가 없다 — 감싼 div로 특정한다(실측).
_LIST_TABLE: Final = "div.sub_text02.board table"
#: 칸에 class가 없어 위치로 읽는다. 순서가 바뀌면 날짜·번호 검사가 먼저 터진다.
_NUM_CELL: Final = "td:nth-of-type(1)"
_STATUS_CELL: Final = "td:nth-of-type(2)"
_CATEGORY_CELL: Final = "td:nth-of-type(3)"
_WRITER_CELL: Final = "td:nth-of-type(5)"
_DATE_CELL: Final = "td:nth-of-type(6)"
_VIEWS_CELL: Final = "td:nth-of-type(7)"
_DETAIL_LINK: Final = "td.ellipsis a[href]"
#: 고정공지 — 두 신호가 독립적으로 있다(번호 자리가 "공지" + 링크에 `notice_title` class).
_NOTICE_LINK_CLASS: Final = "notice_title"
#: 비밀글 표시. 상세가 빈 페이지라 수집 대상이 아니다(위 docstring).
_LOCK_ICON: Final = 'td.ellipsis img[src*="ico_lock"]'
#: `javascript:read_post(27558)`
_READ_POST: Final = re.compile(r"read_post\((\d+)\)")
#: `YY.MM.DD` — 세기가 없다.
_TWO_DIGIT_YEAR: Final = re.compile(r"\d{2}\.\d{2}\.\d{2}")
#: 목록 URL 끝의 offset. 페이지를 바꿀 때 이 자리를 갈아끼운다.
_PAGE_SUFFIX: Final = re.compile(r"/page/\d+$")
#: 한 페이지 행 수 = offset 증가폭(실측: 50행 · 링크가 0·50·100…).
_ROWS_PER_PAGE: Final = 50

_BODY: Final = "div#data_content"
#: 첨부 목록 — `div.filelist_area > ul#attachFileList > li > a`(2026-08-05 실측 27468).
#: 첨부가 없는 공고는 이 `ul` 자체가 렌더되지 않는다.
#: ⚠️ 첨부를 본문 안에서 찾으면 안 된다 — 본문에 Cloudflare 이메일 난독화 링크
#: (`/cdn-cgi/l/email-protection`)가 섞여 있어 그것이 첨부로 저장된다(실측).
#: ⚠️ 범위를 페이지 전체로 넓히면 안 된다 — 사이드바·푸터에 사이트 공용 파일 링크
#: (`/home/pdfdownload` "헌법유권해석집")가 **모든 상세에** 있어 전 공고에 가짜 첨부가 붙는다.
_FILE_LIST: Final = "#attachFileList"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. ⚠️ 경로의 숫자는 페이지가 아니라 **행 offset**이다."""
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    base = _PAGE_SUFFIX.sub("", source.list_url)
    return ListRequest(url=f"{base}/page/{(page - 1) * _ROWS_PER_PAGE}")


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지와 비밀글을 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_skippable_row(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by="공지·비밀글 판정(번호 칸·잠금 아이콘)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    본문 셀렉터가 없으면 실패다 — 비밀글의 빈 페이지가 여기로 오면 조용히 통과하지 않고
    시끄럽게 터져야 한다(목록에서 걸러졌어야 하는 글이다).
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    raw_html = structural_html(body)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(soup.select_one(_FILE_LIST), base_url=ref.url)
    return RawPosting(
        ref=ref, raw_text=raw_text, raw_html=raw_html, image_urls=images, attachments=files
    )


def _is_skippable_row(row: Tag) -> bool:
    """고정공지 또는 비밀글 행. 공지는 두 신호를 독립적으로 본다."""
    link = row.select_one(_DETAIL_LINK)
    notice_classes: list[str] = [] if link is None else link.get_attribute_list("class")
    return (
        _NOTICE_LINK_CLASS in notice_classes
        or not cell_text(row, _NUM_CELL).isdigit()
        or row.select_one(_LOCK_ICON) is not None
    )


def _posted_on(row: Tag) -> date:
    """게시일. ⚠️ 2자리 연도라 세기를 붙여 넘긴다(2000년대 게시판이다 · 실측 17~26년)."""
    text = cell_text(row, _DATE_CELL)
    return require_date(
        f"20{text}" if _TWO_DIGIT_YEAR.fullmatch(text) else text,
        source_key=SOURCE_KEY,
        cell=_DATE_CELL,
    )


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    external_id = id_from_js(
        str(link.get("href") or ""),
        pattern=_READ_POST,
        source_key=SOURCE_KEY,
        what="상세 링크(read_post)",
    )
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        url=detail_url(source, external_id),
        title=title,
        posted_on=_posted_on(row),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, _WRITER_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NUM_CELL) or None,
            # 게시판이 직접 붙인 값 — 분류는 직급 추정, 상태는 마감 판단의 근거가 된다.
            "category": cell_text(row, _CATEGORY_CELL) or None,
            "status": cell_text(row, _STATUS_CELL) or None,
        },
    )
