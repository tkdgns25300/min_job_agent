"""fetch 층 테스트 — 전송 정책이 실제로 적용되는지.

**네트워크를 타지 않는다**(가드레일 #7·#10) — `httpx.MockTransport`로 응답을 만들고,
`sleep`·`monotonic`을 주입해 실제로 기다리지 않는다(간격·백오프도 검증 대상이다).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Final
from urllib.parse import urlsplit

import httpx
import pytest

from minjob_ingest.fetch import robots
from minjob_ingest.fetch.client import (
    _RETRY_MAX_DELAY_SECONDS,
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    MIN_BODY_LENGTH,
    RESPECT_CRAWL_DELAY,
    RESPECT_ROBOTS_DISALLOW,
    USER_AGENT,
    FetchError,
    RobotsDisallowed,
    SourceClient,
)
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

type Handler = Callable[[httpx.Request], httpx.Response]

#: 본문 길이 하한을 넘는 정상 응답.
_BODY: Final = "목록 페이지 행 " * 60

_ROBOTS_PATH: Final = "/robots.txt"
#: 제한 없는 robots — 클라이언트가 항상 한 번 받으므로 기본으로 응답해 준다.
_OPEN_ROBOTS: Final = "User-agent: *\nDisallow:"


def _serving_robots(handler: Handler, rules: str = _OPEN_ROBOTS) -> Handler:
    """robots.txt를 대신 응답해 `handler`가 그것을 보지 않게 한다."""

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == _ROBOTS_PATH:
            return httpx.Response(200, text=rules)
        return handler(request)

    return wrapped


@pytest.fixture(scope="module")
def sources() -> tuple[SourceConfig, ...]:
    return load_sources(None)


class _Recorder:
    """요청·대기를 기록하는 테스트 하네스."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self.slept: list[float] = []
        self._handler = handler
        self._ticks: Iterator[int] = iter(range(0, 100_000))

    def transport(self) -> httpx.MockTransport:
        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request)

        return httpx.MockTransport(wrapped)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def monotonic(self) -> float:
        return float(next(self._ticks))

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    @property
    def page_paths(self) -> list[str]:
        """robots.txt를 제외한 요청. 대부분의 테스트는 robots에 관심이 없다."""
        return [path for path in self.paths if path != _ROBOTS_PATH]


def _client(
    sources: tuple[SourceConfig, ...], key: str, handler: Handler, *, raw: bool = False
) -> tuple[SourceClient, _Recorder]:
    """`raw=True`면 robots.txt도 `handler`가 받는다(robots 자체를 검증할 때)."""
    source = find_source(sources, key)
    assert source is not None
    recorder = _Recorder(handler if raw else _serving_robots(handler))
    client = SourceClient(
        source,
        transport=recorder.transport(),
        sleep=recorder.sleep,
        monotonic=recorder.monotonic,
    )
    return client, recorder


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=_BODY)


# ── User-Agent ───────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["YTUS", "MTU", "PUTS"])
def test_every_source_sends_the_same_browser_user_agent(
    sources: tuple[SourceConfig, ...], key: str
) -> None:
    """자체 UA로는 403을 주는 게시판이 있고(YTUS 실측) 어디가 그런지 미리 알 수 없다.

    `spoof_ua` 플래그로 갈라 놓으면 표시 없는 게시판에서 조용히 막힌다 → 전 소스 동일.
    """
    client, recorder = _client(sources, key, _ok)
    client.get("/list")
    assert recorder.requests[0].headers["user-agent"] == USER_AGENT


def test_user_agent_is_ascii_only() -> None:
    """HTTP 헤더는 비-ASCII를 담을 수 없다 — 한글을 넣으면 클라이언트 생성부터 죽는다."""
    USER_AGENT.encode("ascii")  # UnicodeEncodeError면 실패


def test_browser_headers_accompany_the_user_agent(sources: tuple[SourceConfig, ...]) -> None:
    """UA만 브라우저이고 나머지가 비어 있으면 그 조합 자체가 눈에 띈다."""
    client, recorder = _client(sources, "YTUS", _ok)
    client.get("/list")
    headers = recorder.requests[0].headers
    assert "text/html" in headers["accept"]
    assert headers["accept-language"].startswith("ko-KR")


