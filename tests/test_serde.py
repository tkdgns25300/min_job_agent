"""레코드 ↔ 행 변환 테스트.

가장 중요한 것은 **모든 필드를 채운 레코드의 왕복 동일성**이다. 인코딩은 필드 순회로
자동이지만 디코딩은 명시적이라, 새 필드를 디코더에 빠뜨리면 조용히 유실된다 —
그 사고를 아래 왕복 테스트가 잡는다.
"""

from __future__ import annotations

import json
from dataclasses import MISSING, fields, replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from minjob_ingest.domain import (
    Confidence,
    CrawlMode,
    Denomination,
    DenominationSource,
    Department,
    EmploymentType,
    IsChurchRecruitment,
    JobKind,
    Position,
    Qualification,
    Region,
    RejectReason,
    ReviewStatus,
    SourceHealthStatus,
    StipendPeriod,
)
from minjob_ingest.models import (
    REVIEW_STATE_FIELDS,
    Attachment,
    CrawlRun,
    ReviewData,
    SourceData,
    SourceHealth,
    new_id,
)
from minjob_ingest.store.serde import (
    SerdeError,
    ledger_entry_of_row,
    ledger_key_of_row,
    row_to_crawl_run,
    row_to_review_data,
    row_to_source_data,
    row_to_source_health,
    to_row,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, 0, 123456, tzinfo=UTC)
FIXED_UUID = UUID("11111111-2222-3333-4444-555555555555")


def _full_source_data() -> SourceData:
    """선택 필드까지 **전부** 채운다 — 기본값으로 통과하는 왕복 테스트는 의미가 없다."""
    return SourceData(
        id=new_id(),
        source_key="YTUS",
        external_id="25553",
        source_url="https://www.ytus.ac.kr/board/view/trXXR/25553",
        title="오천중앙교회 부목사 청빙",
        posted_on=date(2026, 7, 28),
        run_id=new_id(),
        fetched_at=FIXED_NOW,
        raw_text="오천중앙교회에서 부목사님을 모십니다.",
        raw_html="<div><p>오천중앙교회에서 부목사님을 모십니다.</p>"
        '<a href="http://ocjc.or.kr">교회 홈페이지</a></div>',
        image_urls=("https://a/1.jpg", "https://a/2.jpg"),
        attachments=(Attachment(name="공고.hwp", url="https://a/dl/1"),),
        raw_meta={"views": 408, "attach": ["공고.hwp"], "nested": {"page": 1}, "flag": True},
        structured_at=FIXED_NOW + timedelta(minutes=3),
        structure_attempts=2,
        last_structure_error="HTTP 429",
        content_hash="sha256:abc",
    )


def _full_review_data() -> ReviewData:
    return ReviewData(
        source_url="https://www.ytus.ac.kr/board/view/trXXR/25553",
        id=new_id(),
        source_data_id=new_id(),
        run_id=new_id(),
        is_church_recruitment=IsChurchRecruitment.YES,
        confidence=Confidence.MEDIUM,
        denomination_source=DenominationSource.OPERATOR,
        # 사역직·일반직이 섞인 공고 — 배열 두 칸을 한 번에 왕복시킨다
        job_kind=(JobKind.MINISTRY, JobKind.GENERAL),
        role="음향",
        title="오천중앙교회 부목사 청빙",
        position=(Position.ASSOCIATE_PASTOR, Position.EVANGELIST),
        department=Department.YOUTH,
        employment_type=EmploymentType.FULL_TIME,
        qualification=Qualification.ORDAINED,
        headcount="1명",
        start_timing="협의",
        housing_provided=True,
        housing_note="사택 협의 가능",
        pay_min=250,
        pay_max=300,
        pay_note="교회 내규에 따름",
        pay_period=StipendPeriod.MONTH,
        benefit_note="4대보험 · 안식월",
        work_days="주 5일",
        requirements=("신대원 졸업", "면접"),
        preferred=("찬양 인도 경험",),
        required_docs=("이력서", "자기소개서"),
        optional_docs=("추천서",),
        process_steps=("서류", "면접", "설교"),
        description="청년부 담당 부목사를 청빙합니다.",
        posted_at=date(2026, 7, 22),
        deadline=date(2026, 8, 20),
        church_name="오천중앙교회",
        region=Region.GYEONGBUK,
        city="포항시",
        address="중앙로 12",
        denomination=Denomination.TONGHAP,
        denomination_evidence="대한예수교장로회(통합) / 전남노회",
        raw_denomination="예장통합",
        contact_email="ydw0403@example.com",
        contact_tel="054-123-4567",
        contact_link="https://ocjc.or.kr/apply",
        contact_post="경북 포항시 남구 오천읍 1-1",
        heresy_flag=True,
        heresy_evidence="heresy-ref: 교회명 일치",
        dedup_key="오천중앙교회|ASSOCIATE_PASTOR|250",
        review_status=ReviewStatus.REJECTED,
        reject_reason=RejectReason.DUPLICATE,
        matched_church_id=new_id(),
        published_job_id=new_id(),
        reviewed_by="operator@minjob",
        reviewed_at=FIXED_NOW,
        created_at=FIXED_NOW,
    )


