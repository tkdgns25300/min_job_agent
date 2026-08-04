"""YTUS(영남신대 취업/초빙) 어댑터 — 1소스 관통용 첫 구현.

게시판 실측(2026-08-04 · fixture `tests/fixtures/YTUS/`):

```
목록  /board/list/trXXR            페이지 2 이상은 /page/{n}
      tr 21 = 헤더 1 + 공지 2(tr.notice-row) + 공고 18
      칸: td.num(표시번호) td.title(링크) td.author td.rdate(YYYY-MM-DD) td.rnum(조회) td.list-file
상세  /board/view/trXXR/{id}       본문 = div.boardViewContent
      양식 게시판 — `교회명 :` `교단명 : 통합` `전화번호 :` 처럼 라벨이 붙는다.
      **교단이 본문에 명시**되므로 구조화에서 `stated`로 확정된다(SPEC §5.3 · AI 추정 불필요).
```

⚠️ **표시번호(`td.num`)를 `external_id`로 쓰지 않는다.** 실측에서 표시번호 16718과 URL의
25581이 달랐다 — 표시번호는 게시판이 다시 매기는 값이고, 원장 키가 되어야 하는 것은 URL의 id다.
"""

from __future__ import annotations

from datetime import date
from typing import Final
from urllib.parse import urljoin

from bs4 import Tag

