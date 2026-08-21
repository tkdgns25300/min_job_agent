"""공개(SPEC §4.3) · 끌어올림(§4.2b) — 승인된 초안을 `jobs`에 올린다.

파이프라인의 마지막 칸이다. **유료 호출도 게시판 요청도 없다** — 저장된 판정만 보고 움직인다.

⚠️ **판정이 공개보다 먼저다**(SPEC §4.1). 이 패스는 다시 판정하지 않고 **저장된 라벨을 읽는다**
(`dedup_state`·`dedup_key`). 판정을 여기서 또 내리면 `dedup` 명령과 답이 갈릴 수 있다.
그래서 **`dedup_state`가 없는 초안은 공개하지 않는다** — 중복 판정을 거치지 않은 행을 내보내면
같은 자리가 여러 건 올라간다.

⚠️ **여기가 공개 테이블을 만지는 유일한 파이프라인이다.** 쓰는 것은 두 가지뿐이다 —
INSERT와 `posted_at` 한 칸(§8 소유권 경계). 실패는 **글 단위로 격리**하되 연속 실패가 이어지면
멈춘다: `jobs`에 3,000번 실패하며 밀어붙이지 않는다(CLAUDE.md Runner 규칙).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final
from uuid import UUID

from minjob_ingest.domain import DedupState, ReviewStatus
from minjob_ingest.models import ReviewData
from minjob_ingest.store.base import (
    DedupCandidate,
    PublishTarget,
    Store,
    StoreError,
)

_LOG = logging.getLogger(__name__)

#: 연속 실패 상한. ⚠️ 글 단위 격리가 독이 되는 경우를 막는다 — 권한·스키마가 깨졌으면 3,000번
#: 실패하며 밀어붙이는 대신 멈추고 사람에게 알린다. 흩어진 실패는 성공 한 번이 누적을 지운다.
MAX_CONSECUTIVE_FAILURES: Final = 5


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishReport:
    """실행 요약. ⚠️ **건너뛴 이유를 따로 센다** — 합계만 보면 "왜 안 나갔나"에 답할 수 없다."""

    #: `jobs`에 새로 넣은 공고 수.
    published: int = 0
    #: `posted_at`을 최신으로 밀어준 수(SPEC §4.2b).
    bumped: int = 0
    #: 끌어올리려 했는데 **교회가 claim해** 손을 뗀 수. 실패가 아니라 정상적인 결말이다(§8).
    claimed: int = 0
    #: 공개했던 job이 사라져 링크를 비운 수 — 다음 실행이 다시 공개한다(SPEC §4.3).
    released: int = 0
    #: 중복 판정을 안 거쳐 공개를 보류한 수. ⚠️ 조용히 빠지면 "왜 안 올라갔나"를 알 수 없다.
    unjudged: int = 0
    #: 공고별 실패(`review_data_id` → 사유). 한 건이 실패해도 나머지는 계속한다.
    failed: Mapping[str, str] = field(default_factory=dict)


def publish_all(store: Store, jobs: PublishTarget, *, dry_run: bool = False) -> PublishReport:
    """승인된 초안을 공개하고, 이미 공개된 자리의 날짜를 최신으로 민다.

    ⚠️ **스키마를 먼저 대조한다**(SPEC §4.3) — `jobs`는 min_job 소유라 컬럼이 늘 수 있고,
    한 건 넣고 실패하면 **절반만 공개된** 상태가 남는다.
    """
    jobs.check_jobs_columns()
    candidates = store.dedup_candidates()
    linked = _linked_publications(candidates)
    state = jobs.published_state(list(linked))

    released = _release_missing(jobs, linked, state, dry_run=dry_run)
    bumped, claimed = _bump_stale(jobs, candidates, linked, state, dry_run=dry_run)
    published, unjudged, failed = _publish_new(jobs, candidates, dry_run=dry_run)
    return PublishReport(
        published=published,
        bumped=bumped,
        claimed=claimed,
        released=released,
        unjudged=unjudged,
        failed=failed,
    )


def _linked_publications(candidates: Sequence[DedupCandidate]) -> Mapping[UUID, ReviewData]:
    """공개된 job id → 그 초안. 이 묶음이 "우리가 만든 행"의 정의다(SPEC §8)."""
    return {
        candidate.draft.published_job_id: candidate.draft
        for candidate in candidates
        if candidate.draft.published_job_id is not None
    }


def _release_missing(
    jobs: PublishTarget,
    linked: Mapping[UUID, ReviewData],
    state: Mapping[UUID, date],
    *,
    dry_run: bool,
) -> int:
    """공개했던 job이 사라졌으면 링크를 비운다 — **다음 실행이 다시 공개한다**(SPEC §4.3).

    ⚠️ 같은 실행에서 바로 다시 넣지 않는다. 그러려면 방금 읽은 초안을 메모리에서 고쳐야 하고,
    그때 "공개됨"과 "안 됨"이 한 실행 안에 둘 다 참인 상태가 생긴다. 하루 늦는 대신 각 단계가
    저장된 사실만 보고 움직인다.
    """
    released = 0
    for job_id, draft in linked.items():
        if job_id in state:
            continue
        _LOG.info("공개했던 공고가 사라졌다 — 링크를 비운다 (review_data=%s)", draft.id)
        if not dry_run:
            jobs.release_publication(draft.id, job_id)
        released += 1
    return released


def _bump_stale(
    jobs: PublishTarget,
    candidates: Sequence[DedupCandidate],
    linked: Mapping[UUID, ReviewData],
    state: Mapping[UUID, date],
    *,
    dry_run: bool,
) -> tuple[int, int]:
    """끌어올림(SPEC §4.2b) — 그 자리 묶음의 최신 게시일로 `posted_at`을 민다.

    ⚠️ **`jobs`의 현재 값과 다를 때만 쓴다.** 같은 값을 매번 다시 쓰면 리포트가 "끌어올림
    N건"을 영원히 보고하고, 무엇이 실제로 바뀌었는지 알 수 없게 된다.

    ⚠️ **`MASTER`만** 끌어올린다. `UNCERTAIN` 묶음은 사람이 정할 자리라 대표가 확정되지 않았고,
    `_hold_for_review`가 구성원마다 다른 키를 주므로 묶음의 범위도 확정적이지 않다.
    """
    newest = _newest_by_key(candidates)
    bumped = claimed = 0
    for job_id, draft in linked.items():
        if job_id not in state or draft.dedup_state is not DedupState.MASTER:
            continue
        if draft.dedup_key is None:
            continue
        latest = newest.get(draft.dedup_key)
        # 뒤로 밀지 않는다 — 이미 더 최근이면 손대지 않는다.
        if latest is None or latest <= state[job_id]:
            continue
        if dry_run:
            bumped += 1
            continue
        if jobs.bump_posted_at(job_id, latest):
            bumped += 1
        else:
            # 교회가 claim했다 — 소유권이 넘어가고 크롤러는 손을 뗀다(SPEC §8).
            _LOG.info("교회가 claim한 공고라 끌어올리지 않았다 (jobs=%s)", job_id)
            claimed += 1
    return bumped, claimed


def _newest_by_key(candidates: Sequence[DedupCandidate]) -> Mapping[str, date]:
    """`dedup_key` 묶음별 **원문 게시일** 최댓값.

    ⚠️ 초안의 `posted_at`이 아니라 **원자료 게시일**(`source_data.posted_on` · write-once)을
    쓴다 — `DedupCandidate`가 그 값을 함께 주는 이유와 같다. 대표의 `posted_at`은 묶음 최신으로
    덮이는 파생값이라, 판정의 입력으로 쓰면 값이 실행마다 움직일 여지가 생긴다.

    ⚠️ **지금은 두 값의 최댓값이 같다** — 대표의 `posted_at`이 곧 그 묶음 `posted_on`의
    최댓값이기 때문이다. 그래서 어느 쪽을 써도 결과가 같고, 이 선택은 **테스트가 값으로
    구분하지 못한다**(아래 `test_the_group_date_comes_from_the_raw_record`가 의도만 못 박는다).
    간단해 보인다고 파생값으로 바꾸지 말 것.
    """
    newest: dict[str, date] = {}
    for candidate in candidates:
        key = candidate.draft.dedup_key
        if key is None:
            continue
        known = newest.get(key)
        if known is None or candidate.posted_on > known:
            newest[key] = candidate.posted_on
    return newest


def _publish_new(
    jobs: PublishTarget, candidates: Sequence[DedupCandidate], *, dry_run: bool
) -> tuple[int, int, Mapping[str, str]]:
    """승인됐고 아직 안 나간 초안을 `jobs`에 넣는다(SPEC §4.3).

    ⚠️ **id를 먼저 `review_data`에 적고 INSERT한다**(`reserve_publication`) — 반대로 하면
    중간에 죽었을 때 "공개됐는데 우리는 모르는 행"이 남아 매 실행 다시 공개한다.
    """
    published = unjudged = 0
    failed: dict[str, str] = {}
    consecutive = 0
    for candidate in candidates:
        draft = candidate.draft
        if draft.review_status is not ReviewStatus.APPROVED or draft.published_job_id is not None:
            continue
        if draft.dedup_state is None:
            # ⚠️ 중복 판정을 안 거친 행이다. 내보내면 같은 자리가 여러 건 올라간다 — SPEC §4.1이
            #    "판정이 공개보다 먼저"라고 한 이유다. 세어서 운영자에게 알린다.
            unjudged += 1
            continue
        if dry_run:
            published += 1
            continue
        try:
            job_id = jobs.reserve_publication(draft.id)
            jobs.publish(draft, job_id=job_id, posted_at=draft.posted_at)
        except StoreError as err:
            failed[str(draft.id)] = str(err)
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                raise StoreError(
                    f"공개가 연속 {consecutive}번 실패해 멈췄다 — 권한·스키마를 확인할 것"
                    f" (마지막: {err})"
                ) from err
            continue
        consecutive = 0
        published += 1
    return published, unjudged, failed
