"""구조화를 되돌린다 — 그 공고를 **아직 판정하지 않은 상태**로 만든다.

**로컬 JSON 전용이고 CLI 명령이 아니다.** 운영자가 실수로 부를 자리에 두지 않는다.

왜 필요한가 — `structured_at`은 앞으로만 간다. 한 번 판정한 공고는 다시 잡히지 않으므로,
전량(₩5만)을 저장한 뒤 프롬프트에서 문제 하나를 발견하면 **그 공고들에 고친 것을 적용할
방법이 없다**. 되돌릴 수단 없이 전량을 저장하지 않는다(ROADMAP 1-2 3단계).

무엇을 지우나 — 처리 상태 세 칸(`structured_at`·`structure_attempts`·`last_structure_error`)과
그 공고의 검수 초안. **원문 증거는 건드리지 않는다**(write-once · SPEC §6).

⚠️ **운영자가 손댄 초안은 되돌리지 않는다.** 승인·거절했거나 값을 고쳐둔 행을 지우면 사람이
한 일이 사라진다. 그런 공고는 건너뛰고 이름을 적어 내보낸다.

    .venv/bin/python scripts/reset_structure.py --source PCKWORLD   # 무엇을 되돌릴지만
    .venv/bin/python scripts/reset_structure.py --all --write       # 실제로 되돌린다
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Final

from minjob_ingest.domain import normalize_source_key
from minjob_ingest.settings import Settings
from minjob_ingest.store.json_store import _MUTABLE_STATE_FIELDS, FILE_VERSION
from minjob_ingest.store.serde import SerdeError, row_to_review_data

#: 만질 원장 파일. ⚠️ store의 사적 이름을 끌어오지 않는다 — 파일 경로가 store 밖으로 새면
#: 안 된다는 규칙(CLAUDE.md §Store)은 스크립트에도 같다. 이름이 바뀌면 여기서 즉시 터진다.
_SOURCE_DATA_FILE: Final = "source_data.json"
_REVIEW_DATA_FILE: Final = "review_data.json"

#: 되돌릴 때 되돌려 놓는 처리 상태.
_RESET_STATE: Final = {"structured_at": None, "structure_attempts": 0, "last_structure_error": None}

# ⚠️ store가 갱신을 허용하는 칸과 **같은 집합**이어야 한다. 넷째 상태 칸이 생겼을 때 여기만
#    모르면 그 값이 남아 되돌린 공고가 재구조화에서 조용히 다르게 처리된다.
if set(_RESET_STATE) != set(_MUTABLE_STATE_FIELDS):
    raise RuntimeError(f"처리 상태 칸이 store와 다르다: {set(_MUTABLE_STATE_FIELDS)}")


class ResetRefused(Exception):
    """되돌릴 수 없는 상태 — 아무것도 쓰지 않고 멈춘다."""


def _load(path: Path) -> dict[str, object]:
    """읽고 **포맷 버전을 확인한다** — 옛 파일을 그대로 고치면 그 파일이 계속 살아남는다."""
    if not path.exists():
        return {"version": FILE_VERSION, "records": []}
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    version = document.get("version")
    if version != FILE_VERSION:
        raise ResetRefused(
            f"{path}: 파일 포맷 버전 {version!r} — 이 코드는 {FILE_VERSION}만 읽는다"
        )
    return document


def _rows(document: dict[str, object]) -> list[dict[str, object]]:
    records = document.get("records")
    if not isinstance(records, list):
        raise ResetRefused("records가 배열이 아니다")
    return records


def _write(path: Path, document: dict[str, object]) -> None:
    """임시파일 → fsync → rename. `JsonStore._write_rows`와 같은 규칙이다 — 17MB 원장이라
    전원이 나가면 이름만 바뀐 잘린 파일이 원장을 대체할 수 있다."""
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def protected_ids(review_rows: list[dict[str, object]]) -> set[str]:
    """되돌리면 안 되는 초안의 `source_data_id`.

    기준은 `ReviewData.is_safe_to_replace` **하나뿐**이다 — 저장 쪽과 같은 판정을 써야 한쪽이
    다른 쪽이 지키기로 한 행을 지우지 않는다.

    ⚠️ **읽을 수 없는 행이 하나라도 있으면 멈춘다.** 그 행이 승인된 것인지 알 수 없는데
    지우면 되돌릴 방법이 없다.
    """
    protected: set[str] = set()
    for row in review_rows:
        try:
            draft = row_to_review_data(row)
        except SerdeError as err:
            # 무엇을 지켜야 할지 모르는 상태다 — 지우지 않고 멈춘다.
            raise ResetRefused(f"읽을 수 없는 초안이 있다: {err}") from err
        if not draft.is_safe_to_replace:
            protected.add(str(draft.source_data_id))
    return protected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--source", default=None, help="한 게시판만 (예: PCKWORLD)")
    scope.add_argument("--all", action="store_true", help="판정된 전부")
    parser.add_argument("--write", action="store_true", help="실제로 되돌린다 (기본은 미리보기)")
    args = parser.parse_args()
    try:
        return _reset(source=args.source, write=bool(args.write))
    except ResetRefused as err:
        print(f"⚠️ 되돌리지 않았다 — {err}")
        return 1


def _reset(*, source: str | None, write: bool) -> int:
    data_dir = Settings.load().data_dir
    source_path = data_dir / _SOURCE_DATA_FILE
    review_path = data_dir / _REVIEW_DATA_FILE
    source_document = _load(source_path)
    review_document = _load(review_path)
    source_rows = _rows(source_document)
    review_rows = _rows(review_document)

    wanted = None if source is None else normalize_source_key(source)
    protected = protected_ids(review_rows)

    targets: list[dict[str, object]] = []
    skipped: list[str] = []
    for row in source_rows:
        if row.get("structured_at") is None:
            continue
        if wanted is not None and row["source_key"] != wanted:
            continue
        if str(row["id"]) in protected:
            skipped.append(f"{row['source_key']}/{row['external_id']}")
            continue
        targets.append(row)

    target_ids = {str(row["id"]) for row in targets}
    drafts = [row for row in review_rows if str(row.get("source_data_id")) in target_ids]
    scope_label = "전체" if wanted is None else wanted
    print(f"되돌릴 공고 {len(targets)}건 ({scope_label}) · 함께 지울 초안 {len(drafts)}건")
    if skipped:
        print(f"⚠️ 운영자가 손대 건너뜀 {len(skipped)}건: {', '.join(skipped[:10])}")

    if not write:
        print("미리보기다 — 되돌리려면 --write 를 준다.")
        return 0
    if not targets:
        return 0

    for row in targets:
        row.update(_RESET_STATE)
    review_document["records"] = [
        row for row in review_rows if str(row.get("source_data_id")) not in target_ids
    ]
    # ⚠️ **판정을 먼저 되돌린다**(`store/base.py` 저장 순서의 역순). 초안을 먼저 지우면 그 사이에
    #    죽었을 때 "판정 완료 + 초안 없음"이 남는데, SPEC §4가 그 상태를 재시도 기준으로 쓰지
    #    않아 사후 탐지가 불가능하다. 이 순서면 "미판정 + 낡은 초안"이 되고, 다음 `structure`가
    #    같은 `source_data_id`로 덮어써서 스스로 복구한다.
    _write(source_path, source_document)
    _write(review_path, review_document)
    print(f"{len(targets)}건을 미판정으로 되돌렸다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
