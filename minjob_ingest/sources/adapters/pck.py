"""PCK(예장통합 총회 교단교역자 청빙) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/PCK/`):

```
목록  /bbs/board.php?bo_table=SM05_05     2페이지 이상은 &page={n}
      그누보드지만 **mw_basic 스킨**이라 `tr.bo_notice`가 없다. 목록 표가
      `form#fboardlist` 안에 있고 행 사이에 1px 구분 `tr`이 섞여 있다.
      제목 칸을 가진 tr 18 = 공지 2 + 공고 16
      칸: 번호 | 구인상태(모집 중) | 제목 | 작성자 | 등록일(YYYY-MM-DD) | 모집마감 | 조회
상세  같은 board.php의 &wr_id={id}       본문 = div.mw_basic_view_content
```

⚠️ **`fetch_note`와 다른 점 2가지**(2026-08-04 재실측): 공지는 5건이 아니라 **2건**이고,
`bo_notice` 클래스가 아니라 **번호 칸의 아이콘 이미지**로만 구분된다.

⚠️ **등록일 칸에 연도가 있다**(`2026-08-04`). 같은 값이 모바일용 `div.list_desc_info`에는
`08-04`로도 있으나 연도 없는 쪽을 쓰지 않는다 — 연말·연초에 1년이 어긋난다.
"""

from __future__ import annotations

import re
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

SOURCE_KEY: Final = "PCK"

_LIST_TABLE: Final = "form#fboardlist table"
#: 데이터 행 = **제목 칸을 가진** tr. `rows_with_data`(td 보유)로는 1px 구분선 행까지 데이터로
#: 세어 "공지 판정이 전부 걸러냈다" 경보(`require_some_kept`)가 무력해진다(실측 41행 중 18행만 글).
_ROW: Final = "tr:has(td.mw_basic_list_subject)"
_NO_CELL: Final = "td:nth-of-type(1)"
_STATUS_CELL: Final = "td:nth-of-type(2)"
_DETAIL_LINK: Final = "td.mw_basic_list_subject a[href]"
_AUTHOR_CELL: Final = "td.mw_basic_list_name"
_DATE_CELL: Final = "td.mw_basic_list_datetime"
#: 마감·조회 칸은 클래스가 `mw_basic_list_hit`로 **겹쳐서** 위치로만 구분된다(실측).
_DEADLINE_CELL: Final = "td:nth-of-type(6)"
_VIEWS_CELL: Final = "td:nth-of-type(7)"
_PAGE_PARAM: Final = "page"
#: 마감일 미입력이 빈 칸이 아니라 `0000-00-00`으로 온다(실측) — 날짜로 흘리면 안 된다.
_EMPTY_DATE: Final = "0000-00-00"

_BODY: Final = "div.mw_basic_view_content"
#: 확장필드(`교회명 :`·`노회명 :`)는 본문이 아니라 **작성자 줄 안**에 있다(실측).
#: 본문만 담으면 구조화가 교회명·노회를 잃는다 — 교단 확정 근거가 되는 값이다(SPEC §5.3).
_EXTRA_FIELDS: Final = "div.mw_basic_view_title div"
#: 첨부 상자는 본문의 **형제**다. 범위를 넓히면 댓글 앵커·푸터 파일이 첨부로 들어온다.
_FILE_BOX: Final = "div.mw_basic_view_file"
_FILE_LINK: Final = 'a[href^="javascript:file_download"]'
_FILE_URL: Final = re.compile(r"file_download\('([^']+)'")
_UNNAMED_FILE: Final = "attachment"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(쿼리 없이 1페이지가 나온다 · 실측)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = table.select(_ROW)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by="공지 판정(번호 칸 아이콘·빈 번호)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문(+확장필드) + 이미지 + 첨부."""
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    extra = soup.select_one(_EXTRA_FIELDS)
    raw_text = "\n\n".join(
        part
        for part in ("" if extra is None else normalized_text(extra), normalized_text(body))
        if part
    )
    images = image_urls_in(body, base_url=ref.url)
    files = _attachments(soup.select_one(_FILE_BOX), base_url=ref.url)
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _attachments(box: Tag | None, *, base_url: str) -> tuple[Attachment, ...]:
    """첨부 URL은 href가 아니라 **JS 호출 안**에 있다(실측):
    `javascript:file_download('https://…/bbs/download.php?…&no=0', '0')`.

    base의 `attachments_in`을 쓰면 `javascript:…` 문자열이 그대로 URL로 저장돼 구조화가
    첨부를 못 읽는다. 파일명은 링크 텍스트뿐이다 — 다운로드 URL에는 없다.
    """
    if box is None:
        return ()
    found: list[Attachment] = []
    for link in box.select(_FILE_LINK):
        href = str(link.get("href") or "")
        matched = _FILE_URL.search(href)
        if matched is None:
            raise ParseError(f"{SOURCE_KEY}: 첨부 링크에서 URL을 못 뽑음 ({href[:60]!r})")
        name = link.get_text(" ", strip=True) or _UNNAMED_FILE
        found.append(Attachment(name=name, url=urljoin(base_url, matched.group(1))))
    return tuple(found)


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 번호 칸에 아이콘 이미지가 들어가고 번호 텍스트가 빈다(두 신호 · 실측).

    ⚠️ `cell.select("img")`로 **칸을 먼저 좁힌 뒤** 훑는다. `row.select("td:nth-of-type(1) img")`는
    스킨 전체를 감싼 `td#mw_basic`이 조상으로 걸려 **모든 행이 공지로 판정된다**(실측 —
    상대 select의 조상 조건은 문서 전체에서 평가된다).
    """
    cell = require_one(row, _NO_CELL, what=f"{SOURCE_KEY} 목록 번호 칸")
    return bool(cell.select("img")) or not cell.get_text(strip=True)


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
    return PostingRef(
        external_id=external_id,
        # 목록 href는 `:443`이 붙은 형태다(실측) — 같은 글의 `source_url`이 갈리지 않게 정규형.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, _AUTHOR_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NO_CELL) or None,
            # 이 게시판만의 칸. 마감 여부는 운영자 검수·구조화에 쓰인다(공개 판정은 사람).
            "status": cell_text(row, _STATUS_CELL) or None,
            "deadline": _real_date(cell_text(row, _DEADLINE_CELL)),
        },
    )


def _real_date(text: str) -> str | None:
    """`0000-00-00`(미입력)을 날짜로 흘리지 않는다 — 그대로 두면 마감일이 있는 것처럼 보인다."""
    trimmed = text.strip()
    return None if trimmed in ("", _EMPTY_DATE) else trimmed
