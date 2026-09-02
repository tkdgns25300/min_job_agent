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
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from minjob_ingest.domain import (
    CrawlMode,
    DedupState,
    Department,
    Position,
    Region,
    RejectReason,
    ReviewStatus,
)
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
class GoneTarget:
    """삭제 감지의 판정 대상 한 건(SPEC §4 gone 단계) — 원장 신원 + 내릴 곳.

    저장소는 **기계적 조건만** 거른다(창 안 · `source_gone_at IS NULL` · 공개됐거나 검수
    대기) — 무엇을 삭제로 볼지는 `pipeline/gone`이 정한다(`dedup_candidates`와 같은 분업).
    `source_gone_at`이 이미 있는 행을 빼는 것은 정책이 아니라 낭비 방지다: 이미 관측한
    사실을 매일 다시 확인하지 않고, 되살아난 공고를 자동으로 다시 열지도 않는다(ROADMAP).
    """

    review_data_id: UUID
    #: 공개된 공고면 그 `jobs` 행 — 삭제 확정 시 내릴 대상. 검수 대기면 `None`.
    published_job_id: UUID | None
    source_key: str
    external_id: str
    source_url: str
    title: str
    posted_on: date


@dataclass(frozen=True, slots=True)
class PendingWork:
    """아직 끝나지 않은 일의 개수(`status` 화면 · SPEC §7).

    ⚠️ **셋은 "0이어야 정상"이다** — `given_up`·`approved_unpublished`는 값이 있으면 사람이
    봐야 하고, `unstructured`는 유료 호출이 남았다는 뜻이다(데일리에서 0이 아니면 상한에
    걸렸거나 구조화가 중간에 죽었다). `pending_review`만 정상적으로 값을 갖는다.
    """

    #: 아직 판정하지 않은 원자료. 💰 다음 실행이 부를 대상이다.
    unstructured: int = 0
    #: 재시도 상한(3회)을 넘겨 포기한 원자료 — 원인을 사람이 봐야 한다.
    given_up: int = 0
    #: min_job 검수 페이지에 뜨는 초안.
    pending_review: int = 0
    #: ⚠️ 승인됐는데 공개되지 않은 초안. **공개 경로가 막힌 것**이다(2026-08-23 실측 9건).
    approved_unpublished: int = 0


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
        self, limit: int | None, *, source_key: str | None = None
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

    def requeue_for_structure(
        self, *, source_key: str | None = None, external_ids: Sequence[str] | None = None
    ) -> RequeueResult:
        """판정을 지워 그 공고를 **다시 구조화할 수 있게** 되돌린다.

        `external_ids`를 주면 그 게시판의 **그 공고들만** 되돌린다. ⚠️ `external_id`는 게시판
        안에서만 유일하므로 `source_key` 없이 줄 수 없다 — 주면 `ValueError`다. 이게 없으면
        결함 하나를 고치고 몇 건만 다시 판정하려 해도 게시판 전체가 재과금된다(실측: BU 3건을
        되살리려면 40건을 다시 부른다).

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

    # ── 소멸 감지 (SPEC §4 gone 단계) ────────────────────────────

    def gone_targets(self, *, since: date) -> tuple[GoneTarget, ...]:
        """삭제 감지의 판정 대상 — `since` 이후 게시 + 소멸 관측 없음 + 공개됐거나 검수 대기.

        ⚠️ **`jobs`의 상태는 보지 않는다**(스테이징만으로 답한다). 운영자가 이미 내린 공고가
        섞여도 관측 기록은 여전히 값어치가 있고("왜 내려갔나"의 근거), 내리기 자체는
        `PublishTarget.close_job`의 조건(WHERE)이 걸러낸다 — 조인 하나를 없애는 값이다.
        """
        ...

    def published_job_ids(self) -> frozenset[UUID]:
        """우리가 공개한 `jobs` 행 전부. **우리 것 판별의 정본이다**(SPEC §8) — `jobs.source`
        로는 알 수 없다(운영자 수동 등록도 `OPERATOR`다). 마감 정리가 남의 행을 닫지 않게
        `close_job` 후보를 이 집합으로 좁힌다.
        """
        ...

    def mark_gone(self, review_data_ids: Sequence[UUID], *, at: datetime) -> int:
        """원문 소멸 관측을 기록하고 **새로 기록된 행 수**를 돌려준다(이미 기록된 행은 멱등).

        ⚠️ **운영자 소유 가드를 지나지 않는다** — 판정이 아니라 게시판에서 본 사실이고,
        사람이 교정한 값과 부딪히는 칸이 아니다(`is_operator_owned`는 판정·내용 칸을 지킨다).
        ⚠️ 존재 검증은 하지 않는다 — 입력은 같은 실행의 `gone_targets`가 준 id뿐이고,
        그 사이 행이 사라지는 경로가 없다(`review_data`는 삭제되지 않는다).
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

    def recent_runs(self, limit: int) -> tuple[CrawlRun, ...]:
        """최근 실행을 **새것부터**. `status` 화면과 **데일리 창 계산**이 함께 쓴다.

        ⚠️ 순서가 계약이다 — 창 계산이 "가장 최근의 성공한 실행"을 앞에서부터 찾는다.
        """
        ...

    def all_health(self) -> tuple[SourceHealth, ...]:
        """게시판 상태 전량(30행). 문제 있는 곳만 걸러 오지 않는다 — 무엇이 문제인지는
        `pipeline.health.alerts_for`가 정하고, 그 기준이 여기로 새면 두 곳으로 갈라진다."""
        ...

    def pending_work(self) -> PendingWork:
        """남은 일의 **개수만**. 화면 한 줄씩이 되는 값들이다.

        ⚠️ **레코드를 받아 세지 않는다.** `list_unstructured`로 세면 `raw_text`·`raw_html`까지
        전량이 와서 수천 행에서 수십 MB가 된다 — 조회 한 번에 개수만 받는다.
        """
        ...


