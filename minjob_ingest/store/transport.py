"""Supabase로 보내는 HTTP 요청 하나 — **재시도 정책이 사는 한 곳**.

`PostgrestClient`(원장)와 `SupabaseStorage`(포스터)가 **같은 호스트에 같은 키로** 말한다.
정책을 각자 들고 있으면 한쪽을 고칠 때 다른 쪽이 조용히 뒤처지고, 그 어긋남은 "왜 이쪽만
429에서 죽나"로만 드러난다(`store/guards.py`를 뺀 것과 같은 이유).

⚠️ **`fetch/client.py`와 섞지 않는다.** 그쪽은 게시판용이라 브라우저 UA 위장 · robots
`Crawl-delay` · 소스별 최소 간격이 붙어 있다. 그 정책이 우리 DB 요청에 새면 행 하나 쓸 때마다
1.5초를 기다리고, 우리 DB에 대고 남의 브라우저를 흉내내게 된다. 같은 `httpx`를 쓸 뿐이다.

⚠️ **예외 메시지에 요청 헤더를 넣지 않는다** — 거기 `service_role` 키가 있다.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping
from typing import Final

import httpx

from minjob_ingest.store.base import StoreError

_LOG = logging.getLogger(__name__)

#: 요청 상한. 서버 쪽 `statement_timeout`이 8초라 그보다 넉넉히 잡는다 — 짧게 잡으면 DB가
#: 보내주는 "왜 느렸나"를 못 보고 클라이언트가 먼저 끊어 원인을 잃는다.
REQUEST_TIMEOUT_SECONDS: Final = 30.0

#: 총 시도 횟수(최초 1 + 재시도 2) — `fetch/client.py`와 같은 셈법.
#: ⚠️ **비공개다.** 테스트는 이 값을 가져오지 않고 **횟수를 손으로 적는다** — 상수를 상징으로
#: 쓰면 값이 바뀔 때 테스트가 함께 따라가서 변경을 잡지 못한다(실측으로 배운 것).
_MAX_ATTEMPTS: Final = 3
_RETRY_BASE_DELAY_SECONDS: Final = 0.5
_RETRY_MAX_DELAY_SECONDS: Final = 8.0
_MAX_RETRY_AFTER_SECONDS: Final = 30.0
_RETRY_JITTER_FLOOR: Final = 0.5

#: 일시적 오류만 재시도한다. 4xx는 대개 우리 요청이 틀린 것이라 다시 보내도 같은 답이 온다.
_RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

#: 백오프 대기. 테스트가 실제로 기다리지 않게 주입한다(`fetch/client.py`와 같은 방식) —
#: 재시도 간격도 검증 대상이다.
type Sleeper = Callable[[float], None]


def send_with_retry(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    params: Mapping[str, str],
    content: bytes | str | None,
    headers: Mapping[str, str],
    sleep: Sleeper,
) -> httpx.Response:
    """재시도·백오프·`Retry-After`. 실패는 전부 `StoreError`로 바꿔 던진다.

    ⚠️ **본문은 이미 인코딩된 것을 받는다** — JSON이냐 바이트냐는 부르는 쪽의 계약이다.
    여기서 인코딩하면 이 층이 두 계약을 알게 되고, 새 종류가 생길 때마다 분기가 는다.
    """
    last_error = "(원인 미기록)"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = client.request(
                method, path, params=dict(params), content=content, headers=dict(headers)
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
            _wait(attempt, _retry_after_seconds(response), sleep)
            continue
        _wait(attempt, None, sleep)
    raise StoreError(f"{method} {path}: {_MAX_ATTEMPTS}회 시도 실패 ({last_error})")


def _wait(attempt: int, retry_after: float | None, sleep: Sleeper) -> None:
    if attempt >= _MAX_ATTEMPTS:
        return
    # 서버가 알려준 대기 시간이 우리 추측보다 정확하다(fetch 층과 같은 규칙).
    backoff = min(_RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1), _RETRY_MAX_DELAY_SECONDS)
    if retry_after is not None:
        backoff = max(backoff, min(retry_after, _MAX_RETRY_AFTER_SECONDS))
    delay = backoff * random.uniform(_RETRY_JITTER_FLOOR, 1.0)
    _LOG.debug("Supabase 재시도 %d/%d — %.1fs 대기", attempt, _MAX_ATTEMPTS, delay)
    sleep(delay)


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


#: 오류 본문에서 읽을 칸 — **PostgREST와 Storage의 모양을 합친 것**이다. 둘 다 JSON 객체를
#: 주는데 PostgREST는 `message`·`details`·`hint`·`code`, Storage는 `statusCode`·`error`·
#: `message`를 쓴다. 없는 칸은 건너뛰므로 한 함수로 둘을 다 읽는다.
_ERROR_KEYS: Final = ("message", "details", "hint", "code", "error", "statusCode")


def _error_message(response: httpx.Response) -> str:
    """오류 본문에서 사람이 읽을 부분만. ⚠️ 헤더는 담지 않는다(키가 있다)."""
    status = f"HTTP {response.status_code}"
    try:
        payload: object = response.json()
    except ValueError:
        return status
    if not isinstance(payload, dict):
        return status
    parts = [str(payload[key]) for key in _ERROR_KEYS if payload.get(key)]
    return f"{status} — {' · '.join(parts)}" if parts else status
