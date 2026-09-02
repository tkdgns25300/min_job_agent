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
from minjob_ingest.pipeline.normalize import NAME_BRACKETS, NAME_NOISE
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

#: 부서를 말하지 않은 공고의 키 조각. **값이 없다는 사실 자체가 값**이다(위 docstring).
NO_DEPARTMENT: Final = "-"

#: 일반직은 직분이 없고 직무가 있다. 자유 텍스트라 공백·기호를 떼서 쓴다.
_ROLE_PREFIX: Final = "ROLE:"

#: 직분이 여럿일 때 열쇠 안에서 잇는 글자(`ASSOCIATE_PASTOR+EVANGELIST`).
_POSITION_JOINER: Final = "+"

#: 직분을 **말하지 않은** 열쇠 조각 — `ETC`(기타) 하나뿐이다. 침묵은 "다른 직분"이 아니라 안 적은
#: 것이다(`_is_role_variant`). ⚠️ 직무 글자(`ROLE:…`)는 침묵이 아니다 — 일반직이라는 뜻이다.
_SILENT_ROLE: Final = frozenset({Position.ETC.value})

#: 한 칸에 **여러 곳이 들어간다** — 연락처는 조립 칸이라 원문에 둘이 적혀 있으면 둘 다 담긴다
#: (실측 `apply@x.org, office@x.org`). 그래서 조각으로 쪼개 **겹치는지**를 본다.
_MAIL_TOKEN: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: 이름 변형을 견줄 때 떼는 꼬리. `한밭제일교회`/`한밭제일장로교회`처럼 **가운데에 끼어드는**
#: 말이 있어 접미를 떼야 포함 관계가 보인다(`한밭제일` ⊂ `한밭제일장로`).
_CHURCH_TAIL: Final = re.compile(r"교회$")

#: 주소 판정에 필요한 **아는 주소**의 최소 개수. 하나로는 견줄 상대가 없다.
_PLACES_TO_JUDGE: Final = 2

#: 주소를 견줄 때 지우는 것 — 공백·구두점. 게시판마다 띄어쓰기가 다르다.
_PLACE_NOISE: Final = re.compile(r"[\s,.()\[\]-]+")

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
    #: 마감일. 지났으면 자리 다툼에서 빠지고(`_still_open`), 앵커는 보이는 행이라 늘 `None`이다.
    deadline: date | None
    department: Department | None
    #: 접수 이메일 조각. 자리를 가르는 유일한 연락처다(SPEC §4.1 5단계).
    mailboxes: frozenset[str]
    #: 교회가 어디인가 — (도시, 주소)를 견줄 꼴로. **메일함이 갈렸을 때만** 쓴다(§4.1 5b단계).
    #: 앵커(`jobs`)는 주소를 읽지 않아 비어 있고, 그때는 판정하지 않는다.
    place: tuple[str, str]
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
        deadline=draft.deadline,
        department=draft.department,
        mailboxes=_tokens(draft.contact_email, _MAIL_TOKEN),
        place=_place(draft.city, draft.address),
        is_owned=draft.is_operator_owned,
        completeness=_completeness(draft),
        identity=str(draft.id),
        draft=draft,
    )


