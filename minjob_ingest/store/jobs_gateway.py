"""`jobs` 접근 — `PublishTarget` 프로토콜의 구현(SPEC §4.2·§4.2b·§4.3).

⚠️ **여기가 크롤러가 공개 테이블을 만지는 유일한 자리다.** 쓰는 것은 두 가지뿐이다 —
INSERT와 `posted_at` 한 칸 UPDATE. 그 외 모든 `jobs` 행은 읽기만 하고 `churches`에는 아예
접근하지 않는다(§8 소유권 경계).

⚠️ **지금은 DB가 그 경계를 강제하지 않는다.** 컬럼 단위 GRANT는 별도 `crawler` 롤이 와야 듣고
(운영자 결정 2026-08-21 · SPEC §8), 그때까지 유일한 방어선은 **이 파일이 `posted_at` 말고는
UPDATE할 경로를 갖지 않는 것**이다. 다른 칸을 고치는 메서드를 여기 추가하지 말 것 —
`bump_posted_at`이 보내는 값 집합은 테스트가 고정한다.

⚠️ **`jobs`는 min_job 소유다**(DATA.md 정본). 컬럼이 늘면 깨지는 곳이 공개 테이블이므로
`check_jobs_columns()`를 INSERT **전에** 부른다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from minjob_ingest.clock import parse_iso_date
from minjob_ingest.domain import Department, Position, Region
from minjob_ingest.models import JsonValue, ReviewData
from minjob_ingest.store.base import JobAnchor, StoreError
from minjob_ingest.store.postgrest import PostgrestClient, chunked, eq, in_values, is_null
from minjob_ingest.store.serde import to_row

_LOG = logging.getLogger(__name__)

_JOBS: Final = "jobs"
_REVIEW_DATA: Final = "review_data"

#: 상시모집(마감일 없음)이 목록에 남는 기간. **min_job `ALWAYS_OPEN_MAX_DAYS`의 미러**다
#: (DATA.md §6-1). ⚠️ 그쪽이 90→120으로 바꾸면 우리 앵커가 조용히 어긋난다 — 앵커는 정의상
#: "min_job 목록에 지금 보이는 것"이라 판정이 같아야 하고, 다르면 중복이 새거나 자리가 사라진다.
#: ⚠️ **뷰로 넘기지 않기로 했다**(운영자 결정 2026-08-22) — DB는 저장 전용이라 판정 규칙을
#: 거기 두지 않는다. 대신 이 사본을 `docs/REVIEW_PAGE.md` §12에 적어 min_job이 대조하게 하고,
#: 바뀌면 알려 달라고 요청해 두었다. **이 상수를 고칠 때 그 문서도 같이 고친다.**
ALWAYS_OPEN_MAX_DAYS: Final = 90

#: 목록에 보이는 상태. ⚠️ **화이트리스트다**(제외 목록이 아니다) — 그래서 min_job이 값을
#: 지워도(2026-08-21 `PENDING` 제거) 우리는 안전하고, 반대로 **보이는 값이 추가되면 조용히
#: 깨진다**(그 자리의 재게시를 새 공고로 공개해 같은 자리 2건이 뜬다). 지금 남는 값은
#: `OPEN`·`CLOSED` 둘뿐이라 "보이는 것 = OPEN"이 유일한 판정이다.
_VISIBLE_STATUS: Final = "OPEN"

#: 크롤러가 넣는 공고의 출처. ⚠️ `jobs.source`로는 우리 것인지 알 수 없다(운영자 수동 등록도
#: `OPERATOR`다) — 구분은 `review_data.published_job_id`로 한다(SPEC §8).
_CRAWLER_SOURCE: Final = "OPERATOR"

#: 이름이 같아 **그대로 복사**되는 칸(SPEC §6 승격 목적지). 매핑 표가 아니라 복사라서
#: `review_data`에 칸이 붙어도 여기 이름을 적지 않으면 새지 않는다.
_COPIED_COLUMNS: Final = (
    "title",
    "position",
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
    "benefit_note",
    "work_days",
    "requirements",
    "preferred",
    "required_docs",
    "optional_docs",
    "process_steps",
    "description",
    "deadline",
    "church_name",
    "region",
    "city",
    "address",
    "contact_email",
    "contact_tel",
    "contact_link",
    "contact_post",
    "job_kind",
    "role",
    "source_url",
)

#: 그대로 복사하지 않고 손보는 칸 — 아래 `_job_row`가 각각 이유를 적는다.
_ADJUSTED_COLUMNS: Final = ("denomination", "pay_period", "posted_at")

#: 승격 게이트가 통과했다면 채워져 있어야 하는 칸 = `jobs`의 NOT NULL(기본값 없음) 중 우리 몫.
#: ⚠️ 여기서 막는 이유: 비어 있으면 **공개 테이블에 대고** NOT NULL 위반이 난다. 게이트가
#: 먼저 걸러내지만(`pipeline/confidence`), 그 판정과 이 INSERT 사이에 재구조화가 끼면 값이
#: 빌 수 있다.
_REQUIRED_IN_JOBS: Final = ("church_name", "title", "description")

#: 지원 연락처 넷 중 **하나는** 있어야 한다(min_job `jobs_needs_contact`). ⚠️ `source_url`은
#: 세지 않는다 — 세면 크롤 공고는 항상 통과해 제약이 장식이 된다.
_CONTACT_COLUMNS: Final = ("contact_email", "contact_tel", "contact_link", "contact_post")

#: 우리가 이름으로 가리키는 `jobs` 컬럼 전부 — **쓰는 것 + 읽는 것**. 드리프트 검사의 기준이다.
#: ⚠️ `status`·`church_id`를 빼면 안 된다. INSERT에 쓰지 않아도 앵커 조회(`status`)와 끌어올림
#:    조건(`church_id`)이 그 이름으로 거른다 — 사라지면 **공개는 되는데 중복을 못 막는** 상태가
#:    되고, 그게 제일 나쁘다.
_TOUCHED_COLUMNS: Final = frozenset(
    (*_COPIED_COLUMNS, *_ADJUSTED_COLUMNS, "id", "church_id", "source", "status")
)

if set(_COPIED_COLUMNS) & set(_ADJUSTED_COLUMNS):  # pragma: no cover - 임포트 시 계약 검사
    raise RuntimeError("복사 칸과 손보는 칸이 겹친다 — 한 칸을 두 번 쓴다")


class SupabaseJobs:
    """`PublishTarget`의 PostgREST 구현. 전송은 `SupabaseStore`와 같은 클라이언트를 쓴다."""

    def __init__(self, client: PostgrestClient) -> None:
        self._client = client

    # ── 스키마 대조 (SPEC §4.3) ─────────────────────────────────

    def check_jobs_columns(self) -> None:
        actual = self._client.column_names(_JOBS)
        missing = sorted(_TOUCHED_COLUMNS - actual)
        if missing:
            raise StoreError(
                f"jobs 컬럼이 우리가 아는 모양과 다르다 (없는 칸: {', '.join(missing)})"
                " — min_job 스키마가 바뀐 것으로 보인다. 공개를 시작하지 않는다"
            )

    # ── 앵커 (SPEC §4.2) ────────────────────────────────────────

    def visible_anchors(
        self, *, today: date, exclude: frozenset[UUID] = frozenset()
    ) -> tuple[JobAnchor, ...]:
        # ⚠️ 노출 조건을 파이썬에서 판정한다. min_job DATA.md §6-1의 식을 **그대로 옮겨 놓고**
        #    한눈에 대조할 수 있게 두는 쪽이, PostgREST `or=(...)` 문법으로 흩어 놓는 것보다
        #    어긋남을 찾기 쉽다. 서버에서는 `status`만 좁힌다.
        rows = self._client.select(
            _JOBS,
            columns=(
                "id,church_name,region,position,role,department,posted_at,deadline,contact_email"
            ),
            order="id",
            filters={"status": eq(_VISIBLE_STATUS)},
        )
        anchors: list[JobAnchor] = []
        for row in rows:
            posted_at = _day(row, "posted_at")
            if not _is_visible(posted_at, _optional_day(row, "deadline"), today=today):
                continue
            job_id = _uuid(row, "id")
            if job_id in exclude:
                # 그 행은 후보에 이미 우리 초안으로 들어와 있다 — 자기 자신과 견주게 된다.
                continue
            anchor = _anchor_of(row, job_id=job_id, posted_at=posted_at)
            if anchor is not None:
                anchors.append(anchor)
        return tuple(anchors)

    # ── 공개 (SPEC §4.3) ────────────────────────────────────────

    def reserve_publication(self, review_data_id: UUID) -> UUID:
        job_id = uuid4()
        claimed = self._client.patch(
            _REVIEW_DATA,
            # 아직 안 나간 행만 잡는다 — 이미 값이 있으면 두 번 공개하려는 것이다.
            filters={"id": eq(str(review_data_id)), "published_job_id": is_null()},
            values={"published_job_id": str(job_id)},
        )
        if not claimed:
            raise StoreError(
                f"review_data {review_data_id}: 공개 자리를 잡지 못했다"
                " — 이미 공개됐거나 그 초안이 없다"
            )
        return job_id

    def publish(self, draft: ReviewData, *, job_id: UUID, posted_at: date) -> None:
        self._client.insert(_JOBS, [_job_row(draft, job_id=job_id, posted_at=posted_at)])

    def bump_posted_at(self, job_id: UUID, posted_at: date) -> bool:
        # ⚠️ **`posted_at` 하나만 보낸다.** 제목·연락처·마감일·상태는 운영자·교회의 몫이다(§8).
        #    `updated_at`도 건드리지 않는다 — 그 칸은 min_job의 Server Action이 쓴다.
        changed = self._client.patch(
            _JOBS,
            # 교회가 claim하면 소유권이 넘어가고 크롤러는 손을 뗀다 — 조건을 DB가 판정한다.
            filters={"id": eq(str(job_id)), "church_id": is_null()},
            values={"posted_at": posted_at.isoformat()},
        )
        return bool(changed)

    def published_state(self, job_ids: Sequence[UUID]) -> Mapping[UUID, date]:
        if not job_ids:
            return {}
        state: dict[UUID, date] = {}
        for chunk in chunked(sorted(str(job_id) for job_id in job_ids)):
            for row in self._client.select(
                _JOBS,
                columns="id,posted_at",
                order="id",
                filters={"id": in_values(list(chunk))},
            ):
                state[_uuid(row, "id")] = _day(row, "posted_at")
        return state

    def count_jobs(self) -> int:
        return self._client.count(_JOBS)

    def release_publication(self, review_data_id: UUID, job_id: UUID) -> None:
        released = self._client.patch(
            _REVIEW_DATA,
            # 우리가 적어둔 그 값일 때만 비운다 — 그 사이 다른 값이 들어왔으면 손대지 않는다.
            filters={"id": eq(str(review_data_id)), "published_job_id": eq(str(job_id))},
            values={"published_job_id": None},
        )
        if not released:
            _LOG.info("공개 링크가 이미 바뀌어 비우지 않았다 (review_data=%s)", review_data_id)


# ── 행 만들기 ──────────────────────────────────────────────────


def _job_row(draft: ReviewData, *, job_id: UUID, posted_at: date) -> Mapping[str, JsonValue]:
    """초안 → `jobs` 한 행. 손보는 칸은 각각 이유가 있다."""
    _check_publishable(draft)
    source = to_row(draft)
    row: dict[str, JsonValue] = {name: source[name] for name in _COPIED_COLUMNS}
    row["id"] = str(job_id)
    # 교회 행은 만들지 않는다 — 교회가 claim할 때 채워진다(§8).
    row["church_id"] = None
    row["source"] = _CRAWLER_SOURCE
    # ⚠️ 묶음의 **가장 최근 게시일**이다(§4.1) — 초안의 값이 아니다.
    row["posted_at"] = posted_at.isoformat()
    # ⚠️ `UNKNOWN`·`ai_guess`는 내보내지 않는다 — 전자는 `jobs` CHECK가 거부하고 후자는 확정이
    #    아니다(SPEC §5.3). 레코드가 그 판정을 갖고 있다.
    publishable = draft.denomination_for_publish
    row["denomination"] = None if publishable is None else publishable.value
    # ⚠️ 비어 있으면 **칸을 아예 보내지 않는다.** `jobs.pay_period`는 NOT NULL DEFAULT 'MONTH'라
    #    NULL을 보내면 거부되고, 우리가 임의로 'MONTH'를 넣으면 연봉이 월급으로 공개된다.
    if draft.pay_period is not None:
        row["pay_period"] = draft.pay_period.value
    return row


def _check_publishable(draft: ReviewData) -> None:
    """`jobs`의 NOT NULL·CHECK를 **보내기 전에** 확인한다.

    승격 게이트(`pipeline/confidence`)가 먼저 걸러내지만, 그 판정과 이 INSERT 사이에
    재구조화가 끼면 값이 빌 수 있다. 그때 깨지는 곳이 **공개 테이블**이므로 여기서 멈춘다.
    """
    blank = [name for name in _REQUIRED_IN_JOBS if not getattr(draft, name)]
    if blank:
        raise StoreError(f"review_data {draft.id}: 공개에 필요한 칸이 비었다 ({', '.join(blank)})")
    if not draft.job_kind:
        # min_job `jobs_kind_matches_seat` — 빈 배열은 거부된다(게이트2를 안 돈 초안이다).
        raise StoreError(f"review_data {draft.id}: job_kind가 없어 공개할 수 없다")
    if not any(getattr(draft, name) for name in _CONTACT_COLUMNS):
        # min_job `jobs_needs_contact` — "어디로 지원하나"를 알 수 없는 공고는 공개할 값이 없다.
        raise StoreError(f"review_data {draft.id}: 지원 연락처가 없어 공개할 수 없다")


def _is_visible(posted_at: date, deadline: date | None, *, today: date) -> bool:
    """min_job DATA.md §6-1 "공개 목록에 뜬다"의 미러.

    ```
    deadline ≠ null  ?  deadline >= today
                     :  posted_at + ALWAYS_OPEN_MAX_DAYS >= today
    ```
    ⚠️ **마감일이 있으면 `posted_at`을 보지 않는다.** 둘을 AND로 걸면 게시일이 오래됐지만 마감일이
    남은 공고를 앵커에서 빠뜨려, min_job은 보여주는데 우리는 "안 보인다"고 판단해 **같은 자리가
    두 번 공개된다**(2026-08-21 정정 · SPEC §4.2).
    """
    if deadline is not None:
        return deadline >= today
    return posted_at + timedelta(days=ALWAYS_OPEN_MAX_DAYS) >= today


def _anchor_of(row: Mapping[str, JsonValue], *, job_id: UUID, posted_at: date) -> JobAnchor | None:
    """`jobs` 행 → 앵커. 허용값 밖의 enum이 있으면 **그 앵커만 버리고 경고한다**.

    ⚠️ 버리는 쪽이 안전하다: 앵커가 없으면 같은 자리가 한 번 더 공개될 수 있지만(되돌릴 수
    있다), 엉뚱한 값으로 키를 만들면 **다른 교회 공고와 묶여** 남의 자리를 덮는다(되돌릴 수
    없다 · SPEC §4.1 `seat_of`와 같은 판단).

    우리 CHECK 집합과 min_job의 것이 같으므로(`tests/test_migration.py`) 실제로는 일어나지
    않아야 한다 — 일어났다면 스키마가 갈라진 신호라서 로그를 남긴다.
    """
    try:
        return JobAnchor(
            job_id=job_id,
            church_name=_text(row, "church_name"),
            region=_enum(row, "region", Region),
            position=_enums(row, "position", Position),
            role=_text(row, "role"),
            department=_enum(row, "department", Department),
            posted_at=posted_at,
            contact_email=_text(row, "contact_email"),
        )
    except ValueError as err:
        _LOG.warning("jobs %s: 모르는 값이 있어 앵커에서 뺐다 (%s)", job_id, err)
        return None


def _uuid(row: Mapping[str, JsonValue], key: str) -> UUID:
    value = row.get(key)
    if not isinstance(value, str):
        raise StoreError(f"jobs.{key}: uuid 문자열이어야 함 ({value!r})")
    try:
        return UUID(value)
    except ValueError as err:
        raise StoreError(f"jobs.{key}: uuid가 아님 ({value!r})") from err


def _text(row: Mapping[str, JsonValue], key: str) -> str | None:
    value = row.get(key)
    if value is None or isinstance(value, str):
        return value
    raise StoreError(f"jobs.{key}: 문자열이어야 함 ({type(value).__name__})")


def _day(row: Mapping[str, JsonValue], key: str) -> date:
    value = _optional_day(row, key)
    if value is None:
        raise StoreError(f"jobs.{key}: 비어 있을 수 없음")
    return value


def _optional_day(row: Mapping[str, JsonValue], key: str) -> date | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StoreError(f"jobs.{key}: 날짜 문자열이어야 함 ({type(value).__name__})")
    try:
        return parse_iso_date(value)
    except ValueError as err:
        raise StoreError(f"jobs.{key}: 날짜가 아님 ({value!r})") from err


def _enum[E: StrEnum](row: Mapping[str, JsonValue], key: str, kind: type[E]) -> E | None:
    """⚠️ 허용값 밖이면 `ValueError` — 호출자가 그 앵커를 버린다."""
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StoreError(f"jobs.{key}: 문자열이어야 함 ({type(value).__name__})")
    return kind(value)


def _enums[E: StrEnum](row: Mapping[str, JsonValue], key: str, kind: type[E]) -> tuple[E, ...]:
    value = row.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise StoreError(f"jobs.{key}: 배열이어야 함 ({type(value).__name__})")
    members: list[E] = []
    for item in value:
        if not isinstance(item, str):
            raise StoreError(f"jobs.{key}: 원소는 문자열이어야 함 ({type(item).__name__})")
        members.append(kind(item))
    return tuple(members)
