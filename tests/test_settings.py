"""설정 로딩 테스트 — 빈 값·우선순위·`.env` 격리가 실제로 동작하는지."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from minjob_ingest.paths import (
    DEFAULT_DATA_DIR,
    DEFAULT_DOTENV_PATH,
    DEFAULT_SOURCES_PATH,
    PROJECT_ROOT,
)
from minjob_ingest.settings import (
    DEFAULT_VERTEX_LOCATION,
    DEFAULT_VERTEX_MODEL,
    ENV_DATA_DIR,
    ENV_SOURCES,
    ENV_VERTEX_CLIENT_EMAIL,
    ENV_VERTEX_LOCATION,
    ENV_VERTEX_MODEL,
    ENV_VERTEX_PRIVATE_KEY,
    ENV_VERTEX_PROJECT,
    Settings,
    VertexConfigError,
    env_str,
)

_VERTEX_ENV_NAMES = (
    ENV_VERTEX_PROJECT,
    ENV_VERTEX_LOCATION,
    ENV_VERTEX_CLIENT_EMAIL,
    ENV_VERTEX_PRIVATE_KEY,
    ENV_VERTEX_MODEL,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """실제 셸 환경이 새지 않게 격리한다. `.env`는 `dotenv_path=None`으로 차단한다."""
    monkeypatch.delenv(ENV_DATA_DIR, raising=False)
    monkeypatch.delenv(ENV_SOURCES, raising=False)
    for name in _VERTEX_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _load(*, data_dir: Path | None = None, sources_path: Path | None = None) -> Settings:
    # 기본적으로 리포의 실제 `.env`를 읽지 않는다(운영자 비밀이 테스트 프로세스로 들어오면 안 됨).
    return Settings.load(data_dir=data_dir, sources_path=sources_path, dotenv_path=None)


def test_env_str_treats_blank_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # `KEY=`(값을 지운 .env)를 빈 문자열로 통과시키면 빈 경로가 아래로 흘러간다.
    monkeypatch.setenv(ENV_DATA_DIR, "   ")
    assert env_str(ENV_DATA_DIR) is None


def test_env_str_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_DATA_DIR, "  /tmp/x  ")
    assert env_str(ENV_DATA_DIR) == "/tmp/x"


def test_env_str_returns_none_when_missing() -> None:
    assert env_str("MINJOB_DEFINITELY_NOT_SET") is None


def test_defaults_are_repo_anchored() -> None:
    # CWD 기준 상대경로면 실행 위치마다 저장소가 갈라져 원장을 잃는다.
    settings = _load()
    assert settings.data_dir == DEFAULT_DATA_DIR
    assert settings.sources_path == DEFAULT_SOURCES_PATH
    assert settings.data_dir.is_absolute()
    assert settings.data_dir.parent == PROJECT_ROOT


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_DATA_DIR, "/tmp/minjob-data")
    monkeypatch.setenv(ENV_SOURCES, "/tmp/other-sources.json")
    settings = _load()
    assert settings.data_dir == Path("/tmp/minjob-data")
    assert settings.sources_path == Path("/tmp/other-sources.json")


def test_explicit_arguments_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_DATA_DIR, "/tmp/from-env")
    assert _load(data_dir=Path("/tmp/from-flag")).data_dir == Path("/tmp/from-flag")


def test_blank_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SOURCES, "")
    assert _load().sources_path == DEFAULT_SOURCES_PATH


def test_dotenv_is_read_when_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{ENV_DATA_DIR}=/tmp/from-dotenv\n", encoding="utf-8")
    settings = Settings.load(dotenv_path=dotenv)
    assert settings.data_dir == Path("/tmp/from-dotenv")
    monkeypatch.delenv(ENV_DATA_DIR, raising=False)  # load_dotenv가 심은 값 정리


def test_shell_env_wins_over_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # override=False — CI 시크릿이 파일 값에 덮이면 안 된다.
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{ENV_DATA_DIR}=/tmp/from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv(ENV_DATA_DIR, "/tmp/from-shell")
    assert Settings.load(dotenv_path=dotenv).data_dir == Path("/tmp/from-shell")


def test_dotenv_none_ignores_repo_env_file() -> None:
    # python-dotenv는 인자 없이 부르면 **모듈 파일 위치** 기준으로 상위를 훑어 리포의 실제
    # `.env`(운영자 Vertex 키)를 읽는다 → 명시 경로/None으로만 다룬다.
    assert _load().data_dir == DEFAULT_DATA_DIR


def test_missing_dotenv_path_is_not_an_error(tmp_path: Path) -> None:
    assert Settings.load(dotenv_path=tmp_path / "nope.env").data_dir == DEFAULT_DATA_DIR


def test_env_names_are_documented_in_env_example() -> None:
    # 코드가 읽는 이름과 운영자가 채우는 이름이 다르면 설정이 조용히 무시된다.
    documented = {
        line.split("=", 1)[0].strip()
        for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert {ENV_DATA_DIR, ENV_SOURCES, *_VERTEX_ENV_NAMES} <= documented


def test_settings_is_frozen() -> None:
    settings = _load()
    with pytest.raises(AttributeError):
        settings.data_dir = Path("/tmp/nope")  # type: ignore[misc]


# ── Vertex 설정 ──────────────────────────────────────────────────


def _set_vertex(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {
        ENV_VERTEX_PROJECT: "test-project",
        ENV_VERTEX_CLIENT_EMAIL: "crawler@test.iam.gserviceaccount.com",
        ENV_VERTEX_PRIVATE_KEY: "-----BEGIN PRIVATE KEY-----\\nAAA\\n-----END PRIVATE KEY-----\\n",
        **overrides,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_require_vertex_lists_every_missing_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # 하나씩 알려주면 운영자가 세 번 실행해야 한다 → 빠진 것 전부를 한 번에 말한다.
    monkeypatch.setenv(ENV_VERTEX_PROJECT, "test-project")
    with pytest.raises(VertexConfigError) as caught:
        _load().require_vertex()
    message = str(caught.value)
    assert ENV_VERTEX_CLIENT_EMAIL in message
    assert ENV_VERTEX_PRIVATE_KEY in message
    assert ENV_VERTEX_PROJECT not in message


def test_require_vertex_treats_blank_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """값을 지운 `.env`(`KEY=`)를 통과시키면 인증 오류가 호출 시점에야 터진다.

    빈 값도 **전부 한 번에** 보고해야 한다 — 하나씩 알려주면 운영자가 여러 번 실행한다.
    """
    _set_vertex(monkeypatch, **{ENV_VERTEX_PROJECT: "   ", ENV_VERTEX_CLIENT_EMAIL: ""})
    with pytest.raises(VertexConfigError) as caught:
        _load().require_vertex()
    message = str(caught.value)
    assert ENV_VERTEX_PROJECT in message
    assert ENV_VERTEX_CLIENT_EMAIL in message


def test_require_vertex_applies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_vertex(monkeypatch)
    vertex = _load().require_vertex()
    assert vertex.location == DEFAULT_VERTEX_LOCATION
    assert vertex.model == DEFAULT_VERTEX_MODEL


def test_require_vertex_restores_pem_newlines(monkeypatch: pytest.MonkeyPatch) -> None:
    # `.env`는 개행을 리터럴 `\n`으로 담는다 — PEM으로 복원되지 않으면 인증이 실패한다.
    _set_vertex(monkeypatch)
    vertex = _load().require_vertex()
    assert vertex.private_key.startswith("-----BEGIN PRIVATE KEY-----\n")
    assert "\\n" not in vertex.private_key


def test_vertex_repr_masks_the_private_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """트레이스백·디버그 로그에 비밀키 전문이 찍히면 안 된다."""
    _set_vertex(monkeypatch)
    vertex = _load().require_vertex()
    assert "AAA" not in repr(vertex)
    assert "BEGIN PRIVATE KEY" not in repr(vertex)
    assert vertex.project_id in repr(vertex)  # 진단에 필요한 값은 남는다


@pytest.mark.skipif(
    not DEFAULT_DOTENV_PATH.is_file(), reason="리포에 `.env`가 없으면 유출 위험 자체가 없다"
)
def test_conftest_blocks_the_operator_dotenv() -> None:
    """`tests/conftest.py`의 차단이 살아 있는지 확인한다.

    이 차단이 풀리면 `Settings.load()`를 지나는 어떤 테스트든 운영자 서비스계정 키를
    `os.environ`에 들이고, `check-gemini` 경로를 타는 순간 **유료 API를 실제로 호출**한다.
    차단은 conftest에 있어 스스로는 검증되지 않으므로 여기서 감시한다.
    """
    Settings.load()  # 인자 없음 = 리포 루트 `.env`를 읽으려는 경로
    assert os.environ.get(ENV_VERTEX_PRIVATE_KEY) is None