def _full_source_health() -> SourceHealth:
    return SourceHealth(
        source_key="YTUS",
        last_run_at=FIXED_NOW,
        last_status=SourceHealthStatus.OK,
        first_run_at=FIXED_NOW - timedelta(days=30),
        last_run_id=FIXED_UUID,
        last_success_at=FIXED_NOW - timedelta(days=1),
        last_cutoff=date(2026, 5, 4),
        last_rows=18,
        last_new_count=8,
        last_posted_on=date(2026, 8, 4),
        consecutive_failures=0,
        consecutive_empty_runs=3,
        total_collected=41,
        last_error=None,
    )


def _full_crawl_run() -> CrawlRun:
    return CrawlRun(
        id=new_id(),
        mode=CrawlMode.BACKFILL,
        started_at=FIXED_NOW,
        finished_at=FIXED_NOW + timedelta(minutes=12),
        sources_ok=30,
        sources_failed=1,
        new_count=42,
        error_detail={"CSU": "세션 만료"},
    )


# ── 왕복 동일성: 필드가 하나라도 빠지면 실패한다 ──────────────────


def test_source_data_roundtrip_is_identical() -> None:
    original = _full_source_data()
    assert row_to_source_data(to_row(original)) == original


def test_review_data_roundtrip_is_identical() -> None:
    original = _full_review_data()
    assert row_to_review_data(to_row(original)) == original


def test_source_health_roundtrip_is_identical() -> None:
    original = _full_source_health()
    assert row_to_source_health(to_row(original)) == original


def test_crawl_run_roundtrip_is_identical() -> None:
    original = _full_crawl_run()
    assert row_to_crawl_run(to_row(original)) == original


def test_roundtrip_survives_json_text() -> None:
    # 실제 경로는 파일을 거친다 — dict 비교만 하면 직렬화 불가 값을 놓친다.
    original = _full_source_data()
    restored = row_to_source_data(json.loads(json.dumps(to_row(original))))
    assert restored == original


@pytest.mark.parametrize(
    "record",
    [_full_source_data(), _full_review_data(), _full_source_health(), _full_crawl_run()],
    ids=["source_data", "review_data", "source_health", "crawl_run"],
)
def test_row_covers_every_field(
    record: SourceData | ReviewData | SourceHealth | CrawlRun,
) -> None:
    # 행의 키 집합이 SPEC §6 컬럼(=레코드 필드)과 정확히 같아야 "그대로 INSERT"가 성립한다.
    assert set(to_row(record)) == {f.name for f in fields(record)}


@pytest.mark.parametrize(
    "record",
    [_full_source_data(), _full_review_data(), _full_source_health(), _full_crawl_run()],
    ids=["source_data", "review_data", "source_health", "crawl_run"],
)
def test_row_is_json_serializable(
    record: SourceData | ReviewData | SourceHealth | CrawlRun,
) -> None:
    assert json.loads(json.dumps(to_row(record))) == to_row(record)


# ── 인코딩 형태 ──────────────────────────────────────────────────


def test_encodes_uuid_as_string() -> None:
    record = _full_source_data()
    row = to_row(record)
    assert row["id"] == str(record.id)
    assert UUID(str(row["run_id"])) == record.run_id


def test_encodes_timestamp_with_the_kst_offset_and_microseconds() -> None:
    """저장 표기는 KST(`+09:00`)다 — 운영자가 파일을 열었을 때 한국 시간으로 읽힌다.

    ⚠️ `Z`와 `+09:00`은 **같은 순간의 다른 표기**이고 Postgres `timestamptz`는 둘을 동일하게
    저장한다. 바뀐 것은 표기뿐이다(2026-08-05 운영자 결정).
    """
    row = to_row(_full_source_data())
    assert row["fetched_at"] == "2026-07-29T21:00:00.123456+09:00"


