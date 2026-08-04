"""전송 단일 창구 — 모든 HTTP는 여기를 지난다(CLAUDE.md Fetch · SPEC §3).

어댑터는 파싱만 한다. UA·인코딩·타임아웃·재시도·요청 간격·TLS·세션 쿠키를 어댑터가 각자
다루면 31곳에 31가지 예외가 생기고, 그중 하나만 빠뜨려도 그 게시판이 조용히 0건이 된다.
`httpx`를 직접 import하는 것은 이 패키지 밖에서 lint로 금지된다(pyproject TID251).

**소스 내 순차는 인스턴스가 보장한다** — `SourceClient` 하나가 한 소스를 담당하고 요청 간
최소 간격을 스스로 지킨다. 소스 간 병렬은 클라이언트를 여러 개 만들어 한다(31곳의 호스트가
전부 다르므로 = 호스트당 요청 1개 · SPEC §3).

**UA는 전 소스 동일한 브라우저 UA**다(운영자 결정) — 자체 UA로는 403을 주는 게시판이 있고
어디가 그런지 사전에 알 수 없다. 따라서 `spoof_ua`는 **코드 분기가 아니라 실측 기록**이다.

**플래그 중 이 층이 구현하는 것**: `insecure_tls`(검증 생략) · `needs_session`(쿠키 선확보) ·
robots **`Crawl-delay`만 준수**(`Disallow`는 운영자 판단으로 무시).
`www_required`·`http_only`는 **이미 `list_url`에 반영돼 있고 레지스트리가 로드 시
강제**하므로 여기서 할 일이 없다(상대 URL은 `urljoin`이 호스트·스킴을 물려준다).
`soft_200`·`image_only`는 전송이 아니라 **본문 판정**이라 호출자 몫이다.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Self
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx

from minjob_ingest.fetch import robots
from minjob_ingest.sources.registry import SourceConfig

_LOG = logging.getLogger(__name__)

#: 요청 상한. 응답 없는 게시판 하나가 실행 전체를 붙잡지 못하게 한다.
REQUEST_TIMEOUT_SECONDS: Final = 20.0

#: 같은 소스에 연속 요청할 때의 최소 간격(예의 · 가드레일 #7).
MIN_REQUEST_INTERVAL_SECONDS: Final = 1.5

#: 총 시도 횟수(최초 1 + 재시도 2).
MAX_ATTEMPTS: Final = 3
_RETRY_BASE_DELAY_SECONDS: Final = 1.0
_RETRY_MAX_DELAY_SECONDS: Final = 10.0
#: 서버가 알려준 `Retry-After`도 이 이상은 기다리지 않는다(한 소스가 실행을 붙잡지 않게).
MAX_RETRY_AFTER_SECONDS: Final = 60.0
#: 지터 하한 비율 — 여러 소스가 동시에 재시도해 같은 순간에 몰리지 않게 한다.
_RETRY_JITTER_FLOOR: Final = 0.5

#: 일시적 오류만 재시도한다. 403·404는 재시도해도 같은 답이 온다.
_RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

#: **전 소스 동일한 브라우저 UA**(운영자 결정 2026-08-04).
#:
#: 자체 UA(`minjob-ingest/...`)로는 게시판이 막는다 — YTUS 실측: 자체 UA 403(25바이트) vs
#: 브라우저 UA 200(99KB). 헤더를 다 갖춰도 **UA 문자열만 보고** 거부한다. 31곳 중 어디가
#: 그런지 사전에 알 수 없고 시간이 지나며 바뀌므로, 보드별 예외가 아니라 기본값으로 둔다.
#: ⚠️ **ASCII만** — HTTP 헤더는 비-ASCII를 담을 수 없어 클라이언트 생성부터 죽는다.
USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

#: UA만 브라우저이고 나머지 헤더가 없으면 그 자체가 눈에 띈다 — 실제 브라우저가 보내는 것을
#: 함께 보낸다. `Accept-Encoding`은 httpx가 알아서 넣는다.
_BROWSER_HEADERS: Final = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
}

#: JSON 티어(`CSU`·`HANIL`)는 페이지의 jQuery AJAX 엔드포인트다 — 실제 페이지가 보내는
#: 헤더를 맞춘다(서버가 `X-Requested-With`를 확인하는 경우가 있다).
_AJAX_HEADERS: Final = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

#: 이보다 짧은 본문은 실패로 본다 — 상태코드 200이면서 빈/스텁 응답을 주는 보드가 있다.
MIN_BODY_LENGTH: Final = 200

#: robots.txt의 두 지시는 **성격이 다르므로 따로 판단한다**(운영자 결정 2026-07-30).
#:
#: `Disallow` = "여기 오지 마"(허락의 문제) → **따르지 않는다.** 공개 게시판이고 문제없음을
#: 확인했다. 다시 켜려면 True로 — 게시판 한 곳이 요청하면 되돌릴 수 있어야 한다.
RESPECT_ROBOTS_DISALLOW: Final = False

#: `Crawl-delay` = "요청 사이에 N초 쉬어"(서버 용량의 문제) → **따른다.** 우리 기본 1.5초보다
#: 긴 값을 선언한 사이트를 그 속도로 두드리면 **IP 차단**을 부르고, 차단은 403·타임아웃으로
#: 나타나 "게시판이 고장났나"로 오진된다. 선언한 곳만 느려지므로 비용이 거의 없다.
RESPECT_CRAWL_DELAY: Final = True

type Sleeper = Callable[[float], None]
type Monotonic = Callable[[], float]


class FetchError(Exception):
    """전송 실패 — 상태코드·연결·타임아웃·빈 본문. 이 층의 **유일한** 실패 신호다.

    호출자(수집 파이프라인)는 이것만 잡아 소스 단위로 격리한다(SPEC §3 에러 격리).
    """


class RobotsDisallowed(FetchError):
    """robots.txt가 막은 경로. 조용히 건너뛰지 않고 던진다 — 호출자가 기록해야 한다.

    `FetchError`의 하위라 소스 격리 로직이 그대로 잡지만, 타입으로 구분해 "우리가 막힌 것"과
    "게시판이 고장난 것"을 `source_health`에서 갈라 볼 수 있다.
    """


@dataclass(frozen=True, slots=True)
class Response:
    """디코드까지 끝난 응답. 어댑터는 여기서부터 파싱만 한다."""

    url: str
    status: int
    text: str

    @property
    def is_json_content(self) -> bool:
        return self.text.lstrip()[:1] in {"{", "["}


def _retry_after_seconds(raw: httpx.Response) -> float | None:
    """429·503의 `Retry-After`(초). 서버가 알려준 대기 시간은 우리 추측보다 정확하다.

    이걸 무시하고 우리 백오프로 밀어붙이는 것이 IP 차단의 흔한 경로다. 날짜 형식(HTTP-date)은
    쓰는 곳이 드물어 초 단위만 읽고, 비정상적으로 긴 값은 실행을 붙잡지 않도록 상한을 둔다.
    """
    header = raw.headers.get("retry-after")
    if header is None:
        return None
    try:
        seconds = float(header.strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


class SourceClient:
    """한 소스 전용 HTTP 클라이언트.

    ⚠️ **스레드 안전하지 않다.** 인스턴스 하나 = 소스 하나 = 순차 실행이 전제이며, 그게
    "한 호스트에 요청 1개" 원칙을 지키는 방식이다. 소스 간 병렬은 인스턴스를 나눠서 한다.

    `sleep`·`monotonic`은 테스트가 실제로 기다리지 않게 주입한다(간격 로직도 검증 대상).
    """

    def __init__(
        self,
        source: SourceConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleeper = time.sleep,
        monotonic: Monotonic = time.monotonic,
        min_interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self._source = source
        self._sleep = sleep
        self._monotonic = monotonic
        self._min_interval = min_interval_seconds
        self._last_request_at: float | None = None
        self._session_ready = False
        self._robots_loaded = False
        self._robots: RobotFileParser | None = None
        self._robots_text = ""
        self._client = httpx.Client(
            headers=dict(_BROWSER_HEADERS),
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            verify=not source.flags.insecure_tls,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── 요청 ────────────────────────────────────────────────────

    def get(self, url: str) -> Response:
        """상대 URL은 `list_url` 기준으로 합쳐진다(호스트·스킴이 따라온다)."""
        return self._request("GET", url, form=None)

    def post_form(self, url: str, form: Mapping[str, str]) -> Response:
        """JSON 티어 소스용(`CSU`·`HANIL`은 목록이 POST다)."""
        return self._request("POST", url, form=form)

    # ── 내부 ────────────────────────────────────────────────────

    def _request(self, method: str, url: str, *, form: Mapping[str, str] | None) -> Response:
        absolute = urljoin(self._source.list_url, url)
        self._apply_robots_policy(absolute)
        self._ensure_session()
        return self._send(method, url, form=form)

    def _apply_robots_policy(self, absolute_url: str) -> None:
        """`Disallow`와 `Crawl-delay`를 **따로** 적용한다(위 두 상수 참조)."""
        if not (RESPECT_ROBOTS_DISALLOW or RESPECT_CRAWL_DELAY):
            return
        self._load_robots(absolute_url)
        agent = self._user_agent()
        if RESPECT_ROBOTS_DISALLOW and not robots.allows(self._robots, agent, absolute_url):
            raise RobotsDisallowed(f"{self._source.key} {absolute_url}: robots.txt가 막은 경로")
        if RESPECT_CRAWL_DELAY:
            self._honor_crawl_delay(agent)

    def _honor_crawl_delay(self, agent: str) -> None:
        declared = robots.crawl_delay_seconds(self._robots, agent, self._robots_text)
        if declared is None or declared <= self._min_interval:
            return  # 우리 기본값이 더 느리면 그대로 둔다(더 빠르게 만들지 않는다).
        _LOG.info("%s robots Crawl-delay %.1fs 적용", self._source.key, declared)
        self._min_interval = declared

    def _load_robots(self, absolute_url: str) -> None:
        """호스트당 한 번만 가져온다. 실패는 '제한 없음'으로 본다(robots 부재의 표준 해석)."""
        if self._robots_loaded:
            return
        self._robots_loaded = True  # 재진입·재시도 방지 — 아래 요청이 이 함수를 다시 지난다.
        try:
            # robots.txt는 짧다 → 본문 길이 하한 미적용(적용하면 정상 robots를 실패로 본다).
            fetched = self._send(
                "GET", robots.robots_url_for(absolute_url), form=None, min_body_length=0
            )
        except FetchError as err:
            _LOG.info("%s robots.txt 없음/실패 — 제한 없음으로 진행 (%s)", self._source.key, err)
            return
        self._robots = robots.parse_robots(fetched.text)
        self._robots_text = fetched.text

    def _send(
        self,
        method: str,
        url: str,
        *,
        form: Mapping[str, str] | None,
        min_body_length: int = MIN_BODY_LENGTH,
    ) -> Response:
        absolute = urljoin(self._source.list_url, url)
        last_error: str = ""
        retry_after: float | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry_after = None
            self._wait_for_turn()
            try:
                raw = self._client.request(
                    method,
                    absolute,
                    data=dict(form) if form is not None else None,
                    headers=_AJAX_HEADERS if form is not None else None,
                )
            except httpx.HTTPError as err:
                # 연결·타임아웃 — 메시지가 빈 예외도 있어(TimeoutError) 타입명을 함께 남긴다.
                last_error = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
            else:
                if raw.status_code not in _RETRYABLE_STATUS:
                    return self._decoded(absolute, raw, min_body_length)
                last_error = f"HTTP {raw.status_code}"
                retry_after = _retry_after_seconds(raw)

            if attempt < MAX_ATTEMPTS:
                delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
                _LOG.info(
                    "%s 일시 오류 — %.1fs 후 재시도 %d/%d (%s)",
                    self._source.key,
                    delay,
                    attempt,
                    MAX_ATTEMPTS - 1,
                    last_error,
                )
                self._sleep(delay)
        raise FetchError(
            f"{self._source.key} {absolute}: {MAX_ATTEMPTS}회 시도 실패 ({last_error})"
        )

    def _decoded(self, url: str, raw: httpx.Response, min_body_length: int) -> Response:
        if raw.status_code >= 400:
            raise FetchError(f"{self._source.key} {url}: HTTP {raw.status_code}")
        text = raw.content.decode(self._source.encoding.python_codec, errors="replace")
        # 200인데 빈/스텁 본문을 주는 보드가 있다 → 상태코드만으로 성공을 판정하지 않는다.
        # (내용 수준의 검증은 어댑터가 한다 — `soft_200` 소스는 그게 필수다.)
        if len(text.strip()) < min_body_length:
            raise FetchError(
                f"{self._source.key} {url}: 본문이 너무 짧음"
                f" ({len(text.strip())}자 < {min_body_length}) — 스텁 응답 의심"
            )
        return Response(url=url, status=raw.status_code, text=text)

    def _ensure_session(self) -> None:
        """`needs_session` 소스는 목록 페이지를 먼저 GET해 쿠키를 얻는다.

        CALVIN은 쿠키 없이 상세를 요청하면 404, CSU는 API가 code 22000을 준다 —
        둘 다 "성공처럼 보이는 실패"라서 세션을 빠뜨리면 원인을 찾기 어렵다.
        """
        if self._session_ready or not self._source.flags.needs_session:
            return
        self._session_ready = True  # 재진입 방지 — 아래 GET이 다시 이 함수를 지난다.
        _LOG.info("%s 세션 쿠키 확보 중", self._source.key)
        self._send("GET", self._source.list_url, form=None)

    def _wait_for_turn(self) -> None:
        if self._last_request_at is not None:
            elapsed = self._monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request_at = self._monotonic()

    def _backoff_delay(self, attempt: int) -> float:
        # `2 ** n`은 음수 지수에서 float이 될 수 있어 mypy가 Any로 본다 → 2.0으로 고정.
        growth = _RETRY_BASE_DELAY_SECONDS * 2.0 ** (attempt - 1)
        capped = min(_RETRY_MAX_DELAY_SECONDS, growth)
        jitter: float = random.uniform(_RETRY_JITTER_FLOOR, 1.0)
        return capped * jitter

    def _user_agent(self) -> str:
        """robots 판정에 쓰는 UA. 전 소스 동일하다(`spoof_ua` 분기 없음)."""
        return USER_AGENT
