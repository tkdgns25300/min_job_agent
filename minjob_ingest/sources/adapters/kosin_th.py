"""KOSIN_TH(고신대 신학과 자유게시판) 어댑터 — IIS/PHP 게시판.

게시판 실측(2026-08-04 · fixture `tests/fixtures/KOSIN_TH/`):

```
목록  /th/index.php?pCode=MN6000030&mode=list      2페이지 이상은 &pg={n} (실측 총 29페이지)
      table.isDataList tr 17 = 헤더 1(th) + 공지 1(tr.isnotice) + 공고 15
      칸: td.f-num td.f-tit(링크 + 첨부 아이콘) td.f-nm td.f-date(YYYY-MM-DD) td.f-hits
      공지행의 번호 칸에는 숫자가 아니라 `공지` 이미지가 들어간다.
상세  같은 index.php의 &mode=view&idx={id}
      본문 div.board-view-contents · 첨부 div.board-view-files
```

⚠️ **청빙과 무관한 글(취업과정 홍보)·다른 교단 공고가 섞여 있다.** 어댑터가 걸러내지 않는다 —
제목만 보고 자르면 진짜 청빙을 조용히 잃는다(DAESHIN과 같은 판단). 교단은 공고별로 판정하고
(SPEC §5.3) 청빙 여부는 게이트1이 정한다 — 게시판의 교단 힌트(GOSIN)는 힌트일 뿐이다(가드레일 #6).

⚠️ **첨부에 다운로드 링크가 없다.** 이 게시판은 첨부를 `mode=fv&idx=…&num=N` 이미지로 본문 위에
렌더한다(실측 idx=304339: 공고 포스터 1장). 그래서 이미지형 첨부는 `image_urls`로 들어가고
`attachments`는 앵커가 있을 때만 채워진다.

⚠️ **2페이지부터 상세 href에 `pg`가 끼어든다** — `?pCode=…&pg=2&mode=view&idx=291245`(실측).
config `detail_pattern`의 접두사(`?pCode=…&mode=view&idx=`)로 자르면 **2페이지 이후 전 행이
탈락**한다. 그래서 위치가 아니라 **쿼리 파라미터 이름**으로 뽑는다(1페이지만 보면 안 드러난다).
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
    external_id_from_query,
    image_urls_in,
    normalized_text,
    parse_html,
    require_attachment_evidence,
    require_date,
    require_one,
    require_some_kept,
    rows_with_data,
    structural_html,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "KOSIN_TH"

_LIST_TABLE: Final = "table.isDataList"
_DETAIL_LINK: Final = "td.f-tit a[href]"
_DATE_CELL: Final = "td.f-date"
_NO_CELL: Final = "td.f-num"
#: 고정공지 — class와 번호 칸(숫자 없음) 두 신호를 독립적으로 본다(실측: 둘 다 있다).
_NOTICE_CLASS: Final = "isnotice"
#: 첨부 표시 아이콘의 alt(실측). 상세 첨부·이미지가 빗나갔는지 보는 독립 신호.
_ATTACHMENT_ICON_ALT: Final = "첨부파일있음"
_PAGE_PARAM: Final = "pg"
#: 상세 URL이 담는 글번호의 쿼리 파라미터 이름(config `detail_pattern`과 같은 이름).
_ID_PARAM: Final = "idx"
#: 본문 텍스트만. 첨부 이미지는 형제 컨테이너에 있어 여기 들어오지 않는다(실측).
_BODY: Final = "div.board-view-contents"
#: 첨부 컨테이너. 이미지는 `<img>`로, (있다면) 파일은 앵커로 나온다.
#: ⚠️ 앵커 수집을 본문까지 넓히지 않는다 — 본문에 교회 홈페이지 링크가 흔해 첨부로 오인된다(실측).
_FILE_BOX: Final = "div.board-view-files"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(`mode=list`가 이미 들어 있다)."""
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

    본문이 비어 있어도 실패로 보지 않는다 — 내용을 포스터 이미지 한 장으로만 올리는 공고가 있고
    그때는 그것이 유일한 증거다(Gemini 멀티모달이 읽는다 · SPEC §5).
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    file_box = soup.select_one(_FILE_BOX)
    raw_text = normalized_text(body)
    raw_html = structural_html(body)
    images = image_urls_in(body, file_box, base_url=ref.url)
    files = attachments_in(file_box, base_url=ref.url)
    require_attachment_evidence(
        ref, source_key=SOURCE_KEY, selector=_FILE_BOX, found=(*files, *images)
    )
    return RawPosting(
        ref=ref, raw_text=raw_text, raw_html=raw_html, image_urls=images, attachments=files
    )


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 번호 칸이 이미지(`공지`)라 텍스트가 비고 숫자가 아니다(실측)."""
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, _NO_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = external_id_from_query(
        urljoin(source.list_url, href), param=_ID_PARAM, source_key=SOURCE_KEY
    )
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href에 페이지·검색 상태가 붙어도 원장 키가 흔들리지 않게 **정규형**으로 만든다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "author": cell_text(row, "td.f-nm") or None,
            "views": as_int(cell_text(row, "td.f-hits")),
            "display_no": cell_text(row, _NO_CELL) or None,
            "has_attachment": bool(row.select(f'img[alt="{_ATTACHMENT_ICON_ALT}"]')),
        },
    )
