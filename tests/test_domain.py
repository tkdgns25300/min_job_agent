"""enum 드리프트 테스트.

CLAUDE.md: Python 이식으로 min_job과 코드를 공유하지 않게 되었으므로, enum 정합은
**CONTRACT §1 계약 + 이 테스트**로 지킨다. 값이 조용히 바뀌면 승격 시 min_job의
CHECK 제약에 걸리거나(런타임 실패) 잘못된 값이 공개된다.
허용값을 여기 하드코딩해 "코드가 바뀌면 테스트가 깨지도록" 못박는다.
"""

from __future__ import annotations

from enum import StrEnum

from minjob_ingest.domain import (
    PUBLISHABLE_DENOMINATIONS,
    Confidence,
    CrawlMode,
    Denomination,
    DenominationSource,
    Department,
    EmploymentType,
    Encoding,
    FetchTier,
    IsChurchRecruitment,
    JobKind,
    Position,
    Qualification,
    Region,
    ReviewStatus,
    SourceHealthStatus,
    StipendPeriod,
)


def _values(enum_type: type[StrEnum]) -> set[str]:
    return {member.value for member in enum_type}


# ── min_job 미러 (CONTRACT §1 · min_job DATA.md §2) ──────────────


def test_denomination_matches_contract() -> None:
    # 9대형 + ETC = 10키. KIJANG은 없다(기장 → ETC). UNKNOWN은 review 전용 임시값.
    assert _values(Denomination) == {
        "HAPDONG", "TONGHAP", "BAEKSEOK", "GAMLI", "SUNBOK",
        "BAPTIST", "SEONGGYUL", "GOSIN", "HAPSIN", "ETC",
        "UNKNOWN",
    }  # fmt: skip
    assert len(PUBLISHABLE_DENOMINATIONS) == 10
    assert "KIJANG" not in _values(Denomination)


def test_region_matches_data_md() -> None:
    assert _values(Region) == {
        "SEOUL", "GYEONGGI", "INCHEON", "GANGWON", "CHUNGBUK", "CHUNGNAM",
        "DAEJEON", "SEJONG", "GYEONGBUK", "GYEONGNAM", "DAEGU", "ULSAN",
        "BUSAN", "JEONBUK", "JEONNAM", "GWANGJU", "JEJU", "OVERSEAS",
    }  # fmt: skip


def test_position_matches_data_md() -> None:
    assert _values(Position) == {
        "SENIOR_PASTOR",
        "ASSOCIATE_PASTOR",
        "EVANGELIST",
        "LICENSED_MINISTER",
        "ETC",
    }


def test_department_matches_data_md() -> None:
    assert _values(Department) == {
        "INFANT", "CHILDREN", "YOUTH", "YOUNG_ADULT", "DISTRICT", "WORSHIP", "ADMIN", "ETC",
    }  # fmt: skip


def test_employment_type_matches_data_md() -> None:
    assert _values(EmploymentType) == {"FULL_TIME", "SEMI_FULL_TIME", "PART_TIME"}


def test_qualification_matches_data_md() -> None:
    assert _values(Qualification) == {"ANY", "ENTRY", "EXPERIENCED", "ORDAINED", "SEMINARIAN"}


def test_stipend_period_matches_data_md() -> None:
    assert _values(StipendPeriod) == {"MONTH", "YEAR"}


# ── 크롤러 전용 (SPEC §5·§6) ─────────────────────────────────────


def test_job_kind_matches_spec() -> None:
    assert _values(JobKind) == {"MINISTRY", "GENERAL"}


def test_is_church_recruitment_matches_spec() -> None:
    # SPEC §5.1: 불리언처럼 보이는 true/false 문자열을 쓰지 않는다.
    assert _values(IsChurchRecruitment) == {"YES", "NO", "UNCERTAIN"}


def test_denomination_source_matches_spec() -> None:
    # SPEC이 소문자로 규정한 값 — 대문자로 "정리"하면 계약 위반.
    # `operator`는 운영자가 검수에서 확정한 근거(SPEC §5.3의 "승격 전 10키로 해소").
    assert _values(DenominationSource) == {
        "stated",
        "registry",
        "ai_guess",
        "unknown",
        "operator",
    }


def test_review_status_matches_spec() -> None:
    assert _values(ReviewStatus) == {"PENDING", "APPROVED", "REJECTED"}


def test_confidence_matches_spec() -> None:
    assert _values(Confidence) == {"high", "medium", "low"}


def test_crawl_mode_matches_spec() -> None:
    assert _values(CrawlMode) == {"BACKFILL", "DAILY"}


def test_source_health_status_matches_spec() -> None:
    assert _values(SourceHealthStatus) == {"OK", "FAIL", "ZERO"}


def test_fetch_tier_matches_spec() -> None:
    assert _values(FetchTier) == {"static", "json", "headless"}


# ── 전송 규칙 ────────────────────────────────────────────────────


def test_encoding_codec_mapping() -> None:
    # EUC-KR 선언 보드는 cp949로 디코드한다(확장 한글에서 순정 코덱이 예외를 던진다).
    assert _values(Encoding) == {"utf-8", "euc-kr"}
    assert Encoding.UTF8.python_codec == "utf-8"
    assert Encoding.EUC_KR.python_codec == "cp949"


def test_every_encoding_member_has_a_codec() -> None:
    # 멤버를 늘리고 매핑을 잊으면 여기서 걸린다(조용히 utf-8로 떨어지지 않게).
    for member in Encoding:
        assert member.python_codec


def test_stored_values_are_ascii() -> None:
    # 저장값에 한글을 쓰지 않는다(CLAUDE.md Naming).
    for enum_type in (
        Denomination, Region, Position, Department, EmploymentType, Qualification,
        StipendPeriod, JobKind, IsChurchRecruitment, DenominationSource, ReviewStatus,
        Confidence, CrawlMode, SourceHealthStatus, FetchTier, Encoding,
    ):  # fmt: skip
        for value in _values(enum_type):
            assert value.isascii(), f"{enum_type.__name__}: {value!r}"
