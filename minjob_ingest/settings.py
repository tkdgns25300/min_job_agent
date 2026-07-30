"""환경 설정 단일 창구.

CLAUDE.md: "env는 `settings.py` 한 곳에서 읽는다. import 시점에 캡처하지 말고(dotenv 로드보다
먼저 실행됨), 빈 문자열은 미설정으로 취급한다."

그래서 모듈 상단에서 값을 읽지 않고, CLI가 `Settings.load()`를 **한 번** 호출해 아래로 넘긴다.
비밀은 코드·config·로그에 남기지 않는다(가드레일).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from minjob_ingest.paths import DEFAULT_DATA_DIR, DEFAULT_DOTENV_PATH, DEFAULT_SOURCES_PATH

ENV_DATA_DIR = "MINJOB_DATA_DIR"
ENV_SOURCES = "MINJOB_SOURCES"

ENV_VERTEX_PROJECT = "VERTEX_AI_PROJECT_ID"
ENV_VERTEX_LOCATION = "VERTEX_AI_LOCATION"
ENV_VERTEX_CLIENT_EMAIL = "VERTEX_AI_CLIENT_EMAIL"
ENV_VERTEX_PRIVATE_KEY = "VERTEX_AI_PRIVATE_KEY"
ENV_VERTEX_MODEL = "VERTEX_MODEL"

DEFAULT_VERTEX_LOCATION = "global"
#: 운영자가 실사용 가능함을 확인한 모델(2026-07-29). `gemini-2.5-flash-lite`도 사용 가능.
DEFAULT_VERTEX_MODEL = "gemini-2.5-flash"

_MASKED = "***"


class VertexConfigError(Exception):
    """Vertex 설정이 없거나 비어 있을 때. 호출 전에 알려주기 위한 오류다."""


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
    (frozen dataclass의 `repr`은 모든 필드를 나열한다). 그래서 직접 정의한다 — 가드레일:
    비밀은 코드·config·로그에 남기지 않는다.
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
class Settings:
    """실행 1회분 설정. 필요한 곳에 인자로 넘긴다(전역 조회 금지)."""

    #: 로컬 JSON 저장소 디렉터리.
    data_dir: Path
    #: 소스 레지스트리 config 경로.
    sources_path: Path

    @classmethod
    def load(
        cls,
        *,
        data_dir: Path | None = None,
        sources_path: Path | None = None,
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
        )

    def require_vertex(self) -> VertexSettings:
        """Vertex 설정을 검증해 반환한다. 하나라도 없으면 `VertexConfigError`.

        `Settings`를 거쳐야만 얻을 수 있게 **메서드로** 둔다 — `load()`가 `.env`를 이미
        읽었음이 보장되므로 "dotenv보다 먼저 env를 읽어 빈 값을 보는" 순서 사고가 불가능하다.
        `list-sources`처럼 Gemini가 필요 없는 명령은 이걸 부르지 않아 GCP 계정 없이도 돈다.
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
            model=env_str(ENV_VERTEX_MODEL) or DEFAULT_VERTEX_MODEL,
        )


def _require_env(name: str) -> str:
    value = env_str(name)
    if value is None:
        raise VertexConfigError(f"환경변수 미설정: {name} (.env 확인)")
    return value


def _first_path(explicit: Path | None, from_env: str | None, fallback: Path) -> Path:
    if explicit is not None:
        return explicit
    if from_env is not None:
        return Path(from_env)
    return fallback