@dataclass(frozen=True, slots=True)
class JobAnchor:
    """이미 공개돼 **지금 목록에 보이는** `jobs` 한 행(SPEC §4.2).

    ⚠️ **필드 이름을 `ReviewData`와 똑같이 둔다.** 앵커는 §4.1과 **같은 키 함수**를 지나야
    하는데(`pipeline.dedup.seat_of`), 이름이 다르면 앵커용 키를 따로 만들게 되고 두 계산이
    갈라진 순간 **이미 공개된 자리를 못 알아봐 중복이 공개된다**. 구조적 프로토콜
    (`dedup.SeatSource`)이 둘을 함께 받는 것이 그 보장이다.

    ⚠️ 담는 것은 **키를 만드는 데 필요한 것 + 끌어올림 대상 판정**뿐이다. `jobs`의 나머지 칸은
    읽지 않는다 — 크롤러는 그 테이블을 앵커로만 본다(§8).
    """

    job_id: UUID
    church_name: str | None
    region: Region | None
    position: tuple[Position, ...]
    role: str | None
    department: Department | None
    posted_at: date
    #: 접수 이메일. ⚠️ **자리를 가르는 유일한 연락처다**(SPEC §4.1 5단계) — 앵커에 이 값이
    #: 없으면 메일함이 다른 다른 자리를 자동으로 거절하게 되고, 그때 **우리 공고가 사라진다**.
    contact_email: str | None = None


@dataclass(frozen=True, slots=True)
class Poster:
    """보관할 파일 하나 — 형식과 바이트.

    ⚠️ `pipeline/media.Media`와 같은 모양인데 **여기 따로 두는 이유**: `store/`가 `pipeline/`을
    가져오면 층이 뒤집힌다(파이프라인이 저장소를 부르는 방향이어야 한다). 경계에서 한 줄
    변환하는 값이 층 방향을 지키는 값보다 싸다 — `DedupCandidate`를 여기 둔 것과 같은 이유다.
    """

    media_type: str
    data: bytes


