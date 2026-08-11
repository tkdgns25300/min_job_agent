"""구조화 패스 테스트 — 판정·저장 순서·실패 처리.

AI는 가짜(`_FakeExtractor`)로 바꾼다. **네트워크도 유료 호출도 없다**(가드레일 #7·#10) —
`Extractor` 프로토콜이 있는 이유가 이것이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.clock import KST
from minjob_ingest.domain import Confidence, DenominationSource, IsChurchRecruitment, ReviewStatus
from minjob_ingest.lib.gemini import GeminiError
from minjob_ingest.models import (
    MAX_DESCRIPTION_CHARS,
    MAX_STRUCTURE_ATTEMPTS,
    Attachment,
    ReviewData,
    SourceData,
    new_id,
)
from minjob_ingest.pipeline.extraction import Extraction, ExtractionError
from minjob_ingest.pipeline.structure import (
    StructureOptions,
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
        run_id=_RUN_ID,
        fetched_at=fetched_at,
        raw_text=raw_text,
        image_urls=image_urls,
        attachments=attachments,
    )


def _extraction(gate1: IsChurchRecruitment = IsChurchRecruitment.YES) -> Extraction:
    return Extraction(
        is_church_recruitment=gate1,
        church_name="점촌제일교회",
        title="전임 사역자 청빙",
        description="전임 사역자를 청빙합니다.",
    )


@dataclass
class _FakeExtractor:
    """정해둔 결과를 돌려주고 호출 횟수를 센다. 예외를 주면 그걸 던진다."""

    result: Extraction | Exception = field(default_factory=_extraction)
    calls: list[str] = field(default_factory=list)

    def extract(self, record: SourceData) -> Extraction:
        self.calls.append(record.external_id)
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


def test_the_draft_inherits_the_collect_run() -> None:
    """`run_id`는 수집 실행을 승계한다 — 구조화는 자기 `crawl_run`을 만들지 않는다(SPEC §2)."""
    record = _source_data()

    draft = build_draft(record, _extraction())

    assert draft.run_id == _RUN_ID
    assert draft.source_url == record.source_url


def test_the_first_pass_cannot_claim_a_denomination_or_confidence() -> None:
    """1단계는 4필드뿐이라 승격 근거가 없다 — `LOW` + 교단 `unknown`이어야 한다."""
    draft = build_draft(_source_data(), _extraction())

    assert draft.confidence is Confidence.LOW
    assert draft.denomination_source is DenominationSource.UNKNOWN
    assert draft.denomination is None


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

    def extract(self, record: SourceData) -> Extraction:
        self.calls.append(record.external_id)
        if record.external_id in self.failing:
            raise GeminiError("이 글만 실패")
        return _extraction()


def test_dry_run_still_assembles_the_draft(store: JsonStore) -> None:
    """⚠️ 미리보기가 조립을 건너뛰면 **리허설은 통과하고 본 실행만 터진다**.

    저장할 수 없는 값(요약 상한 초과)을 미리보기에서도 실패로 봐야 한다 — 아니면 사장님이
    "이대로 저장해도 된다"고 판단한 뒤 본 실행에서 같은 공고가 실패한다.
    """
    record = _source_data()
    store.save_source_data(record)
    too_long = Extraction(
        is_church_recruitment=IsChurchRecruitment.YES,
        description="가" * (MAX_DESCRIPTION_CHARS + 1),
    )

    result = structure_one(record, store, _FakeExtractor(too_long), dry_run=True)

    assert result.verdict is Verdict.FAILED
    assert len(store.list_unstructured(10)) == 1, "미리보기는 시도 횟수도 남기지 않는다"


def test_a_draft_that_cannot_be_stored_is_a_failure(store: JsonStore) -> None:
    """레코드 불변식과 어긋난 모델 값은 크래시가 아니라 그 공고 하나의 실패다."""
    record = _source_data()
    store.save_source_data(record)
    too_long = Extraction(
        is_church_recruitment=IsChurchRecruitment.YES,
        description="가" * (MAX_DESCRIPTION_CHARS + 1),
    )

    result = structure_one(record, store, _FakeExtractor(too_long))

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

    result = structure_one(record, store, extractor)

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
