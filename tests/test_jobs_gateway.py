"""`jobs` 접근 테스트 — **공개 테이블**에 대고 도는 코드라 여기가 가장 조심스러운 자리다.

세 가지를 못 박는다:
1. **앵커 판정이 min_job DATA.md §6-1과 같은가.** 어긋나면 같은 자리가 두 번 공개되거나
   재게시가 영영 안 뜬다(SPEC §4.2의 2026-08-21 정정이 그 사례다).
2. **`jobs`에 쓰는 것이 INSERT와 `posted_at` 한 칸뿐인가.** 컬럼 단위 GRANT가 아직 없어
   (SPEC §8) 이 테스트가 유일한 방어선이다.
3. **공개 순서** — id를 `review_data`에 먼저 적고 INSERT한다(SPEC §4.3).

**네트워크를 타지 않는다** — `tests/fake_postgrest.py`가 `jobs`까지 흉내낸다.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest

from minjob_ingest.domain import (
    Confidence,
    Denomination,
    DenominationSource,
    Department,
    IsChurchRecruitment,
    JobKind,
    Position,
    Region,
    StipendPeriod,
)
from minjob_ingest.models import ReviewData
from minjob_ingest.pipeline.dedup import seat_of
from minjob_ingest.settings import SupabaseSettings
from minjob_ingest.store.base import StoreError
from minjob_ingest.store.jobs_gateway import ALWAYS_OPEN_MAX_DAYS, SupabaseJobs
from minjob_ingest.store.postgrest import PostgrestClient
from tests.fake_postgrest import FakePostgrest

TODAY: Final = date(2026, 8, 21)
FIXED_NOW: Final = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
_SETTINGS: Final = SupabaseSettings(url="https://x.supabase.co", service_role_key="k")

#: `jobs`가 실제로 가진 컬럼(min_job init.sql 43칸 중 우리가 대조하는 것들).
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
    "featured_tier",
    "featured_until",
    "posted_at",
    "deadline",
    "created_at",
    "updated_at",
}


@pytest.fixture
def server() -> FakePostgrest:
    fake = FakePostgrest()
    # review_data도 알려 준다 — 공개 링크를 적고 비우는 경로가 그 표를 만진다.
    fake.schema = {
        "jobs": set(_JOBS_COLUMNS),
        "review_data": {f.name for f in fields(ReviewData)},
    }
    return fake


@pytest.fixture
def jobs(server: FakePostgrest) -> SupabaseJobs:
    return SupabaseJobs(PostgrestClient(_SETTINGS, transport=server.transport()))


def _job(
    *,
    status: str = "OPEN",
    posted_at: date = TODAY,
    deadline: date | None = None,
    church_id: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": str(uuid4()),
        "church_id": church_id,
        "church_name": "오천중앙교회",
        "region": Region.GYEONGBUK.value,
        "position": [Position.ASSOCIATE_PASTOR.value],
        "role": None,
        "department": None,
        "status": status,
        "posted_at": posted_at.isoformat(),
        "deadline": None if deadline is None else deadline.isoformat(),
    }
    return {**row, **overrides}


def _draft(**overrides: object) -> ReviewData:
    base = ReviewData(
        posted_at=date(2026, 7, 1),
        source_url="https://www.ytus.ac.kr/board/view/trXXR/25553",
        source_data_id=uuid4(),
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
        pay_period=StipendPeriod.MONTH,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# ── 스키마 드리프트 (SPEC §4.3) ─────────────────────────────────


def test_a_matching_schema_lets_us_start(jobs: SupabaseJobs) -> None:
    jobs.check_jobs_columns()


@pytest.mark.parametrize(
    "column",
    ["housing_note", "status", "church_id", "source"],
    ids=["INSERT에 쓰는 칸", "앵커가 거르는 칸", "끌어올림 조건", "출처"],
)
def test_a_missing_column_stops_us_before_any_insert(
    jobs: SupabaseJobs, server: FakePostgrest, column: str
) -> None:
    """⚠️ 한 건 넣고 실패하면 **절반만 공개된** 상태가 남는다 — 시작 전에 멈춘다.

    ⚠️ **읽기만 하는 칸도 검사한다.** `status`가 사라지면 앵커 조회가 깨져 **공개는 되는데
    중복을 못 막는** 상태가 되고, 그게 제일 나쁘다.
    """
    assert server.schema is not None
    server.schema["jobs"].remove(column)

    with pytest.raises(StoreError, match=column):
        jobs.check_jobs_columns()


def test_an_unavailable_schema_also_stops_us(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """모양을 **모른 채로** 공개를 시작하지 않는다."""
    server.schema = None

    with pytest.raises(StoreError, match="컬럼 목록을 얻지 못했다"):
        jobs.check_jobs_columns()


# ── 앵커: min_job §6-1 미러 ─────────────────────────────────────


def test_a_posting_with_a_future_deadline_is_an_anchor(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    server.seed("jobs", _job(deadline=TODAY + timedelta(days=3)))
    assert len(jobs.visible_anchors(today=TODAY)) == 1


def test_a_posting_past_its_deadline_is_not_an_anchor(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 숨겨진 공고를 앵커로 쓰면 교회가 새 마감일로 올린 재게시가 **영영 안 뜬다**."""
    server.seed("jobs", _job(deadline=TODAY - timedelta(days=1)))
    assert jobs.visible_anchors(today=TODAY) == ()


