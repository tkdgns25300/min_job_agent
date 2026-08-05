"""MOKWON(목원대 신학과 사역지정보) 어댑터 — PCMS 게시판.

게시판 실측(2026-08-04 · fixture `tests/fixtures/MOKWON/`):

```
목록  /mt1954/html/sub06/0602.html          2페이지 이상은 ?mode=L&GotoPage={n}
      table.board_list tr 16 = 헤더 1(th) + 공지 1(tr.bbs_notice) + 공고 14
      칸: td.ntt_no td.title td.wrt td.inq_cnt td.reg_date(YYYY-MM-DD) td.atch_nm
상세  같은 .html의 ?mode=V&no={id}          목록/상세가 한 파일이고 mode로 갈린다
```

⚠️ **`external_id`가 숫자가 아니다** — `no`는 32자리 hex다
(`501103573814a8ef882b3f885d1fb33b`). `require_numeric_id`를 쓰면 전 행이 탈락한다.
표시번호(`td.ntt_no` = 695)가 숫자인 쪽이고, 그건 게시판이 다시 매기는 값이라 원장 키가 아니다.

⚠️ config `fetch_note`의 정정 사실: **완전 static이다**(tbody 서버렌더). 페이지에 있는
`ajaxprototyOpen`은 사이트 공용 메뉴 팝업이고 목록과 무관하다(실측 확인) — headless가 필요하다는
초기 의심은 틀렸다.
"""

from __future__ import annotations

import re
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
    attachments_in,
    cell_text,
    external_id_from_url,
    image_urls_in,
    normalized_text,
    parse_html,
    require_attachment_evidence,
    require_date,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "MOKWON"

_LIST_TABLE: Final = "table.board_list"
_DETAIL_LINK: Final = "td.title a[href]"
_DATE_CELL: Final = "td.reg_date"
_NO_CELL: Final = "td.ntt_no"
_ATTACHMENT_CELL: Final = "td.atch_nm"
#: 고정공지 — class와 표시번호(`공지`) 두 신호를 독립적으로 본다(실측: 둘 다 있다).
_NOTICE_CLASS: Final = "bbs_notice"
#: 목록 페이지 파라미터. `mode=L`이 목록 모드다(상세는 `mode=V`).
_PAGE_QUERY: Final = "?mode=L&GotoPage="
#: `no`의 형태 — 32자리 hex. 숫자 검사(`require_numeric_id`)를 이걸로 대체한다.
_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")
#: 본문(실측). 상세도 목록과 같은 파일이라 좁혀야 사이트 내비게이션·안내문이 섞이지 않는다.
_BODY: Final = "div.bbs--view--content"
#: 공고 카드 전체(제목·작성자 + 본문). ⚠️ **첨부 범위**로만 쓴다 — 첨부가 달린 공고를 아직
#: 실측하지 못해(목록 1페이지 15행 전부 `td.atch_nm` 빈칸) PCMS가 파일 목록을 본문 안에 두는지
#: 카드 아래에 두는지 모른다. 카드 밖으로는 넓히지 않는다(사이트 공용 파일이 들어온다 · DAESHIN).
_POSTING_VIEW: Final = "div.bbs--view"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(쿼리 없이 목록이 나온다 · 실측)."""
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    if page == 1:
        return ListRequest(url=source.list_url)
    return ListRequest(url=f"{source.list_url}{_PAGE_QUERY}{page}")


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

    첨부는 **공고 카드 안으로 제한**한다 — 밖으로 넓히면 사이트 공용 링크가 들어온다(DAESHIN 실측).
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(soup.select_one(_POSTING_VIEW), base_url=ref.url)
    require_attachment_evidence(ref, source_key=SOURCE_KEY, selector=_POSTING_VIEW, found=files)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _is_notice(row: Tag) -> bool:
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, _NO_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = _external_id_from(urljoin(source.list_url, href), source)
    title = link.get_text(" ", strip=True)
    attachment_cell = row.select_one(_ATTACHMENT_CELL)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 `GotoPage`가 붙어 있어 **정규형**으로 다시 만든다 — 같은 글을 다른
        # 페이지에서 만났을 때 `source_url`이 달라지면 안 된다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, "td.wrt") or None,
            "views": as_int(cell_text(row, "td.inq_cnt")),
            "display_no": cell_text(row, _NO_CELL) or None,
            "has_attachment": attachment_cell is not None
            and bool(attachment_cell.select("a, img")),
        },
    )


def _external_id_from(url: str, source: SourceConfig) -> str:
    """상세 URL의 `no`. **32자리 hex**여야 한다(모듈 docstring 참조)."""
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    found = external_id_from_url(url, detail_pattern=source.detail_pattern, what=SOURCE_KEY)
    if not _ID_PATTERN.match(found):
        raise ParseError(
            f"{SOURCE_KEY}: `no`가 32자리 hex가 아님 ({found!r}) — 링크 형태가 바뀌었다"
        )
    return found
