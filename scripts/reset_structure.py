"""구조화를 되돌린다 — 그 공고를 **아직 판정하지 않은 상태**로 만든다.

**CLI 명령이 아니다.** 운영자가 실수로 부를 자리에 두지 않는다.

왜 필요한가 — `structured_at`은 앞으로만 간다. 한 번 판정한 공고는 다시 잡히지 않으므로,
전량(₩5만)을 저장한 뒤 프롬프트에서 문제 하나를 발견하면 **그 공고들에 고친 것을 적용할
방법이 없다**. 되돌릴 수단 없이 전량을 저장하지 않는다(ROADMAP 1-2 3단계).

⚠️ **원장은 `Store`가 만진다.** 여기서 파일을 직접 열지 않는다 — 그러면 Supabase 전환
(ROADMAP 1-6) 뒤에 이 스크립트가 원장이 아닌 로컬 JSON을 고치면서 "됐다"고 답한다.
무엇을 지우고 무엇을 지키는지는 `Store.requeue_for_structure` 계약에 있다.

    .venv/bin/python scripts/reset_structure.py --source PCKWORLD   # 무엇을 되돌릴지만
    .venv/bin/python scripts/reset_structure.py --all --write       # 실제로 되돌린다
"""

from __future__ import annotations

import argparse

from minjob_ingest.domain import normalize_source_key
from minjob_ingest.settings import Settings
from minjob_ingest.store.base import StoreError
from minjob_ingest.store.json_store import JsonStore

#: 건너뛴 공고를 화면에 몇 개까지 적을까. 전부 찍으면 되돌린 수가 안 보인다.
_SKIPPED_SAMPLE = 10


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--source", default=None, help="한 게시판만 (예: PCKWORLD)")
    scope.add_argument("--all", action="store_true", help="판정된 전부")
    parser.add_argument("--write", action="store_true", help="실제로 되돌린다 (기본은 미리보기)")
    args = parser.parse_args()

    source = None if args.source is None else normalize_source_key(str(args.source))
    store = JsonStore(Settings.load().data_dir)
    scope_label = "전체" if source is None else source

    if not args.write:
        # ⚠️ 미리보기는 **세기만 한다** — 되돌리기는 `Store`가 하나로 처리하므로 여기서
        #    같은 판정을 흉내 내면 두 기준이 갈린다. 남은 미판정 수로 규모만 보여준다.
        pending = store.list_unstructured(100_000, source_key=source)
        print(f"{scope_label}: 지금 미판정 {len(pending)}건 — 되돌리면 여기에 더해진다")
        print("미리보기다 — 되돌리려면 --write 를 준다.")
        return 0

    try:
        result = store.requeue_for_structure(source_key=source)
    except StoreError as err:
        print(f"⚠️ 되돌리지 않았다 — {err}")
        return 1

    print(f"{scope_label}: {result.requeued}건을 미판정으로 되돌렸다")
    if result.skipped:
        names = ", ".join(result.skipped[:_SKIPPED_SAMPLE])
        print(f"⚠️ 초안을 지켜 건너뜀 {len(result.skipped)}건: {names}")
        print("   (검수 상태가 PENDING이 아닌 행 — 운영자 승인·거절과 코드가 만든 거절 둘 다 포함)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
