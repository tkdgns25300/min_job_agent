"""KAICAM(독립교회연합회 청빙·청원) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/KAICAM/`):

```
목록  /webchon.layout/board/white2022/list.asp?boardid=D9537   2페이지 이상은 &page={n}
      table#BOARD_white2022_list tr 32 = 헤더 1(tr.fix) + tr.list 31 = 공지 1 + 공고 30
      페이지 크기는 숨은 입력 `LISTLINE=30`이 알려준다(실측 31행 = 공지 1 + 30).
      칸: td.lt(표시번호 · 공지는 icon_notice.gif) · td(제목: div.innerBoardTitles>a) ·
          div.username · div.date · td:last-child(조회)
상세  /webchon.layout/board/white2022/view.asp?boardid=D9537&boardmasterseq=2726&boarddetailseq={id}
```

⚠️ **`soft_200`**: `view.asp`는 없는 `boarddetailseq`에도 200을 준다(config `fetch_note`).
2026-08-04 실측(`boarddetailseq=999999999`): **HTTP 200 · 2,071바이트의 빈 껍데기** —
`<title>청빙청원</title>`만 있고 `#WC_BOARD_TITLES`도 `div#contents`도 없다. 그래서 상세 파싱은
**제목이 목록에서 가져온 것과 같은지 먼저 확인**한다. 이 검사가 없으면 빈 껍데기나 남의 글이
이 공고의 증거로 저장되고, 상태코드만 보는 코드는 그것을 성공으로 기록한다(SPEC §3).

⚠️ **페이지 링크가 href가 아니라 `onclick="goPage('2')"`** 다. 실제 요청 파라미터는 숨은
`pagingPrefix`(`./list.asp?boardmasterseq=2726&…&boardtype=list&rwd=1`)와 같은 CMS를 쓰는
PGAK의 페이저(`list.asp?boardid=…&page=2`)에서 `page`로 확인했다(2페이지 fixture로 검증).

표시번호(730)와 원장 키(`boarddetailseq`=436518)는 다르다 — 표시번호는 게시판이 다시 매긴다.
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
    cell_text,
    external_id_from_url,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "KAICAM"

#: 표에 class가 없어 id로 잡는다. id에 스킨명(`white2022`)이 박혀 있고 그 값은 `list_url`
#: 경로에도 들어 있다 — 스킨이 바뀌면 둘이 함께 바뀌므로 `ParseError`로 즉시 드러난다.
_LIST_TABLE: Final = "table#BOARD_white2022_list"
_ROW: Final = "tr.list"
#: 고정공지 — 표시번호 칸이 숫자 대신 아이콘이다. 두 신호를 독립적으로 본다.
_NOTICE_ICON: Final = 'td.lt img[src*="icon_notice"]'
_DISPLAY_NO: Final = "td.lt"
_DETAIL_LINK: Final = "div.innerBoardTitles a[href]"
_AUTHOR: Final = "div.username"
_DATE: Final = "div.date"
_VIEWS: Final = "td:last-child"
_PAGE_PARAM: Final = "page"
#: 상세 본문. ⚠️ **상세 페이지는 아래에 목록을 다시 그린다** — 범위를 넓히면 다른 공고 30건의
#: 제목이 이 공고의 증거로 저장된다(실측).
_BODY: Final = "div#contents"
#: 상세 페이지가 제목을 담는 곳. `soft_200` 검증의 기준값이다.
_DETAIL_TITLE: Final = "#WC_BOARD_TITLES"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `page` 쿼리다(모듈 docstring의 근거 참조)."""
    return page_query_request(source, page, param=_PAGE_PARAM, always_include=True)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    rows = table.select(_ROW)
    if not rows:
        raise ParseError(f"{SOURCE_KEY} 목록: `{_ROW}` 행이 없음 — 사이트 개편 의심")
    refs = [_ref_from_row(row, source) for row in rows if not _is_notice(row)]
    require_some_kept(
        refs, rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NOTICE_ICON}`·표시번호)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 본문 안 이미지. **받아온 글이 요청한 글인지 먼저 확인한다.**

    본문이 비어도 실패로 보지 않는다 — 포스터 이미지만 올리는 공고가 있다. 둘 다 없으면
    파싱이 빗나간 것이므로 실패다.

    ⚠️ **첨부는 수집하지 않는다.** 목록에 첨부 칸이 없고 실측한 상세에도 첨부 영역이 없다
    (HTML에 `file`·`attach`를 담은 class·id가 하나도 없다). 위치를 추측해 넓히면 본문의 링크나
    사이트 공용 파일이 첨부로 저장된다(DAESHIN 실측). 첨부가 달린 공고를 만나면 실측해 채운다.
    """
    soup = parse_html(html)
    _require_same_posting(soup, ref)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    if not raw_text and not images:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images)


def _require_same_posting(soup: Tag, ref: PostingRef) -> None:
    """받아온 페이지가 **요청한 글**인지 확인한다(`soft_200` 방어 · 모듈 docstring).

    상태코드로는 판정할 수 없는 게시판이라 **본문 내용으로** 성공을 판정한다(SPEC §3).
    두 단계다:

    1. 제목 요소가 있어야 한다 — 없는 글의 껍데기에는 이게 없다(실측).
    2. 목록에서 온 `ref`면 제목이 같아야 한다 — 목록 원필드(`list_meta["list_title"]`)를
       기준으로 삼는다. 그것이 없는 `ref`는 목록을 거치지 않은 것이라(적합성 테스트의 탐침·
       상세 재파싱) 대조 기준이 없다. `ref.title`을 쓰면 그런 호출을 전부 실패시키게 된다.
    """
    found = require_one(soup, _DETAIL_TITLE, what=f"{SOURCE_KEY} 상세 제목")
    expected = ref.list_meta.get("list_title")
    if not isinstance(expected, str):
        return
    title = found.get_text(" ", strip=True)
    if _comparable(title) != _comparable(expected):
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 상세 제목이 목록과 다름"
            f" (목록 {expected!r} ≠ 상세 {title!r}) — soft_200(없는 글에도 200)일 수 있다"
        )


def _comparable(title: str) -> str:
    """공백만 접어 비교한다. 목록은 `div`, 상세는 `span`이라 공백이 갈릴 수 있다(실측)."""
    return "".join(title.split())


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 아이콘과 표시번호를 독립적으로 본다 — 하나가 바뀌어도 걸린다."""
    return bool(row.select(_NOTICE_ICON)) or not cell_text(row, _DISPLAY_NO).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
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
    # `New`·`Hit` 배지는 앵커 **밖**의 `div.innerBoardIcons`에 있어 제목에 섞이지 않는다(실측).
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href는 `view.asp?…` 상대 경로다 — **정규형**으로 다시 만든다
        # (`boardmasterseq=2726`은 config가 들고 있다).
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE), source_key=SOURCE_KEY, cell=_DATE),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE) or None,
            "author": cell_text(row, _AUTHOR) or None,
            "views": as_int(cell_text(row, _VIEWS)),
            "display_no": cell_text(row, _DISPLAY_NO) or None,
        },
    )
