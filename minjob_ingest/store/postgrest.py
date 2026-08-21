"""PostgREST 전송 — Supabase 저장소가 HTTP로 말하는 **유일한 창구**.

⚠️ **`fetch/client.py`와 섞지 않는다.** 그쪽은 게시판용이라 브라우저 UA 위장 · robots
`Crawl-delay` · 소스별 최소 간격이 붙어 있다. 그 정책이 우리 DB 요청에 새면 행 하나 쓸 때마다
1.5초를 기다리고, 우리 DB에 대고 남의 브라우저를 흉내내게 된다. 같은 `httpx`를 쓸 뿐 정책은
공유하지 않는다.

**계약**: 주고받는 것은 전부 JSON이다(uuid·timestamptz·date는 **문자열**) — `store/serde.py`의
행 값 계약과 같다. 그래서 이 층은 값을 변환하지 않고 그대로 옮긴다.

**실패는 `StoreError` 하나로 나온다.** 네트워크 오류든 4xx든 "원장을 못 썼다"는 같은 뜻이고,
runner가 그것으로 연속 실패를 세어 실행을 멈춘다(CLAUDE.md Runner 규칙). 예외 메시지에
**요청 헤더를 넣지 않는다** — 거기 `service_role` 키가 있다.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Final

import httpx

from minjob_ingest.models import JsonValue
from minjob_ingest.settings import SupabaseSettings
from minjob_ingest.store.base import StoreError

_LOG = logging.getLogger(__name__)

#: 요청 상한. 서버 쪽 `statement_timeout`이 8초라 그보다 넉넉히 잡는다 — 짧게 잡으면 DB가
#: 보내주는 "왜 느렸나"를 못 보고 클라이언트가 먼저 끊어 원인을 잃는다.
REQUEST_TIMEOUT_SECONDS: Final = 30.0

#: 전량 조회 한 페이지 크기. 서버가 응답 행 수를 자르더라도 우리가 먼저 끊어 받는다.
PAGE_SIZE: Final = 1000

#: `in.(...)` 한 번에 넣을 값 수. URL이 길어지면 서버·프록시가 414로 거절한다.
MAX_IN_VALUES: Final = 200

#: 총 시도 횟수(최초 1 + 재시도 2) — `fetch/client.py`와 같은 셈법.
MAX_ATTEMPTS: Final = 3
_RETRY_BASE_DELAY_SECONDS: Final = 0.5
_RETRY_MAX_DELAY_SECONDS: Final = 8.0
_MAX_RETRY_AFTER_SECONDS: Final = 30.0
_RETRY_JITTER_FLOOR: Final = 0.5

#: 일시적 오류만 재시도한다. 4xx는 대개 우리 요청이 틀린 것이라 다시 보내도 같은 답이 온다.
_RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

#: `Content-Range`에서 총 행 수. ⚠️ **범위 쪽이 `*`인 형식도 온다** — 돌려줄 행이 없으면
#: PostgREST가 `*/0`(빈 표)·`*/2`(offset이 끝을 넘음)로 답한다(2026-08-21 실측). 그 형식을
#: 못 읽으면 **빈 표를 읽는 것만으로 전량 조회가 실패한다**. 세지 않았을 때는 총계가 `*`다.
_CONTENT_RANGE: Final = re.compile(r"^(?:\d+-\d+|\*)/(?P<total>\d+|\*)$")

#: 오가는 행 한 건. 값은 JSON 타입뿐이다(위 계약).
#: ⚠️ `serde.Row`(= `Mapping[str, object]`)와 **이름을 겹치지 않게** 둔다 — 같은 이름이 두 뜻을
#: 가지면 어느 계약을 읽는지 사람이 매번 확인해야 한다. 디코딩은 serde가 하고 여기는 옮기기만 한다.
type JsonRow = Mapping[str, JsonValue]

#: 백오프 대기. 테스트가 실제로 기다리지 않게 주입한다(`fetch/client.py`와 같은 방식) —
#: 재시도 간격도 검증 대상이다.
type Sleeper = Callable[[float], None]


class PostgrestClient:
    """PostgREST 요청 하나하나. 저장 의미(무엇이 원장인가)는 모른다 — 그건 `SupabaseStore`다."""

    def __init__(
        self,
        settings: SupabaseSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleeper = time.sleep,
    ) -> None:
        # `transport`는 테스트가 가짜 PostgREST를 끼우는 자리다(네트워크 없이 계약 검증).
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=settings.rest_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
            headers={
                "apikey": settings.service_role_key,
                "Authorization": f"Bearer {settings.service_role_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PostgrestClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── 조회 ────────────────────────────────────────────────────

    def select(
        self,
        table: str,
        *,
        columns: str,
        order: str,
        filters: Mapping[str, str] | None = None,
        limit: int | None = None,
    ) -> list[JsonRow]:
        """행을 읽는다. `limit`이 없으면 **전량**(페이지네이션 + 개수 검산).

        ⚠️ **`order`가 필수 인자다.** 정렬 없이 `offset`으로 페이지를 넘기면 Postgres가 행
        순서를 보장하지 않아 **어떤 행은 두 번, 어떤 행은 한 번도 오지 않는다**. 기본값을 두면
        호출자가 잊고, 잊었다는 사실이 조용한 데이터 손실로만 드러난다.
        """
        params = {"select": columns, "order": order, **(filters or {})}
        if limit is not None:
            return self._request("GET", table, params={**params, "limit": str(limit)})
        return self._select_every_page(table, params)

    def _select_every_page(self, table: str, params: Mapping[str, str]) -> list[JsonRow]:
        """전량 조회 — **받은 행 수가 서버가 말한 총 행 수와 다르면 멈춘다**.

        ⚠️ 이 검산이 이 모듈의 존재 이유 중 하나다(SPEC §4.1 · ROADMAP 1-6). 중복 판정은
        전량을 봐야 하는데, 응답이 잘리면 **에러 없이** 일부만 보고 대표를 잘못 뽑아 중복이
        공개된다. 페이지네이션 자체에 버그가 있어도 여기서 걸린다.

        읽는 중에 다른 프로세스가 행을 넣어도 개수가 어긋나 멈춘다 — **옳은 동작**이다.
        움직이는 표본으로 중복을 판정하면 결과가 실행마다 달라진다.
        """
        rows: list[JsonRow] = []
        total: int | None = None
        while True:
            page, reported = self._request_with_total(
                "GET",
                table,
                params={**params, "limit": str(PAGE_SIZE), "offset": str(len(rows))},
                want_total=True,
            )
            if total is None:
                total = reported
                if total is None:
                    # ⚠️ 총량을 모르면 언제 끝인지도 모른다 — "일단 더 받아보자"로 다루면
                    #    응답이 계속 오는 동안 빠져나오지 못한다.
                    raise StoreError(f"{table}: 서버가 총 행 수를 주지 않아 전량을 확인할 수 없다")
            # ⚠️ 짧은 페이지로 끝내지 않는다. 서버가 우리 `limit`보다 작게 자를 수 있고
            #    (PostgREST `db-max-rows`), 그때 끝내면 정상적인 상한을 "잘렸다"로 오판해
            #    전량 조회가 통째로 불가능해진다. **총량에 닿을 때까지** 이어 받는다.
            rows.extend(page)
            if len(rows) >= total:
                break
            if not page:
                # 남은 행이 있다는데 한 줄도 안 온다 — 진전이 없다(무한 루프 방지).
                raise StoreError(
                    f"{table}: {total}행 중 {len(rows)}행에서 응답이 비었다 — 더 받을 수 없다"
                )
        if len(rows) != total:
            raise StoreError(
                f"{table}: 전량 조회가 어긋남 — 서버는 {total}행이라 했는데 {len(rows)}행 받았다"
                " (응답이 잘렸거나 읽는 중에 행이 바뀌었다)"
            )
        return rows

    def column_names(self, table: str) -> frozenset[str]:
        """`table`의 컬럼 이름 — PostgREST가 내주는 OpenAPI 스키마에서 읽는다.

        ⚠️ `GET /{table}?limit=1`로는 알 수 없다 — **행이 없으면 키도 없다**. 공개 전 드리프트
        검사는 빈 테이블에서도 돌아야 하므로(SPEC §4.3) 스키마를 직접 묻는다.
        """
        # ⚠️ 루트는 OpenAPI 문서라 `application/openapi+json`으로 온다. 클라이언트 기본
        #    `Accept: application/json`만 보내면 PostgREST가 415로 거절할 수 있어, 이 요청만
        #    둘을 함께 받는다고 알린다.
        response = self._send(
            "GET",
            "/",
            params={},
            body=None,
            headers={"Accept": "application/openapi+json, application/json"},
        )
        try:
            payload: object = response.json()
        except ValueError as err:
            raise StoreError(f"{table}: 스키마 응답이 JSON이 아니다 ({err})") from err
        definitions = payload.get("definitions") if isinstance(payload, dict) else None
        entry = definitions.get(table) if isinstance(definitions, dict) else None
        properties = entry.get("properties") if isinstance(entry, dict) else None
        if not isinstance(properties, dict) or not properties:
            raise StoreError(
                f"{table}: 컬럼 목록을 얻지 못했다 — 모양을 모른 채 공개를 시작하지 않는다"
            )
        return frozenset(str(name) for name in properties)

    # ── 쓰기 ────────────────────────────────────────────────────

    def insert(
        self,
        table: str,
        rows: Sequence[Mapping[str, JsonValue]],
        *,
        on_conflict: str | None = None,
        ignore_duplicates: bool = False,
        returning: bool = False,
    ) -> list[JsonRow]:
        """행을 넣는다. `ignore_duplicates`면 충돌한 행을 건너뛴다(= ON CONFLICT DO NOTHING).

        `returning=True`면 실제로 들어간 행이 돌아온다 — 충돌로 건너뛴 것과 새로 넣은 것을
        **행 수로** 구분하는 유일한 방법이다.
        """
        prefer = ["resolution=ignore-duplicates"] if ignore_duplicates else []
        params = {} if on_conflict is None else {"on_conflict": on_conflict}
        return self._request(
            "POST", table, params=params, body=list(rows), prefer=prefer, returning=returning
        )

    def upsert(
        self, table: str, rows: Sequence[Mapping[str, JsonValue]], *, on_conflict: str
    ) -> None:
        """있으면 **행 전체를 교체**하고 없으면 넣는다.

        ⚠️ **부분 컬럼으로 부르지 말 것.** PostgREST upsert는 본문에 없는 컬럼을 **기본값으로
        덮는다** — 판정 두 칸만 담아 보내면 나머지 51칸이 날아간다. 호출자는 항상 `to_row`로
        만든 온전한 행을 넘긴다. 값 몇 개만 고칠 때는 `patch`를 쓴다.
        """
        self._request(
            "POST",
            table,
            params={"on_conflict": on_conflict},
            body=list(rows),
            prefer=["resolution=merge-duplicates"],
        )

    def patch(
        self,
        table: str,
        *,
        filters: Mapping[str, str],
        values: Mapping[str, JsonValue],
        returning: bool = True,
    ) -> list[JsonRow]:
        """`filters`에 맞는 행의 준 칸만 고친다. 돌아온 행 수가 **실제로 바뀐 수**다.

        ⚠️ **필터를 조건으로 쓴다.** "읽어서 확인하고 쓴다"로 하면 그 사이에 min_job admin이
        승인한 것을 덮어쓴다 — 조건을 필터에 넣으면 **DB가 판정하고**, 0행이 돌아오면
        "누군가 손댔다"를 알 수 있다.

        ⚠️ `filters`가 비면 **테이블 전체**를 고친다. 빈 필터는 사고이므로 거부한다.
        """
        if not filters:
            raise StoreError(f"{table}: 필터 없는 UPDATE는 테이블 전체를 고친다 — 거부한다")
        return self._request(
            "PATCH", table, params=dict(filters), body=dict(values), returning=returning
        )

    def delete(self, table: str, *, filters: Mapping[str, str]) -> None:
        """`filters`에 맞는 행을 지운다. 빈 필터는 `patch`와 같은 이유로 거부한다."""
        if not filters:
            raise StoreError(f"{table}: 필터 없는 DELETE는 테이블을 비운다 — 거부한다")
        self._request("DELETE", table, params=dict(filters))

    # ── 요청 ────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: Mapping[str, str],
        body: object = None,
        prefer: Sequence[str] = (),
        returning: bool = False,
    ) -> list[JsonRow]:
        rows, _ = self._request_with_total(
            method, table, params=params, body=body, prefer=prefer, returning=returning
        )
        return rows

    def _request_with_total(
        self,
        method: str,
        table: str,
        *,
        params: Mapping[str, str],
        body: object = None,
        prefer: Sequence[str] = (),
        returning: bool = False,
        want_total: bool = False,
    ) -> tuple[list[JsonRow], int | None]:
        wanted = [*prefer]
        if want_total:
            # ⚠️ **필요할 때만 센다.** `count=exact`는 서버가 필터 조건으로 전체를 세게 만든다 —
            #    `limit`을 준 조회에까지 붙이면 20건을 받으려고 수천 행을 세고, 8초
            #    `statement_timeout`에 걸릴 수도 있다. 전량 검산과 `count()`만 이걸 쓴다.
            wanted.append("count=exact")
        wanted.append("return=representation" if returning else "return=minimal")
        headers = {"Prefer": ",".join(wanted)}
        response = self._send(method, f"/{table}", params=params, body=body, headers=headers)
        return _rows_of(response, table), _total_of(response)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str],
        body: object,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        """재시도·백오프·`Retry-After`. 실패는 전부 `StoreError`로 바꿔 던진다."""
        last_error = "(원인 미기록)"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.request(
                    method,
                    path,
                    params=dict(params),
                    content=(
                        None
                        if body is None
                        # allow_nan=False — NaN·Infinity는 유효한 JSON이 아니고 jsonb가
                        # 거부한다(`JsonStore._write_rows`와 같은 규칙).
                        else json.dumps(body, ensure_ascii=False, allow_nan=False)
                    ),
                    headers=dict(headers),
                )
            except httpx.HTTPError as err:
                # 메시지에 URL만 남는다 — 헤더(=키)는 절대 넣지 않는다.
                last_error = f"{type(err).__name__}: {err}"
            else:
                if response.is_success:
                    return response
                last_error = _error_message(response)
                if response.status_code not in _RETRYABLE_STATUS:
                    raise StoreError(f"{method} {path}: {last_error}")
                self._wait(attempt, _retry_after_seconds(response))
                continue
            self._wait(attempt, None)
        raise StoreError(f"{method} {path}: {MAX_ATTEMPTS}회 시도 실패 ({last_error})")

    def _wait(self, attempt: int, retry_after: float | None) -> None:
        if attempt >= MAX_ATTEMPTS:
            return
        # 서버가 알려준 대기 시간이 우리 추측보다 정확하다(fetch 층과 같은 규칙).
        backoff = min(_RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1), _RETRY_MAX_DELAY_SECONDS)
        if retry_after is not None:
            backoff = max(backoff, min(retry_after, _MAX_RETRY_AFTER_SECONDS))
        delay = backoff * random.uniform(_RETRY_JITTER_FLOOR, 1.0)
        _LOG.debug("PostgREST 재시도 %d/%d — %.1fs 대기", attempt, MAX_ATTEMPTS, delay)
        self._sleep(delay)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """429·503의 `Retry-After`(초). 서버가 알려준 대기 시간이 우리 추측보다 정확하다.

    `fetch/client.py`와 같은 규칙이다 — 날짜 형식(HTTP-date)은 쓰는 곳이 드물어 초 단위만
    읽고, 비정상적으로 긴 값은 실행을 붙잡지 않도록 상한을 둔다.
    """
    header = response.headers.get("retry-after")
    if header is None:
        return None
    try:
        seconds = float(header.strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def eq(value: str) -> str:
    """`col=eq.value` 필터.

    ⚠️ **인용하지 않는다.** `eq.`는 뒤 전체를 값으로 읽으므로 인용이 필요 없고, 인용하면
    **따옴표가 값의 일부가 되어** uuid·숫자 컬럼에서 `22P02`로 거절된다(2026-08-21 실측:
    `eq."7d7a…"` → 400 · `eq.7d7a…` → 200). 예약문자는 httpx가 URL 인코딩한다.
    쉼표로 항목을 나누는 `in.(...)`만 인용이 필요하다(`in_values`).
    """
    return f"eq.{value}"


def lt(value: int) -> str:
    """`col=lt.3` — 시도 상한처럼 **코드가 정본인 수치**를 서버에서 거를 때."""
    return f"lt.{value}"


def lte(value: int) -> str:
    """`col=lte.3` — 조건부 갱신에서 "그동안 더 커지지 않았나"를 DB가 판정하게 한다."""
    return f"lte.{value}"


def is_null(*, negated: bool = False) -> str:
    """`col=is.null` / `col=not.is.null`."""
    return "not.is.null" if negated else "is.null"


def in_values(values: Sequence[str]) -> str:
    """`col=in.("a","b")`. ⚠️ 값에 쉼표·괄호가 있으면 인용 없이는 필터가 갈라진다."""
    return f"in.({','.join(_quoted(value) for value in values)})"


def chunked(values: Sequence[str], size: int = MAX_IN_VALUES) -> Iterator[Sequence[str]]:
    """`in.(...)`에 넣을 값을 나눈다 — 한 번에 다 넣으면 URL이 414로 거절된다."""
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _quoted(value: str) -> str:
    """PostgREST 필터 값 인용. `"`는 백슬래시로 이스케이프한다."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _rows_of(response: httpx.Response, table: str) -> list[JsonRow]:
    if response.status_code == httpx.codes.NO_CONTENT or not response.content:
        return []
    try:
        payload: object = response.json()
    except ValueError as err:
        raise StoreError(f"{table}: 응답이 JSON이 아니다 ({err})") from err
    if not isinstance(payload, list):
        raise StoreError(f"{table}: 응답이 배열이 아니다 ({type(payload).__name__})")
    rows: list[JsonRow] = []
    for position, row in enumerate(payload):
        if not isinstance(row, dict):
            raise StoreError(f"{table}: 응답[{position}]이 객체가 아니다")
        rows.append(row)
    return rows


def _total_of(response: httpx.Response) -> int | None:
    matched = _CONTENT_RANGE.match(response.headers.get("content-range", ""))
    if matched is None:
        return None
    total = matched.group("total")
    return None if total == "*" else int(total)


def _error_message(response: httpx.Response) -> str:
    """PostgREST 오류 본문에서 사람이 읽을 부분만. ⚠️ 헤더는 담지 않는다(키가 있다)."""
    try:
        payload: object = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if not isinstance(payload, dict):
        return f"HTTP {response.status_code}"
    parts = [
        str(payload[key]) for key in ("message", "details", "hint", "code") if payload.get(key)
    ]
    status = f"HTTP {response.status_code}"
    return f"{status} — {' · '.join(parts)}" if parts else status
