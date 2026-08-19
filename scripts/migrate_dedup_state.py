"""저장된 초안에서 **없어진 `dedup_state` 값을 지운다**. **일회성**(2026-08-19).

`SEPARATE`("키는 같지만 다른 자리로 확정")를 없앴다 — 연락처 규칙이 바뀌어(SPEC §4.1 ⑤)
확정 대신 **사람이 정하는** 것으로 갔고, 그 값을 만드는 경로가 사라졌다.

⚠️ **enum 값을 없애면 저장된 행을 읽을 수 없다.** 경계 검증이 `'SEPARATE'는 허용값 아님`으로
거부하고, 중복 판정은 그 행이 대표였을 수 있어 **멈춘다**(건너뛰면 대표가 아닌 쪽이 공개된다).
그래서 옛 값을 지우는 일을 이 스크립트가 맡는다.

**지우는 것은 파생값 둘뿐이다** — `dedup_key`·`dedup_state`. 판정 상태(`review_status`·
`reject_reason`)는 손대지 않는다: 없어진 값을 갖던 행은 거절된 적이 없어(등급대로 살아 있었다)
지울 것이 없고, 다음 `dedup` 실행이 새 규칙으로 다시 판정해 채운다.

    .venv/bin/python scripts/migrate_dedup_state.py           # 무엇이 바뀔지만 보여준다
    .venv/bin/python scripts/migrate_dedup_state.py --write   # 실제로 지운다
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Final

from minjob_ingest.domain import DedupState
from minjob_ingest.settings import Settings

_REVIEW_DATA_FILE: Final = "review_data.json"

#: 지금 살아 있는 값. 그 밖은 없어진 값이다.
_ALLOWED: Final = frozenset(state.value for state in DedupState)


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


def clear_stale(rows: list[dict[str, object]]) -> list[str]:
    """없어진 값을 가진 행의 파생 칸을 지우고, 그 행들의 표시를 돌려준다."""
    cleared: list[str] = []
    for row in rows:
        state = row.get("dedup_state")
        if state is None or state in _ALLOWED:
            continue
        if row.get("reject_reason") is not None:
            # 거절된 행이 없어진 값을 갖고 있으면 상태까지 손봐야 하는데, 그건 이 스크립트가
            # 할 일이 아니다(판정은 `dedup`이 내린다) — 멈추고 사람에게 알린다.
            raise MigrationRefused(
                f"거절된 행이 없어진 값을 갖고 있다 (id={row.get('id')} · {state})"
                " — 손으로 확인할 것"
            )
        row["dedup_key"] = None
        row["dedup_state"] = None
        cleared.append(f"{state} → 지움 (id={row.get('id')})")
    return cleared


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
        cleared = clear_stale(rows)
    except MigrationRefused as err:
        print(f"멈췄다: {err}")
        return 1

    print(f"{path.name}: {len(rows)}행 중 {len(cleared)}행에서 없어진 값을 지운다")
    for line in cleared[:10]:
        print(f"   {line}")
    if not cleared:
        return 0
    if not args.write:
        print("미리보기다 — 지우려면 --write 를 준다. 그 뒤 `minjob-ingest dedup`이 다시 판정한다.")
        return 0
    _write(path, document)
    print("지웠다 — 이제 `minjob-ingest dedup`을 돌리면 새 규칙으로 다시 판정한다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
