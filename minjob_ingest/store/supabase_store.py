"""Supabase 저장소 — `Store` 프로토콜의 원격 구현(ROADMAP 1-6).

`JsonStore`와 **같은 계약**을 지킨다. 그래서 파이프라인 코드는 한 줄도 바뀌지 않고, 두 구현이
같은 계약 테스트를 통과한다. 규칙(write-once 검사·단조 증가·중복 판정 반영)은 `store/guards.py`
한 벌을 공유한다 — 각자 들고 있으면 한쪽만 고쳐지고 그 어긋남은 찾기 어렵다.

**JsonStore와 의도적으로 다른 것 — 더 강해지는 쪽으로만**:

- **락이 없다.** 파일 하나를 read-modify-write하는 구조가 아니라 행 단위로 쓴다. 대신 위험한
  갱신은 **조건을 필터에 넣어 DB가 판정**하게 한다(아래 `upsert_review_data`·
  `update_structure_state`) — "읽고 확인하고 쓴다" 사이에 min_job admin이 승인한 것을 덮어쓰는
  길이 그렇게 닫힌다.
- **트랜잭션이 없다.** PostgREST는 요청 하나가 트랜잭션이라 여러 요청을 묶을 수 없다. 그래서
  순서가 중요한 곳은 **중단돼도 무해한 순서**로 둔다(`requeue_for_structure` 주석).

⚠️ **지금은 전권 `service_role`로 붙는다**(운영자 결정 2026-08-21 · SPEC §8). `jobs`를 컬럼
단위로 지키는 GRANT는 별도 `crawler` 롤이 와야 듣는다 — 그때까지 이 파일은 staging 4테이블만
건드리고, `jobs`는 손대지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Final

from minjob_ingest.clock import kst_now, parse_iso_date
from minjob_ingest.domain import CrawlMode, normalize_source_key
from minjob_ingest.models import (
    MAX_STRUCTURE_ATTEMPTS,
    CrawlRun,
    JsonValue,
    ReviewData,
    SourceData,
    SourceHealth,
)
from minjob_ingest.store.base import (
    DedupCandidate,
    DedupUpdate,
    LedgerEntry,
    RequeueResult,
    StoreError,
)
from minjob_ingest.store.guards import (
    MUTABLE_STATE_FIELDS,
    REQUEUED_STATE,
    check_only_state_changed,
    check_state_moves_forward,
    with_dedup,
)
from minjob_ingest.store.postgrest import (
    JsonRow,
    PostgrestClient,
    chunked,
    eq,
    in_values,
    is_null,
    lt,
    lte,
)
from minjob_ingest.store.serde import (
    SerdeError,
    ledger_entry_of_row,
    ledger_key_of_row,
    row_to_review_data,
    row_to_source_data,
    row_to_source_health,
    to_row,
)

_LOG = logging.getLogger(__name__)

_SOURCE_DATA: Final = "source_data"
_REVIEW_DATA: Final = "review_data"
_SOURCE_HEALTH: Final = "source_health"
_CRAWL_RUN: Final = "crawl_run"

#: serde 디코더가 컬럼 집합을 정확히 대조하므로(스키마 드리프트 가드) 부분 select로는 레코드를
#: 만들 수 없다. 그 가드를 한 호출자 편의로 약화시키지 않는다 — 전 컬럼을 읽는다.
_ALL: Final = "*"

#: 한 번에 쓸 행 수. ⚠️ 전량을 한 요청에 담으면 본문이 수 MB가 되어 게이트웨이가 거절한다.
#: 나눠 쓰면 중간에 끊길 수 있지만 중복 판정은 **다시 돌리는 것이 되돌리기**라 회복된다
#: (`minjob-ingest dedup`은 지난 판정도 매번 처음부터 다시 본다).
_WRITE_BATCH_ROWS: Final = 500

#: 손상 행 보고 훅 — (테이블명, 예외). 기본은 경고 로그(`JsonStore`와 같은 계약).
type CorruptRowHandler = Callable[[str, SerdeError], None]


def _log_corrupt_row(source: str, error: SerdeError) -> None:
    _LOG.warning("손상된 행을 건너뜀 (%s): %s", source, error)


class SupabaseStore:
    """`Store` 프로토콜의 PostgREST 구현."""

    def __init__(
        self, client: PostgrestClient, *, on_corrupt_row: CorruptRowHandler = _log_corrupt_row
    ) -> None:
        self._client = client
        self._on_corrupt_row = on_corrupt_row

    # ── 원장·수집 (SPEC §4) ──────────────────────────────────────

    def seen_postings(
        self, source_key: str, external_ids: Sequence[str]
    ) -> Mapping[str, LedgerEntry]:
        # 저장 시엔 모델이 정규화하므로 조회도 같은 정규화를 거쳐야 원장이 빗나가지 않는다.
        # (빗나가면 이미 수집한 글의 상세를 매 실행 다시 요청한다.)
        wanted = {external_id.strip(): external_id for external_id in external_ids}
        if not wanted:
            return {}
        key = normalize_source_key(source_key)
        seen: dict[str, LedgerEntry] = {}
        for chunk in chunked(sorted(wanted)):
            for row in self._client.select(
                _SOURCE_DATA,
                columns=_ALL,
                order="external_id",
                filters={"source_key": eq(key), "external_id": in_values(list(chunk))},
            ):
                try:
                    _, stored_id = ledger_key_of_row(row)
                    entry = ledger_entry_of_row(row)
                except SerdeError as err:
                    self._on_corrupt_row(_SOURCE_DATA, err)
                    continue
                # 호출자가 넘긴 원본 문자열을 키로 돌려준다 — 호출자가 자기 목록을 이걸로 걸러낸다.
                original = wanted.get(stored_id)
                if original is not None:
                    seen[original] = entry
        return seen

    def save_source_data(self, record: SourceData) -> bool:
        # `Prefer: resolution=ignore-duplicates` = ON CONFLICT DO NOTHING. 돌아온 행 수가
        # "새로 넣었나"를 말해준다 — 다시 조회하지 않아도 된다.
        landed = self._client.insert(
            _SOURCE_DATA,
            [to_row(record)],
            on_conflict="source_key,external_id",
            ignore_duplicates=True,
            returning=True,
        )
        return len(landed) == 1

    # ── 구조화 (SPEC §4) ─────────────────────────────────────────

    def list_unstructured(
        self, limit: int, *, source_key: str | None = None
    ) -> tuple[SourceData, ...]:
        if limit <= 0:
            raise ValueError(f"limit는 1 이상이어야 함 ({limit})")
        # ⚠️ 시도 상한을 **서버에서** 거른다. `MAX_STRUCTURE_ATTEMPTS`는 Python 상수이고
        #    DB에 박지 않았으므로(마이그레이션 주석) 값을 쿼리로 넘긴다.
        filters = {"structured_at": is_null(), "structure_attempts": lt(MAX_STRUCTURE_ATTEMPTS)}
        if source_key is not None:
            filters["source_key"] = eq(normalize_source_key(source_key))
        rows = self._client.select(
            _SOURCE_DATA, columns=_ALL, order="fetched_at.asc", filters=filters, limit=limit
        )
        return tuple(self._decoded(rows, row_to_source_data, _SOURCE_DATA))

    def update_structure_state(self, record: SourceData) -> None:
        stored = self._one(
            _SOURCE_DATA, column="id", value=str(record.id), decode=row_to_source_data
        )
        if stored is None:
            raise StoreError(f"source_data {record.id} 없음 — 상태를 갱신할 대상이 없다")
        check_only_state_changed(stored, record)
        check_state_moves_forward(stored, record)
        # ⚠️ **상태 세 칸만 보낸다** — 원문 증거는 물리적으로 갱신 경로에 오르지 않는다.
        #    (위 검사는 "낡은 레코드로 부르는 버그"를 요란하게 만드는 역할이고, 이 필터가
        #     실제 방어선이다.)
        row = to_row(record)
        changed = self._client.patch(
            _SOURCE_DATA,
            # 그동안 다른 실행이 시도 횟수를 올렸으면 되돌리지 않는다 — DB가 판정한다.
            filters={
                "id": eq(str(record.id)),
                "structure_attempts": lte(record.structure_attempts),
            },
            values={name: row[name] for name in MUTABLE_STATE_FIELDS},
        )
        if not changed:
            raise StoreError(
                f"source_data {record.id}: 상태를 갱신하지 못했다"
                " — 읽은 뒤 시도 횟수가 올라간 것으로 보인다"
            )

    def requeue_for_structure(self, *, source_key: str | None = None) -> RequeueResult:
        protected = self._protected_ids()
        filters = {"structured_at": is_null(negated=True)}
        if source_key is not None:
            filters["source_key"] = eq(normalize_source_key(source_key))
        # ⚠️ 전 컬럼을 읽는다 — `is_safe_to_replace`·`label`이 레코드 계약이라 부분 행으로는
        #    만들 수 없다. 운영자가 직접 부르는 드문 명령이라 그 비용을 받아들인다.
        records = self._decoded(
            self._client.select(_SOURCE_DATA, columns=_ALL, order="id", filters=filters),
            row_to_source_data,
            _SOURCE_DATA,
        )

        requeued = [str(record.id) for record in records if str(record.id) not in protected]
        skipped = tuple(record.label for record in records if str(record.id) in protected)
        if not requeued:
            return RequeueResult(skipped=skipped)

        # ⚠️ **판정을 먼저 되돌린다**(`Store.requeue_for_structure` 계약). 반대로 하면
        #    "판정 완료 + 초안 없음"이 남고 그 상태는 사후 탐지가 불가능하다. 트랜잭션이 없어
        #    중간에 끊길 수 있는데, 이 순서라면 남는 상태가 "미판정 + 초안 있음"이고 그건
        #    재구조화가 초안을 교체하므로 무해하다.
        for chunk in chunked(requeued):
            self._client.patch(
                _SOURCE_DATA,
                filters={"id": in_values(list(chunk))},
                values=REQUEUED_STATE,
                returning=False,
            )
        for chunk in chunked(requeued):
            self._client.delete(_REVIEW_DATA, filters={"source_data_id": in_values(list(chunk))})
        return RequeueResult(requeued=len(requeued), skipped=skipped)

    def _protected_ids(self) -> frozenset[str]:
        """되돌리면 안 되는 초안의 `source_data_id`.

        ⚠️ 읽을 수 없는 행이 하나라도 있으면 **멈춘다** — 승인된 행인지 알 수 없는데 지우면
        되돌릴 방법이 없다. 손상 행을 건너뛰는 다른 조회들과 여기가 다른 이유다.
        """
        protected: set[str] = set()
        for row in self._client.select(_REVIEW_DATA, columns=_ALL, order="id"):
            try:
                draft = row_to_review_data(row)
            except SerdeError as err:
                raise StoreError(f"읽을 수 없는 초안이 있어 되돌리지 않았다: {err}") from err
            if not draft.is_safe_to_replace:
                protected.add(str(draft.source_data_id))
        return frozenset(protected)

    def upsert_review_data(self, record: ReviewData) -> bool:
        existing = self._one(
            _REVIEW_DATA,
            column="source_data_id",
            value=str(record.source_data_id),
            decode=row_to_review_data,
        )
        if existing is None:
            self._client.insert(_REVIEW_DATA, [to_row(record)])
            return True
        if not existing.is_safe_to_replace:
            _LOG.info(
                "운영자가 손댄 초안이라 재구조화 결과를 버림 (source_data_id=%s, status=%s)",
                record.source_data_id,
                existing.review_status.value,
            )
            return False
        merged = record.carrying_operator_state_of(existing)
        changed = self._client.patch(
            _REVIEW_DATA, filters=_untouched_since(existing), values=to_row(merged)
        )
        if not changed:
            # 읽은 뒤 admin이 승인·수정한 경우다. 필터가 그걸 막았다 — JsonStore에는 없는 방어선.
            _LOG.info(
                "읽은 뒤 초안이 바뀌어 재구조화 결과를 버림 (source_data_id=%s)",
                record.source_data_id,
            )
            return False
        return True

    def dedup_candidates(self) -> tuple[DedupCandidate, ...]:
        # ⚠️ **전량을 한 번에 본다.** 중복은 글 하나만 보고 판정할 수 없어 배치로 쪼갤 수 없다
        #    (SPEC §4.1). 전송 층이 페이지네이션 + 개수 검산으로 "다 받았음"을 보장한다.
        posted_on = self._posted_on_by_source_id()
        candidates: list[DedupCandidate] = []
        for row in self._client.select(_REVIEW_DATA, columns=_ALL, order="id"):
            # ⚠️ 깨진 행을 건너뛰지 않는다 — 그 행이 대표였을 수 있고, 그러면 대표가 아닌
            #    쪽이 공개되면서 아무 표시도 남지 않는다.
            try:
                draft = row_to_review_data(row)
            except SerdeError as err:
                raise StoreError(f"읽을 수 없는 초안이 있어 중복 판정을 멈췄다: {err}") from err
            source_posted_on = posted_on.get(str(draft.source_data_id))
            if source_posted_on is None:
                raise StoreError(f"초안의 원자료가 없다 (source_data_id={draft.source_data_id})")
            candidates.append(DedupCandidate(draft=draft, posted_on=source_posted_on))
        return tuple(candidates)

    def _posted_on_by_source_id(self) -> Mapping[str, date]:
        """라운드 경계용 게시일. **원자료에서 가져온다**(`DedupCandidate` docstring).

        두 컬럼만 읽는 이유: `raw_text`·`raw_html`까지 3,188행 받으면 수십 MB다. 여기서는
        레코드를 만들지 않으니 컬럼 집합 검사를 거치지 않아도 된다.
        """
        by_id: dict[str, date] = {}
        for row in self._client.select(_SOURCE_DATA, columns="id,posted_on", order="id"):
            raw_id, raw_date = row.get("id"), row.get("posted_on")
            if not isinstance(raw_id, str) or not isinstance(raw_date, str):
                raise StoreError(f"source_data 게시일 행이 이상하다 (id={raw_id!r})")
            try:
                by_id[raw_id] = parse_iso_date(raw_date)
            except ValueError as err:
                raise StoreError(f"source_data {raw_id}: 게시일을 읽지 못했다 ({err})") from err
        return by_id

    def apply_dedup(self, updates: Sequence[DedupUpdate]) -> int:
        if not updates:
            return 0
        wanted = {str(update.review_data_id): update for update in updates}
        stored = self._drafts_by_id(sorted(wanted))
        missing = sorted(set(wanted) - set(stored))
        if missing:
            raise StoreError(f"초안이 없어 판정을 적용할 수 없다 (id={missing[0]})")

        judged: list[Mapping[str, JsonValue]] = []
        for review_data_id, update in wanted.items():
            before = stored[review_data_id]
            after = with_dedup(before, update)
            if after != before:  # 값이 이미 같은 행은 세지 않는다(멱등)
                judged.append(to_row(after))
        for start in range(0, len(judged), _WRITE_BATCH_ROWS):
            self._client.upsert(
                _REVIEW_DATA, judged[start : start + _WRITE_BATCH_ROWS], on_conflict="id"
            )
        return len(judged)

    def _drafts_by_id(self, review_data_ids: Sequence[str]) -> Mapping[str, ReviewData]:
        drafts: dict[str, ReviewData] = {}
        for chunk in chunked(review_data_ids):
            for row in self._client.select(
                _REVIEW_DATA, columns=_ALL, order="id", filters={"id": in_values(list(chunk))}
            ):
                # 판정 대상이 깨져 있으면 멈춘다 — `dedup_candidates`와 같은 이유다.
                try:
                    draft = row_to_review_data(row)
                except SerdeError as err:
                    raise StoreError(f"읽을 수 없는 초안에 판정을 쓸 수 없다: {err}") from err
                drafts[str(draft.id)] = draft
        return drafts

    # ── 실행·상태 (SPEC §6 ③④) ──────────────────────────────────

    def start_run(self, mode: CrawlMode) -> CrawlRun:
        # id는 레코드가 만든다 — DB default에 맡기면 되돌려 읽어야 하고, 그 사이 크래시하면
        # 어느 행이 우리 실행인지 알 수 없다.
        run = CrawlRun(mode=mode, started_at=kst_now())
        self._client.insert(_CRAWL_RUN, [to_row(run)])
        return run

    def finish_run(self, record: CrawlRun) -> None:
        changed = self._client.patch(
            _CRAWL_RUN, filters={"id": eq(str(record.id))}, values=to_row(record)
        )
        if not changed:
            raise StoreError(f"crawl_run {record.id} 없음 — 시작 기록 없이 종료할 수 없다")

    def get_health(self, source_key: str) -> SourceHealth | None:
        # 정규화하지 않으면 조회가 빗나가 previous=None이 되고, 누적 카운터가 매 실행
        # 초기화돼 SPEC §7 경보(연속 실패·연속 0건)가 영구히 울리지 않는다.
        # 손상 행을 None으로 삼키지 않는다 — 같은 이유로 그대로 던진다.
        return self._one(
            _SOURCE_HEALTH,
            column="source_key",
            value=normalize_source_key(source_key),
            decode=row_to_source_health,
        )

    def upsert_health(self, record: SourceHealth) -> None:
        self._client.upsert(_SOURCE_HEALTH, [to_row(record)], on_conflict="source_key")

    # ── 공통 헬퍼 ───────────────────────────────────────────────

    def _one[R](
        self, table: str, *, column: str, value: str, decode: Callable[[JsonRow], R]
    ) -> R | None:
        """**유일 컬럼 하나로** 한 행. 없으면 `None`, 있으면 디코딩해서 준다(손상은 그대로 던진다).

        ⚠️ 필터 묶음이 아니라 컬럼·값 한 쌍을 받는다. 정렬을 **그 컬럼으로** 하기 때문이다 —
        `order`를 따로 받으면 `source_health`(PK가 `source_key`)를 `id`로 정렬하는 실수가
        생기고, PostgREST는 없는 컬럼에 400을 준다(2026-08-21 실측). 유일 컬럼은 반드시
        존재하므로 이 모양에서는 그 실수가 불가능하다.
        """
        rows = self._client.select(
            table, columns=_ALL, order=column, filters={column: eq(value)}, limit=1
        )
        return None if not rows else decode(rows[0])

    def _decoded[R](
        self, rows: Sequence[JsonRow], decode: Callable[[JsonRow], R], table: str
    ) -> list[R]:
        """행 단위 격리 — 손상된 한 행이 전체 로드를 죽이지 않게 한다(`Store` 손상 행 정책)."""
        records: list[R] = []
        for row in rows:
            try:
                records.append(decode(row))
            except SerdeError as err:
                self._on_corrupt_row(table, err)
        return records


def _untouched_since(existing: ReviewData) -> Mapping[str, str]:
    """읽었을 때와 **똑같은 행만** 고치게 하는 필터.

    `is_safe_to_replace`가 보는 네 칸을 읽은 값으로 못 박는다 — 그 사이 min_job admin이
    승인하거나 교단을 확정하면 어느 칸이 바뀌므로 0행이 돌아오고, 우리는 덮어쓰지 않는다.
    JsonStore는 락으로 이걸 막았지만 여기는 **DB가 판정**한다.
    """
    return {
        "id": eq(str(existing.id)),
        "review_status": eq(existing.review_status.value),
        "denomination_source": eq(existing.denomination_source.value),
        "reject_reason": (
            is_null() if existing.reject_reason is None else eq(existing.reject_reason.value)
        ),
        "reviewed_by": is_null() if existing.reviewed_by is None else eq(existing.reviewed_by),
    }
