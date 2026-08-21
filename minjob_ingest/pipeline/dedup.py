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
from typing import Final, Protocol

from minjob_ingest.clock import months_before, today_kst
from minjob_ingest.domain import (
    Confidence,
    DedupState,
    Department,
    Position,
    Region,
    RejectReason,
    ReviewStatus,
)
from minjob_ingest.models import REVIEW_STATE_FIELDS, ReviewData
from minjob_ingest.pipeline.confidence import review_status_for
from minjob_ingest.store.base import (
    DedupCandidate,
    DedupUpdate,
    DedupVerdict,
    JobAnchor,
    PublishTarget,
    Store,
)

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
class _Member:
    """판정에 참가하는 한 자리 — **우리 초안**이거나 **이미 공개된 앵커**다(SPEC §4.2).

    사슬(라운드·부서·메일함·대표 순위)이 초안을 직접 들여다보지 않게 필요한 것만 꺼내 둔다.
    그래서 `jobs` 행도 같은 규칙을 그대로 지나간다 — 앵커용 판정을 따로 만들면 두 계산이
    갈라지고, 갈라진 순간 **이미 공개된 자리를 못 알아봐 같은 자리가 두 번 공개된다.**

    ⚠️ **앵커는 `draft`가 `None`이다** — `jobs` 행이라 `review_data`에 쓸 칸이 없다. 묶임과
    대표 선정에만 참여하고 판정은 나가지 않는다.
    """

    #: 라운드 경계의 기준. 초안은 원문 게시일(불변), 앵커는 `jobs.posted_at`.
    posted_on: date
    department: Department | None
    #: 접수 이메일 조각. 자리를 가르는 유일한 연락처다(SPEC §4.1 4단계).
    mailboxes: frozenset[str]
    #: 크롤러가 판정을 덮어선 안 되는 자리인가 — **대표 순위 1번**.
    #: 초안은 `ReviewData.is_operator_owned`, **앵커는 항상 True**(이미 공개돼 있다).
    is_owned: bool
    #: 채워진 칸 수. ⚠️ 앵커는 위 순위에서 이미 이기므로 이 값이 판정을 바꾸지 않는다.
    completeness: int
    #: 동점일 때의 **고정된** 순서. 흔들리면 라운드 경계도 흔들린다.
    identity: str
    #: 판정을 쓸 대상. **앵커면 `None`.**
    draft: ReviewData | None = None


def _member_of(candidate: DedupCandidate) -> _Member:
    draft = candidate.draft
    return _Member(
        posted_on=candidate.posted_on,
        department=draft.department,
        mailboxes=_tokens(draft.contact_email, _MAIL_TOKEN),
        is_owned=draft.is_operator_owned,
        completeness=_completeness(draft),
        identity=str(draft.id),
        draft=draft,
    )


def _member_of_anchor(anchor: JobAnchor) -> _Member:
    """이미 공개된 `jobs` 행. **항상 대표**다 — 새 공고가 공개된 자리를 밀어내지 않는다."""
    return _Member(
        posted_on=anchor.posted_at,
        department=anchor.department,
        mailboxes=_tokens(anchor.contact_email, _MAIL_TOKEN),
        is_owned=True,
        completeness=0,
        identity=str(anchor.job_id),
    )


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
    #: `jobs` 전체 행 수. ⚠️ **앵커 수만 찍으면 이상함이 드러나지 않는다** — 0건은 정상일
    #: 수도 있다(전부 마감). `1,204행 중 0건`이라야 노출 규칙이 어긋났음을 사람이 알아본다.
    jobs_rows: int = 0
    #: 그중 지금 목록에 보여 판정에 참가한 것(SPEC §4.2).
    anchors: int = 0

    def count(self, state: DedupState) -> int:
        return self.states.get(state.value, 0)


def dedup_all(store: Store, jobs: PublishTarget | None, *, dry_run: bool) -> DedupReport:
    """전체를 훑어 중복을 판정하고 저장한다.

    ⚠️ **배치로 쪼개지 않는다**(SPEC §4.1) — 한 글만 보고는 대표를 고를 수 없다. 3,188건이
    메모리에 다 들어오고 유료 호출도 없다.

    `jobs`를 주면 **이미 공개된 자리(앵커)** 도 함께 본다(SPEC §4.2).

    ⚠️ **`jobs`에 기본값을 두지 않는다.** 기본값이 있으면 호출부 하나가 잊고, 그러면 같은
    데이터에 답이 둘이 된다 — 앵커 없이 판정한 쪽이 이미 공개된 자리를 `ALONE`으로 만들고
    공개 패스가 그것을 또 올린다(2026-08-21에 실제로 `structure`의 자동 dedup이 그 상태였다).
    `jobs`가 없는 저장소(로컬 파일)에서는 **호출부가 `None`을 명시**한다.
    """
    candidates = store.dedup_candidates()
    published = frozenset(
        candidate.draft.published_job_id
        for candidate in candidates
        if candidate.draft.published_job_id is not None
    )
    # ⚠️ 우리가 공개한 행은 앵커로 읽지 않는다 — 후보에 이미 초안으로 들어와 있어서
    #    자기 자신과 중복 판정하게 된다(SPEC §4.2).
    anchors = () if jobs is None else jobs.visible_anchors(today=today_kst(), exclude=published)
    updates = plan(candidates, anchors=anchors)
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
        jobs_rows=0 if jobs is None else jobs.count_jobs(),
        anchors=len(anchors),
    )


