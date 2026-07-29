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

from minjob_agent.paths import DEFAULT_DATA_DIR, DEFAULT_DOTENV_PATH, DEFAULT_SOURCES_PATH

ENV_DATA_DIR = "MINJOB_DATA_DIR"
ENV_SOURCES = "MINJOB_SOURCES"


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


def _first_path(explicit: Path | None, from_env: str | None, fallback: Path) -> Path:
    if explicit is not None:
        return explicit
    if from_env is not None:
        return Path(from_env)
    return fallback
