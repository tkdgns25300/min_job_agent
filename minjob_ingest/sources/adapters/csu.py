"""CSU(총신대 사역게시판) 어댑터 — SPA. 목록 API가 본문·첨부·구조화 필드까지 준다.

게시판 실측(2026-08-05 · 운영자 개발자도구 캡처로 파라미터 확정):

```
목록  POST /api/user/board/getBoardContentSummaryList
      form(**camelCase**): boardIdList=178 · page={n} · count=10 · includeBody=1
                           includeAttachmentList=1 · includeProperties=1
                           isAvailable=1 · isDeleted=0 · orderByCode=4 · parentBoardContentId=-1
      → {code:10000, body:{total_count:75528, list:[10건]}}
상세  받지 않는다 — `includeBody=1`이면 목록이 본문까지 준다(요청이 1/11로 줄어든다).
      (필요하면 POST /api/board/getBoardContent {id} 가 익명으로 동작한다 · 실측)
board_id  하드코딩하지 않아도 된다: POST /api/website/getMenu {id:1110} →
          `content.data.id`가 178을 준다. 다만 매 실행 요청이 하나 늘어 상수로 둔다.
```

⚠️ **`board_id`가 아니라 `boardIdList`다.** snake_case로 쓰면 서버가 본문을 못 읽고
`{"code":22000,"유효하지 않은 세션입니다"}`를 준다 — 메시지가 세션을 가리켜 쿠키를 찾게 만들지만
**쿠키는 필요 없다**(브라우저 요청에도 `Cookie` 헤더가 없다). `Content-Type`에 `charset=UTF-8`이
붙어야 하는 것도 같은 이유이고, 그건 fetch 층이 처리한다.

⚠️⚠️ **개인정보**: 목록 응답에는 공고와 무관한 **작성자 신원**이 섞여 있다 —
`properties.cert_data`(본인인증 `CI`(주민번호 파생 연계정보)·생년월일·성별·휴대폰·실명),
`registered_from_ip_address`, `registered_by_user_id/idx`. **전부 버린다**.
남기는 것은 교회가 공고에 스스로 적은 필드뿐이다.

교단이 `properties.order_name`에 명시돼 있어 이 게시판은 구조화에서 `stated`로 확정된다
(SPEC §5.3 · AI 추정 불필요).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Final
from urllib.parse import urljoin

from minjob_ingest.models import Attachment, JsonValue
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
    structural_html,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "CSU"

#: ⚠️ 상세를 따로 받지 않는다 — 목록이 `includeBody=1`로 본문을 준다(모듈 docstring).
NEEDS_DETAIL_REQUEST: Final = False

_LIST_API: Final = "/api/user/board/getBoardContentSummaryList"
#: 글 하나를 물어보는 API(삭제 확인 전용 · `gone_request`). 익명으로 동작한다(실측).
_CONTENT_API: Final = "/api/board/getBoardContent"
#: 삭제된 글에 오는 code — `"삭제된 게시물입니다."`가 함께 온다(실측 2026-08-30).
_DELETED_CODE: Final = 42004
#: 사역게시판(menu_id=1110). 취업게시판은 1111이고 이 리포 대상이 아니다.
_BOARD_ID: Final = "178"
_PER_PAGE: Final = 10
_SUCCESS_CODE: Final = 10000
#: 응답에서 본문이 담겨 오는 칸. `includeBody=1`이 먹으면 **있고, 아니면 키가 없다**(실측).
_BODY_FIELD: Final = "body"
#: 목록 JSON이 본문을 넘기는 통로. `_` 접두라 `collect`가 `raw_meta`에 저장하지 않는다.
_BODY_KEY: Final = "_body_html"
#: 첨부 목록이 `parse_detail`로 건너가는 통로(같은 이유로 `_` 접두).
_ATTACHMENTS_KEY: Final = "_attachments"
#: 첨부 경로 앞에 붙는 파일 API(실측 2026-08-23). JSON은 상대 경로만 준다
#: (`board/202608//x.pdf`) → `https://csu.ac.kr/api/file/get?path=board/202608//x.pdf`.
#:
#: ⚠️ **`/upload/`가 아니다.** 처음엔 그렇게 추측했는데 **모든 첨부가 404**였다 — 그런데도
#: 포스터가 저장돼 있어서 오래 안 드러났다. 그 포스터는 전부 **본문 인라인 그림**이고,
#: 그건 게시판 에디터가 본문 HTML에 이미 이 API 형태로 써 둔다(`html_editor/...`). 즉 인라인은
#: 되고 첨부만 안 되는 상태였다. 두 URL이 다른 모양이라 한쪽 성공이 다른 쪽을 가려 준 것이다.
_FILE_API_PREFIX: Final = "/api/file/get?path="

#: 공고에서 살릴 `properties` 키. **화이트리스트로 둔다** — 서버가 필드를 추가했을 때
#: 그것이 개인정보여도 자동으로 흘러들지 않게 하려는 것이다(`cert_data`가 그 예다).
_KEPT_PROPERTIES: Final = (
    "church_name",
    "order_name",
    "presbytery_name",
    "senior_pastor",
    "location",
    "address",
    "ministry_dept",
    "number",
    "certification",
    "apply_documents",
    "gratuity",
    "phone",
    "email",
)


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """목록은 **POST**이고 파라미터가 camelCase다(모듈 docstring 참조)."""
    if page < 1:
        raise ValueError(f"page는 1 이상이어야 함 ({page})")
    return ListRequest(
        url=urljoin(source.list_url, _LIST_API),
        form={
            "boardIdList": _BOARD_ID,
            "isAvailable": "1",
            "isDeleted": "0",
            "page": str(page),
            "count": str(_PER_PAGE),
            # 본문·첨부·공고 필드를 함께 받아 상세 요청을 없앤다.
            "includeBody": "1",
            "includeAttachmentList": "1",
            "includeProperties": "1",
            "orderByCode": "4",
            "parentBoardContentId": "-1",
        },
    )


def gone_request(source: SourceConfig, external_id: str) -> ListRequest:
    """삭제 확인 — 상세 API가 답을 직접 준다(모듈 docstring의 `getBoardContent` · 익명 동작).

    상세 HTML로는 판정할 수 없다: 살아있는 글과 삭제된 글에 **똑같은 SPA 껍데기**가 온다
    (실측 2026-08-30 · 62,885B 동일). API는 삭제된 글에 `code 42004 "삭제된 게시물입니다"`를,
    살아있는 글에 `code 10000`을 준다.
    """
    return ListRequest(url=urljoin(source.list_url, _CONTENT_API), form={"id": external_id})


def parse_gone(body: str) -> bool | None:
    """상세 API 응답 → 삭제됐나. 모르는 code는 `None` — 모르면 내리지 않는다(SPEC §4)."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    if code == _DELETED_CODE:
        return True
    if code == _SUCCESS_CODE:
        return False
    return None


