"""PostgREST 전송 층 테스트 — **조용한 데이터 손실**을 막는 장치가 실제로 도는지.

**네트워크를 타지 않는다** — `httpx.MockTransport`로 가짜 PostgREST를 세우고 `sleep`을 주입해
실제로 기다리지 않는다(백오프도 검증 대상이다).

여기서 제일 중요한 것은 **전량 조회 검산**이다. 중복 판정은 전량을 봐야 하는데(SPEC §4.1)
응답이 잘리면 에러 없이 일부만 보고 대표를 잘못 뽑아 **중복이 공개된다** — 그 실패는 사후에
알아낼 방법이 없으므로 여기서 요란하게 멈춰야 한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Final

import httpx
import pytest

from minjob_ingest.settings import SupabaseSettings
from minjob_ingest.store.base import StoreError
from minjob_ingest.store.postgrest import (
    PAGE_SIZE,
    PostgrestClient,
    chunked,
    eq,
    in_values,
    is_null,
)

_KEY: Final = "secret-service-role-key"
_SETTINGS: Final = SupabaseSettings(url="https://x.supabase.co", service_role_key=_KEY)

type Handler = Callable[[httpx.Request], httpx.Response]


class Fake:
    """가짜 PostgREST. 주고받은 요청을 다 들고 있어 헤더·파라미터까지 검증한다."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self.slept: list[float] = []
        self._handler = handler

    def client(self) -> PostgrestClient:
        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            response = self._handler(request)
            # ⚠️ **`Prefer: count=exact`를 안 보내면 실제 PostgREST는 총 행 수를 주지 않는다**
            #    (`Content-Range: 0-2/*`). 가짜가 늘 숫자를 주면 "우리가 count를 요청하는지"를
            #    아무 테스트도 확인하지 못한다 — 진짜 서버에서만 전량 조회가 깨진다.
            if "count=exact" not in request.headers.get("prefer", ""):
                ranged = response.headers.get("content-range")
                if ranged is not None:
                    response.headers["content-range"] = f"{ranged.rsplit('/', 1)[0]}/*"
            return response

        return PostgrestClient(
            _SETTINGS, transport=httpx.MockTransport(wrapped), sleep=self.slept.append
        )

    @property
    def params(self) -> list[dict[str, str]]:
        return [dict(request.url.params) for request in self.requests]

    @property
    def offsets(self) -> list[str | None]:
        return [request.url.params.get("offset") for request in self.requests]


def _page(rows: list[dict[str, str]], *, total: int | str) -> httpx.Response:
    """`Prefer: count=exact`에 대한 응답 — `Content-Range`에 총 행 수가 온다."""
    start = 0
    end = max(len(rows) - 1, 0)
    return httpx.Response(
        200,
        json=rows,
        headers={"content-range": f"{start}-{end}/{total}"},
    )


def _rows(count: int, *, start: int = 0) -> list[dict[str, str]]:
    return [{"id": str(index)} for index in range(start, start + count)]


# ── 전량 조회 ──────────────────────────────────────────────────


def test_a_single_page_is_returned_whole() -> None:
    fake = Fake(lambda _: _page(_rows(3), total=3))
    assert len(fake.client().select("review_data", columns="*", order="id")) == 3


def test_every_page_is_fetched_and_joined() -> None:
    """⚠️ 한 페이지만 읽고 끝내면 3,188건 중 1,000건으로 중복을 판정한다."""
    pages = [
        _page(_rows(PAGE_SIZE), total=PAGE_SIZE + 7),
        _page(_rows(7, start=PAGE_SIZE), total=PAGE_SIZE + 7),
    ]
    fake = Fake(lambda _: pages.pop(0))
    rows = fake.client().select("review_data", columns="*", order="id")

    assert len(rows) == PAGE_SIZE + 7
    assert fake.offsets == ["0", str(PAGE_SIZE)]  # 두 번째 페이지를 offset으로 이어 받았다


def test_a_truncated_response_stops_the_run() -> None:
    """서버가 "3,188행"이라 했는데 그만큼 오지 않으면 **멈춘다**.

    이게 없으면 잘린 응답이 정상으로 흘러 대표가 뒤바뀐다 — 사후 탐지가 불가능한 실패다.
    """
    fake = Fake(lambda _: _page(_rows(500), total=3188))
    with pytest.raises(StoreError, match="전량 조회가 어긋남"):
        fake.client().select("review_data", columns="*", order="id")


def test_a_response_without_a_total_stops_the_run() -> None:
    """`Content-Range`가 `*`면 몇 행인지 모른다 — 모르는 채로 전량이라 하지 않는다."""
    fake = Fake(lambda _: _page(_rows(3), total="*"))
    with pytest.raises(StoreError, match="총 행 수"):
        fake.client().select("review_data", columns="*", order="id")