def test_an_old_posting_with_a_live_deadline_is_still_an_anchor(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ **2026-08-21 정정의 핵심.** min_job은 마감일이 있으면 `posted_at`을 보지 않는다.

    둘을 AND로 걸면(게시일 3개월 컷) 이 공고를 앵커에서 빠뜨리고, min_job은 보여주는데 우리는
    "안 보인다"고 판단해 **같은 자리가 두 번 공개된다**.
    """
    server.seed(
        "jobs",
        _job(posted_at=TODAY - timedelta(days=200), deadline=TODAY + timedelta(days=10)),
    )
    assert len(jobs.visible_anchors(today=TODAY)) == 1


def test_the_always_open_window_mirrors_min_job() -> None:
    """⚠️ **리터럴로 못 박는다.** 이 값은 우리 취향이 아니라 min_job `ALWAYS_OPEN_MAX_DAYS`의
    미러다(DATA.md §6-1). 아래 경계 테스트들은 상수를 그대로 써서 값이 바뀌면 함께 움직이므로,
    **여기가 유일하게 드리프트를 잡는 자리**다. 그쪽이 90→120으로 바꾸면 이 테스트가 깨지고,
    그때 두 판정을 같이 맞춰야 한다.
    """
    assert ALWAYS_OPEN_MAX_DAYS == 90


def test_an_always_open_posting_lives_for_ninety_days(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    server.seed("jobs", _job(posted_at=TODAY - timedelta(days=ALWAYS_OPEN_MAX_DAYS)))
    assert len(jobs.visible_anchors(today=TODAY)) == 1


def test_an_always_open_posting_expires_after_ninety_days(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    server.seed("jobs", _job(posted_at=TODAY - timedelta(days=ALWAYS_OPEN_MAX_DAYS + 1)))
    assert jobs.visible_anchors(today=TODAY) == ()


@pytest.mark.parametrize("status", ["CLOSED", "PENDING"])
def test_only_open_postings_are_anchors(
    jobs: SupabaseJobs, server: FakePostgrest, status: str
) -> None:
    """`PENDING`은 아직 공개되지 않은 공고라 앵커가 아니다(SPEC §4.2)."""
    server.seed("jobs", _job(status=status))
    assert jobs.visible_anchors(today=TODAY) == ()


def test_our_own_published_rows_are_excluded(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """그 행은 후보에 이미 우리 초안으로 들어와 있다 — 자기 자신과 중복 판정하게 된다."""
    row = _job()
    server.seed("jobs", row)
    mine = UUID(str(row["id"]))

    assert jobs.visible_anchors(today=TODAY, exclude=frozenset({mine})) == ()


def test_an_anchor_makes_the_same_seat_as_a_draft(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ **앵커는 §4.1과 같은 키 함수를 지나야 한다.** 계산이 갈라지면 이미 공개된 자리를
    못 알아본다 — `seat_of`가 둘을 함께 받는 것이 그 보장이다."""
    server.seed("jobs", _job())
    (anchor,) = jobs.visible_anchors(today=TODAY)

    assert seat_of(anchor) == seat_of(_draft())


def test_an_unknown_enum_drops_only_that_anchor(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """묶지 않는 쪽이 안전하다 — 중복이 남는 것은 되돌릴 수 있고, 남의 자리를 덮는 것은 아니다."""
    server.seed("jobs", _job(region="ATLANTIS"), _job())

    assert len(jobs.visible_anchors(today=TODAY)) == 1


def test_a_missing_posted_at_is_a_hard_error(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """`jobs.posted_at`은 NOT NULL이다 — 비어 있으면 우리가 모르는 일이 벌어진 것이다."""
    server.seed("jobs", _job(posted_at=TODAY) | {"posted_at": None})

    with pytest.raises(StoreError, match="posted_at"):
        jobs.visible_anchors(today=TODAY)


# ── 공개 (SPEC §4.3) ────────────────────────────────────────────


def test_the_job_id_is_written_to_the_draft_before_the_insert(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ 순서를 뒤집으면 "공개됐는데 우리는 모르는 행"이 남아 **매 실행 다시 공개**한다."""
    draft = _draft()
    server.seed("review_data", {"id": str(draft.id), "published_job_id": None})

    job_id = jobs.reserve_publication(draft.id)
    jobs.publish(draft, job_id=job_id, posted_at=TODAY)

    assert server.rows["review_data"][0]["published_job_id"] == str(job_id)
    assert server.rows["jobs"][0]["id"] == str(job_id)
    assert server.methods == ["PATCH", "POST"]  # 적기 → 넣기


def test_reserving_twice_is_refused(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    draft = _draft()
    server.seed("review_data", {"id": str(draft.id), "published_job_id": None})
    jobs.reserve_publication(draft.id)

    with pytest.raises(StoreError, match="공개 자리를 잡지 못했다"):
        jobs.reserve_publication(draft.id)


def test_the_published_row_has_no_church_and_says_operator(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """크롤 공고는 `church_id=NULL`로 들어가고 교회가 claim할 때 연결된다(SPEC §8)."""
    jobs.publish(_draft(), job_id=uuid4(), posted_at=TODAY)

    row = server.rows["jobs"][0]
    assert row["church_id"] is None
    assert row["source"] == "OPERATOR"


def test_the_published_date_is_the_groups_latest_not_the_drafts(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """대표는 묶음의 최신 게시일을 쓴다(SPEC §4.1) — 초안의 값이 아니다."""
    jobs.publish(_draft(posted_at=date(2026, 7, 1)), job_id=uuid4(), posted_at=TODAY)

    assert server.rows["jobs"][0]["posted_at"] == TODAY.isoformat()


@pytest.mark.parametrize(
    ("source", "value"),
    [
        (DenominationSource.UNKNOWN, Denomination.UNKNOWN),
        (DenominationSource.AI_GUESS, Denomination.TONGHAP),
    ],
    ids=["UNKNOWN은 jobs CHECK가 거부한다", "ai_guess는 확정이 아니다"],
)
def test_a_denomination_we_cannot_publish_goes_out_as_null(
    jobs: SupabaseJobs,
    server: FakePostgrest,
    source: DenominationSource,
    value: Denomination,
) -> None:
    jobs.publish(
        _draft(denomination_source=source, denomination=value), job_id=uuid4(), posted_at=TODAY
    )

    assert server.rows["jobs"][0]["denomination"] is None


def test_an_unknown_pay_period_is_left_to_the_database_default(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """⚠️ NULL을 보내면 NOT NULL이 거부하고, 우리가 'MONTH'를 지어 넣으면 **연봉이 월급으로
    공개된다**. 칸을 아예 보내지 않는다."""
    jobs.publish(_draft(pay_period=None), job_id=uuid4(), posted_at=TODAY)

    body = json.loads(server.requests[-1].content)
    assert "pay_period" not in body[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"church_name": None},
        {"title": None},
        {"description": None},
    ],
    ids=["교회명", "제목", "요약"],
)
def test_a_blank_required_column_is_refused_before_sending(
    jobs: SupabaseJobs, server: FakePostgrest, overrides: dict[str, object]
) -> None:
    """비어 있으면 **공개 테이블에 대고** NOT NULL 위반이 난다 — 보내기 전에 멈춘다."""
    with pytest.raises(StoreError, match="필요한 칸이 비었다"):
        jobs.publish(_draft(**overrides), job_id=uuid4(), posted_at=TODAY)
    assert server.requests == []


def test_a_draft_without_a_seat_kind_is_refused(jobs: SupabaseJobs) -> None:
    with pytest.raises(StoreError, match="job_kind"):
        jobs.publish(_draft(job_kind=(), position=()), job_id=uuid4(), posted_at=TODAY)


def test_a_draft_nobody_can_apply_to_is_refused(jobs: SupabaseJobs) -> None:
    """min_job `jobs_needs_contact` — "어디로 지원하나"를 알 수 없으면 공개할 값이 없다."""
    with pytest.raises(StoreError, match="지원 연락처"):
        jobs.publish(_draft(contact_email=None), job_id=uuid4(), posted_at=TODAY)


# ── 끌어올림 (SPEC §4.2b) ───────────────────────────────────────


def test_only_posted_at_is_ever_updated(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """⚠️ **컬럼 단위 GRANT가 아직 없다**(SPEC §8) — 이 테스트가 유일한 방어선이다.

    크롤러가 제목·연락처·마감일·상태를 덮는 길이 코드에 없어야 한다.
    """
    row = _job()
    server.seed("jobs", row)

    assert jobs.bump_posted_at(UUID(str(row["id"])), TODAY) is True

    body = json.loads(server.requests[-1].content)
    assert set(body) == {"posted_at"}
    assert server.rows["jobs"][0]["posted_at"] == TODAY.isoformat()


def test_a_claimed_posting_is_left_alone(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """교회가 claim하면 소유권이 넘어가고 크롤러는 손을 뗀다 — 실패가 아니라 정상적인 결말이다."""
    row = _job(church_id=str(uuid4()), posted_at=date(2026, 1, 1))
    server.seed("jobs", row)

    assert jobs.bump_posted_at(UUID(str(row["id"])), TODAY) is False
    assert server.rows["jobs"][0]["posted_at"] == "2026-01-01"


# ── 소멸 감지: 내리기 (SPEC §4 gone 단계) ───────────────────────


def test_closing_sends_only_the_status_column(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """`bump_posted_at`과 같은 규율 — 제목·연락처·마감일을 덮는 길이 코드에 없어야 한다."""
    row = _job()
    server.seed("jobs", row)

    assert jobs.close_job(UUID(str(row["id"]))) is True

    body = json.loads(server.requests[-1].content)
    assert set(body) == {"status"}
    assert server.rows["jobs"][0]["status"] == "CLOSED"


def test_a_claimed_posting_is_never_closed(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """교회 것이 된 공고는 원문이 사라져도 그 교회가 정리한다(§8) — 크롤러는 손을 뗀다."""
    row = _job(church_id=str(uuid4()))
    server.seed("jobs", row)

    assert jobs.close_job(UUID(str(row["id"]))) is False
    assert server.rows["jobs"][0]["status"] == "OPEN"


def test_closing_an_already_closed_job_is_a_quiet_no(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """운영자가 먼저 내렸어도 실패가 아니다 — 관측 기록(`source_gone_at`)은 그대로 남는다."""
    row = _job(status="CLOSED")
    server.seed("jobs", row)

    assert jobs.close_job(UUID(str(row["id"]))) is False


# ── 지워진 공고 복구 (SPEC §4.3) ────────────────────────────────


def test_the_state_answers_both_questions_in_one_read(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    """키가 없으면 지워진 것 · 날짜가 다르면 끌어올릴 것 — 읽기 한 번이 둘 다 답한다."""
    alive = _job(posted_at=date(2026, 8, 1))
    server.seed("jobs", alive)
    gone = uuid4()

    state = jobs.published_state([UUID(str(alive["id"])), gone])

    assert state == {UUID(str(alive["id"])): date(2026, 8, 1)}
    assert gone not in state  # 운영자가 지웠다


def test_asking_about_nothing_asks_the_server_nothing(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    assert jobs.published_state([]) == {}
    assert server.requests == []


def test_counting_rows_needs_no_column(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """⚠️ 앵커 0건은 정상일 수도 있다 — `N행 중 0건`이라야 이상함이 드러난다."""
    server.seed("jobs", _job(), _job())

    assert jobs.count_jobs() == 2
    assert "select" not in server.params[-1]


def test_a_dead_link_is_cleared_so_the_next_run_republishes(
    jobs: SupabaseJobs, server: FakePostgrest
) -> None:
    draft_id, job_id = uuid4(), uuid4()
    server.seed("review_data", {"id": str(draft_id), "published_job_id": str(job_id)})

    jobs.release_publication(draft_id, job_id)

    assert server.rows["review_data"][0]["published_job_id"] is None


def test_a_link_that_changed_is_not_cleared(jobs: SupabaseJobs, server: FakePostgrest) -> None:
    """우리가 적어둔 값일 때만 비운다 — 그 사이 다른 값이 들어왔으면 손대지 않는다."""
    draft_id, other = uuid4(), uuid4()
    server.seed("review_data", {"id": str(draft_id), "published_job_id": str(other)})

    jobs.release_publication(draft_id, uuid4())

    assert server.rows["review_data"][0]["published_job_id"] == str(other)
