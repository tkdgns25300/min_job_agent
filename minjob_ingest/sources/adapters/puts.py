"""PUTS(장신대 초빙 · 장신Lounge) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/PUTS/`):

```
목록  /www/board/list.general.asp?bd_name=jangshin_jboard04
      div.BoardListTy1 table tr 54 = 공지 4(tr.ntc) + 공고 50   (헤더 tr이 없다)
      칸이 2개뿐이고 나머지는 span으로 들어 있다:
        td.c1  span.num(표시번호=seq) · div.file(첨부 아이콘)
        td.c2  div.tit>a(제목 · span.grp=분류 배지) · span.name(작성자) ·
               span.date · span.count(조회)
상세  /www/board/view.general.asp?seq={id}&bd_name=jangshin_jboard04
```

⚠️ **행의 첫 `a`는 제목이 아니다** — 첨부가 있는 행은 `div.file`의 `a href="#"`이 먼저 온다.
느슨한 셀렉터로 잡으면 그 행들만 조용히 `#`을 상세 URL로 쓴다(50행 중 9행 · 실측).

⚠️ **목록 행에 다른 게시판 글이 섞인다**(`fetch_note`: 공지에 `jnotice02` 혼입). 이 사이트의
`seq`는 게시판을 가로질러 매겨지므로(공지 149165 · 공고 157665가 같은 대역) 섞인 행을 그대로
받으면 **남의 게시판 글이 PUTS 원장에 들어간다**. 그래서 공지 class와 별개로 행 href의
`bd_name`을 config의 것과 대조한다 — 신호 둘이 독립이라 하나가 바뀌어도 걸린다.

날짜 표기가 행 종류마다 다르다(공지 `2025-05-30` · 공고 `2026.08.04`) —
`require_date`가 둘 다 받는다.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import parse_qs, urljoin, urlsplit

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
    parse_html,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "PUTS"

_LIST_TABLE: Final = "div.BoardListTy1 table"
#: 고정공지 행.
_NOTICE_CLASS: Final = "ntc"
#: ⚠️ `div.tit` 안까지 좁힌다 — 행의 첫 `a`는 첨부 아이콘(`href="#"`)일 수 있다(실측).
_DETAIL_LINK: Final = "td.c2 div.tit a[href]"
_DATE: Final = "td.c2 span.date"
#: 제목 앞에 붙는 분류 배지(`초빙공고`·`초빙완료`). 제목이 아니라 상태 표시라 따로 담는다 —
#: 공지에서 "초빙완료시 [초빙공고] -> [초빙완료]로 수정 요청"이라 안내하므로 값이 변한다.
_CATEGORY: Final = "span.grp"
_DISPLAY_NO: Final = "td.c1 span.num"
_AUTHOR: Final = "td.c2 span.name"
_VIEWS: Final = "td.c2 span.count"
#: 첨부 아이콘. 상세에서 첨부 셀렉터가 빗나갔는지 교차 확인하는 독립 신호다.
_FILE_ICON: Final = "td.c1 div.file img"
#: 게시판 식별자. **하드코딩하지 않고 config의 `list_url`에서 읽는다.**
_BOARD_PARAM: Final = "bd_name"
_PAGE_PARAM: Final = "page"
#: ⚠️ 페이지 크기를 **1페이지에도 명시**한다. 서버 기본값(실측 50)에 기대면 그 값이 바뀌는 날
#: 1페이지는 20건, 2페이지는 51번째부터가 되어 **사이의 글이 조용히 사라진다**.
_PAGE_SIZE_PARAM: Final = "pagesize"
_PAGE_SIZE: Final = 50
#: 상세 본문. 교회 정보(교단·노회·주소·연락처)가 `dl` 양식으로, 자유 서술이 그 뒤에 온다.
#: ⚠️ 자유 서술 전체가 `<a href="#">`로 감싸여 있다(실측) — 첨부를 본문에서 찾으면 안 되는 이유다.
_BODY: Final = "div.BoardRead div.cont"
#: 첨부 전용 칸. 없는 글에서는 주석만 남아 비어 있다(실측).
_FILE_LIST: Final = "div.BoardRead div.file"
#: 모든 글에 똑같이 붙는 사이트 안내문(개인정보 노출 삭제 요청·번호 대체표시). 공고 내용이
#: 아니라서 떼어낸다 — 남기면 50건마다 같은 240자가 raw_text와 AI 입력에 실린다.
_BOILERPLATE: Final = ("div.notesBox2", "div.notesBox3")


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `page`+`pagesize` 쿼리다(`fetch_note` 실측)."""
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    separator = "&" if "?" in source.list_url else "?"
    return ListRequest(
        url=f"{source.list_url}{separator}{_PAGE_PARAM}={page}&{_PAGE_SIZE_PARAM}={_PAGE_SIZE}"
    )


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지와 **타 게시판 행**을 제외한다."""
    board = _board_of(source)
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source, board) for row in data_rows if not _is_skippable(row, board)]
    require_some_kept(
        refs,
        data_rows,
        source_key=SOURCE_KEY,
        filtered_by=f"공지 판정(`{_NOTICE_CLASS}`)·`{_BOARD_PARAM}={board}` 대조",
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    본문이 비어도 실패로 보지 않는다 — 포스터 이미지 한 장만 올리는 공고가 흔하다(구조화가
    멀티모달로 읽는다). 셋 다 없으면 파싱이 빗나간 것이므로 실패다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    for selector in _BOILERPLATE:
        for boilerplate in body.select(selector):
            boilerplate.decompose()
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    # 첨부는 **본문 밖의 전용 목록**에 있다. 범위를 넓히면 이전/다음글 링크와 사이트 공용
    # 파일이 첨부로 들어온다(DAESHIN 실측).
    files = attachments_in(soup.select_one(_FILE_LIST), base_url=ref.url)
    if ref.list_meta.get("has_attachment") and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 목록이 첨부 있다고 표시했는데 상세에서 0개 —"
            f" 셀렉터 `{_FILE_LIST}` 확인"
        )
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _board_of(source: SourceConfig) -> str:
    """config `list_url`이 가리키는 게시판 식별자. 행 대조의 기준값이다."""
    found = parse_qs(urlsplit(source.list_url).query).get(_BOARD_PARAM, [])
    if not found or not found[0].strip():
        raise ParseError(
            f"{SOURCE_KEY}: list_url에 `{_BOARD_PARAM}`이 없어 게시판을 특정할 수 없음"
            f" ({source.list_url})"
        )
    return found[0].strip()


def _is_skippable(row: Tag, board: str) -> bool:
    """고정공지이거나 **다른 게시판 글**인 행. 두 신호를 독립적으로 본다."""
    classes: list[str] = row.get_attribute_list("class")
    if _NOTICE_CLASS in classes:
        return True
    link = row.select_one(_DETAIL_LINK)
    return link is None or _board_in_href(link) != board


def _board_in_href(link: Tag) -> str | None:
    found = parse_qs(urlsplit(str(link.get("href") or "")).query).get(_BOARD_PARAM, [])
    return found[0] if found else None


def _ref_from_row(row: Tag, source: SourceConfig, board: str) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:  # `_is_skippable`이 이미 걸렀지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    external_id = require_numeric_id(
        external_id_from_url(
            urljoin(source.list_url, str(link.get("href") or "")),
            detail_pattern=source.detail_pattern,
            what=SOURCE_KEY,
        ),
        source_key=SOURCE_KEY,
    )
    category = _take_category(link)
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 skin·검색·page가 줄줄이 붙어 있다 — **정규형**으로 다시 만들어
        # 같은 글이 페이지를 옮겨도 `source_url`이 흔들리지 않게 한다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE), source_key=SOURCE_KEY, cell=_DATE),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE) or None,
            "author": cell_text(row, _AUTHOR) or None,
            "views": as_int(cell_text(row, _VIEWS)),
            "display_no": cell_text(row, _DISPLAY_NO) or None,
            "category": category,
            "board": board,
            "has_attachment": bool(row.select(_FILE_ICON)),
        },
    )


def _take_category(link: Tag) -> str | None:
    """분류 배지를 제목에서 **떼어내** 따로 돌려준다.

    떼지 않으면 모든 제목이 `초빙공고 ...`로 시작해 원문 제목과 달라지고, 나중에 배지가
    `초빙완료`로 바뀌면 같은 글의 제목이 변한 것처럼 보인다(원장 경보를 헛되게 울린다).
    """
    badge = link.select_one(_CATEGORY)
    if badge is None:
        return None
    text = badge.get_text(" ", strip=True)
    badge.extract()
    return text or None
