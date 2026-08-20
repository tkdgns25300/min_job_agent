"""저장소 전환 테스트 — **어디에 쓰는지**가 실행마다 분명한지.

여기서 막는 사고는 하나다: 운영자가 Supabase로 넘어갔다고 믿는데 원장이 로컬 파일에 쌓이는 것.
러너에서는 그 파일이 매 실행 사라져서 31곳 전량 재크롤 + 전량 재구조화(유료)가 된다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minjob_ingest.domain import StoreBackend
from minjob_ingest.settings import (
    ENV_STORE,
    ENV_SUPABASE_SERVICE_KEY,
    ENV_SUPABASE_URL,
    Settings,
    SupabaseConfigError,
)
from minjob_ingest.store.factory import opened_store
from minjob_ingest.store.json_store import JsonStore
from minjob_ingest.store.supabase_store import SupabaseStore


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _settings(data_dir: Path) -> Settings:
    return Settings.load(data_dir=data_dir, dotenv_path=None)


def test_the_default_is_the_local_file_store(data_dir: Path) -> None:
    """Phase 1 기본이다 — 명시하지 않으면 지금까지와 똑같이 돈다."""
    with opened_store(_settings(data_dir)) as session:
        assert isinstance(session.store, JsonStore)
        assert session.label == str(data_dir)


def test_the_local_store_cannot_publish(data_dir: Path) -> None:
    """⚠️ 로컬 파일에는 `jobs`가 없다 — 공개 경로는 이 값이 `None`이면 시작하지 않는다."""
    with opened_store(_settings(data_dir)) as session:
        assert session.jobs is None


def test_supabase_is_chosen_only_when_asked(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ **암묵 전환을 두지 않는다** — 키만 넣어 둔 상태에서 돌린 수집이 어디에 쌓였는지
    사람이 알 수 없게 되기 때문이다."""
    monkeypatch.setenv(ENV_SUPABASE_URL, "https://x.supabase.co")
    monkeypatch.setenv(ENV_SUPABASE_SERVICE_KEY, "secret")

    with opened_store(_settings(data_dir)) as session:
        assert isinstance(session.store, JsonStore)


def test_asking_for_supabase_gives_the_remote_store(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_STORE, StoreBackend.SUPABASE.value)
    monkeypatch.setenv(ENV_SUPABASE_URL, "https://x.supabase.co/")
    monkeypatch.setenv(ENV_SUPABASE_SERVICE_KEY, "secret")

    with opened_store(_settings(data_dir)) as session:
        assert isinstance(session.store, SupabaseStore)
        assert session.jobs is not None
        # 끝의 `/`는 떼고 보여준다 — `//rest/v1`이 되지 않게 설정이 정규화한다.
        assert session.label == "https://x.supabase.co"


def test_the_label_never_shows_the_key(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """화면·로그에 찍히는 값이라 비밀이 섞이면 안 된다."""
    monkeypatch.setenv(ENV_STORE, StoreBackend.SUPABASE.value)
    monkeypatch.setenv(ENV_SUPABASE_URL, "https://x.supabase.co")
    monkeypatch.setenv(ENV_SUPABASE_SERVICE_KEY, "top-secret-key")

    with opened_store(_settings(data_dir)) as session:
        assert "top-secret-key" not in session.label


def test_missing_supabase_settings_stop_us_before_any_write(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ 절반만 쌓인 원장이 제일 나쁘다 — 한 건도 쓰기 전에 멈춘다."""
    monkeypatch.setenv(ENV_STORE, StoreBackend.SUPABASE.value)
    monkeypatch.delenv(ENV_SUPABASE_URL, raising=False)
    monkeypatch.delenv(ENV_SUPABASE_SERVICE_KEY, raising=False)

    with (
        pytest.raises(SupabaseConfigError, match=ENV_SUPABASE_URL),
        opened_store(_settings(data_dir)),
    ):
        pass


def test_a_typo_in_the_backend_is_refused(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ `MINJOB_STORE=supabse` 하나로 원장이 조용히 로컬 파일에 쌓이면 알아차릴 때는 이미
    전량 재구조화다 — 모르는 값을 기본값으로 떨어뜨리지 않는다."""
    monkeypatch.setenv(ENV_STORE, "supabse")

    with pytest.raises(SupabaseConfigError, match="허용값 아님"):
        _settings(data_dir)


def test_the_backend_name_is_case_insensitive(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env`에 대문자로 적는 사람이 있다 — 오타와 대소문자는 다른 문제다."""
    monkeypatch.setenv(ENV_STORE, "SUPABASE")
    assert _settings(data_dir).store_backend is StoreBackend.SUPABASE