def _member_of_anchor(anchor: JobAnchor) -> _Member:
    """이미 공개된 `jobs` 행. **항상 대표**다 — 새 공고가 공개된 자리를 밀어내지 않는다."""
    return _Member(
        posted_on=anchor.posted_at,
        deadline=None,  # `visible_anchors`가 이미 보이는 행만 준다 — 마감 지난 앵커는 오지 않는다
        department=anchor.department,
        mailboxes=_tokens(anchor.contact_email, _MAIL_TOKEN),
        place=("", ""),  # `jobs`에서 주소를 읽지 않는다(§8: 앵커로만 본다)
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


@dataclass(frozen=True, slots=True)
class DedupPlan:
    """`plan`의 결과 — 쓸 것과, 쓰지 않고 빠진 것."""

    updates: tuple[DedupUpdate, ...]
    #: 마감이 지난 자리라 다툼에서 빠진 초안의 id(`_still_open`). 판정이 아니라 **빠짐**이다 —
    #: 아무것도 쓰지 않는다. 리포트가 이것을 "이미 결론"에 세지 않으면 "견줄 수 없음"이 부푼다.
    expired: frozenset[str]


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
    today = today_kst()
    anchors = () if jobs is None else jobs.visible_anchors(today=today, exclude=published)
    planned = plan(candidates, today=today, anchors=anchors)
    updates = planned.updates
    judged = {update.review_data_id for update in updates}
    skipped = [candidate.draft for candidate in candidates if candidate.draft.id not in judged]
    # 다툼에서 빠진 행은 "이미 결론"이다 — 사람 몫(unjudged)에 세면 삭제·마감이 쌓일수록
    # 리포트의 "견줄 수 없음"이 부풀어 검수자가 헛짚는다.
    settled = sum(1 for draft in skipped if _sits_out(draft) or str(draft.id) in planned.expired)
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
    return NAME_NOISE.sub("", NAME_BRACKETS.sub("", name)) or None


def plan(
    candidates: Sequence[DedupCandidate], *, today: date, anchors: Sequence[JobAnchor] = ()
) -> DedupPlan:
    """중복 판정. **순수 함수** — 같은 입력이면 항상 같은 결과다(멱등).

    `updates`에 없는 행은 **판정하지 않았다는 뜻**이다 — 자물쇠가 비었거나, 결론이 난 행이거나
    (`_sits_out`), 마감이 지난 자리에서 빠졌다(`_still_open` · `expired`에 적힌다). `today`는
    마감을 볼 기준일이다 — 벽시계를 안에서 읽지 않는다.

    `anchors`는 **이미 공개돼 지금 목록에 보이는 `jobs` 행**이다(SPEC §4.2). 후보와 같은
    사슬을 지나 **항상 대표**가 되고, 판정은 받지 않는다 — 그래서 그 자리의 새 공고가
    중복으로 걸러진다. ⚠️ 우리가 공개한 행은 여기 오지 않는다(이미 초안으로 들어와 있다) —
    저장소가 `published_job_id`로 빼고 넘긴다.
    """
    seats: dict[Seat, list[_Member]] = defaultdict(list)
    for candidate in candidates:
        if _sits_out(candidate.draft):
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

    merged = _merge_key_variants(seats)
    updates: list[DedupUpdate] = []
    expired: set[str] = set()
    for seat in sorted(merged):
        alive, closed = _still_open(merged[seat], today=today)
        expired.update(member.identity for member in closed)
        if not alive:
            continue
        for number, members in enumerate(_rounds(alive), start=1):
            updates.extend(_judge(seat, number, members))
    return DedupPlan(updates=tuple(updates), expired=frozenset(expired))


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


def _sits_out(draft: ReviewData) -> bool:
    """자리 다툼에 참가하지 않는 행 — 결론이 났거나(이단·마감·운영자 거절) 원문이 사라졌다(§4.4).

    둘은 같은 이유다: **이 행이 대표로 서면 살아 있는 같은 자리 공고가 그 밑에 묻힌다.** 원문
    소멸은 실측 2026-08-30 삭제 35건 중 27건이 다른 게시판에 살아있는 같은 자리를 갖고 있었다 —
    빠지면 그쪽이 대표를 물려받아 다음 공개에 나간다.

    ⚠️ **마감이 지난 글은 여기서 빼지 않는다** — 형제를 봐야 한다(`_still_open`).
    """
    return draft.reject_reason in _SETTLED_REASONS or draft.source_gone_at is not None


def _still_open(members: Sequence[_Member], *, today: date) -> tuple[list[_Member], list[_Member]]:
    """마감이 지난 자리에서 **살아 있는 글만** 남긴다(2026-09-02) — (산 것, 빠진 것).

    위 거절(`_sits_out`)은 **구조화 때** 이미 마감이었던 글(§5.4b)만 잡는다. 공개된 뒤 마감이
    지난 대표는 `APPROVED`인 채 남아 **여전히 이기고**, 교회가 마감을 새로 달아 다시 올린 글은
    새 글이라 §5.4b에 걸리지 않고 그 밑에 `DUPLICATE`로 묻힌다 — 실측 2026-09-02: 대표는 마감으로
    CLOSED, 재공고는 묻혀 **교회는 뽑는데 min_job엔 안 보이는** 자리가 3곳. 원문 소멸과 같은 길을
    연다: 빠지면 살아 있는 재공고가 자리를 물려받아 등급대로 나간다.

    ⚠️⚠️ **마감 뒤에 올라온 글만 재공고다.** 처음엔 "제 마감이 지난 글만 빠진다"로 했다가 실데이터
    에서 바로 고쳤다(2026-09-02): 형제가 있는 10자리 중 **6자리의 형제가 마감 전에 올라온 글**이었다
    — 같은 청빙을 다른 게시판에 마감 없이 올린 교차게시다. 그것을 대표로 세우면 **닫힌 청빙이
    되살아난다.** 그래서 마감 없는 글은 **가장 늦은 마감보다 뒤에 올라왔을 때만** 산다.
    ⚠️ 제 마감이 아직 안 지난 글은 게시일과 상관없이 산다 — 교회가 살아 있다고 말한 글이다.
    ⚠️ 앵커(`jobs` 행)는 늘 산다 — `visible_anchors`가 보이는 행만 주고, 빠뜨리면 그 자리의
    재공고가 앵커 옆에 또 공개된다.
    ⚠️ 경계는 `deadline < today`다 — 마감 당일은 산다(min_job 노출 규칙·`expired_job_ids`와 같다).
    ⚠️ 거절도 내림도 아니다 — **견주는 자리에서 빠지기만** 한다. 살아 있는 글이 없으면 그 자리는
    아무것도 공개되지 않는다. 빠진 행의 라벨은 그대로 남는다(소멸 행과 같다).
    """
    closed_on = max(
        (deadline for member in members if (deadline := member.deadline) and deadline < today),
        default=None,
    )
    if closed_on is None:
        return list(members), []
    alive: list[_Member] = []
    closed: list[_Member] = []
    for member in members:
        if member.draft is None:
            alive.append(member)  # 앵커
        elif _is_expired(member, today):
            closed.append(member)  # 제 마감이 지났다
        elif member.deadline is not None:
            alive.append(member)  # 제 마감이 아직 남았다 — 교회가 살아 있다고 말했다
        elif member.posted_on <= closed_on:
            closed.append(member)  # 마감 없이 마감 전에 올라왔다 — 같은 공고의 교차게시
        else:
            alive.append(member)  # 마감 뒤에 올라왔다 — 재공고
    return alive, closed


def _is_expired(member: _Member, today: date) -> bool:
    return member.deadline is not None and member.deadline < today


def seat_of(draft: SeatSource) -> Seat | None:
    """자물쇠 셋. 하나라도 없으면 `None` — 그 공고는 아무와도 견주지 않는다.

    ⚠️ 근거가 없을 때 묶지 않는 쪽이 안전하다: 중복이 남는 것은 되돌릴 수 있지만, **다른
    교회를 합치는 것은 되돌릴 수 없다**(교회명 894종 중 70종이 두 지역 이상에 있다).
    """
    church = normalize_church_name(draft.church_name)
    if church is None or draft.region is None:
        return None
    if draft.position:
        role = _POSITION_JOINER.join(member.value for member in draft.position)
    elif draft.role:
        role = _ROLE_PREFIX + NAME_NOISE.sub("", draft.role)
    else:
        return None
    return (church, draft.region.value, role)


def _merge_key_variants(seats: Mapping[Seat, list[_Member]]) -> dict[Seat, list[_Member]]:
    """열쇠 표기만 다른 자리를 하나로 합친다(SPEC §4.1 2단계).

    자물쇠 세 칸 중 지역은 enum이라 안 흔들리지만 **교회명과 직분은 원문 글자에서 나온다.**
    게시판마다 제목을 달리 쓰고(`남광교회`/`광주남광교회`), 직분은 문구에 따라 뽑히는 목록이
    달라진다(`중고등부 파트` → 기타 · `파트 전도사, 강도사, 부목사` → 셋 — 모델은 둘 다 맞았고
    교회가 문구를 고쳤다). 그러면 같은 청빙이 다른 자리로 갈려 **둘 다 공개된다** — 실측
    2026-09-02: 이름 갈림 18곳 · 직분 갈림 65곳이 min_job에 두 번씩 떠 있었다.

    합치는 조건은 **"다르되 충돌하지 않고, 같은 교회라는 근거가 있다"** 다:
    - 지역이 같다
    - 교회명이 같거나 표기 변형이다(`_is_name_variant`)
    - 직분이 같거나 표기 변형이다(`_is_role_variant` — 한쪽이 `기타`거나 덜 적었다 · 일반직은 제외)
    - **접수 메일함이 겹친다** — 유일한 "같다"는 근거. 이것 없이 표기만 보고 합치면 `중앙교회`와
      `광주중앙교회`가, `전도사`와 `전도사+부목사`를 뽑는 두 자리가 붙는다 — **다른 자리를
      합치는 것**이고 되돌릴 수 없다(`seat_of`와 같은 판단: 중복이 남는 쪽이 안전하다)
    - **주소가 어긋나지 않는다** — 거부권. 짧은 이름 쪽에는 동명이교회가 섞여 있을 수 있다
      (`예수로교회` 한 자리에 남양주와 성남이 함께 들어 있었다 · 2026-09-02) — 메일 하나가
      겹치는 것만으로 통째로 합치면 다른 교회가 끌려 들어오고, 부서가 섞여 §4.1 5b(주소)를
      볼 차례가 오지 않는다. 주소를 모르면(해외 교회) 막지 않는다 — 메일함이 근거다

    ⚠️ **`seat_of`(키 계산)는 건드리지 않는다.** 키를 바꾸면 이미 판정된 수천 건이 전부 다시
    판정된다(`normalize_church_name` 참조). 여기서는 **판정 직전에 묶음만** 합치고, 합친
    묶음은 심사(라운드·부서·이메일·주소·대표)를 그대로 지난다. 남는 키는 **정보가 많은 쪽**의
    것이다(`_specificity`).
    """
    by_region: dict[str, list[Seat]] = defaultdict(list)
    for seat in seats:
        _, region, _ = seat
        by_region[region].append(seat)

    winner: dict[Seat, Seat] = {}
    for peers in by_region.values():
        for one, other in combinations(sorted(peers), 2):
            if not _keys_agree(one, other):
                continue
            if not _share_a_mailbox(seats[one], seats[other]):
                continue
            if _same_place([*seats[one], *seats[other]]) is False:
                continue
            loser, keep = sorted((_resolve(winner, one), _resolve(winner, other)), key=_specificity)
            if loser != keep:
                winner[loser] = keep
    _unlink_bridged(winner, seats)

    merged: dict[Seat, list[_Member]] = defaultdict(list)
    for seat, members in seats.items():
        merged[_resolve(winner, seat)].extend(members)
    return dict(merged)


def _unlink_bridged(winner: dict[Seat, Seat], seats: Iterable[Seat]) -> None:
    """침묵이 **다리**가 되어 충돌하는 열쇠가 이어진 묶음은 통째로 풀어 준다.

    쌍마다 보면 `기타`~`부목사`도, `기타`~`전도사`도 변형이라 붙는데, 사슬을 따라가면 `부목사`와
    `전도사`가 한 자리가 된다 — 쌍으로는 절대 붙이지 않기로 한 조합이다(`_is_role_variant`).
    실측 2026-09-02: 합쳐진 42묶음 중 1곳(혜천교회 · 기타·부목사·전도사 셋). 어느 쪽에 `기타`를
    붙여야 할지 알 수 없으니 **묶음 전체를 합치지 않는다** — 근거 없으면 안 묶는 쪽이다.
    """
    components: dict[Seat, list[Seat]] = defaultdict(list)
    for seat in seats:
        components[_resolve(winner, seat)].append(seat)
    for peers in components.values():
        if not all(_keys_agree(one, other) for one, other in combinations(peers, 2)):
            for seat in peers:
                winner.pop(seat, None)


def _keys_agree(one: Seat, other: Seat) -> bool:
    """두 열쇠가 같은 자리의 다른 표기일 수 있나 — 칸마다 **같거나 변형**이어야 한다.

    지역은 같은 무리 안에서만 견주므로 여기서 보지 않는다. 두 칸이 동시에 달라도 된다(이름도
    직분도 변형) — 조건은 "칸마다 충돌이 없다"이고, 같은 교회라는 근거는 메일함이 따로 댄다.
    """
    (name, _, role), (other_name, _, other_role) = one, other
    names_agree = name == other_name or _is_name_variant(name, other_name)
    roles_agree = role == other_role or _is_role_variant(role, other_role)
    return names_agree and roles_agree


def _resolve(winner: Mapping[Seat, Seat], seat: Seat) -> Seat:
    """합쳐진 자리의 최종 대표. 사슬을 따라간다(`한길` → `한길교회` → `인천한길교회`).

    맴돌지 않는다 — 이어붙일 때 **순위가 낮은 쪽만** 높은 쪽을 가리키므로(`_specificity`)
    사슬을 따라갈수록 순위가 오르고, 순위는 유한하다.
    """
    while seat in winner:
        seat = winner[seat]
    return seat


def _specificity(seat: Seat) -> tuple[int, int, Seat]:
    """남길 열쇠 고르기 — **직분을 많이 적은 쪽**, 그다음 **이름이 긴 쪽**. 같으면 자리 자체로
    정해 **결과가 안 흔들린다**.

    직분이 먼저다: 지원자가 거르는 칸이고, `기타`·짧은 이름일수록 남의 자리와 겹치기 쉽다.
    """
    named = (_positions_in(seat[2]) or frozenset()) - _SILENT_ROLE
    return (len(named), len(seat[0]), seat)


def _is_name_variant(one: str, other: str) -> bool:
    """같은 교회의 다른 표기인가 — 꼬리(`교회`)를 떼고 한쪽이 다른 쪽을 품는가.

    실측에 두 모양이 다 있다: 앞에 지역이 붙거나(`남광` ⊂ `광주남광`), 가운데에 교단이
    끼거나(`한밭제일` ⊂ `한밭제일장로`). 접미를 떼면 둘 다 포함 관계가 된다.
    """
    if one == other:
        return False
    left, right = _CHURCH_TAIL.sub("", one), _CHURCH_TAIL.sub("", other)
    if not left or not right:
        return False
    # 꼬리만 다른 것(`한길`/`한길교회`)도 변형이다 — 묶음 안의 충돌 검사(`_unlink_bridged`)가 이
    # 쌍을 "충돌"로 읽으면 사슬로 잘 붙은 셋을 통째로 풀어 버린다(테스트가 잡았다).
    return left == right or left in right or right in left


def _is_role_variant(one: str, other: str) -> bool:
    """같은 자리의 다른 직분 표기인가 — 한쪽이 **안 적었거나**(침묵) **덜 적었다**(부분집합).

    부서 규칙(§4.1 4단계 · 2026-08-19)과 같은 판단이다: 침묵은 "다른 직분"이 아니라 안 적은
    것이고, 부분집합은 덜 적은 것이다. 실측 2026-09-02(공개 중 65곳): 한쪽 침묵 37곳 · 한쪽이
    다른 쪽에 포함 21곳 · **겹치는 게 없음 7곳** — 마지막이 아래 첫 ⚠️다.

    ⚠️ **겹치는 게 없으면 붙이지 않는다**(`전도사` vs `부목사`) — 진짜 두 자리일 수 있다.
       **반만 겹쳐도** 붙이지 않는다(`부목사+전도사` vs `전도사+강도사`). 근거 없으면 안 묶는다.
    ⚠️ **직무(일반직)는 견주지 않는다.** 직분이 빈 열쇠는 직무 글자(`ROLE:반주자`)인데, 그것은
       침묵이 아니라 **일반직이라는 뜻**이다(`ReviewData`: 사역직은 직분이 항상 있고 직무는
       일반직에만 있다). 반주자는 전도사와도, 사무간사와도 다른 자리다 — 실측 65곳 중 6곳이
       이 모양이었고 붙이지 않는다.
    """
    left, right = _positions_in(one), _positions_in(other)
    if left is None or right is None or left == right:
        return False
    if left <= _SILENT_ROLE or right <= _SILENT_ROLE:
        return True
    return left <= right or right <= left


def _positions_in(role: str) -> frozenset[str] | None:
    """열쇠의 직분 칸을 집합으로. 직무 글자(`ROLE:…`)는 일반직이라 **직분이 아니다** — `None`."""
    if role.startswith(_ROLE_PREFIX):
        return None
    return frozenset(role.split(_POSITION_JOINER))


def _share_a_mailbox(one: Sequence[_Member], other: Sequence[_Member]) -> bool:
    """두 자리가 접수 메일함을 나눠 쓰는가 — 같은 교회라는 **유일한 근거**다.

    ⚠️ 전화·링크·우편은 보지 않는다(`_mailboxes_differ`와 같은 이유) — 대표번호·홈페이지는
    교회가 같아도 게시판마다 다르게 적히고, 반대로 겹쳐도 자리를 가르는 정보가 없다.
    """
    ours = {box for member in one for box in member.mailboxes}
    return any(box in ours for member in other for box in member.mailboxes)


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
    """한 라운드를 부서로 가른다(SPEC §4.1 4단계)."""
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
    """한 자리로 볼 후보들 — **접수 이메일이 뒤집지 않으면** 같은 자리다(SPEC §4.1 5단계).

    ⚠️ `department`는 이 자리의 부서이고, 부서를 **말하지 않은 글도 함께 받는다**(4단계에서
    침묵을 그 부서로 읽는다) — 그래서 "부서가 같은 것끼리"가 아니라 "한 자리"다.
    """
    key = dedup_key(seat, department, round_number=number)
    if len(members) == 1:
        # ⚠️ 앵커 혼자면 아무것도 쓰지 않는다(`_restore`가 `None`) — `jobs` 행이라 쓸 칸이 없다.
        alone = _restore(members[0], key, DedupState.ALONE)
        return [] if alone is None else [alone]
    if _mailboxes_differ(members):
        # ⚠️ 접수 메일함이 다르면 **다른 자리일 수 있다** — 그런데 메일만으로는 확정할 수 없다
        #    (한 담당자가 메일을 바꿔 올렸을 수도 있다). 그때 **주소가 답을 준다**.
        verdict = _same_place(members)
        if verdict is None:
            # 주소를 못 견줬다 — 지금까지처럼 사람이 정한다(운영자 결정 2026-08-19).
            return _hold_for_review(seat, number, members)
        if not verdict:
            # 주소가 다르다 = **여러 교회가 한 자리로 묶였다.** 주소로 나눠 각각 판정한다.
            return _split_by_place(seat, number, key, members)
    return _settle(seat, number, key, members)


def _settle(seat: Seat, number: int, key: str, members: Sequence[_Member]) -> list[DedupUpdate]:
    """한 자리로 확정된 무리를 판정한다 — 혼자면 `ALONE`, 아니면 대표를 뽑아 병합.

    ⚠️ 사람이 이미 본 자리가 둘 이상이면 정리도 사람이 한다 — 어느 쪽을 내릴지 우리가 고를 수
    없고(둘 다 승인·게재됐을 수 있다) 판정을 쓸 권한도 없다.
    """
    if len(members) == 1:
        one = _restore(members[0], key, DedupState.ALONE)
        return [] if one is None else [one]
    if _owned(members) > 1:
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


def _split_by_place(
    seat: Seat, number: int, key: str, members: Sequence[_Member]
) -> list[DedupUpdate]:
    """여러 교회가 한 자리로 묶였다 — **주소로 나눠 각각 판정**한다.

    ⚠️⚠️ **나눈 뒤에도 무리 안에서는 병합해야 한다.** 전부 `ALONE`으로 흩으면 같은 교회가
    올린 재게시가 **각각 공개된다** — 실측 2026-08-27: `함께하는교회`가 성남 3건(주소·메일
    완전 동일)과 의정부 1건으로 묶여 있었는데, 흩는 순간 성남 3건이 전부 승인됐다. 중복을
    막으려고 만든 판정이 중복을 만드는 셈이다.

    ⚠️ 주소를 모르는 멤버(앵커)는 어느 무리인지 알 수 없어 **사람이 본다.**
    ⚠️ 키는 무리마다 같다 — 자물쇠(교회명·지역·직분)가 같으니 당연하고, §4.2의 앵커 조회도
    그 자물쇠로 한다. 주소는 이 판정 안에서만 쓰는 값이다.
    """
    무리, 모름 = _grouped_by_place(members)
    updates = [update for group in 무리 for update in _settle(seat, number, key, group)]
    if 모름:
        updates.extend(_hold_for_review(seat, number, 모름))
    return updates


def _grouped_by_place(
    members: Sequence[_Member],
) -> tuple[list[list[_Member]], list[_Member]]:
    """주소가 같은 것끼리 묶는다. 주소를 모르는 멤버는 따로 돌려준다.

    ⚠️ 먼저 온 무리에 붙인다 — `_fits`는 이행적이지 않아서(A⊂B, B⊂C인데 A⊄C) 무리가
    순서에 따라 갈릴 수 있다. 후보 순서가 고정돼 있으므로(`identity`) 결과는 멱등하다.
    """
    무리: list[list[_Member]] = []
    모름: list[_Member] = []
    for member in members:
        if not all(member.place):
            모름.append(member)
            continue
        같은곳 = next(
            (group for group in 무리 if _same_place([group[0], member])),
            None,
        )
        if 같은곳 is None:
            무리.append([member])
        else:
            같은곳.append(member)
    return 무리, 모름


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


def _master_priority(member: _Member) -> tuple[bool, bool, int, int, date, str]:
    """대표 순위: **사람이 확인한 것** > 자동 승인된 것 > **직분을 자세히 적은 것** > 빈 칸 적은 것
    > 최신 > id.

    ⚠️ "가장 충실한 것"이 최신보다 앞이다(운영자 결정 2026-08-17). 교차게시는 같은 날 여러
    게시판에 올라와 날짜가 자주 동점이고, 포스터 공고가 대표가 되면 사람이 볼 건수가 늘어난다.
    ⚠️ **직분이 빈 칸 수보다 앞이다**(운영자 결정 2026-09-02). 직분 창구(§4.1 2단계)가 `기타`와
    `전도사`를 한 자리로 묶은 뒤로, 빈 칸 수로만 고르면 사례비 한 칸 더 채운 `기타` 쪽이 남아
    지원자가 직분으로 거를 때 못 본다 — 지원자가 거르는 칸이 앞이다.
    ⚠️ 마지막이 식별자인 이유: 여기까지 동점이면 **무엇이든 고정된 순서**여야 한다(멱등).
    ⚠️ **앵커는 첫 기준에서 이긴다** — 이미 공개된 자리를 새 공고가 밀어내지 않는다(SPEC §4.2).
    """
    draft = member.draft
    return (
        member.is_owned,
        draft is not None and draft.confidence is Confidence.HIGH,
        _named_positions(draft),
        member.completeness,
        member.posted_on,
        member.identity,
    )


def _named_positions(draft: ReviewData | None) -> int:
    """`기타`를 뺀 직분 수. 앵커는 0 — 첫 기준에서 이미 이기므로 값이 쓰이지 않는다."""
    if draft is None:
        return 0
    return sum(1 for position in draft.position if position is not Position.ETC)


def _completeness(draft: ReviewData) -> int:
    """채워진 칸 수. 어느 칸을 셀지 손으로 적지 않는다 — 칸이 늘면 자동으로 따라온다."""
    return sum(
        1 for info in fields(draft) if info.name not in _NOT_CONTENT and getattr(draft, info.name)
    )


def _place(city: str | None, address: str | None) -> tuple[str, str]:
    """(도시, 주소)를 견줄 꼴로. 한쪽이라도 비면 그 자리는 빈 문자열이다.

    ⚠️ **붙이지 않고 따로 둔다.** 이어 붙이면 단위 차이가 **문자열 중간**에 들어가 부분 포함이
    깨진다(`고성군|중앙로…` vs `고성군|고성읍|중앙로…` — 실측).
    ⚠️ 공백·기호를 지운다 — `자양로45길 62`와 `자양로 45길62`는 같은 곳이다.
    """
    return (_PLACE_NOISE.sub("", city or ""), _PLACE_NOISE.sub("", address or ""))


def _same_place(members: Sequence[_Member]) -> bool | None:
    """주소가 같은 교회인가. **못 견주면 `None`**(한 곳이라도 주소가 비었다).

    ⚠️ **메일함이 갈렸을 때만 부른다.** 자물쇠(교회명·지역·직분)는 지역을 **광역**까지만 보므로
    같은 광역 안의 동명이교회를 못 가른다 — 실측 2026-08-26: `신광교회`가 관악구와 중구에,
    `영광교회`가 강서구와 금천구에 각각 있었고 둘 다 검수 큐에서만 드러났다.

    ⚠️ **"같다"를 근거로 합치는 데도 쓴다** — 같은 교회가 담당자를 바꿔 올리면 메일이 갈리는데
    (실측 101건 중 87건), 주소가 하나면 그건 같은 자리다.

    ⚠️ **한쪽이 다른 쪽의 부분이면 같은 곳이다** — 게시판마다 적는 단위가 다르다
    (`청주시 오송읍 연제길 26` vs `연제길 26` · `고성군` vs `고성군 고성읍`).

    ⚠️⚠️ **주소를 아는 것끼리만 견준다.** 앵커(`jobs`)는 주소를 읽지 않아 늘 비어 있는데,
    전원이 알아야 판정한다고 하면 **이미 공개된 자리가 낀 묶음에서 규칙이 통째로 죽는다** —
    실측 2026-08-26: 영광교회·신광교회가 정확히 그 모양이라, 규칙을 넣고도 둘 다 `UNCERTAIN`에
    그대로 남았다(그 둘을 가르려고 만든 규칙인데).

    ⚠️ 여기서는 **"전부 같은가"만** 답한다. 어긋났을 때 어떻게 나눌지는 `_split_by_place`가
    정한다 — 흩어 놓지 않고 주소로 묶어 각각 판정한다(그 함수의 ⚠️⚠️ 참조).
    """
    places = [member.place for member in members if all(member.place)]
    if len(places) < _PLACES_TO_JUDGE:
        return None  # 견줄 주소가 둘도 안 된다 — 판정하지 않는다
    return all(
        _fits(left_city, right_city) and _fits(left_road, right_road)
        for (left_city, left_road), (right_city, right_road) in combinations(places, 2)
    )


def _fits(left: str, right: str) -> bool:
    """한쪽이 다른 쪽의 부분이면 같은 것으로 본다 — **숫자를 자르지 않을 때만**.

    실측(2026-08-26 · 같은 자리 안의 주소 쌍 26개 중 21개): 표기 차이는 거의 다 읍·면·동이
    붙고 빠지는 것이다(`향교길29` ⊂ `의성읍향교길29` · `지정로125` ⊂ `지정로125지축동911`).

    ⚠️ **그런데 그냥 부분 포함으로 두면 번지가 잘려도 통과한다** — `강서로41`이 `강서로412`의
    부분이라 **다른 주소 두 곳이 한 자리로 합쳐진다**. 잘린 자리의 글자가 숫자면 거절한다.
    """
    short, long = sorted((left, right), key=len)
    at = long.find(short)
    if at < 0:
        return False
    return not (
        long[at - 1 : at].isdigit() or long[at + len(short) : at + len(short) + 1].isdigit()
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
