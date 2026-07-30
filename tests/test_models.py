"""레코드 불변식 테스트 — 잘못된 상태가 애초에 만들어지지 않는지(SPEC §6).

⚠️ 일부러 `**overrides: object` 헬퍼를 쓰지 않는다 — 그러면 호출부 타입이 지워져
mypy가 `fetched_at="문자열"` 같은 실수를 놓친다. 유효 레코드 하나를 만들고
`dataclasses.replace`로 필드를 바꿔 검증이 다시 돌게 한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from minjob_ingest.clock import utc_now
from minjob_ingest.domain import (
    Confidence,
    CrawlMode,
    Denomination,
    DenominationSource,
    IsChurchRecruitment,
    ReviewStatus,
    SourceHealthStatus,
)
from minjob_ingest.models import (
    MAX_DESCRIPTION_CHARS,
    MAX_STRUCTURE_ATTEMPTS,
    CrawlRun,
    JsonValue,
    ReviewData,
    SourceData,
    SourceHealth,
    new_id,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
KST = timezone(timedelta(hours=9))


def _source_data() -> SourceData:
    return SourceData(
        source_key="YTUS",
        external_id="25553",
        source_url="https://www.ytus.ac.kr/board/view/trXXR/25553",
        run_id=new_id(),
        fetched_at=FIXED_NOW,
        raw_text="오천중앙교회에서 부목사님을 모십니다.",
    )


def _review_data() -> ReviewData:
    return ReviewData(
        source_data_id=new_id(),
        run_id=new_id(),
        is_church_recruitment=IsChurchRecruitment.YES,
        confidence=Confidence.HIGH,
        denomination_source=DenominationSource.STATED,
        denomination=Denomination.TONGHAP,
    )


def _health_ok() -> SourceHealth:
    return SourceHealth(
        source_key="YTUS",
        last_run_at=FIXED_NOW,
        last_status=SourceHealthStatus.OK,
        last_success_at=FIXED_NOW,
    )


# ── SourceData: 원장·판정 상태 ────────────────────────────────────


def test_new_source_data_has_no_verdict() -> None:
    record = _source_data()
    assert record.structured_at is None
    assert record.has_verdict is False
    assert record.needs_restructure is True
    assert record.structure_attempts == 0
    assert record.image_urls == ()


def test_verdict_recorded_stops_restructuring() -> None:
    # 게이트1 탈락(review 미생성)도 이 전이를 타야 재호출 루프가 생기지 않는다(SPEC §4).
    record = _source_data().with_verdict_recorded(FIXED_NOW)
    assert record.has_verdict is True
    assert record.needs_restructure is False
    assert record.structure_attempts == 1


def test_failed_attempt_keeps_record_restructurable() -> None:
    # 실패는 structured_at을 남기지 않는다 — 남기면 그 공고는 영구히 재시도되지 않는다.
    record = _source_data().with_failed_attempt("HTTP 429")
    assert record.structured_at is None
    assert record.structure_attempts == 1
    assert record.needs_restructure is True


def test_attempts_are_exhausted_at_the_cap() -> None:
    record = _source_data()
    for _ in range(MAX_STRUCTURE_ATTEMPTS):
        record = record.with_failed_attempt("HTTP 429")
    assert record.structure_attempts == MAX_STRUCTURE_ATTEMPTS
    assert record.needs_restructure is False  # 무한 재호출 방지
    assert record.exhausted_attempts is True  # 운영자 리포트 대상


def test_verdict_recorded_is_not_exhausted() -> None:
    record = _source_data().with_verdict_recorded()
    assert record.exhausted_attempts is False


def test_transitions_keep_identity_and_evidence() -> None:
    original = _source_data()
    moved = original.with_verdict_recorded()
    assert moved.id == original.id
    assert moved.ledger_key == original.ledger_key
    assert moved.raw_text == original.raw_text


def test_ledger_key_is_the_unique_pair() -> None:
    assert _source_data().ledger_key == ("YTUS", "25553")


def test_source_data_is_frozen() -> None:
    with pytest.raises(AttributeError):
        _source_data().raw_text = "바뀜"  # type: ignore[misc]


# ── SourceData: 정규화 ───────────────────────────────────────────


def test_timestamps_are_normalized_to_utc() -> None:
    # 검사만 하고 원본을 저장하면 +09:00이 남아 날짜 경계 비교가 로컬시간으로 어긋난다.
    record = replace(_source_data(), fetched_at=datetime(2026, 7, 29, 21, 0, tzinfo=KST))
    assert record.fetched_at.utcoffset() == timedelta(0)
    assert record.fetched_at == FIXED_NOW


def test_identity_fields_are_stripped() -> None:
    # 공백 변형이 남으면 UNIQUE(source_key, external_id)가 쪼개져 같은 공고가 두 번 수집된다.
    record = replace(_source_data(), source_key="  YTUS  ", external_id=" 25553 ")
    assert record.ledger_key == ("YTUS", "25553")


def test_image_urls_are_coerced_to_tuple() -> None:
    # JSON에서 되읽으면 list로 온다 — 타입 검사를 안 지나는 경로(serde)를 위한 안전망.
    record = replace(_source_data(), image_urls=["https://a/1.jpg", "https://a/2.jpg"])  # type: ignore[arg-type]
    assert record.image_urls == ("https://a/1.jpg", "https://a/2.jpg")


def test_raw_meta_is_snapshotted_against_later_mutation() -> None:
    # 원문 증거 레코드가 호출자 dict를 통해 사후 변조되면 안 된다.
    source: dict[str, JsonValue] = {"views": 408}
    record = replace(_source_data(), raw_meta=source)
    source["views"] = 999
    source["injected"] = True
    assert dict(record.raw_meta) == {"views": 408}


def test_raw_meta_is_read_only() -> None:
    record = _source_data()
    with pytest.raises(TypeError):
        record.raw_meta["injected"] = True  # type: ignore[index]


# ── SourceData: 거부 ─────────────────────────────────────────────


@pytest.mark.parametrize("bad_key", ["ytus", "영남", "1ST", "A B", "YTUS-1"])
def test_rejects_invalid_source_key(bad_key: str) -> None:
    with pytest.raises(ValueError, match="source_key"):
        replace(_source_data(), source_key=bad_key)


def test_rejects_blank_external_id() -> None:
    with pytest.raises(ValueError, match="비어있을 수 없음"):
        replace(_source_data(), external_id="   ")


def test_rejects_blank_source_url() -> None:
    with pytest.raises(ValueError, match="비어있을 수 없음"):
        replace(_source_data(), source_url="   ")


def test_rejects_naive_fetched_at() -> None:
    with pytest.raises(ValueError, match="naive"):
        replace(_source_data(), fetched_at=datetime(2026, 7, 29, 12, 0))  # noqa: DTZ001


def test_rejects_naive_structured_at() -> None:
    with pytest.raises(ValueError, match="naive"):
        replace(_source_data(), structured_at=datetime(2026, 7, 29, 12, 0))  # noqa: DTZ001


def test_rejects_negative_attempts() -> None:
    with pytest.raises(ValueError, match="음수"):
        replace(_source_data(), structure_attempts=-1)


def test_allows_empty_raw_text_for_image_only_boards() -> None:
    # PCKWORLD처럼 본문이 이미지뿐인 보드는 빈 raw_text가 정상이다(config image_only).
    record = replace(_source_data(), raw_text="", image_urls=("https://pckworld/a.jpg",))
    assert record.raw_text == ""


# ── ReviewData ───────────────────────────────────────────────────


def test_review_data_defaults_to_pending() -> None:
    record = _review_data()
    assert record.review_status.value == "PENDING"
    assert record.heresy_flag is False
    assert record.requirements == ()
    assert record.created_at.utcoffset() == timedelta(0)


def test_rejects_gate1_no() -> None:
    # 게이트1 NO는 review_data를 만들지 않는다 — source_data.structured_at만 기록(SPEC §5.1).
    with pytest.raises(ValueError, match="NO"):
        replace(_review_data(), is_church_recruitment=IsChurchRecruitment.NO)


def test_uncertain_requires_low_confidence() -> None:
    # UNCERTAIN은 운영자 우선검토로 보내는 값이라 낮은 confidence여야 한다(SPEC §5.1).
    with pytest.raises(ValueError, match="UNCERTAIN"):
        replace(_review_data(), is_church_recruitment=IsChurchRecruitment.UNCERTAIN)


def test_uncertain_with_low_confidence_is_allowed() -> None:
    record = replace(
        _review_data(),
        is_church_recruitment=IsChurchRecruitment.UNCERTAIN,
        confidence=Confidence.LOW,
    )
    assert record.is_church_recruitment is IsChurchRecruitment.UNCERTAIN


def test_rejects_inverted_stipend_range() -> None:
    with pytest.raises(ValueError, match="stipend_min"):
        replace(_review_data(), stipend_min=300, stipend_max=200)


def test_accepts_equal_stipend_bounds() -> None:
    assert replace(_review_data(), stipend_min=250, stipend_max=250).stipend_min == 250


def test_rejects_negative_stipend() -> None:
    with pytest.raises(ValueError, match="음수"):
        replace(_review_data(), stipend_min=-1)


def test_rejects_source_claiming_evidence_without_denomination() -> None:
    # 근거가 값을 요구하는데 비어 있으면 거부한다(SPEC §5.3).
    with pytest.raises(ValueError, match="교단이 비어 있음"):
        replace(_review_data(), denomination=None)


def test_rejects_source_claiming_evidence_with_unknown_denomination() -> None:
    with pytest.raises(ValueError, match="교단이 비어 있음"):
        replace(_review_data(), denomination=Denomination.UNKNOWN)


def test_unknown_source_allows_missing_denomination() -> None:
    record = replace(
        _review_data(), denomination=None, denomination_source=DenominationSource.UNKNOWN
    )
    assert record.needs_operator_review is True
    assert record.is_denomination_publishable is False


def test_unknown_denomination_needs_operator() -> None:
    record = replace(
        _review_data(),
        denomination=Denomination.UNKNOWN,
        denomination_source=DenominationSource.UNKNOWN,
    )
    assert record.needs_operator_review is True


def test_ai_guess_still_needs_operator_review() -> None:
    # ai_guess는 값이 있어도 확정이 아니다 — 여기서 통과시키면 운영자 게이트가 무력화된다.
    record = replace(_review_data(), denomination_source=DenominationSource.AI_GUESS)
    assert record.denomination is Denomination.TONGHAP
    assert record.needs_operator_review is True
    assert record.is_denomination_publishable is False


def test_stated_source_is_publishable() -> None:
    record = _review_data()
    assert record.needs_operator_review is False
    assert record.is_denomination_publishable is True


def test_operator_resolution_is_readable_back() -> None:
    # SPEC §5.3: 운영자가 UNKNOWN을 10키로 해소한다 → 그 행이 되읽혀도 크래시하면 안 된다.
    record = replace(
        _review_data(),
        denomination=Denomination.ETC,
        denomination_source=DenominationSource.OPERATOR,
    )
    assert record.needs_operator_review is False
    assert record.is_denomination_publishable is True


def test_heresy_flag_requires_evidence() -> None:
    # 근거 없는 이단 플래그는 명예훼손 위험 — 판단은 사람이 한다(가드레일 #5).
    with pytest.raises(ValueError, match="heresy_evidence"):
        replace(_review_data(), heresy_flag=True)


def test_heresy_flag_rejects_blank_evidence() -> None:
    with pytest.raises(ValueError, match="heresy_evidence"):
        replace(_review_data(), heresy_flag=True, heresy_evidence="   ")


def test_heresy_flag_with_evidence_is_allowed() -> None:
    record = replace(_review_data(), heresy_flag=True, heresy_evidence="heresy-ref: 교회명 일치")
    assert record.heresy_flag is True


def test_rejects_datetime_in_date_column() -> None:
    # datetime은 date의 서브클래스라 타입 검사·런타임 모두 통과한다 → YYYY-MM-DD 컬럼이 오염된다.
    with pytest.raises(ValueError, match="datetime"):
        replace(_review_data(), posted_at=FIXED_NOW)


def test_accepts_plain_date() -> None:
    assert replace(_review_data(), posted_at=date(2026, 7, 22)).posted_at == date(2026, 7, 22)


def test_review_collections_are_coerced_to_tuple() -> None:
    # JSON에서 되읽으면 list로 온다(serde 경로 안전망).
    record = replace(_review_data(), requirements=["신대원 졸업", "면접"])  # type: ignore[arg-type]
    assert record.requirements == ("신대원 졸업", "면접")


# ── SourceHealth ─────────────────────────────────────────────────


def test_health_requires_error_when_failed() -> None:
    with pytest.raises(ValueError, match="last_error"):
        replace(_health_ok(), last_status=SourceHealthStatus.FAIL)


def test_health_rejects_blank_error_when_failed() -> None:
    with pytest.raises(ValueError, match="last_error"):
        replace(_health_ok(), last_status=SourceHealthStatus.FAIL, last_error="   ")


def test_health_ok_requires_success_time() -> None:
    # OK인데 마지막 성공 시각이 없으면 §7 경보 기준이 사라진다.
    with pytest.raises(ValueError, match="last_success_at"):
        replace(_health_ok(), last_success_at=None)


def test_health_rejects_lowercase_source_key() -> None:
    with pytest.raises(ValueError, match="source_key"):
        replace(_health_ok(), source_key="ytus")


def test_health_rejects_negative_failures() -> None:
    with pytest.raises(ValueError, match="음수"):
        replace(_health_ok(), consecutive_failures=-1)


def test_health_advance_first_success() -> None:
    health = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=FIXED_NOW,
        status=SourceHealthStatus.OK,
        new_count=8,
    )
    assert health.last_success_at == FIXED_NOW
    assert health.consecutive_failures == 0
    assert health.last_new_count == 8


def test_health_advance_failure_preserves_last_success() -> None:
    # 실패 1회로 마지막 성공 시각이 지워지면 §7 경보가 무의미해진다.
    ok = SourceHealth.advance(
        previous=None, source_key="YTUS", run_at=FIXED_NOW, status=SourceHealthStatus.OK
    )
    later = FIXED_NOW + timedelta(days=1)
    failed = SourceHealth.advance(
        previous=ok,
        source_key="YTUS",
        run_at=later,
        status=SourceHealthStatus.FAIL,
        error="HTTP 500",
    )
    assert failed.last_success_at == FIXED_NOW
    assert failed.consecutive_failures == 1
    assert failed.last_error == "HTTP 500"


def test_health_advance_accumulates_failures_then_resets() -> None:
    state = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=FIXED_NOW,
        status=SourceHealthStatus.FAIL,
        error="timeout",
    )
    state = SourceHealth.advance(
        previous=state,
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=1),
        status=SourceHealthStatus.FAIL,
        error="timeout",
    )
    assert state.consecutive_failures == 2
    assert state.last_success_at is None

    recovered = SourceHealth.advance(
        previous=state,
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=2),
        status=SourceHealthStatus.OK,
        new_count=3,
    )
    assert recovered.consecutive_failures == 0
    assert recovered.last_error is None


def test_health_advance_zero_status_counts_as_response() -> None:
    # ZERO = 응답 정상인데 신규 0건. 실패가 아니므로 성공 시각을 갱신한다(소프트 실패 후보).
    zero = SourceHealth.advance(
        previous=None, source_key="YTUS", run_at=FIXED_NOW, status=SourceHealthStatus.ZERO
    )
    assert zero.last_success_at == FIXED_NOW
    assert zero.last_error is None


# ── CrawlRun ─────────────────────────────────────────────────────


def test_crawl_run_starts_unfinished() -> None:
    run = CrawlRun(mode=CrawlMode.DAILY, started_at=utc_now())
    assert run.is_finished is False
    assert dict(run.error_detail) == {}


def test_crawl_run_finish_keeps_id() -> None:
    run = CrawlRun(mode=CrawlMode.DAILY, started_at=FIXED_NOW)
    finished = run.finish(
        at=FIXED_NOW + timedelta(minutes=12),
        sources_ok=30,
        sources_failed=1,
        new_count=42,
        error_detail={"CSU": "세션 만료"},
    )
    assert finished.is_finished is True
    assert finished.id == run.id  # 하위 레코드 FK가 유효해야 한다
    assert finished.new_count == 42
    assert dict(finished.error_detail) == {"CSU": "세션 만료"}


def test_crawl_run_rejects_finish_before_start() -> None:
    with pytest.raises(ValueError, match="finished_at"):
        CrawlRun(
            mode=CrawlMode.DAILY,
            started_at=FIXED_NOW,
            finished_at=FIXED_NOW - timedelta(hours=1),
        )


def test_crawl_run_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="음수"):
        CrawlRun(mode=CrawlMode.BACKFILL, started_at=utc_now(), sources_failed=-1)


def test_crawl_run_error_detail_is_read_only() -> None:
    run = CrawlRun(mode=CrawlMode.DAILY, started_at=FIXED_NOW, error_detail={"CSU": "x"})
    with pytest.raises(TypeError):
        run.error_detail["YTUS"] = "y"  # type: ignore[index]


def test_records_get_distinct_ids() -> None:
    assert _source_data().id != _source_data().id


# ── serde 경로: 문자열이 들어와도 불변식이 살아야 한다 ─────────────


def test_enum_strings_are_coerced_on_read() -> None:
    # JSON에서 되읽으면 값이 문자열이다. enum으로 바꾸지 않으면 `is` 비교 불변식이 전부 죽는다.
    record = ReviewData(
        source_data_id=new_id(),
        run_id=new_id(),
        is_church_recruitment="YES",  # type: ignore[arg-type]
        confidence="high",  # type: ignore[arg-type]
        denomination_source="stated",  # type: ignore[arg-type]
        denomination="TONGHAP",  # type: ignore[arg-type]
        review_status="APPROVED",  # type: ignore[arg-type]
    )
    assert record.is_church_recruitment is IsChurchRecruitment.YES
    assert record.confidence is Confidence.HIGH
    assert record.denomination is Denomination.TONGHAP


def test_gate1_rejection_survives_string_input() -> None:
    # 문자열 "NO"가 통과하면 제외 공고가 검수 큐에 쌓인다.
    with pytest.raises(ValueError, match="NO"):
        ReviewData(
            source_data_id=new_id(),
            run_id=new_id(),
            is_church_recruitment="NO",  # type: ignore[arg-type]
            confidence="low",  # type: ignore[arg-type]
            denomination_source="unknown",  # type: ignore[arg-type]
        )


def test_rejects_unknown_enum_string() -> None:
    with pytest.raises(ValueError, match="허용값 아님"):
        replace(_review_data(), confidence="very-high")  # type: ignore[arg-type]


def test_health_status_string_is_coerced() -> None:
    health = replace(_health_ok(), last_status="ZERO")  # type: ignore[arg-type]
    assert health.last_status is SourceHealthStatus.ZERO


def test_health_fail_string_still_requires_error() -> None:
    with pytest.raises(ValueError, match="last_error"):
        replace(_health_ok(), last_status="FAIL")  # type: ignore[arg-type]


# ── raw_meta: JSON 안전성 ────────────────────────────────────────


def test_raw_meta_rejects_non_json_value() -> None:
    # datetime을 넣으면 생성은 통과하고 저장(json.dump) 때 터진다 — fetch 비용을 쓴 뒤에.
    with pytest.raises(ValueError, match="JSON으로 저장할 수 없는"):
        replace(_source_data(), raw_meta={"when": FIXED_NOW})  # type: ignore[dict-item]


def test_raw_meta_deep_copies_nested_containers() -> None:
    inner: dict[str, JsonValue] = {"count": 1}
    outer: dict[str, JsonValue] = {"attach": inner, "tags": ["a"]}
    record = replace(_source_data(), raw_meta=outer)
    inner["count"] = 999
    tags = outer["tags"]
    assert isinstance(tags, list)
    tags.append("injected")
    assert dict(record.raw_meta) == {"attach": {"count": 1}, "tags": ["a"]}


def test_crawl_run_error_detail_rejects_non_string() -> None:
    with pytest.raises(ValueError, match="문자열"):
        CrawlRun(mode=CrawlMode.DAILY, started_at=FIXED_NOW, error_detail={"CSU": 500})  # type: ignore[dict-item]


# ── 실패 원인·재진입 ─────────────────────────────────────────────


def test_failed_attempt_records_reason() -> None:
    record = _source_data().with_failed_attempt("HTTP 429 RESOURCE_EXHAUSTED")
    assert record.last_structure_error == "HTTP 429 RESOURCE_EXHAUSTED"


def test_failed_attempt_requires_reason() -> None:
    # 원인 없는 실패는 상한 초과 리포트를 무의미하게 만든다.
    with pytest.raises(ValueError, match="비어있을 수 없음"):
        _source_data().with_failed_attempt("   ")


def test_verdict_clears_previous_error() -> None:
    record = _source_data().with_failed_attempt("일시 오류").with_verdict_recorded()
    assert record.last_structure_error is None
    assert record.structure_attempts == 2


def test_attempts_reset_requeues_exhausted_row() -> None:
    # 운영자가 원인(프롬프트·파서 버그)을 고친 뒤 다시 시도할 경로가 있어야 한다.
    record = _source_data()
    for _ in range(MAX_STRUCTURE_ATTEMPTS):
        record = record.with_failed_attempt("파싱 오류")
    assert record.exhausted_attempts is True
    requeued = record.with_attempts_reset()
    assert requeued.needs_restructure is True
    assert requeued.last_structure_error is None
    assert requeued.id == record.id


# ── 재구조화: 검수 상태 보존 ──────────────────────────────────────


def test_redraft_preserves_identity_and_review_state() -> None:
    # UNIQUE(source_data_id) upsert에서 새 초안을 그대로 쓰면 승인 상태가 PENDING으로 돌아간다.
    approved = replace(
        _review_data(),
        review_status=ReviewStatus.APPROVED,
        reviewed_by="operator@minjob",
        reviewed_at=FIXED_NOW,
        matched_church_id=new_id(),
        published_job_id=new_id(),
    )
    fresh = replace(_review_data(), title="다시 구조화한 제목")
    merged = fresh.carrying_review_state_of(approved)

    assert merged.id == approved.id  # admin 참조가 끊기지 않는다
    assert merged.created_at == approved.created_at  # 큐 정렬·감사 기준 유지
    assert merged.review_status is ReviewStatus.APPROVED
    assert merged.matched_church_id == approved.matched_church_id
    assert merged.published_job_id == approved.published_job_id
    assert merged.title == "다시 구조화한 제목"  # 새 구조화 결과는 반영


# ── SourceHealth: 소프트 실패(연속 0건) ──────────────────────────


def test_zero_runs_accumulate_for_soft_failure_alarm() -> None:
    # 응답은 200인데 신규가 계속 0이면 셀렉터 깨짐·로그인벽 전환 신호다(§7).
    state = SourceHealth.advance(
        previous=None, source_key="YTUS", run_at=FIXED_NOW, status=SourceHealthStatus.ZERO
    )
    state = SourceHealth.advance(
        previous=state,
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=1),
        status=SourceHealthStatus.ZERO,
    )
    assert state.consecutive_zero_runs == 2
    assert state.is_soft_failing is True


def test_new_postings_reset_zero_streak() -> None:
    zero = SourceHealth.advance(
        previous=None, source_key="YTUS", run_at=FIXED_NOW, status=SourceHealthStatus.ZERO
    )
    ok = SourceHealth.advance(
        previous=zero,
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=1),
        status=SourceHealthStatus.OK,
        new_count=5,
    )
    assert ok.consecutive_zero_runs == 0
    assert ok.is_soft_failing is False


def test_failure_holds_zero_streak() -> None:
    # 실패한 실행은 "0건"을 판정할 수 없으므로 카운터를 늘리지도 지우지도 않는다.
    zero = SourceHealth.advance(
        previous=None, source_key="YTUS", run_at=FIXED_NOW, status=SourceHealthStatus.ZERO
    )
    failed = SourceHealth.advance(
        previous=zero,
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=1),
        status=SourceHealthStatus.FAIL,
        error="timeout",
    )
    assert failed.consecutive_zero_runs == 1


def test_advance_records_reason_for_empty_exception_message() -> None:
    # str(TimeoutError())는 빈 문자열이다 — 그대로 넘기면 기록 코드가 크래시한다.
    failed = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=FIXED_NOW,
        status=SourceHealthStatus.FAIL,
        error=str(TimeoutError()),
    )
    assert failed.last_error == "FAIL"


def test_advance_rejects_error_on_success() -> None:
    # 부분 실패 상세는 crawl_run.error_detail에 남긴다 — 조용히 버리지 않는다.
    with pytest.raises(ValueError, match="error가 주어짐"):
        SourceHealth.advance(
            previous=None,
            source_key="YTUS",
            run_at=FIXED_NOW,
            status=SourceHealthStatus.OK,
            error="상세 3건 실패",
        )


# ── description 상한(가드레일 #3) ────────────────────────────────


def test_rejects_description_longer_than_summary_cap() -> None:
    # 원문 통째 복사를 레코드 차원에서 막는다.
    with pytest.raises(ValueError, match="요약"):
        replace(_review_data(), description="가" * (MAX_DESCRIPTION_CHARS + 1))


def test_accepts_description_at_cap() -> None:
    record = replace(_review_data(), description="가" * MAX_DESCRIPTION_CHARS)
    assert record.description is not None
