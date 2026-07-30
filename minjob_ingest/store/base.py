"""저장소 계약 — 파이프라인이 파일/DB 구현을 모르게 하는 seam(CLAUDE.md 저장 seam).

메서드를 **좁게** 둔다. 범용 `update(record)`를 노출하면 원문 증거(`raw_text`·`raw_meta` 등)를
덮어쓰는 경로가 열려 write-once가 주석으로 전락한다 → 갱신은 "구조화 상태"만 받는다.

**호출 순서 계약(중요)**: 구조화 결과를 반영할 때는
`upsert_review_data` → `update_structure_state` **순서로** 부른다.
반대로 하면 판정 기록 직후 크래시한 공고가 "판정 완료(재구조화 대상 아님) + 초안 없음"으로
남아 **아무도 모르게 유실**된다 — SPEC §4가 "review_data 없음"을 재시도 기준으로 쓸 수 없게
만들었기 때문에 이 유실은 사후 탐지가 불가능하다.

**손상 행 정책**: 배치 읽기는 행 단위로 격리한다 — 한 행이 깨졌다고 전체 로드를 실패시키면
원장을 잃고 31곳을 다시 긁는다(가드레일 #7). 구현은 손상 행을 건너뛰고 `on_corrupt_row`로
보고한다. 반면 **단건 읽기(`get_health`)는 그대로 던진다** — 조용히 `None`을 주면 누적
카운터가 초기화돼 §7 경보가 무의미해진다.
`serde.SerdeError`만 "이 행 손상"으로 취급하고, 다른 예외는 버그이므로 중단시킨다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from minjob_ingest.domain import CrawlMode
from minjob_ingest.models import CrawlRun, ReviewData, SourceData, SourceHealth


class StoreError(Exception):
    """저장소 상태가 계약과 어긋날 때(없는 run 종료, 증거 필드 변경 시도 등)."""


class Store(Protocol):
    """스테이징 4테이블 접근. 구현: `JsonStore`(Phase 1) → `SupabaseStore`(ROADMAP 1-6)."""

    # ── 원장·수집 (SPEC §4) ──────────────────────────────────────

    def seen_external_ids(self, source_key: str, external_ids: Sequence[str]) -> set[str]:
        """이미 수집한 식별자만 골라 반환한다.

        목록 페이지당 **한 번** 부른다(행마다 조회하면 31곳 곱하기 수백 행이 된다).
        "이미 본 글에서 중단"이 아니라 **unseen만 고르는** 데 쓴다(SPEC §4).
        """
        ...

    def save_source_data(self, record: SourceData) -> bool:
        """원자료 적재. 이미 있으면 아무것도 하지 않는다(= ON CONFLICT DO NOTHING).

        Returns: 새로 저장했으면 True, 이미 있어 건너뛰었으면 False(신규 건수 집계용).
        """
        ...

    # ── 구조화 (SPEC §4) ─────────────────────────────────────────

    def list_unstructured(self, limit: int) -> tuple[SourceData, ...]:
        """판정이 안 끝났고 시도 상한도 안 넘긴 원자료를 오래된 것부터 최대 `limit`건.

        백필 직후 backlog가 한 실행을 폭주시키지 않도록 **상한이 필수**다.
        """
        ...

    def update_structure_state(self, record: SourceData) -> None:
        """구조화 상태(`structured_at`·`structure_attempts`·`last_structure_error`)만 반영한다.

        - 원문 증거 필드가 저장된 값과 다르면 `StoreError` — write-once를 구현이 강제한다.
        - 상태는 **앞으로만** 간다: 기록된 판정을 지우거나 시도 횟수를 줄이면 `StoreError`.
          낡은 레코드로 부르는 사고를 막는다 — 판정이 지워지면 Gemini 재과금이고, 시도 횟수가
          줄면 상한에 영원히 도달하지 못해 영구 실패 공고를 무한 재호출한다(SPEC §4).
          운영자의 시도 리셋은 이 경로가 아니라 전용 메서드로 들어온다(ROADMAP 1-6).
        """
        ...

    def upsert_review_data(self, record: ReviewData) -> bool:
        """초안을 저장한다. 같은 `source_data_id`가 있으면 교체(SPEC §6 ②).

        기존 행에 **운영자 손길이 있으면 덮어쓰지 않고 건너뛴다**(`is_operator_touched`):
        (a) 이미 검수됨(PENDING 아님) — 재구조화가 승인을 PENDING으로 되돌리면 안 된다,
        (b) PENDING이지만 운영자가 교단·교회명 등을 고쳐둠 — 이어받는 필드는
        `REVIEW_STATE_FIELDS`뿐이라 나머지 교정은 AI 초안으로 덮인다. 그러면 `reviewed_by`만
        남아 "봤는데 고친 흔적이 없는" 모순 행이 된다.
        손대지 않은 PENDING이면 `id`·`created_at`·검수 메타를 이어받아
        (`ReviewData.carrying_review_state_of`) 초안 필드만 갱신한다.

        Returns: 기록했으면 True, 이미 검수돼 건너뛰었으면 False.
        """
        ...

    # ── 실행·상태 (SPEC §6 ③④) ──────────────────────────────────

    def start_run(self, mode: CrawlMode) -> CrawlRun:
        """실행 시작 행을 만들어 반환한다 — 하위 레코드가 참조할 `run_id`를 여기서 얻는다."""
        ...

    def finish_run(self, record: CrawlRun) -> None:
        """집계를 채운 실행 행으로 갱신한다. 없는 `id`면 `StoreError`.

        조용히 넘기면 실행이 영구 "진행중"으로 남아 대시보드가 거짓말을 한다.
        """
        ...

    def get_health(self, source_key: str) -> SourceHealth | None:
        """직전 상태(없으면 None). 누적 카운터·마지막 성공 시각을 이어붙이는 데 필요하다.

        `source_key`는 저장 때와 **같은 정규화**를 거친다 — 안 하면 조회가 빗나가 매 실행
        `previous=None`이 되고, 누적 카운터가 초기화돼 §7 경보가 영구히 울리지 않는다.
        """
        ...

    def upsert_health(self, record: SourceHealth) -> None:
        """게시판 상태를 기록한다. 이어붙이기는 `SourceHealth.advance`가 계산한다."""
        ...
