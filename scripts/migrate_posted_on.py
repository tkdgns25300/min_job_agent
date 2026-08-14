"""저장된 원장에 게시일을 채우고 파일 포맷을 2로 올린다. **일회성**(2026-08-14).

`source_data.posted_on`·`review_data.posted_at`이 필수가 됐다 — min_job이 게시일로 만료를
판정하는데(SPEC §9) 없으면 그 공고를 언제까지 보여줄지 정할 수 없다.

⚠️ **옛 파일을 그냥 두면 그 공고들이 유령이 된다.** 원장 조회는 `posted_on: null`을 읽어
"이미 본 글"이라 하고, 구조화 조회는 손상 행으로 건너뛴다 → 수집도 구조화도 되지 않는데
아무 경보도 울리지 않는다. 그래서 `FILE_VERSION`을 2로 올려 옛 파일을 **즉시 거부**하고,
이 스크립트가 재작성을 맡는다.

날짜를 어디서 얻나 — `PCKWORLD`만 비어 있고(목록에 날짜 칸이 없다) **썸네일 파일명**에
업로드 시각이 들어 있다(`/upimg/adsearch/20260729171107.jpg` → 2026-07-29 · 실측 60/60).
게시판에 다시 요청하지 않는다.

⚠️ **날짜 칸 말고는 한 글자도 바꾸지 않는다.** 하나라도 채우지 못하면 **파일을 쓰지 않는다** —
반쯤 이관된 원장이 제일 나쁘다.

    .venv/bin/python scripts/migrate_posted_on.py            # 무엇이 바뀔지만 보여준다
    .venv/bin/python scripts/migrate_posted_on.py --write    # 실제로 이관한다
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Final

from minjob_ingest.paths import PROJECT_ROOT
from minjob_ingest.sources.adapters.pckworld import uploaded_on
from minjob_ingest.store.json_store import FILE_VERSION

#: 원장 파일과 채워야 하는 날짜 칸(`None`이면 버전만 올린다). ⚠️ **여기 적힌 파일만 만진다** —
#: `data/`에는 `--out` 미리보기와 백업도 있고 그건 원장이 아니다(`migrate_timestamps_to_kst`와
#: 같은 방식).
_LEDGER_FILES: Final = {
    "source_data.json": "posted_on",
    "review_data.json": "posted_at",
    "source_health.json": None,
    "crawl_run.json": None,
}

#: 이 스크립트가 읽을 수 있는 옛 버전. 그보다 낮으면 사람이 봐야 한다.
_FROM_VERSION: Final = 1


class MigrationRefused(Exception):
    """이관할 수 없는 상태 — 아무것도 쓰지 않는다."""


def date_for(row: dict[str, object]) -> str | None:
    """이 행의 게시일. 채울 수 없으면 `None`."""
    thumbnail = row.get("raw_meta") or {}
    if not isinstance(thumbnail, dict):
        return None
    found = uploaded_on(str(thumbnail.get("thumbnail") or "") or None)
    return None if found is None else found.isoformat()


def fill(rows: list[dict[str, object]], field: str) -> int:
    """빈 날짜를 채우고 채운 수를 돌려준다. 못 채우면 `MigrationRefused`."""
    filled = 0
    for row in rows:
        if row.get(field):
            continue
        found = date_for(row)
        if found is None:
            label = f"{row.get('source_key')}/{row.get('external_id')}"
            raise MigrationRefused(f"{label}: {field}을 채울 근거가 없다 — 재수집이 필요하다")
        row[field] = found
        filled += 1
    return filled


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


def migrate_file(path: Path, *, write: bool) -> str:
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    version = document.get("version")
    if version == FILE_VERSION:
        return "이미 최신"
    if version != _FROM_VERSION:
        raise MigrationRefused(f"{path.name}: 버전 {version!r} — {_FROM_VERSION}만 이관한다")
    records = document.get("records")
    if not isinstance(records, list):
        raise MigrationRefused(f"{path.name}: records가 배열이 아니다")
    field = _LEDGER_FILES[path.name]
    filled = 0 if field is None else fill(records, field)
    document["version"] = FILE_VERSION
    if write:
        _write(path, document)
    return f"{len(records):>5}행 · 날짜 {filled}개 채움"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="실제로 이관한다 (기본은 미리보기)")
    args = parser.parse_args()

    data_dir = PROJECT_ROOT / "data"
    if not data_dir.exists():
        print("data/ 없음 — 할 일이 없다")
        return 0
    try:
        for name in _LEDGER_FILES:
            path = data_dir / name
            if not path.exists():
                print(f"  {name:<20} 없음 — 건너뜀")
                continue
            print(f"  {name:<20} {migrate_file(path, write=args.write)}")
    except MigrationRefused as err:
        print(f"⚠️ 이관하지 않았다 — {err}")
        return 1
    if not args.write:
        print("미리보기다 — 이관하려면 --write 를 준다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