def test_json_tier_post_sends_ajax_headers(sources: tuple[SourceConfig, ...]) -> None:
    """`CSU`·`HANIL`은 페이지의 jQuery AJAX 엔드포인트 — 서버가 헤더를 확인하는 경우가 있다."""
    client, recorder = _client(sources, "HANIL", _ok)
    client.post_form("/portal/bbs/article_list.ajax", {"pageIndex": "1"})
    posted = recorder.requests[-1]
    assert posted.headers["x-requested-with"] == "XMLHttpRequest"
    assert "application/json" in posted.headers["accept"]


# ── Retry-After ──────────────────────────────────────────────────


def test_retry_after_is_honored_over_our_backoff(sources: tuple[SourceConfig, ...]) -> None:
    """서버가 알려준 대기 시간을 무시하고 우리 백오프로 밀어붙이면 IP 차단을 부른다."""
    attempts = {"n": 0}

    def throttled(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, text=_BODY)

    client, recorder = _client(sources, "YTUS", throttled)
    assert client.get("/a").status == 200
    assert max(recorder.slept) == pytest.approx(30.0)


def test_retry_after_is_capped(sources: tuple[SourceConfig, ...]) -> None:
    # 비정상적으로 긴 값이 실행 전체를 붙잡지 않게 한다.
    client, recorder = _client(
        sources, "YTUS", lambda _r: httpx.Response(503, headers={"Retry-After": "99999"})
    )
    with pytest.raises(FetchError):
        client.get("/a")
    assert max(recorder.slept) == pytest.approx(MAX_RETRY_AFTER_SECONDS)


def test_malformed_retry_after_falls_back_to_backoff(sources: tuple[SourceConfig, ...]) -> None:
    # HTTP-date 형식이나 쓰레기 값이면 우리 백오프를 쓴다(예외로 죽지 않는다).
    client, recorder = _client(
        sources, "YTUS", lambda _r: httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2026"})
    )
    with pytest.raises(FetchError):
        client.get("/a")
    assert max(recorder.slept) <= _RETRY_MAX_DELAY_SECONDS


# ── 인코딩 ───────────────────────────────────────────────────────


def test_euc_kr_source_is_decoded_with_cp949(sources: tuple[SourceConfig, ...]) -> None:
    # 순정 EUC-KR 코덱은 확장 한글에서 예외를 던져 페이지 전체를 잃는다 → cp949를 쓴다.
    body = "한글 본문 확인 " * 30

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode("cp949"))

    client, _ = _client(sources, "PUTS", handler)
    assert "한글 본문 확인" in client.get("/list.asp").text


