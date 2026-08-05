"""PGAK(백석총회 사역자구함) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/PGAK/`):

```
목록  /sys-infra/components/board/list.asp?skin=basic&boardid=B5FF8   2페이지 이상은 &page={n}
      table.board-list tr 25 = 헤더 1(tr.fix) + tr.list 24
      ⚠️ tr.list 24 = 공고 12 + **모바일 중복행 12**(tr.list.m_wrap)
      칸: td(표시번호) td.title(링크 + div.innerBoardIcons) td.user td.date td.hit
상세  /sys-infra/components/board/view.asp?boarddetailseq={id}&boardid=B5FF8
```

⚠️ **반응형 레이아웃이 한 공고를 두 행으로 렌더한다.** `tr.list`를 그대로 쓰면 행이 두 배가 되고,
짝수 행(`m_wrap`)에는 제목 링크가 없어 소스 전체가 `ParseError`로 죽는다(실측 24행 중 12행).

공지 행은 실측하지 못했다(1페이지 12건 전부 공고 · HTML에 `notice`·`공지` 문자열 없음).
행을 **`tr.list` 화이트리스트로** 고르므로 다른 class를 쓰는 공지는 자동으로 빠진다.

⚠️ **상세 페이지는 아래에 목록을 다시 그린다**(같은 `table.board-list`가 들어 있다) — 본문 범위를
넓히면 다른 공고 12건의 제목이 이 공고의 증거로 저장된다.

이 게시판은 Cloudflare 뒤에 있고 빈 UA에 520을 준다(`fetch_note`) — 전송 층 문제이고 파싱과는
무관하다.

**첨부(2026-08-05 실측 · 표본 7건 중 1건):** 상세 본문 상자(`div#contents`)의 **형제**로
`div.tag_box > p.tit(첨부파일) + div.file-list-container > div.file-item`이 붙는다. 첨부가 없는
공고에는 `tag_box`가 아예 없다. 파일 링크는 `span.file-name > a[href]`이고 앵커 텍스트가 원본
파일명(`이슬람 소개1.hwpx`)이다 — 다운로드 URL은 `pds.rh2.kr`의 타임스탬프 이름
(`이슬람 소개1_ts1784808688052.hwpx`)이라 URL만으로는 이름이 다르다.

⚠️ **목록에 첨부 표시 칸이 없고 상세 제목칸에도 없다** — 스킨 CSS(`view.css`)에
`.titles .icon_file` 규칙이 있는데도 첨부 4개인 공고에서 그 요소가 렌더되지 않았다(실측).
그래서 `require_attachment_evidence`로 대조할 독립 신호가 없다.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

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
    page_query_request,
    parse_html,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "PGAK"

#: 스킨이 만든 `id`(`BOARD_skin_basic_list_list`)가 아니라 class로 잡는다 — id에 스킨명이
#: 박혀 있어 `skin=` 파라미터가 바뀌면 함께 바뀐다.
_LIST_TABLE: Final = "table.board-list"
_ROW: Final = "tr.list"
#: 같은 공고의 모바일용 중복 행. 제목 링크가 없어 반드시 걸러야 한다.
_MOBILE_CLASS: Final = "m_wrap"
_DETAIL_LINK: Final = "td.title a[href]"
_DATE: Final = "td.date"
_AUTHOR: Final = "td.user"
_VIEWS: Final = "td.hit"
#: 표시번호 칸에는 class가 없다(헤더만 `td.num`을 쓴다 · 실측).
_DISPLAY_NO: Final = "td:first-child"
_PAGE_PARAM: Final = "page"
#: ⚠️ 상세 본문은 **숨은 textarea에 HTML 문자열로** 들어 있고, 화면에 보이는 `div#contentWrap`은
#: 브라우저 JS가 채운다(실측). 그래서 값을 꺼내 **한 번 더 파싱**한다 — 그러지 않으면 본문이
#: `1.교회소개<br /> …`처럼 태그가 글자로 남고, 본문 안 이미지를 통째로 잃는다.
_RAW_CONTENT: Final = "div#contents textarea#temp-raw-content"
#: 첨부 목록. ⚠️ **`div.tag_box`를 셀렉터로 쓰면 안 된다** — 그 상자는 첨부 전용이 아니라
#: 태그 목록도 담는 공용 상자다(스킨 CSS `view.css`에 `.tag_box .tag span` 규칙이 있다 · 실측).
#: `div#viewBoard`로 한정하는 이유: 상세 페이지 **아래에 목록이 다시 그려지고**
#: (`div.BOARD_skin_basic_list_wrap`) 그쪽에 같은 class가 생기면 남의 첨부가 섞인다.
_ATTACHMENTS: Final = "div#viewBoard div.file-list-container"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `page` 쿼리다(실측 페이저: `list.asp?boardid=…&page=2`)."""
    return page_query_request(source, page, param=_PAGE_PARAM, always_include=True)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 모바일 중복 행은 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    rows = table.select(_ROW)
    if not rows:
        raise ParseError(f"{SOURCE_KEY} 목록: `{_ROW}` 행이 없음 — 사이트 개편 의심")
    refs = [_ref_from_row(row, source) for row in rows if not _is_mobile_duplicate(row)]
    require_some_kept(
        refs, rows, source_key=SOURCE_KEY, filtered_by=f"모바일 중복행 판정(`{_MOBILE_CLASS}`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 본문 안 이미지 + 첨부.

    ⚠️ **첨부는 본문 밖에서만 온다**(`_ATTACHMENTS`). 본문까지 범위를 넓히면 공고에 적힌 교회
    홈페이지 링크가 첨부로 저장된다 — 잘못된 첨부는 없는 첨부보다 나쁘다(DAESHIN 실측).

    ⚠️ **첨부 상자의 `<img>`는 이미지가 아니라 확장자 아이콘**이다
    (`img.rh2.kr/board/skin_basic/file/hwp.gif`) — `image_urls`를 본문에서만 모으는 이유다.
    첨부가 이미지 파일이면 `Attachment.name`이 `.jpg`로 끝나 구조화가 알아본다.
    """
    soup = parse_html(html)
    body = _content_of(soup, ref)
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    attachments = attachments_in(soup.select_one(_ATTACHMENTS), base_url=ref.url)
    if not raw_text and not images and not attachments:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 —"
            f" 셀렉터 `{_RAW_CONTENT}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=attachments)


def _content_of(soup: BeautifulSoup, ref: PostingRef) -> Tag:
    """숨은 textarea에 든 본문 HTML을 꺼내 **다시 파싱**한다(모듈 상단 `_RAW_CONTENT` 참조).

    렌더 대상(`div#contentWrap`)은 정적 HTML에서 항상 비어 있으므로 그것을 읽으면 모든 공고가
    빈 본문이 된다 — 조용한 실패의 교과서적 형태다.
    """
    holder = require_one(soup, _RAW_CONTENT, what=f"{SOURCE_KEY} 상세 본문")
    stored = holder.get_text()
    if not stored.strip():
        raise ParseError(f"{SOURCE_KEY} {ref.external_id}: 본문 textarea가 비었음")
    return parse_html(stored)


def _is_mobile_duplicate(row: Tag) -> bool:
    """같은 공고를 다시 그린 모바일 행. 제목 링크가 없어 데이터로 쓸 수 없다."""
    classes: list[str] = row.get_attribute_list("class")
    return _MOBILE_CLASS in classes


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
        # 목록 href는 `./view.asp?…` 상대 경로다 — **정규형**으로 다시 만든다.
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
