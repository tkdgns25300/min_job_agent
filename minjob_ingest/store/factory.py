"""저장소를 여는 단 한 곳 — CLI가 `JsonStore`·`SupabaseStore`를 직접 만들지 않게 한다.

여기 모으는 이유: 저장소를 고르는 판단이 호출 지점마다 있으면 **한 곳만 옮겨지고 나머지는
로컬 파일에 쓴다**. 그러면 같은 실행이 원장을 두 곳에 나눠 남기고, 그 어긋남은 "왜 이 공고가
없지"로만 드러난다.

⚠️ **전환은 명시로만 한다**(`StoreBackend`) — `SUPABASE_URL`이 있으면 자동으로 넘어가게 하면,
키를 넣어 두기만 한 상태에서 돌린 수집이 어디에 쌓였는지 사람이 알 수 없다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from minjob_ingest.domain import StoreBackend
from minjob_ingest.settings import Settings
from minjob_ingest.store.base import PosterStore, PublishTarget, Store
from minjob_ingest.store.jobs_gateway import SupabaseJobs
from minjob_ingest.store.json_store import JsonStore
from minjob_ingest.store.postgrest import PostgrestClient
from minjob_ingest.store.storage import SupabaseStorage
from minjob_ingest.store.supabase_store import SupabaseStore


@dataclass(frozen=True, slots=True)
class StoreSession:
    """한 실행이 쓰는 저장소들. 전송을 공유하므로 함께 열고 함께 닫는다."""

    #: staging 4테이블. 백엔드가 무엇이든 항상 있다.
    store: Store
    #: `jobs` 접근. ⚠️ **JSON 백엔드에는 없다**(`None`) — 로컬 파일에는 공개 테이블이 없다.
    #: 공개 경로는 이 값이 `None`이면 시작하지 않는다.
    jobs: PublishTarget | None
    #: 포스터 보관. ⚠️ **JSON 백엔드에는 없다**(`None`) — 그러면 `poster_paths`가 빈 채로
    #: 남는다. 검수 화면은 Supabase에서만 도므로 로컬 실행에 손실이 없다.
    posters: PosterStore | None
    #: 화면에 찍을 이름. 어디에 쓰는지 매 실행 보이게 한다.
    label: str


@contextmanager
def opened_store(settings: Settings) -> Iterator[StoreSession]:
    """설정이 가리키는 저장소를 열고, 끝나면 전송을 닫는다.

    ⚠️ Supabase 설정이 비어 있으면 **한 건도 쓰기 전에** `SupabaseConfigError`로 멈춘다
    (`Settings.require_supabase`) — 절반만 쌓인 원장이 제일 나쁘다.
    """
    match settings.store_backend:
        case StoreBackend.JSON:
            # 로컬 파일은 닫을 것이 없다. `jobs`도 없다 — 공개는 Supabase에서만 한다.
            yield StoreSession(
                store=JsonStore(settings.data_dir),
                jobs=None,
                posters=None,
                label=str(settings.data_dir),
            )
        case StoreBackend.SUPABASE:
            supabase = settings.require_supabase()
            # ⚠️ 전송이 둘이다(원장은 `/rest/v1` · 포스터는 `/storage/v1`) — 함께 열고 함께
            #    닫는다. 하나만 닫으면 소켓이 남는다.
            with PostgrestClient(supabase) as client, SupabaseStorage(supabase) as storage:
                yield StoreSession(
                    store=SupabaseStore(client),
                    jobs=SupabaseJobs(client),
                    posters=storage,
                    label=supabase.url,
                )
