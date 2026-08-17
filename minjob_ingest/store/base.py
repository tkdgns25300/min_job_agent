"""저장소 계약 — 파이프라인이 파일/DB 구현을 모르게 하는 seam(CLAUDE.md 저장 seam).

메서드를 **좁게** 둔다. 범용 `update(record)`를 노출하면 원문 증거(`raw_text`·`raw_meta` 등)를
덮어쓰는 경로가 열려 write-once가 주석으로 전락한다 → 갱신은 "구조화 상태"만 받는다.

**호출 순서 계약(중요)**: 구조화 결과를 반영할 때는
`upsert_review_data` → `update_structure_state` **순서로** 부른다.
반대로 하면 판정 기록 직후 크래시한 공고가 "판정 완료(재구조화 대상 아님) + 초안 없음"으로
남아 **아무도 모르게 유실**된다 — SPEC §4가 "review_data 없음"을 재시도 기준으로 쓸 수 없게
만들었기 때문에 이 유실은 사후 탐지가 불가능하다.

**손상 행 정책**: 배치 읽기는 행 단위로 격리한다 — 한 행이 깨졌다고 전체 로드를 실패시키면
원장을 잃고 31곳을 다시 긁는다. 구현은 손상 행을 건너뛰고 `on_corrupt_row`로
보고한다. 반면 **단건 읽기(`get_health`)는 그대로 던진다** — 조용히 `None`을 주면 누적
카운터가 초기화돼 §7 경보가 무의미해진다.
`serde.SerdeError`만 "이 행 손상"으로 취급하고, 다른 예외는 버그이므로 중단시킨다.
⚠️ **`dedup_candidates`도 그대로 던진다** — 중복 판정은 행 하나가 아니라 **묶음**을 보고
대표를 고르므로(SPEC §4.1), 깨진 행을 건너뛰면 그 행이 대표였을 때 **대표가 아닌 쪽이
공개되면서 아무 표시도 남지 않는다**.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from minjob_ingest.domain import CrawlMode, DedupState, RejectReason, ReviewStatus
from minjob_ingest.models import CrawlRun, ReviewData, SourceData, SourceHealth


class StoreError(Exception):
    """저장소 상태가 계약과 어긋날 때(없는 run 종료, 증거 필드 변경 시도 등)."""


@dataclass(frozen=True, slots=True)
class RequeueResult:
    """되돌리기 결과. **건너뛴 것을 함께 돌려준다** — 숫자만 주면 운영자가 왜 덜 되돌려졌는지
    알 수 없고, 그때 손으로 파일을 고치게 된다."""

    #: 미판정으로 되돌린 공고 수.
    requeued: int = 0
    #: 초안을 지킬 이유가 있어 건너뛴 공고(`source_key/external_id`).
    #: ⚠️ **"운영자가 손댔다"는 뜻이 아니다** — 기준은 `ReviewData.is_safe_to_replace`이고,
    #: 거기에는 코드가 만든 거절(`HERESY`·`CLOSED`)도 걸린다. 그런 행은 목록·규칙을 고쳐도
    #: 되돌아오지 않으므로, 세는 쪽이 이유를 단정하지 않는다.
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DedupCandidate:
    """중복 판정에 들어가는 한 건 — 초안 + **원자료의 게시일**(SPEC §4.1).

    ⚠️ **날짜를 원자료에서 가져오는 이유**: 대표 행의 `review_data.posted_at`은 묶음의 최신
    날짜로 덮어쓴다. 그 값으로 라운드 경계를 계산하면 **다시 돌릴 때마다 경계가 움직여**
    같은 데이터에서 다른 결과가 나온다. `source_data.posted_on`은 write-once라 안 흔들린다.
    """

    draft: ReviewData
    posted_on: date


@dataclass(frozen=True, slots=True)
class DedupVerdict:
    """판정을 적용할 때 함께 바뀌는 칸들. 라벨만 붙일 때는 이게 없다."""

    review_status: ReviewStatus
    reject_reason: RejectReason | None
    #: 대표는 묶음의 최신 게시일을 쓴다(계속 끌어올린다 = 아직 뽑고 있다 · SPEC §4.1).
    posted_at: date


@dataclass(frozen=True, slots=True)
class DedupUpdate:
    """dedup이 한 행에 반영할 것.

    ⚠️ **판정은 선택이다.** 운영자가 손댔거나 이미 공개된 행에는 `dedup_key`·`dedup_state`
    **라벨만** 붙인다(`verdict=None`) — 사람이 한 일을 크롤러가 덮으면 안 되고, 이미 공개한
    공고를 나중에 중복으로 거절하면 목록에서 사라진다. 그래도 라벨은 붙여야 SPEC §4.2가
    "이미 공개된 같은 자리"를 이 키로 찾을 수 있다.
    """

    review_data_id: UUID
    dedup_key: str
    dedup_state: DedupState
    verdict: DedupVerdict | None = None


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """원장에 이미 있는 글의 식별 정보. `external_id`가 여전히 같은 글을 가리키는지 확인용.

    원장 판정 자체는 **`(source_key, external_id)`만으로** 한다(SPEC §4) — 이 값들은 그
    번호가 다른 글로 바뀌었는지 보는 **경보**일 뿐이다.
    """

    title: str
    #: 저장된 게시일. `source_data.posted_on`이 필수라 여기서도 항상 있다.
    posted_on: date

    def points_to_another_posting(self, *, title: str, posted_on: date | None) -> bool:
        """같은 번호인데 **제목과 게시일이 둘 다** 다른가.

        둘 다 다르면 그 번호가 완전히 다른 글을 가리킨다는 뜻이고, 원인은 두 가지다:
        게시판이 번호를 재사용했거나(그 공고를 영구히 놓친다), 사이트 개편으로 우리가 엉뚱한
        칸을 읽기 시작했거나(모든 행이 이 모양이 된다). 둘 다 **조용히 건너뛰면 안 되는** 일이다.

        하나만 다른 경우는 **정상으로 본다** — 작성자가 제목에 `[끌어올림]`·`(마감)`을 붙이거나
        날짜를 손보는 일이 흔하고, 그때 경보를 울리면 상시 잡음이 되어 아무도 안 본다.
        """
        return _differs(self.title, title) and self.posted_on != posted_on


def _differs(stored: str, fresh: str) -> bool:
    """공백 차이는 같은 것으로 본다 — 게시판이 `&nbsp;`를 흔하게 쓴다.

    `str.split()`이 NBSP(U+00A0)도 공백으로 취급하므로 따로 치환하지 않는다.
    """
    return _collapsed(stored) != _collapsed(fresh)


def _collapsed(value: str) -> str:
    return " ".join(value.split())


class Store(Protocol):
    """스테이징 4테이블 접근. 구현: `JsonStore`(Phase 1) → `SupabaseStore`(ROADMAP 1-6)."""

    # ── 원장·수집 (SPEC §4) ──────────────────────────────────────

    def seen_postings(
        self, source_key: str, external_ids: Sequence[str]
    ) -> Mapping[str, LedgerEntry]:
        """이미 수집한 식별자와 그때 저장한 제목·게시일을 반환한다.

        목록 페이지당 **한 번** 부른다(행마다 조회하면 31곳 곱하기 수백 행이 된다).
        "이미 본 글에서 중단"이 아니라 **unseen만 고르는** 데 쓴다(SPEC §4) — 반환된 키에
        없는 것이 새 글이다.

        제목·게시일을 함께 주는 이유: 호출자가 목록에서 방금 읽은 값과 대조해
        `LedgerEntry.points_to_another_posting`으로 번호가 다른 글로 바뀌었는지 본다.
        **추가 요청이 없다** — 양쪽 값을 이미 손에 들고 있다.

        키는 **호출자가 넘긴 원본 문자열**이다(정규화된 값이 아니다) — 호출자가 이 결과로
        자기 목록을 걸러내기 때문이다.
        """
        ...

    def save_source_data(self, record: SourceData) -> bool:
        """원자료 적재. 이미 있으면 아무것도 하지 않는다(= ON CONFLICT DO NOTHING).

        Returns: 새로 저장했으면 True, 이미 있어 건너뛰었으면 False(신규 건수 집계용).
        """
        ...

    # ── 구조화 (SPEC §4) ─────────────────────────────────────────

    def list_unstructured(
        self, limit: int, *, source_key: str | None = None
    ) -> tuple[SourceData, ...]:
        """판정이 안 끝났고 시도 상한도 안 넘긴 원자료를 오래된 것부터 최대 `limit`건.

        백필 직후 backlog가 한 실행을 폭주시키지 않도록 **상한이 필수**다.

        `source_key`를 주면 그 게시판만 돌려준다. ⚠️ **필터가 여기 있어야 한다** — 반환값을
        호출자가 거르면 `limit`이 "그 게시판에서 N건"이 아니라 "전체에서 N건 중 남은 것"이
        되어, 프롬프트를 다듬을 때 표본이 0건이 되는 일이 생긴다(수집 시각 순이라 오래된
        쪽은 한 게시판에 뭉쳐 있다 · 2026-08-10 실측).
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

    def requeue_for_structure(self, *, source_key: str | None = None) -> RequeueResult:
        """판정을 지워 그 공고를 **다시 구조화할 수 있게** 되돌린다.

        `structured_at`은 앞으로만 가므로(위 `update_structure_state`) 되돌리는 길이 따로
        있어야 한다. ⚠️ **전량 저장 전에 이게 있어야 한다** — 저장한 뒤 프롬프트 문제를
        발견하면 고친 것을 적용할 방법이 없다(ROADMAP 1-2).

        ⚠️ **운영자가 손댄 초안은 되돌리지 않는다**(`ReviewData.is_safe_to_replace`). 승인된
        행을 지우면 `published_job_id`가 사라져 이미 공개한 공고를 한 번 더 승격하게 된다.

        ⚠️ **저장은 판정 → 초안 순서**다(위 `upsert_review_data`의 역순). 초안을 먼저 지우면
        중간에 죽었을 때 "판정 완료 + 초안 없음"이 남는데, SPEC §4가 그 상태를 재시도 기준으로
        쓰지 않아 사후 탐지가 불가능하다.
        """
        ...

    def upsert_review_data(self, record: ReviewData) -> bool:
        """초안을 저장한다. 같은 `source_data_id`가 있으면 교체(SPEC §6 ②).

        기존 행에 **운영자 손길이 있으면 덮어쓰지 않고 건너뛴다**(`is_operator_touched`):
        (a) 이미 검수됨(PENDING 아님) — 재구조화가 승인을 PENDING으로 되돌리면 안 된다,
        (b) PENDING이지만 운영자가 교단·교회명 등을 고쳐둠 — 이어받는 필드는
        `CARRIED_ON_RESTRUCTURE`뿐이라 나머지 교정은 AI 초안으로 덮인다. 그러면 `reviewed_by`만
        남아 "봤는데 고친 흔적이 없는" 모순 행이 된다.
        손대지 않은 PENDING이면 `id`·`created_at`·운영자 기록을 이어받아
        (`ReviewData.carrying_operator_state_of`) 초안 필드와 **판정**을 갱신한다 — 판정까지
        이어받으면 새로 붙은 이단·마감 거절이 옛 `PENDING`으로 덮여 사라진다(SPEC §5.7).

        Returns: 기록했으면 True, 이미 검수돼 건너뛰었으면 False.
        """
        ...

    def dedup_candidates(self) -> tuple[DedupCandidate, ...]:
        """중복 판정에 넣을 초안 전부 + 각 초안의 **원자료 게시일**(SPEC §4.1).

        ⚠️ **걸러내지 않는다** — 무엇을 판정 대상으로 볼지는 `pipeline/dedup`이 정한다
        (이단·마감으로 거절된 행은 그쪽에서 뺀다). 저장소가 정책을 알면 규칙을 고칠 때
        순수 함수 테스트가 아니라 저장소 테스트를 고쳐야 한다.

        ⚠️ **전체를 한 번에 준다.** 중복은 글 하나만 보고 판정할 수 없어서(SPEC §4.1) 배치로
        쪼갤 수 없다 — 1번째 글을 볼 때 31번째가 없으면 대표가 순서에 따라 달라진다.
        """
        ...

    def apply_dedup(self, updates: Sequence[DedupUpdate]) -> int:
        """판정을 반영하고 **실제로 바뀐 행 수**를 돌려준다.

        ⚠️ **한 번에 쓴다.** 행마다 파일을 다시 쓰면 3,000번 재작성이고, 중간에 죽으면
        일부만 반영된 원장이 남는다.

        ⚠️ 없는 `review_data_id`는 `StoreError`다 — 조용히 넘기면 판정이 사라진 것을 아무도
        모른다. 값이 이미 같은 행은 세지 않는다(멱등).
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