class PosterStore(Protocol):
    """포스터 보관 — 검수 화면이 그림을 띄울 수 있게 한다(docs/REVIEW_PAGE.md §7.1).

    `Store`와 나눠 두는 이유는 `PublishTarget`과 같다: **`JsonStore`에는 Storage가 없다.**
    로컬 실행에서는 이 값이 `None`이고, 그러면 `poster_paths`가 빈 채로 남는다 — 검수 화면은
    Supabase에서만 도므로 손실이 없다.

    ⚠️ **게시판이 아니라 우리 저장소로 가는 전송이다** — UA 위장·`Crawl-delay`·소스별 간격이
    붙으면 안 된다(`store/transport.py`).
    """

    def check_bucket(self) -> None:
        """버킷이 있는지 **첫 업로드 전에** 확인한다.

        ⚠️ 없으면 전량 실행이 480번 실패한다(포스터 공고 추정치). 이름이 틀렸거나 권한이 없는
        것은 **한 번 물어보면 알 수 있는 것**이라, 글마다 실패로 알아내지 않는다
        (`PublishTarget.check_jobs_columns`와 같은 자리·같은 이유).
        """

    def upload(
        self, *, source_key: str, source_data_id: UUID, posters: Sequence[Poster]
    ) -> tuple[str, ...]:
        """올리고 **경로들**을 돌려준다(`review_data.poster_paths`에 그대로 들어간다).

        경로 규약은 `{source_key}/{source_data_id}/{n}.{ext}`이고 `n`은 **받은 순서**다(0부터).
        ⚠️ 원문의 절대 인덱스가 아니다 — 못 받은 그림은 애초에 목록에 없다(`MediaSet.failures`).
        상대 순서는 원문 그대로라 여러 장인 공고도 순서가 맞는다.

        ⚠️ **경로가 결정적이라 다시 올리면 덮어쓴다** — 재구조화가 고아 파일을 만들지 않는다.
        """


