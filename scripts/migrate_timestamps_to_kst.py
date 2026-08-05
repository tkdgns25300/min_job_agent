"""저장된 시각 표기를 UTC(`...Z`)에서 KST(`+09:00`)로 옮긴다. **일회성**(2026-08-05).

⚠️ **순간은 바뀌지 않는다** — `2026-08-05T09:33:08Z`와 `2026-08-05T18:33:08+09:00`은 같은
시점이고 Postgres `timestamptz`도 동일하게 저장한다. 바뀌는 것은 사람이 읽는 표기뿐이다.

⚠️ **시각 필드만 만진다.** `date` 컬럼(`posted_on`·`deadline`·`last_cutoff`·`last_posted_on`)은
시간대가 없는 값이고, 게시판이 이미 KST로 표시한 날짜다 — 하루씩 밀면 백필 컷오프가 어긋난다.
그 밖의 값(원문·URL·첨부·raw_meta)은 **한 글자도 건드리지 않는다**.

검증: 바꾼 뒤 모든 값을 되읽어 **바꾸기 전과 같은 순간**인지 확인하고, 시각 아닌 필드는
바이트 단위로 같은지 비교한다. 하나라도 다르면 파일을 쓰지 않는다.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Final

from minjob_ingest.clock import to_iso
from minjob_ingest.paths import PROJECT_ROOT

#: 시각 컬럼(파일별). ⚠️ 여기 없는 키는 만지지 않는다.
TIMESTAMP_FIELDS: Final = {
    "source_data.json": ("fetched_at", "structured_at"),
    "review_data.json": ("reviewed_at", "created_at"),
    "source_health.json": ("last_run_at", "first_run_at", "last_success_at"),
    "crawl_run.json": ("started_at", "finished_at"),
}
#: ⚠️ 절대 만지지 않는 날짜 컬럼 — 기록으로 남긴다(누가 나중에 추가하지 않게).
DATE_FIELDS_UNTOUCHED: Final = (
    "posted_on",
    "posted_at",
    "deadline",
    "last_cutoff",
    "last_posted_on",
)


def convert(value: str) -> str:
    """ISO8601 시각 문자열을 KST 표기로. 순간은 유지된다."""
    return to_iso(datetime.fromisoformat(value))


def migrate_file(path: Path, fields: tuple[str, ...]) -> tuple[int, int]:
    """한 파일을 옮긴다. `(바꾼 값 수, 행 수)`. 검증 실패 시 예외."""
    original_text = path.read_text(encoding="utf-8")
    document = json.loads(original_text)
    rows = document["records"]
    before = [dict(row) for row in rows]

    changed = 0
    for row in rows:
        for field in fields:
            value = row.get(field)
            if isinstance(value, str) and value:
                moved = convert(value)
                if moved != value:
                    row[field] = moved
                    changed += 1

    _verify(before, rows, fields)
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)
    return changed, len(rows)


def _verify(
    before: list[dict[str, object]], after: list[dict[str, object]], fields: tuple[str, ...]
) -> None:
    """같은 순간인지 + 나머지가 그대로인지 확인한다."""
    if len(before) != len(after):
        raise SystemExit("행 수가 달라졌다 — 중단")
    for old, new in zip(before, after, strict=True):
        if set(old) != set(new):
            raise SystemExit(f"키 집합이 달라졌다 — 중단 ({sorted(set(old) ^ set(new))})")
        for key in old:
            if key in fields and isinstance(old[key], str) and old[key]:
                left = datetime.fromisoformat(str(old[key]))
                right = datetime.fromisoformat(str(new[key]))
                if left != right:
                    raise SystemExit(f"순간이 달라졌다 — 중단 ({key}: {old[key]} → {new[key]})")
            elif old[key] != new[key]:
                raise SystemExit(f"시각 아닌 값이 바뀌었다 — 중단 ({key})")


def main() -> int:
    data_dir = PROJECT_ROOT / "data"
    if not data_dir.exists():
        print("data/ 없음 — 할 일이 없다")
        return 0
    for name, fields in TIMESTAMP_FIELDS.items():
        path = data_dir / name
        if not path.exists():
            print(f"  {name:<20} 없음 — 건너뜀")
            continue
        changed, total = migrate_file(path, fields)
        print(f"  {name:<20} {total:>5}행 · 시각 {changed}개 이관")
    print(f"\n  건드리지 않은 날짜 컬럼: {', '.join(DATE_FIELDS_UNTOUCHED)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