def test_encodes_date_without_time() -> None:
    row = to_row(_full_review_data())
    assert row["posted_at"] == "2026-07-22"


def test_encodes_enum_as_its_value() -> None:
    row = to_row(_full_review_data())
    assert row["confidence"] == "medium"  # 소문자 SPEC 값 유지
    assert row["review_status"] == "REJECTED"
    assert row["reject_reason"] == "DUPLICATE"
    assert row["denomination_source"] == "operator"


def test_encodes_tuple_as_array_and_mapping_as_object() -> None:
    row = to_row(_full_source_data())
    assert row["image_urls"] == ["https://a/1.jpg", "https://a/2.jpg"]
    assert row["attachments"] == [{"name": "공고.hwp", "url": "https://a/dl/1"}]
    assert row["raw_meta"] == {
        "views": 408,
        "attach": ["공고.hwp"],
        "nested": {"page": 1},
        "flag": True,
    }


def test_encodes_none_for_unset_optionals() -> None:
    minimal = SourceData(
        source_key="YTUS",
        external_id="1",
        source_url="https://x/1",
        title="제목",
        posted_on=FIXED_NOW.date(),
        run_id=new_id(),
        fetched_at=FIXED_NOW,
        raw_text="본문",
    )
    row = to_row(minimal)
    assert row["structured_at"] is None
    assert row["content_hash"] is None
    assert row["image_urls"] == []


# ── 디코딩 거부: 누락·타입 불일치를 조용히 넘기지 않는다 ──────────


def test_missing_column_is_rejected_not_defaulted() -> None:
    # id를 빠뜨렸을 때 새 UUID를 만들면 원장·FK가 조용히 깨진다.
    row = dict(to_row(_full_source_data()))
    del row["id"]
    with pytest.raises(SerdeError, match="컬럼 집합 불일치"):
        row_to_source_data(row)


def test_missing_optional_column_is_still_rejected() -> None:
    # "값이 null"과 "컬럼이 없음"은 다르다 — 후자는 스키마 불일치다.
    row = dict(to_row(_full_source_data()))
    del row["content_hash"]
    with pytest.raises(SerdeError, match="content_hash"):
        row_to_source_data(row)


def test_unexpected_column_is_rejected() -> None:
    # 필드를 지웠는데 데이터가 남았거나, 디코더가 못 읽는 컬럼이 생긴 상황 → 조용히 넘기지 않는다.
    row = dict(to_row(_full_source_data()))
    row["legacy_flag"] = True
    with pytest.raises(SerdeError, match="잉여"):
        row_to_source_data(row)


def test_created_at_must_be_present() -> None:
    # 빠뜨리면 "지금"이 찍혀 큐 정렬·감사 기준이 조용히 바뀐다.
    row = dict(to_row(_full_review_data()))
    del row["created_at"]
    with pytest.raises(SerdeError, match="created_at"):
        row_to_review_data(row)


def test_rejects_malformed_uuid() -> None:
    row = dict(to_row(_full_source_data()))
    row["id"] = "not-a-uuid"
    with pytest.raises(SerdeError, match="UUID"):
        row_to_source_data(row)


def test_rejects_naive_timestamp() -> None:
    row = dict(to_row(_full_source_data()))
    row["fetched_at"] = "2026-07-29T12:00:00"
    with pytest.raises(SerdeError, match="fetched_at"):
        row_to_source_data(row)


def test_rejects_datetime_in_date_column() -> None:
    row = dict(to_row(_full_review_data()))
    row["posted_at"] = "2026-07-22T12:00:00Z"
    with pytest.raises(SerdeError, match="posted_at"):
        row_to_review_data(row)


def test_rejects_unknown_enum_value() -> None:
    row = dict(to_row(_full_review_data()))
    row["confidence"] = "very-high"
    with pytest.raises(SerdeError, match="허용값 아님"):
        row_to_review_data(row)


def test_rejects_bool_where_int_expected() -> None:
    # bool은 int의 서브클래스라 True가 1로 새어들면 집계가 틀어진다.
    row = dict(to_row(_full_source_data()))
    row["structure_attempts"] = True
    with pytest.raises(SerdeError, match="정수"):
        row_to_source_data(row)