def normalize_church_name(name: str | None) -> str | None:
    """교회명을 견줄 꼴로. **정규화는 이 칸 하나뿐이다** — 나머지 셋은 enum이라 안 흔들린다."""
    if name is None:
        return None
    return _NOISE.sub("", _BRACKETS.sub("", name)) or None


def plan(
    candidates: Sequence[DedupCandidate], *, anchors: Sequence[JobAnchor] = ()
) -> tuple[DedupUpdate, ...]:
    """중복 판정. **순수 함수** — 같은 입력이면 항상 같은 결과다(멱등).

    돌려주지 않은 행은 **판정하지 않았다는 뜻**이다(자물쇠가 비었거나 이미 결론이 난 행).

    `anchors`는 **이미 공개돼 지금 목록에 보이는 `jobs` 행**이다(SPEC §4.2). 후보와 같은
    사슬을 지나 **항상 대표**가 되고, 판정은 받지 않는다 — 그래서 그 자리의 새 공고가
    중복으로 걸러진다. ⚠️ 우리가 공개한 행은 여기 오지 않는다(이미 초안으로 들어와 있다) —
    저장소가 `published_job_id`로 빼고 넘긴다.
    """
    seats: dict[Seat, list[_Member]] = defaultdict(list)
    for candidate in candidates:
        if candidate.draft.reject_reason in _SETTLED_REASONS:
            continue
        seat = seat_of(candidate.draft)
        if seat is None:
            continue
        seats[seat].append(_member_of(candidate))
    for anchor in anchors:
        seat = seat_of(anchor)
        if seat is None:
            # ⚠️ 자물쇠가 비면 앵커가 되지 못한다 — 중복이 남는 쪽이 안전하다(`seat_of`).
            continue
        seats[seat].append(_member_of_anchor(anchor))

    updates: list[DedupUpdate] = []
    for seat in sorted(seats):
        for number, members in enumerate(_rounds(seats[seat]), start=1):
            updates.extend(_judge(seat, number, members))
    return tuple(updates)


class SeatSource(Protocol):
    """자물쇠 셋을 만들 수 있는 것 — `ReviewData`와 `JobAnchor`가 **둘 다** 만족한다.

    ⚠️ **앵커도 같은 키 함수를 지나야 한다**(SPEC §4.2 "§4.1과 같은 키"). 타입을 `ReviewData`로
    좁혀 두면 앵커용 키를 따로 만들게 되고, 두 계산이 갈라진 순간 **이미 공개된 자리를 못
    알아봐 같은 자리가 두 번 공개된다**. 그래서 구조적 프로토콜로 받는다.
    """

    @property
    def church_name(self) -> str | None: ...
    @property
    def region(self) -> Region | None: ...
    @property
    def position(self) -> tuple[Position, ...]: ...
    @property
    def role(self) -> str | None: ...


def seat_of(draft: SeatSource) -> Seat | None:
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


def _rounds(members: Sequence[_Member]) -> list[list[_Member]]:
    """게시일 순으로 놓고 3개월 넘게 벌어지는 곳에서 자른다."""
    ordered = sorted(members, key=_ordering)
    rounds: list[list[_Member]] = [[ordered[0]]]
    for member in ordered[1:]:
        previous = rounds[-1][-1]
        if months_before(member.posted_on, ROUND_MONTHS) <= previous.posted_on:
            rounds[-1].append(member)
        else:
            rounds.append([member])
    return rounds


def _ordering(member: _Member) -> tuple[date, str]:
    """시간순. 같은 날이면 식별자로 — **순서가 흔들리면 라운드 경계도 흔들린다**."""
    return (member.posted_on, member.identity)


def _judge(seat: Seat, number: int, members: Sequence[_Member]) -> list[DedupUpdate]:
    """한 라운드를 부서로 가른다(SPEC §4.1 3단계)."""
    by_department: dict[Department | None, list[_Member]] = defaultdict(list)
    for member in members:
        by_department[member.department].append(member)

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
        return _judge_one_seat(seat, number, named[0], members)

    updates: list[DedupUpdate] = []
    for department in sorted(by_department, key=_department_order):
        updates.extend(_judge_one_seat(seat, number, department, by_department[department]))
    return updates


