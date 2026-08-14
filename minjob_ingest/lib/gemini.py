"""Vertex AI(Gemini) 호출 창구 — 구조화 AI(SPEC §5).

**재시도·백오프·타임아웃을 손으로 만들지 않는다.** SDK가 tenacity 기반으로 제공하는 것을
설정으로 쓴다(`HttpRetryOptions`): 408·429·5xx + `httpx` 타임아웃/커넥션 오류를 지수 백오프
+ 지터로 재시도한다. 직접 만들면 예외 타입을 추측하게 되고(`cause` 체인·gRPC 코드), 실제
SDK가 던지는 것과 어긋나 **재시도되어야 할 오류가 즉시 실패**하거나 그 반대가 된다.

⚠️ SDK 재시도가 놓치는 전송 오류(`httpx.ReadError` 등 응답 도중 끊김)는 여기서 잡지 않는다 —
구조화 실패는 `SourceData.with_failed_attempt`로 기록되고 **다음 실행이 다시 집는다**
(SPEC §4, 상한 3회). 즉 유실이 아니라 지연이므로, 재시도 계층을 두 겹으로 쌓지 않는다.

⚠️ **토큰이 곧 돈이다**. 여기는 "부르는 법"만 담고, 무엇을 몇 번 부를지는
파이프라인(`pipeline/structure.py`)이 결정한다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final, Protocol

from google import genai
from google.genai import types
from google.oauth2 import service_account

from minjob_ingest.settings import VertexConfigError, VertexSettings

_LOG = logging.getLogger(__name__)

#: 요청 상한(ms). 응답이 오지 않는 호출에 실행 전체가 묶이지 않게 한다.
REQUEST_TIMEOUT_MS: Final = 60_000

#: 재시도 정책 — TS 프로토타입에서 검증한 값(최대 5회 = 최초 1 + 재시도 4).
_RETRY_ATTEMPTS: Final = 5
_RETRY_INITIAL_DELAY_SEC: Final = 1.0
_RETRY_MAX_DELAY_SEC: Final = 30.0

#: 스모크·범용 텍스트 호출 파라미터. 구조화는 `structure_config`가 따로 쓴다.
_SMOKE_MAX_OUTPUT_TOKENS: Final = 256
#: 같은 입력에 같은 출력을 원한다(구조화는 창작이 아니다).
_DETERMINISTIC_TEMPERATURE: Final = 0.0

#: 구조화 응답 상한. 34필드 + 목록 다섯 칸을 담아야 한다. 상한에 걸려 잘린
#: JSON은 `require_text`가 `MAX_TOKENS`로 걸러 실패로 만든다(반쯤 온 JSON을 파싱하지 않는다).
_STRUCTURE_MAX_OUTPUT_TOKENS: Final = 4_096

#: 응답 형식. `response_schema`와 **함께** 줘야 한다 — mime만 주면 "JSON 비슷한 것"이 온다.
_JSON_MIME_TYPE: Final = "application/json"

_SCOPES: Final = ("https://www.googleapis.com/auth/cloud-platform",)

#: 오류 메시지에 쓰는 환경변수 이름(운영자가 고칠 대상을 정확히 가리킨다).
ENV_PRIVATE_KEY_HINT: Final = "VERTEX_AI_PRIVATE_KEY"


class MediaPart(Protocol):
    """이 층이 그림에 대해 아는 전부. 구현은 `pipeline/media.Media`.

    구조화 모듈을 import하지 않으려고 프로토콜로 둔다 — 전송 층이 파이프라인을 알면 층이
    뒤집힌다(CLAUDE.md Layer Responsibilities).
    """

    @property
    def media_type(self) -> str: ...
    @property
    def data(self) -> bytes: ...


class GeminiError(Exception):
    """모델 호출이 실패했거나 쓸 수 있는 응답이 오지 않았을 때.

    파이프라인은 이걸 잡아 `with_failed_attempt`로 기록한다 — `structured_at`은 남기지
    않으므로 다음 실행이 재시도한다(SPEC §4).
    """


def build_client(settings: VertexSettings) -> genai.Client:
    """서비스계정으로 인증한 Vertex 클라이언트.

    `google-genai`는 ADC(gcloud 로그인·메타데이터 서버)를 먼저 찾으므로, 운영자 로컬과
    GitHub Actions에서 **같은 자격증명**을 쓰도록 `.env`의 서비스계정을 명시로 넘긴다.
    """
    try:
        # google-auth는 py.typed를 제공하지만 이 팩토리만 주석이 없다(`info, **kwargs`) →
        # 이 호출 한 줄만 예외로 두고 나머지는 계속 strict로 검사한다.
        credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            {
                "type": "service_account",
                "project_id": settings.project_id,
                "client_email": settings.client_email,
                "private_key": settings.private_key,
                "token_uri": "https://oauth2.googleapis.com/token",
            },
            scopes=list(_SCOPES),
        )
    except ValueError as err:
        # `.env`에 PEM을 넣다 개행을 놓치는 것이 가장 흔한 셋업 실수다. 생 트레이스백 대신
        # 무엇을 고쳐야 하는지 알려준다(예외 메시지에 키 자료는 포함되지 않는다).
        raise VertexConfigError(
            f"{ENV_PRIVATE_KEY_HINT} 형식 오류 — 개행을 `\\n`으로 넣었는지 확인 ({err})"
        ) from err
    return genai.Client(
        vertexai=True,
        project=settings.project_id,
        location=settings.location,
        credentials=credentials,
        http_options=http_options(),
    )


def http_options() -> types.HttpOptions:
    """타임아웃 + 재시도. `timeout`은 **밀리초**다(SDK 규약)."""
    return types.HttpOptions(
        timeout=REQUEST_TIMEOUT_MS,
        retry_options=types.HttpRetryOptions(
            attempts=_RETRY_ATTEMPTS,
            initial_delay=_RETRY_INITIAL_DELAY_SEC,
            max_delay=_RETRY_MAX_DELAY_SEC,
        ),
    )


def smoke_config() -> types.GenerateContentConfig:
    """연결 확인용 최소 설정 — 생각 예산 0으로 토큰을 아낀다."""
    return types.GenerateContentConfig(
        temperature=_DETERMINISTIC_TEMPERATURE,
        max_output_tokens=_SMOKE_MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


def structure_config(schema: types.Schema) -> types.GenerateContentConfig:
    """구조화 설정 — 출력 스키마를 모델에 **강제**한다(SPEC §5).

    `response_mime_type`만 주면 "JSON처럼 생긴 것"이 오고 필드·타입은 보장되지 않는다.
    스키마까지 줘야 모델이 그 모양으로만 답한다.

    ⚠️ **생각 예산 0**: 추출은 추론이 아니라 옮겨 적기다(입력 중앙값 507자 · 2026-08-10 실측).
    켜두면 생각 토큰이 출력 상한을 먹어 **본문 대신 빈 응답**으로 끝나는 경로가 생긴다.
    품질이 모자라면 2단계에서 올린다. ⚠️ 0을 받지 않는 모델(2.5 Pro 계열)로 `VERTEX_MODEL`을
    바꾸면 이 값도 함께 고쳐야 한다 — `smoke_config`도 같은 전제다.
    """
    return types.GenerateContentConfig(
        temperature=_DETERMINISTIC_TEMPERATURE,
        max_output_tokens=_STRUCTURE_MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        response_mime_type=_JSON_MIME_TYPE,
        response_schema=schema,
    )


#: 정상 종료. `None`은 응답에 종료 사유가 없는 경우(비스트리밍에선 드묾)로, 텍스트가 있으면 받는다.
_COMPLETE_REASONS: Final = (None, types.FinishReason.STOP)


def require_text(response: types.GenerateContentResponse) -> str:
    """온전히 끝난 응답의 텍스트를 꺼낸다. 아니면 `GeminiError`.

    두 가지를 모두 막는다 — 통과시키면 **실패가 성공으로 보여** 그 공고는 판정 완료로
    기록되고 영구히 재시도되지 않는다(SPEC §4).
      ① 텍스트 없음: 안전 차단(`SAFETY`)·생각만 하고 끝난 응답.
      ② **잘린 텍스트**: `MAX_TOKENS`로 중간에 끊긴 JSON은 "값이 있으니 성공"처럼 보인다.
    """
    reason = _finish_reason(response)
    if reason not in _COMPLETE_REASONS:
        raise GeminiError(f"모델 응답이 온전히 끝나지 않음 (finishReason={_reason_label(reason)})")
    text = response.text
    if text:
        return text
    raise GeminiError(f"모델 응답에 텍스트가 없음 (finishReason={_reason_label(reason)})")


def _finish_reason(response: types.GenerateContentResponse) -> types.FinishReason | None:
    candidates = response.candidates
    return None if not candidates else candidates[0].finish_reason


def _reason_label(reason: types.FinishReason | None) -> str:
    return "unknown" if reason is None else str(reason.value)


class GeminiClient:
    """Gemini 호출 래퍼. 실행 1회분을 들고 다닌다(전역 캐시 없음)."""

    def __init__(self, settings: VertexSettings) -> None:
        self._model = settings.model
        self._client = build_client(settings)

    @property
    def model(self) -> str:
        return self._model

    def generate_smoke_text(self, prompt: str) -> str:
        """연결 확인용 텍스트 호출. 실패·빈 응답·잘린 응답은 `GeminiError`.

        ⚠️ **구조화에 쓰지 말 것.** `smoke_config()`의 출력 상한(256토큰)에 걸려 공고 대부분이
        잘린다 — 구조화는 `generate_structured_json`이다.
        """
        _LOG.info("Gemini 연결 확인 (model=%s, prompt=%d자)", self._model, len(prompt))
        return self._generate([types.Part.from_text(text=prompt)], smoke_config())

    def generate_structured_json(
        self, prompt: str, *, schema: types.Schema, images: Sequence[MediaPart] = ()
    ) -> str:
        """스키마를 강제해 받은 JSON **텍스트**. 실패·빈 응답·잘린 응답은 `GeminiError`.

        파싱하지 않고 문자열로 돌려준다 — 무엇을 기대하는지는 부르는 쪽(`pipeline/extraction.py`)이
        알고 이 층은 전송만 안다. SDK의 `response.parsed`를 쓰지 않는 이유도 같다: 그건 우리
        dataclass가 아니고, 받아 쓰면 경계 검증을 건너뛴다(CLAUDE.md "경계에서 검증").

        ⚠️ **그림은 프롬프트 뒤에 붙인다.** 앞에 두면 모델이 지시를 읽기 전에 그림부터 보고,
        무엇을 뽑아야 하는지 모른 채 묘사를 시작한다.
        """
        _LOG.info(
            "Gemini 구조화 (model=%s, prompt=%d자, 그림 %d장)",
            self._model,
            len(prompt),
            len(images),
        )
        parts = [
            types.Part.from_text(text=prompt),
            *(types.Part.from_bytes(data=item.data, mime_type=item.media_type) for item in images),
        ]
        return self._generate(parts, structure_config(schema))

    def _generate(self, parts: Sequence[types.Part], config: types.GenerateContentConfig) -> str:
        # ⚠️ SDK가 받는 것은 여러 형식의 합집합 리스트다. `list[Part]`로 넘기면 리스트가
        #    불변(invariant)이라 타입이 안 맞는다 — 값은 그대로고 선언만 넓힌다.
        contents: list[types.PartUnionDict] = list(parts)
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except Exception as err:  # SDK 예외 계층이 넓다 → 파이프라인이 다룰 한 종류로 좁힌다.
            raise GeminiError(f"Gemini 호출 실패: {err}") from err
        return require_text(response)
