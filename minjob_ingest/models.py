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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from uuid import UUID, uuid4

from minjob_ingest.clock import ensure_kst, kst_now, require_plain_date
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
    normalize_source_key,
)

#: JSON으로 그대로 직렬화 가능한 값. `raw_meta`에 datetime·set이 들어가면 생성은 통과하고
#: 저장 시점(`json.dump`)에 터진다 — fetch 비용을 다 쓴 뒤에. 그래서 값까지 검증한다.
type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None

#: 구조화 재시도 상한(SPEC §4). 초과분은 재시도에서 빼고 운영자 리포트로 돌린다.
MAX_STRUCTURE_ATTEMPTS = 3

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
    "reject_reason",
    "matched_church_id",
    "published_job_id",
    "reviewed_by",
    "reviewed_at",
)


def new_id() -> UUID:
    """레코드 id. DB의 `gen_random_uuid()`와 같은 역할을 애플리케이션에서 한다."""
    return uuid4()


def _unique_by_url(items: Iterable[Attachment]) -> tuple[Attachment, ...]:
    """URL 기준 중복 제거(순서 유지)."""
    unique: dict[str, Attachment] = {}
    for item in items:
        unique.setdefault(item.url, item)
    return tuple(unique.values())


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


def _as_enum_tuple[E: StrEnum](value: object, enum_type: type[E], field_name: str) -> tuple[E, ...]:
    """여러 값을 담는 enum 칸(`job_kind`·`position`).

    ⚠️ **중복을 제거하고 순서를 고정한다.** `전임목사·교육목사`가 둘 다 `ASSOCIATE_PASTOR`로
    겹치는 일이 흔한데, 그대로 두면 같은 공고가 실행마다 다른 값을 갖고 `dedup_key`가
    흔들린다(키에 이 칸이 들어간다 · SPEC §4.1). enum 정의 순서로 정렬해 항상 같게 만든다.
    """
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError(f"{field_name}: 여러 값을 담는 칸이다 — 목록이어야 함 ({value!r})")
    members = {_as_enum(item, enum_type, field_name) for item in value}
    order = list(enum_type)
    return tuple(sorted(members, key=order.index))


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
class Attachment:
    """첨부파일 하나. **이름을 함께 보관한다** — 확장자로 종류를 알아야 하고, 운영자가 검수에서
    "HWP를 열어봐야 하는 공고"를 알아볼 수 있어야 한다.

    URL만 저장하면 안 되는 이유: 이 게시판들의 다운로드 URL은 `/board/download/…/6337/57439f…`
    처럼 파일명을 담지 않는다(실측).
    """

    name: str
    url: str

    def __post_init__(self) -> None:
        _set(self, "name", _require_non_empty(self.name, "attachment.name"))
        _set(self, "url", _require_non_empty(self.url, "attachment.url"))

    @property
    def is_image(self) -> bool:
        """파일명이 이미지 확장자인가. **사실 판정일 뿐 "Gemini에 보낼 대상"이 아니다.**

        무엇을 멀티모달로 보낼지는 `pipeline/media.py`가 정한다 — 이 레코드는 파일 이름이
        말하는 것만 안다.
        """
        return self.name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))

    @property
    def is_pdf(self) -> bool:
        """파일명이 PDF인가. `is_image`와 마찬가지로 **사실 판정**이다.

        ⚠️ HWP·DOCX와 나눠 두는 이유: Gemini는 PDF를 직접 읽고 HWP는 못 읽는다. 이 구분이
        없으면 공고문이 PDF에만 있는 공고(실측 2건 — 본문 0자·24자)를 제목만 보고 판정한다.
        """
        return self.name.lower().endswith(".pdf")


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceData:
    """① 원자료 + 원장 (SPEC §6 ①).

    **원문 증거 필드**(`raw_text`·`title`·`posted_on`·`image_urls`·`attachments`·`raw_meta`·
    `source_url`)는 write-once —
    갱신 경로를 두지 않는다. 예외는 운영자 opt-out·법적 삭제.
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
    #: 게시판 목록의 제목 **그대로**. `review_data.title`은 여기서 머리표만 뗀 값이다
    #: (`pipeline/normalize.clean_title` — 모델을 거치지 않는다).
    #: 별도 컬럼인 이유: `raw_meta`에 묻어두면 운영자가 원자료 표를 열었을 때 무슨 공고인지
    #: 안 보이고, 원장 대조에서 Store가 어댑터의 키 이름을 알아야 한다(계층 침범).
    title: str
    run_id: UUID
    fetched_at: datetime
    raw_text: str
    #: 구조만 남긴 본문 HTML. **원문 증거**이고 `raw_text`를 대체하지 않는다 — 구조화는
    #: `raw_text`를 읽고, 이 값은 나중에 필요해진 것(링크 `href`·표 대응)을 **재수집 없이**
    #: 뽑는 자리다. 스타일·클래스·워드 주석을 걷어내 평균 818B다(2026-08-05 실측 27곳).
    raw_html: str = ""
    id: UUID = field(default_factory=new_id)
    #: 게시일. **필수다** — min_job의 `게시일+N개월 자동 만료`(SPEC §9) 기준이라 없으면 그
    #: 공고를 언제까지 보여줄지 정할 수 없다. 백필 컷오프(`--months N`)도 이 값을 본다(SPEC §4).
    #: ⚠️ 목록에 날짜 칸이 없는 게시판은 **어댑터가 채운다**(`PCKWORLD`는 썸네일 파일명).
    #: 그래도 없으면 `collect`가 오늘로 둔다 — 지어낸 과거 날짜보다 "오늘 처음 봤다"가 정직하다.
    posted_on: date
    #: **본문에 인라인으로 박힌** 이미지 URL. 구조화 직전 바이트 fetch용. 빈 튜플 = 없음.
    image_urls: tuple[str, ...] = ()
    #: **첨부파일 전부**(이름 + URL). 이미지만이 아니라 HWP·PDF도 담는다 — 원문 증거를 최대한
    #: 남기고, 구조화가 `Attachment.is_image`로 Gemini에 보낼 것을 고른다. 못 읽는 형식은
    #: 운영자 검수로 넘어간다(URL이 있으니 사람이 열 수 있다).
    attachments: tuple[Attachment, ...] = ()
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
        _set(self, "title", _require_non_empty(self.title, "title"))
        # `datetime`은 `date`의 서브클래스라 그냥 통과한다 → date 컬럼에 시각이 섞인다.
        _set(self, "posted_on", require_plain_date(self.posted_on))
        _require_non_negative(self.structure_attempts, "structure_attempts")
        _set(self, "fetched_at", ensure_kst(self.fetched_at))
        if self.structured_at is not None:
            _set(self, "structured_at", ensure_kst(self.structured_at))
        # 순서를 지키며 중복 제거 — 같은 파일을 두 번 받으면 바이트 fetch와 Gemini 비용이
        # 두 배다. **여기 한 곳에서만** 한다(어댑터·RawPosting은 있는 대로 보고한다).
        _set(self, "image_urls", tuple(dict.fromkeys(self.image_urls)))
        _set(self, "attachments", _unique_by_url(self.attachments))
        _set(self, "raw_meta", _freeze_json_mapping(self.raw_meta, "raw_meta"))

    @property
    def has_verdict(self) -> bool:
        """구조화 판정이 끝났는가(게이트1 결과 무관). False면 재구조화 대상(SPEC §4)."""
        return self.structured_at is not None

    @property
    def is_empty(self) -> bool:
        """구조화에 넣을 증거가 하나도 없는가(본문·이미지·첨부 전무).

        게시판에는 **내용 없이 올라온 글이 실제로 있다**(YTUS 25309 = `<p>&nbsp;</p>` · 실측).
        그건 수집 실패가 아니라 사실이므로 저장하되, 구조화는 이런 행에 Gemini를 호출하지
        않는다(빈 입력에 돈을 쓰는 것이고 결과는 게이트1 탈락이다).
        """
        return not self.raw_text.strip() and not self.image_urls and not self.attachments

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

    @property
    def label(self) -> str:
        """로그·리포트에 쓰는 사람이 읽는 식별자. **저장값이 아니다.**

        여기 두는 이유: 층마다 따로 조립하면 리포트와 진행 표시의 형식이 조용히 갈라진다.
        """
        return f"{self.source_key}/{self.external_id}"

    def with_verdict_recorded(self, at: datetime | None = None) -> SourceData:
        """판정 완료로 표시한다 — 게이트1 탈락(review 미생성)도 반드시 이걸 부른다."""
        return replace(
            self,
            structured_at=at if at is not None else kst_now(),
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
    #: 공고 원문 링크. **필수다** — `source_data.source_url`을 그대로 복사한다.
    #:
    #: ⚠️ 정규화상으로는 `source_data_id`로 JOIN하면 되니 중복이다. 그래도 복사하는 이유:
    #: min_job `jobs.source_url`은 **원문 재게시 금지·출처 표기의 핵심 필드**이고,
    #: 승격 코드가 JOIN을 잊으면 출처 없이 공개된다. 승격이 이 테이블 하나만 보고 끝나게 한다.
    source_url: str
    id: UUID = field(default_factory=new_id)

    # 분류(게이트2)
    #: ⚠️ **여러 개일 수 있다**(운영자 결정 2026-08-11). 한 글이 사역직과 일반직을 같이
    #: 뽑는 공고가 있는데(`② 교육전도사 2명 ③ 관리직원 1명`), 단일 값이면 **표현 자체가
    #: 불가능해 절반을 버려야 한다**. 빈 튜플 = 아직 판정 안 됨.
    job_kind: tuple[JobKind, ...] = ()
    #: GENERAL 대략 분류(방송·행정·시설 등). 통제 목록이 아니라 자유 텍스트.
    role: str | None = None

    # 공고 (min_job jobs 미러)
    title: str | None = None
    #: ⚠️ **여러 개일 수 있다**. 실측 954건이 직분을 2개 이상 적는데, 그중 826건은
    #: **한 자리에 자격만 여러 직분**이다(`전임사역자(전도사, 강도사, 목사)`). 대표 1개만
    #: 담으면 나머지 직분으로 검색한 지원자에게 안 보인다. 행을 쪼개는 대신 여기 다 담는다
    #: (쪼개면 실제로 없는 자리 826건이 공개된다 · ROADMAP 1-2).
    position: tuple[Position, ...] = ()
    department: Department | None = None
    employment_type: EmploymentType | None = None
    qualification: Qualification | None = None
    #: 모집 인원. **정수가 아니다** — "약간명"·"1~2명" 같은 비정형이 흔하다(min_job DATA.md).
    headcount: str | None = None
    #: 부임 시기. "즉시"·"협의"·"2월 중" 같은 비정형.
    start_timing: str | None = None
    #: 사택. **`None`이면 "언급 없음"이고 `False`(명시적 미제공)와 다르다** — 언급이 없는 것을
    #: 미제공으로 바꾸면 우리가 틀린 정보를 만든다(실측 언급률 40%).
    housing_provided: bool | None = None
    #: "사택 협의"·"보증금 지원" 등 비정형 표현. `pay_note`와 같은 역할.
    housing_note: str | None = None
    #: 만원 단위(min_job DATA.md). 화면 라벨은 `job_kind`로 갈린다(사례비/급여).
    pay_min: int | None = None
    pay_max: int | None = None
    #: "교회 내규에 따름" 등 비정형 표현을 원문 그대로 보존.
    pay_note: str | None = None
    pay_period: StipendPeriod | None = None
    #: 4대보험·교육비·안식월 등 그 외 처우.
    benefit_note: str | None = None
    work_days: str | None = None
    requirements: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()
    #: 제출 서류 — **필수**. 선택 서류는 `optional_docs`로 나눈다(min_job이 배열 2개로 받는다).
    required_docs: tuple[str, ...] = ()
    optional_docs: tuple[str, ...] = ()
    #: 전형 절차(서류→면접→설교…).
    process_steps: tuple[str, ...] = ()
    #: **요약**이다. 원문 전문을 넣지 않는다.
    #: ⚠️ **길이 상한을 두지 않는다**(운영자 결정 2026-08-11). 상한이 있으면 모델이 조금
    #: 넘겼을 때 그 공고가 실패하고, 재시도 상한을 넘겨 **조용히 사라진다**(`position`에서
    #: 같은 일이 있었다). 요약을 강제하는 자리는 **프롬프트**이고, 원문 재게시를 막는
    #: 최종 방어선은 **운영자 검수**다. 원문은 `source_data.raw_text`에 그대로 있다.
    description: str | None = None
    #: 게시일. **필수다** — min_job의 `게시일+N개월 자동 만료`(SPEC §9) 기준이다.
    #: `source_data.posted_on`을 그대로 물려받는다.
    posted_at: date
    deadline: date | None = None

    # 교회 초안 (승인 시 churches로 매칭·생성)
    church_name: str | None = None
    region: Region | None = None
    city: str | None = None

    # 교단 — UNKNOWN은 여기서만 허용되는 임시값(승격 전 운영자가 해소)
    denomination: Denomination | None = None
    denomination_evidence: str | None = None
    raw_denomination: str | None = None

    # 지원 연락처 — 지원용으로 **공개된 것만**(원문 대조는 structure 층).
    #
    # ⚠️ 대표 문자열 하나가 아니라 **방법별 컬럼 4개**다(min_job DATA.md 2026-08-05).
    # `APPLY_METHODS`가 `ETC` 없는 닫힌 4키라 컬럼이 1:1로 대응하고, 승격이 파싱 없이 그대로
    # INSERT한다. **승격 게이트는 이 넷 중 하나 이상**이며 `source_url`은 세지 않는다.
    contact_email: str | None = None
    contact_tel: str | None = None
    contact_link: str | None = None
    #: 우편·방문 접수처(주소).
    contact_post: str | None = None

    heresy_flag: bool = False
    heresy_evidence: str | None = None

    # 검수 메타 — 승격 시 min_job으로 넘기지 않는다
    dedup_key: str | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING
    #: `REJECTED`일 때 **왜**인지. 자동 거부(중복·이단)를 되짚는 유일한 통로다 —
    #: 구분이 없으면 "우리 dedup이 틀렸나"·"이단 오판인가"를 확인할 방법이 없다.
    reject_reason: RejectReason | None = None
    matched_church_id: UUID | None = None
    published_job_id: UUID | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime = field(default_factory=kst_now)

    def __post_init__(self) -> None:
        _set(self, "source_url", _require_non_empty(self.source_url, "source_url"))
        self._coerce_enums()
        self._check_gate1()
        self._check_pay()
        self._check_denomination()
        self._check_job_kind()
        self._check_heresy()
        self._check_reject_reason()
        _set(self, "created_at", ensure_kst(self.created_at))
        if self.reviewed_at is not None:
            _set(self, "reviewed_at", ensure_kst(self.reviewed_at))
        require_plain_date(self.posted_at)
        if self.deadline is not None:
            require_plain_date(self.deadline)
        for name in (
            "requirements",
            "preferred",
            "required_docs",
            "optional_docs",
            "process_steps",
        ):
            _set(self, name, tuple(getattr(self, name)))

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
        _set(
            self,
            "reject_reason",
            _as_optional_enum(self.reject_reason, RejectReason, "reject_reason"),
        )
        _set(self, "job_kind", _as_enum_tuple(self.job_kind, JobKind, "job_kind"))
        _set(self, "position", _as_enum_tuple(self.position, Position, "position"))
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
            "pay_period",
            _as_optional_enum(self.pay_period, StipendPeriod, "pay_period"),
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

    def _check_pay(self) -> None:
        if self.pay_min is not None:
            _require_non_negative(self.pay_min, "pay_min")
        if self.pay_max is not None:
            _require_non_negative(self.pay_max, "pay_max")
        if self.pay_min is not None and self.pay_max is not None and self.pay_min > self.pay_max:
            raise ValueError(f"pay_min({self.pay_min}) > pay_max({self.pay_max})")

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

    def _check_job_kind(self) -> None:
        """`job_kind` ↔ `position`/`role` 정합성(min_job DATA.md §3 CHECK와 같은 규칙).

        "사역직이면 직분이 있고, 아니면 직분도 없다" — **양방향**이다.

        ⚠️ 여기서 막지 않으면 min_job DB만 막게 되어 어긋난 초안이 **승격 시점에야** 터진다.
        그때는 이미 판정이 기록돼 재구조화 대상도 아니다 — 저장 전에 걸려야 그 공고 하나만
        실패하고 배치가 계속된다.

        ⚠️ 게이트2를 아직 안 돈 초안(`job_kind`가 빈 튜플)은 통과시킨다 — 1단계처럼 분류를
        뽑지 않는 패스가 있고, 그건 "아직 판정 안 됨"이지 모순이 아니다.
        """
        if not self.job_kind:
            if self.position or self.role is not None:
                raise ValueError("job_kind가 없는데 position·role이 있음")
            return
        if (JobKind.MINISTRY in self.job_kind) != bool(self.position):
            raise ValueError(
                f"job_kind={[k.value for k in self.job_kind]}와 position이 어긋남 "
                "(MINISTRY면 직분이 있어야 하고, 아니면 없어야 한다)"
            )
        if (JobKind.GENERAL in self.job_kind) != (self.role is not None):
            raise ValueError(
                f"job_kind={[k.value for k in self.job_kind]}와 role이 어긋남 "
                "(GENERAL이면 직무가 있어야 하고, 아니면 없어야 한다)"
            )

    def _check_heresy(self) -> None:
        if self.heresy_flag and (
            self.heresy_evidence is None or self.heresy_evidence.strip() == ""
        ):
            raise ValueError("heresy_flag=True면 heresy_evidence가 있어야 함")

    def _check_reject_reason(self) -> None:
        """거절이면 이유가 있어야 하고, 거절이 아니면 이유가 없어야 한다."""
        rejected = self.review_status is ReviewStatus.REJECTED
        if rejected and self.reject_reason is None:
            raise ValueError("review_status=REJECTED면 reject_reason이 있어야 함")
        if not rejected and self.reject_reason is not None:
            raise ValueError(
                f"review_status={self.review_status.value}인데 reject_reason이 있음"
                f" ({self.reject_reason.value})"
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

    @property
    def is_safe_to_replace(self) -> bool:
        """재구조화가 이 초안을 **버려도 되는가**.

        ⚠️ `is_operator_touched`만으로는 부족하다 — admin이 `reviewed_by`를 안 채운 채 승인만
        해도 `published_job_id`가 붙는데, 그 링크가 사라지면 이미 공개한 공고를 한 번 더
        승격하게 된다(SPEC §4.2가 그 값으로 끌어올림을 찾는다). **검수가 끝난 행도 지킨다.**

        ⚠️ 이 판정은 **한 곳에만 있어야 한다** — 저장(`JsonStore.upsert_review_data`)과
        되돌리기(`scripts/reset_structure.py`)가 서로 다른 기준을 쓰면 한쪽이 다른 쪽이
        지키기로 한 행을 지운다(실측 2026-08-14 검수).
        """
        return self.review_status is ReviewStatus.PENDING and not self.is_operator_touched

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
    """③ 게시판별 상태. 매 실행 UPSERT — **게시판 1곳 = 1행**(이력이 아니라 현재 상태다).

    누적값(`consecutive_failures`·`consecutive_empty_runs`·`total_collected`)과
    `last_success_at`·`first_run_at` 보존에는 직전 값이 필요하다 → store에서 읽어 `advance`로
    이어붙인다(SPEC §6 ③).

    ⚠️ **`last_*` 값들은 시점이 섞일 수 있다.** 실패한 실행은 아무것도 측정하지 못하므로
    마지막으로 *관측된* 값을 보존한다(그게 언제 것인지는 `last_success_at`이 말해준다).
    """

    source_key: str
    last_run_at: datetime
    last_status: SourceHealthStatus
    #: 이 게시판을 처음 훑은 시각. `total_collected=0`이 "3일째라 아직 없음"인지
    #: "3개월째인데 하나도 없음"인지 구분하는 데 필요하다.
    first_run_at: datetime
    #: 이 상태를 만든 실행(`crawl_run.id`). 실패를 봤을 때 그 실행으로 되짚는 유일한 연결이다.
    last_run_id: UUID | None = None
    #: ⚠️ **마지막으로 목록을 읽은 시각**(`OK`였던 실행). `EMPTY`·`FAIL`은 갱신하지 않는다 —
    #: 갱신하면 목록 0행이 며칠 이어질 때 "마지막 성공"이 계속 오늘로 밀려, 정작 필요한
    #: "언제까지는 정상이었나"를 영구히 잃는다. 한 번도 읽은 적이 없으면 `None`이다.
    last_success_at: datetime | None = None
    #: ⚠️ **이번에 훑은 기간의 시작**(게시일 컷오프). 이게 없으면 `last_rows`·`last_new_count`를
    #: 해석할 수 없다 — 3개월 백필 258행과 데일리 18행이 "급감"으로 보인다.
    last_cutoff: date | None = None
    #: 목록에서 읽은 행 수(범위 밖 포함). **0이면 목록 자체를 못 읽었다는 뜻**이다.
    last_rows: int = 0
    #: 그중 새로 저장한 건수. 데일리에서 0인 것은 **정상**이다(원장이 걸러낸 것).
    last_new_count: int = 0
    #: 컷오프 안에서 관측한 가장 최근 게시일. `None`이면 그 기간에 글이 없었다는 뜻이다.
    last_posted_on: date | None = None
    consecutive_failures: int = 0
    #: **목록 행이 0**인 실행이 연속된 횟수 — §7 소프트 실패(셀렉터 깨짐·로그인벽 전환) 신호.
    #: ⚠️ "신규 0건"이 아니다(그건 정상) — 이유는 `SourceHealthStatus` 참조.
    consecutive_empty_runs: int = 0
    #: 이 게시판에서 지금까지 저장한 누적 건수.
    total_collected: int = 0
    last_error: str | None = None

    def __post_init__(self) -> None:
        _set(self, "source_key", normalize_source_key(self.source_key))
        _set(self, "last_status", _as_enum(self.last_status, SourceHealthStatus, "last_status"))
        for name in (
            "last_rows",
            "last_new_count",
            "consecutive_failures",
            "consecutive_empty_runs",
            "total_collected",
        ):
            _require_non_negative(getattr(self, name), name)
        if self.last_new_count > self.last_rows:
            # 신규는 목록 행의 부분집합이다. 어기면 rows 자리에 fresh를 넣은 배선 오류다.
            raise ValueError(
                f"last_new_count({self.last_new_count})가 last_rows({self.last_rows})보다 큼"
            )
        _set(self, "last_run_at", ensure_kst(self.last_run_at))
        _set(self, "first_run_at", ensure_kst(self.first_run_at))
        if self.first_run_at > self.last_run_at:
            raise ValueError("first_run_at이 last_run_at보다 늦을 수 없음")
        if self.last_success_at is not None:
            _set(self, "last_success_at", ensure_kst(self.last_success_at))
        if self.last_cutoff is not None:
            _set(self, "last_cutoff", require_plain_date(self.last_cutoff))
        if self.last_posted_on is not None:
            _set(self, "last_posted_on", require_plain_date(self.last_posted_on))
        self._check_status_matches_rows()

    def _check_status_matches_rows(self) -> None:
        """상태와 행 수가 어긋나면 경보 판정이 무의미해진다 — 정의를 타입으로 못 박는다."""
        if self.last_status is SourceHealthStatus.FAIL:
            if self.last_error is None:
                raise ValueError("last_status=FAIL이면 last_error가 있어야 함(원인 없는 실패 금지)")
            _set(self, "last_error", _require_non_empty(self.last_error, "last_error"))
            return
        if self.last_status is SourceHealthStatus.OK and self.last_success_at is None:
            # OK는 정의상 이번에 목록을 읽었다 — 그 시각이 곧 마지막 성공이다.
            raise ValueError("last_status=OK면 last_success_at이 필요함")
        if self.last_status is SourceHealthStatus.EMPTY and self.last_rows != 0:
            raise ValueError(f"EMPTY는 목록 행 0을 뜻함 (last_rows={self.last_rows})")
        if self.last_status is SourceHealthStatus.OK and self.last_rows == 0:
            raise ValueError("목록 행이 0이면 OK가 아니라 EMPTY다")

    @property
    def is_soft_failing(self) -> bool:
        """응답은 오는데 **목록이 계속 비어 있나** — 셀렉터 깨짐 의심(§7).

        임계값은 runner가 정한다. ⚠️ 신규 0건으로 판정하지 않는다(그건 정상 · SPEC §7).
        """
        return self.consecutive_empty_runs > 0

    @classmethod
    def advance(
        cls,
        *,
        previous: SourceHealth | None,
        source_key: str,
        run_at: datetime,
        status: SourceHealthStatus,
        run_id: UUID | None = None,
        cutoff: date | None = None,
        rows: int = 0,
        new_count: int = 0,
        posted_on: date | None = None,
        error: str | None = None,
    ) -> SourceHealth:
        """직전 상태에 이번 실행 결과를 접어 다음 상태를 만든다.

        `OK`가 아닌 실행은 `last_success_at`을 **건드리지 않는다**(실패·목록 0행 모두). 이 규칙이
        없으면 한 번의 실패로 마지막 성공 시각이 지워지거나, 반대로 목록 0행이 이어질 때 성공
        시각이 계속 오늘로 밀려 "언제까지는 정상이었나"를 잃는다.
        `EMPTY`(목록 행 0)는 응답은 받았으니 실패로 세지 않되 **연속 카운터를 올린다**.

        ⚠️ **실패는 측정이 아니다** — `FAIL`이면 관측값(`cutoff`·`rows`·`new_count`·`posted_on`)을
        인자로 받지 않고 직전 값을 그대로 보존한다. 0으로 덮으면 실패 한 번이 "목록이 비었다"로
        보여 EMPTY 경보와 구분되지 않는다.
        """
        checked_status = _as_enum(status, SourceHealthStatus, "status")
        failed = checked_status is SourceHealthStatus.FAIL
        if not failed and error is not None:
            raise ValueError(
                f"status={checked_status.value}인데 error가 주어짐 — 부분 실패 상세는"
                " crawl_run.error_detail에 남긴다"
            )
        empty_runs = _next_empty_runs(previous, checked_status)
        observed = _Observation.of(
            previous,
            failed=failed,
            cutoff=cutoff,
            rows=rows,
            new_count=new_count,
            posted_on=posted_on,
        )
        return cls(
            source_key=source_key,
            last_run_at=run_at,
            last_status=checked_status,
            first_run_at=previous.first_run_at if previous is not None else run_at,
            last_run_id=run_id,
            last_success_at=(
                run_at
                if checked_status is SourceHealthStatus.OK
                else (previous.last_success_at if previous is not None else None)
            ),
            last_cutoff=observed.cutoff,
            last_rows=observed.rows,
            last_new_count=observed.new_count,
            last_posted_on=observed.posted_on,
            consecutive_failures=(
                (previous.consecutive_failures if previous else 0) + 1 if failed else 0
            ),
            consecutive_empty_runs=empty_runs,
            total_collected=(previous.total_collected if previous else 0) + new_count,
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
        _set(self, "started_at", ensure_kst(self.started_at))
        if self.finished_at is not None:
            _set(self, "finished_at", ensure_kst(self.finished_at))
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
            finished_at=at if at is not None else kst_now(),
            sources_ok=sources_ok,
            sources_failed=sources_failed,
            new_count=new_count,
            error_detail=error_detail if error_detail is not None else self.error_detail,
        )


@dataclass(frozen=True, slots=True)
class _Observation:
    """이번 실행이 관측한 값들. 실패면 직전 관측을 그대로 물려준다(0으로 덮지 않는다)."""

    cutoff: date | None
    rows: int
    new_count: int
    posted_on: date | None

    @classmethod
    def of(
        cls,
        previous: SourceHealth | None,
        *,
        failed: bool,
        cutoff: date | None,
        rows: int,
        new_count: int,
        posted_on: date | None,
    ) -> _Observation:
        if not failed:
            return cls(cutoff=cutoff, rows=rows, new_count=new_count, posted_on=posted_on)
        if previous is None:
            return cls(cutoff=None, rows=0, new_count=0, posted_on=None)
        return cls(
            cutoff=previous.last_cutoff,
            rows=previous.last_rows,
            new_count=previous.last_new_count,
            posted_on=previous.last_posted_on,
        )


def _next_empty_runs(previous: SourceHealth | None, status: SourceHealthStatus) -> int:
    """목록이 빈 실행의 연속 횟수. 실패는 판정 자체를 못 하므로 유지한다."""
    carried = previous.consecutive_empty_runs if previous is not None else 0
    if status is SourceHealthStatus.EMPTY:
        return carried + 1
    if status is SourceHealthStatus.FAIL:
        return carried
    return 0


def _set(record: object, field_name: str, value: object) -> None:
    """frozen 레코드의 정규화된 값을 `__post_init__`에서 심는다(슬롯 dataclass에서도 동작)."""
    object.__setattr__(record, field_name, value)