def test_an_empty_table_reads_as_nothing() -> None:
    """총 0행은 정상이다 — "진전이 없다"로 오판하면 빈 원장을 못 읽는다."""
    fake = Fake(lambda _: _page([], total=0))
    assert fake.client().select("review_data", columns="*", order="id") == []
    assert len(fake.requests) == 1


def test_a_server_page_cap_smaller_than_ours_is_followed() -> None:
    """⚠️ 서버가 우리 `limit`보다 작게 잘라도 **정상**이다(PostgREST `db-max-rows`).

    짧은 페이지를 곧 "잘렸다"로 보면 그런 프로젝트에서는 전량 조회가 통째로 불가능해진다 —
    총량에 닿을 때까지 이어 받아야 한다.
    """
    served = 0

    def capped(_: httpx.Request) -> httpx.Response:
        nonlocal served
        page = _rows(min(400, 1000 - served), start=served)
        served += len(page)
        return _page(page, total=1000)

    fake = Fake(capped)
    assert len(fake.client().select("review_data", columns="*", order="id")) == 1000
    assert fake.offsets == ["0", "400", "800"]


def test_no_progress_stops_instead_of_looping_forever() -> None:
    """남은 행이 있다는데 한 줄도 안 오면 멈춘다 — 안 그러면 영원히 요청한다."""
    fake = Fake(lambda _: _page([], total=10))
    with pytest.raises(StoreError, match="응답이 비었다"):
        fake.client().select("review_data", columns="*", order="id")


def test_a_limited_select_makes_one_request() -> None:
    fake = Fake(lambda _: _page(_rows(2), total=2))
    fake.client().select("source_data", columns="*", order="fetched_at", limit=2)

    assert len(fake.requests) == 1
    assert fake.params[0]["limit"] == "2"


def test_a_limited_read_does_not_make_the_server_count() -> None:
    """⚠️ `count=exact`는 서버가 필터 조건으로 **전체를 세게** 만든다.

    `limit`을 준 조회에까지 붙이면 20건을 받으려고 수천 행을 세고, 8초 `statement_timeout`에
    걸릴 수도 있다. 세는 것은 전량 검산과 `count()`만이다.
    """
    fake = Fake(lambda _: _page(_rows(2), total=2))
    fake.client().select("source_data", columns="*", order="id", limit=2)

    assert "count=exact" not in fake.requests[0].headers.get("prefer", "")


def test_the_order_is_always_sent() -> None:
    """⚠️ 정렬 없이 offset으로 넘기면 어떤 행은 두 번, 어떤 행은 한 번도 오지 않는다."""
    fake = Fake(lambda _: _page(_rows(1), total=1))
    fake.client().select("source_data", columns="*", order="fetched_at")

    assert fake.params[0]["order"] == "fetched_at"


def test_filters_ride_along() -> None:
    fake = Fake(lambda _: _page([], total=0))
    fake.client().select(
        "source_data", columns="id", order="id", filters={"source_key": eq("YTUS")}
    )

    assert fake.params[0]["source_key"] == 'eq."YTUS"'


# ── 쓰기 ───────────────────────────────────────────────────────


def test_insert_can_ignore_duplicates_and_report_what_landed() -> None:
    """새로 넣은 것과 충돌로 건너뛴 것은 **돌아온 행 수**로만 구분된다."""
    fake = Fake(lambda _: httpx.Response(201, json=[{"id": "1"}]))
    landed = fake.client().insert(
        "source_data",
        [{"id": "1"}],
        on_conflict="source_key,external_id",
        ignore_duplicates=True,
        returning=True,
    )

    assert len(landed) == 1
    prefer = fake.requests[0].headers["prefer"]
    assert "resolution=ignore-duplicates" in prefer
    assert "return=representation" in prefer
    assert fake.params[0]["on_conflict"] == "source_key,external_id"


def test_upsert_replaces_the_whole_row() -> None:
    fake = Fake(lambda _: httpx.Response(201))
    fake.client().upsert("review_data", [{"id": "1"}], on_conflict="id")

    assert "resolution=merge-duplicates" in fake.requests[0].headers["prefer"]


def test_patch_sends_the_filter_as_the_condition() -> None:
    """조건을 필터에 넣으면 DB가 판정한다 — 읽고-확인-쓰기 사이의 경쟁이 사라진다."""
    fake = Fake(lambda _: httpx.Response(200, json=[{"id": "1"}]))
    changed = fake.client().patch(
        "review_data",
        filters={"id": eq("1"), "review_status": eq("PENDING")},
        values={"dedup_key": "k"},
    )

    assert len(changed) == 1  # 0행이면 "누군가 손댔다"는 뜻이다
    assert fake.params[0]["review_status"] == 'eq."PENDING"'
    assert json.loads(fake.requests[0].content) == {"dedup_key": "k"}


