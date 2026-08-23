"""JsonStore 테스트 — 원장·격리·write-once·검수 상태 보존.

실제 파일을 쓰지만 전부 `tmp_path`다(네트워크·리포 `data/` 미접촉).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

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
    Attachment,
    ReviewData,
    SourceData,
    SourceHealth,
    new_id,
)
from minjob_ingest.store.base import (
    DedupUpdate,
    DedupVerdict,
    LedgerEntry,
    Store,
    StoreError,
)
from minjob_ingest.store.guards import MUTABLE_STATE_FIELDS
from minjob_ingest.store.json_store import FILE_VERSION, JsonStore
from minjob_ingest.store.serde import SerdeError, to_row

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def store(data_dir: Path) -> JsonStore:
    return JsonStore(data_dir)


@pytest.fixture
def corruption_log() -> list[str]:
    return []


@pytest.fixture
def lenient_store(data_dir: Path, corruption_log: list[str]) -> JsonStore:
    """손상 행 보고를 모아 확인할 수 있는 store."""
    return JsonStore(
        data_dir, on_corrupt_row=lambda source, err: corruption_log.append(f"{source}: {err}")
    )


def _source_data(external_id: str = "25553", *, run_id: object = None) -> SourceData:
    return SourceData(
        source_key="YTUS",
        external_id=external_id,
        source_url=f"https://www.ytus.ac.kr/board/view/trXXR/{external_id}",
        title=f"공고 {external_id}",
        posted_on=FIXED_NOW.date(),
        run_id=run_id if run_id is not None else new_id(),  # type: ignore[arg-type]
        fetched_at=FIXED_NOW,
        raw_text="오천중앙교회에서 부목사님을 모십니다.",
    )


def _review_data(source_data_id: object) -> ReviewData:
    return ReviewData(
        posted_at=FIXED_NOW.date(),
        source_url="https://www.ytus.ac.kr/board/view/trXXR/25553",
        source_data_id=source_data_id,  # type: ignore[arg-type]
        run_id=new_id(),
        is_church_recruitment=IsChurchRecruitment.YES,
        confidence=Confidence.HIGH,
        denomination_source=DenominationSource.STATED,
        denomination=Denomination.TONGHAP,
    )


def _read_raw(store_dir: Path, file_name: str) -> list[dict[str, object]]:
    document = json.loads((store_dir / file_name).read_text(encoding="utf-8"))
    records: list[dict[str, object]] = document["records"]
    return records


def _write_raw(
    store_dir: Path, file_name: str, records: list[object], version: int = FILE_VERSION
) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / file_name).write_text(
        json.dumps({"version": version, "records": records}, ensure_ascii=False), encoding="utf-8"
    )


# ── 프로토콜 준수 ────────────────────────────────────────────────


def test_json_store_satisfies_store_protocol(store: JsonStore) -> None:
    def accepts(candidate: Store) -> Store:
        return candidate

    assert accepts(store) is store


# ── 원장(증분) ───────────────────────────────────────────────────


def test_empty_store_reports_nothing_seen(store: JsonStore) -> None:
    assert store.seen_postings("YTUS", ["1", "2"]) == {}


def test_seen_ids_are_scoped_by_source(store: JsonStore) -> None:
    store.save_source_data(_source_data("100"))
    # 같은 글번호를 다른 게시판이 써도 별개다(UNIQUE는 두 컬럼 조합).
    assert set(store.seen_postings("YTUS", ["100", "200"])) == {"100"}
    assert store.seen_postings("PUTS", ["100"]) == {}


def test_save_is_idempotent_on_ledger_key(store: JsonStore) -> None:
    first = store.save_source_data(_source_data("100"))
    # 같은 글을 다시 넣으면 새 레코드(id 다름)여도 원장 키가 같아 무시된다.
    second = store.save_source_data(_source_data("100"))
    assert (first, second) == (True, False)
    assert len(store.list_unstructured(limit=10)) == 1


def test_duplicate_save_keeps_the_first_evidence(store: JsonStore) -> None:
    # DO NOTHING이어야 한다 — 덮어쓰면 최초 수집 원문(증거)이 재수집본으로 갈린다.
    original = _source_data("100")
    store.save_source_data(original)
    store.save_source_data(replace(_source_data("100"), raw_text="나중에 수정된 본문"))
    stored = store.list_unstructured(limit=10)
    assert len(stored) == 1
    assert stored[0].raw_text == original.raw_text
    assert stored[0].id == original.id


def test_seen_ids_with_empty_request_does_not_read(store: JsonStore) -> None:
    assert store.seen_postings("YTUS", []) == {}


def test_seen_ids_normalizes_the_lookup_key(store: JsonStore) -> None:
    # 저장 때는 모델이 정규화하므로 조회도 같은 정규화를 거쳐야 원장이 빗나가지 않는다.
    store.save_source_data(_source_data("100"))
    assert set(store.seen_postings(" YTUS ", ["100"])) == {"100"}
    # 반환 키는 호출자가 넘긴 원본이어야 한다 — 호출자가 이걸로 자기 목록을 걸러낸다.
    assert set(store.seen_postings("YTUS", [" 100"])) == {" 100"}


# ── 원장 대조: 번호가 다른 글로 바뀌었는지 ─────────────────────


def test_seen_postings_returns_the_stored_title_and_date(store: JsonStore) -> None:
    """제목·게시일을 함께 돌려주므로 **추가 요청 없이** 목록 값과 대조할 수 있다."""
    store.save_source_data(replace(_source_data("100"), title="삼성교회 담임목사 청빙"))
    entry = store.seen_postings("YTUS", ["100"])["100"]
    assert entry.title == "삼성교회 담임목사 청빙"
    assert entry.posted_on == FIXED_NOW.date()


def test_same_posting_is_not_flagged() -> None:
    entry = LedgerEntry(title="삼성교회 담임목사 청빙", posted_on=date(2026, 8, 3))
    assert not entry.points_to_another_posting(
        title="삼성교회 담임목사 청빙", posted_on=date(2026, 8, 3)
    )


def test_whitespace_and_nbsp_differences_are_not_a_change() -> None:
    """게시판이 `&nbsp;`를 흔하게 쓴다 — 눈에 안 보이는 차이로 경보가 울리면 안 된다.

    ⚠️ 날짜를 **다르게** 준다. 같게 주면 AND 조건 때문에 제목 비교가 결과에 영향을 못 줘
    정규화를 지워도 테스트가 통과한다(실제로 그랬다).
    """
    entry = LedgerEntry(title="삼성교회  담임목사 청빙", posted_on=date(2026, 8, 3))
    assert not entry.points_to_another_posting(
        title="삼성교회\xa0담임목사  청빙", posted_on=date(2026, 8, 10)
    )


def test_title_only_change_is_treated_as_an_edit() -> None:
    """작성자가 `[끌어올림]`·`(마감)`을 붙이는 일이 흔하다 — 경보로 만들면 상시 잡음이 된다."""
    entry = LedgerEntry(title="내당교회에서 부목사님을 모십니다.", posted_on=date(2026, 8, 2))
    assert not entry.points_to_another_posting(
        title="[끌어올림]내당교회에서 부목사님을 모십니다.", posted_on=date(2026, 8, 2)
    )


def test_date_only_change_is_treated_as_an_edit() -> None:
    entry = LedgerEntry(title="삼성교회 담임목사 청빙", posted_on=date(2026, 8, 3))
    assert not entry.points_to_another_posting(
        title="삼성교회 담임목사 청빙", posted_on=date(2026, 8, 10)
    )


def test_both_changed_means_the_number_points_elsewhere() -> None:
    """제목과 날짜가 둘 다 다르면 그 번호가 다른 글을 가리킨다.

    원인은 둘이다 — 게시판이 번호를 재사용(그 공고를 영구히 놓친다), 또는 사이트 개편으로
    엉뚱한 칸을 읽기 시작(모든 행이 이 모양이 된다). 둘 다 조용히 건너뛰면 안 된다.
    """
    entry = LedgerEntry(title="삼성교회 담임목사 청빙", posted_on=date(2026, 8, 3))
    assert entry.points_to_another_posting(title="○○교회 부목사 청빙", posted_on=date(2026, 9, 15))


def test_a_row_the_list_no_longer_dates_counts_as_different() -> None:
    """⚠️ 저장된 행에는 날짜가 반드시 있다(`posted_on` 필수). 목록 쪽이 날짜를 잃는 것은
    셀렉터가 깨졌다는 신호이고, 제목까지 다르면 그 번호가 다른 글을 가리키는 것이다."""
    entry = LedgerEntry(title="가", posted_on=date(2026, 8, 3))

    assert entry.points_to_another_posting(title="나", posted_on=None)
    assert not entry.points_to_another_posting(title="가", posted_on=None)


def test_corrupt_entry_row_is_skipped_not_fatal(
    lenient_store: JsonStore, corruption_log: list[str], data_dir: Path
) -> None:
    """원장 키는 읽히는데 제목이 깨진 행 — 전체 조회를 죽이면 원장을 잃고 31곳을 다시 긁는다."""
    good = to_row(_source_data("1"))
    broken = dict(to_row(_source_data("2")))
    broken["title"] = 12345  # 문자열이어야 함
    _write_raw(data_dir, "source_data.json", [good, broken])

    seen = lenient_store.seen_postings("YTUS", ["1", "2"])
    assert set(seen) == {"1"}
    assert len(corruption_log) == 1


# ── 구조화 대상 조회 ─────────────────────────────────────────────


def test_lists_only_unstructured_oldest_first(store: JsonStore) -> None:
    old = replace(_source_data("1"), fetched_at=FIXED_NOW - timedelta(days=2))
    new = replace(_source_data("2"), fetched_at=FIXED_NOW)
    done = replace(_source_data("3"), fetched_at=FIXED_NOW).with_verdict_recorded()
    for record in (new, done, old):
        store.save_source_data(record)
    assert [r.external_id for r in store.list_unstructured(limit=10)] == ["1", "2"]


def test_list_unstructured_respects_limit(store: JsonStore) -> None:
    for index in range(5):
        store.save_source_data(_source_data(str(index)))
    assert len(store.list_unstructured(limit=2)) == 2


def test_list_unstructured_rejects_zero_limit(store: JsonStore) -> None:
    # 상한 없는 배치는 백필 직후 backlog로 실행을 폭주시킨다.
    with pytest.raises(ValueError, match="limit"):
        store.list_unstructured(limit=0)


def test_exhausted_rows_drop_out_of_the_queue(store: JsonStore) -> None:
    record = _source_data("1")
    store.save_source_data(record)
    for _ in range(MAX_STRUCTURE_ATTEMPTS):
        record = record.with_failed_attempt("HTTP 429")
        store.update_structure_state(record)
    assert store.list_unstructured(limit=10) == ()


def test_failed_attempt_stays_in_the_queue(store: JsonStore) -> None:
    # 실패는 structured_at을 남기지 않으므로 다음 run이 다시 집어야 한다(SPEC §4).
    record = _source_data("1")
    store.save_source_data(record)
    store.update_structure_state(record.with_failed_attempt("일시 오류"))
    queued = store.list_unstructured(limit=10)
    assert len(queued) == 1
    assert queued[0].structure_attempts == 1
    assert queued[0].last_structure_error == "일시 오류"


# ── write-once 강제 ─────────────────────────────────────────────


#: 상태 3필드 **말고 전부**가 write-once다. `id`는 조회 키라 제외(바꾸면 다른 레코드다).
_EVIDENCE_TAMPERINGS: Final = {
    "source_key": "PUTS",
    "external_id": "9999",
    "source_url": "https://elsewhere.example/1",
    "title": "바꿔치기한 제목",
    "posted_on": FIXED_NOW.date() - timedelta(days=30),
    "run_id": UUID("00000000-0000-4000-8000-00000000ffff"),
    "fetched_at": FIXED_NOW - timedelta(days=1),
    "raw_text": "바꿔치기",
    "raw_html": "<div>바꿔치기</div>",
    "image_urls": ("https://x/injected.png",),
    "attachments": (Attachment(name="끼워넣은.hwp", url="https://x/dl/9"),),
    "raw_meta": {"injected": True},
    "content_hash": "deadbeef",
}


def test_evidence_tampering_cases_cover_every_immutable_field() -> None:
    """필드가 추가되면 이 테스트가 먼저 깨진다 — 새 필드가 조용히 보호에서 빠지는 걸 막는다."""
    immutable = {f.name for f in fields(SourceData)} - set(MUTABLE_STATE_FIELDS) - {"id"}
    assert immutable == set(_EVIDENCE_TAMPERINGS)


@pytest.mark.parametrize("field_name", list(_EVIDENCE_TAMPERINGS))
def test_state_update_rejects_changed_evidence(store: JsonStore, field_name: str) -> None:
    record = _source_data("1")
    store.save_source_data(record)
    # `replace(**{...})`는 필드마다 타입이 달라 정적으로 못 쓴다 → 검증을 우회해 직접 심는다.
    # 어차피 "생성자를 통과하지 못할 값이 들어온 레코드"까지 store가 막아야 한다.
    tampered = replace(record.with_verdict_recorded())
    object.__setattr__(tampered, field_name, _EVIDENCE_TAMPERINGS[field_name])
    with pytest.raises(StoreError, match="원문 증거"):
        store.update_structure_state(tampered)


def test_state_update_rejects_erasing_a_recorded_verdict(store: JsonStore) -> None:
    # 낡은 in-memory 레코드로 갱신하면 판정이 지워져 Gemini에 재과금된다(SPEC §4).
    record = _source_data("1")
    store.save_source_data(record)
    store.update_structure_state(record.with_verdict_recorded(FIXED_NOW))
    with pytest.raises(StoreError, match="판정"):
        store.update_structure_state(record)
    assert store.list_unstructured(limit=10) == ()


def test_state_update_rejects_lowering_attempts(store: JsonStore) -> None:
    # 시도 횟수가 줄면 상한에 영원히 도달하지 못해 영구 실패 공고를 무한 재호출한다.
    record = _source_data("1")
    store.save_source_data(record)
    store.update_structure_state(record.with_failed_attempt("HTTP 429"))
    with pytest.raises(StoreError, match="시도 횟수"):
        store.update_structure_state(record.with_attempts_reset())


def test_state_update_rejects_unknown_record(store: JsonStore) -> None:
    with pytest.raises(StoreError, match="없음"):
        store.update_structure_state(_source_data("1").with_verdict_recorded())


def test_state_update_persists_verdict(store: JsonStore) -> None:
    record = _source_data("1")
    store.save_source_data(record)
    store.update_structure_state(record.with_verdict_recorded(FIXED_NOW))
    assert store.list_unstructured(limit=10) == ()


# ── review_data upsert: 검수 상태 보존 ───────────────────────────


def test_first_upsert_inserts(store: JsonStore) -> None:
    assert store.upsert_review_data(_review_data(new_id())) is True


def test_restructure_replaces_pending_draft_and_keeps_identity(
    store: JsonStore, data_dir: Path
) -> None:
    source_id = new_id()
    original = _review_data(source_id)
    store.upsert_review_data(original)
    redraft = replace(_review_data(source_id), title="다시 구조화한 제목")
    assert store.upsert_review_data(redraft) is True

    rows = _read_raw(data_dir, "review_data.json")
    assert len(rows) == 1  # UNIQUE(source_data_id)
    assert rows[0]["id"] == str(original.id)  # admin 참조가 끊기지 않는다
    assert rows[0]["title"] == "다시 구조화한 제목"


def test_restructure_does_not_overwrite_reviewed_draft(store: JsonStore, data_dir: Path) -> None:
    # 재구조화가 운영자 승인을 PENDING으로 되돌리면 안 된다.
    source_id = new_id()
    approved = replace(
        _review_data(source_id),
        review_status=ReviewStatus.APPROVED,
        reviewed_by="operator@minjob",
        reviewed_at=FIXED_NOW,
    )
    store.upsert_review_data(approved)
    assert store.upsert_review_data(_review_data(source_id)) is False

    rows = _read_raw(data_dir, "review_data.json")
    assert rows[0]["review_status"] == "APPROVED"
    assert rows[0]["reviewed_by"] == "operator@minjob"


def test_restructure_does_not_erase_operator_edits_on_a_pending_draft(
    store: JsonStore, data_dir: Path
) -> None:
    """운영자가 고쳐놓고 승인 전에 멈춘 행 — 이어받는 필드는 검수 메타뿐이라 나머지는 덮인다.

    `review_status`만 보고 판단하면 손으로 확정한 교단이 AI 초안(UNKNOWN)으로 되돌아가고
    `reviewed_by`만 남아 "봤는데 고친 흔적이 없는" 모순 행이 된다.
    """
    source_id = new_id()
    corrected = replace(
        _review_data(source_id),
        denomination=Denomination.TONGHAP,
        denomination_source=DenominationSource.OPERATOR,
        church_name="오천중앙교회",
        review_status=ReviewStatus.PENDING,
    )
    store.upsert_review_data(corrected)

    # AI가 교단을 못 알아낸 초안(근거 unknown이라 값 없음이 허용된다).
    ai_redraft = replace(
        _review_data(source_id),
        denomination=None,
        denomination_source=DenominationSource.UNKNOWN,
        church_name=None,
    )
    assert store.upsert_review_data(ai_redraft) is False

    rows = _read_raw(data_dir, "review_data.json")
    assert rows[0]["denomination"] == "TONGHAP"
    assert rows[0]["denomination_source"] == "operator"
    assert rows[0]["church_name"] == "오천중앙교회"


def test_restructure_replaces_untouched_pending_draft(store: JsonStore, data_dir: Path) -> None:
    # 반대편 경계: 운영자가 손대지 않은 PENDING 초안은 그대로 교체돼야 한다.
    source_id = new_id()
    store.upsert_review_data(replace(_review_data(source_id), church_name="옛 초안"))
    assert store.upsert_review_data(replace(_review_data(source_id), church_name="새 초안")) is True
    assert _read_raw(data_dir, "review_data.json")[0]["church_name"] == "새 초안"


# ── crawl_run ───────────────────────────────────────────────────


def test_start_run_returns_persisted_record(store: JsonStore) -> None:
    run = store.start_run(CrawlMode.DAILY)
    assert run.is_finished is False
    store.finish_run(run.finish(sources_ok=30, sources_failed=1, new_count=42))


def test_finish_run_keeps_id_and_records_totals(store: JsonStore, data_dir: Path) -> None:
    run = store.start_run(CrawlMode.BACKFILL)
    # 시작 시각은 store가 실제 now로 찍으므로 종료 시각을 고정값으로 주면 안 된다(과거가 됨).
    store.finish_run(run.finish(sources_ok=31, sources_failed=0, new_count=8))
    rows = _read_raw(data_dir, "crawl_run.json")
    assert len(rows) == 1
    assert rows[0]["id"] == str(run.id)
    assert rows[0]["new_count"] == 8
    assert rows[0]["finished_at"] is not None


def test_finish_run_rejects_unknown_run(store: JsonStore, data_dir: Path) -> None:
    # 조용히 넘기면 실행이 영구 "진행중"으로 남아 대시보드가 거짓말을 한다.
    orphan = store.start_run(CrawlMode.DAILY)
    other = JsonStore(data_dir / "elsewhere")
    with pytest.raises(StoreError, match="없음"):
        other.finish_run(orphan.finish(sources_ok=0, sources_failed=0, new_count=0))


# ── source_health ───────────────────────────────────────────────


def test_health_is_absent_before_first_run(store: JsonStore) -> None:
    assert store.get_health("YTUS") is None


def test_get_health_normalizes_the_lookup_key(store: JsonStore) -> None:
    """조회가 빗나가면 previous=None이 되고, 누적 카운터가 매 실행 초기화된다(§7 경보 사망)."""
    store.upsert_health(
        SourceHealth.advance(
            previous=None,
            source_key="YTUS",
            run_at=FIXED_NOW,
            status=SourceHealthStatus.FAIL,
            error="timeout",
        )
    )
    found = store.get_health(" YTUS ")
    assert found is not None
    assert found.consecutive_failures == 1


def test_health_roundtrip_and_upsert_replaces(store: JsonStore, data_dir: Path) -> None:
    first = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=FIXED_NOW,
        status=SourceHealthStatus.OK,
        rows=20,
        new_count=8,
    )
    store.upsert_health(first)
    assert store.get_health("YTUS") == first

    second = SourceHealth.advance(
        previous=store.get_health("YTUS"),
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=1),
        status=SourceHealthStatus.FAIL,
        error="HTTP 500",
    )
    store.upsert_health(second)
    stored = store.get_health("YTUS")
    assert stored is not None
    assert stored.consecutive_failures == 1
    assert stored.last_success_at == FIXED_NOW  # 실패가 마지막 성공을 지우지 않는다
    rows = _read_raw(data_dir, "source_health.json")
    assert len(rows) == 1  # source_key PK


# ── 손상 행 격리 ────────────────────────────────────────────────


def test_corrupt_row_is_skipped_not_fatal(
    lenient_store: JsonStore, corruption_log: list[str], data_dir: Path
) -> None:
    # 한 행이 깨졌다고 전체 로드를 실패시키면 원장을 잃고 31곳을 다시 긁는다.
    good = to_row(_source_data("1"))
    _write_raw(data_dir, "source_data.json", [good, {"broken": True}])

    queued = lenient_store.list_unstructured(limit=10)
    assert [r.external_id for r in queued] == ["1"]
    assert len(corruption_log) == 1
    assert "source_data.json" in corruption_log[0]


def test_ledger_ignores_corrupt_row(
    lenient_store: JsonStore, corruption_log: list[str], data_dir: Path
) -> None:
    good = to_row(_source_data("1"))
    _write_raw(data_dir, "source_data.json", [good, {"external_id": 5}])
    assert set(lenient_store.seen_postings("YTUS", ["1"])) == {"1"}
    assert corruption_log


def test_corrupt_health_row_is_not_silently_none(store: JsonStore, data_dir: Path) -> None:
    # None으로 삼키면 누적 카운터가 초기화돼 §7 경보가 무의미해진다.
    _write_raw(data_dir, "source_health.json", [{"source_key": "YTUS"}])
    with pytest.raises(SerdeError):
        store.get_health("YTUS")


# ── 파일 형식·원자성 ────────────────────────────────────────────


def test_written_file_has_version_and_records(store: JsonStore, data_dir: Path) -> None:
    store.save_source_data(_source_data("1"))
    document = json.loads((data_dir / "source_data.json").read_text(encoding="utf-8"))
    assert document["version"] == FILE_VERSION
    assert len(document["records"]) == 1


def test_unknown_file_version_is_rejected(store: JsonStore, data_dir: Path) -> None:
    # 백필 후 스키마가 바뀌면 "컬럼 누락"이 아니라 버전 불일치로 원인이 드러나야 한다.
    _write_raw(data_dir, "source_data.json", [], version=99)
    with pytest.raises(StoreError, match="버전"):
        store.list_unstructured(limit=1)


def test_broken_json_file_is_rejected(store: JsonStore, data_dir: Path) -> None:
    target = data_dir / "source_data.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StoreError, match="파싱 실패"):
        store.list_unstructured(limit=1)


def test_write_leaves_no_temp_file(store: JsonStore, data_dir: Path) -> None:
    store.save_source_data(_source_data("1"))
    assert list(data_dir.glob("*.tmp")) == []


def test_failed_write_keeps_the_old_file_and_cleans_up(
    store: JsonStore, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """쓰기 도중 실패(디스크 풀 등) — 기존 원장은 온전하고 부분 파일은 남지 않아야 한다.

    ⚠️ **`StoreError`로 올린다**(`store/base.py` 계약). 날 `OSError`로 던지면 구조화가 글 단위
    격리도 연속 실패 중단도 지나쳐 배치가 통째로 죽는다 — 운영자는 리포트도 미리보기도 못
    받고, 그 사이 다른 게시판은 계속 유료 호출을 낸다(2026-08-15 검수에서 재현).
    """
    store.save_source_data(_source_data("1"))
    before = (data_dir / "source_data.json").read_text(encoding="utf-8")

    def disk_full(_descriptor: int) -> None:
        raise OSError("디스크가 꽉 찼다")

    monkeypatch.setattr("minjob_ingest.store.json_store.os.fsync", disk_full)
    with pytest.raises(StoreError, match="디스크"):
        store.save_source_data(_source_data("2"))

    assert list(data_dir.glob("*.tmp")) == []
    assert (data_dir / "source_data.json").read_text(encoding="utf-8") == before


def test_missing_file_reads_as_empty(store: JsonStore) -> None:
    # 첫 실행에는 파일이 없다 — 이때 예외가 나면 크롤이 시작조차 못 한다.
    assert store.list_unstructured(limit=5) == ()
    assert store.get_health("YTUS") is None


def test_list_unstructured_can_be_bound_to_one_source(store: JsonStore) -> None:
    """⚠️ 필터가 여기 있어야 `limit`이 "그 게시판에서 N건"이 된다.

    반환값을 호출자가 거르면, 수집 시각 순이라 오래된 쪽이 한 게시판에 뭉쳐 있어 표본이
    0건이 되는 일이 생긴다(2026-08-10 실측: 가장 오래된 100건이 게시판 2곳).
    """
    store.save_source_data(_source_data("1"))
    store.save_source_data(replace(_source_data("2"), source_key="PUTS"))

    only_puts = store.list_unstructured(10, source_key="PUTS")

    assert [record.source_key for record in store.list_unstructured(10)] == ["YTUS", "PUTS"]
    assert [record.external_id for record in only_puts] == ["2"]
    assert store.list_unstructured(10, source_key="ACTS") == ()


# ── 동시 쓰기 ────────────────────────────────────────────────────


def test_records_survive_boards_writing_at_the_same_time(tmp_path: Path) -> None:
    """⚠️ 파일 전체를 다시 쓰는 구조라 잠그지 않으면 **나중 쓰기가 앞의 것을 통째로 덮는다**.

    구조화가 게시판 간 병렬로 돌기 때문에(`pipeline/structure.py`) 실제로 일어나는 경로다.
    잃는 것이 `structured_at`이면 Gemini 재과금이고, 초안이면 탐지할 방법이 없다.
    """
    store = JsonStore(tmp_path)
    records = [_source_data(f"{number}") for number in range(40)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        saved = list(pool.map(store.save_source_data, records))

    assert all(saved)
    assert len(_read_raw(tmp_path, "source_data.json")) == 40


def test_a_state_update_does_not_drop_a_concurrent_insert(tmp_path: Path) -> None:
    """읽고-고쳐-쓰는 두 갱신이 겹치는 경우. 한쪽이 읽은 뒤 다른 쪽이 쓰면 그 행이 사라진다."""
    store = JsonStore(tmp_path)
    existing = _source_data("100")
    store.save_source_data(existing)

    def update() -> None:
        store.update_structure_state(existing.with_verdict_recorded())

    def insert(number: int) -> None:
        store.save_source_data(_source_data(f"{number}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(insert, number) for number in range(20)]
        futures.append(pool.submit(update))
        for future in futures:
            future.result()

    rows = _read_raw(tmp_path, "source_data.json")
    assert len(rows) == 21
    stored = {row["external_id"]: row for row in rows}
    assert stored["100"]["structured_at"] is not None


# ── 되돌리기 ─────────────────────────────────────────────────────


def _structured(store: JsonStore, record: SourceData) -> SourceData:
    store.save_source_data(record)
    done = record.with_verdict_recorded()
    store.update_structure_state(done)
    return done


def test_requeue_clears_the_verdict_and_the_draft(store: JsonStore, data_dir: Path) -> None:
    """⚠️ `structured_at`은 앞으로만 간다 — 되돌리는 길이 없으면 전량 저장이 외길이 된다."""
    record = _structured(store, _source_data("1"))
    store.upsert_review_data(_review_data(record.id))

    result = store.requeue_for_structure()

    assert (result.requeued, result.skipped) == (1, ())
    assert [row.external_id for row in store.list_unstructured(10)] == ["1"]
    assert _read_raw(data_dir, "review_data.json") == []


def test_requeue_keeps_a_draft_the_operator_touched(store: JsonStore, data_dir: Path) -> None:
    """⚠️ 승인된 초안을 지우면 `published_job_id`가 사라져 **이미 공개한 공고를 다시 승격**한다.

    판정 기준은 저장 쪽과 같은 `is_safe_to_replace` 하나여야 한다(2026-08-14 검수).
    """
    record = _structured(store, _source_data("1"))
    approved = replace(_review_data(record.id), review_status=ReviewStatus.APPROVED)
    store.upsert_review_data(approved)

    result = store.requeue_for_structure()

    assert result.requeued == 0
    assert result.skipped == (record.label,)
    assert store.list_unstructured(10) == (), "판정이 그대로 남는다"
    assert len(_read_raw(data_dir, "review_data.json")) == 1, "초안도 그대로 남는다"


def test_requeue_can_be_narrowed_to_one_board(store: JsonStore) -> None:
    _structured(store, _source_data("1"))
    other = replace(_source_data("2"), source_key="CSU")
    _structured(store, other)

    result = store.requeue_for_structure(source_key="CSU")

    assert result.requeued == 1
    assert [row.source_key for row in store.list_unstructured(10)] == ["CSU"]


def test_requeue_can_be_narrowed_to_single_postings(store: JsonStore) -> None:
    """⚠️ 이게 없으면 3건을 되살리려고 게시판 전체를 재과금한다."""
    for external_id in ("1", "2", "3"):
        _structured(store, _source_data(external_id))

    result = store.requeue_for_structure(source_key="YTUS", external_ids=("1", "3"))

    assert result.requeued == 2
    assert sorted(row.external_id for row in store.list_unstructured(10)) == ["1", "3"]


def test_requeue_by_posting_needs_the_board(store: JsonStore) -> None:
    """⚠️ `external_id`는 게시판 안에서만 유일하다 — 번호만 받으면 남의 공고를 지운다."""
    _structured(store, _source_data("1"))

    with pytest.raises(ValueError, match="source_key"):
        store.requeue_for_structure(external_ids=("1",))

    assert store.list_unstructured(10) == (), "아무것도 바뀌지 않았다"


def test_requeue_refuses_when_a_draft_cannot_be_read(store: JsonStore, data_dir: Path) -> None:
    """⚠️ 그 행이 승인된 것인지 모르는 채로 지우면 되돌릴 방법이 없다 — 멈추는 쪽이 맞다."""
    record = _structured(store, _source_data("1"))
    store.upsert_review_data(_review_data(record.id))
    path = data_dir / "review_data.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["records"][0]["confidence"]
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(StoreError, match="되돌리지 않았다"):
        store.requeue_for_structure()

    assert store.list_unstructured(10) == (), "아무것도 바뀌지 않았다"


# ── 중복 판정 읽기·쓰기 (SPEC §4.1) ──────────────────────────────


def _drafted(store: JsonStore, external_id: str) -> ReviewData:
    record = _structured(store, _source_data(external_id))
    draft = _review_data(record.id)
    store.upsert_review_data(draft)
    return draft


def test_dedup_candidates_carry_the_source_posting_date(store: JsonStore) -> None:
    """⚠️ 대표의 `review_data.posted_at`은 묶음의 최신으로 덮인다 — 그 값으로 라운드를 계산하면
    다시 돌릴 때마다 경계가 움직인다. 원자료 게시일은 write-once라 안 흔들린다."""
    draft = _drafted(store, "1")

    (candidate,) = store.dedup_candidates()

    assert candidate.draft.id == draft.id
    assert candidate.posted_on == FIXED_NOW.date()


def test_dedup_candidates_are_not_filtered_by_the_store(store: JsonStore) -> None:
    """무엇을 판정 대상으로 볼지는 `pipeline/dedup`이 정한다 — 저장소가 정책을 알면 규칙을
    고칠 때 순수 함수 테스트가 아니라 저장소 테스트를 고쳐야 한다."""
    _drafted(store, "1")
    record = _structured(store, _source_data("2"))
    store.upsert_review_data(
        replace(
            _review_data(record.id),
            review_status=ReviewStatus.REJECTED,
            reject_reason=RejectReason.HERESY,
            heresy_flag=True,
            heresy_evidence="heresy-ref: 교회명 일치",
        )
    )

    assert len(store.dedup_candidates()) == 2


def test_dedup_stops_instead_of_skipping_a_broken_draft(store: JsonStore, data_dir: Path) -> None:
    """⚠️ 건너뛴 행이 대표였을 수 있다 — 그러면 대표가 아닌 쪽이 공개되고 아무 표시도 없다."""
    _drafted(store, "1")
    path = data_dir / "review_data.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["records"][0]["confidence"]
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(StoreError, match="중복 판정을 멈췄다"):
        store.dedup_candidates()


def test_applying_a_verdict_rejects_the_duplicate(store: JsonStore) -> None:
    draft = _drafted(store, "1")
    key = "오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:-:R1"

    changed = store.apply_dedup(
        [
            DedupUpdate(
                review_data_id=draft.id,
                dedup_key=key,
                dedup_state=DedupState.DUPLICATE,
                verdict=DedupVerdict(
                    review_status=ReviewStatus.REJECTED,
                    reject_reason=RejectReason.DUPLICATE,
                    posted_at=FIXED_NOW.date(),
                ),
            )
        ]
    )

    (stored,) = store.dedup_candidates()
    assert changed == 1
    assert stored.draft.dedup_key == key
    assert stored.draft.dedup_state is DedupState.DUPLICATE
    assert stored.draft.reject_reason is RejectReason.DUPLICATE


def test_applying_the_same_verdict_twice_writes_nothing(store: JsonStore) -> None:
    """멱등 — 매 실행 파일이 바뀌면 무엇이 실제로 변했는지 알 수 없다."""
    draft = _drafted(store, "1")
    update = DedupUpdate(
        review_data_id=draft.id,
        dedup_key="오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:-:R1",
        dedup_state=DedupState.ALONE,
    )

    assert store.apply_dedup([update]) == 1
    assert store.apply_dedup([update]) == 0


def test_applying_a_verdict_to_a_missing_draft_stops_the_run(store: JsonStore) -> None:
    """⚠️ 조용히 넘기면 판정이 사라진 것을 아무도 모른다."""
    with pytest.raises(StoreError, match="초안이 없어"):
        store.apply_dedup(
            [
                DedupUpdate(
                    review_data_id=new_id(),
                    dedup_key="없는교회:SEOUL:ETC:-:R1",
                    dedup_state=DedupState.ALONE,
                )
            ]
        )


def test_a_label_is_allowed_on_a_row_the_operator_owns(store: JsonStore) -> None:
    """⚠️ 라벨은 붙여야 한다 — 없으면 SPEC §4.2가 "이미 공개된 같은 자리"를 못 찾는다."""
    record = _structured(store, _source_data("1"))
    published = replace(
        _review_data(record.id),
        review_status=ReviewStatus.APPROVED,
        published_job_id=new_id(),
    )
    store.upsert_review_data(published)
    key = "오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:-:R1"

    changed = store.apply_dedup(
        [DedupUpdate(review_data_id=published.id, dedup_key=key, dedup_state=DedupState.MASTER)]
    )

    (stored,) = store.dedup_candidates()
    assert changed == 1
    assert stored.draft.dedup_key == key
    assert stored.draft.review_status is ReviewStatus.APPROVED, "판정은 그대로다"


def test_a_verdict_on_a_row_the_operator_owns_is_a_bug(store: JsonStore) -> None:
    """⚠️ 조용히 무시하면 사람이 한 일이 덮인 뒤에도 아무 표시가 없다 — 멈추고 알린다."""
    record = _structured(store, _source_data("1"))
    published = replace(
        _review_data(record.id),
        review_status=ReviewStatus.APPROVED,
        published_job_id=new_id(),
    )
    store.upsert_review_data(published)

    with pytest.raises(StoreError, match="판정을 쓸 수 없다"):
        store.apply_dedup(
            [
                DedupUpdate(
                    review_data_id=published.id,
                    dedup_key="오천중앙교회:GYEONGBUK:ASSOCIATE_PASTOR:-:R1",
                    dedup_state=DedupState.DUPLICATE,
                    verdict=DedupVerdict(
                        review_status=ReviewStatus.REJECTED,
                        reject_reason=RejectReason.DUPLICATE,
                        posted_at=FIXED_NOW.date(),
                    ),
                )
            ]
        )
