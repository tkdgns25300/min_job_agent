"""설정 로딩 테스트 — 빈 값·우선순위·`.env` 격리가 실제로 동작하는지."""

from __future__ import annotations

from pathlib import Path

import pytest

from minjob_agent.paths import DEFAULT_DATA_DIR, DEFAULT_SOURCES_PATH, PROJECT_ROOT
from minjob_agent.settings import ENV_DATA_DIR, ENV_SOURCES, Settings, env_str


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """실제 셸 환경이 새지 않게 격리한다. `.env`는 `dotenv_path=None`으로 차단한다."""
    monkeypatch.delenv(ENV_DATA_DIR, raising=False)
    monkeypatch.delenv(ENV_SOURCES, raising=False)


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
    # CWD 기준 상대경로면 실행 위치마다 저장소가 갈라져 원장을 잃는다(가드레일 #7·#11).
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
    assert {ENV_DATA_DIR, ENV_SOURCES} <= documented


def test_settings_is_frozen() -> None:
    settings = _load()
    with pytest.raises(AttributeError):
        settings.data_dir = Path("/tmp/nope")  # type: ignore[misc]
