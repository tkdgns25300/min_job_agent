"""로컬 JSON 파일 저장소 — Phase 1용 `Store` 구현.

레코드 모양이 SPEC §6과 같으므로(serde) Supabase 전환은 이 파일만 갈아끼우면 된다.

⚠️ **쓰기는 한 줄로 세운다.** 파일 단위 read-modify-write라 동시에 쓰면 나중 것이 앞의 것을
덮어 **레코드가 조용히 사라진다** — 잃는 것이 `structured_at`이면 Gemini 재과금이고 초안이면
탐지 불가능한 유실이다. 소스 간 병렬(SPEC §3)에서 공유되는 자원은 이 파일들뿐이므로,
**갱신 메서드를 락 하나로 감싸는 것으로 충분**하다. 읽기는 잠그지 않는다 — `_write_rows`가
임시파일 → rename이라 읽는 쪽은 항상 교체 전이나 후의 온전한 파일을 본다.
한 건에 0.14초라 3,188건을 전부 직렬화해도 병렬 이득을 먹지 않는다(2026-08-14 실측).

⚠️ **로컬 실행 전용.** GitHub Actions 러너는 끝나면 사라져 원장이 매 실행 초기화된다 →
31곳 전량 재크롤 + 전량 재구조화(비용) + 산출물 유실. Actions 배포는
Supabase 전환(ROADMAP 1-6) 이후에만 한다(CLAUDE.md 순서 제약).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Final

from minjob_ingest.clock import kst_now
from minjob_ingest.domain import CrawlMode, normalize_source_key
from minjob_ingest.models import CrawlRun, ReviewData, SourceData, SourceHealth
from minjob_ingest.store.base import LedgerEntry, RequeueResult, StoreError
from minjob_ingest.store.serde import (
    Row,
    SerdeError,
    ledger_entry_of_row,
    ledger_key_of_row,
    row_to_review_data,
    row_to_source_data,
    row_to_source_health,
    to_row,
)

_LOG = logging.getLogger(__name__)

#: 파일 포맷 버전. 스키마가 바뀌면(백필 후 필드 추가 등) 이 값을 올리고 `data/`를 재작성한다 —
#: 버전을 확인하지 않으면 옛 파일이 "컬럼 누락"으로만 보여 원인을 찾기 어렵다(serde docstring).
#:
#: **2 (2026-08-14)** — `posted_on`·`posted_at` 필수화. ⚠️ 옛 파일을 그냥 두면 더 나쁘다:
#: 원장 조회는 `posted_on: null`을 읽어 "이미 본 글"이라 하고 구조화 조회는 손상 행으로 건너뛰어,
#: 그 공고가 **수집도 구조화도 안 되는 유령**이 된다. 버전으로 즉시 거부해 재작성을 강제한다.
FILE_VERSION: Final = 2

_SOURCE_DATA_FILE: Final = "source_data.json"
_REVIEW_DATA_FILE: Final = "review_data.json"
_SOURCE_HEALTH_FILE: Final = "source_health.json"
_CRAWL_RUN_FILE: Final = "crawl_run.json"

#: 되돌릴 때 처리 상태를 이 값으로 되돌린다. ⚠️ `_MUTABLE_STATE_FIELDS`와 **같은 집합**이어야
#: 한다 — 넷째 상태 칸이 생겼을 때 여기만 모르면 그 값이 남아 재구조화가 조용히 달라진다.
_REQUEUED_STATE: Final[Row] = {
    "structured_at": None,
    "structure_attempts": 0,
    "last_structure_error": None,
}

#: `update_structure_state`가 갱신할 수 있는 **유일한** 필드들(SPEC §6 ① 처리 상태).
#: 나머지 전부가 자동으로 write-once가 된다 — 증거 필드를 나열하는 방식이면 `SourceData`에
#: 필드가 추가될 때마다 보호에서 빠져(예: `content_hash`) 갱신 경로로 새는 걸 못 막는다.
_MUTABLE_STATE_FIELDS: Final = ("structured_at", "structure_attempts", "last_structure_error")

if set(_REQUEUED_STATE) != set(_MUTABLE_STATE_FIELDS):  # pragma: no cover - 임포트 시 계약 검사
    raise RuntimeError(f"되돌리기 상태가 갱신 허용 칸과 다르다: {set(_MUTABLE_STATE_FIELDS)}")

#: 손상 행 보고 훅 — (파일명, 예외). 기본은 경고 로그.
type CorruptRowHandler = Callable[[str, SerdeError], None]


def _log_corrupt_row(source: str, error: SerdeError) -> None:
    _LOG.warning("손상된 행을 건너뜀 (%s): %s", source, error)


class JsonStore:
    """`Store` 프로토콜의 로컬 파일 구현."""

    def __init__(
        self, data_dir: Path, *, on_corrupt_row: CorruptRowHandler = _log_corrupt_row
    ) -> None:
        self._dir = data_dir
        self._on_corrupt_row = on_corrupt_row
        # 게시판 간 병렬 실행에서 갱신을 한 줄로 세운다(모듈 docstring). 재진입 가능한 락을
        # 쓰는 것은 갱신 메서드가 서로를 부를 여지를 남겨두기 위해서다.
        self._write_lock = threading.RLock()

    # ── 원장·수집 ────────────────────────────────────────────────

    def seen_postings(
        self, source_key: str, external_ids: Sequence[str]
    ) -> Mapping[str, LedgerEntry]:
        # 저장 시엔 모델이 정규화하므로 조회도 같은 정규화를 거쳐야 원장이 빗나가지 않는다.
        # (빗나가면 이미 수집한 글의 상세를 매 실행 다시 요청한다.)
        wanted = {external_id.strip(): external_id for external_id in external_ids}
        if not wanted:
            return {}
        key = normalize_source_key(source_key)
        seen: dict[str, LedgerEntry] = {}
        # 본문(`raw_text`)은 디코딩하지 않는다 — 원장 판정에 필요한 네 컬럼만 본다.
        for row in self._read_rows(_SOURCE_DATA_FILE):
            try:
                stored_key, stored_id = ledger_key_of_row(row)
            except SerdeError as err:
                self._on_corrupt_row(_SOURCE_DATA_FILE, err)
                continue
            if stored_key != key or stored_id not in wanted:
                continue
            try:
                entry = ledger_entry_of_row(row)
            except SerdeError as err:
                self._on_corrupt_row(_SOURCE_DATA_FILE, err)
                continue
            # 호출자가 넘긴 원본 문자열을 키로 돌려준다 — 호출자가 자기 목록을 이걸로 걸러낸다.
            seen[wanted[stored_id]] = entry
        return seen

    def save_source_data(self, record: SourceData) -> bool:
        with self._write_lock:
            rows = self._read_rows(_SOURCE_DATA_FILE)
            if any(self._ledger_key_or_none(row) == record.ledger_key for row in rows):
                return False  # ON CONFLICT DO NOTHING
            rows.append(to_row(record))
            self._write_rows(_SOURCE_DATA_FILE, rows)
            return True

    # ── 구조화 ──────────────────────────────────────────────────

    def list_unstructured(
        self, limit: int, *, source_key: str | None = None
    ) -> tuple[SourceData, ...]:
        if limit <= 0:
            raise ValueError(f"limit는 1 이상이어야 함 ({limit})")
        wanted = None if source_key is None else normalize_source_key(source_key)
        pending = [
            record
            for record in self._decode_rows(_SOURCE_DATA_FILE, row_to_source_data)
            if record.needs_restructure and (wanted is None or record.source_key == wanted)
        ]
        pending.sort(key=lambda record: record.fetched_at)
        return tuple(pending[:limit])

    def update_structure_state(self, record: SourceData) -> None:
        with self._write_lock:
            rows = self._read_rows(_SOURCE_DATA_FILE)
            index = self._index_of(rows, "id", str(record.id))
            if index is None:
                raise StoreError(f"source_data {record.id} 없음 — 상태를 갱신할 대상이 없다")
            stored = row_to_source_data(rows[index])
            self._check_only_state_changed(stored, record)
            self._check_state_moves_forward(stored, record)
            rows[index] = to_row(record)
            self._write_rows(_SOURCE_DATA_FILE, rows)

    def requeue_for_structure(self, *, source_key: str | None = None) -> RequeueResult:
        with self._write_lock:
            review_rows = self._read_rows(_REVIEW_DATA_FILE)
            protected = self._protected_ids(review_rows)
            wanted = None if source_key is None else normalize_source_key(source_key)
            source_rows = self._read_rows(_SOURCE_DATA_FILE)

            requeued: set[str] = set()
            skipped: list[str] = []
            # ⚠️ 행을 고치지 않고 **바꿔 끼운다**(`Row`는 읽기 전용 계약 · `update_structure_state`와
            #    같은 방식) — 제자리 수정은 읽는 쪽이 들고 있는 값을 몰래 바꾼다.
            rewritten: list[Row] = []
            for row in source_rows:
                record = row_to_source_data(row)
                if record.structured_at is None or (
                    wanted is not None and record.source_key != wanted
                ):
                    rewritten.append(row)
                    continue
                if str(record.id) in protected:
                    skipped.append(record.label)
                    rewritten.append(row)
                    continue
                requeued.add(str(record.id))
                rewritten.append({**row, **_REQUEUED_STATE})

            if not requeued:
                return RequeueResult(skipped=tuple(skipped))
            # ⚠️ 판정을 먼저 되돌린다(`Store.requeue_for_structure` 계약) — 반대로 하면
            #    "판정 완료 + 초안 없음"이 남고 그 상태는 사후 탐지가 불가능하다.
            self._write_rows(_SOURCE_DATA_FILE, rewritten)
            self._write_rows(
                _REVIEW_DATA_FILE,
                [row for row in review_rows if str(row.get("source_data_id")) not in requeued],
            )
            return RequeueResult(requeued=len(requeued), skipped=tuple(skipped))

    def _protected_ids(self, review_rows: list[Row]) -> set[str]:
        """되돌리면 안 되는 초안의 `source_data_id`.

        ⚠️ 읽을 수 없는 행이 하나라도 있으면 **멈춘다** — 승인된 행인지 알 수 없는데 지우면
        되돌릴 방법이 없다. 손상 행을 건너뛰는 다른 조회들과 여기가 다른 이유다.
        """
        protected: set[str] = set()
        for row in review_rows:
            try:
                draft = row_to_review_data(row)
            except SerdeError as err:
                raise StoreError(f"읽을 수 없는 초안이 있어 되돌리지 않았다: {err}") from err
            if not draft.is_safe_to_replace:
                protected.add(str(draft.source_data_id))
        return protected

    def upsert_review_data(self, record: ReviewData) -> bool:
        with self._write_lock:
            rows = self._read_rows(_REVIEW_DATA_FILE)
            index = self._index_of(rows, "source_data_id", str(record.source_data_id))
            if index is None:
                rows.append(to_row(record))
                self._write_rows(_REVIEW_DATA_FILE, rows)
                return True

            # 기존 행이 깨졌으면 조용히 덮어쓰지 않는다 — 운영자 승인 상태를 날릴 수 있다.
            existing = row_to_review_data(rows[index])
            if not existing.is_safe_to_replace:
                _LOG.info(
                    "운영자가 손댄 초안이라 재구조화 결과를 버림 (source_data_id=%s, status=%s)",
                    record.source_data_id,
                    existing.review_status.value,
                )
                return False
            rows[index] = to_row(record.carrying_operator_state_of(existing))
            self._write_rows(_REVIEW_DATA_FILE, rows)
            return True

    # ── 실행·상태 ───────────────────────────────────────────────

    def start_run(self, mode: CrawlMode) -> CrawlRun:
        with self._write_lock:
            run = CrawlRun(mode=mode, started_at=kst_now())
            rows = self._read_rows(_CRAWL_RUN_FILE)
            rows.append(to_row(run))
            self._write_rows(_CRAWL_RUN_FILE, rows)
            return run

    def finish_run(self, record: CrawlRun) -> None:
        with self._write_lock:
            rows = self._read_rows(_CRAWL_RUN_FILE)
            index = self._index_of(rows, "id", str(record.id))
            if index is None:
                raise StoreError(f"crawl_run {record.id} 없음 — 시작 기록 없이 종료할 수 없다")
            rows[index] = to_row(record)
            self._write_rows(_CRAWL_RUN_FILE, rows)

    def get_health(self, source_key: str) -> SourceHealth | None:
        rows = self._read_rows(_SOURCE_HEALTH_FILE)
        # 정규화하지 않으면 조회가 빗나가 previous=None이 되고, 누적 카운터가 매 실행
        # 초기화돼 SPEC §7 경보(연속 실패·연속 0건)가 영구히 울리지 않는다.
        index = self._index_of(rows, "source_key", normalize_source_key(source_key))
        # 손상 행을 None으로 삼키면 누적 카운터가 초기화돼 §7 경보가 죽는다 → 그대로 던진다.
        return None if index is None else row_to_source_health(rows[index])

    def upsert_health(self, record: SourceHealth) -> None:
        with self._write_lock:
            rows = self._read_rows(_SOURCE_HEALTH_FILE)
            index = self._index_of(rows, "source_key", record.source_key)
            if index is None:
                rows.append(to_row(record))
            else:
                rows[index] = to_row(record)
            self._write_rows(_SOURCE_HEALTH_FILE, rows)

    # ── 파일 I/O ────────────────────────────────────────────────

    def _path(self, file_name: str) -> Path:
        return self._dir / file_name

    def _read_rows(self, file_name: str) -> list[Row]:
        path = self._path(file_name)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as err:
            raise StoreError(f"{path}: 읽지 못함 ({err})") from err
        try:
            document: object = json.loads(text)
        except json.JSONDecodeError as err:
            raise StoreError(f"{path}: JSON 파싱 실패 ({err})") from err
        return self._extract_rows(document, path)

    def _extract_rows(self, document: object, path: Path) -> list[Row]:
        if not isinstance(document, dict):
            raise StoreError(f"{path}: 최상위가 객체가 아님")
        version = document.get("version")
        if version != FILE_VERSION:
            raise StoreError(
                f"{path}: 파일 포맷 버전 {version!r} — 이 코드는 {FILE_VERSION}만 읽는다"
                " (data/ 재작성 필요)"
            )
        records = document.get("records")
        if not isinstance(records, list):
            raise StoreError(f"{path}: records가 배열이 아님")
        rows: list[Row] = []
        for position, row in enumerate(records):
            if not isinstance(row, dict):
                raise StoreError(f"{path}: records[{position}]가 객체가 아님")
            rows.append(row)
        return rows

    def _write_rows(self, file_name: str, rows: Sequence[Row]) -> None:
        """임시파일 → rename으로 원자적 교체. 중단돼도 기존 파일이 잘리지 않는다."""
        path = self._path(file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": FILE_VERSION, "records": list(rows)}
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        # allow_nan=False — NaN·Infinity는 유효한 JSON이 아니고 jsonb가 거부한다.
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        try:
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                # rename 전에 내용을 디스크로 내린다 — 안 하면 전원 손실 시 이름만 바뀐
                # 빈/잘린 파일이 원장을 대체할 수 있다.
                os.fsync(handle.fileno())
            temp.replace(path)
        except OSError as err:
            # 부분 기록된 임시파일을 남기지 않는다(본 파일은 rename 전이라 온전하다).
            temp.unlink(missing_ok=True)
            # ⚠️ **`StoreError`로 바꿔 던진다**(`store/base.py` 계약). 디스크가 차거나 읽기
            #    전용이 되는 것은 "이 공고가 이상하다"가 아니라 "원장을 못 쓴다"이고, 그대로
            #    올리면 글 단위 격리도 연속 실패 중단도 지나쳐 배치가 통째로 죽는다 —
            #    운영자는 리포트도 미리보기 파일도 못 받는다.
            raise StoreError(f"{path}: 쓰지 못함 ({err})") from err

    # ── 공통 헬퍼 ───────────────────────────────────────────────

    def _decode_rows[R](self, file_name: str, decode: Callable[[Row], R]) -> list[R]:
        """행 단위 격리 — 손상된 한 행이 전체 로드를 죽이지 않게 한다."""
        records: list[R] = []
        for row in self._read_rows(file_name):
            try:
                records.append(decode(row))
            except SerdeError as err:
                self._on_corrupt_row(file_name, err)
        return records

    def _ledger_key_or_none(self, row: Row) -> tuple[str, str] | None:
        try:
            return ledger_key_of_row(row)
        except SerdeError as err:
            self._on_corrupt_row(_SOURCE_DATA_FILE, err)
            return None

    @staticmethod
    def _index_of(rows: Sequence[Row], key: str, value: str) -> int | None:
        for index, row in enumerate(rows):
            if row.get(key) == value:
                return index
        return None

    @staticmethod
    def _check_only_state_changed(stored: SourceData, incoming: SourceData) -> None:
        """원문 증거는 write-once — 갱신 경로로 바뀌면 구현이 막는다."""
        changed = [
            f.name
            for f in fields(SourceData)
            if f.name not in _MUTABLE_STATE_FIELDS
            and getattr(stored, f.name) != getattr(incoming, f.name)
        ]
        if changed:
            raise StoreError(f"원문 증거 필드는 갱신할 수 없음: {changed}")

    @staticmethod
    def _check_state_moves_forward(stored: SourceData, incoming: SourceData) -> None:
        """구조화 상태는 단조 증가만 허용한다 — 뒤로 가면 돈과 데이터가 같이 샌다.

        낡은 in-memory 레코드(attempts=0, structured_at=None)로 이 메서드를 부르면
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
