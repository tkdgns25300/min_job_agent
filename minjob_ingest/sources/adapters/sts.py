"""STS(순복음대학원대 청빙및취업) 어댑터 — anyboard 스킨.

게시판 실측(2026-08-04 · fixture `tests/fixtures/STS/`):

```
목록  /main/sub.html?pageCode=38       2페이지 이상은 `&page={n}`
      table.jmboardskin1 · tr 17 = 헤더 1(th) + 여백행 1(tr.jTh2) + 공고 15
      칸: td.jNum(표시번호) td.jSubject(링크) td.jWriter td.jDate(YYYY.MM.DD) td.jView
상세  /main/sub.html?Mode=view&boardID=www38&num={id}    본문 = div.mdView_cont
```

⚠️ **목록과 상세가 같은 `sub.html`인데 파라미터가 다르다** — 목록은 `pageCode=38`,
상세는 `boardID=www38`이다(같은 게시판을 가리키는 두 이름). config가 둘을 각각 들고 있으므로
어댑터는 `list_url`에 페이지만 얹고, 상세 URL은 `detail_pattern`으로 만든다.

⚠️ **제목을 `td.jSubject` 텍스트로 읽으면 안 된다.** 그 칸에는 모바일용 `<p>`가 함께 있어
ANYSECURE 암호문(작성자)·등록일·조회수가 제목 뒤에 붙는다(실측). 앵커 텍스트만 쓴다.

작성자는 `list_meta`에 담지 않는다 — 이 스킨은 작성자명을 ANYSECURE 암호문
(`eyJjdCI6…`)으로만 내려주므로 저장해도 읽을 수 없고, 개인정보 최소 원칙에도 맞지 않는다.
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
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "STS"

_LIST_TABLE: Final = "table.jmboardskin1"
_DETAIL_LINK: Final = "td.jSubject a[href]"
_DATE_CELL: Final = "td.jDate"
#: 표시번호 칸. **숫자가 아니면 공고가 아니다** — 고정공지는 여기에 `공지`가 들어가고,
#: 헤더 아래 여백행(`tr.jTh2`)은 `colspan` 한 칸뿐이라 이 칸이 아예 없다(실측).
#: 두 경우를 한 규칙으로 걸러낸다.
_NUMBER_CELL: Final = "td.jNum"
_VIEWS_CELL: Final = "td.jView"
_PAGE_PARAM: Final = "page"
#: 본문. 안쪽 `#lightgallery`에 에디터 내용이 들어간다.
_BODY: Final = "div.mdView_cont"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로.

    게시판 자신의 페이징 링크는 `?page=N&boardID=www38`(pageCode 없음) 형태지만, 우리는
    `pageCode=38&page=N`으로 요청한다 — 라이브 확인 결과 같은 결과가 나온다(실측: 2페이지
    글번호 7133~6801). config의 `list_url` 하나만 정본으로 두기 위해서다.
    """
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지·여백행은 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if _has_posting_number(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"표시번호 판정(`{_NUMBER_CELL}`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    ⚠️ 첨부가 달린 공고를 아직 실측하지 못했다 — 목록에 첨부 표시 칸도 없어 교차 확인 신호가
    없다(YTUS의 `has_attachment` 같은 것). 첨부는 **본문 안에서만** 찾는다. 넓히면 스킨 푸터의
    SNS 아이콘과 PREV/NEXT 링크가 첨부로 들어온다.
    """
    body = require_one(parse_html(html), _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(body, base_url=ref.url)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _has_posting_number(row: Tag) -> bool:
    """표시번호가 숫자인 행만 공고다(모듈 상단 `_NUMBER_CELL` 주석 참조)."""
    return cell_text(row, _NUMBER_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = _external_id_from(urljoin(source.list_url, href), source)
    # 앵커 텍스트만 제목이다 — 칸 전체를 읽으면 암호문·날짜·조회수가 붙는다(모듈 docstring).
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 빈 검색·페이지 파라미터(`&page=&keyfield=&key=&bCate=`)가 달려 있어
        # **정규형**으로 다시 만든다 — 같은 글의 `source_url`이 달라지면 안 된다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NUMBER_CELL) or None,
        },
    )


def _external_id_from(url: str, source: SourceConfig) -> str:
    """상세 URL의 `num`. 표시번호(`td.jNum`)가 아니다 — 실측 111 vs 7319."""
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    found = external_id_from_url(url, detail_pattern=source.detail_pattern, what=SOURCE_KEY)
    return require_numeric_id(found, source_key=SOURCE_KEY)
