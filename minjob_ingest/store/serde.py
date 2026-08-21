"""레코드 ↔ 저장 행(row) 변환.

행의 **키는 SPEC §6 컬럼명**이고 레코드 필드명과 1:1이라, Supabase 전환은 이 모듈의
값 변환만 남기고 "그대로 INSERT"가 된다(CLAUDE.md 저장 seam).

- **인코딩은 필드 순회로 자동** — 필드를 추가해도 이름을 따로 적을 필요가 없다.
- **디코딩은 명시적** — 어느 필드가 UUID·시각·날짜인지 알아야 문자열을 되돌릴 수 있다.
  필드를 추가하고 디코더에 빠뜨리면 컬럼 집합 검사(`_check_columns`)와 왕복 동일성
  테스트가 잡는다.
- 값이 빠졌으면 **기본값으로 얼버무리지 않고 예외**를 던진다 — `id`가 빠졌을 때 새 UUID를
  만들면 원장·FK가 조용히 깨진다. 컬럼 집합이 다르면(누락이든 잉여든) 거부한다.
- **행에서 레코드로 못 만드는 모든 경우는 `SerdeError` 하나로 나온다.** store는
  `SerdeError`=해당 행만 격리, 그 외 예외=버그(중단)로 구분한다 — 여기서 타입이 갈리면
  격리 코드가 `ValueError`를 넓게 잡아 store 자신의 버그까지 삼킨다.

**행 값 계약**: 전부 JSON 타입이다(uuid·timestamptz·date는 **문자열**). 따라서 Supabase
구현은 PostgREST(`supabase-py`)처럼 JSON을 주고받는 클라이언트여야 한다 — psycopg처럼
네이티브 `datetime`·`UUID`를 돌려주는 드라이버는 이 디코더가 거부한다.

⚠️ **Phase 1 스키마 진화**: 컬럼 집합을 엄격히 검사하므로, 백필 뒤에 필드를 추가하면 기존
`data/*.json`을 읽을 수 없다. 필드 추가는 **`data/` 일괄 재작성과 함께** 넣는다(마이그레이션은
스키마가 굳는 1-6까지 만들지 않으므로 이게 유일한 방어선이다).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from minjob_ingest.clock import parse_iso, parse_iso_date, to_iso, to_iso_date
from minjob_ingest.domain import (
    Confidence,
    CrawlMode,
    DedupState,
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
    Attachment,
    CrawlRun,
    JsonValue,
    ReviewData,
    SourceData,
    SourceHealth,
)
from minjob_ingest.store.base import LedgerEntry

#: 이 모듈이 다루는 레코드들.
type StagingRecord = SourceData | ReviewData | SourceHealth | CrawlRun

Row = Mapping[str, object]


class SerdeError(Exception):
    """행이 레코드 계약을 위반했을 때(누락·잉여·타입 불일치·불변식 위반).

    store는 이 예외만 "그 행을 격리"로 처리하고, 다른 예외는 버그로 보고 중단한다.
    """


# ── 인코딩 (레코드 → 행) ─────────────────────────────────────────


def to_row(record: StagingRecord) -> dict[str, JsonValue]:
    """레코드를 JSON에 바로 넣을 수 있는 dict로. 키 = SPEC §6 컬럼명."""
    return {f.name: _encode(getattr(record, f.name), f.name) for f in fields(record)}


def _encode(value: object, where: str) -> JsonValue:
    # StrEnum·datetime은 각각 str·date의 서브클래스라 **먼저** 걸러야 한다.
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return to_iso_date(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Attachment):
        return {"name": value.name, "url": value.url}
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {str(key): _encode(item, f"{where}.{key}") for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_encode(item, f"{where}[]") for item in value]
    raise SerdeError(f"{where}: 저장할 수 없는 값 ({type(value).__name__})")


# ── 디코딩 (행 → 레코드) ─────────────────────────────────────────


def row_to_source_data(row: Row) -> SourceData:
    _check_columns(row, SourceData)
    try:
        return SourceData(
            id=_uuid(row, "id"),
            source_key=_str(row, "source_key"),
            external_id=_str(row, "external_id"),
            source_url=_str(row, "source_url"),
            run_id=_uuid(row, "run_id"),
            fetched_at=_timestamp(row, "fetched_at"),
            raw_text=_str(row, "raw_text", allow_empty=True),
            raw_html=_str(row, "raw_html", allow_empty=True),
            title=_str(row, "title"),
            posted_on=_date(row, "posted_on"),
            image_urls=_str_tuple(row, "image_urls"),
            attachments=_attachments(row),
            raw_meta=_json_mapping(row, "raw_meta"),
            structured_at=_optional_timestamp(row, "structured_at"),
            structure_attempts=_int(row, "structure_attempts"),
            last_structure_error=_optional_str(row, "last_structure_error"),
            content_hash=_optional_str(row, "content_hash"),
        )
    except ValueError as err:  # 레코드 불변식 위반도 격리 가능한 한 종류로 모은다
        raise SerdeError(f"source_data: {err}") from err


def row_to_review_data(row: Row) -> ReviewData:
    _check_columns(row, ReviewData)
    try:
        return ReviewData(
            id=_uuid(row, "id"),
            source_data_id=_uuid(row, "source_data_id"),
            run_id=_uuid(row, "run_id"),
            source_url=_str(row, "source_url"),
            is_church_recruitment=_enum(row, "is_church_recruitment", IsChurchRecruitment),
            confidence=_enum(row, "confidence", Confidence),
            denomination_source=_enum(row, "denomination_source", DenominationSource),
            job_kind=_enum_tuple(row, "job_kind", JobKind),
            role=_optional_str(row, "role"),
            title=_optional_str(row, "title"),
            position=_enum_tuple(row, "position", Position),
            department=_optional_enum(row, "department", Department),
            employment_type=_optional_enum(row, "employment_type", EmploymentType),
            qualification=_optional_enum(row, "qualification", Qualification),
            headcount=_optional_str(row, "headcount"),
            start_timing=_optional_str(row, "start_timing"),
            housing_provided=_optional_bool(row, "housing_provided"),
            housing_note=_optional_str(row, "housing_note"),
            pay_min=_optional_int(row, "pay_min"),
            pay_max=_optional_int(row, "pay_max"),
            pay_note=_optional_str(row, "pay_note"),
            pay_period=_optional_enum(row, "pay_period", StipendPeriod),
            benefit_note=_optional_str(row, "benefit_note"),
            work_days=_optional_str(row, "work_days"),
            requirements=_str_tuple(row, "requirements"),
            preferred=_str_tuple(row, "preferred"),
            required_docs=_str_tuple(row, "required_docs"),
            optional_docs=_str_tuple(row, "optional_docs"),
            process_steps=_str_tuple(row, "process_steps"),
            description=_optional_str(row, "description"),
            posted_at=_date(row, "posted_at"),
            deadline=_optional_date(row, "deadline"),
            church_name=_optional_str(row, "church_name"),
            region=_optional_enum(row, "region", Region),
            city=_optional_str(row, "city"),
            address=_optional_str(row, "address"),
            denomination=_optional_enum(row, "denomination", Denomination),
            denomination_evidence=_optional_str(row, "denomination_evidence"),
            raw_denomination=_optional_str(row, "raw_denomination"),
            contact_email=_optional_str(row, "contact_email"),
            contact_tel=_optional_str(row, "contact_tel"),
            contact_link=_optional_str(row, "contact_link"),
            contact_post=_optional_str(row, "contact_post"),
            heresy_flag=_bool(row, "heresy_flag"),
            heresy_evidence=_optional_str(row, "heresy_evidence"),
            dedup_key=_optional_str(row, "dedup_key"),
            dedup_state=_optional_enum(row, "dedup_state", DedupState),
            review_status=_enum(row, "review_status", ReviewStatus),
            reject_reason=_optional_enum(row, "reject_reason", RejectReason),
            published_job_id=_optional_uuid(row, "published_job_id"),
            reviewed_by=_optional_str(row, "reviewed_by"),
            reviewed_at=_optional_timestamp(row, "reviewed_at"),
            review_note=_optional_str(row, "review_note"),
            poster_paths=_str_tuple(row, "poster_paths"),
            created_at=_timestamp(row, "created_at"),
        )
    except ValueError as err:
        raise SerdeError(f"review_data: {err}") from err


def row_to_source_health(row: Row) -> SourceHealth:
    _check_columns(row, SourceHealth)
    try:
        return SourceHealth(
            source_key=_str(row, "source_key"),
            last_run_at=_timestamp(row, "last_run_at"),
            last_status=_enum(row, "last_status", SourceHealthStatus),
            first_run_at=_timestamp(row, "first_run_at"),
            last_run_id=_optional_uuid(row, "last_run_id"),
            last_success_at=_optional_timestamp(row, "last_success_at"),
            last_cutoff=_optional_date(row, "last_cutoff"),
            last_rows=_int(row, "last_rows"),
            last_new_count=_int(row, "last_new_count"),
            last_posted_on=_optional_date(row, "last_posted_on"),
            consecutive_failures=_int(row, "consecutive_failures"),
            consecutive_empty_runs=_int(row, "consecutive_empty_runs"),
            total_collected=_int(row, "total_collected"),
            last_error=_optional_str(row, "last_error"),
        )
    except ValueError as err:
        raise SerdeError(f"source_health: {err}") from err


def row_to_crawl_run(row: Row) -> CrawlRun:
    _check_columns(row, CrawlRun)
    try:
        return CrawlRun(
            id=_uuid(row, "id"),
            mode=_enum(row, "mode", CrawlMode),
            started_at=_timestamp(row, "started_at"),
            finished_at=_optional_timestamp(row, "finished_at"),
            sources_ok=_int(row, "sources_ok"),
            sources_failed=_int(row, "sources_failed"),
            new_count=_int(row, "new_count"),
            error_detail=_str_mapping(row, "error_detail"),
        )
    except ValueError as err:
        raise SerdeError(f"crawl_run: {err}") from err


def ledger_key_of_row(row: Row) -> tuple[str, str]:
    """원장 조회용 키만 꺼낸다 — 본문 전체를 디코딩하지 않고 증분 판정을 할 수 있게.

    저장된 값은 인코더를 거쳤으므로 이미 정규화(대문자·공백 제거)되어 있다.
    """
    return (_str(row, "source_key"), _str(row, "external_id"))


# ── 필드 추출 헬퍼 — 누락·타입 불일치는 조용히 넘기지 않는다 ──────


def _check_columns(row: Row, record_type: type[StagingRecord]) -> None:
    """컬럼 집합이 레코드 필드와 정확히 같은지. 잉여 컬럼도 스키마 불일치로 본다."""
    expected = {f.name for f in fields(record_type)}
    actual = set(row)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise SerdeError(
            f"{record_type.__name__}: 컬럼 집합 불일치 (누락 {missing} · 잉여 {unexpected})"
        )


def _raw(row: Row, key: str) -> object:
    if key not in row:
        raise SerdeError(f"{key}: 컬럼 누락(기본값으로 대체하지 않는다)")
    return row[key]


def _str(row: Row, key: str, *, allow_empty: bool = False) -> str:
    value = _raw(row, key)
    if not isinstance(value, str):
        raise SerdeError(f"{key}: 문자열이어야 함 ({value!r})")
    if not allow_empty and value.strip() == "":
        raise SerdeError(f"{key}: 비어있을 수 없음")
    return value


def _optional_str(row: Row, key: str) -> str | None:
    value = _raw(row, key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SerdeError(f"{key}: 문자열 또는 null이어야 함 ({value!r})")
    return value


def _int(row: Row, key: str) -> int:
    value = _raw(row, key)
    # bool은 int의 서브클래스라 따로 막는다(True가 1로 새어들면 집계가 틀어진다).
    if isinstance(value, bool) or not isinstance(value, int):
        raise SerdeError(f"{key}: 정수여야 함 ({value!r})")
    return value


def _optional_int(row: Row, key: str) -> int | None:
    return None if _raw(row, key) is None else _int(row, key)


def _bool(row: Row, key: str) -> bool:
    value = _raw(row, key)
    if not isinstance(value, bool):
        raise SerdeError(f"{key}: true/false여야 함 ({value!r})")
    return value


def _optional_bool(row: Row, key: str) -> bool | None:
    return None if _raw(row, key) is None else _bool(row, key)


def _uuid(row: Row, key: str) -> UUID:
    value = _str(row, key)
    try:
        return UUID(value)
    except ValueError as err:
        raise SerdeError(f"{key}: UUID가 아님 ({value!r})") from err


def _optional_uuid(row: Row, key: str) -> UUID | None:
    return None if _raw(row, key) is None else _uuid(row, key)


def _timestamp(row: Row, key: str) -> datetime:
    value = _str(row, key)
    try:
        return parse_iso(value)
    except ValueError as err:
        raise SerdeError(f"{key}: {err}") from err


def _optional_timestamp(row: Row, key: str) -> datetime | None:
    return None if _raw(row, key) is None else _timestamp(row, key)


def _date(row: Row, key: str) -> date:
    """필수 날짜. 없으면 실패다 — 조용히 오늘로 채우면 그 행이 언제 글인지 영영 알 수 없다."""
    found = _optional_date(row, key)
    if found is None:
        raise SerdeError(f"{key}: 필수인데 비어 있음")
    return found


def _optional_date(row: Row, key: str) -> date | None:
    if _raw(row, key) is None:
        return None
    value = _str(row, key)
    try:
        return parse_iso_date(value)
    except ValueError as err:
        raise SerdeError(f"{key}: {err}") from err


def _enum[E: StrEnum](row: Row, key: str, enum_type: type[E]) -> E:
    value = _str(row, key)
    try:
        return enum_type(value)
    except ValueError as err:
        allowed = sorted(member.value for member in enum_type)
        raise SerdeError(f"{key}: {value!r}는 허용값 아님 (허용 {allowed})") from err


def _optional_enum[E: StrEnum](row: Row, key: str, enum_type: type[E]) -> E | None:
    return None if _raw(row, key) is None else _enum(row, key, enum_type)


def _str_tuple(row: Row, key: str) -> tuple[str, ...]:
    value = _raw(row, key)
    # 문자열도 순회 가능해서 그냥 통과시키면 글자 단위로 쪼개진다.
    if not isinstance(value, list | tuple):
        raise SerdeError(f"{key}: 배열이어야 함 ({value!r})")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise SerdeError(f"{key}[{index}]: 문자열이어야 함 ({item!r})")
        items.append(item)
    return tuple(items)


def _enum_tuple[E: StrEnum](row: Row, key: str, enum_type: type[E]) -> tuple[E, ...]:
    """여러 값을 담는 enum 칸(`job_kind`·`position`).

    중복 제거·정렬은 레코드가 한다(`ReviewData.__post_init__`). 허용값은 여기서 보되
    **형제 헬퍼와 같은 메시지**를 쓴다 — `_enum`이 만든 "허용 [...]" 안내가 배열 칸에서만
    영어 기본 메시지로 바뀌면 손상 행을 고칠 때 원인을 못 읽는다.
    """
    return tuple(_as_member(item, key, enum_type) for item in _str_tuple(row, key))


def _as_member[E: StrEnum](value: str, key: str, enum_type: type[E]) -> E:
    try:
        return enum_type(value)
    except ValueError as err:
        allowed = sorted(member.value for member in enum_type)
        raise SerdeError(f"{key}: {value!r}는 허용값 아님 (허용 {allowed})") from err


def _json_mapping(row: Row, key: str) -> Mapping[str, JsonValue]:
    """모양만 확인하고 넘긴다 — 값의 JSON 안전성·스냅샷은 레코드 생성자가 한 곳에서 한다."""
    value = _raw(row, key)
    if not isinstance(value, Mapping):
        raise SerdeError(f"{key}: 객체여야 함 ({value!r})")
    for map_key in value:
        if not isinstance(map_key, str):
            raise SerdeError(f"{key}: 키는 문자열이어야 함 ({map_key!r})")
    return MappingProxyType(dict(value))


def _str_mapping(row: Row, key: str) -> Mapping[str, str]:
    value = _raw(row, key)
    if not isinstance(value, Mapping):
        raise SerdeError(f"{key}: 객체여야 함 ({value!r})")
    snapshot: dict[str, str] = {}
    for map_key, item in value.items():
        if not isinstance(map_key, str) or not isinstance(item, str):
            raise SerdeError(f"{key}: 문자열 → 문자열 매핑이어야 함 ({map_key!r}: {item!r})")
        snapshot[map_key] = item
    return MappingProxyType(snapshot)


def ledger_entry_of_row(row: Row) -> LedgerEntry:
    """원장 대조용 두 컬럼만 꺼낸다 — 레코드 전체를 디코딩하지 않는다.

    목록 페이지당 수백 행을 훑으므로 `raw_text`·`attachments`까지 디코딩하면 낭비다.
    """
    _check_columns(row, SourceData)
    return LedgerEntry(title=_str(row, "title"), posted_on=_date(row, "posted_on"))


def _attachments(row: Row) -> tuple[Attachment, ...]:
    """`[{"name":…,"url":…}]` → `Attachment`들. 모양이 어긋나면 `SerdeError`(손상 행)."""
    value = row["attachments"]
    if not isinstance(value, list):
        raise SerdeError(f"attachments: 배열이어야 함 ({type(value).__name__})")
    parsed: list[Attachment] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SerdeError(f"attachments[{index}]: 객체여야 함")
        name, url = item.get("name"), item.get("url")
        if not isinstance(name, str) or not isinstance(url, str):
            raise SerdeError(f"attachments[{index}]: name·url이 문자열이어야 함")
        parsed.append(Attachment(name=name, url=url))
    return tuple(parsed)
