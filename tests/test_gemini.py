"""Gemini 래퍼 테스트 — 네트워크·실호출 없음(가드레일 #10).

SDK 클라이언트 생성(`build_client`)을 가짜로 바꿔 호출 경로를 검증한다. 실제 인증·연결은
운영자가 `minjob-ingest check-gemini`로 확인한다.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from google.genai import types
from google.oauth2 import service_account

from minjob_ingest.lib import gemini
from minjob_ingest.lib.gemini import (
    GeminiClient,
    GeminiError,
    build_client,
    require_text,
    smoke_config,
)
from minjob_ingest.settings import VertexConfigError, VertexSettings

_SETTINGS = VertexSettings(
    project_id="test-project",
    location="global",
    client_email="crawler@test.iam.gserviceaccount.com",
    private_key="-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
    model="gemini-2.5-flash",
)


def _response(
    text: str | None, finish_reason: types.FinishReason | None = types.FinishReason.STOP
) -> types.GenerateContentResponse:
    """텍스트가 있으면 part로, 없으면 content 없는 candidate로 SDK 응답을 만든다."""
    content = None if text is None else types.Content(parts=[types.Part(text=text)], role="model")
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=content, finish_reason=finish_reason)]
    )


class _FakeModels:
    """`client.models` 대역. 마지막 호출 인자를 기록하고 정해둔 결과를 돌려준다."""

    def __init__(
        self, result: types.GenerateContentResponse | Exception, calls: list[dict[str, object]]
    ) -> None:
        self._result = result
        self._calls = calls

    def generate_content(self, **kwargs: object) -> types.GenerateContentResponse:
        self._calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeClient:
    def __init__(self, result: types.GenerateContentResponse | Exception) -> None:
        self.calls: list[dict[str, object]] = []
        self.models = _FakeModels(result, self.calls)


def _client_with(
    monkeypatch: pytest.MonkeyPatch, result: types.GenerateContentResponse | Exception
) -> tuple[GeminiClient, _FakeClient]:
    fake = _FakeClient(result)
    monkeypatch.setattr(gemini, "build_client", lambda _settings: fake)
    return GeminiClient(_SETTINGS), fake


# ── 응답 해석 ────────────────────────────────────────────────────


def test_require_text_returns_the_model_text() -> None:
    assert require_text(_response("OK")) == "OK"


def test_require_text_rejects_empty_response_with_reason() -> None:
    # 빈 응답을 통과시키면 실패가 성공으로 보여 그 공고는 영구히 재시도되지 않는다(SPEC §4).
    with pytest.raises(GeminiError, match="SAFETY"):
        require_text(_response(None, finish_reason=types.FinishReason.SAFETY))


def test_require_text_rejects_blank_text() -> None:
    with pytest.raises(GeminiError, match="텍스트가 없음"):
        require_text(_response(""))


def test_require_text_reports_unknown_when_no_candidates() -> None:
    with pytest.raises(GeminiError, match="unknown"):
        require_text(types.GenerateContentResponse(candidates=[]))


# ── 호출 파라미터 ────────────────────────────────────────────────


def test_timeout_is_milliseconds_not_seconds() -> None:
    """SDK의 `timeout`은 ms다 — 초로 착각하면 60ms 만료로 모든 호출이 실패한다."""
    options = gemini.http_options()
    assert options.timeout == 60_000
    retry = options.retry_options
    assert retry is not None
    assert retry.attempts == 5


def test_smoke_config_is_deterministic_and_cheap() -> None:
    config = smoke_config()
    assert config.temperature == 0.0
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 0


def test_generate_smoke_text_uses_the_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake = _client_with(monkeypatch, _response("OK"))
    assert client.generate_smoke_text("연결 확인") == "OK"
    assert fake.calls[0]["model"] == "gemini-2.5-flash"
    assert fake.calls[0]["contents"] == "연결 확인"


# ── 실패 처리 ────────────────────────────────────────────────────


def test_sdk_error_becomes_gemini_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # 파이프라인이 한 종류만 잡아 `with_failed_attempt`로 기록할 수 있어야 한다.
    client, _ = _client_with(monkeypatch, RuntimeError("HTTP 429 RESOURCE_EXHAUSTED"))
    with pytest.raises(GeminiError, match="429") as caught:
        client.generate_smoke_text("x")
    assert isinstance(caught.value.__cause__, RuntimeError)  # 원인을 잃지 않는다


def test_empty_response_from_the_call_path_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client_with(
        monkeypatch, _response(None, finish_reason=types.FinishReason.MAX_TOKENS)
    )
    with pytest.raises(GeminiError, match="MAX_TOKENS"):
        client.generate_smoke_text("x")


# ── 잘린 응답 ────────────────────────────────────────────────────


def test_truncated_response_is_a_failure_not_a_success() -> None:
    """`MAX_TOKENS`로 끊긴 텍스트는 값이 있어도 실패다.

    통과시키면 중간에 잘린 JSON이 정상 구조화로 기록되고(`structured_at`), 그 공고는
    영구히 재구조화되지 않는다(SPEC §4).
    """
    truncated = _response('{"title": "부목사 청', finish_reason=types.FinishReason.MAX_TOKENS)
    with pytest.raises(GeminiError, match="온전히 끝나지 않음"):
        require_text(truncated)


def test_recitation_block_is_a_failure() -> None:
    with pytest.raises(GeminiError, match="RECITATION"):
        require_text(_response("일부 텍스트", finish_reason=types.FinishReason.RECITATION))


def test_missing_finish_reason_with_text_is_accepted() -> None:
    # 종료 사유가 없는 응답에서 텍스트가 온전하면 받는다(과잉 차단 방지).
    assert require_text(_response("OK", finish_reason=None)) == "OK"


# ── 클라이언트 조립 (네트워크 없음) ──────────────────────────────


def test_built_client_actually_carries_timeout_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """`http_options()`를 만들어놓고 클라이언트에 싣지 않으면 무의미하다.

    싣지 않으면 SDK 기본값이 적용되는데, 그건 **재시도 없음 + 타임아웃 무제한**이라
    한 소스가 응답하지 않으면 실행 전체가 멈춘다. 그래서 조립된 결과를 직접 확인한다.
    """

    def fake_credentials(_info: object, **_kwargs: object) -> None:
        """실제 시그니처는 `(info, **kwargs)`다 — 자격증명 검증만 건너뛴다."""
        return

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", fake_credentials)
    client = build_client(_SETTINGS)
    # SDK가 공개 접근자를 주지 않아 내부 속성을 본다(버전 올릴 때 이 테스트가 먼저 깨진다).
    options = client._api_client._http_options
    assert options.timeout == 60_000
    assert options.retry_options is not None
    assert options.retry_options.attempts == 5
    assert client.vertexai is True


def test_broken_private_key_is_reported_as_a_config_error() -> None:
    """`.env`에 PEM 개행을 놓치는 것이 가장 흔한 셋업 실수 — 생 트레이스백 대신 고칠 곳을 알린다."""
    broken = replace(_SETTINGS, private_key="-----BEGIN PRIVATE KEY-----\nGARBAGE\n-----END")
    with pytest.raises(VertexConfigError, match="VERTEX_AI_PRIVATE_KEY"):
        build_client(broken)


def test_config_error_does_not_leak_the_key(caplog: pytest.LogCaptureFixture) -> None:
    broken = replace(_SETTINGS, private_key="-----BEGIN PRIVATE KEY-----\nSHHH-SECRET\n-----END")
    with pytest.raises(VertexConfigError) as caught:
        build_client(broken)
    assert "SHHH-SECRET" not in str(caught.value)
    assert "SHHH-SECRET" not in caplog.text