def parse_list(text: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 JSON → 공고 참조들. 고정공지(`is_always_on_top`)는 제외한다."""
    rows = _require_rows(text)
    _require_body_field(rows)
    refs = [_ref_from_row(row, source) for row in rows if not _is_pinned(row)]
    if rows and not refs:
        raise ParseError(
            f"{SOURCE_KEY}: 행 {len(rows)}개가 전부 공지로 판정됨 — `is_always_on_top` 확인"
        )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """본문은 **목록에서 이미 받았다** — 인자 `html`은 빈 문자열이다."""
    if html:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 상세 HTML이 넘어왔다 —"
            f" 이 게시판은 본문을 목록에서 받는다(NEEDS_DETAIL_REQUEST=False)"
        )
    body_html = ref.list_meta.get(_BODY_KEY)
    if not isinstance(body_html, str):
        raise ParseError(f"{SOURCE_KEY} {ref.external_id}: 목록에서 본문이 넘어오지 않음")
    fragment = parse_html(body_html)
    images = image_urls_in(fragment, base_url=ref.url)
    attachments = _attachments_of(ref)
    raw_text = normalized_text(fragment)
    raw_html = structural_html(fragment)
    # ⚠️ **이미지도 증거다.** 본문이 포스터 한 장뿐인 공고가 흔하다(실측 1117808 — 성실교회).
    # 이걸 빼먹어 그런 공고를 "증거 없음"으로 버렸다(2026-08-05). 나머지 29곳은 셋을 다 본다.
    return RawPosting(
        ref=ref,
        raw_text=raw_text,
        raw_html=raw_html,
        image_urls=images,
        attachments=attachments,
    )


def _attachments_of(ref: PostingRef) -> tuple[Attachment, ...]:
    """목록이 준 첨부. 파일명이 링크 텍스트가 되도록 앵커로 만들어 공용 파서에 넘긴다.

    경로가 상대(`board/202608//x.pdf`)라 `attachments_in`이 `base_url`로 절대화한다.

    ⚠️ 접두어는 **본문 인라인 그림과 같은 파일 API**다(`_FILE_API_PREFIX` 주석 참조).
    """
    pairs = ref.list_meta.get(_ATTACHMENTS_KEY)
    if not isinstance(pairs, list) or not pairs:
        return ()
    links = "".join(
        f'<a href="{_FILE_API_PREFIX}{pair[1]}">{pair[0]}</a>'
        for pair in pairs
        if isinstance(pair, list) and len(pair) == 2
    )
    return attachments_in(parse_html(links), base_url=ref.url)


def _require_body_field(rows: Sequence[dict[str, object]]) -> None:
    """`includeBody`가 먹었는지 **페이지 단위로** 확인한다.

    ⚠️ **행 하나에 `body` 키가 없는 것은 정상이다** — 이 API는 값이 null인 키를 응답에서
    빼고(실측 40건 중 1건), 그 공고는 내용을 첨부·`properties`에 담고 있다. 그래서 행 단위로
    판정하면 정상 공고 하나가 게시판 전체를 실패시킨다.

    반대로 **한 행도** 본문을 주지 않으면 파라미터가 깨진 것이다(실측: `includeBody=0`이면 전
    행에서 키가 사라진다). 그건 게시판 전체의 본문 유실이라 조용히 넘기면 안 된다.
    """
    if rows and not any(_BODY_FIELD in row for row in rows):
        raise ParseError(
            f"{SOURCE_KEY}: 목록 {len(rows)}행 전부에 `{_BODY_FIELD}` 키가 없음 —"
            f" `includeBody=1`이 먹지 않았다(파라미터 이름·Content-Type charset 확인)"
        )


def _is_pinned(row: dict[str, object]) -> bool:
    return _as_flag(row.get("is_always_on_top"))


def _require_rows(text: str) -> list[dict[str, object]]:
    """응답에서 행 목록을 꺼낸다. 형태가 다르면 실패시킨다 — 조용한 0건을 만들지 않는다."""
    try:
        payload = json.loads(text)
    except ValueError as err:
        raise ParseError(f"{SOURCE_KEY}: 목록 응답이 JSON이 아님 ({text[:80]!r})") from err
    if not isinstance(payload, dict):
        raise ParseError(f"{SOURCE_KEY}: 목록 응답이 객체가 아님")
    code = payload.get("code")
    if code != _SUCCESS_CODE:
        # 22000 = 본문을 못 읽었다는 뜻이다(파라미터 이름·Content-Type charset 확인).
        raise ParseError(
            f"{SOURCE_KEY}: API가 실패를 반환 (code={code}, {payload.get('message')!r})"
        )
    body = payload.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("list"), list):
        raise ParseError(f"{SOURCE_KEY}: 응답에 `body.list`가 없음")
    return [row for row in body["list"] if isinstance(row, dict)]


def _ref_from_row(row: dict[str, object], source: SourceConfig) -> PostingRef:
    external_id = _text(row, "id")
    title = _text(row, "title")
    if not external_id or not title:
        raise ParseError(f"{SOURCE_KEY}: 목록 행에 `id`/`title`이 없음 — JSON 필드가 바뀌었다")
    meta: dict[str, JsonValue] = {
        "list_title": title,
        "list_date": _text(row, "registered_date") or None,
        "views": as_int(_text(row, "view_count")),
        "has_attachment": bool(as_int(_text(row, "attachment_count"))),
        _BODY_KEY: _text(row, _BODY_FIELD),
        _ATTACHMENTS_KEY: _attachment_pairs(row),
    }
    meta.update(_public_properties(row))
    return PostingRef(
        external_id=external_id,
        url=detail_url(source, external_id),
        title=title,
        posted_on=_posted_on(row),
        list_meta=meta,
    )


def _public_properties(row: dict[str, object]) -> dict[str, JsonValue]:
    """공고 필드만 화이트리스트로 옮긴다.

    ⚠️ `properties`를 통째로 넣으면 `cert_data`(작성자 본인인증 정보)가 그대로 저장된다 —
    공고 내용이 아니라 **신원 정보**라 저장하지 않는다.
    """
    properties = row.get("properties")
    if not isinstance(properties, dict):
        return {}
    kept: dict[str, JsonValue] = {}
    for name in _KEPT_PROPERTIES:
        value = properties.get(name)
        if isinstance(value, str) and value.strip():
            kept[name] = value.strip()
    return kept


def _attachment_pairs(row: dict[str, object]) -> list[JsonValue]:
    """첨부를 `[[파일명, 경로], …]`로. `parse_detail`이 여기서 받아 앵커로 만든다."""
    found = row.get("attachment_list")
    if not isinstance(found, list):
        return []
    return [
        [str(item.get("original_filename") or ""), str(item.get("url") or "")]
        for item in found
        if isinstance(item, dict) and item.get("url")
    ]


def _posted_on(row: dict[str, object]) -> date:
    """`registered_date`는 `YYYY-MM-DD HH:MM:SS`다 — 날짜 부분만 쓴다."""
    text = _text(row, "registered_date")
    if not text:
        raise ParseError(f"{SOURCE_KEY}: `registered_date`가 비었음 — JSON 필드 확인")
    try:
        return date.fromisoformat(text.split(" ")[0])
    except ValueError as err:
        raise ParseError(f"{SOURCE_KEY}: `registered_date` 형식이 예상과 다름 ({text!r})") from err


def _as_flag(value: object) -> bool:
    """이 API는 boolean을 0/1 정수로 준다."""
    return value in (1, "1", True)


def _text(row: dict[str, object], name: str) -> str:
    """JSON 칸을 문자열로. `id`·`view_count`는 정수로 오므로 둘 다 받는다."""
    value = row.get(name)
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    return value.strip() if isinstance(value, str) else ""
