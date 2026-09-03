"""공개·끌어올림 테스트 — **공개 테이블에 대고 도는 코드**라 여기가 가장 조심스럽다.

**네트워크를 타지 않는다** — `tests/fake_postgrest.py`가 `review_data`와 `jobs`를 함께 흉내낸다.
저장소를 진짜로 지나가므로 판정뿐 아니라 **요청 문법과 순서**까지 함께 검증된다.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID, uuid4

import pytest

from minjob_ingest.clock import today_kst
from minjob_ingest.domain import (
    Confidence,
    DedupState,
    Denomination,
    DenominationSource,
    Department,
    IsChurchRecruitment,
    JobKind,
    Position,
    Region,
    RejectReason,
    ReviewStatus,
)
from minjob_ingest.models import ReviewData, SourceData
from minjob_ingest.pipeline.dedup import dedup_all
from minjob_ingest.pipeline.publish import (
    MAX_CONSECUTIVE_FAILURES,
    PublishReport,
    publish_all,
)
from minjob_ingest.settings import SupabaseSettings
from minjob_ingest.store.base import StoreError
from minjob_ingest.store.jobs_gateway import SupabaseJobs
from minjob_ingest.store.postgrest import PostgrestClient
from minjob_ingest.store.serde import to_row
from minjob_ingest.store.supabase_store import SupabaseStore
from tests.fake_postgrest import FakePostgrest

FIXED_NOW: Final = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_DAY: Final = date(2026, 8, 1)
_SETTINGS: Final = SupabaseSettings(url="https://x.supabase.co", service_role_key="k")

_JOBS_COLUMNS: Final = {
    "id",
    "church_id",
    "church_name",
    "denomination",
    "region",
    "city",
    "address",
    "title",
    "job_kind",
    "position",
    "role",
    "department",
    "employment_type",
    "qualification",
    "headcount",
    "start_timing",
    "housing_provided",
    "housing_note",
    "pay_min",
    "pay_max",
    "pay_note",
    "pay_period",
    "benefit_note",
    "status",
    "source",
    "source_url",
    "contact_email",
    "contact_tel",
    "contact_link",
    "contact_post",
    "work_days",
    "requirements",
    "preferred",
    "required_docs",
    "optional_docs",
    "process_steps",
    "description",
    "posted_at",
    "deadline",
    "created_at",
    "updated_at",
}


@pytest.fixture
def server() -> FakePostgrest:
    fake = FakePostgrest()
    fake.schema = {
        "jobs": set(_JOBS_COLUMNS),
        "source_data": {f.name for f in fields(SourceData)},
        "review_data": {f.name for f in fields(ReviewData)},
    }
    # ⚠️ `jobs`의 DB DEFAULT를 흉내낸다 — 우리는 이 칸들을 빼고 INSERT하므로, 가짜가 안 채우면
    #    공개한 행이 앵커 조회(`status=eq.OPEN`)에 안 걸려 그 경로가 검증되지 않는다.
    fake.defaults = {"jobs": {"status": "OPEN", "pay_period": "MONTH"}}
    return fake


@pytest.fixture
def client(server: FakePostgrest) -> PostgrestClient:
    return PostgrestClient(_SETTINGS, transport=server.transport())


@pytest.fixture
def store(client: PostgrestClient) -> SupabaseStore:
    return SupabaseStore(client)


@pytest.fixture
def jobs(client: PostgrestClient) -> SupabaseJobs:
    return SupabaseJobs(client)


def _source(external_id: str, *, on: date = _DAY) -> SourceData:
    return SourceData(
        source_key="YTUS",
        external_id=external_id,
        source_url=f"https://www.ytus.ac.kr/board/view/trXXR/{external_id}",
        title=f"공고 {external_id}",
        posted_on=on,
        run_id=uuid4(),
        fetched_at=FIXED_NOW,
        raw_text="오천중앙교회에서 부목사님을 모십니다.",
    )


def _draft(source: SourceData, **overrides: object) -> ReviewData:
    base = ReviewData(
        posted_at=source.posted_on,
        source_url=source.source_url,
        source_data_id=source.id,
        run_id=uuid4(),
        is_church_recruitment=IsChurchRecruitment.YES,
        confidence=Confidence.HIGH,
        denomination_source=DenominationSource.STATED,
        denomination=Denomination.TONGHAP,
        church_name="오천중앙교회",
        region=Region.GYEONGBUK,
        title="부목사 청빙",
        description="오천중앙교회가 부목사를 청빙합니다.",
        job_kind=(JobKind.MINISTRY,),
        position=(Position.ASSOCIATE_PASTOR,),
        department=Department.YOUTH,
        contact_email="church@example.com",
        review_status=ReviewStatus.APPROVED,
        dedup_state=DedupState.ALONE,
        dedup_key="오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:YOUTH:R1",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _seed(server: FakePostgrest, source: SourceData, draft: ReviewData) -> None:
    server.seed("source_data", to_row(source))
    server.seed("review_data", to_row(draft))


# ── 공개 (SPEC §4.3) ────────────────────────────────────────────


def test_an_approved_draft_reaches_jobs(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    source = _source("1")
    draft = _draft(source)
    _seed(server, source, draft)

    report = publish_all(store, jobs)

    assert report.published == 1
    row = server.rows["jobs"][0]
    assert row["church_name"] == "오천중앙교회"
    assert row["source"] == "OPERATOR"
    assert row["church_id"] is None
    # 링크가 초안에 적혔다 — 다음 실행이 두 번 공개하지 않는 근거다.
    assert server.rows["review_data"][0]["published_job_id"] == row["id"]


def test_the_link_is_written_before_the_insert(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 순서를 뒤집으면 "공개됐는데 우리는 모르는 행"이 남아 **매 실행 다시 공개**한다."""
    source = _source("1")
    _seed(server, source, _draft(source))
    server.requests.clear()

    publish_all(store, jobs)

    writes = [
        (request.method, request.url.path.rsplit("/", 1)[-1])
        for request in server.requests
        if request.method in {"PATCH", "POST"}
    ]
    assert writes == [("PATCH", "review_data"), ("POST", "jobs")]


