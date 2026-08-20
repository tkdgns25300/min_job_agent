"""두 저장소 구현이 **공유하는 순수 판정**.

`JsonStore`와 `SupabaseStore`가 같은 계약을 지켜야 하는데, 규칙을 각자 들고 있으면 한쪽을
고칠 때 다른 쪽이 조용히 뒤처진다 — 그리고 그 어긋남은 "저장은 됐는데 값이 다르다"로만
드러나서 찾기 어렵다. 그래서 규칙은 여기 한 벌만 둔다(CLAUDE.md: 중복은 상속이 아니라 함수로).

**여기 있는 것은 전부 순수 함수다** — 네트워크·파일을 모른다. 그래서 유료 호출도 DB도 없이
테스트된다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Final

from minjob_ingest.models import JsonValue, ReviewData, SourceData
from minjob_ingest.store.base import DedupUpdate, StoreError

#: `update_structure_state`가 갱신할 수 있는 **유일한** 필드들(SPEC §6 ① 처리 상태).
#: 나머지 전부가 자동으로 write-once가 된다 — 증거 필드를 나열하는 방식이면 `SourceData`에
#: 필드가 추가될 때마다 보호에서 빠져(예: `content_hash`) 갱신 경로로 새는 걸 못 막는다.
MUTABLE_STATE_FIELDS: Final = ("structured_at", "structure_attempts", "last_structure_error")

#: 되돌릴 때 처리 상태를 이 값으로 되돌린다. ⚠️ `MUTABLE_STATE_FIELDS`와 **같은 집합**이어야
#: 한다 — 넷째 상태 칸이 생겼을 때 여기만 모르면 그 값이 남아 재구조화가 조용히 달라진다.
REQUEUED_STATE: Final[Mapping[str, JsonValue]] = {
    "structured_at": None,
    "structure_attempts": 0,
    "last_structure_error": None,
}

if set(REQUEUED_STATE) != set(MUTABLE_STATE_FIELDS):  # pragma: no cover - 임포트 시 계약 검사
    raise RuntimeError(f"되돌리기 상태가 갱신 허용 칸과 다르다: {set(MUTABLE_STATE_FIELDS)}")


def check_only_state_changed(stored: SourceData, incoming: SourceData) -> None:
    """원문 증거는 write-once — 갱신 경로로 바뀌면 구현이 막는다.

    ⚠️ 컬럼 단위로 쓰는 구현(`SupabaseStore`)은 증거 칸을 애초에 보내지 않아 **물리적으로**
    바뀌지 않는다. 그래도 이 검사를 함께 두는 이유: 호출자가 증거를 고친 레코드를 넘겼다는
    것은 낡은 레코드로 갱신하려는 **버그 신호**이고, 조용히 무시하면 그 버그가 드러나지 않는다.
    """
    changed = [
        f.name
        for f in fields(SourceData)
        if f.name not in MUTABLE_STATE_FIELDS
        and getattr(stored, f.name) != getattr(incoming, f.name)
    ]
    if changed:
        raise StoreError(f"원문 증거 필드는 갱신할 수 없음: {changed}")


def check_state_moves_forward(stored: SourceData, incoming: SourceData) -> None:
    """구조화 상태는 단조 증가만 허용한다 — 뒤로 가면 돈과 데이터가 같이 샌다.

    낡은 in-memory 레코드(attempts=0, structured_at=None)로 갱신하면
    (a) 판정 끝난 공고가 재구조화 대상으로 돌아가 Gemini에 재과금되고,
    (b) 시도 횟수가 상한에 영원히 도달하지 못해 영구 실패 공고를 무한 재호출하며,
    (c) 재구조화가 운영자 교정을 덮어쓰는 경로(`upsert_review_data`)까지 열린다.
    운영자의 정당한 시도 리셋은 전용 경로로 들어온다(`SourceData.with_attempts_reset`).
    """
    if stored.has_verdict and not incoming.has_verdict:
        raise StoreError(
            f"source_data {stored.id}: 기록된 판정({stored.structured_at})을 지울 수 없음"
            " — 낡은 레코드로 갱신하려는 것으로 보인다"
        )
    if incoming.structure_attempts < stored.structure_attempts:
        raise StoreError(
            f"source_data {stored.id}: 시도 횟수를 줄일 수 없음"
            f" ({stored.structure_attempts} → {incoming.structure_attempts})"
        )


def with_dedup(stored: ReviewData, update: DedupUpdate) -> ReviewData:
    """판정을 반영한 초안. **라벨과 판정을 나눠 적용한다**(`DedupUpdate.verdict`).

    ⚠️ 운영자가 손댄 행에 판정이 실려 오면 버그다 — 조용히 무시하면 사람이 한 일이 덮인 뒤에도
    아무 표시가 없다(`pipeline/dedup`이 그런 행에는 라벨만 만든다).
    """
    if update.verdict is None:
        return replace(stored, dedup_key=update.dedup_key, dedup_state=update.dedup_state)
    if stored.is_operator_owned:
        raise StoreError(f"운영자가 손댄 초안에는 판정을 쓸 수 없다 (id={stored.id})")
    # ⚠️ **한 번에 바꾼다.** 라벨을 먼저 붙이면 `dedup_state=DUPLICATE`인데 아직 거절이 아닌
    #    중간 상태가 생기고, 레코드 불변식이 그걸 막는다(`_check_dedup`) — 옳은 거부다.
    return replace(
        stored,
        dedup_key=update.dedup_key,
        dedup_state=update.dedup_state,
        review_status=update.verdict.review_status,
        reject_reason=update.verdict.reject_reason,
        posted_at=update.verdict.posted_at,
    )