class PublishTarget(Protocol):
    """`jobs` 접근 — **공개 경로만** 쓰는 별도 계약(SPEC §4.2·§4.2b·§4.3).

    `Store`와 나눠 두는 이유: `JsonStore`에는 `jobs`가 없다. `Store`에 넣고 로컬 구현이 예외를
    던지게 하면 "JSON 저장소로 공개를 시도하는" 코드가 **런타임까지 살아 있다** — 프로토콜을
    나누면 그 조합이 타입에서 표현 불가능해진다(CLAUDE.md: 잘못된 상태를 타입으로 막는다).

    ⚠️ **크롤러가 `jobs`에 쓰는 것은 두 가지뿐이다** — INSERT(§4.3)와 `posted_at` 한 칸
    UPDATE(§4.2b). 그 외 모든 행은 읽기만 한다(§8 소유권 경계). `churches`에는 접근하지 않는다.
    """

    def check_jobs_columns(self) -> None:
        """`jobs`가 우리가 아는 모양인지 **INSERT 전에** 대조한다(SPEC §4.3).

        `jobs`는 min_job 소유라 컬럼이 늘 수 있고, 그때 깨지는 곳은 **공개 테이블**이다.
        한 건 넣고 실패하면 절반만 공개된 상태가 남으므로 시작 전에 멈춘다.

        ⚠️ **확인할 수 없으면 그것도 실패다.** 모양을 모른 채 공개를 시작하지 않는다.
        """
        ...

    def visible_anchors(
        self, *, today: date, exclude: frozenset[UUID] = frozenset()
    ) -> tuple[JobAnchor, ...]:
        """지금 목록에 보이는 공고들(SPEC §4.2).

        `exclude`는 **우리 `published_job_id`로 이어진 job id**다 — 그 행은 후보에 이미 우리
        초안으로 들어와 있어서, 빼지 않으면 자기 자신과 중복 판정한다.

        ⚠️ 노출 조건은 **min_job DATA.md §6-1을 글자 그대로** 따라야 한다. 어긋나면 중복이
        새거나 자리가 사라진다(SPEC §4.2의 2026-08-21 정정이 그 사례다).
        """
        ...

    def reserve_publication(self, review_data_id: UUID) -> UUID:
        """공개할 job id를 만들어 `review_data.published_job_id`에 **먼저 적고** 돌려준다.

        ⚠️ **순서를 뒤집으면 안 된다**(SPEC §4.3). INSERT를 먼저 하고 죽으면 "공개됐는데 우리는
        모르는 행"이 남아 **매 실행 다시 공개**한다. 먼저 적어두면 다음 실행이 "적혔는데 `jobs`에
        없음"을 보고 이어서 넣는다.
        """
        ...

    def publish(self, draft: ReviewData, *, job_id: UUID, posted_at: date) -> None:
        """초안을 `jobs`에 INSERT한다(SPEC §4.3).

        `posted_at`은 **그 자리 묶음의 가장 최근 게시일**이다(§4.1) — 초안의 값이 아니다.
        `church_id`는 NULL이고 `source`는 `OPERATOR`다(교회 행은 만들지 않는다 · §8).
        """
        ...

    def bump_posted_at(self, job_id: UUID, posted_at: date) -> bool:
        """끌어올림 — `posted_at` **한 칸만** 갱신한다(SPEC §4.2b).

        Returns: 갱신했으면 True. **교회가 claim했으면 False**(그 순간 소유권이 넘어가고
        크롤러는 손을 뗀다 · §8) — 실패가 아니라 정상적인 결말이다.
        """
        ...

    def expired_job_ids(self, *, today: date) -> tuple[UUID, ...]:
        """마감이 지났는데 아직 `OPEN`인 행(claim 안 된 것만). min_job은 화면에서 가리지만
        DB 상태는 그대로 쌓인다(실측 2026-09-01: 57건) — `close_job`으로 정리할 후보다.

        ⚠️ **`deadline`은 `jobs`의 값을 쓴다**(review_data가 아니라) — 운영자가 검수에서
        고친 마감일이 정본이다(§8).
        """
        ...

    def close_job(self, job_id: UUID) -> bool:
        """원문이 사라진 공고를 내린다(SPEC §4 gone 단계) — `status` **한 칸만** 쓴다.

        Returns: 내렸으면 True. **교회가 claim했거나 이미 OPEN이 아니면 False** — 둘 다
        실패가 아니라 정상적인 결말이다(`bump_posted_at`과 같은 계약). 조건은 DB가 판정한다:
        코드 규율로 두면 언젠가 어긴다(CLAUDE.md 저장소 규칙).
        """
        ...

    def published_state(self, job_ids: Sequence[UUID]) -> Mapping[UUID, date]:
        """우리가 공개한 공고들의 **지금 `posted_at`**. 없는 id는 키가 없다.

        읽기 한 번이 두 가지에 답한다:
        - **키가 없다** = 운영자가 지웠다 → 링크를 비우고 다시 공개한다(SPEC §4.3).
        - **날짜가 다르다** = 끌어올릴 것이 있다(SPEC §4.2b). ⚠️ 이 비교가 없으면 매 실행
          같은 값을 다시 써서 리포트가 "끌어올림 N건"을 영원히 보고한다.
        """
        ...

    def count_jobs(self) -> int:
        """`jobs` 전체 행 수 — 앵커 계기판용.

        ⚠️ 앵커 0건은 정상일 수도 있다(전부 마감). `1,204행 중 0건`이라야 이상함이 드러난다 —
        노출 규칙이 어긋났는지 사람이 알아볼 유일한 신호다(SPEC §4.2).
        """
        ...

    def release_publication(self, review_data_id: UUID, job_id: UUID) -> None:
        """공개했던 job이 사라졌을 때 링크를 비운다 — 다음 실행이 다시 공개한다(SPEC §4.3)."""
        ...