def test_rejects_string_where_array_expected() -> None:
    # 문자열도 순회 가능해서 그냥 통과시키면 글자 단위로 쪼개진다.
    row = dict(to_row(_full_source_data()))
    row["image_urls"] = "https://a/1.jpg"
    with pytest.raises(SerdeError, match="배열"):
        row_to_source_data(row)


def test_attachments_must_be_a_list() -> None:
    row = dict(to_row(_full_source_data()))
    row["attachments"] = {"name": "공고.hwp", "url": "https://a/1"}
    with pytest.raises(SerdeError, match="배열"):
        row_to_source_data(row)


def test_attachment_entry_must_be_an_object() -> None:
    row = dict(to_row(_full_source_data()))
    row["attachments"] = ["공고.hwp"]
    with pytest.raises(SerdeError, match="객체"):
        row_to_source_data(row)


def test_attachment_requires_name_and_url() -> None:
    row = dict(to_row(_full_source_data()))
    row["attachments"] = [{"name": "공고.hwp"}]
    with pytest.raises(SerdeError, match="name·url"):
        row_to_source_data(row)


def test_attachment_rejects_non_string_fields() -> None:
    row = dict(to_row(_full_source_data()))
    row["attachments"] = [{"name": 1, "url": "https://a/1"}]
    with pytest.raises(SerdeError, match="name·url"):
        row_to_source_data(row)


def test_rejects_non_string_item_in_array() -> None:
    row = dict(to_row(_full_source_data()))
    row["image_urls"] = ["https://a/1.jpg", 42]
    with pytest.raises(SerdeError, match=r"image_urls\[1\]"):
        row_to_source_data(row)


def test_rejects_non_object_raw_meta() -> None:
    row = dict(to_row(_full_source_data()))
    row["raw_meta"] = ["views"]
    with pytest.raises(SerdeError, match="raw_meta"):
        row_to_source_data(row)


def test_rejects_non_string_error_detail_value() -> None:
    row = dict(to_row(_full_crawl_run()))
    row["error_detail"] = {"CSU": 500}
    with pytest.raises(SerdeError, match="error_detail"):
        row_to_crawl_run(row)


def test_rejects_blank_required_string() -> None:
    row = dict(to_row(_full_source_data()))
    row["source_key"] = "   "
    with pytest.raises(SerdeError, match="source_key"):
        row_to_source_data(row)


def test_allows_empty_raw_text_for_image_only_boards() -> None:
    # PCKWORLD처럼 본문이 이미지뿐인 보드는 빈 raw_text가 정상이다(config image_only).
    row = dict(to_row(_full_source_data()))
    row["raw_text"] = ""
    assert row_to_source_data(row).raw_text == ""


def test_encoding_rejects_unstorable_value() -> None:
    """레코드 생성자가 이미 막지만 인코딩 층도 스스로 방어한다.

    필드 타입이 늘어날 때 조용히 `json.dump` 단계에서 터지지 않도록,
    생성자를 우회해 심은 값도 여기서 걸리는지 확인한다.
    """
    record = _full_source_data()
    object.__setattr__(record, "content_hash", {1, 2})  # 생성자를 우회한 주입
    with pytest.raises(SerdeError, match="저장할 수 없는 값"):
        to_row(record)


# ── 레코드 불변식은 읽기 경로에서도 살아 있어야 한다 ───────────────


def test_record_invariants_apply_on_read_as_serde_error() -> None:
    """게이트1 NO는 review_data에 존재할 수 없다 — 저장돼 있었더라도 읽을 때 걸러야 한다.

    타입 불일치든 불변식 위반이든 **SerdeError 하나로** 나와야 store가
    "이 행만 격리"를 안전하게 구분할 수 있다(ValueError를 넓게 잡으면 store 버그까지 삼킨다).
    """
    row = dict(to_row(_full_review_data()))
    row["is_church_recruitment"] = "NO"
    with pytest.raises(SerdeError, match="NO"):
        row_to_review_data(row)


def test_operator_resolved_denomination_reads_back() -> None:
    # SPEC §5.3: 운영자가 확정한 행이 되읽혀도 크래시하면 안 된다.
    restored = row_to_review_data(to_row(_full_review_data()))
    assert restored.denomination_source is DenominationSource.OPERATOR
    assert restored.needs_operator_review is False


# ── 드리프트 방어: 픽스처가 실제로 "전부 채워졌는지" ──────────────


