"""저장소 전환 테스트 — **어디에 쓰는지**가 실행마다 분명한지.

여기서 막는 사고는 하나다: 운영자가 Supabase로 넘어갔다고 믿는데 원장이 로컬 파일에 쌓이는 것.
러너에서는 그 파일이 매 실행 사라져서 31곳 전량 재크롤 + 전량 재구조화(유료)가 된다.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from minjob_ingest.clock import kst_now
from minjob_ingest.domain import (
    Confidence,
    CrawlMode,
    Denomination,
    DenominationSource,
    IsChurchRecruitment,
    ReviewStatus,
    SourceHealthStatus,
    StoreBackend,
)
from minjob_ingest.models import (
    MAX_STRUCTURE_ATTEMPTS,
    ReviewData,
    SourceData,
    SourceHealth,
    new_id,
)
from minjob_ingest.settings import (
    ENV_STORE,
    ENV_SUPABASE_SERVICE_KEY,
    ENV_SUPABASE_URL,
    Settings,
    SupabaseConfigError,
    SupabaseSettings,
)
from minjob_ingest.store.base import Store
from minjob_ingest.store.factory import opened_store
from minjob_ingest.store.json_store import JsonStore
from minjob_ingest.store.postgrest import PostgrestClient
from minjob_ingest.store.storage import SupabaseStorage
from minjob_ingest.store.supabase_store import SupabaseStore
from tests.fake_postgrest import FakePostgrest


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


def test_the_local_store_has_no_poster_storage(data_dir: Path) -> None:
    """⚠️ 로컬 파일에는 Storage가 없다 — `poster_paths`가 빈 채로 남고 그게 정상이다."""
    with opened_store(_settings(data_dir)) as session:
        assert session.posters is None


def test_supabase_brings_poster_storage(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """검수 화면이 포스터를 띄우려면 이 값이 있어야 한다(docs/REVIEW_PAGE.md §7.1)."""
    monkeypatch.setenv(ENV_STORE, StoreBackend.SUPABASE.value)
    monkeypatch.setenv(ENV_SUPABASE_URL, "https://x.supabase.co")
    monkeypatch.setenv(ENV_SUPABASE_SERVICE_KEY, "secret")

    with opened_store(_settings(data_dir)) as session:
        assert isinstance(session.posters, SupabaseStorage)


def test_the_storage_root_is_built_once(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 경로를 메서드마다 이어 붙이면 갈라진다 — `rest_url`과 같은 자리에서 만든다."""
    monkeypatch.setenv(ENV_SUPABASE_URL, "https://x.supabase.co/")
    monkeypatch.setenv(ENV_SUPABASE_SERVICE_KEY, "secret")
    supabase = _settings(data_dir).require_supabase()

    assert supabase.storage_url == "https://x.supabase.co/storage/v1"
    assert supabase.rest_url == "https://x.supabase.co/rest/v1"


# ── 두 구현이 같은 답을 내나 (SPEC §7 `status`) ──────────────────────


def _seeded(store: Store) -> None:
    """같은 사실을 두 저장소에 똑같이 넣는다 — 미판정 2 · 포기 1 · 검수 1 · 미공개 승인 1."""
    now = kst_now()
    first = SourceData(
        source_key="YTUS",
        external_id="1",
        source_url="https://www.ytus.ac.kr/board/view/trXXR/1",
        title="성원교회 부목사 청빙",
        posted_on=now.date(),
        run_id=new_id(),
        fetched_at=now,
        raw_text="성원교회 부목사 청빙",
    )
    second = replace(first, id=new_id(), external_id="2", source_url="https://x.test/2")
    exhausted = replace(
        first,
        id=new_id(),
        external_id="3",
        source_url="https://x.test/3",
        structure_attempts=MAX_STRUCTURE_ATTEMPTS,
    )
    for record in (first, second, exhausted):
        store.save_source_data(record)
    for record, status in ((first, ReviewStatus.APPROVED), (second, ReviewStatus.PENDING)):
        store.upsert_review_data(
            ReviewData(
                posted_at=now.date(),
                source_url=record.source_url,
                source_data_id=record.id,
                run_id=record.run_id,
                is_church_recruitment=IsChurchRecruitment.YES,
                confidence=(
                    Confidence.HIGH if status is ReviewStatus.APPROVED else Confidence.MEDIUM
                ),
                denomination_source=DenominationSource.STATED,
                denomination=Denomination.TONGHAP,
                review_status=status,
            )
        )
    store.upsert_health(
        SourceHealth(
            source_key="YTUS",
            last_run_at=now,
            last_status=SourceHealthStatus.OK,
            first_run_at=now,
            last_success_at=now,
            last_rows=5,
            last_posted_on=now.date(),
        )
    )
    run = store.start_run(CrawlMode.DAILY)
    store.finish_run(run.finish(sources_ok=1, sources_failed=0, new_count=3, error_detail={}))


def test_both_stores_answer_status_the_same_way(data_dir: Path) -> None:
    """⚠️ **`status`는 저장소를 바꿔도 같은 답을 내야 한다.** 두 구현에 같은 이름의 테스트를
    두는 것으로는 한쪽 기대값을 함께 고치면 드리프트가 통과한다 — 여기서 **직접 견준다.**

    한쪽만 서버 필터를 쓰고 다른 쪽은 레코드 속성을 쓰기 때문에(개수 조회 vs `needs_restructure`)
    두 판정이 갈라질 수 있는 실제 여지가 있다.
    """
    server = FakePostgrest()
    local: Store = JsonStore(data_dir)
    remote: Store = SupabaseStore(
        PostgrestClient(
            SupabaseSettings(url="https://x.supabase.co", service_role_key="secret"),
            transport=server.transport(),
        )
    )
    for store in (local, remote):
        _seeded(store)

    assert local.pending_work() == remote.pending_work()
    assert len(local.all_health()) == len(remote.all_health())
    assert [run.mode for run in local.recent_runs(5)] == [run.mode for run in remote.recent_runs(5)]
