"""KTS(고려신학대학원 교역자초빙) 어댑터 — 그누보드5 기본 스킨.

게시판 실측(2026-08-04 · fixture `tests/fixtures/KTS/`):

```
목록  /home/pinvit                 2페이지 이상은 ?page={n}
      div.tbl_head01 table · tr 17 = 헤더 1 + 공지 1(tr.bo_notice) + 공고 15
      칸: td.td_num2(번호) td.td_subject(제목·첨부아이콘) td.td_name td.td_num(조회) td.td_datetime
상세  /home/pinvit/{id}            본문 = #bo_v_con · 첨부 = #bo_v_file
```

⚠️ **목록 게시일에 연도가 없다** — 오늘 글은 `15:58`, 그 이전 글은 `09-26`이다.
연도 복원은 `gnuboard_list_date`가 한다(아래 · 그누보드 계열 공용).

⚠️ 표시번호(`td.td_num2` 10976)와 URL의 글번호(31528)는 다르다 — 표시번호는 게시판이 다시
매기는 값이고(끌어올림으로 순서가 뒤집힌다) 원장 키가 되어야 하는 것은 URL의 id다.
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
    parse_html,
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "KTS"

_LIST_TABLE: Final = "div.tbl_head01 table"
#: 고정공지 — 그누보드5 기본 스킨은 `tr`에 클래스를 준다(스킨을 바꾼 게시판은 다르다).
_NOTICE_CLASS: Final = "bo_notice"
_DETAIL_LINK: Final = "td.td_subject a[href]"
_NO_CELL: Final = "td.td_num2"
_AUTHOR_CELL: Final = "td.td_name"
_VIEWS_CELL: Final = "td.td_num"
_DATE_CELL: Final = "td.td_datetime"
#: 목록의 첨부 표시(실측 3행). 상세에서 첨부 셀렉터가 빗나갔는지 보는 **독립 신호**다.
_ATTACHMENT_ICON: Final = "i.fa-download"
_PAGE_PARAM: Final = "page"

_BODY: Final = "#bo_v_con"
_FILE_BOX: Final = "#bo_v_file"

# ── 그누보드 목록 날짜 ────────────────────────────────────────────
# ⚠️ **base.py 공용화 후보.** 연도 없는 목록 날짜는 그누보드 계열 전체(KTS·HAPSHIN·…)가
# 공유하는 문제라 base에 있어야 하지만, 지금 여러 갈래가 동시에 어댑터를 놓는 중이라
# base.py를 건드리지 않는다. 그누보드 어댑터는 여기서 가져다 쓴다.

#: 게시판이 표시하는 시각의 기준. 러너는 UTC라 그대로 쓰면 KST 00~09시에 올라온 글이
#: **어제**가 된다 — 하루가 어긋나면 백필 컷오프와 §7 급감 경보가 함께 흔들린다.
# ── 어댑터 계약 ───────────────────────────────────────────────────


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(clean URL이라 쿼리가 없다 · 실측)."""
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
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NOTICE_CLASS}`·빈 번호)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부."""
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    files = attachments_in(soup.select_one(_FILE_BOX), base_url=ref.url)
    if ref.list_meta.get("has_attachment") and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 목록에 첨부 아이콘이 있는데 첨부가 0건 —"
            f" 셀렉터 `{_FILE_BOX}` 확인"
        )
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 클래스와 번호 칸(공지는 숫자 대신 `공지`)을 독립적으로 본다."""
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, _NO_CELL).isdigit()


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
    subject = row.select_one("td.td_subject")
    return PostingRef(
        external_id=external_id,
        url=detail_url(source, external_id),
        title=title,
        posted_on=gnuboard_list_date(shown_date, source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            # 연도 없는 원문을 그대로 남긴다 — 복원한 연도가 틀렸을 때 대조할 근거가 된다.
            "list_date": shown_date or None,
            "author": cell_text(row, _AUTHOR_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NO_CELL) or None,
            "has_attachment": subject is not None and bool(subject.select(_ATTACHMENT_ICON)),
        },
    )
