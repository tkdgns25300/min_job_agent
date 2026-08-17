"""구조화 패스 — 원자료 1건을 판정해 검수 초안으로 만든다(SPEC §4·§5).

```
source_data ──▶ 빈 공고?  ──예──▶ structured_at 만 (Gemini 호출 없음)
                  │아니오
                  ▼
              그림 있나? ──예──▶ 바이트 확보(못 받으면 사유만 남기고 계속)
                  │
                  ▼
              Gemini 추출 (텍스트 + 그림) ──실패──▶ structured_at 없음 + 시도 +1
                  │성공
                  ▼
              게이트1 NO ──▶ structured_at 만 (초안 없음 · 제외됐음을 기록)
              게이트1 YES/UNCERTAIN ──▶ review_data(APPROVED 또는 PENDING · §5.7) + structured_at
```

**한 패스에서 끝내고 완성된 레코드를 한 번만 INSERT**한다 — 넣었다 고치지 않는다.

⚠️ **`run_id`는 `source_data.run_id`를 승계**하고 이 패스는 `crawl_run`을 만들지 않는다
(SPEC §2 · 집계가 전부 게시판 단위라 공고 단위 작업이 들어갈 칸이 없다).

⚠️ **게시판 간 병렬 · 게시판 안은 순차**(SPEC §3). 게시판 하나를 스레드 하나가 통째로 맡는다 —
그래서 그 게시판의 접속 클라이언트(요청 간격·세션 쿠키)를 아무도 같이 건드리지 않고, fetch
층에 잠금이 필요 없다. 스레드가 공유하는 것은 저장과 집계 둘뿐이고 각각 락 하나로 세운다.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final, Protocol, assert_never

from minjob_ingest.domain import (
    Confidence,
    IsChurchRecruitment,
    JobKind,
    Position,
    RejectReason,
    ReviewStatus,
)
from minjob_ingest.lib.gemini import GeminiError
from minjob_ingest.models import ReviewData, SourceData
from minjob_ingest.pipeline.confidence import grade, review_status_for
from minjob_ingest.pipeline.denomination import confirm
from minjob_ingest.pipeline.extraction import Extraction, ExtractionError
from minjob_ingest.pipeline.heresy import HeresyMatch, HeresyRef, screen
from minjob_ingest.pipeline.media import Media, MediaSet, MediaSource, failure_note, wanted_urls
from minjob_ingest.pipeline.normalize import clean_title, closed_by_board
from minjob_ingest.pipeline.verify import VerifyReport, verify
from minjob_ingest.store.base import Store, StoreError
from minjob_ingest.store.serde import SerdeError

_LOG = logging.getLogger(__name__)

#: 리포트에 담을 실패 표본 수. 전부 남기면 대량 실패 때 리포트가 로그가 된다
#: (`collect.py`가 같은 이유로 3건만 남긴다).
_FAILURE_SAMPLE_SIZE: Final = 5

#: 저장이 **연속으로** 이만큼 실패하면 실행을 멈춘다. 글 단위 격리는 한 행이 깨진 경우를
#: 위한 것이고, 원장 파일이 통째로 깨진 경우에는 격리가 오히려 독이 된다 — 공고마다 Gemini를
#: 부른 뒤 저장이 실패해 **돈만 쓰고 아무것도 남지 않는다**(ROADMAP 1-2).
#: ⚠️ 값이 작으면 한 행이 깨진 정상 상황에서 실행이 멎는다. 게시판을 나눠 도는 것도 감안해
#: 여유를 둔다.
STORE_FAILURE_LIMIT: Final = 5

#: 동시에 돌릴 **게시판 수**. 자원 보호용 상한이라 정책이 아니라 실행 옵션이다(CLAUDE.md
#: fetch 층) — CLI의 `--workers`가 덮어쓴다. 올리기 전에 Vertex 분당 요청 한도를 확인한다:
#: 넘기면 429가 오고 SDK가 기다렸다 다시 걸어 **결국 한도만큼만 나간다**.
DEFAULT_WORKERS: Final = 4

#: 일반직인데 무슨 일인지 안 적힌 공고의 `role`. min_job DATA.md §3이 정한 fallback이다
#: ("못 맞추면 기타") — 비워두면 레코드 불변식에 걸려 그 공고가 사라진다.
_GENERAL_ROLE_FALLBACK: Final = "기타"

#: `--all`의 조회 상한. JSON 단계에선 `list_unstructured`가 어차피 전량을 읽고 자르므로
#: 배치로 나눠도 얻는 것이 없다. ⚠️ Supabase 전환 때 배치 루프로 바꾼다(ROADMAP 1-2 3단계).
_ALL_LIMIT: Final = 100_000


class Verdict(StrEnum):
    """한 공고를 어떻게 처리했나. 리포트 집계와 진행 표시가 이 값으로 갈린다."""

    #: 검수 초안을 만들었다(게이트1 YES·UNCERTAIN).
    DRAFTED = "DRAFTED"
    #: 개교회 채용이 아니라 초안을 만들지 않았다(게이트1 NO). **실패가 아니다.**
    EXCLUDED = "EXCLUDED"
    #: 넣을 내용이 없어 Gemini를 부르지 않았다(`SourceData.is_empty`).
    EMPTY = "EMPTY"
    #: 그림이 있는데 **가져올 수단 없이** 실행돼 미뤘다. 판정을 남기지 않는다.
    #: ⚠️ CLI는 항상 그림 소스를 넘기므로 여기서는 나오지 않는다 — 그림 없이 부르는
    #: 프로그램 경로(테스트·배치 도구)를 위한 안전장치다.
    DEFERRED = "DEFERRED"
    #: 호출·응답·저장이 실패했다. `structured_at`을 남기지 않아 다음 실행이 재시도한다.
    FAILED = "FAILED"


#: Gemini를 부르지 않은 판정 — 유료 상한(`_Budget`)을 먹지 않는다. `_Tally.judged`가 세는
#: 것의 여집합이라 **둘이 갈라지면 상한이 틀어진다**(적합성은 테스트가 지킨다).
_FREE_VERDICTS: Final = frozenset({Verdict.EMPTY, Verdict.DEFERRED})


class Extractor(Protocol):
    """파이프라인이 AI에게 필요한 전부. 구현은 `extraction.GeminiExtractor`.

    구상 클래스가 아니라 프로토콜에 의존하는 이유: 테스트가 네트워크·유료 호출 없이
    돌아야 한다.
    """

    def extract(self, record: SourceData, images: Sequence[Media] = ()) -> Extraction: ...


@dataclass(frozen=True, slots=True)
class StructureOptions:
    """실행 옵션.

    ⚠️ **범위(`limit`)에 기본값을 두지 않는다.** 유료 호출이라 "그냥 실행"이 전량으로
    번지면 안 된다 — CLI가 `--limit N` 또는 `--all`을 반드시 받는다(ROADMAP 1-2).
    """

    #: **판정할** 최대 건수. `None` = 전량(`--all`).
    #:
    #: ⚠️ "훑을 건수"가 아니라 **"판정할 건수"** 다 — 이 값은 비용 상한이고, Gemini를 부르지
    #: 않는 공고(빈 공고·이미지 대기)는 세지 않는다. 훑은 수로 세면 미판정 목록 앞머리에 쌓인
    #: 대기 공고가 상한을 다 먹어 **뒤의 공고에 영원히 도달하지 못한다**(수집 시각 순 정렬이라
    #: 매 실행 같은 것이 앞에 온다 — 실측: 그림 대기 237건 때문에 `--limit 20`이 7번 만에 굶었다).
    limit: int | None
    #: 한 게시판만. 프롬프트를 다듬을 때 표본을 고르는 수단이다(수집 시각 순이라
    #: 필터가 없으면 오래된 한 게시판만 계속 나온다).
    source_key: str | None = None
    #: 호출은 하되 **아무것도 저장하지 않는다**. ⚠️ 프롬프트를 고치며 비교하려면 필수다 —
    #: 저장하면 그 공고에 판정이 찍혀 **같은 표본이 다시 나오지 않는다**.
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 1:
            raise ValueError(f"limit는 1 이상이어야 함 ({self.limit})")


@dataclass(frozen=True, slots=True)
class StructureFailure:
    """실패 표본 한 줄. 개수만으로는 무엇이 깨졌는지 알 수 없다."""

    posting: str
    reason: str


@dataclass(frozen=True, slots=True)
class StructureResult:
    """공고 1건의 결과. 진행 표시와 `--dry-run` 출력이 이걸 그대로 읽는다."""

    record: SourceData
    verdict: Verdict
    extraction: Extraction | None = None
    #: 저장됐거나 **저장됐을** 초안. ⚠️ `--dry-run`이 이걸 보여줘야 한다 — 모델 답만 보여주면
    #: 운영자는 실제로 무엇이 들어가는지(`confidence`·교단 근거·검수 상태) 알 수 없다.
    draft: ReviewData | None = None
    #: 그림을 놓쳤을 때의 사유. **실패가 아니다** — 텍스트만으로 판정했다는 표시다(SPEC §3).
    media_note: str | None = None
    #: 원문 대조 결과. 비운 칸이 있으면 리포트가 센다.
    verified: VerifyReport = field(default_factory=VerifyReport)
    error: str | None = None
    #: 실패가 **저장**에서 났나. ⚠️ Gemini 실패와 갈라야 한다 — 원장이 통째로 깨지면 공고마다
    #: **부른 뒤에** 저장이 실패해서, 실행이 그대로면 3,000번 과금하고 아무것도 남지 않는다.
    store_failed: bool = False


@dataclass(frozen=True, slots=True)
class StructureReport:
    """실행 집계. 진행 중에는 지금까지의 누적이고, 끝나면 최종값이다."""

    scanned: int = 0
    drafted: int = 0
    excluded: int = 0
    empty: int = 0
    deferred: int = 0
    failed: int = 0
    #: 만들면서 거절한 초안 수와 사유별 횟수(이단·마감 · SPEC §5.4·§5.4b). ⚠️ **초안 수에
    #: 포함된다** — 초안은 만들어졌고 검수 큐에 안 뜰 뿐이다. 이 숫자가 안 보이면
    #: "잘못 걸러도 영원히 드러나지 않는다"가 그대로 일어난다.
    rejected: int = 0
    rejected_reasons: Mapping[str, int] = field(default_factory=dict)
    #: 검수 상태별 초안 수(SPEC §5.7). ⚠️ **`APPROVED`는 사람을 거치지 않고 공개된다** —
    #: 이 숫자가 안 보이면 규칙이 느슨해진 것을 실행 화면에서 알아챌 수 없다.
    #: ⚠️ 등급이 아니라 **상태**로 센다: 거절된 초안도 등급은 `high`일 수 있다.
    statuses: Mapping[str, int] = field(default_factory=dict)
    #: 그림을 하나라도 못 읽고 텍스트만으로 판정한 공고 수. **실패가 아니지만 조용히
    #: 넘기면 안 된다** — 포스터 공고가 그렇게 판정되면 되돌릴 수 없다.
    text_only: int = 0
    #: 원문에 없어 **비운** 칸 수와 그 칸 이름별 횟수. 프롬프트를 고쳤을 때 나아졌는지가
    #: 이 숫자로 보인다(없으면 매번 원문과 손으로 대조해야 한다).
    scrubbed: int = 0
    scrubbed_fields: Mapping[str, int] = field(default_factory=dict)
    #: 원문에서 확인 못 했지만 **비우지 않은** 값 수 — 그림·PDF 공고에서만 생긴다.
    unverifiable: int = 0
    #: 조립 칸에서 원문과 어긋난 값 수와 칸별 횟수. ⚠️ **실패는 아니지만 무죄도 아니다** —
    #: 프롬프트가 이으라고 시킨 결과일 수도, 지어낸 것일 수도 있고 코드는 구분하지 못한다.
    unchecked: int = 0
    unchecked_fields: Mapping[str, int] = field(default_factory=dict)
    failures: tuple[StructureFailure, ...] = ()
    #: 그림 실패 사유 표본.
    media_failures: tuple[StructureFailure, ...] = ()
    #: 실행을 중간에 멈춘 사유. `None`이면 끝까지 돌았다.
    halted: str | None = None


#: 진행 상황 수신자 — 방금 끝난 1건과 그때까지의 누적. CLI가 화면에 그린다.
#: ⚠️ 집계를 파이프라인이 넘긴다: 받는 쪽이 따로 세면 같은 스트림을 두 번 세게 되고
#: 두 숫자가 갈라진다(`collect.py`의 `Progress`와 같은 이유).
ResultSink = Callable[[StructureResult, StructureReport], None]


@dataclass
class _Tally:
    """집계 누적기. 리포트가 frozen이라 여기 모았다가 필요할 때 만든다."""

    scanned: int = 0
    drafted: int = 0
    excluded: int = 0
    empty: int = 0
    deferred: int = 0
    failed: int = 0
    text_only: int = 0
    rejected_reasons: Counter[str] = field(default_factory=Counter)
    statuses: Counter[str] = field(default_factory=Counter)
    scrubbed_fields: Counter[str] = field(default_factory=Counter)
    unverifiable: int = 0
    unchecked_fields: Counter[str] = field(default_factory=Counter)
    failures: list[StructureFailure] = field(default_factory=list)
    media_failures: list[StructureFailure] = field(default_factory=list)
    consecutive_store_failures: int = 0
    halted: str | None = None

    @property
    def judged(self) -> int:
        """Gemini를 부를 수 있었던 건수 = `limit`이 세는 단위(위 `StructureOptions.limit`)."""
        return self.drafted + self.excluded + self.failed

    def add(self, result: StructureResult) -> None:
        self.scanned += 1
        self._watch_store(result)
        self.scrubbed_fields.update(result.verified.scrubbed)
        self.unverifiable += result.verified.unverifiable
        self.unchecked_fields.update(result.verified.unchecked_fields)
        if result.media_note is not None:
            self.text_only += 1
            if len(self.media_failures) < _FAILURE_SAMPLE_SIZE:
                self.media_failures.append(StructureFailure(result.record.label, result.media_note))
        # ⚠️ `assert_never`가 있어야 진짜 exhaustive다 — `match`만으로는 `add`가 `None`을
        # 반환해 mypy가 빠진 분기를 잡지 못하고, 새 판정이 어느 칸에도 안 세어진다.
        match result.verdict:
            case Verdict.DRAFTED:
                self.drafted += 1
                if result.draft is not None:
                    # ⚠️ 등급이 아니라 **검수 상태**로 센다 — 거절된 초안도 등급은 `high`일 수
                    #    있어서(SPEC §5.7) 등급으로 세면 "자동 승인"이 실제보다 커 보인다.
                    self.statuses[result.draft.review_status.value] += 1
                    if result.draft.reject_reason is not None:
                        self.rejected_reasons[result.draft.reject_reason.value] += 1
            case Verdict.EXCLUDED:
                self.excluded += 1
            case Verdict.EMPTY:
                self.empty += 1
            case Verdict.DEFERRED:
                self.deferred += 1
            case Verdict.FAILED:
                self._note_failure(result)
            case unhandled:
                assert_never(unhandled)

    def _watch_store(self, result: StructureResult) -> None:
        """저장이 연속으로 실패하면 멈춘다 — 원장이 깨진 것이지 이 공고가 이상한 게 아니다."""
        if not result.store_failed:
            self.consecutive_store_failures = 0
            return
        self.consecutive_store_failures += 1
        if self.consecutive_store_failures >= STORE_FAILURE_LIMIT and self.halted is None:
            self.halted = (
                f"저장이 연속 {self.consecutive_store_failures}번 실패해 멈췄다"
                " — 원장 파일을 확인한다(호출 비용만 나가고 아무것도 저장되지 않는다)"
            )

    def _note_failure(self, result: StructureResult) -> None:
        self.failed += 1
        if len(self.failures) < _FAILURE_SAMPLE_SIZE:
            self.failures.append(StructureFailure(result.record.label, result.error or "사유 없음"))

    def report(self) -> StructureReport:
        return StructureReport(
            scanned=self.scanned,
            drafted=self.drafted,
            excluded=self.excluded,
            empty=self.empty,
            deferred=self.deferred,
            failed=self.failed,
            text_only=self.text_only,
            rejected=sum(self.rejected_reasons.values()),
            rejected_reasons=dict(self.rejected_reasons),
            statuses=dict(self.statuses),
            scrubbed=sum(self.scrubbed_fields.values()),
            scrubbed_fields=dict(self.scrubbed_fields),
            unverifiable=self.unverifiable,
            unchecked=sum(self.unchecked_fields.values()),
            unchecked_fields=dict(self.unchecked_fields),
            failures=tuple(self.failures),
            media_failures=tuple(self.media_failures),
            halted=self.halted,
        )


class _Budget:
    """유료 호출 상한을 게시판들이 나눠 쓴다. `None`이면 상한 없음(`--all`).

    ⚠️ **부르기 전에 자리를 잡는다.** 부르고 나서 세면 동시에 돌던 게시판들이 각자 한 건씩
    더 보내 **상한을 워커 수만큼 넘긴다** — `--limit 20`이 24건 청구되는 경로를 만들지 않는다.

    ⚠️ **Gemini를 부르지 않은 판정은 자리를 돌려준다**(빈 공고·그림 대기). 안 돌려주면 미판정
    목록 앞머리에 쌓인 대기 공고가 상한을 다 먹어 뒤의 공고에 영원히 도달하지 못한다
    (`StructureOptions.limit` 참조 — 실측: 그림 대기 237건 때문에 `--limit 20`이 7번 만에 굶었다).
    """

    def __init__(self, limit: int | None) -> None:
        self._left = limit
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._left is None:
                return True
            if self._left <= 0:
                return False
            self._left -= 1
            return True

    def give_back(self) -> None:
        with self._lock:
            if self._left is not None:
                self._left += 1


def group_by_source(pending: Sequence[SourceData]) -> tuple[tuple[SourceData, ...], ...]:
    """게시판별로 묶는다 — **큰 게시판이 먼저 출발**하고, 게시판 안은 수집 시각 순 그대로.

    ⚠️ 전체 실행 시간은 **가장 큰 게시판 하나**가 정한다(CSU 731건). 작은 것부터 내보내면
    큰 게시판이 마지막에 혼자 남아 병렬이 무의미해진다 — 워커를 늘려도 줄지 않는 하한이라
    큰 게시판은 `--source`로 따로 돌리는 편이 낫다(RUNBOOK).
    """
    boards: dict[str, list[SourceData]] = {}
    for record in pending:
        boards.setdefault(record.source_key, []).append(record)
    ordered = sorted(boards.values(), key=len, reverse=True)
    return tuple(tuple(records) for records in ordered)


def structure_pending(
    store: Store,
    extractor: Extractor,
    options: StructureOptions,
    *,
    heresy: HeresyRef,
    on_result: ResultSink | None = None,
    images: MediaSource | None = None,
    workers: int = DEFAULT_WORKERS,
) -> StructureReport:
    """미판정 원자료를 게시판별로 나눠 처리한다 — 게시판 간 병렬 · 게시판 안은 오래된 것부터.

    ⚠️ **한 건의 실패가 나머지를 멈추지 않는다**(CLAUDE.md "에러는 경계에서만"). 실패는
    세어서 리포트로 돌리고 다음 공고로 간다 — 3,188건짜리 실행이 350번째에서 죽으면
    나머지에 영원히 도달하지 못한다.

    ⚠️ **집계와 진행 표시는 락 안에서 한 스레드씩** 한다. `_Tally`도 `on_result`가 쓰는
    화면·파일도 동시 접근을 견디지 않는다 — 여기서 세우면 받는 쪽은 병렬을 몰라도 된다.
    """
    if workers < 1:
        raise ValueError(f"workers는 1 이상이어야 함 ({workers})")
    pending = store.list_unstructured(_ALL_LIMIT, source_key=options.source_key)
    if len(pending) == _ALL_LIMIT:
        # 조용한 부분 성공을 만들지 않는다 — 리포트만 보면 전량을 끝낸 것처럼 보인다.
        _LOG.warning("조회 상한 %d건에 걸렸다 — 남은 공고가 더 있다", _ALL_LIMIT)
    boards = group_by_source(pending)
    budget = _Budget(options.limit)
    tally = _Tally()
    tally_lock = threading.Lock()

    def run_board(records: Sequence[SourceData]) -> None:
        for record in records:
            if tally.halted is not None or not budget.take():
                return
            result = structure_one(
                record, store, extractor, heresy=heresy, dry_run=options.dry_run, images=images
            )
            if result.verdict in _FREE_VERDICTS:
                budget.give_back()
            with tally_lock:
                tally.add(result)
                if on_result is not None:
                    on_result(result, tally.report())

    if boards:
        with ThreadPoolExecutor(max_workers=min(workers, len(boards))) as pool:
            # `list`가 있어야 게시판 하나가 던진 예외가 여기서 다시 오른다 — 삼키면
            # 리포트만 보고 "그 게시판은 처리할 게 없었다"로 읽힌다.
            list(pool.map(run_board, boards))
    return tally.report()


def structure_one(
    record: SourceData,
    store: Store,
    extractor: Extractor,
    *,
    heresy: HeresyRef,
    dry_run: bool = False,
    images: MediaSource | None = None,
) -> StructureResult:
    """공고 1건을 판정하고 저장한다. 예외를 밖으로 던지지 않는다 — 실패도 결과다."""
    try:
        return _judge(record, store, extractor, heresy=heresy, dry_run=dry_run, images=images)
    except (StoreError, SerdeError) as err:
        # ⚠️ 저장이 깨진 행 **하나** 때문에 배치가 멈추면 뒤의 수천 건에 영원히 도달하지
        # 못한다(SPEC §4 글 단위 격리). 시도 횟수도 못 올리므로(그 기록 역시 저장이다)
        # 이 행은 매 실행 다시 시도된다 — 리포트에 남겨 운영자가 원인을 고치게 한다.
        return StructureResult(
            record=record, verdict=Verdict.FAILED, error=_reason(err), store_failed=True
        )


def build_draft(
    record: SourceData,
    extraction: Extraction,
    *,
    heresy: HeresyMatch | None = None,
    media_sent: bool,
    media_missed: bool,
) -> ReviewData:
    """검수 초안 조립.

    ⚠️ **모델이 뽑은 값은 그대로 옮기고, 판정은 규칙이 붙인다.** `dedup_key`만 아직 비어 있다
    (ROADMAP 1-3).

    ⚠️ **등급은 조립이 끝난 뒤에 매긴다**(`pipeline/confidence.py`). 판정 대상이 모델 답이
    아니라 **실제로 저장될 레코드**라 여기서 손으로 옮긴 값과 갈릴 수 없다. `high`면
    `review_status=APPROVED` — 사람을 거치지 않고 공개된다(SPEC §5.7).

    ⚠️ **그림 신호 둘은 기본값이 없다.** 빠뜨리기 쉬운데 빠뜨린 쪽이 **자동 승인**이라
    (포스터 공고는 `verify`가 어느 칸도 대조하지 않는다 · SPEC §5.5b), 새 호출자가 잊으면
    확인 안 된 값이 그대로 공개된다. 부를 때마다 답하게 한다.

    ⚠️ **이단은 목록이 아니라 판정 결과를 받는다**(`heresy`). 여기서 목록을 뒤지면 이 함수가
    파일을 알아야 하고, 기본값이 "목록 없음"이 되어 **조용히 아무도 안 걸리는** 길이 생긴다.
    `None`은 "목록에 없다"는 **참인 값**이다 — 목록을 못 읽은 경우는 CLI가 먼저 멈춘다.

    ⚠️ **교단은 규칙이 확정한다**(`pipeline/denomination.py` · SPEC §5.3 ①). 모델은 원문 표기를
    옮기기만 하고, key로 바꾸는 것은 코드다 — 모델에게 시키면 같은 `예장 합동`이 실행마다
    다른 key가 될 수 있다. 못 알아보면 `UNKNOWN`이고 지어내지 않는다.

    `run_id`는 **수집 실행**을 승계한다(SPEC §2).
    """
    classified = classify(extraction)
    denomination, denomination_source, denomination_evidence = confirm(
        extraction.raw_denomination, record
    )
    pay_min, pay_max = _pay_range(extraction)
    # ⚠️ 끝난 공고는 **만들면서 거절한다**(이단과 같은 방식 · SPEC §5.4). 그대로 두면
    #    `jobs.status` 기본값이 `OPEN`이라 **이미 채워진 자리가 공개된다**(실측 110건).
    #    레코드와 근거는 남으므로 오판을 되짚을 수 있다.
    #
    # ⚠️ **모델에게 묻지 않는다** — 게시판 상태 필드와 제목의 `청빙완료`·`마감` 표시를 보는
    #    일이라 글자만 보면 되고, 유료 호출과 실행별 흔들림 없이 코드가 판정한다.
    closed = closed_by_board(record.title, record.raw_meta)
    # ⚠️ **이단이 마감보다 우선한다.** `reject_reason`은 한 칸뿐인데, 마감은 그 공고에 관한
    #    사실이고 이단은 그 교회에 관한 사실이라 뒤에 올 공고에도 그대로 적용된다.
    reject_reason = _reject_reason(heresy, closed)
    draft = ReviewData(
        source_data_id=record.id,
        run_id=record.run_id,
        source_url=record.source_url,
        is_church_recruitment=extraction.is_church_recruitment,
        confidence=Confidence.LOW,
        denomination=denomination,
        denomination_source=denomination_source,
        denomination_evidence=denomination_evidence,
        job_kind=classified.job_kind,
        role=classified.role,
        # ⚠️ 모델이 아니라 **게시판 제목**이다 — 원문이 이미 있는데 다시 물으면 손질만 된다.
        title=clean_title(record.title),
        position=classified.position,
        department=extraction.department,
        employment_type=extraction.employment_type,
        qualification=extraction.qualification,
        headcount=extraction.headcount,
        start_timing=extraction.start_timing,
        housing_provided=extraction.housing_provided,
        housing_note=extraction.housing_note,
        pay_min=pay_min,
        pay_max=pay_max,
        pay_note=extraction.pay_note,
        pay_period=extraction.pay_period,
        benefit_note=extraction.benefit_note,
        work_days=extraction.work_days,
        requirements=extraction.requirements,
        preferred=extraction.preferred,
        required_docs=extraction.required_docs,
        optional_docs=extraction.optional_docs,
        process_steps=extraction.process_steps,
        description=extraction.description,
        # ⚠️ 게시일은 **모델에게 묻지 않는다** — 수집이 이미 목록에서 파싱해 뒀다
        #    (3,128/3,188건). 다시 뽑게 하면 게시판마다 다른 표기(`26.07.28`)를 잘못 읽어
        #    있는 날짜를 잃는다. 있는 값을 다시 사는 셈이기도 하다.
        posted_at=record.posted_on,
        deadline=extraction.deadline,
        church_name=extraction.church_name,
        region=extraction.region,
        city=extraction.city,
        address=extraction.address,
        raw_denomination=extraction.raw_denomination,
        contact_email=extraction.contact_email,
        contact_tel=extraction.contact_tel,
        contact_link=extraction.contact_link,
        contact_post=extraction.contact_post,
        heresy_flag=heresy is not None,
        heresy_evidence=heresy.evidence if heresy else None,
        review_status=ReviewStatus.REJECTED if reject_reason else ReviewStatus.PENDING,
        reject_reason=reject_reason,
    )
    # ⚠️ 등급을 매긴 뒤 검수 상태를 다시 정한다 — 등급이 조립된 레코드에서 나오므로
    #    한 번에 만들 수 없다. `replace`가 불변식을 다시 검사하므로 게이트1 `UNCERTAIN`에
    #    `high`를 매기면 여기서 레코드가 거부된다(SPEC §5.1).
    confidence = grade(draft, media_sent=media_sent, media_missed=media_missed)
    return replace(
        draft,
        confidence=confidence,
        review_status=review_status_for(confidence, reject_reason),
    )


def _reject_reason(heresy: HeresyMatch | None, closed: bool) -> RejectReason | None:
    if heresy is not None:
        return RejectReason.HERESY
    return RejectReason.CLOSED if closed else None


def waits_for_media(record: SourceData, images: MediaSource | None) -> bool:
    """그림이 있는데 **가져올 수단이 없는** 공고인가.

    ⚠️ 그대로 텍스트만으로 판정하면 포스터 공고가 "내용 없음"으로 읽혀 게이트1 `NO`가 되고,
    `structured_at`이 찍혀 **다시 볼 수 없다**(판정은 단조 증가라 되돌리는 코드 경로가 없다).
    실측 237건(7.4%)이 그림을 갖고 있고 그중 116건은 본문이 아예 없다(2026-08-10).

    → 판정도 시도 횟수도 남기지 않고 **그대로 둔다**. 그림 소스를 붙이면 저절로 사라진다.
    """
    return images is None and bool(wanted_urls(record))


def _judge(
    record: SourceData,
    store: Store,
    extractor: Extractor,
    *,
    heresy: HeresyRef,
    dry_run: bool,
    images: MediaSource | None,
) -> StructureResult:
    if record.is_empty:
        # 빈 입력에 돈을 쓰지 않는다. 판정은 남겨야 매 실행 다시 집히지 않는다(SPEC §4).
        _record_verdict(record, store, dry_run=dry_run)
        return StructureResult(record=record, verdict=Verdict.EMPTY)
    if waits_for_media(record, images):
        return StructureResult(record=record, verdict=Verdict.DEFERRED)
    # ⚠️ 그림을 못 받아도 계속한다 — 텍스트만으로 충분한 공고까지 재시도에 걸리면 안 된다.
    #    대신 무엇을 놓쳤는지 결과에 남겨 검수가 볼 수 있게 한다(SPEC §3).
    urls = wanted_urls(record)
    # ⚠️ 그림이 없는 공고에는 묻지 않는다 — 소스가 게시판에 헛되이 요청하게 된다.
    gathered = MediaSet() if images is None or not urls else images.media_for(record)
    note = failure_note(gathered, urls)
    try:
        answer = extractor.extract(record, gathered.items)
    except (GeminiError, ExtractionError) as err:
        return _note_failure(record, store, err, dry_run=dry_run, media_note=note)
    # ⚠️ **초안을 만들기 전에 검산한다.** 모델이 고쳐 쓴 값은 여기서 비워지고, 무엇을 비웠는지는
    #    결과에 실려 리포트로 올라간다 — 조용히 지우지 않는다.
    extraction, verified = verify(record, answer, media_sent=bool(gathered.items))
    if extraction.is_church_recruitment is IsChurchRecruitment.NO:
        _record_verdict(record, store, dry_run=dry_run)
        # ⚠️ 사유를 여기서도 들고 나온다 — **그림을 못 읽어 게이트1 NO 가 난 경우**가
        # 가장 알아야 할 상황인데, 그때 판정이 기록돼 되돌릴 수 없다.
        return StructureResult(
            record=record,
            verdict=Verdict.EXCLUDED,
            extraction=extraction,
            media_note=note,
            verified=verified,
        )
    try:
        # ⚠️ `--dry-run`에서도 **만들어 본다**. 조립을 건너뛰면 리허설은 통과하고 본 실행만
        # 터진다 — 미리보기의 목적이 "이대로 저장해도 되는가"를 보는 것이라 무의미해진다.
        # ⚠️ **검산이 끝난 값으로 대조한다** — `verify`가 지어낸 교회명을 이미 비웠으므로,
        #    없는 교회 이름 때문에 거절되는 일이 없다.
        draft = build_draft(
            record,
            extraction,
            heresy=screen(
                extraction.church_name, extraction.raw_denomination, extraction.region, heresy
            ),
            # ⚠️ 그림을 **보냈나**와 **못 받았나**는 뜻이 다르다(SPEC §5.7): 보낸 공고는
            #    어느 칸도 원문 대조가 안 됐고, 못 받은 공고는 내용 자체가 없을 수 있다.
            media_sent=bool(gathered.items),
            media_missed=note is not None,
        )
    except ValueError as err:
        # 모델 값이 레코드 불변식과 어긋났다(SPEC §5.1·§6). 저장할 수 없는 초안이므로 실패다.
        return _note_failure(record, store, err, dry_run=dry_run, media_note=note)
    if not dry_run:
        # ⚠️ 순서가 계약이다(store/base.py): 초안 먼저, 판정 나중. 뒤집으면 그 사이에 죽은
        # 공고가 "판정 완료 + 초안 없음"으로 남는데, SPEC §4가 "review_data 없음"을 재시도
        # 기준으로 쓰지 않아 **사후 탐지가 불가능한 유실**이 된다.
        #
        # 반환값(False = 운영자가 이미 손댄 행이라 덮지 않음)은 여기서 쓰지 않는다 —
        # 검수 UI가 생기기 전(ROADMAP 1-6)에는 발생할 수 없고, 생긴 뒤에는 "덮지 않음"을
        # 리포트에 따로 세야 한다.
        store.upsert_review_data(draft)
        store.update_structure_state(record.with_verdict_recorded())
    return StructureResult(
        record=record,
        verdict=Verdict.DRAFTED,
        extraction=extraction,
        draft=draft,
        media_note=note,
        verified=verified,
    )


@dataclass(frozen=True, slots=True)
class _Classification:
    """`job_kind`와 그에 딸린 두 칸을 **서로 맞춘** 결과."""

    job_kind: tuple[JobKind, ...]
    position: tuple[Position, ...]
    role: str | None


def classify(extraction: Extraction) -> _Classification:
    """게이트2 세 칸을 레코드가 받아들일 모양으로 맞춘다(min_job DATA.md §3 CHECK와 같은 규칙).

    ⚠️ **모순을 실패로 만들지 않는다.** 규칙은 양방향이라 어긋나는 조합이 네 가지인데
    (사역직인데 직분 없음 / 아닌데 직분 있음 / 일반직인데 직무 없음 / 아닌데 직무 있음),
    하나라도 그대로 두면 그 공고가 **3번 과금된 뒤 재시도 상한을 넘겨 조용히 사라진다**.
    스키마로는 "GENERAL일 때만 role"을 표현할 수 없어 모델 답이 어긋나는 것은 정상 범위다.

    **`job_kind`를 믿고 딸린 칸을 맞춘다** — 최상위 분류 축이기 때문이다(min_job DATA.md §3).
    없으면 채우고(`ETC`·`기타` — DATA.md가 정한 fallback), 분류에 없는 칸은 버린다.
    """
    kinds = extraction.job_kind
    ministry, general = JobKind.MINISTRY in kinds, JobKind.GENERAL in kinds
    position = extraction.position if ministry else ()
    if ministry and not position:
        position = (Position.ETC,)
    role = extraction.role if general else None
    if general and role is None:
        role = _GENERAL_ROLE_FALLBACK
    return _Classification(job_kind=kinds, position=position, role=role)


def _pay_range(extraction: Extraction) -> tuple[int | None, int | None]:
    """사례비 범위를 작은 값 → 큰 값으로 맞춘다.

    ⚠️ 뒤집힌 답을 실패로 두면 그 공고가 3번 과금된 뒤 사라진다(`classify`와 같은 이유).
    범위는 순서가 뒤바뀌어도 같은 범위이므로 바로잡는 것이 맞다.
    """
    low, high = extraction.pay_min, extraction.pay_max
    if low is not None and high is not None and low > high:
        return high, low
    return low, high


def _record_verdict(record: SourceData, store: Store, *, dry_run: bool) -> None:
    """초안 없이 판정만 기록한다(빈 공고·게이트1 NO).

    ⚠️ 이걸 빼면 "제외된 공고"와 "구조화 실패"를 구분할 수 없어, 제외된 공고를 **매 실행
    Gemini에 재전송하는 비용 루프**가 된다(SPEC §4).
    """
    if not dry_run:
        store.update_structure_state(record.with_verdict_recorded())


def _note_failure(
    record: SourceData,
    store: Store,
    err: Exception,
    *,
    dry_run: bool,
    media_note: str | None = None,
) -> StructureResult:
    """실패를 기록한다 — `structured_at`은 남기지 않으므로 다음 실행이 다시 집는다.

    시도 횟수는 올린다. 상한(`MAX_STRUCTURE_ATTEMPTS`)에 닿으면 `list_unstructured`가
    빼주므로 영구 실패가 무한 재호출되지 않는다(SPEC §4).
    """
    reason = _reason(err)
    store_failed = False
    if not dry_run:
        try:
            store.update_structure_state(record.with_failed_attempt(reason))
        except (StoreError, SerdeError) as store_err:
            # ⚠️ 저장 실패로 **원인을 덮지 않는다**. 운영자가 봐야 하는 것은 첫 번째 실패다
            # (`last_structure_error`가 존재하는 이유이기도 하다) — 둘 다 남긴다.
            reason = f"{reason} · 실패 기록도 실패({_reason(store_err)})"
            # ⚠️ **여기서도 원장이 깨진 것을 알린다.** 안 하면 모델 실패와 저장 실패가 겹칠 때
            #    (프롬프트가 깨져 전건이 `ExtractionError` + 원장 손상) 멈춤이 영원히 안 걸려
            #    3,188번 과금하고 아무것도 저장하지 못한다. `structure_attempts`도 저장이라
            #    시도 상한조차 올라가지 않아 다음 실행이 그대로 반복한다.
            store_failed = True
    return StructureResult(
        record=record,
        verdict=Verdict.FAILED,
        error=reason,
        media_note=media_note,
        store_failed=store_failed,
    )


def _reason(err: Exception) -> str:
    """리포트에 남길 실패 사유. 종류를 붙여 "연결이 문제였나, 응답이 문제였나"를 가른다."""
    return f"{type(err).__name__}: {err}"