def test_patch_without_a_filter_is_refused() -> None:
    """⚠️ 필터가 비면 테이블 전체를 고친다 — 사고이므로 요청을 보내지 않는다."""
    fake = Fake(lambda _: httpx.Response(200, json=[]))
    with pytest.raises(StoreError, match="테이블 전체"):
        fake.client().patch("review_data", filters={}, values={"dedup_key": "k"})
    assert fake.requests == []


def test_delete_without_a_filter_is_refused() -> None:
    fake = Fake(lambda _: httpx.Response(204))
    with pytest.raises(StoreError, match="테이블을 비운다"):
        fake.client().delete("review_data", filters={})
    assert fake.requests == []


# ── 실패·재시도 ────────────────────────────────────────────────


def test_a_transient_error_is_retried() -> None:
    answers = [httpx.Response(503), _page(_rows(1), total=1)]
    fake = Fake(lambda _: answers.pop(0))

    assert len(fake.client().select("crawl_run", columns="*", order="id")) == 1
    assert len(fake.slept) == 1


def test_retry_after_beats_our_backoff() -> None:
    answers = [
        httpx.Response(429, headers={"retry-after": "5"}),
        _page(_rows(1), total=1),
    ]
    fake = Fake(lambda _: answers.pop(0))
    fake.client().select("crawl_run", columns="*", order="id")

    # 지터가 붙어 정확히 5는 아니지만, 우리 기본 백오프(0.5s)보다 훨씬 크다.
    assert fake.slept[0] > 1.0


def test_a_permanent_error_is_not_retried() -> None:
    """4xx는 대개 우리 요청이 틀린 것이다 — 다시 보내도 같은 답이 온다."""
    fake = Fake(lambda _: httpx.Response(400, json={"message": "column x does not exist"}))
    with pytest.raises(StoreError, match="column x does not exist"):
        fake.client().select("review_data", columns="x", order="id")

    assert len(fake.requests) == 1
    assert fake.slept == []


def test_a_connection_error_becomes_a_store_error() -> None:
    """네트워크 오류도 "원장을 못 썼다"는 뜻이다 — runner가 연속 실패로 세게 한다."""

    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 실패")

    fake = Fake(boom)
    with pytest.raises(StoreError, match="시도 실패"):
        fake.client().select("crawl_run", columns="*", order="id")


def test_the_service_key_never_reaches_the_error_message() -> None:
    """⚠️ 예외는 로그·트레이스백에 남는다 — 키가 거기 섞이면 비밀이 아니게 된다."""
    fake = Fake(lambda _: httpx.Response(401, json={"message": "invalid token"}))
    with pytest.raises(StoreError) as caught:
        fake.client().select("review_data", columns="*", order="id")

    assert _KEY not in str(caught.value)


def test_the_key_is_sent_on_every_request() -> None:
    fake = Fake(lambda _: _page([], total=0))
    fake.client().select("crawl_run", columns="*", order="id")

    request = fake.requests[0]
    assert request.headers["apikey"] == _KEY
    assert request.headers["authorization"] == f"Bearer {_KEY}"


def test_the_settings_repr_masks_the_key() -> None:
    assert _KEY not in repr(_SETTINGS)


# ── 필터 문법 ──────────────────────────────────────────────────


def test_in_values_quotes_so_commas_do_not_split_the_filter() -> None:
    """⚠️ 인용하지 않으면 `a,b`가 값 두 개로 갈려 엉뚱한 행을 고른다."""
    assert in_values(["a,b", "c"]) == 'in.("a,b","c")'


def test_in_values_escapes_quotes() -> None:
    assert in_values(['he said "hi"']) == 'in.("he said \\"hi\\"")'


def test_is_null_both_ways() -> None:
    assert is_null() == "is.null"
    assert is_null(negated=True) == "not.is.null"


def test_chunked_splits_long_lists() -> None:
    """`in.(...)`에 수백 개를 한 번에 넣으면 URL이 414로 거절된다."""
    assert [list(part) for part in chunked(["a", "b", "c"], size=2)] == [["a", "b"], ["c"]]


def test_chunked_of_nothing_yields_nothing() -> None:
    assert list(chunked([])) == []


def test_a_read_always_asks_the_server_to_count() -> None:
    """⚠️ 이걸 안 보내면 `Content-Range`가 `*`로 와서 전량 조회가 통째로 실패한다."""
    fake = Fake(lambda _: _page(_rows(1), total=1))
    fake.client().select("crawl_run", columns="*", order="id")

    assert "count=exact" in fake.requests[0].headers["prefer"]
