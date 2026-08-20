"""저장된 초안에서 **없어진 `matched_church_id` 칸을 지운다**. **일회성**(2026-08-20).

교회 연결은 min_job이 `jobs.church_id`에 쓴다(SPEC §8) — 우리가 저장할 것이 없어 칸을
삭제했다. 원래는 공개 **전에** 교회명으로 어느 교회 행일지 미리 추측해 이어두려던 칸이고,
동명이교회를 가릴 수 없어 2026-08-06에 이미 쓰지 않기로 했다(그때는 컬럼을 남겼다).

⚠️ **칸을 없애면 저장된 행을 읽을 수 없다.** `serde`가 컬럼 집합을 엄격히 대조해 **잉여
컬럼도 거부**하므로, 이 키가 남은 694행은 전부 `SerdeError`가 되어 조용히 건너뛰어진다.
그래서 키를 떼는 일을 이 스크립트가 맡는다.

**값이 들어 있는 행이 있으면 멈춘다.** 항상 NULL이어야 하는 칸이라 값이 있다는 건 우리가
모르는 경로가 채웠다는 뜻이다 — 그걸 조용히 버리면 무엇을 잃었는지 아무도 모른다.

    .venv/bin/python scripts/drop_matched_church_id.py           # 무엇이 바뀔지만 보여준다
    .venv/bin/python scripts/drop_matched_church_id.py --write   # 실제로 지운다
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Final

from minjob_ingest.settings import Settings

_REVIEW_DATA_FILE: Final = "review_data.json"

#: 없어진 칸 이름.
_DROPPED: Final = "matched_church_id"


class MigrationRefused(Exception):
    """반쯤 이관된 원장이 제일 나쁘다 — 하나라도 어긋나면 아무것도 쓰지 않는다."""


def _write(path: Path, document: dict[str, object]) -> None:
    """임시파일 → fsync → rename. `JsonStore._write_rows`와 같은 규칙이다."""
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


def drop_column(rows: list[dict[str, object]]) -> int:
    """없어진 키를 떼고, 실제로 뗀 행 수를 돌려준다."""
    dropped = 0
    for row in rows:
        if _DROPPED not in row:
            continue
        value = row[_DROPPED]
        if value is not None:
            # 항상 NULL이어야 하는 칸이다 — 값이 있으면 무엇을 잃는지 사람이 봐야 한다.
            raise MigrationRefused(
                f"{_DROPPED}에 값이 있는 행이 있다 (id={row.get('id')} · {value!r})"
                " — 손으로 확인할 것"
            )
        del row[_DROPPED]
        dropped += 1
    return dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="실제로 지운다 (기본은 미리보기)")
    args = parser.parse_args()

    path = Settings.load().data_dir / _REVIEW_DATA_FILE
    if not path.exists():
        print(f"{path.name}: 파일이 없다 — 지울 것도 없다")
        return 0

    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("records"), list):
        print(f"{path.name}: 최상위 모양이 다르다 — 손으로 확인할 것")
        return 1
    document: dict[str, object] = loaded
    records = document["records"]
    assert isinstance(records, list)  # 위에서 확인했다 — 타입만 좁힌다
    rows = [row for row in records if isinstance(row, dict)]

    try:
        dropped = drop_column(rows)
    except MigrationRefused as err:
        print(f"멈췄다: {err}")
        return 1

    print(f"{path.name}: {len(rows)}행 중 {dropped}행에서 `{_DROPPED}` 키를 뗀다")
    if not dropped:
        return 0
    if not args.write:
        print("미리보기다 — 지우려면 --write 를 준다.")
        return 0
    _write(path, document)
    print("뗐다 — 이제 원장을 읽을 수 있다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
