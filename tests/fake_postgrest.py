"""메모리 위의 **가짜 PostgREST** — `SupabaseStore`를 네트워크 없이 계약 검증한다.

우리가 실제로 쓰는 문법만 해석한다(`eq`·`in`·`is.null`·`not.is.null`·`lt`·`lte` · `order` ·
`limit`/`offset` · `on_conflict` + `resolution=ignore-duplicates`/`merge-duplicates` ·
`Prefer: count=exact`/`return=representation`). 그 밖의 문법이 오면 **조용히 무시하지 않고
예외를 던진다** — 가짜가 관대하면 테스트는 초록불인데 진짜 서버에서만 깨진다.

⚠️ **이것이 진짜 PostgREST를 대체하지는 않는다.** 문법 해석이 실제와 다를 수 있어서, 실행 ②③의
소수 스모크가 여전히 필요하다(ROADMAP 1-6). 여기서 잡는 것은 **우리 저장소 로직**이다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Final
from urllib.parse import unquote

import httpx

#: 테이블별 유일 키 — `on_conflict`이 가리키는 컬럼 묶음이 실제로 유일해야 충돌을 흉내낼 수 있다.
_UNIQUE_KEYS: Final[Mapping[str, tuple[tuple[str, ...], ...]]] = {
    "source_data": (("id",), ("source_key", "external_id")),
    "review_data": (("id",), ("source_data_id",)),
    "source_health": (("source_key",),),
    "crawl_run": (("id",),),
    # min_job 소유 테이블 — 크롤러는 앵커로 읽고 INSERT·`posted_at`만 쓴다(SPEC §8).
    "jobs": (("id",),),
}

type Row = dict[str, object]

#: 요청 파라미터 중 컬럼 이름이 아닌 것들.
_NOT_A_COLUMN: Final = frozenset({"select", "order", "limit", "offset", "on_conflict", "or"})

_QUOTED: Final = re.compile(r'"((?:[^"\\]|\\.)*)"')


class FakePostgrest:
    """표를 들고 있는 가짜 서버. 테스트가 `rows`로 직접 들여다볼 수 있다."""

    def __init__(self) -> None:
        self.rows: dict[str, list[Row]] = {name: [] for name in _UNIQUE_KEYS}
        self.requests: list[httpx.Request] = []
        #: 읽기 **직후** 끼어드는 훅 — "우리가 읽은 뒤 admin이 승인했다"를 흉내낼 자리다.
        #: 그 경쟁을 만들 수 없으면 조건부 쓰기(필터에 조건을 넣는 방어)가 검증되지 않는다.
        self.after_read: Callable[[str], None] | None = None
        #: OpenAPI 루트가 알려줄 컬럼(스키마 드리프트 검사용). `None`이면 스키마를 안 내준다 —
        #: 그 경우에도 공개가 시작되지 않아야 한다(SPEC §4.3).
        self.schema: dict[str, set[str]] | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def seed(self, table: str, *rows: Mapping[str, object]) -> None:
        self.rows[table].extend(dict(row) for row in rows)

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.requests]

    # ── 요청 처리 ───────────────────────────────────────────────

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # base_url이 `.../rest/v1`이라 경로는 `/rest/v1/<table>`로 온다 — 마지막 조각이 표다.
        table = request.url.path.rsplit("/", 1)[-1]
        if table == "":
            return self._schema()
        if table not in self.rows:
            return httpx.Response(404, json={"message": f"relation {table} does not exist"})
        params = dict(request.url.params)
        prefer = request.headers.get("prefer", "")
        match request.method:
            case "GET" | "HEAD":
                answer = self._read(table, params, prefer, body=request.method == "GET")
                if self.after_read is not None:
                    self.after_read(table)
                return answer
            case "POST":
                return self._insert(table, params, prefer, request.content)
            case "PATCH":
                return self._update(table, params, prefer, request.content)
            case "DELETE":
                return self._delete(table, params)
            case other:  # pragma: no cover - 우리가 쓰지 않는 메서드
                raise AssertionError(f"가짜 서버가 모르는 메서드: {other}")

    def _schema(self) -> httpx.Response:
        """PostgREST OpenAPI 루트. 스키마를 안 내주는 프로젝트도 흉내낼 수 있어야 한다."""
        if self.schema is None:
            return httpx.Response(200, json={"swagger": "2.0"})
        definitions: dict[str, object] = {
            name: {"properties": {column: {} for column in columns}}
            for name, columns in self.schema.items()
        }
        return httpx.Response(200, json={"swagger": "2.0", "definitions": definitions})

    def _read(
        self, table: str, params: Mapping[str, str], prefer: str, *, body: bool
    ) -> httpx.Response:
        self._check_columns(table, _referenced_columns(params))
        matched = self._matching(table, params)
        total = len(matched)
        ordered = _ordered(matched, params.get("order"))
        offset = int(params.get("offset", "0"))
        limit = params.get("limit")
        window = ordered[offset : offset + int(limit)] if limit is not None else ordered[offset:]
        selected = [_project(row, params.get("select")) for row in window]
        reported = str(total) if "count=exact" in prefer else "*"
        # ⚠️ **돌려줄 행이 없으면 범위 쪽이 `*`다** — 진짜 PostgREST가 `*/0`으로 답한다.
        #    가짜가 늘 `0-N/총계`를 보내면 그 형식이 테스트에 한 번도 안 나오고, **빈 표를
        #    읽는 것만으로 실패하는 버그**가 실 서버에서만 드러난다(2026-08-21 실측).
        ranged = f"{offset}-{offset + len(window) - 1}" if window else "*"
        return httpx.Response(
            200,
            json=selected if body else None,
            headers={"content-range": f"{ranged}/{reported}"},
        )

    def _insert(
        self, table: str, params: Mapping[str, str], prefer: str, content: bytes
    ) -> httpx.Response:
        incoming = _body_rows(content)
        self._check_columns(table, _referenced_columns(params))
        for row in incoming:
            self._check_columns(table, frozenset(row))
        conflict = _conflict_columns(table, params.get("on_conflict"))
        landed: list[Row] = []
        for row in incoming:
            existing = self._find_conflict(table, row, conflict)
            if existing is None:
                self.rows[table].append(dict(row))
                landed.append(dict(row))
            elif "resolution=merge-duplicates" in prefer:
                existing.clear()
                existing.update(row)
                landed.append(dict(row))
            elif "resolution=ignore-duplicates" in prefer:
                continue
            else:
                return httpx.Response(409, json={"code": "23505", "message": "duplicate key value"})
        return self._answer(landed, prefer, created=True)

    def _update(
        self, table: str, params: Mapping[str, str], prefer: str, content: bytes
    ) -> httpx.Response:
        values = _body_object(content)
        self._check_columns(table, _referenced_columns(params) | frozenset(values))
        changed: list[Row] = []
        for row in self._matching(table, params):
            row.update(values)
            changed.append(dict(row))
        return self._answer(changed, prefer, created=False)

    def _delete(self, table: str, params: Mapping[str, str]) -> httpx.Response:
        self._check_columns(table, _referenced_columns(params))
        doomed = self._matching(table, params)
        self.rows[table] = [row for row in self.rows[table] if not any(row is d for d in doomed)]
        return httpx.Response(204)

    def _answer(self, rows: Sequence[Row], prefer: str, *, created: bool) -> httpx.Response:
        status = 201 if created else 200
        if "return=representation" not in prefer:
            return httpx.Response(204 if created else 200, json=None)
        return httpx.Response(status, json=list(rows))

    def _check_columns(self, table: str, referenced: frozenset[str]) -> None:
        """⚠️ **없는 컬럼을 참조하면 진짜 PostgREST는 400을 준다.**

        가짜가 `row.get(column)`으로 조용히 `None`을 주면, `order=id`로 `source_health`를
        조회하는 코드가 테스트를 통과하고 **실 서버에서만 깨진다**(실제로 그 버그를
        2026-08-21에 이렇게 찾았다). 표 모양을 아는 테스트는 `schema`를 채워 이 검사를 켠다.
        """
        known = None if self.schema is None else self.schema.get(table)
        if known is None:
            return
        unknown = sorted(referenced - known)
        if unknown:
            raise AssertionError(f"{table}: 없는 컬럼을 참조했다 — {', '.join(unknown)}")

    # ── 필터 ────────────────────────────────────────────────────

    def _matching(self, table: str, params: Mapping[str, str]) -> list[Row]:
        conditions = [
            (column, value)
            for column, value in params.items()
            if column not in {"select", "order", "limit", "offset", "on_conflict"}
        ]
        return [
            row
            for row in self.rows[table]
            if all(_passes(row.get(column), value) for column, value in conditions)
        ]

    def _find_conflict(
        self, table: str, row: Mapping[str, object], columns: tuple[str, ...]
    ) -> Row | None:
        for stored in self.rows[table]:
            if all(stored.get(name) == row.get(name) for name in columns):
                return stored
        return None


def _referenced_columns(params: Mapping[str, str]) -> frozenset[str]:
    """요청이 이름으로 가리키는 컬럼 전부 — 필터 키 · `select` · `order` · `on_conflict`."""
    named = {column for column in params if column not in _NOT_A_COLUMN}
    select = params.get("select")
    if select is not None and select != "*":
        named |= {name.strip() for name in select.split(",")}
    order = params.get("order")
    if order is not None:
        named.add(order.split(".")[0])
    conflict = params.get("on_conflict")
    if conflict is not None:
        named |= {name.strip() for name in conflict.split(",")}
    return frozenset(named)


def _conflict_columns(table: str, on_conflict: str | None) -> tuple[str, ...]:
    if on_conflict is None:
        return ("id",) if ("id",) in _UNIQUE_KEYS[table] else _UNIQUE_KEYS[table][0]
    columns = tuple(name.strip() for name in on_conflict.split(","))
    if columns not in _UNIQUE_KEYS[table]:
        raise AssertionError(f"{table}: {columns}는 유일 키가 아니다 — on_conflict가 안 듣는다")
    return columns


def _passes(stored: object, expression: str) -> bool:
    """PostgREST 필터 하나. ⚠️ 모르는 문법은 조용히 통과시키지 않는다."""
    operator, _, argument = expression.partition(".")
    match operator:
        case "eq":
            # ⚠️ **인용을 풀지 않는다.** 진짜 PostgREST는 `eq.`의 따옴표를 값의 일부로 읽는다
            #    — 가짜가 풀어주면 `eq."<uuid>"`가 테스트를 통과하고 실 서버에서만 400이
            #    난다(2026-08-21 실측으로 그 버그를 찾았다).
            return stored == argument
        case "in":
            return stored in _in_list(argument)
        case "lt":
            return isinstance(stored, int) and stored < int(argument)
        case "lte":
            return isinstance(stored, int) and stored <= int(argument)
        case "is":
            return stored is None if argument == "null" else _unknown(expression)
        case "not":
            return not _passes(stored, argument)
        case _:
            return _unknown(expression)


def _unknown(expression: str) -> bool:
    raise AssertionError(f"가짜 서버가 모르는 필터: {expression!r}")


def _in_list(argument: str) -> list[str]:
    return [_unescape(found) for found in _QUOTED.findall(unquote(argument))]


def _unquote(argument: str) -> str:
    found = _QUOTED.fullmatch(argument)
    return _unescape(found.group(1)) if found is not None else argument


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _ordered(rows: Sequence[Row], order: str | None) -> list[Row]:
    if order is None:
        raise AssertionError("가짜 서버: order 없는 조회는 페이지 순서를 보장할 수 없다")
    column = order.split(".")[0]
    descending = order.endswith(".desc")
    return sorted(rows, key=lambda row: _sortable(row.get(column)), reverse=descending)


def _sortable(value: object) -> tuple[int, str]:
    # None을 뒤로 보낸다 — Postgres 기본(`NULLS LAST`)과 같게.
    return (1, "") if value is None else (0, str(value))


def _project(row: Row, select: str | None) -> Row:
    if select is None or select == "*":
        return dict(row)
    wanted = [name.strip() for name in select.split(",")]
    return {name: row.get(name) for name in wanted}


def _body_rows(content: bytes) -> list[Row]:
    parsed = json.loads(content)
    if not isinstance(parsed, list):
        raise AssertionError("가짜 서버: INSERT 본문은 배열이어야 한다")
    return [dict(row) for row in parsed]


def _body_object(content: bytes) -> Row:
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise AssertionError("가짜 서버: PATCH 본문은 객체여야 한다")
    return dict(parsed)
