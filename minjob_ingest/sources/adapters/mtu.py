"""MTU(감신대 취업게시판) 어댑터 — eGov `.do` 게시판.

게시판 실측(2026-08-04 · fixture `tests/fixtures/MTU/`):

```
목록  /mtu/board/list.do?mId=162            2페이지 이상은 &page={n} (실측 총 549페이지)
      table.tbListA tr 22 = 헤더 1(th) + 공지 1(tr.notice) + 공고 20
      칸: td.number td.tltle(오타가 아니라 실제 class) td.file td.writer td.date td.hit
      공지행의 번호 칸에는 숫자가 아니라 `공지사항` 이미지가 들어간다.
상세  /mtu/board/view.do?mId=162&brdIdx={id}
      ⚠️ `mId=162`가 함께 없으면 게시판이 특정되지 않는다(config `detail_pattern`이 들고 있다).
      본문 div.board-contents(`<pre>` 안 HWP 붙여넣기) · 첨부 dl.attached-file-wrapper
```

⚠️ **2페이지부터 상세 href에 `page`가 끼어든다** — `view.do?mId=162&page=2&brdIdx=20670`(실측).
config `detail_pattern`의 접두사(`?mId=162&brdIdx=`)로 자르면 **2페이지 이후 전 행이 탈락**한다.
그래서 위치가 아니라 **쿼리 파라미터 이름**으로 뽑는다(1페이지만 보면 안 드러난다).

⚠️ **브라우저 UA가 없으면 보안 스텁이 온다**(0건 · config `spoof_ua`). fetch 층이 31곳 전부에
브라우저 UA를 보내므로 어댑터가 할 일은 없다 — 목록이 갑자기 0건이면 그 스텁을 의심한다.
스텁은 `table.tbListA`가 없어 `ParseError`가 되므로 조용히 0건이 되지는 않는다.
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

SOURCE_KEY: Final = "MTU"

_LIST_TABLE: Final = "table.tbListA"
#: 제목 칸의 class는 `tltle`이다 — 게시판 원본의 오타이고, 고치면 아무것도 찾지 못한다(실측).
_DETAIL_LINK: Final = "td.tltle a[href]"
_DATE_CELL: Final = "td.date"
_NO_CELL: Final = "td.number"
_FILE_CELL: Final = "td.file"
#: 고정공지 — class와 번호 칸(숫자 없음) 두 신호를 독립적으로 본다(실측: 둘 다 있다).
_NOTICE_CLASS: Final = "notice"
_PAGE_PARAM: Final = "page"
#: 상세 URL이 담는 글번호의 쿼리 파라미터 이름(config `detail_pattern`과 같은 이름).
_ID_PARAM: Final = "brdIdx"
#: 본문(실측). 상세 페이지에는 사이트 내비게이션·학과 메뉴가 함께 있어 범위를 좁혀야 한다.
#: 본문은 `<pre>` 안에 HWP에서 붙여넣은 `<span>` 더미로 들어온다 — `normalized_text`가 흡수한다.
_BODY: Final = "div.board-contents"
#: 첨부 목록(실측: `download.do?…&fidx=1&itId=file` 앵커 + 파일명).
#: ⚠️ 본문까지 넓히지 않는다 — 공고 본문에 교회 홈페이지 링크가 흔하다.
_FILE_LIST: Final = "dl.attached-file-wrapper"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(`mId`만으로 1페이지가 나온다 · 실측)."""
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    if page == 1:
        return ListRequest(url=source.list_url)
    separator = "&" if "?" in source.list_url else "?"
    return ListRequest(url=f"{source.list_url}{separator}{_PAGE_PARAM}={page}")


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_notice(row)]
    require_some_kept(
        refs,
        data_rows,
        source_key=SOURCE_KEY,
        filtered_by=f"공지 판정(`{_NOTICE_CLASS}`·{_NO_CELL})",
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    본문이 비어 있어도 실패로 보지 않는다 — 내용을 첨부(HWP·이미지)로만 올리는 공고가 있고
    그때는 그것이 유일한 증거다(SPEC §5).
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(soup.select_one(_FILE_LIST), base_url=ref.url)
    _check_attachments_found(ref, files=files)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _check_attachments_found(ref: PostingRef, *, files: tuple[object, ...]) -> None:
    """목록의 첨부 아이콘과 대조한다 — 첨부가 조용히 0개가 되는 것을 막는다."""
    if ref.list_meta.get("has_attachment") and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 목록에 첨부 아이콘이 있는데 첨부가 0개 —"
            f" 셀렉터 `{_FILE_LIST}` 확인"
        )


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 번호 칸이 이미지(`공지사항`)라 텍스트가 비고 숫자가 아니다(실측)."""
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, _NO_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = _external_id_from(urljoin(source.list_url, href))
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href는 상대경로이고 페이지 상태가 붙을 수 있어 **정규형**으로 만든다
        # (`mId=162`를 config가 들고 있어 상세가 게시판을 잃지 않는다).
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, "td.writer") or None,
            "views": as_int(cell_text(row, "td.hit")),
            "display_no": cell_text(row, _NO_CELL) or None,
            "has_attachment": bool(row.select(f"{_FILE_CELL} img")),
        },
    )


def _external_id_from(url: str) -> str:
    """상세 URL의 `brdIdx`. 표시번호(10965)가 아니라 이 값이 원장 키다.

    파라미터 **이름**으로 찾는다 — 2페이지부터 `page`가 끼어들어 접두사 매칭이 깨진다
    (모듈 docstring 참조).
    """
    found = parse_qs(urlsplit(url).query).get(_ID_PARAM)
    if not found or not found[0].strip():
        raise ParseError(
            f"{SOURCE_KEY}: 상세 URL에 `{_ID_PARAM}`가 없음 ({url}) — 링크 형태가 바뀌었다"
        )
    return require_numeric_id(found[0].strip(), source_key=SOURCE_KEY)
