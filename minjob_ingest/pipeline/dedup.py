"""중복 판정(SPEC §4.1) — 같은 자리 하나가 여러 번·여러 게시판에 올라온 것을 하나로 줄인다.

`denomination.py`·`heresy.py`·`confidence.py`와 같은 자리다 — **모델을 부르지 않는다.** 저장된
초안만 보고 판정하므로 유료 호출 없이 검증된다.

실측 3,188건에서 같은 글의 반복이 **약 42%**다(`점촌제일교회` 전임 사역자 한 자리가 31건 —
CSU 23 · DAESHIN 5 · KWANGSHIN 1). 그대로 승격하면 목록 절반이 중복이 된다.

교차게시가 몰린 5개 교회 93건으로 전 구간을 돌린 결과(2026-08-17): **11자리로 줄었다**
(중복 80건 제거 · 86%). `서귀포제일교회` 20건이 1자리, `안양동은교회 유년부` 12건이 1자리다.

⚠️ **글 하나만 보고는 판정할 수 없다.** 그래서 구조화 루프 안이 아니라 **전체를 훑는 별도
패스**다. 구조화 안에서 하면 1번째 글을 처리할 때 31번째가 아직 없어 **먼저 온 글이 항상
대표**가 되고, 게시판을 훑은 순서에 따라 결과가 달라진다.

판정 순서(자물쇠 셋 → 라운드 → 부서 → 연락처):

    1. 후보     교회명(정규화)·지역·직분이 **다 있고 다 같아야** 같은 자리 후보다.
                하나라도 없으면 판정하지 않는다(실측 132건 중 2건 — 지역이 없었다).
    2. 라운드   원문 게시일 순으로 놓고 직전 글과 3개월 초과로 벌어지면 새 라운드(= 재공고).
    3. 부서     같은 값 · 둘 다 없음 · **한 값 + 침묵** → 4번으로(침묵은 안 적은 것이다) ·
                서로 다른 값 → 다른 자리 · **두 값 이상 + 침묵** → 사람이 본다.
    4. 연락처   **접수 이메일**이 양쪽에 있는데 겹치지 않으면 사람이 본다(실측 1묶음).
                전화·링크·우편은 보지 않는다 — 같은 교회라는 뜻일 뿐 자리를 가르지 못한다.

⚠️ **연락처 중 접수 이메일만 본다**(운영자 결정 2026-08-19). 자물쇠에서 이미 같은 교회임을
확인했으므로 전화·홈페이지·교회 주소가 겹친다는 것은 "같은 교회"라는 뜻일 뿐이고, 다르다는 것도
자리를 가르지 못한다 — 게시판마다 대표번호와 담당자 휴대폰을 달리 적는다(실측: 그 셋으로 가르니
11묶음 중 10개가 잘못 갈렸다). 반면 접수 이메일은 담당자별로 달라 자리를 가른다.

⚠️ **제목은 보지 않는다**(2026-08-17 결정). 게시판을 넘으면 같은 자리도 제목이 다르고
(`이스탄불한인교회 담임목사 청빙` / `이스탄불 한인교회(초교파)에서 담임목사를 청빙합니다`),
제목이 같아도 다른 자리인 경우가 24%였다. 같은 게시판 안에서만 비교하는 규칙을 만들어 봤지만
연락처 규칙이 같은 2묶음을 잡으면서 `상동교회 담임목사 청빙 공고` → `…공고(수정)` 재게시를
잘못 가르지 않았다 — 실측 이득 0, 비용 1이라 뺐다.

⚠️ **부서가 섞였을 때 완화하지 않는다**(운영자 결정 2026-08-17). 실측 비용을 알고 내린 결정이다:
`진천중앙교회` 19건은 본문이 같은 한 청빙인데 게시판 폼의 `모집부서` 칸이 `부목사`(5월) →
`교구목사`(6월)로 바뀌어 **19건이 통째로 검수 대기**가 됐다(18건은 눌러 지우는 일). 그래도
완화하지 않는 이유는 방향이 비대칭이기 때문이다 — 침묵을 "같은 부서"로 단정하면 **부서가 다른
자리를 합쳐 공고 하나가 사라지고, 그건 되돌릴 수 없다**. 지금까지 이 규칙이 검수로 보낸 21건
중 진짜 다른 자리는 0건이었지만, 0건은 "없다"가 아니라 "아직 안 봤다"다.

⚠️ **부서를 필수로 만들 수 없다.** 실측 132건 중 91건(69%)이 비어 있고, 그중 30건은 원문에
부서 낱말이 아예 없고(담임목사 14건) 36건은 `교구 및 교육부서를 담당할 부목사`처럼 **한 자리가
부서를 여럿 걸친다**. 모델에게 채우라고 하면 없는 값을 만들거나 둘 중 하나를 버린다 →
**"없음"도 하나의 값으로** 쓰고(`-`), 그 때문에 생기는 위험만 4번이 막는다.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import date
from itertools import combinations
from typing import Final

from minjob_ingest.clock import months_before
from minjob_ingest.domain import Confidence, DedupState, Department, RejectReason, ReviewStatus
from minjob_ingest.models import REVIEW_STATE_FIELDS, ReviewData
from minjob_ingest.pipeline.confidence import review_status_for
from minjob_ingest.store.base import DedupCandidate, DedupUpdate, DedupVerdict, Store

#: 라운드 경계. 이만큼 벌어지면 **재공고**로 보고 따로 남긴다(SPEC §4.1).
#: ⚠️ 짧게 잡는 쪽이 안전하다 — 안 묶이면 중복이 남을 뿐이고, 길게 잡으면 다시 열린 자리가
#: 옛 묶음에 삼켜져 **진짜 공고가 사라진다**.
ROUND_MONTHS: Final = 3

#: 교회명에서 떼는 것 — 괄호 안(지역·교단 표기)과 공백·기호.
#: `[군산] 개복교회(전북 군산)` → `개복교회` · `세상의 빛 이레교회` → `세상의빛이레교회`
_BRACKETS: Final = re.compile(r"[\uff08(\[\u3010][^)\uff09\]\u3011]*[)\uff09\]\u3011]")
_NOISE: Final = re.compile(r"""[\s\u00b7\u30fb.,'"!?~\-\u2013\u2014_/]""")

#: 부서를 말하지 않은 공고의 키 조각. **값이 없다는 사실 자체가 값**이다(위 docstring).
NO_DEPARTMENT: Final = "-"

#: 일반직은 직분이 없고 직무가 있다. 자유 텍스트라 공백·기호를 떼서 쓴다.
_ROLE_PREFIX: Final = "ROLE:"

#: 한 칸에 **여러 곳이 들어간다** — 연락처는 조립 칸이라 원문에 둘이 적혀 있으면 둘 다 담긴다
#: (실측 `apply@x.org, office@x.org`). 그래서 조각으로 쪼개 **겹치는지**를 본다.
_MAIL_TOKEN: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: 이 이유로 거절된 행은 판정에서 뺀다 — 이미 결론이 난 행이다.
#: ⚠️ `DUPLICATE`는 여기 **없다**: 지난 실행이 내린 우리 판정이라 매번 처음부터 다시 본다.
#: 그래야 규칙을 고쳐 다시 돌렸을 때 잘못 거절한 행이 되살아난다(멱등 + 자기 수정).
_SETTLED_REASONS: Final = frozenset(
    {RejectReason.HERESY, RejectReason.CLOSED, RejectReason.OPERATOR}
)

#: 충실함을 셀 때 제외하는 칸 — 판정·식별자라 내용이 아니다.
_NOT_CONTENT: Final = frozenset(REVIEW_STATE_FIELDS) | {
    "source_data_id",
    "run_id",
    "dedup_key",
    "dedup_state",
}

#: 자물쇠 셋(교회명·지역·직분).
type Seat = tuple[str, str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class DedupReport:
    """실행 요약. ⚠️ **상태별로 센다** — 합계만 보면 "무엇이 사라졌나"를 알 수 없다."""

    #: 판정에 들어간 초안 수(이단·마감 거절 포함 — 저장소는 걸러내지 않는다).
    scanned: int = 0
    #: 상태별 행 수(`DedupState` 값). 화면·리포트가 이걸 쓴다.
    states: Mapping[str, int] = field(default_factory=dict)
    #: 중복이 확정된 묶음 수. `중복 21건 → 대표 17건`처럼 몇 자리로 줄었는지 보여준다.
    groups: int = 0
    #: 자물쇠(교회명·지역·직분)가 비어 **견줄 수 없던** 행. 조용히 빠지면 "왜 이 중복이 안
    #: 잡히나"에 답할 수 없다.
    unjudged: int = 0
    #: 이미 결론이 난 행(이단·마감·운영자 거절). ⚠️ 위와 **따로 센다** — 섞으면 "자물쇠가 비었다"는
    #: 화면 설명이 거짓이 된다.
    settled: int = 0
    #: 실제로 바뀐 행 수. `--dry-run`이면 0이다.
    changed: int = 0

    def count(self, state: DedupState) -> int:
        return self.states.get(state.value, 0)


def dedup_all(store: Store, *, dry_run: bool) -> DedupReport:
    """전체를 훑어 중복을 판정하고 저장한다.

    ⚠️ **배치로 쪼개지 않는다**(SPEC §4.1) — 한 글만 보고는 대표를 고를 수 없다. 3,188건이
    메모리에 다 들어오고 유료 호출도 네트워크도 없다.
    """
    candidates = store.dedup_candidates()
    updates = plan(candidates)
    judged = {update.review_data_id for update in updates}
    skipped = [candidate.draft for candidate in candidates if candidate.draft.id not in judged]
    settled = sum(1 for draft in skipped if draft.reject_reason in _SETTLED_REASONS)
    return DedupReport(
        scanned=len(candidates),
        states=dict(Counter(update.dedup_state.value for update in updates)),
        groups=len(
            {update.dedup_key for update in updates if update.dedup_state is DedupState.MASTER}
        ),
        unjudged=len(skipped) - settled,
        settled=settled,
        changed=0 if dry_run else store.apply_dedup(updates),
    )


def normalize_church_name(name: str | None) -> str | None:
    """교회명을 견줄 꼴로. **정규화는 이 칸 하나뿐이다** — 나머지 셋은 enum이라 안 흔들린다."""
    if name is None:
        return None
    return _NOISE.sub("", _BRACKETS.sub("", name)) or None


def plan(candidates: Sequence[DedupCandidate]) -> tuple[DedupUpdate, ...]:
    """중복 판정. **순수 함수** — 같은 입력이면 항상 같은 결과다(멱등).

    돌려주지 않은 행은 **판정하지 않았다는 뜻**이다(자물쇠가 비었거나 이미 결론이 난 행).
    """
    seats: dict[Seat, list[DedupCandidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.draft.reject_reason in _SETTLED_REASONS:
            continue
        seat = seat_of(candidate.draft)
        if seat is None:
            continue
        seats[seat].append(candidate)

    updates: list[DedupUpdate] = []
    for seat in sorted(seats):
        for number, members in enumerate(_rounds(seats[seat]), start=1):
            updates.extend(_judge(seat, number, members))
    return tuple(updates)


def seat_of(draft: ReviewData) -> Seat | None:
    """자물쇠 셋. 하나라도 없으면 `None` — 그 공고는 아무와도 견주지 않는다.

    ⚠️ 근거가 없을 때 묶지 않는 쪽이 안전하다: 중복이 남는 것은 되돌릴 수 있지만, **다른
    교회를 합치는 것은 되돌릴 수 없다**(교회명 894종 중 70종이 두 지역 이상에 있다).
    """
    church = normalize_church_name(draft.church_name)
    if church is None or draft.region is None:
        return None
    if draft.position:
        role = "+".join(member.value for member in draft.position)
    elif draft.role:
        role = _ROLE_PREFIX + _NOISE.sub("", draft.role)
    else:
        return None
    return (church, draft.region.value, role)


def dedup_key(seat: Seat, department: Department | None, *, round_number: int) -> str:
    """`개복교회:JEONBUK:ASSOCIATE_PASTOR:-:R1` — 사람이 읽을 수 있게 둔다.

    ⚠️ 해시로 줄이지 않는다. "왜 이 둘이 합쳐졌나"에 **항상 답할 수 있어야** 한다(SPEC §4.1).
    앞 세 조각이 자물쇠라, 부서만 갈린 두 행은 `교회:지역:직분:` 앞자리로 함께 찾을 수 있다
    (`UNCERTAIN` 묶음이 그 모양이다).
    """
    church, region, role = seat
    slot = department.value if department is not None else NO_DEPARTMENT
    return f"{church}:{region}:{role}:{slot}:R{round_number}"


def _rounds(members: Sequence[DedupCandidate]) -> list[list[DedupCandidate]]:
    """게시일 순으로 놓고 3개월 넘게 벌어지는 곳에서 자른다."""
    ordered = sorted(members, key=_ordering)
    rounds: list[list[DedupCandidate]] = [[ordered[0]]]
    for candidate in ordered[1:]:
        previous = rounds[-1][-1]
        if months_before(candidate.posted_on, ROUND_MONTHS) <= previous.posted_on:
            rounds[-1].append(candidate)
        else:
            rounds.append([candidate])
    return rounds


def _ordering(candidate: DedupCandidate) -> tuple[date, str]:
    """시간순. 같은 날이면 id로 — **순서가 흔들리면 라운드 경계도 흔들린다**."""
    return (candidate.posted_on, str(candidate.draft.id))


def _judge(seat: Seat, number: int, members: Sequence[DedupCandidate]) -> list[DedupUpdate]:
    """한 라운드를 부서로 가른다(SPEC §4.1 3단계)."""
    by_department: dict[Department | None, list[DedupCandidate]] = defaultdict(list)
    for candidate in members:
        by_department[candidate.draft.department].append(candidate)

    named = sorted((name for name in by_department if name is not None), key=_department_order)
    silent = by_department.get(None, [])
    if len(named) > 1 and silent:
        # ⚠️ 명시된 부서가 **둘 이상**인데 말하지 않은 글이 있다 — 그 글이 어느 자리에 붙는지
        #    알 방법이 없다. 사람이 본다.
        return _hold_for_review(seat, number, members)
    if len(named) == 1 and silent:
        # ⚠️ **침묵은 "다른 부서"가 아니라 안 적은 것이다**(2026-08-19 실측으로 고쳤다). 같은
        #    공고가 여러 게시판에 올라오면 **모델이 게시판마다 부서를 다르게 뽑는다** — 2주치
        #    694건에서 부서가 섞인 13묶음이 **전부 접수 이메일이 같았고**(= 같은 자리) 부서 값이
        #    실제로 서로 다른 묶음은 2개뿐이었다. 섞였다는 것만으로 검수로 보내면 53건이 헛검수다.
        #    명시된 부서를 그 자리의 부서로 보고, 가르는 일은 접수 이메일에 맡긴다(4단계).
        return _judge_same_department(seat, number, named[0], members)

    updates: list[DedupUpdate] = []
    for department in sorted(by_department, key=_department_order):
        updates.extend(_judge_same_department(seat, number, department, by_department[department]))
    return updates


def _department_order(department: Department | None) -> str:
    return department.value if department is not None else ""


def _judge_same_department(
    seat: Seat, number: int, department: Department | None, members: Sequence[DedupCandidate]
) -> list[DedupUpdate]:
    """부서까지 같은 후보들 — 연락처가 뒤집지 않으면 같은 자리다(SPEC §4.1 4단계)."""
    key = dedup_key(seat, department, round_number=number)
    if len(members) == 1:
        return [_restore(members[0], key, DedupState.ALONE)]
    if _mailboxes_differ(members):
        # ⚠️ 접수 메일함이 다르면 **다른 자리일 수 있다** — 그런데 확정할 수는 없다(한 담당자가
        #    메일을 바꿔 올렸을 수도 있다). 자동으로 가르면 중복이 남고 자동으로 합치면 자리가
        #    사라지므로 **사람이 정한다**(운영자 결정 2026-08-19).
        return _hold_for_review(seat, number, members)
    if _anchors(members) > 1:
        # ⚠️ 사람이 이미 본 행이 둘 이상이면 정리도 사람이 한다 — 어느 쪽을 내릴지 우리가
        #    고를 수 없고(둘 다 승인·게재됐을 수 있다) 판정을 쓸 권한도 없다.
        return _hold_for_review(seat, number, members)
    return _merge(members, key)


def _merge(members: Sequence[DedupCandidate], key: str) -> list[DedupUpdate]:
    """같은 자리 확정 — 대표 하나만 남기고 나머지는 거절한다."""
    master = max(members, key=_master_priority)
    newest = max(candidate.posted_on for candidate in members)
    updates = [
        _apply(
            master,
            key,
            DedupState.MASTER,
            review_status=review_status_for(master.draft.confidence, None),
            reject_reason=None,
            posted_at=newest,
        )
    ]
    updates.extend(
        _apply(
            candidate,
            key,
            DedupState.DUPLICATE,
            review_status=ReviewStatus.REJECTED,
            reject_reason=RejectReason.DUPLICATE,
            posted_at=candidate.draft.posted_at,
        )
        for candidate in members
        if candidate is not master
    )
    return updates


def _hold_for_review(
    seat: Seat, number: int, members: Sequence[DedupCandidate]
) -> list[DedupUpdate]:
    """판단 불가 — **대표는 그대로 내보내고 나머지만** 검수로 돌린다(운영자 결정 2026-08-17).

    같은 자리였다면 어차피 하나만 공개돼야 하니 결과가 맞고, 다른 자리였다면 운영자가 승인하면
    된다. 전원을 검수로 돌리면 같은 자리인 경우에도 두 건을 보게 된다.
    """
    master = max(members, key=_master_priority)
    updates: list[DedupUpdate] = []
    for candidate in members:
        key = dedup_key(seat, candidate.draft.department, round_number=number)
        if candidate is master:
            updates.append(_restore(candidate, key, DedupState.UNCERTAIN))
            continue
        updates.append(
            _apply(
                candidate,
                key,
                DedupState.UNCERTAIN,
                review_status=ReviewStatus.PENDING,
                reject_reason=None,
                posted_at=candidate.draft.posted_at,
            )
        )
    return updates


def _restore(candidate: DedupCandidate, key: str, state: DedupState) -> DedupUpdate:
    """거절이 아닌 상태로 되돌린다 — **지난 실행이 잘못 거절한 행이 되살아나야 한다.**

    ⚠️ 라벨만 붙이면 지난 실행의 `DUPLICATE` 거절이 그대로 남는다. 규칙을 고쳐 다시 돌렸는데
    그 행이 여전히 안 보이면 dedup을 되돌릴 방법이 없어진다(실측: 테스트가 이걸 잡았다).
    상태는 등급이 정한다 — `confidence`를 건드리지 않으므로 처음 저장될 때와 같은 값이 나온다.
    """
    return _apply(
        candidate,
        key,
        state,
        review_status=review_status_for(candidate.draft.confidence, None),
        reject_reason=None,
        posted_at=candidate.draft.posted_at,
    )


def _label(candidate: DedupCandidate, key: str, state: DedupState) -> DedupUpdate:
    """라벨만. **운영자가 손댄 행에만** 쓴다 — 사람이 한 일을 크롤러가 덮지 않는다."""
    return DedupUpdate(review_data_id=candidate.draft.id, dedup_key=key, dedup_state=state)


def _apply(
    candidate: DedupCandidate,
    key: str,
    state: DedupState,
    *,
    review_status: ReviewStatus,
    reject_reason: RejectReason | None,
    posted_at: date,
) -> DedupUpdate:
    """라벨 + 판정. ⚠️ **운영자가 손댔거나 이미 공개된 행에는 판정을 쓰지 않는다.**"""
    if candidate.draft.is_operator_owned:
        return _label(candidate, key, state)
    return DedupUpdate(
        review_data_id=candidate.draft.id,
        dedup_key=key,
        dedup_state=state,
        verdict=DedupVerdict(
            review_status=review_status, reject_reason=reject_reason, posted_at=posted_at
        ),
    )


def _anchors(members: Iterable[DedupCandidate]) -> int:
    return sum(1 for candidate in members if candidate.draft.is_operator_owned)


def _master_priority(candidate: DedupCandidate) -> tuple[bool, bool, int, date, str]:
    """대표 순위: **사람이 확인한 것** > 자동 승인된 것 > 빈 칸 적은 것 > 최신 > id.

    ⚠️ "가장 충실한 것"이 최신보다 앞이다(운영자 결정 2026-08-17). 교차게시는 같은 날 여러
    게시판에 올라와 날짜가 자주 동점이고, 포스터 공고가 대표가 되면 사람이 볼 건수가 늘어난다.
    ⚠️ 마지막이 id인 이유: 여기까지 동점이면 **무엇이든 고정된 순서**여야 한다(멱등).
    """
    draft = candidate.draft
    return (
        draft.is_operator_owned,
        draft.confidence is Confidence.HIGH,
        _completeness(draft),
        candidate.posted_on,
        str(draft.id),
    )


def _completeness(draft: ReviewData) -> int:
    """채워진 칸 수. 어느 칸을 셀지 손으로 적지 않는다 — 칸이 늘면 자동으로 따라온다."""
    return sum(
        1 for info in fields(draft) if info.name not in _NOT_CONTENT and getattr(draft, info.name)
    )


def _mailboxes_differ(members: Sequence[DedupCandidate]) -> bool:
    """**접수 메일함이 서로 다른가** — 그때만 다른 자리일 수 있다.

    ⚠️ **전화·링크·우편은 보지 않는다**(운영자 결정 2026-08-19 · 실측으로 고쳤다). 자물쇠에서
    이미 **같은 교회**임을 확인했으므로, 교회 대표번호·홈페이지·교회 주소가 겹친다는 것은
    "같은 교회"라는 뜻일 뿐 **자리를 가르는 정보가 없다**. 반면 접수 이메일은 담당자별로 달라
    자리를 가른다(실측: 같은 교회의 두 자리가 `yoon4970` / `klmchwang93`으로 갈렸다).

    그 셋을 어긋남으로 세면 **같은 자리가 갈라진다** — 실측 11묶음 중 10개가 그렇게 갈렸다:
    전화 8묶음(게시판마다 대표번호·담당자 휴대폰을 달리 적는다 · 광진교회 6건이 6자리로),
    링크 2묶음(`www.semmul.org`/`http://www.semmul.or` 오타 · 링크 칸에 교회 이름이 들어옴).

    ⚠️ 반대로 전화가 겹치는 것을 "같은 자리" 근거로 쓰면 더 위험하다 — 대표번호 하나를 두 자리에
    다 적고 담당자 이메일만 다른 교회에서 **한 자리가 사라진다.**

    ⚠️ **한쪽만 적은 것도, 한쪽이 더 많이 적은 것도 근거가 아니다**(실측: 접수용·문의용 이메일을
    함께 적는다). 겹치는 조각이 하나라도 있으면 같은 곳으로 본다.
    """
    return _tokens_clash([_mails(candidate.draft) for candidate in members])


def _tokens_clash(values: Sequence[frozenset[str]]) -> bool:
    """양쪽 다 적었는데 **겹치는 조각이 하나도 없으면** 다른 곳이다."""
    known = [value for value in values if value]
    return any(not (left & right) for left, right in combinations(known, 2))


def _mails(draft: ReviewData) -> frozenset[str]:
    return _tokens(draft.contact_email, _MAIL_TOKEN)


def _tokens(value: str | None, pattern: re.Pattern[str]) -> frozenset[str]:
    """조각으로 쪼갠 값. 꼴이 안 맞으면 **통째로 하나의 조각**으로 둔다 — 못 쪼갠 값을 버리면
    비교할 것이 없어져 그 채널이 조용히 무력해진다."""
    text = (value or "").strip().lower()
    if not text:
        return frozenset()
    found = pattern.findall(text)
    return frozenset(item.rstrip("/.,;") for item in found) or frozenset({text.rstrip("/")})
