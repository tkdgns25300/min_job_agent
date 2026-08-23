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
from minjob_ingest.store.factory import opened_store

#: 건너뛴 공고를 화면에 몇 개까지 적을까. 전부 찍으면 되돌린 수가 안 보인다.
_SKIPPED_SAMPLE = 10


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--source", default=None, help="한 게시판만 (예: PCKWORLD)")
    scope.add_argument(
        "--posting",
        action="append",
        metavar="KEY/ID",
        help="공고 하나만 (예: BU/58590 · 여러 번 줄 수 있다 · 게시판이 섞여도 된다)",
    )
    scope.add_argument("--all", action="store_true", help="판정된 전부")
    parser.add_argument("--write", action="store_true", help="실제로 되돌린다 (기본은 미리보기)")
    args = parser.parse_args()

    if args.posting:
        try:
            wanted = _by_board(args.posting)
        except ValueError as err:
            print(f"⚠️ {err}")
            return 2
    else:
        source = None if args.source is None else normalize_source_key(str(args.source))
        wanted = {source: None}

    # ⚠️ **팩토리를 거친다.** `JsonStore`를 직접 만들면 Supabase로 넘어간 뒤 이 스크립트만
    #    로컬 파일을 되돌려, 운영자는 되돌린 줄 알지만 원장은 그대로다. CLAUDE.md가
    #    "되돌리기 스크립트 없이 전량 저장하지 않는다"고 못 박은 이유가 그것이다.
    with opened_store(Settings.load()) as session:
        store = session.store
        print(f"저장소: {session.label}")

        if not args.write:
            for source, external_ids in wanted.items():
                label = _label(source, external_ids)
                if external_ids is not None:
                    print(f"{label}: 되돌릴 공고 {len(external_ids)}건")
                    continue
                # ⚠️ 미리보기는 **세기만 한다** — 되돌리기는 `Store`가 하나로 처리하므로 여기서
                #    같은 판정을 흉내 내면 두 기준이 갈린다. 남은 미판정 수로 규모만 보여준다.
                pending = store.list_unstructured(100_000, source_key=source)
                print(f"{label}: 지금 미판정 {len(pending)}건 — 되돌리면 여기에 더해진다")
            print("미리보기다 — 되돌리려면 --write 를 준다.")
            return 0

        requeued = 0
        skipped: list[str] = []
        for source, external_ids in wanted.items():
            try:
                result = store.requeue_for_structure(source_key=source, external_ids=external_ids)
            except StoreError as err:
                # ⚠️ 한 게시판이 실패해도 나머지를 되돌린 사실은 알려야 한다 — 조용히 죽으면
                #    운영자가 무엇이 되돌아갔는지 모른 채 재구조화를 돌린다.
                print(f"⚠️ {_label(source, external_ids)}: 되돌리지 않았다 — {err}")
                return 1
            print(f"{_label(source, external_ids)}: {result.requeued}건을 미판정으로 되돌렸다")
            requeued += result.requeued
            skipped.extend(result.skipped)

    if len(wanted) > 1:
        print(f"합계: {requeued}건")
    if skipped:
        names = ", ".join(skipped[:_SKIPPED_SAMPLE])
        print(f"⚠️ 초안을 지켜 건너뜀 {len(skipped)}건: {names}")
        print("   (검수 상태가 PENDING이 아닌 행 — 운영자 승인·거절과 코드가 만든 거절 둘 다 포함)")
    return 0


def _by_board(postings: list[str]) -> dict[str | None, tuple[str, ...] | None]:
    """`KEY/ID` 목록을 게시판별로 묶는다 — `external_id`가 게시판 안에서만 유일해서다."""
    grouped: dict[str | None, tuple[str, ...] | None] = {}
    for raw in postings:
        key, sep, external_id = str(raw).partition("/")
        if not sep or not key.strip() or not external_id.strip():
            raise ValueError(f"공고를 KEY/ID 로 적는다 (받은 값: {raw!r})")
        source = normalize_source_key(key.strip())
        grouped[source] = (*(grouped.get(source) or ()), external_id.strip())
    return grouped


def _label(source: str | None, external_ids: tuple[str, ...] | None) -> str:
    if source is None:
        return "전체"
    if external_ids is None:
        return source
    return f"{source} {'·'.join(external_ids)}"


if __name__ == "__main__":
    raise SystemExit(main())