def _department_order(department: Department | None) -> str:
    return department.value if department is not None else ""


def _judge_one_seat(
    seat: Seat, number: int, department: Department | None, members: Sequence[_Member]
) -> list[DedupUpdate]:
    """한 자리로 볼 후보들 — **접수 이메일이 뒤집지 않으면** 같은 자리다(SPEC §4.1 4단계).

    ⚠️ `department`는 이 자리의 부서이고, 부서를 **말하지 않은 글도 함께 받는다**(3단계에서
    침묵을 그 부서로 읽는다) — 그래서 "부서가 같은 것끼리"가 아니라 "한 자리"다.
    """
    key = dedup_key(seat, department, round_number=number)
    if len(members) == 1:
        # ⚠️ 앵커 혼자면 아무것도 쓰지 않는다(`_restore`가 `None`) — `jobs` 행이라 쓸 칸이 없다.
        alone = _restore(members[0], key, DedupState.ALONE)
        return [] if alone is None else [alone]
    if _mailboxes_differ(members):
        # ⚠️ 접수 메일함이 다르면 **다른 자리일 수 있다** — 그런데 확정할 수는 없다(한 담당자가
        #    메일을 바꿔 올렸을 수도 있다). 자동으로 가르면 중복이 남고 자동으로 합치면 자리가
        #    사라지므로 **사람이 정한다**(운영자 결정 2026-08-19).
        return _hold_for_review(seat, number, members)
    if _owned(members) > 1:
        # ⚠️ 사람이 이미 본 자리가 둘 이상이면 정리도 사람이 한다 — 어느 쪽을 내릴지 우리가
        #    고를 수 없고(둘 다 승인·게재됐을 수 있다) 판정을 쓸 권한도 없다.
        return _hold_for_review(seat, number, members)
    return _merge(members, key)


def _merge(members: Sequence[_Member], key: str) -> list[DedupUpdate]:
    """같은 자리 확정 — 대표 하나만 남기고 나머지는 거절한다.

    ⚠️ 대표가 **앵커**면 대표 몫 판정이 없다(`jobs` 행이라 쓸 칸이 없다) — 나머지가 전부
    거절되고, 그게 SPEC §4.2가 말하는 "이미 공개된 자리를 새 공고가 밀어내지 않는다"다.
    """
    master = max(members, key=_master_priority)
    newest = max(member.posted_on for member in members)
    planned = [
        _apply(
            master,
            key,
            DedupState.MASTER,
            review_status=_status_of(master),
            reject_reason=None,
            posted_at=newest,
        )
    ]
    planned.extend(
        _apply(
            member,
            key,
            DedupState.DUPLICATE,
            review_status=ReviewStatus.REJECTED,
            reject_reason=RejectReason.DUPLICATE,
            posted_at=_posted_at_of(member),
        )
        for member in members
        if member is not master
    )
    return [update for update in planned if update is not None]


def _hold_for_review(seat: Seat, number: int, members: Sequence[_Member]) -> list[DedupUpdate]:
    """판단 불가 — **대표는 그대로 내보내고 나머지만** 검수로 돌린다(운영자 결정 2026-08-17).

    같은 자리였다면 어차피 하나만 공개돼야 하니 결과가 맞고, 다른 자리였다면 운영자가 승인하면
    된다. 전원을 검수로 돌리면 같은 자리인 경우에도 두 건을 보게 된다.

    여기로 오는 길은 셋이다: 부서가 **여러 값으로 갈렸는데 침묵도 섞였다**(3단계) · **접수
    메일함이 다르다**(4단계) · **사람이 이미 본 행이 둘 이상**이다(정리도 사람이 한다).
    """
    master = max(members, key=_master_priority)
    planned: list[DedupUpdate | None] = []
    for member in members:
        key = dedup_key(seat, member.department, round_number=number)
        if member is master:
            planned.append(_restore(member, key, DedupState.UNCERTAIN))
            continue
        planned.append(
            _apply(
                member,
                key,
                DedupState.UNCERTAIN,
                review_status=ReviewStatus.PENDING,
                reject_reason=None,
                posted_at=_posted_at_of(member),
            )
        )
    return [update for update in planned if update is not None]


def _restore(member: _Member, key: str, state: DedupState) -> DedupUpdate | None:
    """거절이 아닌 상태로 되돌린다 — **지난 실행이 잘못 거절한 행이 되살아나야 한다.**

    ⚠️ 라벨만 붙이면 지난 실행의 `DUPLICATE` 거절이 그대로 남는다. 규칙을 고쳐 다시 돌렸는데
    그 행이 여전히 안 보이면 dedup을 되돌릴 방법이 없어진다(실측: 테스트가 이걸 잡았다).
    상태는 등급이 정한다 — `confidence`를 건드리지 않으므로 처음 저장될 때와 같은 값이 나온다.
    """
    return _apply(
        member,
        key,
        state,
        review_status=_status_of(member),
        reject_reason=None,
        posted_at=_posted_at_of(member),
    )


