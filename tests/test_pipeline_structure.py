"""구조화 패스 테스트 — 판정·저장 순서·실패 처리.

AI는 가짜(`_FakeExtractor`)로 바꾼다. **네트워크도 유료 호출도 없다** —
`Extractor` 프로토콜이 있는 이유가 이것이다.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.clock import KST
from minjob_ingest.domain import (
    Confidence,
    Denomination,
    DenominationSource,
    Department,
    EmploymentType,
    IsChurchRecruitment,
    JobKind,
    Position,
    Qualification,
    Region,
    RejectReason,
    ReviewStatus,
    StipendPeriod,
)
from minjob_ingest.lib.gemini import GeminiError
from minjob_ingest.models import (
    MAX_STRUCTURE_ATTEMPTS,
    Attachment,
    ReviewData,
    SourceData,
    new_id,
)
from minjob_ingest.pipeline import structure
from minjob_ingest.pipeline.extraction import Extraction, ExtractionError
from minjob_ingest.pipeline.media import BoardMediaSource, Media, MediaSet
from minjob_ingest.pipeline.structure import (
    STORE_FAILURE_LIMIT,
    StructureOptions,
    StructureReport,
    StructureResult,
    Verdict,
    build_draft,
    structure_one,
    structure_pending,
)
from minjob_ingest.store.base import StoreError
from minjob_ingest.store.json_store import JsonStore
from minjob_ingest.store.serde import row_to_review_data

_NOW: Final = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
_RUN_ID: Final = new_id()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def store(data_dir: Path) -> JsonStore:
    return JsonStore(data_dir)


def _drafts(data_dir: Path) -> list[ReviewData]:
    """저장된 초안을 파일에서 직접 읽는다.

    ⚠️ 읽기용 Store 메서드를 만들지 않는다 — 파이프라인이 쓰지 않는 메서드를 프로토콜에
    올리면 계약이 넓어지고, 넓어진 만큼 Supabase 구현이 따라와야 한다(`store/base.py`).
    """
    path = data_dir / "review_data.json"
    if not path.exists():
        return []
    document: dict[str, list[dict[str, object]]] = json.loads(path.read_text(encoding="utf-8"))
    return [row_to_review_data(row) for row in document["records"]]


def _source_data(
    external_id: str = "37",
    *,
    raw_text: str = "점촌제일교회에서 전임 사역자를 청빙합니다.",
    attachments: tuple[Attachment, ...] = (),
    image_urls: tuple[str, ...] = (),
    source_key: str = "DAESHIN",
    fetched_at: datetime = _NOW,
) -> SourceData:
    return SourceData(
        source_key=source_key,
        external_id=external_id,
        source_url=f"https://example.kr/board/{external_id}",
        title=f"공고 {external_id}",
        posted_on=fetched_at.date(),
        run_id=_RUN_ID,
        fetched_at=fetched_at,
        raw_text=raw_text,
        image_urls=image_urls,
        attachments=attachments,
    )


def _extraction(
    gate1: IsChurchRecruitment = IsChurchRecruitment.YES, **overrides: object
) -> Extraction:
    return Extraction(
        is_church_recruitment=gate1,
        church_name="점촌제일교회",
        description="전임 사역자를 청빙합니다.",
        **overrides,  # type: ignore[arg-type]
    )


@dataclass
class _FakeExtractor:
    """정해둔 결과를 돌려주고 호출 횟수를 센다. 예외를 주면 그걸 던진다."""

    result: Extraction | Exception = field(default_factory=_extraction)
    calls: list[str] = field(default_factory=list)
    image_counts: list[int] = field(default_factory=list)

    def extract(self, record: SourceData, images: Sequence[Media] = ()) -> Extraction:
        self.calls.append(record.external_id)
        self.image_counts.append(len(images))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


# ── 정상 경로 ────────────────────────────────────────────────────


def test_a_draft_is_written_and_the_verdict_is_recorded(store: JsonStore, data_dir: Path) -> None:
    record = _source_data()
    store.save_source_data(record)

    result = structure_one(record, store, _FakeExtractor())

    assert result.verdict is Verdict.DRAFTED
    assert store.list_unstructured(10) == ()  # 판정이 찍혀 재구조화 대상에서 빠졌다
    drafts = _drafts(data_dir)
    assert len(drafts) == 1
    assert drafts[0].church_name == "점촌제일교회"
    assert drafts[0].review_status is ReviewStatus.PENDING


def test_the_draft_title_comes_from_the_board_not_the_model() -> None:
    """⚠️ **이 배선에 테스트가 없으면 아무도 못 잡는다.** `build_draft`의 `title=` 한 줄을
    엉뚱한 값으로 바꿔도 스위트 전체가 통과하는 상태였다(변이 테스트로 확인).

    모델에게 맡겼을 때 20건 중 6건이 끝의 마침표를 잃었다 — 게시판 제목이 원문이고
    코드가 그것을 그대로 쓴다(`normalize.clean_title` · 머리표만 뗀다).
    """
    record = replace(_source_data(), title="(끌어올림)성원교회에서 동역자를 모십니다.")

    draft = build_draft(record, _extraction())

    assert draft.title == "성원교회에서 동역자를 모십니다."


def test_the_draft_title_ignores_what_the_model_says() -> None:
    """모델이 제목을 답할 칸 자체가 없다 — 있었다면 이 테스트가 그걸 드러낸다."""
    record = replace(_source_data(), title="대구한일교회에서 파트 사역자를 모십니다.")

    draft = build_draft(record, _extraction())

    assert draft.title == record.title


def test_the_draft_inherits_the_collect_run() -> None:
    """`run_id`는 수집 실행을 승계한다 — 구조화는 자기 `crawl_run`을 만들지 않는다(SPEC §2)."""
    record = _source_data()

    draft = build_draft(record, _extraction())

    assert draft.run_id == _RUN_ID
    assert draft.source_url == record.source_url


def test_a_posting_that_never_names_its_denomination_stays_unknown() -> None:
    """⚠️ 교단은 **명시된 것만** 확정한다(SPEC §5.3 ①). 없으면 `UNKNOWN`이고 지어내지 않는다."""
    draft = build_draft(_source_data(), _extraction())

    assert draft.confidence is Confidence.LOW
    assert draft.denomination_source is DenominationSource.UNKNOWN
    assert draft.denomination is Denomination.UNKNOWN
    assert draft.denomination_evidence is None


def test_a_stated_denomination_is_confirmed_with_its_evidence() -> None:
    """원문 표기를 key로 바꾸는 것은 **코드**다 — 모델에 맡기면 같은 글자가 실행마다 갈린다."""
    record = _source_data(raw_text="대한예수교장로회 합동 점촌제일교회에서 사역자를 청빙합니다.")

    draft = build_draft(record, _extraction(raw_denomination="대한예수교장로회 합동"))

    assert draft.denomination is Denomination.HAPDONG
    assert draft.denomination_source is DenominationSource.STATED
    assert draft.denomination_evidence == "합동"
    assert draft.raw_denomination == "대한예수교장로회 합동", "원표기도 그대로 남는다"


def test_a_denomination_that_is_nowhere_in_the_source_is_not_confirmed() -> None:
    """⚠️ 근거 없는 값은 원표기만 남기고 확정하지 않는다(SPEC §5.3).

    `stated`는 운영자 검토를 건너뛰므로, 아무도 확인하지 않은 값이 거기 오면 안 된다 —
    그림을 보낸 공고는 `verify`가 값을 비우지 않고 세기만 하기 때문에 여기가 마지막 문이다.
    """
    draft = build_draft(_source_data(), _extraction(raw_denomination="대한예수교장로회 합동"))

    assert draft.denomination is Denomination.UNKNOWN
    assert draft.denomination_source is DenominationSource.UNKNOWN
    assert draft.raw_denomination == "대한예수교장로회 합동", "원표기는 운영자를 위해 남긴다"


def test_uncertain_still_becomes_a_draft(store: JsonStore, data_dir: Path) -> None:
    """경계 공고는 드롭하지 않고 운영자에게 보낸다(SPEC §5.1)."""
    record = _source_data()
    store.save_source_data(record)

    result = structure_one(
        record, store, _FakeExtractor(_extraction(IsChurchRecruitment.UNCERTAIN))
    )

    assert result.verdict is Verdict.DRAFTED
    assert len(_drafts(data_dir)) == 1


# ── 게이트1 NO · 빈 공고 ─────────────────────────────────────────


def test_gate1_no_records_the_verdict_without_a_draft(store: JsonStore, data_dir: Path) -> None:
    """⚠️ 판정을 안 남기면 제외된 공고를 매 실행 Gemini에 재전송한다(SPEC §4)."""
    record = _source_data()
    store.save_source_data(record)

    result = structure_one(record, store, _FakeExtractor(_extraction(IsChurchRecruitment.NO)))

    assert result.verdict is Verdict.EXCLUDED
    assert _drafts(data_dir) == []
    assert store.list_unstructured(10) == ()  # 다시 집히지 않는다


def test_an_empty_posting_never_reaches_the_model(store: JsonStore) -> None:
    """본문·이미지·첨부가 모두 없으면 호출하지 않는다 — 빈 입력에 돈을 쓰지 않는다."""
    record = _source_data(raw_text="   ")
    store.save_source_data(record)
    extractor = _FakeExtractor()

    result = structure_one(record, store, extractor)

    assert result.verdict is Verdict.EMPTY
    assert extractor.calls == []
    assert store.list_unstructured(10) == ()


def test_an_attachment_only_posting_is_not_empty(store: JsonStore) -> None:
    """본문이 없어도 첨부가 있으면 판단할 거리가 있다 — 건너뛰면 실제 공고가 사라진다."""
    record = _source_data(
        raw_text="", attachments=(Attachment(name="청빙공고문.hwp", url="https://e.kr/f.hwp"),)
    )
    store.save_source_data(record)
    extractor = _FakeExtractor()

    result = structure_one(record, store, extractor)

    assert result.verdict is Verdict.DRAFTED
    assert extractor.calls == [record.external_id]


# ── 실패 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [GeminiError("연결 끊김"), ExtractionError("JSON으로 읽을 수 없음")],
    ids=["전송 실패", "응답이 계약과 다름"],
)
def test_a_failure_leaves_the_posting_for_the_next_run(
    store: JsonStore, data_dir: Path, failure: Exception
) -> None:
    record = _source_data()
    store.save_source_data(record)

    result = structure_one(record, store, _FakeExtractor(failure))

    assert result.verdict is Verdict.FAILED
    assert result.error is not None and type(failure).__name__ in result.error
    pending = store.list_unstructured(10)
    assert len(pending) == 1  # 판정이 없으니 다음 실행이 다시 집는다
    assert pending[0].structured_at is None
    assert pending[0].structure_attempts == 1
    assert _drafts(data_dir) == []


def test_repeated_failure_stops_at_the_attempt_cap(store: JsonStore) -> None:
    """영구 실패가 무한 재호출되지 않는다(SPEC §4)."""
    record = _source_data()
    store.save_source_data(record)
    extractor = _FakeExtractor(GeminiError("계속 실패"))

    for _ in range(MAX_STRUCTURE_ATTEMPTS):
        pending = store.list_unstructured(10)
        assert pending, "상한 전에는 계속 재시도 대상이어야 한다"
        structure_one(pending[0], store, extractor)

    assert store.list_unstructured(10) == ()
    assert len(extractor.calls) == MAX_STRUCTURE_ATTEMPTS


# ── 저장 순서 계약 ───────────────────────────────────────────────


def test_the_draft_is_written_before_the_verdict(store: JsonStore) -> None:
    """⚠️ 순서가 뒤집히면 그 사이에 죽은 공고가 "판정 완료 + 초안 없음"으로 남는다.

    SPEC §4가 "review_data 없음"을 재시도 기준으로 쓰지 않기 때문에 **사후 탐지가 불가능한
    유실**이 된다. 초안 저장 직후에 터뜨려, 그 공고가 여전히 재시도 대상인지 본다.
    """
    record = _source_data()
    store.save_source_data(record)
    tracked = _OrderTrackingStore(store)

    with pytest.raises(RuntimeError, match="초안 직후 중단"):
        structure_one(record, tracked, _FakeExtractor())  # type: ignore[arg-type]

    assert tracked.order == ["upsert_review_data"]
    assert len(store.list_unstructured(10)) == 1, "판정이 남으면 이 공고는 영영 초안 없이 끝난다"


@dataclass
class _OrderTrackingStore:
    """호출 순서를 기록하고 초안 저장 직후에 중단시키는 대역(다른 메서드는 위임)."""

    inner: JsonStore
    order: list[str] = field(default_factory=list)

    def upsert_review_data(self, _record: object) -> bool:
        self.order.append("upsert_review_data")
        raise RuntimeError("초안 직후 중단")

    def update_structure_state(self, record: SourceData) -> None:
        self.order.append("update_structure_state")
        self.inner.update_structure_state(record)

    def list_unstructured(
        self, limit: int, *, source_key: str | None = None
    ) -> tuple[SourceData, ...]:
        return self.inner.list_unstructured(limit, source_key=source_key)


# ── 배치 ─────────────────────────────────────────────────────────


def test_the_batch_isolates_one_failure_from_the_rest(store: JsonStore) -> None:
    """한 건이 실패해도 나머지를 계속 처리한다 — 3,188건이 350번째에서 멈추면 안 된다."""
    good = _source_data("1")
    bad = _source_data("2", raw_text="본문", fetched_at=datetime(2026, 8, 10, 9, 1, tzinfo=KST))
    store.save_source_data(good)
    store.save_source_data(bad)

    report = structure_pending(store, _SelectiveExtractor({"2"}), StructureOptions(limit=10))

    assert report.scanned == 2
    assert report.drafted == 1
    assert report.failed == 1
    assert report.failures[0].posting == "DAESHIN/2"


def test_dry_run_writes_nothing(store: JsonStore, data_dir: Path) -> None:
    """⚠️ 저장하면 같은 표본이 다시 안 나와 프롬프트를 비교할 수 없다."""
    record = _source_data()
    store.save_source_data(record)
    extractor = _FakeExtractor()

    report = structure_pending(store, extractor, StructureOptions(limit=10, dry_run=True))

    assert report.drafted == 1
    assert extractor.calls == [record.external_id]  # 호출은 한다
    assert _drafts(data_dir) == []
    assert len(store.list_unstructured(10)) == 1  # 판정도 남기지 않는다


def test_the_source_filter_bounds_the_sample(store: JsonStore, data_dir: Path) -> None:
    """`--source`가 표본을 그 게시판으로 묶는다(근거는 `store/base.py` 계약)."""
    store.save_source_data(_source_data("1", source_key="DAESHIN"))
    store.save_source_data(
        _source_data("2", source_key="YTUS", fetched_at=datetime(2026, 8, 10, 9, 1, tzinfo=KST))
    )

    report = structure_pending(
        store, _FakeExtractor(), StructureOptions(limit=1, source_key="YTUS")
    )

    assert report.scanned == 1
    assert _drafts(data_dir)[0].source_url.endswith("/2")


def test_limit_must_be_at_least_one() -> None:
    """0건짜리 실행이 조용히 성공하면 운영자는 "처리할 게 없다"로 오해한다."""
    with pytest.raises(ValueError, match="limit"):
        StructureOptions(limit=0)


def test_results_reach_the_sink_in_order(store: JsonStore) -> None:
    store.save_source_data(_source_data("1"))
    store.save_source_data(_source_data("2", fetched_at=datetime(2026, 8, 10, 9, 1, tzinfo=KST)))
    seen: list[tuple[str, int]] = []

    structure_pending(
        store,
        _FakeExtractor(),
        StructureOptions(limit=10),
        on_result=lambda result, progress: seen.append(
            (result.record.external_id, progress.scanned)
        ),
    )

    # 누적을 파이프라인이 함께 넘긴다 — 받는 쪽이 따로 세면 두 숫자가 갈라진다.
    assert seen == [("1", 1), ("2", 2)]


@dataclass
class _SelectiveExtractor:
    """지정한 글에서만 실패한다(배치 격리 확인용)."""

    failing: set[str]
    calls: list[str] = field(default_factory=list)

    def extract(self, record: SourceData, _images: Sequence[Media] = ()) -> Extraction:
        self.calls.append(record.external_id)
        if record.external_id in self.failing:
            raise GeminiError("이 글만 실패")
        return _extraction()


def test_dry_run_still_assembles_the_draft(
    store: JsonStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ 미리보기가 조립을 건너뛰면 **리허설은 통과하고 본 실행만 터진다**.

    저장할 수 없는 값(요약 상한 초과)을 미리보기에서도 실패로 봐야 한다 — 아니면 사장님이
    "이대로 저장해도 된다"고 판단한 뒤 본 실행에서 같은 공고가 실패한다.
    """
    record = _source_data()
    store.save_source_data(record)
    monkeypatch.setattr(structure, "build_draft", _refuses_to_build)

    result = structure_one(record, store, _FakeExtractor(), dry_run=True)

    assert result.verdict is Verdict.FAILED, "미리보기가 조립을 건너뛰면 여기서 안 걸린다"
    assert len(store.list_unstructured(10)) == 1, "미리보기는 시도 횟수도 남기지 않는다"