def _fields_left_at_default(record: object) -> set[str]:
    """기본값 그대로인 필드 목록. 왕복 테스트가 의미를 갖으려면 비어 있어야 한다."""
    left: set[str] = set()
    for f in fields(record):  # type: ignore[arg-type]
        current = getattr(record, f.name)
        if (f.default is not MISSING and current == f.default) or (
            f.default_factory is not MISSING and current == f.default_factory()
        ):
            left.add(f.name)
    return left


#: 픽스처에서 기본값을 유지하는 게 **더 현실적인** 필드(사유 명시).
_DEFAULT_ALLOWLIST = {
    # ZERO 상태는 실패가 없으므로 0·None이 정상이다(models.advance 규칙).
    "consecutive_failures",
    "last_error",
}


@pytest.mark.parametrize(
    "record",
    [_full_source_data(), _full_review_data(), _full_source_health(), _full_crawl_run()],
    ids=["source_data", "review_data", "source_health", "crawl_run"],
)
def test_fixture_is_actually_fully_populated(record: object) -> None:
    """왕복 테스트의 방어력은 픽스처 완전성에 달려 있다.

    선택 필드를 기본값으로 두면 디코더가 그 필드를 빠뜨려도 왕복이 통과한다 —
    그래서 "모든 선택 필드가 기본값과 다른가"를 여기서 못박는다.
    """
    left = _fields_left_at_default(record) - _DEFAULT_ALLOWLIST
    assert not left, f"픽스처가 기본값으로 남긴 필드(디코더 누락을 못 잡는다): {sorted(left)}"


def test_review_state_fields_are_all_in_review_data() -> None:
    # §6② upsert가 보존할 컬럼 목록 — 이름이 어긋나면 승인 상태가 조용히 날아간다.
    names = {f.name for f in fields(ReviewData)}
    assert set(REVIEW_STATE_FIELDS) <= names


# ── NaN·Infinity: 유효한 JSON이 아니고 jsonb가 거부한다 ───────────


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_raw_meta_rejects_non_finite_float(bad: float) -> None:
    # json.dumps는 NaN을 그대로 뱉지만 표준 JSON이 아니라 Supabase 적재에서 터진다.
    with pytest.raises(ValueError, match="유한한 수"):
        replace(_full_source_data(), raw_meta={"score": bad})


def test_raw_meta_accepts_finite_float() -> None:
    record = replace(_full_source_data(), raw_meta={"score": 1.5})
    assert row_to_source_data(to_row(record)) == record


# ── 원장 조회: 본문을 디코딩하지 않고 키만 ────────────────────────


def test_ledger_key_of_row_avoids_full_decode() -> None:
    record = _full_source_data()
    assert ledger_key_of_row(to_row(record)) == record.ledger_key


def test_ledger_key_of_row_rejects_missing_key() -> None:
    with pytest.raises(SerdeError, match="source_key"):
        ledger_key_of_row({"external_id": "1"})


def test_a_corrupt_array_enum_says_what_is_allowed() -> None:
    """⚠️ 배열 칸만 영어 기본 메시지가 나오면 손상 행을 고칠 때 원인을 못 읽는다."""
    row = to_row(_full_review_data())
    row["position"] = ["부목사"]

    with pytest.raises(SerdeError, match="허용값 아님"):
        row_to_review_data(row)


# ── 필수 날짜 ────────────────────────────────────────────────────


def test_a_row_without_a_posted_on_is_refused() -> None:
    """⚠️ 이 판정이 없으면 옛 행이 **유령**이 된다.

    원장 조회는 `posted_on: null`을 읽어 "이미 본 글"이라 하고 구조화 조회는 건너뛴다 —
    수집도 구조화도 되지 않는데 아무 경보도 울리지 않는다(2026-08-14 검수).
    """
    row = to_row(_full_source_data())
    row["posted_on"] = None

    with pytest.raises(SerdeError, match="posted_on"):
        row_to_source_data(row)


def test_a_draft_without_a_posted_at_is_refused() -> None:
    """min_job이 게시일로 만료를 판정한다 — 없으면 언제까지 보여줄지 정할 수 없다."""
    row = to_row(_full_review_data())
    row["posted_at"] = None

    with pytest.raises(SerdeError, match="posted_at"):
        row_to_review_data(row)


def test_a_ledger_entry_without_a_date_is_refused() -> None:
    """⚠️ 원장 조회도 같은 계약이어야 한다 — 한쪽만 느슨하면 그 행이 두 경로에서 갈린다."""
    row = to_row(_full_source_data())
    row["posted_on"] = None

    with pytest.raises(SerdeError, match="posted_on"):
        ledger_entry_of_row(row)
