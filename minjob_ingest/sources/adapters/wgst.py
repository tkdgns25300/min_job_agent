"""WGST(웨스트민스터신대원 교역자청빙) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/WGST/`):

```
목록  /wgst_renew/board/board.asp?key=6131      2페이지 이상은 &pageno={n}
      **표가 아니라 리스트**다: ul.newsfeed_lst > li.item 12개(페이지당 12건 · 140페이지)
      li  span.num(표시번호=rowNo) ·
          div.content > strong.subject > a(제목 · font=교회 라벨) ·
          dl.info > dd(작성일) · dd.file(첨부칸) · dd(조회수)
상세  /wgst_renew/board/boardview.asp?key=6131&seq={id}
```

⚠️ **`span.num`(1676)은 `external_id`가 아니다** — 목록 href의 `seq`(6910)가 글번호다.
`rowNo`는 게시판이 다시 매기는 표시번호라 글이 삭제되면 다른 글에 붙는다.

⚠️ **`external_id`를 URL 접두사로 뽑을 수 없다.** 목록 href의 파라미터 순서가
`key → pageno → searchkey → rowNo → seq`로, config의 `detail_pattern`(`key=6131&seq=`)과 다르다
(실측) — 접두사 매칭은 통째로 실패한다. 그래서 쿼리를 파싱해 `seq`를 이름으로 집는다.

⚠️ **고정공지가 없는 게시판**이다(실측: 1페이지 12행 전부 공고). 그래도 표시번호가 숫자가 아닌
행은 걸러낸다 — 공지가 생기면 그 칸에 `공지`가 들어오는 것이 이 계열의 관례이고, 걸러지지 않으면
`require_numeric_id`가 소스 전체를 실패시킨다.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import parse_qs, urlsplit

from bs4 import Tag

from minjob_ingest.sources.adapters.base import (
    ListRequest,
    ParseError,
    PostingRef,
    RawPosting,
    as_int,
    as_listing,
    cell_text,
    image_urls_in,
    normalized_text,
    parse_html,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "WGST"

_LIST: Final = "ul.newsfeed_lst"
_ROW: Final = "li.item"
_DETAIL_LINK: Final = "strong.subject a[href]"
_DISPLAY_NO: Final = "span.num"
#: 제목 앞의 교회 라벨(`<font color="#2366b5">[제자들교회]</font>`). **정규식으로 대괄호를
#: 잘라내면 안 된다** — 제목 자체에 `[청빙중]`·`[용인]`처럼 대괄호가 들어 있다(실측).
_CHURCH_LABEL: Final = "font"
#: 작성일·조회수. 사이에 첨부칸(`dd.file`)이 끼어 있어 위치로만 세면 밀린다.
_INFO_VALUES: Final = "dl.info dd:not(.file)"
_FILE_CELL_CONTENT: Final = "dd.file *"
#: 상세 URL의 글번호 파라미터.
_ID_PARAM: Final = "seq"
_PAGE_PARAM: Final = "pageno"
#: 상세 본문. 정보칸(`newsfeed_cnts_info`)과 클래스 토큰이 달라 겹치지 않는다.
_BODY: Final = "div.newsfeed_view div.newsfeed_cnts"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `pageno` 쿼리다(`fetch_note` 실측).

    1페이지에도 `pageno=1`을 명시한다 — 기본 페이지가 어디인지 서버 구현에 맡기지 않는다.
    """
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    separator = "&" if "?" in source.list_url else "?"
    return ListRequest(url=f"{source.list_url}{separator}{_PAGE_PARAM}={page}")


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 표시번호가 숫자가 아닌 행(공지)은 제외한다."""
    listing = require_one(parse_html(html), _LIST, what=f"{SOURCE_KEY} 목록")
    rows = listing.select(_ROW)
    if not rows:
        raise ParseError(f"{SOURCE_KEY} 목록: `{_ROW}` 행이 없음 — 사이트 개편 의심")
    refs = [_ref_from_row(row, source) for row in rows if _is_posting(row)]
    require_some_kept(
        refs, rows, source_key=SOURCE_KEY, filtered_by=f"표시번호 판정(`{_DISPLAY_NO}`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 본문 안 이미지.

    ⚠️ **첨부는 수집하지 않는다.** 1페이지 12건 중 첨부 표시(`dd.file`)가 있는 행이 하나도 없어
    첨부 영역의 위치를 실측하지 못했다(2026-08-04). 위치를 추측해 셀렉터를 넓히면 본문의
    교회 홈페이지 링크나 푸터의 사이트 공용 PDF(`2022_대학안전관리계획.pdf` · 이 페이지에 있다)를
    첨부로 저장한다 — 잘못된 첨부는 없는 첨부보다 나쁘다(DAESHIN 실측). `has_attachment`가
    True인 행을 만나면 그 글로 실측해 채운다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    if not raw_text and not images:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images)


def _is_posting(row: Tag) -> bool:
    """표시번호가 숫자인 행만 공고로 본다(공지는 그 칸에 `공지`가 들어온다)."""
    return cell_text(row, _DISPLAY_NO).replace(",", "").isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    external_id = require_numeric_id(_seq_in(link), source_key=SOURCE_KEY)
    church = _take_church_label(link)
    title = link.get_text(" ", strip=True)
    posted_text, views = _info_of(row)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 pageno·rowNo·검색어가 붙어 있다 — **정규형**으로 다시 만든다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(posted_text, source_key=SOURCE_KEY, cell=_INFO_VALUES),
        list_meta={
            "list_title": title,
            "list_date": posted_text or None,
            # 이 게시판엔 작성자 칸이 없다 — 교회 라벨이 그 자리를 대신한다.
            "author": church,
            "views": as_int(views),
            "display_no": cell_text(row, _DISPLAY_NO) or None,
            "has_attachment": bool(row.select(_FILE_CELL_CONTENT)),
        },
    )


def _seq_in(link: Tag) -> str:
    """목록 href의 `seq`. 파라미터 **이름으로** 집는다(순서가 config와 다르다 · 모듈 docstring)."""
    found = parse_qs(urlsplit(str(link.get("href") or "")).query).get(_ID_PARAM, [])
    if not found or not found[0].strip():
        raise ParseError(
            f"{SOURCE_KEY}: 목록 링크에 `{_ID_PARAM}`이 없음 ({link.get('href')!r}) —"
            " 링크 형태가 바뀌었다"
        )
    return found[0].strip()


def _take_church_label(link: Tag) -> str | None:
    """제목 앞의 교회 라벨을 **떼어내** 따로 돌려준다.

    떼지 않으면 제목이 `[제자들교회] 제자들교회(동탄)에서 …`처럼 교회명이 두 번 들어간다.
    이 게시판은 `a[title]`에 순수 제목을 담고 있어(실측) 테스트가 그 값과 대조할 수 있다.
    """
    label = link.select_one(_CHURCH_LABEL)
    if label is None:
        return None
    text = label.get_text(" ", strip=True)
    label.extract()
    return text.strip("[]") or None


def _info_of(row: Tag) -> tuple[str, str]:
    """(작성일, 조회수). 첨부칸을 뺀 `dd` 순서가 계약이다."""
    values = [value.get_text(" ", strip=True) for value in row.select(_INFO_VALUES)]
    if len(values) < 2:
        raise ParseError(
            f"{SOURCE_KEY} 목록 행의 정보칸이 {len(values)}개 — 셀렉터 `{_INFO_VALUES}` 확인"
        )
    return values[0], values[1]