def _refuses_to_build(_record: SourceData, _extraction: Extraction) -> ReviewData:
    """레코드 불변식이 거부하는 상황의 대역.

    ⚠️ 지금은 모델 답 때문에 조립이 실패하지 않는다(`classify`·`_pay_range`가 다 맞춘다) —
    그래도 **격리 자체는 고정한다**: 나중에 불변식이 늘면 크래시가 아니라 그 공고 하나의
    실패여야 한다.
    """
    raise ValueError("불변식 위반")


def test_a_draft_that_cannot_be_stored_is_a_failure(
    store: JsonStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """레코드 불변식과 어긋난 값은 크래시가 아니라 그 공고 하나의 실패다."""
    record = _source_data()
    store.save_source_data(record)
    monkeypatch.setattr(structure, "build_draft", _refuses_to_build)

    result = structure_one(record, store, _FakeExtractor())

    assert result.verdict is Verdict.FAILED
    assert len(store.list_unstructured(10)) == 1


# ── 이미지 공고는 2단계까지 미룬다 ───────────────────────────────


@pytest.mark.parametrize(
    "record",
    [
        _source_data(raw_text="", image_urls=("https://e.kr/poster.png",)),
        _source_data(
            raw_text="본문은 있지만 포스터가 진짜 내용이다", image_urls=("https://e.kr/p.jpg",)
        ),
        _source_data(
            raw_text="",
            attachments=(Attachment(name="공고.png", url="https://e.kr/공고.png"),),
        ),
    ],
    ids=["본문 없는 포스터", "본문+이미지", "이미지 첨부"],
)
def test_a_posting_with_images_is_left_untouched(
    store: JsonStore, data_dir: Path, record: SourceData
) -> None:
    """⚠️ 1단계 프롬프트는 텍스트만 보낸다.

    그대로 판정하면 포스터 공고가 "내용 없음"으로 읽혀 게이트1 `NO`가 되고, `structured_at`이
    찍혀 **2단계가 다시 볼 수 없다**(판정은 단조 증가라 되돌리는 코드 경로가 없다).
    """
    store.save_source_data(record)
    extractor = _FakeExtractor()

    result = structure_one(record, store, extractor)  # 그림 소스 없이

    assert result.verdict is Verdict.DEFERRED
    assert extractor.calls == [], "이미지를 못 보내면서 돈을 쓸 이유가 없다"
    pending = store.list_unstructured(10)
    assert len(pending) == 1, "판정이 남으면 2단계가 이 공고를 영영 못 본다"
    assert pending[0].structure_attempts == 0, "실패가 아니므로 시도로 세지 않는다"
    assert _drafts(data_dir) == []


def test_deferred_postings_are_counted_in_the_report(store: JsonStore) -> None:
    store.save_source_data(_source_data("1", image_urls=("https://e.kr/p.png",)))

    report = structure_pending(store, _FakeExtractor(), StructureOptions(limit=10))

    assert report.deferred == 1
    assert report.scanned == 1


# ── 저장이 깨진 행 ───────────────────────────────────────────────


def test_a_broken_store_row_does_not_abort_the_batch(store: JsonStore) -> None:
    """⚠️ 한 행의 저장 실패로 배치가 멈추면 뒤의 수천 건에 영원히 도달하지 못한다.

    `list_unstructured`가 수집 시각 순이라 그 행은 매 실행 맨 앞에 온다 — 배치를 죽이면
    나머지는 한 번도 처리되지 않는다.
    """
    first = _source_data("1")
    second = _source_data("2", fetched_at=datetime(2026, 8, 10, 9, 1, tzinfo=KST))
    store.save_source_data(first)
    store.save_source_data(second)
    broken = _BrokenOnFirstStore(store, poisoned="1")

    report = structure_pending(broken, _FakeExtractor(), StructureOptions(limit=10))  # type: ignore[arg-type]

    assert report.failed == 1
    assert report.drafted == 1, "뒤의 공고는 정상 처리돼야 한다"
    assert report.failures[0].posting == "DAESHIN/1"


@dataclass
class _BrokenOnFirstStore:
    """지정한 글의 초안 저장에서 `StoreError`를 내는 대역(다른 호출은 위임)."""

    inner: JsonStore
    poisoned: str

    def upsert_review_data(self, record: ReviewData) -> bool:
        if str(record.source_url).endswith(f"/{self.poisoned}"):
            raise StoreError("저장된 행이 손상됨")
        return self.inner.upsert_review_data(record)

    def update_structure_state(self, record: SourceData) -> None:
        self.inner.update_structure_state(record)

    def list_unstructured(
        self, limit: int, *, source_key: str | None = None
    ) -> tuple[SourceData, ...]:
        return self.inner.list_unstructured(limit, source_key=source_key)


def test_the_store_is_always_asked_for_everything() -> None:
    """`limit`은 **판정 수**를 세는 비용 상한이라 조회 자체는 항상 전량이다.

    조회를 `limit`으로 자르면 목록 앞머리의 대기 공고가 상한을 먹어 뒤에 도달하지 못한다.
    """
    asked: list[tuple[int, str | None]] = []

    class _RecordingStore:
        def list_unstructured(
            self, limit: int, *, source_key: str | None = None
        ) -> tuple[SourceData, ...]:
            asked.append((limit, source_key))
            return ()

    recording = _RecordingStore()
    structure_pending(recording, _FakeExtractor(), StructureOptions(limit=None))  # type: ignore[arg-type]
    structure_pending(recording, _FakeExtractor(), StructureOptions(limit=7, source_key="YTUS"))  # type: ignore[arg-type]

    assert asked[0][0] > 3_188, "수집한 3,188건이 한 번에 들어가야 한다"
    assert asked[1][0] == asked[0][0], "limit이 조회를 자르지 않는다"
    assert asked[1][1] == "YTUS"


def test_a_failure_to_record_the_failure_keeps_the_original_cause(store: JsonStore) -> None:
    """⚠️ 저장이 또 실패해도 **첫 번째 원인을 덮지 않는다** — 운영자가 봐야 하는 건 그쪽이다."""
    record = _source_data()
    store.save_source_data(record)
    broken = _BrokenStateStore(store)

    result = structure_one(record, broken, _FakeExtractor(GeminiError("안전 차단")), dry_run=False)  # type: ignore[arg-type]

    assert result.verdict is Verdict.FAILED
    assert result.error is not None
    assert "안전 차단" in result.error, "원인이 저장 오류에 덮이면 진단할 수 없다"
    assert "실패 기록도 실패" in result.error


def test_a_verdict_that_cannot_be_stored_does_not_abort_the_batch(store: JsonStore) -> None:
    """판정 기록(`update_structure_state`)이 깨져도 그 공고 하나로 막힌다."""
    store.save_source_data(_source_data("1"))
    store.save_source_data(_source_data("2", fetched_at=datetime(2026, 8, 10, 9, 1, tzinfo=KST)))

    report = structure_pending(
        _BrokenStateStore(store),  # type: ignore[arg-type]
        _FakeExtractor(_extraction(IsChurchRecruitment.NO)),
        StructureOptions(limit=10),
    )

    assert report.failed == 2, "두 건 모두 실패로 세되 배치는 끝까지 돈다"
    assert report.scanned == 2


@dataclass
class _BrokenStateStore:
    """판정 기록만 실패하는 대역(초안 저장·목록은 위임)."""

    inner: JsonStore

    def upsert_review_data(self, record: ReviewData) -> bool:
        return self.inner.upsert_review_data(record)

    def update_structure_state(self, _record: SourceData) -> None:
        raise StoreError("디스크 상태 불일치")

    def list_unstructured(
        self, limit: int, *, source_key: str | None = None
    ) -> tuple[SourceData, ...]:
        return self.inner.list_unstructured(limit, source_key=source_key)


def test_limit_counts_paid_judgements_not_rows_scanned(store: JsonStore) -> None:
    """⚠️ 이미지 대기 공고가 상한을 먹으면 뒤의 공고에 영원히 도달하지 못한다.

    미판정 목록이 수집 시각 순이라 대기 공고는 **매 실행 맨 앞**에 온다 — 훑은 수로 세면
    `--limit 20`이 몇 번 만에 굶고 나머지 수천 건이 영영 처리되지 않는다(실측 이미지 237건).
    """
    for index in range(5):  # 앞머리를 전부 이미지 공고로 막는다
        store.save_source_data(
            _source_data(
                f"img{index}",
                image_urls=("https://e.kr/p.png",),
                fetched_at=datetime(2026, 8, 10, 9, index, tzinfo=KST),
            )
        )
    store.save_source_data(
        _source_data("real", fetched_at=datetime(2026, 8, 10, 10, 0, tzinfo=KST))
    )

    report = structure_pending(store, _FakeExtractor(), StructureOptions(limit=1))

    assert report.drafted == 1, "대기 공고를 지나 실제 공고에 도달해야 한다"
    assert report.deferred == 5


def test_limit_stops_as_soon_as_it_is_reached(store: JsonStore) -> None:
    """비용 상한이므로 넘겨서 부르면 안 된다."""
    for index in range(4):
        store.save_source_data(
            _source_data(str(index), fetched_at=datetime(2026, 8, 10, 9, index, tzinfo=KST))
        )
    extractor = _FakeExtractor()

    report = structure_pending(store, extractor, StructureOptions(limit=2))

    assert report.drafted == 2
    assert len(extractor.calls) == 2, "상한을 넘겨 호출하면 그만큼 돈이 나간다"


# ── 그림 (2-c) ───────────────────────────────────────────────────


@dataclass
class _FakeImages:
    """정해둔 결과를 돌려주는 그림 소스. 게시판에 요청하지 않는다."""

    result: MediaSet = field(default_factory=MediaSet)
    asked: list[str] = field(default_factory=list)

    def media_for(self, record: SourceData) -> MediaSet:
        self.asked.append(record.external_id)
        return self.result


def test_a_poster_posting_is_judged_once_images_can_be_fetched(store: JsonStore) -> None:
    """⚠️ 그림 소스가 붙으면 `DEFERRED`가 저절로 사라져야 한다 — 따로 켜는 스위치가 없다."""
    record = _source_data(raw_text="", image_urls=("https://e.kr/poster.png",))
    store.save_source_data(record)
    extractor = _FakeExtractor()
    images = _FakeImages(MediaSet(items=(Media(media_type="image/png", data=b"x" * 5_000),)))

    result = structure_one(record, store, extractor, images=images)

    assert result.verdict is Verdict.DRAFTED
    assert extractor.image_counts == [1], "그림이 모델에 실제로 넘어가야 한다"
    assert images.asked == [record.external_id]


def test_a_text_posting_never_asks_for_images(store: JsonStore) -> None:
    """그림이 없는 공고에 요청을 보내면 게시판을 헛되이 두드린다."""
    record = _source_data()
    store.save_source_data(record)
    images = _FakeImages()

    structure_one(record, store, _FakeExtractor(), images=images)

    assert images.asked == []


def test_a_posting_still_gets_judged_when_its_images_cannot_be_read(store: JsonStore) -> None:
    """⚠️ 그림 실패로 공고를 통째 실패시키면 텍스트만으로 충분한 것까지 재시도에 걸린다."""
    record = _source_data(image_urls=("https://e.kr/p.png",))
    store.save_source_data(record)
    extractor = _FakeExtractor()
    images = _FakeImages(MediaSet(failures=("p.png: HTTP 404",)))

    result = structure_one(record, store, extractor, images=images)

    assert result.verdict is Verdict.DRAFTED
    assert extractor.image_counts == [0]
    assert result.media_note is not None and "못 읽음" in result.media_note


def test_a_ministry_posting_with_no_recognised_position_becomes_etc() -> None:
    """⚠️ 이게 없으면 `교역자 청빙` 같은 공고가 **3번 과금된 뒤 조용히 사라진다**.

    모델이 허용값 밖 문자열(`교역자`)을 주면 파서가 버려 빈 배열이 되고, 레코드 불변식에
    걸려 매번 실패하다 재시도 상한을 넘긴다. 값이 없는 것은 사실이므로 "그 밖"으로 둔다.
    """
    draft = build_draft(
        _source_data(),
        Extraction(
            is_church_recruitment=IsChurchRecruitment.YES,
            job_kind=(JobKind.MINISTRY,),
            position=(),
        ),
    )

    assert draft.position == (Position.ETC,)


def test_a_general_posting_keeps_its_empty_position() -> None:
    draft = build_draft(
        _source_data(),
        Extraction(
            is_church_recruitment=IsChurchRecruitment.YES,
            job_kind=(JobKind.GENERAL,),
            role="시설관리",
        ),
    )

    assert draft.position == ()


def test_every_extracted_value_reaches_the_draft() -> None:
    """⚠️ `build_draft`는 34칸을 손으로 옮긴다 — 한 줄을 빼먹어도 mypy는 통과하고 값만 사라진다.

    이름이 1:1이므로 전 칸을 순회해 확인한다(serde 왕복 테스트와 같은 해법).
    """
    filled = Extraction(
        is_church_recruitment=IsChurchRecruitment.YES,
        job_kind=(JobKind.MINISTRY, JobKind.GENERAL),
        role="음향",
        position=(Position.EVANGELIST,),
        department=Department.YOUTH,
        employment_type=EmploymentType.FULL_TIME,
        qualification=Qualification.ORDAINED,
        headcount="1명",
        start_timing="즉시",
        housing_provided=True,
        housing_note="사택",
        pay_min=250,
        pay_max=300,
        pay_note="내규",
        pay_period=StipendPeriod.MONTH,
        benefit_note="4대보험",
        work_days="주 5일",
        requirements=("자격",),
        preferred=("우대",),
        required_docs=("이력서",),
        optional_docs=("추천서",),
        process_steps=("서류",),
        description="요약",
        deadline=date(2026, 8, 31),
        church_name="점촌제일교회",
        region=Region.GYEONGBUK,
        city="문경시",
        raw_denomination="예장통합",
        contact_email="a@b.kr",
        contact_tel="054-000-0000",
        contact_link="https://e.kr/apply",
        contact_post="경북 문경시",
    )

    draft = build_draft(_source_data(), filled)

    for info in fields(Extraction):
        if info.name == "evidence":
            continue  # 근거는 검산용이고 저장되지 않는다(`pipeline/verify.py`)
        assert getattr(draft, info.name) == getattr(filled, info.name), f"{info.name}이 안 옮겨졌다"
    assert draft.posted_at == _source_data().posted_on, "게시일은 수집이 파싱한 값을 쓴다"


@pytest.mark.parametrize(
    ("kinds", "position", "role"),
    [
        ((JobKind.MINISTRY,), (), None),
        ((JobKind.MINISTRY,), (), "음향"),
        ((JobKind.GENERAL,), (Position.EVANGELIST,), None),
        ((JobKind.GENERAL,), (), None),
        ((JobKind.MINISTRY, JobKind.GENERAL), (Position.EVANGELIST,), None),
        ((JobKind.MINISTRY, JobKind.GENERAL), (), "음향"),
    ],
    ids=[
        "사역직인데 직분 없음",
        "사역직인데 직무가 붙음",
        "일반직인데 직분이 붙음",
        "일반직인데 직무 없음",
        "혼합인데 직무 없음",
        "혼합인데 직분 없음",
    ],
)
def test_every_contradictory_classification_is_reconciled(
    store: JsonStore,
    kinds: tuple[JobKind, ...],
    position: tuple[Position, ...],
    role: str | None,
) -> None:
    """⚠️ 규칙이 양방향이라 어긋나는 조합이 넷이다 — 하나라도 실패로 두면 그 공고가
    **3번 과금된 뒤 재시도 상한을 넘겨 조용히 사라진다**.

    스키마로는 "GENERAL일 때만 role"을 표현할 수 없어 모델 답이 어긋나는 것은 정상 범위다.
    """
    record = _source_data()
    store.save_source_data(record)
    answer = Extraction(
        is_church_recruitment=IsChurchRecruitment.YES,
        job_kind=kinds,
        position=position,
        role=role,
    )

    result = structure_one(record, store, _FakeExtractor(answer))

    assert result.verdict is Verdict.DRAFTED, "맞춰서 저장해야 한다 — 실패시키면 공고를 잃는다"
    draft = result.draft
    assert draft is not None
    assert (JobKind.MINISTRY in draft.job_kind) == bool(draft.position)
    assert (JobKind.GENERAL in draft.job_kind) == (draft.role is not None)


def test_a_reversed_pay_range_is_corrected_not_failed() -> None:
    """⚠️ 뒤집힌 답을 실패로 두면 그 공고가 3번 과금된 뒤 사라진다 — 범위는 바로잡으면 된다."""
    draft = build_draft(
        _source_data(),
        Extraction(
            is_church_recruitment=IsChurchRecruitment.YES,
            job_kind=(JobKind.MINISTRY,),
            position=(Position.EVANGELIST,),
            pay_min=300,
            pay_max=250,
        ),
    )

    assert (draft.pay_min, draft.pay_max) == (250, 300)


def test_a_failed_posting_still_reports_its_unread_images(store: JsonStore) -> None:
    """⚠️ 포스터를 못 읽었는데 호출까지 실패하면, 사유가 없으면 원인을 영영 모른다."""
    record = _source_data(image_urls=("https://e.kr/p.png",))
    store.save_source_data(record)
    images = _FakeImages(MediaSet(failures=("p.png: HTTP 404",)))

    result = structure_one(record, store, _FakeExtractor(GeminiError("429")), images=images)

    assert result.verdict is Verdict.FAILED
    assert result.media_note is not None and "못 읽음" in result.media_note


def test_the_report_counts_and_samples_unread_images(store: JsonStore) -> None:
    """⚠️ 아무도 안 읽는 값은 없는 값이다 — 집계에 올라와야 CLI가 경고할 수 있다."""
    store.save_source_data(_source_data("1", image_urls=("https://e.kr/p.png",)))
    images = _FakeImages(MediaSet(failures=("p.png: HTTP 404",)))

    report = structure_pending(store, _FakeExtractor(), StructureOptions(limit=5), images=images)

    assert report.text_only == 1
    assert report.media_failures[0].posting == "DAESHIN/1"


def test_an_excluded_posting_keeps_its_image_reason(store: JsonStore) -> None:
    """⚠️ **그림을 못 읽어 게이트1 NO가 난 경우**가 가장 알아야 할 상황이다.

    그런데 그때 판정이 기록돼 되돌릴 수 없다.
    """
    record = _source_data(image_urls=("https://e.kr/p.png",))
    store.save_source_data(record)
    images = _FakeImages(MediaSet(failures=("p.png: HTTP 404",)))

    result = structure_one(
        record, store, _FakeExtractor(_extraction(IsChurchRecruitment.NO)), images=images
    )

    assert result.verdict is Verdict.EXCLUDED
    assert result.media_note is not None


def test_a_local_file_url_is_never_requested(store: JsonStore) -> None:
    """⚠️ 본문에 `file:///C:\\...`가 섞인 공고가 있다(실측 8건 — HWP에서 붙여넣은 흔적).

    요청하면 전송 층이 죽고 **그 실행 내내 그 게시판의 `Crawl-delay`를 못 읽는다**.
    """
    record = _source_data(image_urls=(r"file:///C:\\Users\\church\\poster.jpg",))
    store.save_source_data(record)
    client = _RecordingClient()

    def open_client(_key: str) -> _RecordingClient:
        return client

    BoardMediaSource(open_client=open_client).media_for(record)  # type: ignore[arg-type]

    assert client.got == []


@dataclass
class _RecordingClient:
    got: list[str] = field(default_factory=list)

    def get_bytes(self, url: str) -> object:
        self.got.append(url)
        raise AssertionError("요청하면 안 된다")

    def get(self, url: str) -> object:
        return url

    def close(self) -> None:
        return None


def test_a_closed_posting_is_rejected_on_creation(store: JsonStore, data_dir: Path) -> None:
    """⚠️ 그대로 두면 `jobs.status` 기본값이 `OPEN`이라 **이미 채워진 자리가 공개된다**.

    이단과 같은 방식으로 만들면서 거절한다 — 레코드와 근거는 남는다(SPEC §5.4).

    ⚠️ 마감은 **모델이 아니라 게시판 표시**가 정한다 — 아래 제목의 `청빙완료`가 근거다.
    """
    record = replace(_source_data(), title="성원교회 부교역자 청빙 (청빙완료)")
    store.save_source_data(record)
    extraction = Extraction(
        is_church_recruitment=IsChurchRecruitment.YES,
        job_kind=(JobKind.MINISTRY,),
        position=(Position.EVANGELIST,),
        description="부교역자를 청빙합니다.",
    )

    result = structure_one(record, store, _FakeExtractor(extraction))

    assert result.verdict is Verdict.DRAFTED, "레코드는 남긴다 — 없애는 게 아니다"
    draft = _drafts(data_dir)[0]
    assert draft.review_status is ReviewStatus.REJECTED
    assert draft.reject_reason is RejectReason.CLOSED


def test_a_body_that_merely_mentions_completion_is_not_closed(
    store: JsonStore, data_dir: Path
) -> None:
    """⚠️ 실측 370건이 본문에 `채용 완료 후 폐기합니다`류를 담는다 — 대부분 진행 중이다.

    게시판 표시만 보므로 본문에 `완료`가 있어도 거절되지 않는다.
    """
    record = replace(
        _source_data(), title="성원교회 부교역자 청빙", raw_text="서류는 채용 완료 후 폐기합니다."
    )
    store.save_source_data(record)
    extraction = Extraction(
        is_church_recruitment=IsChurchRecruitment.YES,
        job_kind=(JobKind.MINISTRY,),
        position=(Position.EVANGELIST,),
        description="부교역자를 청빙합니다.",
    )

    structure_one(record, store, _FakeExtractor(extraction))

    assert _drafts(data_dir)[0].review_status is ReviewStatus.PENDING


def test_an_open_posting_stays_pending() -> None:
    draft = build_draft(_source_data(), _extraction())

    assert draft.review_status is ReviewStatus.PENDING
    assert draft.reject_reason is None


# ── 게시판 간 병렬 ───────────────────────────────────────────────


@dataclass
class _BarrierExtractor:
    """두 게시판이 **동시에** 모델을 부를 때만 통과한다 — 순차면 여기서 시간이 다 간다."""

    barrier: threading.Barrier
    seen: list[str] = field(default_factory=list)

    def extract(self, record: SourceData, images: Sequence[Media] = ()) -> Extraction:
        self.barrier.wait(timeout=5)
        self.seen.append(f"{record.source_key}/{len(images)}")
        return _extraction()


@dataclass
class _ConcurrencyProbe:
    """게시판마다 **동시에 몇 건이 모델에 가 있었나**를 잰다."""

    peak: dict[str, int] = field(default_factory=dict)
    inside: Counter[str] = field(default_factory=Counter)
    calls: list[str] = field(default_factory=list)
    image_counts: list[int] = field(default_factory=list)
    delay: float = 0.02
    lock: threading.Lock = field(default_factory=threading.Lock)

    def extract(self, record: SourceData, images: Sequence[Media] = ()) -> Extraction:
        key = record.source_key
        with self.lock:
            self.calls.append(record.external_id)
            self.image_counts.append(len(images))
            self.inside[key] += 1
            self.peak[key] = max(self.peak.get(key, 0), self.inside[key])
        time.sleep(self.delay)
        with self.lock:
            self.inside[key] -= 1
        return _extraction()


def _fill(store: JsonStore, boards: Mapping[str, int], *, raw_text: str | None = None) -> None:
    for key, count in boards.items():
        for number in range(count):
            record = _source_data(f"{key}-{number}", source_key=key)
            store.save_source_data(
                record if raw_text is None else replace(record, raw_text=raw_text)
            )


def test_two_boards_reach_the_model_at_the_same_time(store: JsonStore) -> None:
    """병렬의 유일한 증거. 순차로 돌면 두 번째가 영영 오지 않아 장벽이 깨진다."""
    _fill(store, {"DAESHIN": 1, "YTUS": 1})
    extractor = _BarrierExtractor(threading.Barrier(2))

    report = structure_pending(store, extractor, StructureOptions(limit=10), workers=2)

    assert report.drafted == 2
    assert sorted(extractor.seen) == ["DAESHIN/0", "YTUS/0"]


def test_one_board_never_has_two_postings_in_flight(store: JsonStore) -> None:
    """⚠️ 게시판 안이 순차라는 것이 fetch 층에 잠금을 두지 않는 근거다(SPEC §3 한 호스트 1요청).

    워커를 넉넉히 줘도 한 게시판은 한 번에 한 건이어야 한다 — 아니면 같은 호스트로 그림
    요청이 두 개 나가고, 요청 간격·세션 쿠키를 든 클라이언트를 두 스레드가 함께 만진다.
    """
    _fill(store, {"DAESHIN": 4, "YTUS": 4})
    probe = _ConcurrencyProbe()

    structure_pending(store, probe, StructureOptions(limit=20), workers=8)

    assert probe.peak == {"DAESHIN": 1, "YTUS": 1}


def test_a_bigger_board_starts_first() -> None:
    """⚠️ 전체 시간은 가장 큰 게시판이 정한다 — 늦게 출발하면 그게 혼자 남는다."""
    small = _source_data("1", source_key="MOKWON")
    big = tuple(_source_data(f"b{n}", source_key="CSU") for n in range(3))
    middle = tuple(_source_data(f"m{n}", source_key="YTUS") for n in range(2))

    boards = structure.group_by_source((small, *middle, *big))

    assert [len(board) for board in boards] == [3, 2, 1]
    assert [board[0].source_key for board in boards] == ["CSU", "YTUS", "MOKWON"]


def test_a_board_keeps_the_oldest_first_inside(store: JsonStore) -> None:
    """게시판을 나눠도 그 안의 순서는 수집 시각 그대로다 — 오래된 공고가 뒤로 밀리지 않는다."""
    for number in (2, 0, 1):
        store.save_source_data(
            _source_data(
                f"D{number}",
                source_key="DAESHIN",
                fetched_at=_NOW + timedelta(minutes=number),
            )
        )
    probe = _ConcurrencyProbe(delay=0)

    structure_pending(store, probe, StructureOptions(limit=10), workers=4)

    assert probe.calls == ["D0", "D1", "D2"]


# ── 유료 상한 ────────────────────────────────────────────────────


def test_the_paid_limit_holds_when_boards_run_together(store: JsonStore) -> None:
    """⚠️ 상한은 **부르기 전에** 잡는다 — 부르고 나서 세면 게시판 수만큼 넘겨 청구된다."""
    _fill(store, {"CSU": 4, "PUTS": 4, "YTUS": 4, "BPU": 4, "HTUS": 4, "SJS": 4})
    probe = _ConcurrencyProbe()

    report = structure_pending(store, probe, StructureOptions(limit=5), workers=6)

    assert len(probe.calls) == 5, "게시판이 각자 한 건씩 더 보냈다"
    assert report.drafted == 5


def test_a_posting_the_model_never_saw_does_not_spend_the_limit(store: JsonStore) -> None:
    """빈 공고는 자리를 돌려준다 — 안 그러면 목록 앞머리가 상한을 먹고 뒤에 도달하지 못한다."""
    for number in range(3):
        store.save_source_data(
            replace(_source_data(f"empty{number}"), raw_text="", fetched_at=_NOW)
        )
    for number in range(2):
        store.save_source_data(
            _source_data(f"real{number}", fetched_at=_NOW + timedelta(minutes=1 + number))
        )
    extractor = _FakeExtractor()

    report = structure_pending(store, extractor, StructureOptions(limit=2), workers=4)

    assert report.empty == 3
    assert len(extractor.calls) == 2


def test_only_the_verdicts_that_called_the_model_spend_the_limit() -> None:
    """⚠️ `_FREE_VERDICTS`와 `_Tally.judged`가 갈라지면 상한이 조용히 틀어진다.

    한쪽만 고치면 빈 공고가 유료 자리를 먹거나(굶음) 호출한 건이 안 세어진다(초과 청구).
    """
    for verdict in Verdict:
        tally = structure._Tally()
        tally.add(StructureResult(record=_source_data(), verdict=verdict, error="사유"))
        spends_money = tally.judged == 1
        assert spends_money is (verdict not in structure._FREE_VERDICTS), verdict


def test_workers_must_be_at_least_one(store: JsonStore) -> None:
    with pytest.raises(ValueError, match="workers"):
        structure_pending(store, _FakeExtractor(), StructureOptions(limit=1), workers=0)


# ── 집계·표시 ────────────────────────────────────────────────────


def test_the_tally_loses_nothing_when_boards_finish_together(store: JsonStore) -> None:
    """집계는 락 안에서 한 스레드씩 — 아니면 `scanned`가 실행마다 다른 값이 된다."""
    _fill(store, dict.fromkeys(("CSU", "PUTS", "YTUS", "BPU", "HTUS", "SJS"), 5))

    report = structure_pending(
        store, _ConcurrencyProbe(delay=0), StructureOptions(limit=None), workers=6
    )

    assert report.scanned == 30
    assert report.drafted == 30


def test_the_progress_sink_never_runs_twice_at_once(store: JsonStore) -> None:
    """받는 쪽(화면·`--out` 파일)은 병렬을 몰라도 되게 한다 — 동시 호출을 여기서 막는다."""
    _fill(store, {"CSU": 4, "PUTS": 4, "YTUS": 4})
    inside = 0
    peak = 0
    guard = threading.Lock()

    def sink(_result: StructureResult, _progress: StructureReport) -> None:
        nonlocal inside, peak
        with guard:
            inside += 1
            peak = max(peak, inside)
        time.sleep(0.005)
        with guard:
            inside -= 1

    structure_pending(
        store,
        _ConcurrencyProbe(delay=0),
        StructureOptions(limit=None),
        on_result=sink,
        workers=3,
    )

    assert peak == 1


# ── 저장이 통째로 깨졌을 때 ──────────────────────────────────────


@dataclass
class _BrokenStore:
    """저장만 실패하는 스토어 대역 — 조회는 정상이다(원장 파일 하나가 깨진 모양)."""

    inner: JsonStore

    def list_unstructured(
        self, limit: int, *, source_key: str | None = None
    ) -> Sequence[SourceData]:
        return self.inner.list_unstructured(limit, source_key=source_key)

    def upsert_review_data(self, _record: ReviewData) -> bool:
        raise StoreError("review_data.json: JSON 파싱 실패")

    def update_structure_state(self, _record: SourceData) -> None:
        raise StoreError("source_data.json: JSON 파싱 실패")


def test_a_broken_ledger_stops_the_run_instead_of_burning_the_budget(store: JsonStore) -> None:
    """⚠️ 글 단위 격리가 여기서는 독이 된다.

    원장이 통째로 깨지면 공고마다 **Gemini를 부른 뒤** 저장이 실패한다 — 그대로 두면
    `--all`이 3,000번 과금하고 아무것도 저장하지 못한다(ROADMAP 1-2).
    """
    _fill(store, {"DAESHIN": 20})
    extractor = _FakeExtractor()

    report = structure_pending(
        _BrokenStore(store),  # type: ignore[arg-type]
        extractor,
        StructureOptions(limit=None),
        workers=1,
    )

    assert report.halted is not None
    assert len(extractor.calls) == STORE_FAILURE_LIMIT, "상한을 넘겨 부르지 않는다"
    assert report.scanned == STORE_FAILURE_LIMIT
    # ⚠️ 멈춘 실행은 **실패로도 세어져야** 한다 — CLI 종료 코드가 이 값만 본다.
    assert report.failed == STORE_FAILURE_LIMIT


def test_scattered_broken_rows_do_not_stop_the_run(store: JsonStore) -> None:
    """⚠️ 반대쪽도 지킨다 — 행이 깨졌다고 멈추면 뒤의 수천 건에 도달하지 못한다.

    상한을 넘는 수(여기서는 10건)가 깨져도 **연속이 아니면** 끝까지 돈다. 성공 한 번이
    누적을 지운다 — 안 그러면 흩어진 손상 행 몇 개로 정상 실행이 멎는다.
    """
    _fill(store, {"DAESHIN": 20})
    every_other = {
        record.external_id
        for position, record in enumerate(store.list_unstructured(20))
        if position % 2 == 1
    }
    assert len(every_other) > STORE_FAILURE_LIMIT, "상한보다 많이 깨뜨려야 의미가 있다"

    report = structure_pending(
        _SometimesBrokenStore(store, every_other),  # type: ignore[arg-type]
        _FakeExtractor(),
        StructureOptions(limit=None),
        workers=1,
    )

    assert report.halted is None
    assert report.scanned == 20
    assert report.failed == len(every_other)


@dataclass
class _SometimesBrokenStore:
    """정해둔 공고에서만 저장이 실패하는 대역."""

    inner: JsonStore
    failing: set[str]

    def list_unstructured(
        self, limit: int, *, source_key: str | None = None
    ) -> Sequence[SourceData]:
        return self.inner.list_unstructured(limit, source_key=source_key)

    def upsert_review_data(self, record: ReviewData) -> bool:
        if str(record.source_data_id) in self._broken_ids():
            raise StoreError("review_data.json: 이 행만 깨졌다")
        return self.inner.upsert_review_data(record)

    def update_structure_state(self, record: SourceData) -> None:
        self.inner.update_structure_state(record)

    def _broken_ids(self) -> set[str]:
        return {
            str(record.id)
            for record in self.inner.list_unstructured(1000)
            if record.external_id in self.failing
        }


def test_a_broken_ledger_halts_even_when_the_model_also_fails(store: JsonStore) -> None:
    """⚠️ 모델 실패와 저장 실패가 겹치면 멈춤이 영원히 안 걸리던 구멍(2026-08-15 검수).

    프롬프트가 깨져 전건이 `ExtractionError`인데 원장까지 손상되면, 실패를 기록하는 저장도
    실패한다 — 그 사실을 흘리면 3,188번 과금하고 아무것도 저장하지 못한다. `structure_attempts`
    도 저장이라 시도 상한조차 안 올라가 다음 실행이 그대로 반복한다.
    """
    _fill(store, {"DAESHIN": 20})
    extractor = _FakeExtractor(result=ExtractionError("빈 응답"))

    report = structure_pending(
        _BrokenStore(store),  # type: ignore[arg-type]
        extractor,
        StructureOptions(limit=None),
        workers=1,
    )

    assert report.halted is not None
    assert len(extractor.calls) == STORE_FAILURE_LIMIT


def test_the_overshoot_is_bounded_by_the_worker_count(store: JsonStore) -> None:
    """⚠️ 게시판이 여럿이면 멈춤을 알아채기 전에 각자 한 건씩 더 부른다.

    상한은 `LIMIT + workers - 1`이다(실측 workers=8 → 12건). 이 검사가 없으면 `halted` 확인을
    `budget.take()` 뒤로 옮기는 것 같은 변경이 조용히 이 경계를 넓힌다.
    """
    workers = 8
    _fill(store, dict.fromkeys(("CSU", "PUTS", "YTUS", "BPU", "HTUS", "SJS", "KTS", "PCK"), 10))
    extractor = _FakeExtractor()

    report = structure_pending(
        _BrokenStore(store),  # type: ignore[arg-type]
        extractor,
        StructureOptions(limit=None),
        workers=workers,
    )

    assert report.halted is not None
    assert len(extractor.calls) <= STORE_FAILURE_LIMIT + workers - 1
