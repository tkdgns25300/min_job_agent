"""환경 설정 단일 창구.

CLAUDE.md: "env는 `settings.py` 한 곳에서 읽는다. import 시점에 캡처하지 말고(dotenv 로드보다
먼저 실행됨), 빈 문자열은 미설정으로 취급한다."

그래서 모듈 상단에서 값을 읽지 않고, CLI가 `Settings.load()`를 **한 번** 호출해 아래로 넘긴다.
비밀은 코드·config·로그에 남기지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from minjob_ingest.domain import StoreBackend
from minjob_ingest.paths import (
    DEFAULT_DATA_DIR,
    DEFAULT_DOTENV_PATH,
    DEFAULT_HERESY_PATH,
    DEFAULT_SOURCES_PATH,
)

ENV_DATA_DIR = "MINJOB_DATA_DIR"
ENV_SOURCES = "MINJOB_SOURCES"
ENV_HERESY = "MINJOB_HERESY_REF"
#: 저장소 구현 선택. 미설정이면 `StoreBackend.JSON`(Phase 1 기본).
ENV_STORE = "MINJOB_STORE"

ENV_SUPABASE_URL = "SUPABASE_URL"
ENV_SUPABASE_SERVICE_KEY = "SUPABASE_SERVICE_ROLE_KEY"

ENV_VERTEX_PROJECT = "VERTEX_AI_PROJECT_ID"
ENV_VERTEX_LOCATION = "VERTEX_AI_LOCATION"
ENV_VERTEX_CLIENT_EMAIL = "VERTEX_AI_CLIENT_EMAIL"
ENV_VERTEX_PRIVATE_KEY = "VERTEX_AI_PRIVATE_KEY"
ENV_VERTEX_MODEL = "VERTEX_MODEL"
#: 값싼 대안 모델. **기본이 아니다** — `--lite`로 명시할 때만 쓴다.
ENV_VERTEX_MODEL_LITE = "VERTEX_MODEL_LITE"

DEFAULT_VERTEX_LOCATION = "global"
_MASKED = "***"


class VertexConfigError(Exception):
    """Vertex 설정이 없거나 비어 있을 때. 호출 전에 알려주기 위한 오류다."""


class SupabaseConfigError(Exception):
    """Supabase 설정이 없거나 모양이 틀렸을 때. **한 건이라도 쓰기 전에** 멈추기 위한 오류다."""


def env_str(name: str) -> str | None:
    """환경변수 조회 — 빈 문자열·공백은 미설정으로 취급한다.

    `os.environ.get(name, default)`는 값을 지운 `.env`(`KEY=`)를 빈 문자열로 통과시켜
    빈 경로·빈 모델명이 아래로 흘러간다.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


@dataclass(frozen=True, slots=True)
class VertexSettings:
    """Vertex AI(Gemini) 접속 정보. 서비스계정 비밀을 담으므로 repr를 마스킹한다.

    ⚠️ 기본 `repr`는 예외 트레이스백·디버그 로그에 **private key 전문을 그대로 찍는다**
    (frozen dataclass의 `repr`은 모든 필드를 나열한다). 그래서 직접 정의한다 — 비밀은
    코드·config·로그에 남기지 않는다.
    """

    project_id: str
    location: str
    client_email: str
    private_key: str
    model: str

    def __repr__(self) -> str:
        return (
            f"VertexSettings(project_id={self.project_id!r}, location={self.location!r},"
            f" client_email={self.client_email!r}, private_key={_MASKED}, model={self.model!r})"
        )