def _apply(
    member: _Member,
    key: str,
    state: DedupState,
    *,
    review_status: ReviewStatus,
    reject_reason: RejectReason | None,
    posted_at: date,
) -> DedupUpdate | None:
    """라벨 + 판정.

    ⚠️ **앵커에는 아무것도 쓰지 않는다**(`None`) — `jobs` 행이라 `review_data`에 쓸 칸이 없다.
    ⚠️ **운영자가 손댔거나 이미 공개된 초안에는 라벨만** 쓴다 — 사람이 한 일을 크롤러가 덮지
    않는다. 그래도 라벨은 붙여야 SPEC §4.2가 "이미 공개된 같은 자리"를 찾을 수 있다.
    그 예외(우리가 내린 중복 거절을 되돌릴 때)는 `allows_dedup_verdict`가 정한다.
    """
    draft = member.draft
    if draft is None:
        return None
    if not draft.allows_dedup_verdict(state):
        return DedupUpdate(review_data_id=draft.id, dedup_key=key, dedup_state=state)
    return DedupUpdate(
        review_data_id=draft.id,
        dedup_key=key,
        dedup_state=state,
        verdict=DedupVerdict(
            review_status=review_status,
            reject_reason=reject_reason,
            posted_at=posted_at,
        ),
    )


def _owned(members: Iterable[_Member]) -> int:
    """크롤러가 판정을 덮어선 안 되는 자리 수 — 앵커와 사람이 손댄 초안.

    ⚠️ 이름을 `_anchors`에서 바꿨다(2026-08-21). "앵커"는 이제 `jobs` 행을 뜻하므로 같은
    낱말이 두 뜻을 갖게 됐다 — 읽는 사람이 매번 확인해야 하는 이름을 두지 않는다.
    """
    return sum(1 for member in members if member.is_owned)


def _status_of(member: _Member) -> ReviewStatus:
    """등급이 정하는 상태. 앵커는 판정이 나가지 않으므로 값이 쓰이지 않는다."""
    draft = member.draft
    return ReviewStatus.PENDING if draft is None else review_status_for(draft.confidence, None)


def _posted_at_of(member: _Member) -> date:
    """제 몫 게시일. 앵커는 판정이 나가지 않으므로 값이 쓰이지 않는다."""
    draft = member.draft
    return member.posted_on if draft is None else draft.posted_at


def _master_priority(member: _Member) -> tuple[bool, bool, int, date, str]:
    """대표 순위: **사람이 확인한 것** > 자동 승인된 것 > 빈 칸 적은 것 > 최신 > id.

    ⚠️ "가장 충실한 것"이 최신보다 앞이다(운영자 결정 2026-08-17). 교차게시는 같은 날 여러
    게시판에 올라와 날짜가 자주 동점이고, 포스터 공고가 대표가 되면 사람이 볼 건수가 늘어난다.
    ⚠️ 마지막이 식별자인 이유: 여기까지 동점이면 **무엇이든 고정된 순서**여야 한다(멱등).
    ⚠️ **앵커는 첫 기준에서 이긴다** — 이미 공개된 자리를 새 공고가 밀어내지 않는다(SPEC §4.2).
    """
    draft = member.draft
    return (
        member.is_owned,
        draft is not None and draft.confidence is Confidence.HIGH,
        member.completeness,
        member.posted_on,
        member.identity,
    )


def _completeness(draft: ReviewData) -> int:
    """채워진 칸 수. 어느 칸을 셀지 손으로 적지 않는다 — 칸이 늘면 자동으로 따라온다."""
    return sum(
        1 for info in fields(draft) if info.name not in _NOT_CONTENT and getattr(draft, info.name)
    )


def _mailboxes_differ(members: Sequence[_Member]) -> bool:
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
    return _tokens_clash([member.mailboxes for member in members])


def _tokens_clash(values: Sequence[frozenset[str]]) -> bool:
    """양쪽 다 적었는데 **겹치는 조각이 하나도 없으면** 다른 곳이다."""
    known = [value for value in values if value]
    return any(not (left & right) for left, right in combinations(known, 2))


def _tokens(value: str | None, pattern: re.Pattern[str]) -> frozenset[str]:
    """조각으로 쪼갠 값. 꼴이 안 맞으면 **통째로 하나의 조각**으로 둔다 — 못 쪼갠 값을 버리면
    비교할 것이 없어져 그 채널이 조용히 무력해진다."""
    text = (value or "").strip().lower()
    if not text:
        return frozenset()
    found = pattern.findall(text)
    return frozenset(item.rstrip("/.,;") for item in found) or frozenset({text.rstrip("/")})
