"""NAZARENE(나사렛성결회 목회자청빙) 어댑터 — 그누보드5 + clean URL 스킨.

게시판 실측(2026-08-04 · fixture `tests/fixtures/NAZARENE/`):

```
목록  /ccall                       2페이지 이상은 ?page={n}
      ul.na-table > li.d-md-table-row 15 (헤더 행 없음 · 공지도 없음)
      칸이 `td`가 아니라 **`div`**다: 번호 | 제목(a.na-subject) | 등록자 | 등록일 | 조회
      각 칸 머리에 `span.sr-only` 라벨("번호"·"등록일")이 숨어 있어 그대로 읽으면
      `"번호 112"`·`"등록일 2026.07.06"`이 된다 → 파싱 전에 걷어낸다.
상세  /ccall/{id}                  본문 = #bo_v_con · 첨부 = #bo_v_file (그누보드5 기본 id)
```

⚠️ **회원전용 글은 건너뛴다**(가드레일 #1 — 우회하지 않는다). 표시는 행 안의 `.fa-lock`이고,
실측 1·2페이지 30건에는 **한 건도 없었다**(페이지의 유일한 `fa-lock`은 로그인 폼 자물쇠 아이콘).
`fetch_note`의 "일부 글 잠금"은 지금 목록에서 재현되지 않는다 — 규칙만 심어 둔다.

⚠️ **2페이지 상세 링크에는 `?page=2`가 붙는다**(실측) — 목록 href를 그대로 쓰면 같은 글의
`source_url`이 페이지마다 달라진다. 그래서 `detail_url`로 정규형을 만든다.
"""

from __future__ import annotations

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
    external_id_from_url,
    gnuboard_list_date,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_numeric_id,
    require_one,
    require_some_kept,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "NAZARENE"

_LIST: Final = "ul.na-table"
_ROW: Final = "li.d-md-table-row"
#: 화면에만 보이지 않는 칸 라벨. 남겨 두면 모든 칸 값에 라벨이 앞에 붙는다(실측).
_SCREEN_READER_LABEL: Final = "span.sr-only"
#: 회원전용(잠금) 표시. **우회하지 않고 건너뛴다**(가드레일 #1).
_LOCK: Final = ".fa-lock"
_DETAIL_LINK: Final = "a.na-subject"
#: 칸이 `div`라 클래스로는 못 가른다(부트스트랩 유틸리티 클래스뿐) — 위치로 구분한다.
#: `:scope >`로 **직계 자식**에 묶는다. 없으면 제목 칸 안의 중첩 div까지 세어 칸이 밀린다.
_NO_CELL: Final = ":scope > div:nth-of-type(1)"
_AUTHOR_CELL: Final = ":scope > div:nth-of-type(3)"
_DATE_CELL: Final = ":scope > div:nth-of-type(4)"
_VIEWS_CELL: Final = ":scope > div:nth-of-type(5)"
_PAGE_PARAM: Final = "page"

_BODY: Final = "#bo_v_con"
_FILE_BOX: Final = "#bo_v_file"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(clean URL이라 쿼리가 없다 · 실측)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지·회원전용 글은 제외한다."""
    soup = parse_html(html)
    for label in soup.select(_SCREEN_READER_LABEL):
        label.decompose()
    listing = require_one(soup, _LIST, what=f"{SOURCE_KEY} 목록")
    data_rows = listing.select(_ROW)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_skippable_row(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by="공지·잠금 판정(번호 칸·`.fa-lock`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부."""
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    # ⚠️ 첨부 범위를 `#bo_v_file`로 제한한다. `article#bo_v`까지 넓히면 "관련자료"의 **다른
    # 공고 링크**와 목록·로그인 링크가 첨부로 들어온다(실측).
    files = attachments_in(soup.select_one(_FILE_BOX), base_url=ref.url)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _is_skippable_row(row: Tag) -> bool:
    """건너뛸 행인가 — 회원전용(잠금)이거나 번호가 숫자가 아닌 행(고정공지).

    ⚠️ 잠금 판정을 **링크 파싱보다 먼저** 한다. 잠긴 글은 링크 형태가 다를 수 있고, 그때
    `_ref_from_row`가 먼저 터지면 그 페이지 전체를 잃는다.
    """
    return bool(row.select(_LOCK)) or not cell_text(row, _NO_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    external_id = require_numeric_id(
        external_id_from_url(
            str(link.get("href") or "").strip(),
            detail_pattern=source.detail_pattern,
            what=SOURCE_KEY,
        ),
        source_key=SOURCE_KEY,
    )
    title = link.get_text(" ", strip=True)
    shown_date = cell_text(row, _DATE_CELL)
    return PostingRef(
        external_id=external_id,
        url=detail_url(source, external_id),
        title=title,
        # 실측 범위(최신 2026.07.06)에서는 연도까지 나왔지만, 그누보드는 **오늘 글을 `HH:MM`**로
        # 준다. 저물량 게시판이라 오늘 글을 실측할 기회가 드물어 처음부터 흡수한다.
        posted_on=gnuboard_list_date(shown_date, source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": shown_date or None,
            "author": cell_text(row, _AUTHOR_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NO_CELL) or None,
        },
    )
