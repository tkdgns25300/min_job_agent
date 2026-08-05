"""ACTS(아신대 사역정보) 어댑터 — 자체 ASP 게시판.

게시판 실측(2026-08-04 · fixture `tests/fixtures/ACTS/`):

```
목록  /modules/board/bd_list.asp?id=acts_csrd_guide&ca_no=1   2페이지 이상은 `&gotopage={n}`
      table.tbl--comm tbody tr 11 = 공지 1 + 공고 10
      칸: 번호 | 구분(사역) | 제목 | 작성자 | 첨부 | 등록일(YYYY-MM-DD) | 조회수
      공지행은 번호 칸이 `<img alt="공지">`다 → 번호가 숫자가 아니면 공고가 아니다.
상세  /modules/board/bd_view.asp?no={id}&id=acts_csrd_guide   본문 = td.boardView__cont
```

⚠️ **`ca_no=1`(사역정보 탭)이 올바른 게시판이다.** 폐기된 `bd_jobInfo.asp`(ca_no=6
'실시간채용정보')는 일반 채용이고 headless가 필요했다(config `fetch_note` 2차 검증 대정정).

⚠️ **본문에 `mailto:`·교회 홈페이지 링크가 흔하다** — 제출처·문의처를 링크로 적는 양식이다
(실측). 그래서 첨부를 "본문 안의 모든 링크"로 잡으면 안 된다. 첨부는 다운로드 스크립트
(`/lib/download.asp`)를 가리키는 링크로만 판정한다(실측 `detail_file.html`).

⚠️ 칸에 클래스가 거의 없어(`td.mobi`뿐) **위치로 읽는다**. 그래서 칸 순서가 바뀌면 조용히
어긋날 수 있으므로, 위치 상수를 한곳에 모으고 칸 수가 부족하면 `ParseError`로 끊는다.
"""

from __future__ import annotations

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
    external_id_from_url,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_attachment_evidence,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
    structural_html,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "ACTS"

_LIST_ROWS: Final = "table.tbl--comm tbody tr"
_DETAIL_LINK: Final = "a.boardList__tit"
#: 칸 위치(0부터). 클래스가 없어 순서로 읽는다 — 헤더: 번호·구분·제목·작성자·첨부·등록일·조회수.
_NUMBER, _CATEGORY, _AUTHOR, _ATTACH, _DATE, _VIEWS = 0, 1, 3, 4, 5, 6
_CELL_COUNT: Final = 7
_PAGE_PARAM: Final = "gotopage"
#: 본문은 표 안의 한 칸이다(`<td class="boardView__cont">`).
_BODY: Final = "td.boardView__cont"
#: 스킨이 본문 맨 앞에 넣는 스크린리더용 라벨. 전 공고에 붙으므로 본문에서 뺀다.
_HIDDEN_LABEL: Final = "p.hidden"
#: 상세 영역 전체(본문 + 첨부 행 + 이전/다음 글).
_VIEW: Final = "div.boardView"
#: 첨부 다운로드 링크. **경로로 판정한다** — 본문의 `mailto:`·홈페이지 링크와 섞이지 않는다.
_DOWNLOAD_PATH: Final = "/lib/download.asp"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(`gotopage` 없이 1페이지가 나온다 · 실측)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    soup = parse_html(html)
    data_rows = soup.select(_LIST_ROWS)
    if not data_rows:
        raise ParseError(f"{SOURCE_KEY} 목록: `{_LIST_ROWS}`가 비었음 — 사이트 개편 의심")
    refs = [_ref_from_row(row, source) for row in data_rows if _has_posting_number(row)]
    require_some_kept(refs, data_rows, source_key=SOURCE_KEY, filtered_by="번호 칸의 공지 아이콘")
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부."""
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    for label in body.select(_HIDDEN_LABEL):
        label.decompose()
    raw_text = normalized_text(body)
    raw_html = structural_html(body)
    images = image_urls_in(body, base_url=ref.url)
    files = _attachments(require_one(soup, _VIEW, what=f"{SOURCE_KEY} 상세 영역"), base_url=ref.url)
    require_attachment_evidence(ref, source_key=SOURCE_KEY, selector=_DOWNLOAD_PATH, found=files)
    return RawPosting(
        ref=ref, raw_text=raw_text, raw_html=raw_html, image_urls=images, attachments=files
    )


def _attachments(view: Tag, *, base_url: str) -> tuple[Attachment, ...]:
    """첨부 = 다운로드 스크립트를 가리키는 링크. 파일명은 링크 텍스트다(실측).

    `base.attachments_in`을 쓰지 않는 이유는 모듈 docstring에 있다 — 이 게시판은 본문 안에
    `mailto:`·홈페이지 링크를 담으므로 "컨테이너의 모든 링크"가 첨부가 아니다.
    """
    return tuple(
        Attachment(name=name, url=urljoin(base_url, href))
        for link in view.select(f'a[href*="{_DOWNLOAD_PATH}"]')
        if (href := str(link.get("href") or "").strip())
        and (name := link.get_text(" ", strip=True))
    )


def _has_posting_number(row: Tag) -> bool:
    """번호 칸이 숫자인 행만 공고다. 공지는 그 자리에 아이콘이 들어간다(실측)."""
    return _cell(row, _NUMBER).get_text(" ", strip=True).isdigit()


def _cell(row: Tag, index: int) -> Tag:
    """위치로 칸을 집는다. 칸 수가 모자라면 표 구조가 바뀐 것이다."""
    cells = row.find_all("td", recursive=False)
    if len(cells) < _CELL_COUNT:
        raise ParseError(
            f"{SOURCE_KEY} 목록 행의 칸이 {len(cells)}개(기대 {_CELL_COUNT}) — 표 구조가 바뀌었다"
        )
    return cells[index]


def _text(row: Tag, index: int) -> str:
    return _cell(row, index).get_text(" ", strip=True)


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = _external_id_from(urljoin(source.list_url, href), source)
    title = link.get_text(" ", strip=True)
    posted = _text(row, _DATE)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 `gotopage`·`Pagecount`·`#a1`이 붙어 있어 **정규형**으로 다시 만든다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(posted, source_key=SOURCE_KEY, cell=f"td[{_DATE}]"),
        list_meta={
            "list_title": title,
            "list_date": posted or None,
            "author": _text(row, _AUTHOR) or None,
            "views": as_int(_text(row, _VIEWS)),
            "display_no": _text(row, _NUMBER) or None,
            # 구분 칸 — 이 탭은 전부 `사역`이지만 게시판이 카테고리를 섞어 쓸 수 있어 남긴다.
            "category": _text(row, _CATEGORY) or None,
            # 상세에서 첨부 링크가 빗나갔는지 교차 확인하는 독립 신호.
            "has_attachment": bool(_cell(row, _ATTACH).select("a[href]")),
        },
    )


def _external_id_from(url: str, source: SourceConfig) -> str:
    """상세 URL의 `no`. 표시번호(674)와 다르다 — 실측 no=2375."""
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    found = external_id_from_url(url, detail_pattern=source.detail_pattern, what=SOURCE_KEY)
    return require_numeric_id(found, source_key=SOURCE_KEY)
