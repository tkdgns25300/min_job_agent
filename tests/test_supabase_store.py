"""`SupabaseStore` 계약 테스트 — `JsonStore`와 **같은 약속**을 지키는지.

이 파일이 저장소 스왑의 안전장치다. `JsonStore` 61개 테스트가 계약을 이미 글로 적어 놨으므로,
같은 행동을 여기서 다시 확인하면 **파이프라인 코드를 고치지 않아도 된다**는 것이 증명된다.

**네트워크를 타지 않는다** — `tests/fake_postgrest.py`가 메모리 위에서 PostgREST를 흉내낸다.
가짜는 모르는 문법에 예외를 던지므로, 우리가 실제로 보내는 요청만 통과한다.
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
    CrawlMode,
    DedupState,
    Denomination,
    DenominationSource,
    IsChurchRecruitment,
    RejectReason,
    ReviewStatus,
    SourceHealthStatus,
)
from minjob_ingest.models import (
    MAX_STRUCTURE_ATTEMPTS,
    CrawlRun,
    ReviewData,
    SourceData,
    SourceHealth,
)
from minjob_ingest.settings import SupabaseSettings
from minjob_ingest.store.base import DedupUpdate, DedupVerdict, StoreError
from minjob_ingest.store.guards import (
    DEDUP_LABEL_FIELDS,
    DEDUP_VERDICT_FIELDS,
    MUTABLE_STATE_FIELDS,
    with_dedup,
)
from minjob_ingest.store.postgrest import PostgrestClient
from minjob_ingest.store.serde import to_row
from minjob_ingest.store.supabase_store import SupabaseStore
from tests.fake_postgrest import FakePostgrest

FIXED_NOW: Final = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
_SETTINGS: Final = SupabaseSettings(url="https://x.supabase.co", service_role_key="k")
_REVIEW_DATA_TABLE: Final = "review_data"


@pytest.fixture
def server() -> FakePostgrest:
    fake = FakePostgrest()
    # ⚠️ 표 모양을 알려 주면 가짜가 **없는 컬럼 참조를 거부**한다. 안 켜면 `order=id`처럼
    #    실 서버에서만 400이 나는 실수가 테스트를 통과한다(2026-08-21 실측).
    fake.schema = {
        "source_data": {f.name for f in fields(SourceData)},
        "review_data": {f.name for f in fields(ReviewData)},
        "source_health": {f.name for f in fields(SourceHealth)},
        "crawl_run": {f.name for f in fields(CrawlRun)},
    }
    return fake


@pytest.fixture
def store(server: FakePostgrest) -> SupabaseStore:
    return SupabaseStore(PostgrestClient(_SETTINGS, transport=server.transport()))


@pytest.fixture
def corruption_log() -> list[str]:
    return []


@pytest.fixture
def lenient_store(server: FakePostgrest, corruption_log: list[str]) -> SupabaseStore:
    return SupabaseStore(
        PostgrestClient(_SETTINGS, transport=server.transport()),
        on_corrupt_row=lambda table, err: corruption_log.append(f"{table}: {err}"),
    )


def _source_data(external_id: str = "25553", **overrides: object) -> SourceData:
    base = SourceData(
        source_key="YTUS",
        external_id=external_id,
        source_url=f"https://www.ytus.ac.kr/board/view/trXXR/{external_id}",
        title=f"공고 {external_id}",
        posted_on=FIXED_NOW.date(),
        run_id=uuid4(),
        fetched_at=FIXED_NOW,
        raw_text="오천중앙교회에서 부목사님을 모십니다.",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _review_data(source_data_id: UUID, **overrides: object) -> ReviewData:
    base = ReviewData(
        posted_at=FIXED_NOW.date(),
        source_url="https://www.ytus.ac.kr/board/view/trXXR/25553",
        source_data_id=source_data_id,
        run_id=uuid4(),
        is_church_recruitment=IsChurchRecruitment.YES,
        confidence=Confidence.HIGH,
        denomination_source=DenominationSource.STATED,
        denomination=Denomination.TONGHAP,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _health(**overrides: object) -> SourceHealth:
    base = SourceHealth(
        source_key="YTUS",
        first_run_at=FIXED_NOW,
        last_run_at=FIXED_NOW,
        last_status=SourceHealthStatus.OK,
        last_success_at=FIXED_NOW,
        last_rows=12,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# ── 원장·수집 ──────────────────────────────────────────────────


def test_a_new_posting_is_stored_once(store: SupabaseStore, server: FakePostgrest) -> None:
    record = _source_data()
    assert store.save_source_data(record) is True
    assert store.save_source_data(record) is False  # ON CONFLICT DO NOTHING
    assert len(server.rows["source_data"]) == 1


def test_the_same_number_from_another_board_is_a_different_posting(store: SupabaseStore) -> None:
    """원장 키는 `(source_key, external_id)` 둘이다 — 번호만 같으면 다른 공고다."""
    assert store.save_source_data(_source_data("1")) is True
    assert store.save_source_data(replace(_source_data("1"), source_key="PUTS")) is True


def test_seen_postings_answers_with_the_callers_own_strings(store: SupabaseStore) -> None:
    """호출자가 자기 목록을 이 결과로 걸러내므로 **넘긴 문자열 그대로** 돌려줘야 한다."""
    store.save_source_data(_source_data("25553"))

    seen = store.seen_postings("YTUS", [" 25553 ", "99999"])

    assert list(seen) == [" 25553 "]
    assert seen[" 25553 "].title == "공고 25553"
    assert seen[" 25553 "].posted_on == FIXED_NOW.date()


def test_seen_postings_normalizes_the_source_key(store: SupabaseStore) -> None:
    """조회가 저장과 같은 정규화를 거쳐야 한다 — 빗나가면 매 실행 상세를 다시 요청한다."""
    store.save_source_data(_source_data("7"))
    # ⚠️ 공백이 붙은 채로 넘긴다 — 미리 다듬으면 정규화를 요구하지 않는 테스트가 된다.
    assert list(store.seen_postings(" YTUS ", ["7"])) == ["7"]


def test_seen_postings_asks_nothing_when_the_page_is_empty(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    assert store.seen_postings("YTUS", []) == {}
    assert server.requests == []


# ── 구조화 ─────────────────────────────────────────────────────


def test_only_unjudged_postings_under_the_cap_are_listed(store: SupabaseStore) -> None:
    store.save_source_data(_source_data("fresh"))
    store.save_source_data(_source_data("judged", structured_at=FIXED_NOW))
    store.save_source_data(_source_data("exhausted", structure_attempts=MAX_STRUCTURE_ATTEMPTS))

    listed = store.list_unstructured(10)

    assert [record.external_id for record in listed] == ["fresh"]


def test_the_oldest_fetch_comes_first(store: SupabaseStore) -> None:
    """`limit`이 있으므로 정렬이 틀리면 표본이 엉뚱한 것만 담긴다."""
    store.save_source_data(_source_data("late", fetched_at=FIXED_NOW))
    store.save_source_data(_source_data("early", fetched_at=datetime(2026, 1, 1, tzinfo=UTC)))

    assert [record.external_id for record in store.list_unstructured(1)] == ["early"]


def test_one_board_can_be_asked_for_on_its_own(store: SupabaseStore) -> None:
    """⚠️ 필터가 저장소에 있어야 `limit`이 "그 게시판에서 N건"이 된다."""
    store.save_source_data(_source_data("a"))
    store.save_source_data(replace(_source_data("b"), source_key="PUTS"))

    assert [r.source_key for r in store.list_unstructured(10, source_key="puts".upper())] == [
        "PUTS"
    ]


def test_a_limit_of_zero_is_refused(store: SupabaseStore) -> None:
    with pytest.raises(ValueError, match="1 이상"):
        store.list_unstructured(0)


def test_the_verdict_is_recorded_without_touching_the_evidence(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    record = _source_data()
    store.save_source_data(record)

    store.update_structure_state(record.with_verdict_recorded(FIXED_NOW))

    stored = server.rows["source_data"][0]
    assert stored["structured_at"] is not None
    assert stored["raw_text"] == record.raw_text  # 증거는 그대로다


def test_only_the_three_state_columns_are_sent(store: SupabaseStore, server: FakePostgrest) -> None:
    """⚠️ 증거 칸을 본문에 담으면 write-once가 **물리적으로** 뚫린다.

    위의 `guards` 검사는 "낡은 레코드로 부르는 버그"를 요란하게 만드는 역할이고, 실제 방어선은
    **보내는 컬럼을 세 개로 좁히는 것**이다. 그 의도를 여기서 못 박는다.
    """
    record = _source_data()
    store.save_source_data(record)
    server.requests.clear()

    store.update_structure_state(record.with_verdict_recorded(FIXED_NOW))

    patch = next(request for request in server.requests if request.method == "PATCH")
    assert set(json.loads(patch.content)) == set(MUTABLE_STATE_FIELDS)


def test_updating_a_missing_posting_is_refused(store: SupabaseStore) -> None:
    with pytest.raises(StoreError, match="없음"):
        store.update_structure_state(_source_data())


def test_changing_the_evidence_through_the_state_path_is_refused(store: SupabaseStore) -> None:
    """write-once — 갱신 경로로 원문을 바꾸려 하면 구현이 막는다(`guards`)."""
    record = _source_data()
    store.save_source_data(record)

    with pytest.raises(StoreError, match="원문 증거"):
        store.update_structure_state(replace(record, raw_text="바뀐 본문"))


def test_erasing_a_recorded_verdict_is_refused(store: SupabaseStore) -> None:
    """낡은 레코드로 부르면 판정이 지워져 Gemini에 재과금된다."""
    record = _source_data()
    store.save_source_data(record)
    store.update_structure_state(record.with_verdict_recorded(FIXED_NOW))

    with pytest.raises(StoreError, match="지울 수 없음"):
        store.update_structure_state(record)


def test_lowering_the_attempt_count_is_refused(store: SupabaseStore) -> None:
    record = _source_data()
    store.save_source_data(record)
    store.update_structure_state(record.with_failed_attempt("429"))

    with pytest.raises(StoreError, match="줄일 수 없음"):
        store.update_structure_state(record)


# ── 초안 ───────────────────────────────────────────────────────


def test_a_draft_is_written_once_per_posting(store: SupabaseStore, server: FakePostgrest) -> None:
    source = _source_data()
    store.save_source_data(source)

    assert store.upsert_review_data(_review_data(source.id)) is True
    assert store.upsert_review_data(_review_data(source.id)) is True  # 교체
    assert len(server.rows["review_data"]) == 1


def test_a_redraft_keeps_the_identity_and_the_publication_link(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """⚠️ `published_job_id`가 끊기면 이미 공개한 공고를 한 번 더 승격한다(SPEC §4.2)."""
    source = _source_data()
    store.save_source_data(source)
    published = uuid4()
    first = _review_data(source.id, published_job_id=published)
    store.upsert_review_data(first)

    store.upsert_review_data(_review_data(source.id, title="다시 구조화한 제목"))

    stored = server.rows["review_data"][0]
    assert stored["id"] == str(first.id)
    assert stored["published_job_id"] == str(published)
    assert stored["title"] == "다시 구조화한 제목"


def test_an_operator_touched_draft_is_never_overwritten(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(_review_data(source.id, reviewed_by="operator@minjob"))

    assert store.upsert_review_data(_review_data(source.id, title="AI 초안")) is False
    assert server.rows["review_data"][0]["reviewed_by"] == "operator@minjob"


def test_an_approval_between_our_read_and_write_wins(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """⚠️ `JsonStore`에는 없는 방어선 — 조건을 필터에 넣어 **DB가 판정**한다.

    admin이 읽은 뒤 승인해도 우리가 덮어쓰지 않는다. 필터가 어긋나 0행이 돌아오고 우리는 버린다.
    """
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(_review_data(source.id))
    # 우리가 읽은 다음 admin이 승인한 상황을 만든다. ⚠️ 등급도 함께 내린다 — 자동 승인은
    # `high`에서만 일어나므로, `high`인 채 `APPROVED`면 그건 **우리가** 승인한 행이다.
    server.rows["review_data"][0]["review_status"] = ReviewStatus.APPROVED.value
    server.rows["review_data"][0]["confidence"] = Confidence.MEDIUM.value

    assert store.upsert_review_data(_review_data(source.id, title="덮어쓰려는 초안")) is False
    assert server.rows["review_data"][0]["title"] != "덮어쓰려는 초안"


def test_an_approval_right_after_our_read_still_wins(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """⚠️ **이것이 조건부 쓰기가 막는 유일한 경우다.**

    읽기 **전에** 승인됐으면 `is_safe_to_replace`가 먼저 걸러낸다. 읽은 **직후** 승인되면
    그 검사는 이미 지나갔고, 필터에 조건이 없으면 우리가 승인을 덮어쓴다. `JsonStore`는 락으로
    막았지만 여기서는 DB가 판정한다.
    """
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(_review_data(source.id))

    def approve_once(table: str) -> None:
        if table == _REVIEW_DATA_TABLE:
            server.rows[_REVIEW_DATA_TABLE][0]["review_status"] = ReviewStatus.APPROVED.value
            server.after_read = None

    server.after_read = approve_once

    assert store.upsert_review_data(_review_data(source.id, title="덮어쓰려는 초안")) is False
    assert server.rows[_REVIEW_DATA_TABLE][0]["title"] != "덮어쓰려는 초안"


def test_publishing_right_after_our_read_keeps_the_link(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """⚠️ **admin이 아니라 우리 다음 단계가 만드는 경쟁이다**(2026-08-23에 생겼다).

    자동 승인 행은 게재 링크가 없을 때만 되돌릴 수 있게 좁혔는데(`is_safe_to_replace`), 그러면
    우리가 읽은 뒤 `publish`가 링크를 붙이는 사이가 생긴다 — 그때 덮어쓰면 **방금 공개한 공고를
    가리키는 링크가 사라지고** 다음 실행이 같은 공고를 한 번 더 공개한다.
    """
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(_review_data(source.id, review_status=ReviewStatus.APPROVED))
    published = str(uuid4())

    def publish_once(table: str) -> None:
        if table == _REVIEW_DATA_TABLE:
            server.rows[_REVIEW_DATA_TABLE][0]["published_job_id"] = published
            server.after_read = None

    server.after_read = publish_once

    assert store.upsert_review_data(_review_data(source.id, title="덮어쓰려는 초안")) is False
    assert server.rows[_REVIEW_DATA_TABLE][0]["published_job_id"] == published


# ── 되돌리기 ───────────────────────────────────────────────────


def test_requeue_clears_the_verdict_and_drops_the_draft(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(_review_data(source.id))
    store.update_structure_state(source.with_verdict_recorded(FIXED_NOW))

    result = store.requeue_for_structure()

    assert result.requeued == 1
    assert server.rows["source_data"][0]["structured_at"] is None
    assert server.rows["source_data"][0]["structure_attempts"] == 0
    assert server.rows["review_data"] == []


def test_requeue_clears_the_verdict_before_dropping_the_draft(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """⚠️ 순서가 뒤집히면 중간에 끊길 때 **"판정 완료 + 초안 없음"** 이 남는다.

    SPEC §4는 그 상태를 재시도 기준으로 쓰지 않으므로 사후 탐지가 불가능하다. 트랜잭션이 없어
    이 순서가 유일한 방어다.
    """
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(_review_data(source.id))
    store.update_structure_state(source.with_verdict_recorded(FIXED_NOW))
    server.requests.clear()

    store.requeue_for_structure()

    writes = [method for method in server.methods if method in {"PATCH", "DELETE"}]
    assert writes == ["PATCH", "DELETE"]


def test_requeue_keeps_what_the_operator_touched(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(_review_data(source.id, reviewed_by="operator@minjob"))
    store.update_structure_state(source.with_verdict_recorded(FIXED_NOW))

    result = store.requeue_for_structure()

    assert result.requeued == 0
    assert result.skipped == ("YTUS/25553",)
    assert server.rows["source_data"][0]["structured_at"] is not None
    assert len(server.rows["review_data"]) == 1


def test_requeue_undoes_a_crawler_rejection(store: SupabaseStore) -> None:
    """이단·마감·중복 규칙은 계속 바뀐다 — 그 판정은 되돌아와야 한다(2026-08-19)."""
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(
        _review_data(
            source.id,
            review_status=ReviewStatus.REJECTED,
            reject_reason=RejectReason.HERESY,
            heresy_flag=True,
            heresy_evidence="heresy-ref: 이름+지역 일치",
        )
    )
    store.update_structure_state(source.with_verdict_recorded(FIXED_NOW))

    assert store.requeue_for_structure().requeued == 1


def test_requeue_can_be_limited_to_one_board(store: SupabaseStore) -> None:
    mine, other = _source_data("a"), replace(_source_data("b"), source_key="PUTS")
    for record in (mine, other):
        store.save_source_data(record)
        store.update_structure_state(record.with_verdict_recorded(FIXED_NOW))

    assert store.requeue_for_structure(source_key="PUTS").requeued == 1


def test_requeue_can_be_narrowed_to_single_postings(store: SupabaseStore) -> None:
    """⚠️ 서버가 걸러야 한다 — 전부 받아 여기서 고르면 되돌릴 것이 아닌 행도 지운다."""
    for external_id in ("1", "2", "3"):
        record = _source_data(external_id)
        store.save_source_data(record)
        store.update_structure_state(record.with_verdict_recorded(FIXED_NOW))

    result = store.requeue_for_structure(source_key="YTUS", external_ids=("1", "3"))

    assert result.requeued == 2
    assert sorted(row.external_id for row in store.list_unstructured(10)) == ["1", "3"]


def test_requeue_by_posting_needs_the_board(store: SupabaseStore) -> None:
    """⚠️ 두 구현이 같은 규칙을 지켜야 한다 — 한쪽만 막으면 저장소를 바꿀 때 조용히 깨진다."""
    with pytest.raises(ValueError, match="source_key"):
        store.requeue_for_structure(external_ids=("1",))


def test_requeue_stops_when_a_draft_cannot_be_read(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """⚠️ 승인된 행인지 알 수 없는데 지우면 되돌릴 방법이 없다."""
    source = _source_data()
    store.save_source_data(source)
    store.update_structure_state(source.with_verdict_recorded(FIXED_NOW))
    server.seed("review_data", {"id": str(uuid4()), "source_data_id": str(source.id)})

    with pytest.raises(StoreError, match="읽을 수 없는 초안"):
        store.requeue_for_structure()


# ── 중복 판정 ──────────────────────────────────────────────────


def test_candidates_carry_the_posting_date_from_the_raw_record(store: SupabaseStore) -> None:
    """⚠️ 날짜를 초안이 아니라 **원자료**에서 가져온다 — 대표의 `posted_at`은 덮어써진다."""
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(_review_data(source.id, posted_at=date(2026, 1, 1)))

    (candidate,) = store.dedup_candidates()

    assert candidate.posted_on == FIXED_NOW.date()


def test_a_draft_without_its_raw_record_stops_dedup(store: SupabaseStore) -> None:
    source = _source_data()
    store.save_source_data(source)
    store.upsert_review_data(_review_data(uuid4()))

    with pytest.raises(StoreError, match="원자료가 없다"):
        store.dedup_candidates()


def test_a_broken_draft_stops_dedup_instead_of_being_skipped(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """건너뛰면 그 행이 대표였을 때 **대표가 아닌 쪽이 공개**되고 표시가 남지 않는다."""
    server.seed("review_data", {"id": str(uuid4())})

    with pytest.raises(StoreError, match="읽을 수 없는 초안"):
        store.dedup_candidates()


def test_a_verdict_is_applied_and_counted_once(store: SupabaseStore, server: FakePostgrest) -> None:
    source = _source_data()
    store.save_source_data(source)
    draft = _review_data(source.id)
    store.upsert_review_data(draft)
    update = DedupUpdate(
        review_data_id=draft.id,
        dedup_key="오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:-:R1",
        dedup_state=DedupState.DUPLICATE,
        verdict=DedupVerdict(
            review_status=ReviewStatus.REJECTED,
            reject_reason=RejectReason.DUPLICATE,
            posted_at=FIXED_NOW.date(),
        ),
    )

    assert store.apply_dedup([update]) == 1
    assert store.apply_dedup([update]) == 0  # 값이 같은 행은 세지 않는다(멱등)
    stored = server.rows["review_data"][0]
    assert stored["dedup_state"] == "DUPLICATE"
    assert stored["reject_reason"] == "DUPLICATE"


def test_a_label_only_update_leaves_the_verdict_alone(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    source = _source_data()
    store.save_source_data(source)
    draft = _review_data(source.id)
    store.upsert_review_data(draft)

    store.apply_dedup(
        [DedupUpdate(review_data_id=draft.id, dedup_key="k", dedup_state=DedupState.ALONE)]
    )

    stored = server.rows["review_data"][0]
    assert stored["dedup_key"] == "k"
    assert stored["review_status"] == ReviewStatus.PENDING.value


def test_applying_a_verdict_to_a_missing_draft_is_refused(store: SupabaseStore) -> None:
    """조용히 넘기면 판정이 사라진 것을 아무도 모른다."""
    with pytest.raises(StoreError, match="초안이 없어"):
        store.apply_dedup(
            [DedupUpdate(review_data_id=uuid4(), dedup_key="k", dedup_state=DedupState.ALONE)]
        )


def test_applying_nothing_asks_the_server_nothing(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    assert store.apply_dedup([]) == 0
    assert server.requests == []


# ── 실행·상태 ──────────────────────────────────────────────────


def test_a_run_is_opened_and_closed(store: SupabaseStore, server: FakePostgrest) -> None:
    run = store.start_run(CrawlMode.DAILY)
    # ⚠️ 시작 시각은 `kst_now()`가 만든다(레코드 계약) — 종료는 그보다 뒤여야 한다.
    store.finish_run(replace(run, finished_at=run.started_at, sources_ok=30, new_count=7))

    stored = server.rows["crawl_run"][0]
    assert stored["id"] == str(run.id)  # 하위 레코드가 참조하는 id는 우리가 만든다
    assert stored["finished_at"] is not None
    assert stored["new_count"] == 7


def test_finishing_a_run_that_never_started_is_refused(store: SupabaseStore) -> None:
    """조용히 넘기면 실행이 영구 "진행중"으로 남아 대시보드가 거짓말을 한다."""
    with pytest.raises(StoreError, match="시작 기록 없이"):
        store.finish_run(CrawlRun(mode=CrawlMode.DAILY, started_at=FIXED_NOW))


def test_health_is_written_then_replaced(store: SupabaseStore, server: FakePostgrest) -> None:
    store.upsert_health(_health())
    store.upsert_health(_health(last_rows=99, last_new_count=3))

    assert len(server.rows["source_health"]) == 1
    assert server.rows["source_health"][0]["last_rows"] == 99


def test_health_lookup_normalizes_the_key(store: SupabaseStore) -> None:
    """빗나가면 누적 카운터가 매 실행 초기화돼 §7 경보가 영구히 울리지 않는다."""
    store.upsert_health(_health())
    found = store.get_health(" YTUS ")

    assert found is not None
    assert found.last_rows == 12


def test_health_of_an_unseen_board_is_none(store: SupabaseStore) -> None:
    assert store.get_health("PUTS") is None


def test_a_broken_health_row_is_not_swallowed(store: SupabaseStore, server: FakePostgrest) -> None:
    """`None`으로 삼키면 누적 카운터가 초기화돼 경보가 죽는다 — 그대로 던진다."""
    server.seed("source_health", {"source_key": "YTUS"})

    with pytest.raises(Exception, match="컬럼"):
        store.get_health("YTUS")


# ── 손상 행 정책 ───────────────────────────────────────────────


def test_a_broken_row_is_skipped_in_batch_reads(
    lenient_store: SupabaseStore, server: FakePostgrest, corruption_log: list[str]
) -> None:
    """배치 읽기는 행 단위로 격리한다 — 한 행 때문에 원장을 잃고 31곳을 다시 긁지 않는다."""
    good = _source_data("good")
    # ⚠️ 손상 행도 **필터를 통과해야** 디코더에 닿는다. 필수 칸이 아예 빈 행은 서버가 먼저
    #    걸러내므로(실제 DB에서는 NOT NULL이라 존재조차 못 한다), 필터 칸은 갖추고 값이
    #    깨진 행으로 만든다.
    broken = {**to_row(_source_data("broken")), "posted_on": "어제"}
    server.seed("source_data", to_row(good), broken)

    listed = lenient_store.list_unstructured(10)

    assert [record.external_id for record in listed] == ["good"]
    assert len(corruption_log) == 1


# ── 판정은 바뀌는 칸만 쓴다 (2026-08-22 · REVIEW_PAGE §6.5) ──────


def test_a_label_only_update_sends_only_the_two_label_columns(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """⚠️ 행 전체를 되쓰면 읽고 쓰는 사이 **검수자가 고친 값이 덮인다.**

    검수 화면이 값 교정을 허용하기로 한 뒤로는 실제 위험이다(min_job admin).
    """
    source = _source_data()
    store.save_source_data(source)
    draft = _review_data(source.id)
    store.upsert_review_data(draft)
    server.requests.clear()

    store.apply_dedup(
        [DedupUpdate(review_data_id=draft.id, dedup_key="k", dedup_state=DedupState.ALONE)]
    )

    assert _patched_columns(server) == {"dedup_key", "dedup_state"}


def test_a_verdict_update_sends_the_label_and_the_verdict_only(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    source = _source_data()
    store.save_source_data(source)
    draft = _review_data(source.id)
    store.upsert_review_data(draft)
    server.requests.clear()

    store.apply_dedup(
        [
            DedupUpdate(
                review_data_id=draft.id,
                dedup_key="k",
                dedup_state=DedupState.DUPLICATE,
                verdict=DedupVerdict(
                    review_status=ReviewStatus.REJECTED,
                    reject_reason=RejectReason.DUPLICATE,
                    posted_at=FIXED_NOW.date(),
                ),
            )
        ]
    )

    assert _patched_columns(server) == {
        "dedup_key",
        "dedup_state",
        "review_status",
        "reject_reason",
        "posted_at",
    }


def test_an_edit_made_between_our_read_and_our_write_survives(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """⚠️ 이게 이 변경의 목적이다 — 검수자가 고친 교회명이 판정에 덮이지 않는다."""
    source = _source_data()
    store.save_source_data(source)
    draft = _review_data(source.id)
    store.upsert_review_data(draft)
    # 우리가 읽은 뒤 admin이 고친 상황을 흉내낸다.
    server.rows["review_data"][0]["church_name"] = "운영자가 고친 이름"

    store.apply_dedup(
        [DedupUpdate(review_data_id=draft.id, dedup_key="k", dedup_state=DedupState.ALONE)]
    )

    stored = server.rows["review_data"][0]
    assert stored["church_name"] == "운영자가 고친 이름"
    assert stored["dedup_key"] == "k"


def test_the_columns_we_send_are_exactly_the_ones_the_rule_changes() -> None:
    """⚠️ **드리프트 테스트.** `with_dedup`이 칸을 하나 더 바꾸게 되면 그 값이 조용히 안 실린다.

    이름 목록을 손으로 적어 두는 대신 **규칙이 실제로 바꾼 것**과 대조한다.
    """
    stored = _review_data(uuid4())
    label_only = DedupUpdate(review_data_id=stored.id, dedup_key="k", dedup_state=DedupState.ALONE)
    with_verdict = DedupUpdate(
        review_data_id=stored.id,
        dedup_key="k",
        dedup_state=DedupState.DUPLICATE,
        verdict=DedupVerdict(
            review_status=ReviewStatus.REJECTED,
            reject_reason=RejectReason.DUPLICATE,
            posted_at=FIXED_NOW.date() - timedelta(days=1),
        ),
    )

    assert _changed_fields(stored, with_dedup(stored, label_only)) == set(DEDUP_LABEL_FIELDS)
    assert _changed_fields(stored, with_dedup(stored, with_verdict)) == set(
        DEDUP_LABEL_FIELDS
    ) | set(DEDUP_VERDICT_FIELDS)


def _patched_columns(server: FakePostgrest) -> set[str]:
    """`PATCH` 한 번이 보낸 컬럼 이름. ⚠️ 두 번 이상이면 그 자체가 버그다(행 하나에 요청 하나)."""
    patches = [request for request in server.requests if request.method == "PATCH"]
    assert len(patches) == 1, f"PATCH가 {len(patches)}번 나갔다"
    body: object = json.loads(patches[0].content)
    assert isinstance(body, dict)
    return set(body)


def _changed_fields(before: ReviewData, after: ReviewData) -> set[str]:
    return {f.name for f in fields(ReviewData) if getattr(before, f.name) != getattr(after, f.name)}


# ── status 조회 (SPEC §7) ──────────────────────────────────────


def test_recent_runs_come_newest_first(store: SupabaseStore, server: FakePostgrest) -> None:
    """⚠️ 순서가 계약이다 — 데일리 창 계산이 **앞에서부터** 성공한 실행을 찾는다."""
    older = CrawlRun(mode=CrawlMode.BACKFILL, started_at=FIXED_NOW - timedelta(days=2))
    newer = CrawlRun(mode=CrawlMode.DAILY, started_at=FIXED_NOW)
    # ⚠️ 저장 순서를 **거꾸로** 넣는다 — 정렬이 없으면 통과하는 테스트가 되지 않게.
    server.seed("crawl_run", to_row(older), to_row(newer))

    got = store.recent_runs(10)

    assert [run.id for run in got] == [newer.id, older.id]


def test_recent_runs_refuses_a_useless_limit(store: SupabaseStore) -> None:
    with pytest.raises(ValueError, match="1 이상"):
        store.recent_runs(0)


def test_all_health_returns_every_board(store: SupabaseStore) -> None:
    """문제 있는 곳만 걸러 오지 않는다 — 무엇이 문제인지는 `pipeline.health`가 정한다."""
    for key in ("YTUS", "PUTS"):
        store.upsert_health(_health(source_key=key))

    assert sorted(item.source_key for item in store.all_health()) == ["PUTS", "YTUS"]


def test_pending_work_counts_without_fetching_rows(
    store: SupabaseStore, server: FakePostgrest
) -> None:
    """⚠️ **개수만 받아야 한다.** 레코드로 세면 `raw_text`·`raw_html`까지 와서 수천 행에서
    수십 MB가 된다 — 전량 실행에서 `status`가 그만큼 느려지고 메모리를 먹는다."""
    store.save_source_data(_source_data("1"))
    server.requests.clear()

    store.pending_work()

    assert server.requests, "조회가 있었어야 한다"
    assert all(request.method == "HEAD" for request in server.requests), (
        f"본문을 받는 요청이 섞였다: {[r.method for r in server.requests]}"
    )


def test_pending_work_splits_retryable_from_given_up(store: SupabaseStore) -> None:
    """⚠️ 두 수가 **같은 경계**를 써야 한다 — 겹치면 합이 실제보다 크고, 빠지면 사라진다.

    ⚠️ 개수를 **다르게** 만든다(2 대 1) — 둘이 같으면 경계가 겹쳐도 통과한다.
    """
    for external_id in ("1", "2"):
        store.save_source_data(_source_data(external_id))
    store.save_source_data(replace(_source_data("3"), structure_attempts=MAX_STRUCTURE_ATTEMPTS))

    work = store.pending_work()

    assert (work.unstructured, work.given_up) == (2, 1)


def test_pending_work_ignores_an_approved_draft_already_out(store: SupabaseStore) -> None:
    """⚠️ 이미 공개된 행을 세면 **매일 "막혔다"고 거짓 경보**가 뜬다 — 242행이 다 걸린다."""
    published, waiting = _source_data("1"), _source_data("2")
    for record in (published, waiting):
        store.save_source_data(record)
    store.upsert_review_data(
        _review_data(published.id, review_status=ReviewStatus.APPROVED, published_job_id=uuid4())
    )
    store.upsert_review_data(_review_data(waiting.id, review_status=ReviewStatus.APPROVED))

    work = store.pending_work()

    assert work.approved_unpublished == 1, "공개된 것은 세지 않는다"
