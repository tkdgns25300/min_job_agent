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
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, assert_never

from minjob_ingest.domain import (
    Confidence,
    DenominationSource,
    IsChurchRecruitment,
    JobKind,
    Position,
    RejectReason,
    ReviewStatus,
)
from minjob_ingest.lib.gemini import GeminiError
from minjob_ingest.models import ReviewData, SourceData
from minjob_ingest.pipeline.extraction import Extraction, ExtractionError
from minjob_ingest.pipeline.media import Media, MediaSet, MediaSource, failure_note, wanted_urls
from minjob_ingest.pipeline.normalize import clean_title, closed_by_board
from minjob_ingest.pipeline.verify import VerifyReport, verify
from minjob_ingest.store.base import Store, StoreError
from minjob_ingest.store.serde import SerdeError

_LOG = logging.getLogger(__name__)

#: 리포트에 담을 실패 표본 수. 전부 남기면 대량 실패 때 리포트가 로그가 된다
#: (`collect.py`가 같은 이유로 3건만 남긴다).
_FAILURE_SAMPLE_SIZE: Final = 5

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


@dataclass(frozen=True, slots=True)
class StructureReport:
    """실행 집계. 진행 중에는 지금까지의 누적이고, 끝나면 최종값이다."""

    scanned: int = 0
    drafted: int = 0
    excluded: int = 0
    empty: int = 0
    deferred: int = 0
    failed: int = 0
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
    scrubbed_fields: Counter[str] = field(default_factory=Counter)
    unverifiable: int = 0
    unchecked_fields: Counter[str] = field(default_factory=Counter)
    failures: list[StructureFailure] = field(default_factory=list)
    media_failures: list[StructureFailure] = field(default_factory=list)

    @property
    def judged(self) -> int:
        """Gemini를 부를 수 있었던 건수 = `limit`이 세는 단위(위 `StructureOptions.limit`)."""
        return self.drafted + self.excluded + self.failed

    def add(self, result: StructureResult) -> None:
        self.scanned += 1
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
            text_only=self.text_only,
            scrubbed=sum(self.scrubbed_fields.values()),
            scrubbed_fields=dict(self.scrubbed_fields),
            unverifiable=self.unverifiable,
            unchecked=sum(self.unchecked_fields.values()),
            unchecked_fields=dict(self.unchecked_fields),
            failures=tuple(self.failures),
            media_failures=tuple(self.media_failures),
        )


def structure_pending(
    store: Store,
    extractor: Extractor,
    options: StructureOptions,
    *,
    on_result: ResultSink | None = None,
    images: MediaSource | None = None,
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
        result = structure_one(record, store, extractor, dry_run=options.dry_run, images=images)
        tally.add(result)
        if on_result is not None:
            on_result(result, tally.report())
        if options.limit is not None and tally.judged >= options.limit:
            break
    return tally.report()


def structure_one(
    record: SourceData,
    store: Store,
    extractor: Extractor,
    *,
    dry_run: bool = False,
    images: MediaSource | None = None,
) -> StructureResult:
    """공고 1건을 판정하고 저장한다. 예외를 밖으로 던지지 않는다 — 실패도 결과다."""
    try:
        return _judge(record, store, extractor, dry_run=dry_run, images=images)
    except (StoreError, SerdeError) as err:
        # ⚠️ 저장이 깨진 행 **하나** 때문에 배치가 멈추면 뒤의 수천 건에 영원히 도달하지
        # 못한다(SPEC §4 글 단위 격리). 시도 횟수도 못 올리므로(그 기록 역시 저장이다)
        # 이 행은 매 실행 다시 시도된다 — 리포트에 남겨 운영자가 원인을 고치게 한다.
        return StructureResult(record=record, verdict=Verdict.FAILED, error=_reason(err))


def build_draft(record: SourceData, extraction: Extraction) -> ReviewData:
    """검수 초안 조립.

    ⚠️ **모델이 뽑은 값은 그대로 옮기고, 판정은 붙이지 않는다.** `confidence`·교단 확정·
    이단·`dedup_key`는 규칙이 정한다(ROADMAP 1-2 3단계). 그때까지는 `LOW`(운영자 우선검토)와
    `UNKNOWN`(교단 근거 없음)이다 — 게이트1 `UNCERTAIN`은 레코드 불변식이 `LOW`를 요구하기도
    한다(SPEC §5.1).

    ⚠️ **`raw_denomination`만 담고 `denomination`은 비운다**(SPEC §5.3). 원문 표기를
    교단 key로 확정하는 것은 규칙의 일이고, 여기서 찍으면 근거 없는 값이 공개로 흐른다.

    `run_id`는 **수집 실행**을 승계한다(SPEC §2).
    """
    classified = classify(extraction)
    pay_min, pay_max = _pay_range(extraction)
    # ⚠️ 끝난 공고는 **만들면서 거절한다**(이단과 같은 방식 · SPEC §5.4). 그대로 두면
    #    `jobs.status` 기본값이 `OPEN`이라 **이미 채워진 자리가 공개된다**(실측 110건).
    #    레코드와 근거는 남으므로 오판을 되짚을 수 있다.
    #
    # ⚠️ **모델에게 묻지 않는다** — 게시판 상태 필드와 제목의 `청빙완료`·`마감` 표시를 보는
    #    일이라 글자만 보면 되고, 유료 호출과 실행별 흔들림 없이 코드가 판정한다.
    closed = closed_by_board(record.title, record.raw_meta)
    return ReviewData(
        source_data_id=record.id,
        run_id=record.run_id,
        source_url=record.source_url,
        is_church_recruitment=extraction.is_church_recruitment,
        confidence=Confidence.LOW,
        denomination_source=DenominationSource.UNKNOWN,
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
        raw_denomination=extraction.raw_denomination,
        contact_email=extraction.contact_email,
        contact_tel=extraction.contact_tel,
        contact_link=extraction.contact_link,
        contact_post=extraction.contact_post,
        review_status=ReviewStatus.REJECTED if closed else ReviewStatus.PENDING,
        reject_reason=RejectReason.CLOSED if closed else None,
    )


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
        draft = build_draft(record, extraction)
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
    if not dry_run:
        try:
            store.update_structure_state(record.with_failed_attempt(reason))
        except (StoreError, SerdeError) as store_err:
            # ⚠️ 저장 실패로 **원인을 덮지 않는다**. 운영자가 봐야 하는 것은 첫 번째 실패다
            # (`last_structure_error`가 존재하는 이유이기도 하다) — 둘 다 남긴다.
            reason = f"{reason} · 실패 기록도 실패({_reason(store_err)})"
    return StructureResult(
        record=record, verdict=Verdict.FAILED, error=reason, media_note=media_note
    )


def _reason(err: Exception) -> str:
    """리포트에 남길 실패 사유. 종류를 붙여 "연결이 문제였나, 응답이 문제였나"를 가른다."""
    return f"{type(err).__name__}: {err}"
