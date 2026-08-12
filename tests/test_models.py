"""레코드 불변식 테스트 — 잘못된 상태가 애초에 만들어지지 않는지(SPEC §6).

⚠️ 일부러 `**overrides: object` 헬퍼를 쓰지 않는다 — 그러면 호출부 타입이 지워져
mypy가 `fetched_at="문자열"` 같은 실수를 놓친다. 유효 레코드 하나를 만들고
`dataclasses.replace`로 필드를 바꿔 검증이 다시 돌게 한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from minjob_ingest.clock import kst_now
from minjob_ingest.domain import (
    Confidence,
    CrawlMode,
    Denomination,
    DenominationSource,
    IsChurchRecruitment,
    JobKind,
    Position,
    RejectReason,
    ReviewStatus,
    SourceHealthStatus,
)
from minjob_ingest.models import (
    MAX_STRUCTURE_ATTEMPTS,
    REVIEW_STATE_FIELDS,
    Attachment,
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
        title="오천중앙교회 부목사 청빙",
        posted_on=date(2026, 7, 28),
        run_id=new_id(),
        fetched_at=FIXED_NOW,
        raw_text="오천중앙교회에서 부목사님을 모십니다.",
    )


def _review_data() -> ReviewData:
    return ReviewData(
        source_url="https://www.ytus.ac.kr/board/view/trXXR/25553",
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
        first_run_at=FIXED_NOW,
        last_success_at=FIXED_NOW,
        last_rows=18,
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


def test_timestamps_are_normalized_to_kst() -> None:
    # 검사만 하고 원본을 저장하면 +09:00이 남아 날짜 경계 비교가 로컬시간으로 어긋난다.
    record = replace(_source_data(), fetched_at=datetime(2026, 7, 29, 21, 0, tzinfo=KST))
    assert record.fetched_at.utcoffset() == timedelta(hours=9)
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


def test_source_data_deduplicates_files_by_url() -> None:
    """같은 파일을 두 번 받으면 바이트 fetch와 Gemini 비용이 두 배다.

    **저장되는 레코드 한 곳에서만** 제거한다 — 어댑터·`RawPosting`은 페이지에 있는 대로
    보고하고, 두 곳에서 제거하면 한쪽을 지워도 다른 쪽이 가려 결함을 못 잡는다.
    """
    record = replace(
        _source_data(),
        image_urls=("https://x/a.jpg", "https://x/a.jpg", "https://x/b.jpg"),
        attachments=(
            Attachment(name="공고.hwp", url="https://x/dl/1"),
            Attachment(name="공고.hwp", url="https://x/dl/1"),
            Attachment(name="포스터.jpg", url="https://x/dl/2"),
        ),
    )
    assert record.image_urls == ("https://x/a.jpg", "https://x/b.jpg")
    assert [a.name for a in record.attachments] == ["공고.hwp", "포스터.jpg"]


def test_source_data_normalizes_files_to_tuples() -> None:
    """리스트로 넘겨도 튜플이 된다 — frozen 레코드가 호출자의 리스트 변경에 노출되면 안 된다."""
    record = replace(
        _source_data(),
        image_urls=["https://x/a.jpg"],  # type: ignore[arg-type]
        attachments=[Attachment(name="공고.hwp", url="https://x/dl/1")],  # type: ignore[arg-type]
    )
    assert isinstance(record.image_urls, tuple)
    assert isinstance(record.attachments, tuple)


def test_attachment_image_detection_uses_the_filename() -> None:
    """다운로드 URL에 확장자가 없어(`/download/…/57439f…`) 파일명으로만 판단할 수 있다."""
    assert Attachment(name="포스터.JPEG", url="https://x/dl/1").is_image is True
    assert Attachment(name="공고.hwp", url="https://x/dl/2").is_image is False
    assert Attachment(name="지원서.pdf", url="https://x/dl/3").is_image is False


def test_attachment_rejects_empty_name_or_url() -> None:
    with pytest.raises(ValueError, match=r"attachment\.name"):
        Attachment(name="  ", url="https://x/dl/1")
    with pytest.raises(ValueError, match=r"attachment\.url"):
        Attachment(name="공고.hwp", url="")


# ── ReviewData ───────────────────────────────────────────────────


def test_review_data_defaults_to_pending() -> None:
    record = _review_data()
    assert record.review_status.value == "PENDING"
    assert record.heresy_flag is False
    assert record.requirements == ()
    assert record.created_at.utcoffset() == timedelta(hours=9)


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


def test_rejects_inverted_pay_range() -> None:
    with pytest.raises(ValueError, match="pay_min"):
        replace(_review_data(), pay_min=300, pay_max=200)


def test_accepts_equal_pay_bounds() -> None:
    assert replace(_review_data(), pay_min=250, pay_max=250).pay_min == 250


def test_rejects_negative_pay() -> None:
    with pytest.raises(ValueError, match="음수"):
        replace(_review_data(), pay_min=-1)


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
    # 근거 없는 이단 플래그는 명예훼손 위험 — 판단은 사람이 한다.
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
        rows=20,
        new_count=8,
    )
    assert health.last_success_at == FIXED_NOW
    assert health.consecutive_failures == 0
    assert health.last_new_count == 8


def test_health_advance_failure_preserves_last_success() -> None:
    # 실패 1회로 마지막 성공 시각이 지워지면 §7 경보가 무의미해진다.
    ok = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=FIXED_NOW,
        status=SourceHealthStatus.OK,
        rows=18,
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
        rows=12,
        new_count=3,
    )
    assert recovered.consecutive_failures == 0
    assert recovered.last_error is None


def test_health_advance_empty_status_is_not_a_failure() -> None:
    # EMPTY = 응답은 받았는데 목록 행이 0. 실패로 세지 않되 성공 시각도 갱신하지 않는다.
    empty = SourceHealth.advance(
        previous=None, source_key="YTUS", run_at=FIXED_NOW, status=SourceHealthStatus.EMPTY
    )
    assert empty.consecutive_failures == 0
    assert empty.last_error is None
    assert empty.last_success_at is None  # 한 번도 목록을 읽은 적이 없다


def test_empty_runs_preserve_when_the_board_last_worked() -> None:
    """⚠️ `EMPTY`가 성공 시각을 갱신하면 0행이 이어질 때 그 값이 계속 오늘로 밀린다.

    그러면 경보가 "마지막 성공 = 오늘"이라고 말해 **"언제까지는 정상이었나"를 영구히 잃는다** —
    운영자가 게시판 개편 시점을 되짚을 수 없다.
    """
    working = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=FIXED_NOW,
        status=SourceHealthStatus.OK,
        rows=18,
        new_count=2,
    )
    broken = working
    for day in (1, 2, 3):
        broken = SourceHealth.advance(
            previous=broken,
            source_key="YTUS",
            run_at=FIXED_NOW + timedelta(days=day),
            status=SourceHealthStatus.EMPTY,
        )
    assert broken.last_success_at == FIXED_NOW  # 3일이 지나도 그날을 가리킨다
    assert broken.consecutive_empty_runs == 3


# ── CrawlRun ─────────────────────────────────────────────────────


def test_crawl_run_starts_unfinished() -> None:
    run = CrawlRun(mode=CrawlMode.DAILY, started_at=kst_now())
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
        CrawlRun(mode=CrawlMode.BACKFILL, started_at=kst_now(), sources_failed=-1)


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
        source_url="https://www.ytus.ac.kr/board/view/trXXR/25553",
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
            source_url="https://www.ytus.ac.kr/board/view/trXXR/25553",
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
    health = replace(_health_ok(), last_status="EMPTY", last_rows=0)  # type: ignore[arg-type]
    assert health.last_status is SourceHealthStatus.EMPTY


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


# ── SourceHealth: 소프트 실패(목록이 계속 빔) ────────────────────


def _empty(previous: SourceHealth | None, *, day: int = 0) -> SourceHealth:
    return SourceHealth.advance(
        previous=previous,
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=day),
        status=SourceHealthStatus.EMPTY,
    )


def test_empty_runs_accumulate_for_soft_failure_alarm() -> None:
    # 응답은 200인데 목록 행이 계속 0이면 셀렉터 깨짐·로그인벽 전환 신호다(§7).
    state = _empty(_empty(None), day=1)
    assert state.consecutive_empty_runs == 2
    assert state.is_soft_failing is True


def test_new_postings_reset_the_empty_streak() -> None:
    ok = SourceHealth.advance(
        previous=_empty(None),
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=1),
        status=SourceHealthStatus.OK,
        rows=18,
        new_count=5,
    )
    assert ok.consecutive_empty_runs == 0
    assert ok.is_soft_failing is False


def test_a_quiet_board_is_not_soft_failing() -> None:
    """⚠️ **신규 0건은 정상이다** — 원장이 이미 본 글을 걸러낸 결과다.

    이걸 소프트 실패로 세면 조용한 게시판들이 매일 경보를 울려 **경보가 잡음이 되고 정작
    깨진 게시판이 묻힌다**(그래서 판정 기준이 "신규 0"이 아니라 "목록 행 0"이다).
    """
    quiet = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=FIXED_NOW,
        status=SourceHealthStatus.OK,
        rows=18,  # 목록은 읽혔다
        new_count=0,  # 다 이미 본 글
    )
    assert quiet.consecutive_empty_runs == 0
    assert quiet.is_soft_failing is False


def test_failure_holds_the_empty_streak() -> None:
    # 실패한 실행은 "목록이 비었나"를 판정할 수 없으므로 카운터를 늘리지도 지우지도 않는다.
    failed = SourceHealth.advance(
        previous=_empty(None),
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=1),
        status=SourceHealthStatus.FAIL,
        error="timeout",
    )
    assert failed.consecutive_empty_runs == 1


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


# ── SourceData 불변식 ───────────────────────────────


def test_source_data_requires_a_title() -> None:
    """제목이 비면 운영자가 원자료 표에서 무슨 공고인지 알 수 없다."""
    with pytest.raises(ValueError, match="title"):
        replace(_source_data(), title="   ")


def test_source_data_rejects_datetime_as_posted_on() -> None:
    """`datetime`은 `date`의 서브클래스라 타입 검사·런타임 모두 통과한다 —
    date 컬럼에 시각이 섞이면 백필 컷오프 비교가 어긋난다."""
    with pytest.raises(ValueError, match="datetime"):
        replace(_source_data(), posted_on=datetime(2026, 8, 3, 12, 0, tzinfo=UTC))


def test_source_data_accepts_a_missing_posted_on() -> None:
    """목록에 날짜가 없는 게시판이 있다 — 그런 소스는 페이지 수로 범위를 정한다."""
    assert replace(_source_data(), posted_on=None).posted_on is None


# ── SourceHealth: 상태와 관측값이 어긋나는 것 ────────────────────


def test_ok_with_no_rows_is_rejected() -> None:
    """목록 행이 0인데 OK면 §7 경보가 그 게시판을 영구히 건너뛴다 — 그게 EMPTY의 존재 이유다."""
    with pytest.raises(ValueError, match="EMPTY"):
        replace(_health_ok(), last_rows=0, last_new_count=0)


def test_empty_with_rows_is_rejected() -> None:
    """`EMPTY`는 정의상 목록 행 0이다. 어긋나면 상태의 의미가 무너진다."""
    with pytest.raises(ValueError, match="목록 행 0"):
        replace(_health_ok(), last_status=SourceHealthStatus.EMPTY, last_rows=18)


def test_new_count_cannot_exceed_rows() -> None:
    """신규는 목록 행의 부분집합이다 — 어기면 rows 자리에 fresh를 넣은 배선 오류다."""
    with pytest.raises(ValueError, match="last_rows"):
        replace(_health_ok(), last_rows=3, last_new_count=8)


def test_first_run_cannot_be_later_than_last_run() -> None:
    with pytest.raises(ValueError, match="first_run_at"):
        replace(_health_ok(), first_run_at=FIXED_NOW + timedelta(days=1))


def test_failure_preserves_the_last_observation() -> None:
    """⚠️ 실패는 측정이 아니다 — 0으로 덮으면 실패 한 번이 "목록이 비었다"로 보인다.

    그러면 EMPTY(셀렉터 깨짐) 경보와 FAIL(접속 불가)이 구분되지 않는다.
    """
    ok = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=FIXED_NOW,
        status=SourceHealthStatus.OK,
        cutoff=date(2026, 5, 4),
        rows=258,
        new_count=227,
        posted_on=date(2026, 8, 4),
    )
    failed = SourceHealth.advance(
        previous=ok,
        source_key="YTUS",
        run_at=FIXED_NOW + timedelta(days=1),
        status=SourceHealthStatus.FAIL,
        error="HTTP 500",
    )
    assert failed.last_rows == 258  # 마지막으로 관측된 값
    assert failed.last_cutoff == date(2026, 5, 4)
    assert failed.last_posted_on == date(2026, 8, 4)
    assert failed.total_collected == 227  # 실패는 누적을 늘리지 않는다


def test_first_run_and_total_accumulate_across_runs() -> None:
    """`total_collected=0`이 "3일째"인지 "3개월째"인지 구분하려면 `first_run_at`이 필요하다."""
    state = SourceHealth.advance(
        previous=None,
        source_key="YTUS",
        run_at=FIXED_NOW,
        status=SourceHealthStatus.OK,
        rows=258,
        new_count=227,
    )
    for day in (1, 2):
        state = SourceHealth.advance(
            previous=state,
            source_key="YTUS",
            run_at=FIXED_NOW + timedelta(days=day),
            status=SourceHealthStatus.OK,
            rows=18,
            new_count=2,
        )
    assert state.first_run_at == FIXED_NOW  # 첫 실행 시각은 보존된다
    assert state.total_collected == 231


# ── 증거가 있는가 (`is_empty`) ───────────────────────────────────


def test_a_posting_with_text_is_not_empty() -> None:
    assert _source_data().is_empty is False


def test_whitespace_only_text_counts_as_empty() -> None:
    """게시판이 `<p>&nbsp;</p>`를 준다 — 공백만 남으면 증거가 없는 것이다(실측 YTUS 25309)."""
    assert replace(_source_data(), raw_text="\u00a0\n  ").is_empty is True


def test_an_image_only_posting_is_not_empty() -> None:
    """⚠️ **이걸 빈 것으로 세면 포스터형 게시판이 전량 실패한다**(PCKWORLD는 본문이 원래 0자다).

    `collect`가 "저장분이 전부 비었으면 소스 실패"로 판정하므로, 여기서 틀리면 그 게시판은
    한 건도 못 가져온다.
    """
    poster = replace(_source_data(), raw_text="", image_urls=("https://x.test/poster.jpg",))
    assert poster.is_empty is False


def test_an_attachment_only_posting_is_not_empty() -> None:
    """내용이 전부 HWP에 있는 공고가 있다(실측 WGST) — 첨부가 유일한 증거다."""
    withfile = replace(
        _source_data(),
        raw_text="",
        attachments=(Attachment(name="공고문.hwp", url="https://x.test/a.hwp"),),
    )
    assert withfile.is_empty is False


def test_a_posting_with_nothing_is_empty() -> None:
    assert replace(_source_data(), raw_text="").is_empty is True


def test_a_review_draft_cannot_exist_without_its_source_link() -> None:
    """⚠️ **`jobs.source_url`은 출처 표기의 핵심 필드다**(원문 재게시 금지 · 출처 표기).

    승격 코드가 `source_data`와의 JOIN을 잊으면 출처 없이 공개된다. 정규화상 중복이지만
    **빠지면 법적 문제가 되는 필드**라 우연에 맡기지 않고 타입으로 강제한다.
    """
    with pytest.raises(ValueError, match="source_url"):
        replace(_review_data(), source_url="")


def test_the_source_link_is_carried_on_the_draft() -> None:
    assert _review_data().source_url.startswith("https://")


# ── 거절 이유 (`reject_reason`) ──────────────────────────────────


def test_a_rejection_must_say_why() -> None:
    """⚠️ **자동 거부를 되짚는 유일한 통로다.**

    중복·이단·운영자 거절이 전부 `REJECTED` 하나로 뭉치면 "우리 dedup이 틀렸나"·"이단 오판인가"를
    확인할 방법이 없다. 특히 이단은 **검수 큐에 뜨지 않는 자동 거부**라(SPEC §5.4) 이유가 없으면
    잘못 걸러도 영원히 드러나지 않는다.
    """
    with pytest.raises(ValueError, match="reject_reason이 있어야"):
        replace(_review_data(), review_status=ReviewStatus.REJECTED)


def test_a_non_rejection_cannot_carry_a_reason() -> None:
    """승인된 행에 거절 이유가 남아 있으면 되읽을 때 모순이다."""
    with pytest.raises(ValueError, match="reject_reason이 있음"):
        replace(
            _review_data(),
            review_status=ReviewStatus.APPROVED,
            reject_reason=RejectReason.DUPLICATE,
        )


def test_each_rejection_reason_is_accepted() -> None:
    for reason in RejectReason:
        record = replace(_review_data(), review_status=ReviewStatus.REJECTED, reject_reason=reason)
        assert record.reject_reason is reason


def test_the_reason_survives_restructuring() -> None:
    """재구조화 upsert가 검수 상태를 덮으면 안 된다 — 이유도 상태의 일부다."""
    assert "reject_reason" in REVIEW_STATE_FIELDS


# ── 여러 값을 담는 분류 칸 (2026-08-11) ──────────────────────────


def _review(**overrides: object) -> ReviewData:
    base: dict[str, object] = {
        "source_data_id": new_id(),
        "run_id": new_id(),
        "source_url": "https://example.kr/1",
        "is_church_recruitment": IsChurchRecruitment.YES,
        "confidence": Confidence.LOW,
        "denomination_source": DenominationSource.UNKNOWN,
    }
    base.update(overrides)
    return ReviewData(**base)  # type: ignore[arg-type]


def test_duplicate_positions_collapse_to_one() -> None:
    """⚠️ `전임목사`·`교육목사`가 둘 다 `ASSOCIATE_PASTOR`로 겹치는 일이 흔하다.

    그대로 두면 같은 공고가 실행마다 다른 값을 갖고 `dedup_key`가 흔들린다(키에 이 칸이
    들어간다 · SPEC §4.1).
    """
    draft = _review(
        job_kind=(JobKind.MINISTRY,),
        position=(Position.ASSOCIATE_PASTOR, Position.ASSOCIATE_PASTOR, Position.EVANGELIST),
    )

    assert draft.position == (Position.ASSOCIATE_PASTOR, Position.EVANGELIST)


def test_position_order_is_fixed_regardless_of_input_order() -> None:
    """순서가 흔들리면 같은 공고가 다른 `dedup_key`를 갖는다."""
    forward = _review(
        job_kind=(JobKind.MINISTRY,), position=(Position.EVANGELIST, Position.SENIOR_PASTOR)
    )
    backward = _review(
        job_kind=(JobKind.MINISTRY,), position=(Position.SENIOR_PASTOR, Position.EVANGELIST)
    )

    assert forward.position == backward.position


def test_a_bare_string_is_not_a_position_list() -> None:
    """문자열도 순회 가능해서 그냥 통과시키면 글자 단위로 쪼개진다."""
    with pytest.raises(ValueError, match="목록이어야 함"):
        _review(job_kind=(JobKind.MINISTRY,), position="ASSOCIATE_PASTOR")


def test_a_mixed_posting_can_hold_both_a_position_and_a_role() -> None:
    """⚠️ `② 교육전도사 2명 ③ 관리직원 1명` 같은 공고는 이게 없으면 절반을 버려야 한다."""
    draft = _review(
        job_kind=(JobKind.MINISTRY, JobKind.GENERAL),
        position=(Position.EVANGELIST,),
        role="시설관리",
    )

    assert draft.job_kind == (JobKind.MINISTRY, JobKind.GENERAL)
    assert draft.role == "시설관리"


@pytest.mark.parametrize(
    ("job_kind", "position", "role", "reason"),
    [
        ((JobKind.MINISTRY,), (), None, "position이 어긋남"),
        ((JobKind.GENERAL,), (Position.EVANGELIST,), "음향", "position이 어긋남"),
        ((JobKind.MINISTRY,), (Position.EVANGELIST,), "음향", "role이 어긋남"),
        ((JobKind.GENERAL,), (), None, "role이 어긋남"),
    ],
    ids=[
        "사역직인데 직분 없음",
        "일반직인데 직분 박힘",
        "사역직인데 직무 있음",
        "일반직인데 직무 없음",
    ],
)
def test_job_kind_must_agree_with_position_and_role(
    job_kind: tuple[JobKind, ...],
    position: tuple[Position, ...],
    role: str | None,
    reason: str,
) -> None:
    """⚠️ 여기서 안 막으면 min_job DB만 막아 어긋난 초안이 **승격 시점에야** 터진다.

    그때는 판정이 이미 기록돼 재구조화 대상도 아니다 — 저장 전에 걸려야 그 공고 하나만
    실패하고 배치가 계속된다(min_job DATA.md §3 CHECK와 같은 규칙).
    """
    with pytest.raises(ValueError, match=reason):
        _review(job_kind=job_kind, position=position, role=role)


def test_an_unclassified_draft_is_allowed() -> None:
    """게이트2를 아직 안 돈 초안(1단계)은 모순이 아니라 "아직 판정 안 됨"이다."""
    draft = _review()

    assert draft.job_kind == ()
    assert draft.position == ()


def test_a_draft_without_a_kind_cannot_carry_a_position() -> None:
    with pytest.raises(ValueError, match="job_kind가 없는데"):
        _review(position=(Position.EVANGELIST,))
