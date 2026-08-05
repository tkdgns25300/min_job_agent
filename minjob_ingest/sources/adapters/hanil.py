"""HANIL(한일장신대 청빙게시판) 어댑터 — **목록이 JSON이고 본문까지 담고 있다.**

게시판 실측(2026-08-04):

```
목록  POST /portal/bbs/article_list.ajax
      form: boardId=BBS…262 · menuId=M0004000500000000 · pageIndex={n}
      → {cnt: 12497, list: [10건], isSuccess}
      행: boardSeq(=id) title contents(**본문 HTML**) createDt(YYYYMMDD)
          noticeYn count createUser classification isFile fileSeq
상세  받지 않는다 — view.do 는 JS가 채우는 **빈 껍데기**다(115KB인데 공고 제목조차 없다).
```

⚠️ 그래서 `NEEDS_DETAIL_REQUEST = False`다. 상세를 요청하면 글마다 쓸모없는 요청이 하나씩
늘어난다(3개월 백필이면 수백 건). 본문은 `parse_list`가 `list_meta["_body_html"]`로 넘기고
`parse_detail`이 텍스트로 바꾼다 — `_` 접두 키는 `collect`가 `raw_meta`에 저장하지 않는다
(그러면 `raw_text`와 그대로 중복된다).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Final
from urllib.parse import urljoin

from minjob_ingest.sources.adapters.base import (
    ListRequest,
    ParseError,
    PostingRef,
    RawPosting,
    as_int,
    as_listing,
    attachments_in,
    image_urls_in,
    normalized_text,
    parse_html,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "HANIL"

#: ⚠️ 상세 페이지를 받지 않는다(모듈 docstring 참조). `collect`가 이 값을 본다.
NEEDS_DETAIL_REQUEST: Final = False

_LIST_API: Final = "/portal/bbs/article_list.ajax"
_BOARD_ID: Final = "BBS00000000000000262"
_MENU_ID: Final = "M0004000500000000"
_PAGE_FIELD: Final = "pageIndex"
#: 목록 JSON이 본문을 넘기는 통로. `_` 접두라 `raw_meta`에 저장되지 않는다.
_BODY_KEY: Final = "_body_html"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """목록은 **POST**다. URL 하나로는 표현할 수 없어 `ListRequest`가 form을 함께 든다."""
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    # 호스트를 하드코딩하지 않는다 — config의 `list_url`이 정본이다(www 강제도 거기에 있다).
    return ListRequest(
        url=urljoin(source.list_url, _LIST_API),
        form={"boardId": _BOARD_ID, "menuId": _MENU_ID, _PAGE_FIELD: str(page)},
    )


def parse_list(text: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 JSON → 공고 참조들. 고정공지(`noticeYn == "Y"`)는 제외한다.

    HTML이 아니라 JSON을 받는다 — 계약상 인자 이름은 같지만 내용이 다르다.
    """
    rows = _require_rows(text)
    refs = [_ref_from_row(row, source) for row in rows if str(row.get("noticeYn", "")) != "Y"]
    if rows and not refs:
        raise ParseError(f"{SOURCE_KEY}: 행 {len(rows)}개가 전부 공지로 판정됨 — `noticeYn` 확인")
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """본문은 **목록에서 이미 받았다** — 인자 `html`은 빈 문자열이다(`NEEDS_DETAIL_REQUEST`).

    ⚠️ `html`이 비어 있지 않으면 호출자가 규칙을 어긴 것이므로 조용히 무시하지 않는다.
    """
    if html:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 상세 HTML이 넘어왔다 —"
            f" 이 게시판은 본문을 목록에서 받는다(NEEDS_DETAIL_REQUEST=False)"
        )
    body_html = ref.list_meta.get(_BODY_KEY)
    if not isinstance(body_html, str) or not body_html.strip():
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 목록에서 넘어온 본문이 없음 — `contents` 필드 확인"
        )
    fragment = parse_html(body_html)
    return RawPosting(
        ref=ref,
        raw_text=normalized_text(fragment),
        image_urls=image_urls_in(fragment, base_url=ref.url),
        attachments=attachments_in(fragment, base_url=ref.url),
    )


def _require_rows(text: str) -> list[dict[str, object]]:
    """응답에서 행 목록을 꺼낸다. 형태가 다르면 실패시킨다 — 조용한 0건을 만들지 않는다."""
    try:
        payload = json.loads(text)
    except ValueError as err:
        raise ParseError(f"{SOURCE_KEY}: 목록 응답이 JSON이 아님 ({text[:80]!r})") from err
    if not isinstance(payload, dict) or "list" not in payload:
        raise ParseError(f"{SOURCE_KEY}: 목록 응답에 `list`가 없음 (키 {sorted(payload)[:6]})")
    rows = payload["list"]
    if not isinstance(rows, list):
        raise ParseError(f"{SOURCE_KEY}: `list`가 배열이 아님 ({type(rows).__name__})")
    return [row for row in rows if isinstance(row, dict)]


def _ref_from_row(row: dict[str, object], source: SourceConfig) -> PostingRef:
    external_id = _require_field(row, "boardSeq")
    title = _require_field(row, "title")
    return PostingRef(
        external_id=external_id,
        url=detail_url(source, external_id),
        title=title,
        posted_on=_posted_on(row),
        list_meta={
            "list_title": title,
            "list_date": _text(row, "createDt") or None,
            "author": _text(row, "createUser") or None,
            "views": as_int(_text(row, "count")),
            # 게시판이 스스로 붙인 분류('청빙'·'구인' 등). 교단·직무 판정은 구조화가 한다.
            "classification": _text(row, "classification") or None,
            "has_attachment": _text(row, "isFile") == "Y",
            _BODY_KEY: _text(row, "contents"),
        },
    )


def _posted_on(row: dict[str, object]) -> date:
    """`createDt`는 구분자 없는 `YYYYMMDD`다 — 공용 `require_date`(구분자 기대)와 형태가 다르다."""
    text = _text(row, "createDt")
    if not text:
        raise ParseError(f"{SOURCE_KEY}: `createDt`가 비었음 — 목록 JSON 필드 확인")
    try:
        return date.fromisoformat(text)
    except ValueError as err:
        raise ParseError(f"{SOURCE_KEY}: `createDt` 형식이 예상과 다름 ({text!r})") from err


def _require_field(row: dict[str, object], name: str) -> str:
    value = _text(row, name)
    if not value:
        raise ParseError(f"{SOURCE_KEY}: 목록 행에 `{name}`이 없음 — JSON 필드가 바뀌었다")
    return value


def _text(row: dict[str, object], name: str) -> str:
    """JSON 칸을 문자열로. 타입이 섞여 있어 둘 다 받는다.

    ⚠️ 두 가지 함정이 있다(실측): `boardSeq`·`count`는 **정수**로 오고(문자열만 받으면 id를
    통째로 놓친다), 빈 값은 `None`이 아니라 **문자열 `"None"`**으로 온다(그대로 쓰면 작성자가
    "None"인 공고가 생긴다).
    """
    value = row.get(name)
    if isinstance(value, bool):  # bool은 int의 하위형이라 먼저 걸러낸다
        return ""
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str) or value == "None":
        return ""
    return value.strip()
