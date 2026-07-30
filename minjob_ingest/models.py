"""스테이징 레코드 — SPEC §6의 4테이블.

필드명 = SPEC §6 컬럼명(snake_case)과 1:1이다. 그래야 Supabase 전환이 "그대로 INSERT"가
된다(CLAUDE.md 저장 seam). 값 변환은 store/serde가 담당하고, 여기는 **모양과 불변식**만 둔다.

- 전부 `frozen` — 갱신은 새 객체를 만들어 store를 통해 반영한다(아래 `with_*` 전이).
- `kw_only` — 필드가 많아 위치 인자로 만들면 조용히 뒤바뀐다.
- `__post_init__`은 **검증 + 정규화**를 한다(enum 변환·UTC 변환·공백 제거·컬렉션 고정).
  serde가 JSON에서 되읽을 때도 같은 생성자를 지나므로 경계 검증이 한 곳에서 끝난다.
  ⚠️ enum 필드를 문자열로 받아도 enum으로 바꾼다 — 안 하면 `is` 비교 불변식이
  읽기 경로에서 전부 무력화된다(`is_church_recruitment == "NO"`가 통과해버림).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from uuid import UUID, uuid4

from minjob_ingest.clock import ensure_utc, require_plain_date, utc_now
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
    ReviewStatus,
    SourceHealthStatus,
    StipendPeriod,
    normalize_source_key,
)

#: JSON으로 그대로 직렬화 가능한 값. `raw_meta`에 datetime·set이 들어가면 생성은 통과하고
#: 저장 시점(`json.dump`)에 터진다 — fetch 비용을 다 쓴 뒤에. 그래서 값까지 검증한다.
type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None

#: 구조화 재시도 상한(SPEC §4). 초과분은 재시도에서 빼고 운영자 리포트로 돌린다.
MAX_STRUCTURE_ATTEMPTS = 3

#: `description`은 **요약**이다(가드레일 #3). 원문 통째 복사를 레코드 차원에서 막는 상한.
MAX_DESCRIPTION_CHARS = 1_000

#: 교단 **값이 있어야 하는** 근거. `unknown`만 값 없음을 허용한다.
_SOURCES_REQUIRING_DENOMINATION = frozenset(
    {
        DenominationSource.STATED,
        DenominationSource.REGISTRY,
        DenominationSource.AI_GUESS,
        DenominationSource.OPERATOR,
    }
)

#: 운영자 확인 없이 **공개로 내보낼 수 있는** 근거.
#: `ai_guess`는 값이 있어도 확정이 아니다(SPEC §5.3).
_CONFIRMED_DENOMINATION_SOURCES = frozenset(
    {DenominationSource.STATED, DenominationSource.REGISTRY, DenominationSource.OPERATOR}
)

#: **운영자(min_job admin)가 쓰는 컬럼.** 재구조화 upsert가 덮어써선 안 되는 집합이며,
#: Supabase `ON CONFLICT (source_data_id) DO UPDATE`의 갱신 대상은 이 집합의 **여집합**이다.
#: `carrying_review_state_of`가 이 목록에서 파생된다(두 곳에 적어 어긋나지 않게).
REVIEW_STATE_FIELDS: tuple[str, ...] = (
    "id",
    "created_at",
    "review_status",
    "matched_church_id",
    "published_job_id",
    "reviewed_by",
    "reviewed_at",
)


def new_id() -> UUID:
    """레코드 id. DB의 `gen_random_uuid()`와 같은 역할을 애플리케이션에서 한다."""
    return uuid4()


def _require_non_empty(value: str, field_name: str) -> str:
    stripped = value.strip()
    if stripped == "":
        raise ValueError(f"{field_name}: 비어있을 수 없음")
    return stripped


def _require_non_negative(value: int, field_name: str) -> None:
    if value < 0:
        raise ValueError(f"{field_name}: 음수일 수 없음 ({value})")


def _as_enum[E: StrEnum](value: object, enum_type: type[E], field_name: str) -> E:
    """문자열(JSON에서 되읽은 값)도 enum으로 바꾼다 — `is` 비교 불변식을 살리기 위해."""
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as err:
            allowed = sorted(member.value for member in enum_type)
            raise ValueError(f"{field_name}: {value!r}는 허용값 아님 (허용 {allowed})") from err
    raise ValueError(f"{field_name}: {enum_type.__name__} 또는 문자열이어야 함 ({value!r})")


def _as_optional_enum[E: StrEnum](value: object, enum_type: type[E], field_name: str) -> E | None:
    return None if value is None else _as_enum(value, enum_type, field_name)


def as_json_value(value: object, where: str) -> JsonValue:
    """JSON으로 나갈 수 있는 값인지 재귀 검증하고, 컨테이너는 복사한다.

    복사 덕에 호출자가 나중에 원본을 바꿔도 "원문 증거" 레코드가 변조되지 않는다.
    """
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        # NaN·Infinity는 `json.dumps`가 그대로 뱉지만 **유효한 JSON이 아니고**
        # Postgres jsonb가 거부한다 → 저장 시점이 아니라 여기서 막는다.
        if not isfinite(value):
            raise ValueError(f"{where}: 유한한 수여야 함 ({value!r})")
        return value
    if isinstance(value, Mapping):
        snapshot: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{where}: 키는 문자열이어야 함 ({key!r})")
            snapshot[key] = as_json_value(item, f"{where}.{key}")
        return snapshot
    if isinstance(value, list | tuple):
        return [as_json_value(item, f"{where}[]") for item in value]
    raise ValueError(f"{where}: JSON으로 저장할 수 없는 값 ({type(value).__name__})")


def _freeze_json_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, JsonValue]:
    """읽기전용 스냅샷. serde는 `dict(...)`로 되꺼내 직렬화한다."""
    checked = as_json_value(value, field_name)
    if not isinstance(checked, dict):
        raise ValueError(f"{field_name}: 객체여야 함")
    return MappingProxyType(checked)


def _as_str_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, str]:
    snapshot: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{field_name}: 문자열 → 문자열 매핑이어야 함 ({key!r}: {item!r})")
        snapshot[key] = item
    return MappingProxyType(snapshot)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceData:
    """① 원자료 + 원장 (SPEC §6 ①).

    **원문 증거 필드**(`raw_text`·`image_urls`·`raw_meta`·`source_url`)는 write-once —
    갱신 경로를 두지 않는다. 예외는 운영자 opt-out·법적 삭제(가드레일 #4).
    **처리 상태 필드**(`structured_at`·`structure_attempts`·`last_structure_error`)만
    실행 중 갱신되며, 전이는 `with_verdict_recorded` / `with_failed_attempt`로만 한다.

    ⭐ `structured_at` = **판정이 끝난 시각**(게이트1 YES→review 생성, NO→제외).
    구조화가 **실패하면 None으로 남긴다** — 그래야 다음 run이 재구조화한다(SPEC §4).
    실패에도 시각을 찍으면 그 공고는 영구히 재시도되지 않는다.

    ⚠️ `raw_meta`는 읽기전용 스냅샷이라 `dataclasses.asdict`·`json.dumps`가 바로 먹지 않는다 —
    serde가 재귀 인코딩으로 평범한 dict로 바꿔 준다. 같은 이유로 이 레코드는 해시 불가이니
    집합·dict 키에는 `ledger_key`나 `id`를 쓴다.
    """

    source_key: str
    external_id: str
    source_url: str
    run_id: UUID
    fetched_at: datetime
    raw_text: str
    id: UUID = field(default_factory=new_id)
    #: 본문·첨부 이미지 URL. 구조화 직전 바이트 fetch용. 빈 튜플 = 이미지 없음.
    image_urls: tuple[str, ...] = ()
    #: 작성일·조회수·첨부 등 게시판 원필드(비정형).
    raw_meta: Mapping[str, JsonValue] = field(default_factory=dict)
    #: 판정 완료 시각. None = 미처리/실패 → 재구조화 대상.
    structured_at: datetime | None = None
    structure_attempts: int = 0
    #: 마지막 구조화 실패 원인. 상한 초과 리포트에서 "왜 실패했나"를 알려준다(SPEC §4).
    last_structure_error: str | None = None
    #: 수정/재게시 감지용(Phase 3). MVP 미채움.
    content_hash: str | None = None

    def __post_init__(self) -> None:
        _set(self, "source_key", normalize_source_key(self.source_key))
        _set(self, "external_id", _require_non_empty(self.external_id, "external_id"))
        _set(self, "source_url", _require_non_empty(self.source_url, "source_url"))
        _require_non_negative(self.structure_attempts, "structure_attempts")
        _set(self, "fetched_at", ensure_utc(self.fetched_at))
        if self.structured_at is not None:
            _set(self, "structured_at", ensure_utc(self.structured_at))
        _set(self, "image_urls", tuple(self.image_urls))
        _set(self, "raw_meta", _freeze_json_mapping(self.raw_meta, "raw_meta"))

    @property
    def has_verdict(self) -> bool:
        """구조화 판정이 끝났는가(게이트1 결과 무관). False면 재구조화 대상(SPEC §4)."""
        return self.structured_at is not None

    @property
    def needs_restructure(self) -> bool:
        """다음 run이 다시 구조화해야 하는가. 시도 상한을 넘긴 건 제외한다."""
        return not self.has_verdict and self.structure_attempts < MAX_STRUCTURE_ATTEMPTS

    @property
    def exhausted_attempts(self) -> bool:
        """상한까지 실패해 운영자 리포트로 넘길 대상인가."""
        return not self.has_verdict and self.structure_attempts >= MAX_STRUCTURE_ATTEMPTS

    @property
    def ledger_key(self) -> tuple[str, str]:
        """원장 유일키 — UNIQUE(source_key, external_id). 해시 가능한 식별자."""
        return (self.source_key, self.external_id)

    def with_verdict_recorded(self, at: datetime | None = None) -> SourceData:
        """판정 완료로 표시한다 — 게이트1 탈락(review 미생성)도 반드시 이걸 부른다."""
        return replace(
            self,
            structured_at=at if at is not None else utc_now(),
            structure_attempts=self.structure_attempts + 1,
            last_structure_error=None,
        )

    def with_failed_attempt(self, error: str) -> SourceData:
        """구조화 실패 — 시도 횟수와 원인만 남기고 `structured_at`은 None으로 둔다."""
        return replace(
            self,
            structure_attempts=self.structure_attempts + 1,
            last_structure_error=_require_non_empty(error, "error"),
        )

    def with_attempts_reset(self) -> SourceData:
        """운영자가 실패 원인을 고친 뒤 재시도 대상으로 되돌린다(상한 소진 행의 재진입 경로).

        ⚠️ 이 결과를 `Store.update_structure_state`로 저장할 수는 없다 — 그 경로는 시도 횟수
        감소를 거부한다(낡은 레코드로 판정을 지우는 사고를 막기 위해). 리셋은 운영자 리포트와
        함께 전용 store 메서드로 들어온다(ROADMAP 1-6).
        """
        return replace(self, structure_attempts=0, last_structure_error=None)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewData:
    """② 구조화 초안 + 검수 큐. 원자료 1건당 1행(UNIQUE source_data_id).

    게이트1 `NO`는 여기 오지 않는다 — `source_data.structured_at`만 기록하고 끝낸다(SPEC §5.1).
    `review_status`·`reviewed_*`·`denomination`은 **min_job admin도 쓰는 컬럼**이다 →
    운영자가 편집한 행을 되읽다가 크래시하지 않도록, 운영자 확정은 `denomination_source=operator`로
    표현한다(SPEC §5.3).
    """

    source_data_id: UUID
    run_id: UUID
    is_church_recruitment: IsChurchRecruitment
    confidence: Confidence
    denomination_source: DenominationSource
    id: UUID = field(default_factory=new_id)

    # 분류(게이트2)
    job_kind: JobKind | None = None
    #: GENERAL 대략 분류(방송·행정·시설 등). 통제 목록이 아니라 자유 텍스트.
    role: str | None = None

    # 공고 (min_job jobs 미러)
    title: str | None = None
    position: Position | None = None
    department: Department | None = None
    employment_type: EmploymentType | None = None
    qualification: Qualification | None = None
    housing_provided: bool | None = None
    #: 만원 단위(min_job DATA.md).
    stipend_min: int | None = None
    stipend_max: int | None = None
    #: "교회 내규에 따름" 등 비정형 표현을 원문 그대로 보존.
    stipend_note: str | None = None
    stipend_period: StipendPeriod | None = None
    work_days: str | None = None
    requirements: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()
    required_docs: tuple[str, ...] = ()
    #: **요약**이다. 원문 전문을 넣지 않는다(가드레일 #3 — 상한 `MAX_DESCRIPTION_CHARS`).
    description: str | None = None
    posted_at: date | None = None
    deadline: date | None = None

    # 교회 초안 (승인 시 churches로 매칭·생성)
    church_name: str | None = None
    region: Region | None = None
    city: str | None = None

    # 교단 — UNKNOWN은 여기서만 허용되는 임시값(승격 전 운영자가 해소)
    denomination: Denomination | None = None
    denomination_evidence: str | None = None
    raw_denomination: str | None = None

    #: 지원용으로 공개된 연락처만(가드레일 #4 — 원문 대조는 structure 층).
    contact: str | None = None

    heresy_flag: bool = False
    heresy_evidence: str | None = None

    # 검수 메타 — 승격 시 min_job으로 넘기지 않는다
    dedup_key: str | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING
    matched_church_id: UUID | None = None
    published_job_id: UUID | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self._coerce_enums()
        self._check_gate1()
        self._check_stipend()
        self._check_denomination()
        self._check_heresy()
        self._check_description()
        _set(self, "created_at", ensure_utc(self.created_at))
        if self.reviewed_at is not None:
            _set(self, "reviewed_at", ensure_utc(self.reviewed_at))
        if self.posted_at is not None:
            require_plain_date(self.posted_at)
        if self.deadline is not None:
            require_plain_date(self.deadline)
        _set(self, "requirements", tuple(self.requirements))
        _set(self, "preferred", tuple(self.preferred))
        _set(self, "required_docs", tuple(self.required_docs))

    def _coerce_enums(self) -> None:
        _set(
            self,
            "is_church_recruitment",
            _as_enum(self.is_church_recruitment, IsChurchRecruitment, "is_church_recruitment"),
        )
        _set(self, "confidence", _as_enum(self.confidence, Confidence, "confidence"))
        _set(
            self,
            "denomination_source",
            _as_enum(self.denomination_source, DenominationSource, "denomination_source"),
        )
        _set(self, "review_status", _as_enum(self.review_status, ReviewStatus, "review_status"))
        _set(self, "job_kind", _as_optional_enum(self.job_kind, JobKind, "job_kind"))
        _set(self, "position", _as_optional_enum(self.position, Position, "position"))
        _set(self, "department", _as_optional_enum(self.department, Department, "department"))
        _set(
            self,
            "employment_type",
            _as_optional_enum(self.employment_type, EmploymentType, "employment_type"),
        )
        _set(
            self,
            "qualification",
            _as_optional_enum(self.qualification, Qualification, "qualification"),
        )
        _set(
            self,
            "stipend_period",
            _as_optional_enum(self.stipend_period, StipendPeriod, "stipend_period"),
        )
        _set(self, "region", _as_optional_enum(self.region, Region, "region"))
        _set(
            self, "denomination", _as_optional_enum(self.denomination, Denomination, "denomination")
        )

    def _check_gate1(self) -> None:
        if self.is_church_recruitment is IsChurchRecruitment.NO:
            raise ValueError("게이트1 NO는 review_data를 만들지 않는다(SPEC §5.1)")
        # UNCERTAIN은 운영자 우선검토로 보내는 값이라 낮은 confidence여야 한다(SPEC §5.1).
        if (
            self.is_church_recruitment is IsChurchRecruitment.UNCERTAIN
            and self.confidence is not Confidence.LOW
        ):
            raise ValueError("게이트1 UNCERTAIN은 confidence=low여야 함(SPEC §5.1)")

    def _check_stipend(self) -> None:
        if self.stipend_min is not None:
            _require_non_negative(self.stipend_min, "stipend_min")
        if self.stipend_max is not None:
            _require_non_negative(self.stipend_max, "stipend_max")
        if (
            self.stipend_min is not None
            and self.stipend_max is not None
            and self.stipend_min > self.stipend_max
        ):
            raise ValueError(f"stipend_min({self.stipend_min}) > stipend_max({self.stipend_max})")

    def _check_denomination(self) -> None:
        """근거가 값을 요구하는데 비어 있으면 거부한다(SPEC §5.3).

        반대 방향(값이 있는데 근거가 `unknown`)은 **거부하지 않는다** — 운영자가 검수에서
        해소한 행이 `operator` 근거로 다시 들어오기 때문. 대신 `needs_operator_review`가
        `ai_guess`·미상을 계속 게이트로 잡는다.
        """
        has_value = self.denomination is not None and self.denomination is not Denomination.UNKNOWN
        if self.denomination_source in _SOURCES_REQUIRING_DENOMINATION and not has_value:
            raise ValueError(
                f"denomination_source={self.denomination_source.value}인데 교단이 비어 있음"
                f" ({self.denomination})"
            )

    def _check_heresy(self) -> None:
        if self.heresy_flag and (
            self.heresy_evidence is None or self.heresy_evidence.strip() == ""
        ):
            raise ValueError("heresy_flag=True면 heresy_evidence가 있어야 함(가드레일 #5)")

    def _check_description(self) -> None:
        if self.description is not None and len(self.description) > MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"description은 요약이어야 함 — {MAX_DESCRIPTION_CHARS}자 초과"
                f" ({len(self.description)}자, 가드레일 #3)"
            )

    @property
    def needs_operator_review(self) -> bool:
        """승격 전에 운영자가 교단을 확인해야 하는가(SPEC §5.3).

        `ai_guess`는 값이 있어도 확정이 아니므로 여기서 계속 걸린다.
        """
        return self.denomination_source not in _CONFIRMED_DENOMINATION_SOURCES

    @property
    def is_denomination_publishable(self) -> bool:
        """공개(`churches.denomination`)로 내보낼 수 있는 상태인가."""
        return (
            self.denomination is not None
            and self.denomination is not Denomination.UNKNOWN
            and not self.needs_operator_review
        )

    @property
    def is_operator_touched(self) -> bool:
        """운영자가 이 행을 손댔는가 — 재구조화가 덮어써도 되는지 판단하는 기준.

        `review_status`만 보면 부족하다. 운영자가 교단·교회명 등을 고쳐놓고 승인 전(PENDING)에
        멈춘 행을 재구조화가 AI 초안으로 되돌리면, **손으로 한 교정이 조용히 사라진다**
        (`reviewed_by`만 남아 "봤는데 고친 흔적이 없는" 모순 행이 된다).
        `denomination_source=operator`가 SPEC §5.3의 운영자 확정 표시다.
        """
        return (
            self.reviewed_by is not None or self.denomination_source is DenominationSource.OPERATOR
        )

    def carrying_review_state_of(self, previous: ReviewData) -> ReviewData:
        """재구조화 초안이 기존 행을 대체할 때 **식별자·검수 상태를 이어받는다**.

        SPEC §6 ②는 `UNIQUE(source_data_id)` upsert를 요구한다. 새 초안을 그대로 쓰면
        `id`·`created_at`이 새로 생기고 운영자 승인 상태(`review_status`·매칭·게재 링크)가
        지워진다 → admin 참조가 끊기고 승인이 PENDING으로 되돌아간다.
        """
        carried = {name: getattr(previous, name) for name in REVIEW_STATE_FIELDS}
        return replace(self, **carried)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceHealth:
    """③ 게시판별 상태. 매 실행 UPSERT.

    누적값(`consecutive_failures`·`consecutive_zero_runs`)과 `last_success_at` 보존에는
    직전 값이 필요하다 → store에서 읽어 `advance`로 이어붙인다(SPEC §6 ③).
    """

    source_key: str
    last_run_at: datetime
    last_status: SourceHealthStatus
    last_success_at: datetime | None = None
    last_new_count: int = 0
    consecutive_failures: int = 0
    #: 응답은 정상인데 신규 0건이 연속된 횟수 — §7 소프트 실패(셀렉터 깨짐·로그인벽 전환) 신호.
    consecutive_zero_runs: int = 0
    last_error: str | None = None

    def __post_init__(self) -> None:
        _set(self, "source_key", normalize_source_key(self.source_key))
        _set(self, "last_status", _as_enum(self.last_status, SourceHealthStatus, "last_status"))
        _require_non_negative(self.last_new_count, "last_new_count")
        _require_non_negative(self.consecutive_failures, "consecutive_failures")
        _require_non_negative(self.consecutive_zero_runs, "consecutive_zero_runs")
        _set(self, "last_run_at", ensure_utc(self.last_run_at))
        if self.last_success_at is not None:
            _set(self, "last_success_at", ensure_utc(self.last_success_at))
        if self.last_status is SourceHealthStatus.FAIL:
            if self.last_error is None:
                raise ValueError("last_status=FAIL이면 last_error가 있어야 함(원인 없는 실패 금지)")
            _set(self, "last_error", _require_non_empty(self.last_error, "last_error"))
        elif self.last_success_at is None:
            # OK·ZERO는 응답을 받은 상태이므로 성공 시각이 있어야 §7 경보가 의미를 갖는다.
            raise ValueError(f"last_status={self.last_status.value}면 last_success_at이 필요함")

    @property
    def is_soft_failing(self) -> bool:
        """응답은 오는데 신규가 계속 0인가 — 셀렉터 깨짐 의심(§7). 임계값은 runner가 정한다."""
        return self.consecutive_zero_runs > 0

    @classmethod
    def advance(
        cls,
        *,
        previous: SourceHealth | None,
        source_key: str,
        run_at: datetime,
        status: SourceHealthStatus,
        new_count: int = 0,
        error: str | None = None,
    ) -> SourceHealth:
        """직전 상태에 이번 실행 결과를 접어 다음 상태를 만든다.

        실패해도 `last_success_at`을 보존하고, 성공하면 연속 실패를 0으로 되돌린다 —
        이 규칙이 없으면 실패 1회로 마지막 성공 시각이 지워져 §7 경보가 무의미해진다.
        `ZERO`는 응답은 받았으니 성공 시각을 갱신하되 **연속 0건 카운터를 올린다**(소프트 실패).
        """
        checked_status = _as_enum(status, SourceHealthStatus, "status")
        failed = checked_status is SourceHealthStatus.FAIL
        if not failed and error is not None:
            raise ValueError(
                f"status={checked_status.value}인데 error가 주어짐 — 부분 실패 상세는"
                " crawl_run.error_detail에 남긴다"
            )
        previous_failures = previous.consecutive_failures if previous is not None else 0
        previous_zeros = previous.consecutive_zero_runs if previous is not None else 0
        previous_success = previous.last_success_at if previous is not None else None
        if checked_status is SourceHealthStatus.ZERO:
            zero_runs = previous_zeros + 1
        elif failed:
            zero_runs = previous_zeros  # 실패는 0건 판정 자체를 못 하므로 유지
        else:
            zero_runs = 0
        return cls(
            source_key=source_key,
            last_run_at=run_at,
            last_status=checked_status,
            last_success_at=previous_success if failed else run_at,
            last_new_count=new_count,
            consecutive_failures=previous_failures + 1 if failed else 0,
            consecutive_zero_runs=zero_runs,
            # 예외 메시지가 비는 경우(TimeoutError 등)에도 기록이 남아야 한다.
            last_error=(error or checked_status.value) if failed else None,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CrawlRun:
    """④ 실행별 요약. 시작에 INSERT(run_id 확보) → 종료에 집계 UPDATE(SPEC §6 ④)."""

    mode: CrawlMode
    started_at: datetime
    id: UUID = field(default_factory=new_id)
    finished_at: datetime | None = None
    sources_ok: int = 0
    sources_failed: int = 0
    new_count: int = 0
    #: source_key → 에러 메시지.
    error_detail: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _set(self, "mode", _as_enum(self.mode, CrawlMode, "mode"))
        _require_non_negative(self.sources_ok, "sources_ok")
        _require_non_negative(self.sources_failed, "sources_failed")
        _require_non_negative(self.new_count, "new_count")
        _set(self, "started_at", ensure_utc(self.started_at))
        if self.finished_at is not None:
            _set(self, "finished_at", ensure_utc(self.finished_at))
            if self.finished_at < self.started_at:
                raise ValueError("finished_at이 started_at보다 이를 수 없음")
        _set(self, "error_detail", _as_str_mapping(self.error_detail, "error_detail"))

    @property
    def is_finished(self) -> bool:
        return self.finished_at is not None

    def finish(
        self,
        *,
        at: datetime | None = None,
        sources_ok: int,
        sources_failed: int,
        new_count: int,
        error_detail: Mapping[str, str] | None = None,
    ) -> CrawlRun:
        """집계를 채워 종료 상태로 만든다. `id`는 유지된다(하위 레코드 FK가 유효해야 함)."""
        return replace(
            self,
            finished_at=at if at is not None else utc_now(),
            sources_ok=sources_ok,
            sources_failed=sources_failed,
            new_count=new_count,
            error_detail=error_detail if error_detail is not None else self.error_detail,
        )


def _set(record: object, field_name: str, value: object) -> None:
    """frozen 레코드의 정규화된 값을 `__post_init__`에서 심는다(슬롯 dataclass에서도 동작)."""
    object.__setattr__(record, field_name, value)