from minjob_ingest.clock import parse_iso_date
from minjob_ingest.models import Attachment
from minjob_ingest.sources.adapters.base import (
    ParseError,
    PostingRef,
    RawPosting,
    as_listing,
    attachments_in,
    cell_text,
    external_id_from_url,
    image_urls_in,
    normalized_text,
    parse_html,
    require_one,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "YTUS"

_LIST_TABLE: Final = "div.boardList table"
_ROW: Final = "tr"
#: 고정공지 — 두 신호가 독립적으로 존재한다(class + 빈 표시번호). 하나가 바뀌어도 걸린다.
_NOTICE_CLASS: Final = "notice-row"
_DETAIL_LINK: Final = "td.title a[href]"
_BODY: Final = "div.boardViewContent"
#: 이미지형 첨부의 **미리보기**. 본문의 형제라 본문만 훑으면 놓친다(실측).
_IMAGE_PREVIEW: Final = "div.pnlAttachedImage"
#: **첨부 전체 목록**(다운로드 링크 + 파일명). 여기만 HWP·PDF까지 나온다(실측).
_FILE_LIST: Final = "div.view-file"
#: 2페이지 이상은 목록 URL 뒤에 `/page/{n}`이 붙는다(실측).
_PAGE_SEGMENT: Final = "/page/"


def list_page_url(source: SourceConfig, page: int) -> str:
    """N페이지 목록의 절대 URL. 1페이지는 `list_url` 그대로.

    ⚠️ 페이지 규칙이 config가 아니라 여기 있는 이유: 31곳의 pagination 형태가
    쿼리(`?page=N`)·경로(`/page/N`)·POST 본문으로 갈려 **한 소스만 보고 config 필드를 설계하면
    나머지 30곳에서 안 맞는다**. 1-4에서 형태가 모이면 config로 올린다(ROADMAP).
    """
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    if page == 1:
        return source.list_url
    return f"{source.list_url.rstrip('/')}{_PAGE_SEGMENT}{page}"


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다.

    행이 0건인 것(공고가 없는 페이지)과 테이블 자체가 없는 것(사이트 개편)을 구분한다 —
    후자는 `ParseError`다. 빈 리스트로 돌려주면 "정상인데 0건"으로 기록돼 셀렉터가 깨진 걸
    아무도 모른다.
    """
    soup = parse_html(html)
    table = require_one(soup, _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = [row for row in table.select(_ROW) if row.select("td")]
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_skippable_row(row)]
    if data_rows and not refs:
        # 데이터 행이 있는데 전부 걸러졌다 = 공지 판정 기준(`td.num`)이 안 맞는다.
        # 빈 결과로 흘리면 "정상인데 0건"으로 기록돼 셀렉터가 깨진 걸 아무도 모른다.
        raise ParseError(
            f"{SOURCE_KEY}: 데이터 행 {len(data_rows)}개가 전부 공지로 판정됨 —"
            f" `td.num` 셀렉터 확인(사이트 개편 의심)"
        )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 원문 + 이미지 URL.

    본문이 **비어 있어도 실패로 보지 않는다** — 본문을 이미지·첨부로만 올리는 공고가 있고,
    그때는 그것이 유일한 내용이다(구조화가 Gemini 멀티모달로 읽는다 · SPEC §3).
    셋 다 없으면 파싱이 빗나간 것이므로 실패다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    # ⚠️ 미리보기(`_IMAGE_PREVIEW`)를 `image_urls`에 넣지 않는다 — 같은 첨부가 두 URL
    # (`/filelink/…` 미리보기 · `/download/…` 다운로드)로 **중복 저장**돼 바이트 fetch와
    # Gemini 비용이 두 배가 됐다(실측). 이미지형 첨부는 `attachments`의 `is_image`로 잡힌다.
    # 미리보기는 첨부 목록 셀렉터가 빗나갔는지 **교차 확인**하는 데만 쓴다.
    images = image_urls_in(body, base_url=ref.url)
    preview = soup.select_one(_IMAGE_PREVIEW)
    files = attachments_in(soup.select_one(_FILE_LIST), base_url=ref.url)
    _check_attachments_found(ref, preview=preview, files=files)
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _check_attachments_found(
    ref: PostingRef, *, preview: Tag | None, files: tuple[Attachment, ...]
) -> None:
    """첨부가 있다는 **독립 신호**와 대조한다.

    `_FILE_LIST` 셀렉터가 빗나가면 첨부가 조용히 0개가 되고, 본문이 있는 공고(대다수)는
    "정상인데 첨부 0개"로 통과한다. 페이지에 이미 두 신호가 있으니 쓴다 —
    상세의 이미지 미리보기, 목록의 첨부 아이콘(`list_meta["has_attachment"]`).
    """
    expected = (preview is not None and bool(preview.select("img"))) or bool(
        ref.list_meta.get("has_attachment")
    )
    if expected and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 첨부가 있다고 표시됐는데 목록이 비었음 —"
            f" 셀렉터 `{_FILE_LIST}` 확인"
        )


def _is_skippable_row(row: Tag) -> bool:
    """고정공지 행. 두 신호를 독립적으로 본다(class + 빈 표시번호)."""
    classes: list[str] = row.get_attribute_list("class")
    return _NOTICE_CLASS in classes or not cell_text(row, "td.num")


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = _external_id_from(urljoin(source.list_url, href), source)
    return PostingRef(
        external_id=external_id,
        # 목록 링크를 그대로 쓰지 않고 **정규형**으로 만든다 — 2페이지 링크에는 `/page/2`가
        # 붙어 있어, 같은 글을 1페이지에서 찾았을 때와 `source_url`이 달라진다.
        url=detail_url(source, external_id),
        title=link.get_text(" ", strip=True),
        posted_on=_posted_on(row),
        list_meta={
            "list_title": link.get_text(" ", strip=True),
            "list_date": cell_text(row, "td.rdate") or None,
            "author": cell_text(row, "td.author") or None,
            "views": _as_int(cell_text(row, "td.rnum")),
            "display_no": cell_text(row, "td.num") or None,
            # 상세에서 첨부 셀렉터가 빗나갔는지 교차 확인하는 독립 신호.
            "has_attachment": bool(row.select("td.list-file img")),
        },
    )


def _external_id_from(url: str, source: SourceConfig) -> str:
    """상세 URL의 글번호. **표시번호가 아니라 이 값이 원장 키다**(모듈 docstring 참조)."""
    if source.detail_pattern is None:  # 레지스트리가 로드 시 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    found = external_id_from_url(url, detail_pattern=source.detail_pattern, what=SOURCE_KEY)
    if not found.isdigit():
        raise ParseError(f"{SOURCE_KEY}: 글번호가 숫자가 아님 ({found!r}) — 링크 형태가 바뀌었다")
    return found


def _posted_on(row: Tag) -> date:
    """목록의 작성일. **조용히 None으로 흘리지 않는다** — 날짜는 백필 컷오프의 유일한
    기준이라(SPEC §4) 없거나 형식이 다르면 범위가 무의미해진다."""
    text = cell_text(row, "td.rdate")
    if not text:
        # YTUS는 전 행에 작성일이 있다(실측) → 비면 셀렉터가 깨진 것이다.
        # `PostingRef.posted_on=None`은 "날짜 칸이 없는 게시판"용 계약이라 이 침묵과 구분되지
        # 않는다 → 여기서 실패시켜 백필 범위가 조용히 무의미해지는 것을 막는다.
        raise ParseError(f"{SOURCE_KEY}: 작성일 칸이 비었음 — 셀렉터 `td.rdate` 확인")
    try:
        return parse_iso_date(text)
    except ValueError as err:
        raise ParseError(f"{SOURCE_KEY}: 작성일 형식이 예상과 다름 ({text!r})") from err


def _as_int(text: str) -> int | None:
    digits = text.replace(",", "").strip()
    return int(digits) if digits.isdigit() else None
