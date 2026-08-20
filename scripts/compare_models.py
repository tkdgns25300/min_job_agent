"""두 모델의 `structure --dry-run --out` 결과를 칸별로 견준다. **일회성 비교 도구**(2026-08-12).

`diff`로는 답이 안 나온다 — 20건에 34칸이면 줄이 수백이고, 어느 칸이 몇 번 갈렸는지가
안 보인다. 여기서는 **칸을 세로로 세워** "이 칸에서 두 모델이 몇 번 다르게 답했나"를 낸다.

⚠️ **어느 쪽이 맞다고 말하지 않는다.** 정답은 원문에 있고 그건 사람이 본다. 이 도구는
**볼 곳을 좁혀** 준다 — 갈린 칸만 원문과 대조하면 된다.

    .venv/bin/python scripts/compare_models.py data/lite.json data/flash.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

#: 한 칸에서 값을 보여줄 최대 글자. 요약은 길어서 통째로 찍으면 화면을 덮는다.
_PREVIEW_CHARS: Final = 88

#: 모델이 답하지 않는 칸 — 코드가 채우거나 다음 단계가 채운다. 갈릴 수 없으니 세지 않는다.
_NOT_FROM_MODEL: Final = frozenset({"dedup_key", "heresy_flag", "heresy_evidence", "posted_at"})

#: `--out` 파일이 같은 모델로 두 번 만들어졌을 때의 안내. 비교가 성립하지 않는다.
_SAME_MODEL_WARNING: Final = (
    "⚠️ 두 파일이 **같은 모델**이다 — 비교가 성립하지 않는다. 한쪽에 `--lite`를 빼먹었는지 확인하라."
)


def _load(path: Path) -> tuple[str, dict[str, dict[str, object]]]:
    """`--out` 파일 → (모델 이름, {공고 키: 초안}).

    ⚠️ **모델 이름은 파일에서 읽는다** — 파일 이름을 믿으면 `--lite`를 빼먹고 돌린 실행을
    Lite 결과로 읽어 결론이 조용히 반대가 된다.
    """
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: 배열이어야 한다 — `--out`이 만든 파일인가")
    models = {str(row.get("model")) for row in rows}
    if len(models) > 1:
        raise SystemExit(f"{path}: 한 파일에 모델이 섞여 있다 ({sorted(models)})")
    loaded: dict[str, dict[str, object]] = {}
    for row in rows:
        draft = row.get("draft") or {}
        loaded[str(row["posting"])] = {"__판정": row.get("verdict"), **draft}
    return (models.pop() if models else "(모델 기록 없음)"), loaded


def _show(value: object) -> str:
    text = "∅" if value is None or value == [] else str(value)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit(f"사용법: {argv[0]} <A.json> <B.json>")
    a_path, b_path = Path(argv[1]), Path(argv[2])
    (a_model, a), (b_model, b) = _load(a_path), _load(b_path)

    shared = sorted(set(a) & set(b))
    if not shared:
        raise SystemExit("겹치는 공고가 없다 — 두 실행이 같은 범위를 본 게 맞나(`--dry-run`이었나)")
    only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    print(f"A = {a_model}  ({a_path.name} · {len(a)}건)")
    print(f"B = {b_model}  ({b_path.name} · {len(b)}건)")
    if a_model == b_model:
        print(_SAME_MODEL_WARNING)
    print(f"견주는 공고 {len(shared)}건")
    if only_a or only_b:
        print(f"⚠️ 한쪽에만 있는 공고 — A만 {len(only_a)}건 · B만 {len(only_b)}건")
        print("   두 실행이 같은 20건을 보지 않았다. `--dry-run`을 빼먹으면 이렇게 된다.")

    disagreements: dict[str, list[tuple[str, object, object]]] = {}
    for posting in shared:
        for field in sorted(set(a[posting]) | set(b[posting])):
            if field in _NOT_FROM_MODEL:
                continue
            left, right = a[posting].get(field), b[posting].get(field)
            if left != right:
                disagreements.setdefault(field, []).append((posting, left, right))

    same = len(shared) - len({p for rows in disagreements.values() for p, _, _ in rows})
    print(f"\n두 모델이 **완전히 같게** 답한 공고 {same}/{len(shared)}건")

    if not disagreements:
        print("\n갈린 칸이 없다 — 이 표본에서는 두 모델을 나눌 근거가 없다.")
        return 0

    print(f"\n갈린 칸 {len(disagreements)}개 (많이 갈린 칸부터)")
    for field, rows in sorted(disagreements.items(), key=lambda item: -len(item[1])):
        print(f"\n── {field}  {len(rows)}/{len(shared)}건 갈림")
        for posting, left, right in rows:
            print(f"   {posting}")
            print(f"      A  {_show(left)}")
            print(f"      B  {_show(right)}")
    print("\n⚠️ 어느 쪽이 맞는지는 원문을 봐야 안다 — 위 공고만 열어 보면 된다.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
