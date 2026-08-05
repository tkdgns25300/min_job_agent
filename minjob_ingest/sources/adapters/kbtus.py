"""KBTUS(침신대 취업지원 사역자채용) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/KBTUS/`):

```
목록  /job/CMS/Board/Board.do?mCode=MN014        2페이지 이상은 &page={n}
      table.board-list-table tr 20 = 헤더 1 + 공지 1(tr.isnotice) + 공고 18
      칸: td.num td.cate([파트]/[풀타임]/[기타]) td.subject td.writer td.date(YYYY-MM-DD) td.cnt
      첨부 표시: 제목 칸의 img.isFileIcon(alt="첨부파일") — 18건 중 8건
상세  같은 Board.do의 &mode=view&mgr_seq=91&board_seq={id}    본문 = div.board-view-contents
      첨부 ul.board-view-filelist(div.board-view-winfo 안)
```

**첨부 실측(2026-08-05)**: 표본 4건(37416 HWP · 37365 HWP · 37322 HWP · 37298 DOCX) 전부
`ul.board-view-filelist`에 다운로드 링크가 있었다. `div.board-view-files`(이미지 전용 상자)는
네 건 모두 **빈 채로 렌더**돼 실측하지 못했지만, CMS가 "본문에 없는 이미지 첨부"용으로 두는
자리라 그대로 둔다 — 있으면 `image_urls`로 온다.

⚠️ **상세 링크가 `href="javascript:URL_encode(...)"` 다.** 그 함수는 인자를 URL 인코딩해
`location.href="?"+…`로 넘길 뿐이므로 **실제 요청은 평범한 GET 쿼리**다(실측: 함수 본문 확인).
그래서 어댑터는 JS 호출에서 `board_seq`만 뽑고 URL은 `detail_pattern`으로 다시 만든다.

⚠️ **서버 HEAD는 EUC-KR로 오보고하지만 GET 본문은 UTF-8이다**(fetch_note). 디코드는 config
값을 우선하는 fetch 층이 이미 처리했다 — 여기 들어오는 것은 정상 문자열이다.
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
    require_attachment_evidence,
    require_date,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "KBTUS"

_LIST_TABLE: Final = "table.board-list-table"
#: 게시판이 **명시적으로** "글 없음"을 알리는 행(`colspan` 안내). 공지 판정보다 먼저 본다 —
#: 이 행은 링크도 날짜도 없어 공고 행으로 다루면 엉뚱한 `ParseError`가 난다(실측: `&page=2`).
_EMPTY_MARKER: Final = "td.no-data"
#: 고정공지 — 두 신호가 독립적으로 있다(tr class + `td.num`이 "공지"라 숫자가 아님).
_NOTICE_CLASS: Final = "isnotice"
_NUM_CELL: Final = "td.num"
_DETAIL_LINK: Final = "td.subject a[href]"
_DATE_CELL: Final = "td.date"
#: `javascript:URL_encode("?…&board_seq=37449");` 에서 글번호만.
_BOARD_SEQ: Final = re.compile(r"board_seq=(\d+)")
#: 실측(2026-08-04): `&page=2` → "등록된 게시글이 없습니다." 즉 서버가 이 파라미터를 읽는다.
#: (게시판 자체는 ~20건 롤링이라 평소 1페이지뿐이다.)
_PAGE_PARAM: Final = "page"

_BODY: Final = "div.board-view-contents"
#: 모든 공고 본문 맨 앞에 붙는 저작권 안내. 공고 내용이 아니라 CMS 고정 문구라 지운다 —
#: 남기면 31곳 전체에서 구조화 토큰만 먹고 요약을 흐린다.
_BOILERPLATE: Final = "div.allim-box"
#: "첨부파일이 이미지일 경우, **본문에 없는** 이미지일 경우 보여준다"(CMS 주석 실측).
#: 본문과 겹치지 않으므로 `image_urls`에 넣어도 중복 저장이 되지 않는다.
_IMAGE_ONLY_ATTACHMENTS: Final = "div.board-view-files"
#: 첨부 다운로드 목록(CMS 주석 `첨부파일 목록`). ⚠️ **`div.board-view-winfo`를 그대로 쓰면 안
#: 된다** — 그 상자는 "추가 정보"라 CMS가 임시 필드도 같이 렌더한다. 첨부는 `ul`에서만 온다.
_FILE_LIST: Final = "div.board-view-winfo ul.board-view-filelist"
#: 목록의 첨부 아이콘. 상세 첨부 셀렉터가 빗나갔는지 교차 확인하는 **독립 신호**다.
_ATTACH_MARK: Final = "img.isFileIcon"
_SUBJECT_CELL: Final = "td.subject"
#: ⚠️ 앵커 텍스트가 `파일명.hwp (57KB)`다 — 크기를 떼지 않으면 파일명이 확장자로 끝나지 않아
#: **이미지 첨부의 `is_image`가 거짓**이 되고(구조화가 Gemini에 안 보낸다) 운영자에게도
#: 지저분하게 보인다(2026-08-05 실측).


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(쿼리에 이미 `mCode`가 있다)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    if table.select_one(_EMPTY_MARKER) is not None:
        # 게시판이 스스로 "글 없음"이라고 말했다 — 셀렉터가 깨진 것과 다르므로 에러가 아니다.
        return ()
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NOTICE_CLASS}`·td.num)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    본문이 비어도 실패로 보지 않는다 — 포스터 이미지 한 장만 올린 공고가 있고, 그때는 그것이
    유일한 내용이다. 셋 다 없을 때만 파싱이 빗나간 것이다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    for boilerplate in body.select(_BOILERPLATE):
        boilerplate.decompose()
    raw_text = normalized_text(body)
    images = image_urls_in(body, soup.select_one(_IMAGE_ONLY_ATTACHMENTS), base_url=ref.url)
    files = attachments_in(soup.select_one(_FILE_LIST), base_url=ref.url)
    require_attachment_evidence(
        ref, source_key=SOURCE_KEY, selector=_FILE_LIST, found=(*files, *images)
    )
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 두 신호를 독립적으로 본다(class + 표시번호 자리의 "공지")."""
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, _NUM_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    external_id = id_from_js(
        str(link.get("href") or ""),
        pattern=_BOARD_SEQ,
        source_key=SOURCE_KEY,
        what="상세 링크(URL_encode)",
    )
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # JS 인자를 URL로 쓰지 않고 **정규형**으로 만든다 — 인자 순서가 바뀌어도 같은 글이
        # 같은 `source_url`을 갖게 한다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, "td.writer") or None,
            "views": as_int(cell_text(row, "td.cnt")),
            "display_no": cell_text(row, _NUM_CELL) or None,
            # 게시판이 스스로 붙인 고용형태 구분([파트]/[풀타임]/[기타]) — 구조화의 근거가 된다.
            "category": cell_text(row, "td.cate") or None,
            "has_attachment": bool(row.select(f"{_SUBJECT_CELL} {_ATTACH_MARK}")),
        },
    )