def test_config_encoding_wins_over_server_header(sources: tuple[SourceConfig, ...]) -> None:
    """서버가 인코딩을 틀리게 보고하는 보드가 있다(KBTUS) → config 값이 우선이다."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=("한글 " * 80).encode("cp949"),
            headers={"content-type": "text/html; charset=utf-8"},  # 거짓 선언
        )

    client, _ = _client(sources, "PUTS", handler)  # config = euc-kr
    assert "한글" in client.get("/list.asp").text


# ── URL 결합 ─────────────────────────────────────────────────────


def test_relative_url_inherits_host_and_scheme(sources: tuple[SourceConfig, ...]) -> None:
    # 이것이 www_required·http_only에 코드가 필요 없는 이유다 — list_url이 둘을 이미 담는다.
    client, recorder = _client(sources, "WGST", _ok)  # http 전용 소스
    client.get("/wgst_renew/board/boardview.asp?seq=1")
    url = recorder.requests[0].url
    assert url.scheme == "http"
    assert url.host == "www.wgst.ac.kr"


# ── 세션 ─────────────────────────────────────────────────────────


def test_session_source_fetches_the_list_page_first(sources: tuple[SourceConfig, ...]) -> None:
    """CALVIN은 쿠키 없이 상세를 요청하면 404 — '성공처럼 보이는 실패'다."""
    source = find_source(sources, "CALVIN")
    assert source is not None
    client, recorder = _client(sources, "CALVIN", _ok)
    client.get("/main/boardView.do?brd_no=1")
    assert len(recorder.page_paths) == 2
    # 경로를 하드코딩하지 않는다 — 세션은 config의 list_url로 확보되어야 한다.
    assert recorder.page_paths[0] == urlsplit(source.list_url).path


def test_session_is_established_only_once(sources: tuple[SourceConfig, ...]) -> None:
    client, recorder = _client(sources, "CALVIN", _ok)
    client.get("/main/boardView.do?brd_no=1")
    client.get("/main/boardView.do?brd_no=2")
    assert len(recorder.page_paths) == 3  # 세션 1 + 상세 2


def test_source_without_the_flag_makes_no_session_request(
    sources: tuple[SourceConfig, ...],
) -> None:
    # needs_session이 아닌 소스는 세션용 추가 요청을 하지 않는다(robots.txt 1건은 별개).
    client, recorder = _client(sources, "YTUS", _ok)
    client.get("/board/list/trXXR")
    assert recorder.page_paths == ["/board/list/trXXR"]


# ── 요청 간격 ────────────────────────────────────────────────────


def test_every_request_after_the_first_waits(sources: tuple[SourceConfig, ...]) -> None:
    """한 게시판을 몰아치지 않는다(가드레일 #7). 첫 요청은 기다리지 않는다."""
    client, recorder = _client(sources, "YTUS", _ok)
    client.get("/a")
    client.get("/b")
    client.get("/c")
    assert len(recorder.slept) == len(recorder.requests) - 1
    # monotonic이 요청당 1초씩 흐르므로 1.5초 간격에는 0.5초가 남는다.
    assert recorder.slept == [pytest.approx(0.5)] * len(recorder.slept)


# ── 성공 판정 ────────────────────────────────────────────────────


def test_empty_body_with_status_200_is_a_failure(sources: tuple[SourceConfig, ...]) -> None:
    """상태코드만으로 성공을 판정하지 않는다 — 잘못된 요청에 200+빈 본문을 주는 보드가 있다."""
    client, _ = _client(sources, "KAICAM", lambda _r: httpx.Response(200, text=""))
    with pytest.raises(FetchError, match="너무 짧음"):
        client.get("/view.asp?boarddetailseq=999999")


def test_body_at_the_threshold_is_accepted(sources: tuple[SourceConfig, ...]) -> None:
    client, _ = _client(
        sources, "YTUS", lambda _r: httpx.Response(200, text="가" * MIN_BODY_LENGTH)
    )
    assert client.get("/a").status == 200


def test_client_error_fails_immediately(sources: tuple[SourceConfig, ...]) -> None:
    # 404·403은 재시도해도 같은 답이 온다 → 게시판을 두 번 더 두드리지 않는다.
    client, recorder = _client(sources, "YTUS", lambda _r: httpx.Response(404))
    with pytest.raises(FetchError, match="404"):
        client.get("/gone")
    assert len(recorder.page_paths) == 1


# ── 재시도 ───────────────────────────────────────────────────────


def test_rate_limit_is_retried_then_succeeds(sources: tuple[SourceConfig, ...]) -> None:
    attempts = {"n": 0}

    def flaky(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return (
            httpx.Response(429) if attempts["n"] < MAX_ATTEMPTS else httpx.Response(200, text=_BODY)
        )

    client, recorder = _client(sources, "YTUS", flaky)
    assert client.get("/a").status == 200
    assert len(recorder.page_paths) == MAX_ATTEMPTS


def test_retries_are_capped(sources: tuple[SourceConfig, ...]) -> None:
    client, recorder = _client(sources, "YTUS", lambda _r: httpx.Response(503))
    with pytest.raises(FetchError, match="시도 실패"):
        client.get("/a")
    assert len(recorder.page_paths) == MAX_ATTEMPTS


def test_connection_errors_are_retried(sources: tuple[SourceConfig, ...]) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 거부")

    client, recorder = _client(sources, "YTUS", boom)
    with pytest.raises(FetchError, match="ConnectError"):
        client.get("/a")
    assert len(recorder.page_paths) == MAX_ATTEMPTS


def test_timeout_without_a_message_still_reports_its_type(
    sources: tuple[SourceConfig, ...],
) -> None:
    """`str(TimeoutException())`가 빈 문자열이면 원인이 사라진다 → 타입명을 함께 남긴다."""

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    client, _ = _client(sources, "YTUS", boom)
    with pytest.raises(FetchError, match="ReadTimeout"):
        client.get("/a")


def test_backoff_grows_and_is_jittered(sources: tuple[SourceConfig, ...]) -> None:
    client, recorder = _client(sources, "YTUS", lambda _r: httpx.Response(503))
    with pytest.raises(FetchError):
        client.get("/a")
    # 간격 대기와 백오프가 섞여 있으므로 "1초 이상 기다린 적이 있다"로 백오프를 확인한다.
    assert any(delay >= 0.5 for delay in recorder.slept)


# ── robots ───────────────────────────────────────────────────────


def _with_robots(rules: str) -> Handler:
    """robots.txt를 직접 응답하는 핸들러. `_client(..., raw=True)`와 함께 쓴다 —
    raw가 아니면 `_serving_robots`가 한 겹 더 감싸 이 규칙이 가려진다."""
    return _serving_robots(_ok, rules)


def test_disallow_is_ignored_by_operator_decision(sources: tuple[SourceConfig, ...]) -> None:
    """`Disallow`는 허락의 문제 — 공개 게시판이므로 따르지 않는다(2026-07-30 운영자 결정)."""
    assert RESPECT_ROBOTS_DISALLOW is False
    client, _ = _client(sources, "YTUS", _with_robots("User-agent: *\nDisallow: /board/"), raw=True)
    assert client.get("/board/list/trXXR").status == 200


def test_crawl_delay_is_honored(sources: tuple[SourceConfig, ...]) -> None:
    """`Crawl-delay`는 서버 용량의 문제 — 선언보다 빠르게 두드리면 IP 차단을 부른다."""
    assert RESPECT_CRAWL_DELAY is True
    client, recorder = _client(
        sources, "YTUS", _with_robots("User-agent: *\nCrawl-delay: 5"), raw=True
    )
    client.get("/a")
    client.get("/b")
    # monotonic이 요청당 1초씩 흐르므로, 5초 간격이면 4초 이상 기다린 적이 있어야 한다.
    assert max(recorder.slept) >= 4.0


def test_crawl_delay_never_makes_us_faster(sources: tuple[SourceConfig, ...]) -> None:
    """사이트가 1초를 선언해도 우리 기본 1.5초보다 빠르게 가지 않는다.

    ⚠️ 정수를 쓴다 — 표준 파서가 소수점을 버려서 `0.1`로는 이 가드가 아예 실행되지 않는다.
    """
    client, recorder = _client(
        sources, "YTUS", _with_robots("User-agent: *\nCrawl-delay: 1"), raw=True
    )
    client.get("/a")
    client.get("/b")
    # `[approx(0.5)] * len(slept)`처럼 쓰면 slept가 비어도 통과한다 → 개수를 따로 고정한다.
    assert len(recorder.slept) == len(recorder.requests) - 1
    assert all(delay == pytest.approx(0.5) for delay in recorder.slept)


def test_the_largest_declared_delay_wins(sources: tuple[SourceConfig, ...]) -> None:
    """폴백은 UA 그룹을 구분하지 않으므로 **가장 느린 값**을 골라야 안전하다.

    작은 값을 고르면 사이트가 요청한 것보다 빠르게 두드릴 수 있다.
    """
    rules = "User-agent: SomeBot\nCrawl-delay: 0.5\n\nUser-agent: *\nCrawl-delay: 3.5"
    client, recorder = _client(sources, "YTUS", _with_robots(rules), raw=True)
    client.get("/a")
    client.get("/b")
    assert max(recorder.slept) >= 2.5  # 3.5초 간격 - 흐른 1초


def test_decimal_crawl_delay_is_not_dropped(sources: tuple[SourceConfig, ...]) -> None:
    """표준 파서는 `2.5`를 버린다 — 그러면 2.5초를 요청한 사이트를 1.5초로 두드린다."""
    client, recorder = _client(
        sources, "YTUS", _with_robots("User-agent: *\nCrawl-delay: 2.5"), raw=True
    )
    client.get("/a")
    client.get("/b")
    assert max(recorder.slept) >= 1.5  # 2.5초 간격 - 흐른 1초


def test_disallow_blocks_when_switched_on(
    sources: tuple[SourceConfig, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """되돌릴 수 있어야 한다 — 게시판 한 곳이 요청하면 스위치로 준수를 켠다."""
    monkeypatch.setattr("minjob_ingest.fetch.client.RESPECT_ROBOTS_DISALLOW", True)
    client, _ = _client(sources, "YTUS", _with_robots("User-agent: *\nDisallow: /board/"), raw=True)
    with pytest.raises(RobotsDisallowed, match="robots"):
        client.get("/board/list/trXXR")


def test_short_robots_txt_is_not_mistaken_for_a_stub(sources: tuple[SourceConfig, ...]) -> None:
    """robots.txt는 본문 하한보다 짧다 — 하한을 적용하면 정상 robots를 실패로 보고 무시한다."""
    rules = "User-agent: *\nCrawl-delay: 7"
    assert len(rules) < MIN_BODY_LENGTH
    client, recorder = _client(sources, "YTUS", _with_robots(rules), raw=True)
    client.get("/a")
    client.get("/b")
    assert max(recorder.slept) >= 6.0  # 규칙이 실제로 읽혔다는 뜻


def test_missing_robots_txt_means_unrestricted(sources: tuple[SourceConfig, ...]) -> None:
    # robots 부재의 표준 해석은 "제한 없음" — 반대로 처리하면 대부분 게시판을 못 긁는다.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _ROBOTS_PATH:
            return httpx.Response(404)
        return httpx.Response(200, text=_BODY)

    client, _ = _client(sources, "YTUS", handler, raw=True)
    assert client.get("/board/list/trXXR").status == 200


def test_robots_is_fetched_once_per_client(sources: tuple[SourceConfig, ...]) -> None:
    client, recorder = _client(sources, "YTUS", _with_robots("User-agent: *\nDisallow:"), raw=True)
    for _ in range(3):
        client.get("/board/list/trXXR")
    assert recorder.paths.count("/robots.txt") == 1


# ── robots 파싱 (단위) ───────────────────────────────────────────


def test_robots_url_keeps_scheme_and_host() -> None:
    assert (
        robots.robots_url_for("http://www.wgst.ac.kr/a/b?c=1") == "http://www.wgst.ac.kr/robots.txt"
    )


def test_allows_is_true_without_a_parser() -> None:
    assert robots.allows(None, "any-agent", "https://x/y") is True


def test_crawl_delay_is_read_as_a_float() -> None:
    parser = robots.parse_robots("User-agent: *\nCrawl-delay: 5")
    assert robots.crawl_delay_seconds(parser, "any-agent") == pytest.approx(5.0)


def test_crawl_delay_is_none_when_absent() -> None:
    parser = robots.parse_robots("User-agent: *\nDisallow:")
    assert robots.crawl_delay_seconds(parser, "any-agent") is None


# ── 자원 정리 ────────────────────────────────────────────────────


def test_context_manager_closes_the_connection(sources: tuple[SourceConfig, ...]) -> None:
    source = find_source(sources, "YTUS")
    assert source is not None
    recorder = _Recorder(_ok)
    with SourceClient(
        source, transport=recorder.transport(), sleep=recorder.sleep, monotonic=recorder.monotonic
    ) as client:
        client.get("/a")
    with pytest.raises(RuntimeError):  # 닫힌 클라이언트는 재사용되지 않는다
        client.get("/b")