@dataclass(frozen=True, slots=True)
class SupabaseSettings:
    """Supabase 접속 정보. 비밀 키를 담으므로 `VertexSettings`와 같은 이유로 repr를 마스킹한다.

    ⚠️ 지금은 **전권 `service_role`** 키다(운영자 결정 2026-08-21 · SPEC §8). 컬럼 단위
    GRANT가 실제로 듣는 별도 `crawler` 롤은 RLS 마이그레이션과 함께 온다 — 그때까지 `jobs`를
    지키는 것은 코드가 UPDATE 자리를 한 칸으로 좁혀 둔 것뿐이다.
    """

    url: str
    service_role_key: str

    @property
    def rest_url(self) -> str:
        """PostgREST 루트. 경로를 한 곳에서만 만든다 — 메서드마다 이어 붙이면 갈라진다."""
        return f"{self.url}/rest/v1"

    def __repr__(self) -> str:
        return f"SupabaseSettings(url={self.url!r}, service_role_key={_MASKED})"


@dataclass(frozen=True, slots=True)
class Settings:
    """실행 1회분 설정. 필요한 곳에 인자로 넘긴다(전역 조회 금지)."""

    #: 로컬 JSON 저장소 디렉터리.
    data_dir: Path
    #: 소스 레지스트리 config 경로.
    sources_path: Path
    #: 이단 참고 목록 경로(커밋 금지 · 사람이 관리).
    heresy_path: Path
    #: 어느 저장소에 쓸지. 기본 `JSON` — 넘어가려면 `.env`에 명시한다(`StoreBackend` 참조).
    store_backend: StoreBackend = StoreBackend.JSON

    @classmethod
    def load(
        cls,
        *,
        data_dir: Path | None = None,
        sources_path: Path | None = None,
        heresy_path: Path | None = None,
        dotenv_path: Path | None = DEFAULT_DOTENV_PATH,
    ) -> Settings:
        """`.env`(있으면) + 환경변수에서 읽는다. 인자로 준 값이 최우선(CLI 플래그).

        ⚠️ `dotenv_path`를 **명시**한다 — `load_dotenv()`를 인자 없이 부르면 python-dotenv가
        CWD가 아니라 **호출한 모듈 파일 위치**를 기준으로 상위 디렉터리를 훑는다. 그래서
        테스트가 `chdir`로 격리한 줄 알아도 리포 루트의 실제 `.env`(운영자 Vertex 키 포함)를
        읽어 `os.environ`에 남긴다. `None`을 주면 아예 읽지 않는다(테스트 기본값).
        """
        if dotenv_path is not None and dotenv_path.is_file():
            load_dotenv(dotenv_path=dotenv_path, override=False)
        return cls(
            data_dir=_first_path(data_dir, env_str(ENV_DATA_DIR), DEFAULT_DATA_DIR),
            sources_path=_first_path(sources_path, env_str(ENV_SOURCES), DEFAULT_SOURCES_PATH),
            heresy_path=_first_path(heresy_path, env_str(ENV_HERESY), DEFAULT_HERESY_PATH),
            store_backend=_store_backend(env_str(ENV_STORE)),
        )

    def require_vertex(self, *, lite: bool = False) -> VertexSettings:
        """Vertex 설정을 검증해 반환한다. 하나라도 없으면 `VertexConfigError`.

        `Settings`를 거쳐야만 얻을 수 있게 **메서드로** 둔다 — `load()`가 `.env`를 이미
        읽었음이 보장되므로 "dotenv보다 먼저 env를 읽어 빈 값을 보는" 순서 사고가 불가능하다.
        `list-sources`처럼 Gemini가 필요 없는 명령은 이걸 부르지 않아 GCP 계정 없이도 돈다.

        `lite=True`면 `VERTEX_MODEL_LITE`를 쓴다 — 기본은 `VERTEX_MODEL`이다.
        """
        missing = [
            name
            for name in (ENV_VERTEX_PROJECT, ENV_VERTEX_CLIENT_EMAIL, ENV_VERTEX_PRIVATE_KEY)
            if env_str(name) is None
        ]
        if missing:
            raise VertexConfigError(f"환경변수 미설정: {', '.join(missing)} (.env 확인)")
        return VertexSettings(
            project_id=_require_env(ENV_VERTEX_PROJECT),
            location=env_str(ENV_VERTEX_LOCATION) or DEFAULT_VERTEX_LOCATION,
            client_email=_require_env(ENV_VERTEX_CLIENT_EMAIL),
            # `.env`는 개행을 한 줄로 넣으려고 `\n`(리터럴 백슬래시+n)으로 이스케이프한다 →
            # PEM으로 복원한다. 이미 실제 개행이면 이 치환은 아무 일도 하지 않는다.
            private_key=_require_env(ENV_VERTEX_PRIVATE_KEY).replace("\\n", "\n"),
            # ⚠️ **양쪽 다 env가 없으면 멈춘다.** 기본 모델에만 폴백을 두면 `VERTEX_MODEL`
            #    오타 하나에 낡은 모델 ID로 조용히 청구된다 — `--lite`가 멈추는 것과 같은
            #    이유다(CLAUDE.md: "모델 ID는 env에서 읽고 하드코딩하지 않는다").
            model=_require_env(ENV_VERTEX_MODEL_LITE if lite else ENV_VERTEX_MODEL),
        )

    def require_supabase(self) -> SupabaseSettings:
        """Supabase 설정을 검증해 반환한다. 하나라도 없거나 모양이 틀리면 `SupabaseConfigError`.

        `require_vertex`와 같은 이유로 **메서드**다 — `load()`가 `.env`를 이미 읽었음이
        보장되므로 dotenv보다 먼저 env를 읽어 빈 값을 보는 순서 사고가 불가능하다.

        ⚠️ **URL 모양을 여기서 검증한다.** 오타난 호스트로 붙으면 PostgREST가 아니라 남의
        서버에 요청이 가고, 응답이 JSON이 아니어서 나는 오류는 "설정이 틀렸다"로 읽히지
        않는다. 끝의 `/`도 여기서 떼야 `rest_url`이 `//rest/v1`이 되지 않는다.
        """
        missing = [
            name for name in (ENV_SUPABASE_URL, ENV_SUPABASE_SERVICE_KEY) if env_str(name) is None
        ]
        if missing:
            raise SupabaseConfigError(f"환경변수 미설정: {', '.join(missing)} (.env 확인)")
        url = _require_env(ENV_SUPABASE_URL, SupabaseConfigError).rstrip("/")
        if not url.startswith("https://"):
            raise SupabaseConfigError(f"{ENV_SUPABASE_URL}는 https:// 로 시작해야 함 ({url!r})")
        return SupabaseSettings(
            url=url,
            service_role_key=_require_env(ENV_SUPABASE_SERVICE_KEY, SupabaseConfigError),
        )


def _store_backend(from_env: str | None) -> StoreBackend:
    """`MINJOB_STORE` 해석. ⚠️ 모르는 값을 조용히 기본값으로 떨어뜨리지 않는다.

    `MINJOB_STORE=supabse`(오타) 하나로 원장이 로컬 파일에 쌓이면, 운영자는 Supabase에
    들어갔다고 믿는데 러너에서는 매 실행 사라진다 — 알아차릴 때는 이미 전량 재구조화다.
    """
    if from_env is None:
        return StoreBackend.JSON
    try:
        return StoreBackend(from_env.lower())
    except ValueError as err:
        allowed = ", ".join(backend.value for backend in StoreBackend)
        raise SupabaseConfigError(
            f"{ENV_STORE}={from_env!r}는 허용값 아님 (허용 {allowed})"
        ) from err


def _require_env(name: str, error: type[Exception] = VertexConfigError) -> str:
    value = env_str(name)
    if value is None:
        raise error(f"환경변수 미설정: {name} (.env 확인)")
    return value


def _first_path(explicit: Path | None, from_env: str | None, fallback: Path) -> Path:
    if explicit is not None:
        return explicit
    if from_env is not None:
        return Path(from_env)
    return fallback