def test_publishing_twice_puts_nothing_new_in_jobs(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """멱등 — 이미 나간 행은 `published_job_id`가 있어 대상에서 빠진다."""
    source = _source("1")
    _seed(server, source, _draft(source))

    first = publish_all(store, jobs)
    second = publish_all(store, jobs)

    assert (first.published, second.published) == (1, 0)
    assert len(server.rows["jobs"]) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"review_status": ReviewStatus.PENDING},
        {
            "review_status": ReviewStatus.REJECTED,
            "reject_reason": RejectReason.DUPLICATE,
            "dedup_state": DedupState.DUPLICATE,
        },
    ],
    ids=["검수 대기", "중복 거절"],
)
def test_only_approved_drafts_are_published(
    store: SupabaseStore,
    jobs: SupabaseJobs,
    server: FakePostgrest,
    overrides: dict[str, object],
) -> None:
    source = _source("1")
    _seed(server, source, _draft(source, **overrides))

    assert publish_all(store, jobs).published == 0
    assert server.rows["jobs"] == []


def test_an_unjudged_draft_is_held_back_and_counted(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 중복 판정을 안 거친 행을 내보내면 같은 자리가 여러 건 올라간다(SPEC §4.1).

    조용히 빼면 "왜 안 올라갔나"에 답할 수 없어 **세어서 알린다**.
    """
    source = _source("1")
    _seed(server, source, _draft(source, dedup_state=None, dedup_key=None))

    report = publish_all(store, jobs)

    assert (report.published, report.unjudged) == (0, 1)
    assert server.rows["jobs"] == []


def test_a_dry_run_writes_nothing(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    source = _source("1")
    _seed(server, source, _draft(source))

    report = publish_all(store, jobs, dry_run=True)

    assert report.published == 1  # 무엇이 나갈지는 보여준다
    assert server.rows["jobs"] == []
    assert server.rows["review_data"][0]["published_job_id"] is None


# ── 스키마 드리프트 (SPEC §4.3) ─────────────────────────────────


def test_a_schema_mismatch_stops_before_any_insert(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 한 건 넣고 실패하면 **절반만 공개된** 상태가 남는다."""
    source = _source("1")
    _seed(server, source, _draft(source))
    assert server.schema is not None
    server.schema["jobs"].remove("description")

    with pytest.raises(StoreError, match="description"):
        publish_all(store, jobs)
    assert server.rows["jobs"] == []


# ── 끌어올림 (SPEC §4.2b) ───────────────────────────────────────


def test_the_master_pushes_the_groups_latest_date(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """계속 올린다 = 아직 뽑고 있다 — 갱신하지 않으면 목록에서 사라진다."""
    key = "오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:YOUTH:R1"
    old_source, new_source = _source("1"), _source("2", on=date(2026, 8, 20))
    master = _draft(old_source, dedup_state=DedupState.MASTER, dedup_key=key)
    later = _draft(
        new_source,
        dedup_state=DedupState.DUPLICATE,
        dedup_key=key,
        review_status=ReviewStatus.REJECTED,
        reject_reason=RejectReason.DUPLICATE,
    )
    _seed(server, old_source, master)
    _seed(server, new_source, later)
    publish_all(store, jobs)  # 대표를 먼저 공개한다

    report = publish_all(store, jobs)

    assert report.bumped == 1
    assert server.rows["jobs"][0]["posted_at"] == "2026-08-20"


def test_the_group_date_comes_from_the_raw_record(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """묶음 날짜는 **원자료 게시일**에서 온다 — 초안의 `posted_at`은 파생값이다.

    ⚠️ 정상 상태에서는 두 값의 최댓값이 같아 결과로 구분되지 않는다. 그래서 중복 쪽 초안의
    `posted_at`만 인위로 앞당겨 **어느 쪽을 읽는지**가 값으로 드러나게 만든다. 파생값을 판정의
    입력으로 쓰면 값이 실행마다 움직일 여지가 생긴다(`DedupCandidate.posted_on` 참조).
    """
    key = "오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:YOUTH:R1"
    master_source = _source("1", on=date(2026, 8, 1))
    _seed(
        server,
        master_source,
        _draft(master_source, dedup_state=DedupState.MASTER, dedup_key=key),
    )
    publish_all(store, jobs)  # jobs.posted_at = 8/01

    # 원문은 8/10인데 초안에는 8/05로 적혀 있다 — 읽는 쪽이 갈리면 값이 갈린다.
    dup_source = _source("2", on=date(2026, 8, 10))
    _seed(
        server,
        dup_source,
        _draft(
            dup_source,
            posted_at=date(2026, 8, 5),
            dedup_state=DedupState.DUPLICATE,
            dedup_key=key,
            review_status=ReviewStatus.REJECTED,
            reject_reason=RejectReason.DUPLICATE,
        ),
    )

    assert publish_all(store, jobs).bumped == 1
    assert server.rows["jobs"][0]["posted_at"] == "2026-08-10"  # 파생값이면 08-05가 된다


def test_bumping_the_same_date_again_is_not_reported(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ `jobs`의 현재 값과 다를 때만 쓴다 — 같은 값을 매번 쓰면 리포트가 영원히 거짓말한다."""
    key = "오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:YOUTH:R1"
    old_source, new_source = _source("1"), _source("2", on=date(2026, 8, 20))
    _seed(server, old_source, _draft(old_source, dedup_state=DedupState.MASTER, dedup_key=key))
    _seed(
        server,
        new_source,
        _draft(
            new_source,
            dedup_state=DedupState.DUPLICATE,
            dedup_key=key,
            review_status=ReviewStatus.REJECTED,
            reject_reason=RejectReason.DUPLICATE,
        ),
    )
    publish_all(store, jobs)

    assert publish_all(store, jobs).bumped == 1
    assert publish_all(store, jobs).bumped == 0


def test_a_claimed_posting_is_left_to_the_church(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """교회가 claim하면 소유권이 넘어가고 크롤러는 손을 뗀다 — 실패가 아니다(SPEC §8)."""
    key = "오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:YOUTH:R1"
    old_source, new_source = _source("1"), _source("2", on=date(2026, 8, 20))
    _seed(server, old_source, _draft(old_source, dedup_state=DedupState.MASTER, dedup_key=key))
    _seed(
        server,
        new_source,
        _draft(
            new_source,
            dedup_state=DedupState.DUPLICATE,
            dedup_key=key,
            review_status=ReviewStatus.REJECTED,
            reject_reason=RejectReason.DUPLICATE,
        ),
    )
    publish_all(store, jobs)
    server.rows["jobs"][0]["church_id"] = str(uuid4())  # 교회가 가져갔다

    report = publish_all(store, jobs)

    assert (report.bumped, report.claimed) == (0, 1)
    assert server.rows["jobs"][0]["posted_at"] == _DAY.isoformat()


def test_an_uncertain_group_is_not_bumped(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 사람이 정할 자리라 대표도 묶음 범위도 확정되지 않았다."""
    key = "오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:YOUTH:R1"
    source = _source("1")
    _seed(server, source, _draft(source, dedup_state=DedupState.UNCERTAIN, dedup_key=key))
    publish_all(store, jobs)
    later = _source("2", on=date(2026, 8, 20))
    _seed(server, later, _draft(later, dedup_state=DedupState.UNCERTAIN, dedup_key=key))

    assert publish_all(store, jobs).bumped == 0


# ── 지워진 공고 복구 (SPEC §4.3) ────────────────────────────────


def test_a_deleted_job_frees_the_link(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    source = _source("1")
    _seed(server, source, _draft(source))
    publish_all(store, jobs)
    server.rows["jobs"].clear()  # 운영자가 지웠다

    report = publish_all(store, jobs)

    assert report.released == 1
    assert server.rows["review_data"][0]["published_job_id"] is None


def test_the_next_run_publishes_it_again(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """같은 실행에서 다시 넣지 않는다 — 각 단계가 저장된 사실만 보고 움직인다."""
    source = _source("1")
    _seed(server, source, _draft(source))
    publish_all(store, jobs)
    server.rows["jobs"].clear()

    freed = publish_all(store, jobs)
    again = publish_all(store, jobs)

    assert (freed.published, freed.released) == (0, 1)
    assert again.published == 1


# ── 실패 처리 (CLAUDE.md Runner 규칙) ───────────────────────────


def test_one_bad_posting_does_not_stop_the_rest(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """글 단위 격리 — 한 건이 깨져도 나머지는 나간다."""
    good, bad = _source("1"), _source("2")
    _seed(server, good, _draft(good))
    # 연락처가 없으면 `jobs`의 CHECK를 못 지나 게이트웨이가 보내기 전에 막는다.
    _seed(server, bad, _draft(bad, contact_email=None))

    report = publish_all(store, jobs)

    assert report.published == 1
    assert len(report.failed) == 1
    assert len(server.rows["jobs"]) == 1


def test_a_run_of_failures_stops_the_pass(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 권한·스키마가 깨졌으면 3,000번 실패하며 밀어붙이지 않는다."""
    for index in range(MAX_CONSECUTIVE_FAILURES + 2):
        source = _source(str(index))
        _seed(server, source, _draft(source, contact_email=None))

    with pytest.raises(StoreError, match="연속"):
        publish_all(store, jobs)


# ── dedup 이 앵커를 실제로 읽나 (배선 검증) ─────────────────────


def test_dedup_reads_anchors_and_reports_the_gauge(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 순수 `plan()`만 테스트하면 **읽어오는 배선**이 검증되지 않는다.

    실제로 `structure` 뒤 자동 dedup이 앵커를 빼먹고 있었고(2026-08-21), 그 상태에서도 순수
    함수 테스트는 전부 통과했다.
    """
    theirs = {
        "id": str(uuid4()),
        "church_name": "오천중앙교회",
        "region": Region.GYEONGBUK.value,
        "position": [Position.ASSOCIATE_PASTOR.value],
        "role": None,
        "department": Department.YOUTH.value,
        "contact_email": "church@example.com",
        "status": "OPEN",
        "posted_at": today_kst().isoformat(),
        "deadline": None,
        "church_id": None,
    }
    server.seed("jobs", theirs)
    source = _source("1", on=today_kst())
    _seed(server, source, _draft(source, posted_at=today_kst(), dedup_state=None, dedup_key=None))

    report = dedup_all(store, jobs, dry_run=False)

    assert (report.jobs_rows, report.anchors) == (1, 1)
    # 이미 공개된 자리라 우리 것은 거절된다 — 공개 패스가 또 올리지 않는다.
    assert report.count(DedupState.DUPLICATE) == 1
    assert publish_all(store, jobs).published == 0


def test_our_own_published_row_is_not_read_as_an_anchor(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 빼지 않으면 **자기 자신과 중복 판정**한다(SPEC §4.2) — 공개한 자리가 스스로 거절된다."""
    source = _source("1", on=today_kst())
    _seed(server, source, _draft(source, posted_at=today_kst()))
    publish_all(store, jobs)

    report = dedup_all(store, jobs, dry_run=False)

    assert (report.jobs_rows, report.anchors) == (1, 0)
    assert report.count(DedupState.ALONE) == 1


# ── 앵커 계기판은 dedup 쪽에 있다 ───────────────────────────────


def test_publishing_never_touches_another_partys_job(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 우리가 만들지 않은 `jobs` 행은 **읽기만** 한다(SPEC §8) — 여기서 손대는 경로가 없다."""
    theirs = {
        "id": str(uuid4()),
        "church_name": "다른교회",
        "posted_at": "2026-01-01",
        "status": "OPEN",
        "church_id": None,
    }
    server.seed("jobs", theirs)
    source = _source("1")
    _seed(server, source, _draft(source))

    publish_all(store, jobs)

    kept = next(row for row in server.rows["jobs"] if row["id"] == theirs["id"])
    assert kept["posted_at"] == "2026-01-01"


def test_nothing_to_do_asks_jobs_only_what_it_must(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """빈 원장에서도 스키마는 대조한다 — 그 다음은 쓸 일이 없다."""
    report = publish_all(store, jobs)

    assert report == PublishReport()
    assert [request.method for request in server.requests].count("POST") == 0


def test_a_published_id_that_is_not_ours_is_never_invented(
    store: SupabaseStore, jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """공개 대상은 `published_job_id`가 **비어 있는** 승인 행뿐이다."""
    source = _source("1")
    _seed(server, source, _draft(source, published_job_id=UUID(int=7)))

    report = publish_all(store, jobs)

    assert report.published == 0
    assert server.rows["jobs"] == []
