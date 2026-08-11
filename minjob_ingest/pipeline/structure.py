"""구조화 패스 — 원자료 1건을 판정해 검수 초안으로 만든다(SPEC §4·§5).

```
source_data ──▶ 빈 공고?  ──예──▶ structured_at 만 (Gemini 호출 없음)
                  │아니오
                  ▼
              이미지 있나? ──예──▶ 아무것도 하지 않음 (2단계 대기 · 아래 ⚠️)
                  │아니오
                  ▼
              Gemini 추출 ──실패──▶ structured_at 없음 + 시도 +1  (다음 실행이 재시도)
                  │성공
                  ▼
              게이트1 NO ──▶ structured_at 만 (초안 없음 · 제외됐음을 기록)
              게이트1 YES/UNCERTAIN ──▶ review_data(PENDING) + structured_at
```

**한 패스에서 끝내고 완성된 레코드를 한 번만 INSERT**한다 — 넣었다 고치지 않는다.

⚠️ **`run_id`는 `source_data.run_id`를 승계**하고 이 패스는 `crawl_run`을 만들지 않는다
(SPEC §2 · 집계가 전부 게시판 단위라 공고 단위 작업이 들어갈 칸이 없다).

⚠️ **소스 간에도 순차**다. JsonStore가 파일 전체를 다시 쓰는 구조라 동시 갱신이 서로를
덮어쓴다 — 잃는 것이 `structured_at`이면 Gemini 재과금이고 초안이면 조용한 유실이다.
병렬은 Supabase 전환(ROADMAP 1-6) 이후.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, assert_never

from minjob_ingest.domain import Confidence, DenominationSource, IsChurchRecruitment
from minjob_ingest.lib.gemini import GeminiError
from minjob_ingest.models import ReviewData, SourceData
from minjob_ingest.pipeline.extraction import Extraction, ExtractionError
from minjob_ingest.store.base import Store, StoreError
from minjob_ingest.store.serde import SerdeError

_LOG = logging.getLogger(__name__)

#: 리포트에 담을 실패 표본 수. 전부 남기면 대량 실패 때 리포트가 로그가 된다
#: (`collect.py`가 같은 이유로 3건만 남긴다).
_FAILURE_SAMPLE_SIZE: Final = 5

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
    #: 내용이 이미지에 있어 **2단계(멀티모달)까지 미뤘다**. 판정을 남기지 않는다.
    DEFERRED = "DEFERRED"
    #: 호출·응답·저장이 실패했다. `structured_at`을 남기지 않아 다음 실행이 재시도한다.
    FAILED = "FAILED"


class Extractor(Protocol):
    """파이프라인이 AI에게 필요한 전부. 구현은 `extraction.GeminiExtractor`.

    구상 클래스가 아니라 프로토콜에 의존하는 이유: 테스트가 네트워크·유료 호출 없이
    돌아야 한다(가드레일 #7·#10).
    """

    def extract(self, record: SourceData) -> Extraction: ...


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
    #: 매 실행 같은 것이 앞에 온다 — 실측: 이미지 237건 때문에 `--limit 20`이 7번 만에 굶는다).
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
    error: str | None = None


@dataclass(frozen=True, slots=True)
class StructureReport:
    """실행 집계. 진행 중에는 지금까지의 누적이고, 끝나면 최종값이다."""

    scanned: int = 0
    drafted: int = 0
    excluded: int = 0
    empty: int = 0
    deferred: int = 0
    failed: int = 0
    failures: tuple[StructureFailure, ...] = ()


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
    failures: list[StructureFailure] = field(default_factory=list)

    @property
    def judged(self) -> int:
        """Gemini를 부를 수 있었던 건수 = `limit`이 세는 단위(위 `StructureOptions.limit`)."""
        return self.drafted + self.excluded + self.failed

    def add(self, result: StructureResult) -> None:
        self.scanned += 1
        # ⚠️ `assert_never`가 있어야 진짜 exhaustive다 — `match`만으로는 `add`가 `None`을
        # 반환해 mypy가 빠진 분기를 잡지 못하고, 새 판정이 어느 칸에도 안 세어진다.
        match result.verdict:
            case Verdict.DRAFTED:
                self.drafted += 1
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
            failures=tuple(self.failures),
        )


def structure_pending(
    store: Store,
    extractor: Extractor,
    options: StructureOptions,
    *,
    on_result: ResultSink | None = None,
) -> StructureReport:
    """미판정 원자료를 오래된 것부터 처리한다.

    ⚠️ **한 건의 실패가 나머지를 멈추지 않는다**(CLAUDE.md "에러는 경계에서만"). 실패는
    세어서 리포트로 돌리고 다음 공고로 간다 — 3,188건짜리 실행이 350번째에서 죽으면
    나머지에 영원히 도달하지 못한다.
    """
    pending = store.list_unstructured(_ALL_LIMIT, source_key=options.source_key)
    if len(pending) == _ALL_LIMIT:
        # 조용한 부분 성공을 만들지 않는다 — 리포트만 보면 전량을 끝낸 것처럼 보인다.
        _LOG.warning("조회 상한 %d건에 걸렸다 — 남은 공고가 더 있다", _ALL_LIMIT)
    tally = _Tally()
    for record in pending:
        result = structure_one(record, store, extractor, dry_run=options.dry_run)
        tally.add(result)
        if on_result is not None:
            on_result(result, tally.report())
        if options.limit is not None and tally.judged >= options.limit:
            break
    return tally.report()


def structure_one(
    record: SourceData, store: Store, extractor: Extractor, *, dry_run: bool = False
) -> StructureResult:
    """공고 1건을 판정하고 저장한다. 예외를 밖으로 던지지 않는다 — 실패도 결과다."""
    try:
        return _judge(record, store, extractor, dry_run=dry_run)
    except (StoreError, SerdeError) as err:
        # ⚠️ 저장이 깨진 행 **하나** 때문에 배치가 멈추면 뒤의 수천 건에 영원히 도달하지
        # 못한다(SPEC §4 글 단위 격리). 시도 횟수도 못 올리므로(그 기록 역시 저장이다)
        # 이 행은 매 실행 다시 시도된다 — 리포트에 남겨 운영자가 원인을 고치게 한다.
        return StructureResult(record=record, verdict=Verdict.FAILED, error=_reason(err))


def build_draft(record: SourceData, extraction: Extraction) -> ReviewData:
    """검수 초안 조립.

    ⚠️ **1단계는 4필드뿐이라 근거가 없다** → `confidence=LOW`(운영자 우선검토) ·
    `denomination_source=UNKNOWN`(교단은 명시만 확정 · SPEC §5.3). 나머지 필드는 2단계에서
    채운다. 게이트1 `UNCERTAIN`은 레코드 불변식이 `LOW`를 요구하기도 한다(SPEC §5.1).

    `run_id`는 **수집 실행**을 승계한다(SPEC §2).
    """
    return ReviewData(
        source_data_id=record.id,
        run_id=record.run_id,
        is_church_recruitment=extraction.is_church_recruitment,
        confidence=Confidence.LOW,
        denomination_source=DenominationSource.UNKNOWN,
        source_url=record.source_url,
        church_name=extraction.church_name,
        title=extraction.title,
        description=extraction.description,
    )


def waits_for_images(record: SourceData) -> bool:
    """내용이 이미지에 있어 1단계가 판정하면 안 되는 공고인가.

    ⚠️ **1단계 프롬프트는 텍스트만 보낸다.** 그대로 판정하면 포스터 공고가 "내용 없음"으로
    읽혀 게이트1 `NO`가 되고, `structured_at`이 찍혀 **2단계가 다시 볼 수 없다**(판정은
    단조 증가라 되돌리는 코드 경로가 없다 · ROADMAP 1-2). 실측 237건(7.4%)이 이미지를
    갖고 있고 그중 116건은 본문이 아예 없다(2026-08-10).

    → 판정도 시도 횟수도 남기지 않고 **그대로 둔다**. 2단계가 멀티모달을 붙이면 이 함수는
    사라지고 같은 행이 정상 처리된다.
    """
    return bool(record.image_urls) or any(item.is_image for item in record.attachments)


def _judge(
    record: SourceData, store: Store, extractor: Extractor, *, dry_run: bool
) -> StructureResult:
    if record.is_empty:
        # 빈 입력에 돈을 쓰지 않는다. 판정은 남겨야 매 실행 다시 집히지 않는다(SPEC §4).
        _record_verdict(record, store, dry_run=dry_run)
        return StructureResult(record=record, verdict=Verdict.EMPTY)
    if waits_for_images(record):
        return StructureResult(record=record, verdict=Verdict.DEFERRED)
    try:
        extraction = extractor.extract(record)
    except (GeminiError, ExtractionError) as err:
        return _note_failure(record, store, err, dry_run=dry_run)
    if extraction.is_church_recruitment is IsChurchRecruitment.NO:
        _record_verdict(record, store, dry_run=dry_run)
        return StructureResult(record=record, verdict=Verdict.EXCLUDED, extraction=extraction)
    try:
        # ⚠️ `--dry-run`에서도 **만들어 본다**. 조립을 건너뛰면 리허설은 통과하고 본 실행만
        # 터진다 — 미리보기의 목적이 "이대로 저장해도 되는가"를 보는 것이라 무의미해진다.
        draft = build_draft(record, extraction)
    except ValueError as err:
        # 모델 값이 레코드 불변식과 어긋났다(SPEC §5.1·§6). 저장할 수 없는 초안이므로 실패다.
        return _note_failure(record, store, err, dry_run=dry_run)
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
        record=record, verdict=Verdict.DRAFTED, extraction=extraction, draft=draft
    )


def _record_verdict(record: SourceData, store: Store, *, dry_run: bool) -> None:
    """초안 없이 판정만 기록한다(빈 공고·게이트1 NO).

    ⚠️ 이걸 빼면 "제외된 공고"와 "구조화 실패"를 구분할 수 없어, 제외된 공고를 **매 실행
    Gemini에 재전송하는 비용 루프**가 된다(SPEC §4).
    """
    if not dry_run:
        store.update_structure_state(record.with_verdict_recorded())


def _note_failure(
    record: SourceData, store: Store, err: Exception, *, dry_run: bool
) -> StructureResult:
    """실패를 기록한다 — `structured_at`은 남기지 않으므로 다음 실행이 다시 집는다.

    시도 횟수는 올린다. 상한(`MAX_STRUCTURE_ATTEMPTS`)에 닿으면 `list_unstructured`가
    빼주므로 영구 실패가 무한 재호출되지 않는다(SPEC §4).
    """
    reason = _reason(err)
    if not dry_run:
        try:
            store.update_structure_state(record.with_failed_attempt(reason))
        except (StoreError, SerdeError) as store_err:
            # ⚠️ 저장 실패로 **원인을 덮지 않는다**. 운영자가 봐야 하는 것은 첫 번째 실패다
            # (`last_structure_error`가 존재하는 이유이기도 하다) — 둘 다 남긴다.
            reason = f"{reason} · 실패 기록도 실패({_reason(store_err)})"
    return StructureResult(record=record, verdict=Verdict.FAILED, error=reason)


def _reason(err: Exception) -> str:
    """리포트에 남길 실패 사유. 종류를 붙여 "연결이 문제였나, 응답이 문제였나"를 가른다."""
    return f"{type(err).__name__}: {err}"
