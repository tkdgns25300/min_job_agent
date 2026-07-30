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
    BROWSER_USER_AGENT,
    HONEST_USER_AGENT,
    MAX_ATTEMPTS,
    MIN_BODY_LENGTH,
    RESPECT_ROBOTS,
    FetchError,
    RobotsDisallowed,
    SourceClient,
)
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

type Handler = Callable[[httpx.Request], httpx.Response]

#: 본문 길이 하한을 넘는 정상 응답.
_BODY: Final = "목록 페이지 행 " * 60


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


def _client(
    sources: tuple[SourceConfig, ...], key: str, handler: Handler
) -> tuple[SourceClient, _Recorder]:
    source = find_source(sources, key)
    assert source is not None
    recorder = _Recorder(handler)
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


def test_honest_user_agent_is_sent_by_default(sources: tuple[SourceConfig, ...]) -> None:
    # 빈 UA에 403·520을 주는 보드가 있다 — UA는 항상 비어있지 않아야 한다(SPEC §3).
    client, recorder = _client(sources, "YTUS", _ok)
    client.get("/board/list/trXXR")
    assert recorder.requests[0].headers["user-agent"] == HONEST_USER_AGENT


def test_spoof_ua_source_gets_a_browser_user_agent(sources: tuple[SourceConfig, ...]) -> None:
    # MTU는 정직 UA에 0건 스텁을 준다 — 위장하지 않으면 성공처럼 보이면서 아무것도 못 가져온다.
    client, recorder = _client(sources, "MTU", _ok)
    client.get("/mtu/board/list.do")
    assert recorder.requests[0].headers["user-agent"] == BROWSER_USER_AGENT


def test_user_agents_are_ascii_only() -> None:
    """HTTP 헤더는 비-ASCII를 담을 수 없다 — 한글을 넣으면 클라이언트 생성부터 죽는다."""
    for agent in (HONEST_USER_AGENT, BROWSER_USER_AGENT):
        agent.encode("ascii")  # UnicodeEncodeError면 실패


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
    assert len(recorder.requests) == 2
    # 경로를 하드코딩하지 않는다 — 세션은 config의 list_url로 확보되어야 한다.
    assert recorder.paths[0] == urlsplit(source.list_url).path


def test_session_is_established_only_once(sources: tuple[SourceConfig, ...]) -> None:
    client, recorder = _client(sources, "CALVIN", _ok)
    client.get("/main/boardView.do?brd_no=1")
    client.get("/main/boardView.do?brd_no=2")
    assert len(recorder.requests) == 3  # 세션 1 + 상세 2


def test_source_without_the_flag_makes_no_extra_request(sources: tuple[SourceConfig, ...]) -> None:
    client, recorder = _client(sources, "YTUS", _ok)
    client.get("/board/list/trXXR")
    assert len(recorder.requests) == 1


# ── 요청 간격 ────────────────────────────────────────────────────


def test_consecutive_requests_wait(sources: tuple[SourceConfig, ...]) -> None:
    # 한 게시판을 몰아치지 않는다(가드레일 #7). monotonic이 1초씩 흐르므로 0.5초 대기가 남는다.
    client, recorder = _client(sources, "YTUS", _ok)
    client.get("/a")
    client.get("/b")
    assert recorder.slept == [pytest.approx(0.5)]


def test_first_request_does_not_wait(sources: tuple[SourceConfig, ...]) -> None:
    client, recorder = _client(sources, "YTUS", _ok)
    client.get("/a")
    assert recorder.slept == []


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
    assert len(recorder.requests) == 1


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
    assert len(recorder.requests) == MAX_ATTEMPTS


def test_retries_are_capped(sources: tuple[SourceConfig, ...]) -> None:
    client, recorder = _client(sources, "YTUS", lambda _r: httpx.Response(503))
    with pytest.raises(FetchError, match="시도 실패"):
        client.get("/a")
    assert len(recorder.requests) == MAX_ATTEMPTS


def test_connection_errors_are_retried(sources: tuple[SourceConfig, ...]) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 거부")

    client, recorder = _client(sources, "YTUS", boom)
    with pytest.raises(FetchError, match="ConnectError"):
        client.get("/a")
    assert len(recorder.requests) == MAX_ATTEMPTS


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


def test_robots_is_not_consulted_by_default(sources: tuple[SourceConfig, ...]) -> None:
    """운영자 판단으로 robots를 따르지 않는다(2026-07-30) — 요청조차 하지 않는다."""
    assert RESPECT_ROBOTS is False
    client, recorder = _client(sources, "YTUS", _ok)
    client.get("/board/list/trXXR")
    assert "/robots.txt" not in recorder.paths


def test_disallowed_path_is_fetched_while_the_switch_is_off(
    sources: tuple[SourceConfig, ...],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /board/")
        return httpx.Response(200, text=_BODY)

    client, _ = _client(sources, "YTUS", handler)
    assert client.get("/board/list/trXXR").status == 200


def test_switching_robots_on_blocks_disallowed_paths(
    sources: tuple[SourceConfig, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """되돌릴 수 있어야 한다 — 게시판 한 곳이 요청하면 스위치로 준수를 켠다."""
    monkeypatch.setattr("minjob_ingest.fetch.client.RESPECT_ROBOTS", True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /board/")
        return httpx.Response(200, text=_BODY)

    client, _ = _client(sources, "YTUS", handler)
    with pytest.raises(RobotsDisallowed, match="robots"):
        client.get("/board/list/trXXR")


def test_short_robots_txt_is_not_mistaken_for_a_stub(
    sources: tuple[SourceConfig, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """robots.txt는 본문 하한보다 짧다 — 하한을 적용하면 정상 robots를 실패로 보고 무시한다."""
    monkeypatch.setattr("minjob_ingest.fetch.client.RESPECT_ROBOTS", True)
    rules = "User-agent: *\nDisallow: /board/"
    assert len(rules) < MIN_BODY_LENGTH

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=rules)
        return httpx.Response(200, text=_BODY)

    client, _ = _client(sources, "YTUS", handler)
    with pytest.raises(RobotsDisallowed):  # 규칙이 실제로 읽혔다는 뜻
        client.get("/board/list/trXXR")


def test_missing_robots_txt_means_unrestricted(
    sources: tuple[SourceConfig, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    # robots 부재의 표준 해석은 "제한 없음" — 반대로 처리하면 대부분 게시판을 못 긁는다.
    monkeypatch.setattr("minjob_ingest.fetch.client.RESPECT_ROBOTS", True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=_BODY)

    client, _ = _client(sources, "YTUS", handler)
    assert client.get("/board/list/trXXR").status == 200


def test_robots_is_fetched_once_per_client(
    sources: tuple[SourceConfig, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("minjob_ingest.fetch.client.RESPECT_ROBOTS", True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(200, text=_BODY)

    client, recorder = _client(sources, "YTUS", handler)
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
