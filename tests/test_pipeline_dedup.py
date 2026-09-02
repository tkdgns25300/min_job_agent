"""중복 판정 테스트 — 어떤 두 글을 같은 자리로 보나(SPEC §4.1).

⚠️ 여기서 규칙이 느슨해지면 **진짜 공고가 사라진다**(다른 자리를 합치면 그 자리는 어디에도
안 보인다). 반대로 빡빡해지면 중복이 남는데, 그건 되돌릴 수 있다 — 검사도 그 비대칭을 따른다.

모델도 네트워크도 파일도 없다. 실제 게시판에서 나온 사례를 그대로 재현한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import Final

import pytest

from minjob_ingest.clock import KST
from minjob_ingest.domain import (
    Confidence,
    DedupState,
    Denomination,
    DenominationSource,
    Department,
    IsChurchRecruitment,
    JobKind,
    Position,
    Region,
    RejectReason,
    ReviewStatus,
)
from minjob_ingest.models import ReviewData, new_id
from minjob_ingest.pipeline.dedup import (
    ROUND_MONTHS,
    normalize_church_name,
    plan,
    seat_of,
)
from minjob_ingest.store.base import DedupCandidate, DedupUpdate, JobAnchor
from minjob_ingest.store.guards import with_dedup

_NOW: Final = datetime(2026, 8, 17, 9, 0, tzinfo=KST)
_TODAY: Final = _NOW.date()
_DAY: Final = date(2026, 8, 4)


def _draft(**overrides: object) -> ReviewData:
    """자물쇠 셋이 다 찬 초안. 검사마다 한 칸씩 바꿔 본다."""
    base: dict[str, object] = {
        "source_data_id": new_id(),
        "run_id": new_id(),
        "source_url": "https://example.kr/board/1",
        "is_church_recruitment": IsChurchRecruitment.YES,
        "confidence": Confidence.HIGH,
        "denomination_source": DenominationSource.UNKNOWN,
        "job_kind": (JobKind.MINISTRY,),
        "position": (Position.ASSOCIATE_PASTOR,),
        "title": "장성제일교회에서 동역할 부목사님을 청빙합니다.",
        "description": "장성제일교회가 부목사를 청빙합니다.",
        "church_name": "장성제일교회",
        "region": Region.JEONNAM,
        "contact_email": "shoutlord@hanmail.net",
        "posted_at": _DAY,
        "created_at": _NOW,
    }
    base.update(overrides)
    return ReviewData(**base)  # type: ignore[arg-type]


def _candidate(*, on: date = _DAY, **overrides: object) -> DedupCandidate:
    return DedupCandidate(draft=_draft(**overrides), posted_on=on)


def _plan(
    candidates: Sequence[DedupCandidate], *, anchors: Sequence[JobAnchor] = ()
) -> tuple[DedupUpdate, ...]:
    """기준일을 고정해 부른다 — 마감 판정이 벽시계에 흔들리면 테스트가 날짜에 따라 갈린다."""
    return plan(candidates, today=_TODAY, anchors=anchors).updates


def _by_id(updates: tuple[DedupUpdate, ...]) -> dict[str, DedupUpdate]:
    return {str(update.review_data_id): update for update in updates}


def _after(candidate: DedupCandidate, update: DedupUpdate) -> DedupCandidate:
    """판정을 반영한 초안. ⚠️ 라벨과 판정을 **한 번에** 넣는다 — 나눠 넣으면 "중복인데 살아
    있는" 중간 상태가 생기고 레코드 불변식이 막는다(저장소도 같은 방식이다)."""
    verdict = update.verdict
    draft = candidate.draft
    return DedupCandidate(
        draft=replace(
            draft,
            dedup_key=update.dedup_key,
            dedup_state=update.dedup_state,
            review_status=verdict.review_status if verdict else draft.review_status,
            reject_reason=verdict.reject_reason if verdict else draft.reject_reason,
            posted_at=verdict.posted_at if verdict else draft.posted_at,
        ),
        posted_on=candidate.posted_on,
    )


def _states(updates: tuple[DedupUpdate, ...]) -> list[DedupState]:
    return [update.dedup_state for update in updates]


# ── 교회명 정규화 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[군산] 개복교회(전북 군산)", "개복교회"),
        ("세상의 빛 이레교회", "세상의빛이레교회"),
        ("상도교회(서울시 동작구)", "상도교회"),
        ("온누리교회", "온누리교회"),
    ],
    ids=lambda value: str(value)[:20],
)
def test_the_church_name_is_compared_without_decoration(raw: str, expected: str) -> None:
    """⚠️ 게시판마다 교회명에 지역·교단을 덧붙인다 — 그대로 견주면 같은 교회가 갈린다."""
    assert normalize_church_name(raw) == expected


def test_a_name_that_is_only_decoration_is_no_name() -> None:
    """`(전북 군산)`만 남은 값을 키에 넣으면 서로 다른 교회가 한 키에 모인다."""
    assert normalize_church_name("(전북 군산)") is None
    assert normalize_church_name(None) is None


# ── 이름 표기가 갈린 자리 합치기 (SPEC §4.1 2) ──────────────────


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("남광교회", "광주남광교회"),
        ("성원교회", "군산성원교회"),
        ("한밭제일교회", "한밭제일장로교회"),
        ("아름다운교회", "아름다운침례교회"),
        ("영광", "김제영광교회"),
    ],
    ids=["앞에 지역", "앞에 지역2", "가운데 교단", "가운데 교단2", "꼬리까지 빠짐"],
)
def test_the_same_church_written_two_ways_is_one_seat(one: str, other: str) -> None:
    """실측 2026-09-02 — 게시판마다 제목이 달라 공개 중이던 18곳이 두 번씩 떠 있었다."""
    updates = _plan([_candidate(church_name=one), _candidate(church_name=other)])

    assert len({update.dedup_key for update in updates}) == 1
    assert _states(updates).count(DedupState.DUPLICATE) == 1


def test_the_longer_name_wins_the_key() -> None:
    """짧은 이름일수록 남의 교회와 겹치기 쉽다 — 정보가 많은 쪽을 남긴다."""
    updates = _plan([_candidate(church_name="남광교회"), _candidate(church_name="광주남광교회")])

    assert all(update.dedup_key.startswith("광주남광교회:") for update in updates)


def test_three_spellings_collapse_into_one_seat() -> None:
    """실측 `한길` / `한길교회` / `인천한길교회` — 사슬을 따라 하나로 모인다."""
    updates = _plan(
        [
            _candidate(church_name="한길"),
            _candidate(church_name="한길교회"),
            _candidate(church_name="인천한길교회"),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 1
    assert _states(updates).count(DedupState.DUPLICATE) == 2


def test_a_different_church_with_a_shared_prefix_is_never_merged() -> None:
    """⚠️ 이름이 포함 관계여도 **접수 메일함이 겹치지 않으면 합치지 않는다** —
    `중앙교회`와 `광주중앙교회`를 붙이는 것은 다른 교회를 합치는 것이고 되돌릴 수 없다."""
    updates = _plan(
        [
            _candidate(church_name="중앙교회", contact_email="one@example.kr"),
            _candidate(church_name="광주중앙교회", contact_email="other@example.kr"),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 2
    assert DedupState.DUPLICATE not in _states(updates)


def test_a_short_name_holding_two_churches_is_never_merged() -> None:
    """⚠️ 실측 2026-09-02 `예수로교회` — 경기도에 남양주와 성남 두 곳이 있어 **한 자리에 두
    교회가 들어 있었다**. 통째로 합치면 성남 쪽 메일 하나가 겹치는 것만으로 남양주가 끌려온다.

    기존 파이프라인은 이 자리를 주소로 갈라(§4.1 5b) 둘 다 공개하고 있었다 — 합쳐 놓으면
    부서가 섞여 그 차례가 오지 않는다.
    """
    updates = _plan(
        [
            _candidate(  # 남양주 예수로교회
                church_name="예수로교회",
                city="남양주시",
                address="묵현로 11-1",
                contact_email="one@example.kr",
            ),
            _candidate(  # 성남 예수로교회
                church_name="예수로교회",
                city="성남시 중원구",
                address="금빛로 85",
                contact_email="other@example.kr",
            ),
            _candidate(
                church_name="성남 예수로교회",
                city="성남시 중원구",
                address="금빛로 85",
                contact_email="other@example.kr",
            ),
        ]
    )

    assert {update.dedup_key.split(":")[0] for update in updates} == {
        "예수로교회",
        "성남예수로교회",
    }


def test_a_variant_without_a_mailbox_is_never_merged() -> None:
    """접수 메일이 아예 없으면 근거가 없다 — **갈린 채로 두는 쪽이 안전하다**."""
    updates = _plan(
        [
            _candidate(church_name="중앙교회", contact_email=None),
            _candidate(church_name="광주중앙교회", contact_email=None),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 2


def test_a_variant_in_another_region_is_never_merged() -> None:
    """지역이 다르면 애초에 견주지 않는다 — 자물쇠의 나머지 둘은 enum이라 안 흔들린다."""
    updates = _plan(
        [
            _candidate(church_name="남광교회", region=Region.GWANGJU),
            _candidate(church_name="광주남광교회", region=Region.JEONNAM),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 2


def test_a_variant_for_another_position_is_never_merged() -> None:
    """같은 교회라도 뽑는 직분이 다르면 다른 자리다."""
    updates = _plan(
        [
            _candidate(church_name="남광교회", position=(Position.SENIOR_PASTOR,)),
            _candidate(church_name="광주남광교회", position=(Position.EVANGELIST,)),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 2


def test_merging_keeps_the_published_row_as_master() -> None:
    """이름이 갈린 채 이미 공개된 자리도 합쳐진다 — 그게 이 규칙이 막으려던 사고다."""
    published = _candidate(church_name="성산교회", published_job_id=new_id())
    fresh = _candidate(church_name="마포성산교회")

    updates = _by_id(_plan([published, fresh]))

    assert updates[str(fresh.draft.id)].dedup_state is DedupState.DUPLICATE


# ── 직분 표기가 갈린 자리 합치기 (SPEC §4.1 2단계 · 2026-09-02) ──────


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ((Position.ETC,), (Position.EVANGELIST,)),
        ((Position.ASSOCIATE_PASTOR,), (Position.ASSOCIATE_PASTOR, Position.EVANGELIST)),
        (
            (Position.ASSOCIATE_PASTOR, Position.EVANGELIST),
            (Position.ASSOCIATE_PASTOR, Position.EVANGELIST, Position.LICENSED_MINISTER),
        ),
    ],
    ids=["한쪽이 기타", "한쪽이 덜 적음", "셋 중 둘만"],
)
def test_the_same_seat_written_with_two_position_lists_is_one_seat(
    one: tuple[Position, ...], other: tuple[Position, ...]
) -> None:
    """실측 2026-09-02 — 공개 중 65곳이 직분 표기만 달라 두 번씩 떠 있었다(침묵 37 · 포함 21).
    `중고등부 파트` → 기타, `파트 전도사, 강도사, 부목사` → 셋: 모델은 둘 다 맞았고 교회가
    문구를 고쳤다. 부서 규칙과 같은 판단 — 침묵은 다른 값이 아니라 안 적은 것이다."""
    updates = _plan([_candidate(position=one), _candidate(position=other)])

    assert len({update.dedup_key for update in updates}) == 1
    assert _states(updates).count(DedupState.DUPLICATE) == 1


def test_a_general_job_never_joins_a_ministry_seat() -> None:
    """⚠️ 직분이 빈 열쇠는 침묵이 아니라 **일반직**이다(`ReviewData` 불변식: 직무는 GENERAL에만).
    반주자와 전도사는 메일이 같아도 다른 자리다 — 실측 65곳 중 6곳이 이 모양이었고 붙이지 않는다."""
    updates = _plan(
        [
            _candidate(job_kind=(JobKind.GENERAL,), position=(), role="반주자"),
            _candidate(position=(Position.EVANGELIST,)),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 2


def test_the_richer_position_list_keeps_the_key() -> None:
    """지원자가 거르는 칸이다 — `기타`가 아니라 직분을 말한 열쇠가 남는다."""
    updates = _plan(
        [_candidate(position=(Position.ETC,)), _candidate(position=(Position.EVANGELIST,))]
    )

    assert all(update.dedup_key.split(":")[2] == "EVANGELIST" for update in updates)


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ((Position.EVANGELIST,), (Position.ASSOCIATE_PASTOR,)),
        (
            (Position.ASSOCIATE_PASTOR, Position.EVANGELIST),
            (Position.EVANGELIST, Position.LICENSED_MINISTER),
        ),
    ],
    ids=["겹치는 게 없다", "반만 겹친다"],
)
def test_conflicting_position_lists_stay_two_seats(
    one: tuple[Position, ...], other: tuple[Position, ...]
) -> None:
    """⚠️ 진짜 두 자리일 수 있다(실측 7곳) — 근거 없으면 안 묶는다. 메일이 같아도 그렇다."""
    updates = _plan([_candidate(position=one), _candidate(position=other)])

    assert len({update.dedup_key for update in updates}) == 2
    assert DedupState.DUPLICATE not in _states(updates)


def test_general_jobs_have_no_spelling_variants() -> None:
    """직무는 자유 글자라 포함관계를 따질 수 없다 — `반주자`와 `사무간사`는 진짜 다른 자리고,
    `기타` 직분과도 붙지 않는다."""

    def general(role: str) -> DedupCandidate:
        return _candidate(job_kind=(JobKind.GENERAL,), position=(), role=role)

    two_roles = [general("반주자"), general("사무간사")]
    etc_and_role = [_candidate(position=(Position.ETC,)), general("반주자")]

    for pair in (two_roles, etc_and_role):
        assert len({update.dedup_key for update in _plan(pair)}) == 2


def test_a_silent_key_may_not_bridge_two_conflicting_ones() -> None:
    """⚠️ 실측 2026-09-02 혜천교회 — `기타`가 `부목사`와도 `전도사`와도 붙어 사슬을 따라가면
    부목사와 전도사가 한 자리가 된다. 쌍으로는 절대 붙이지 않는 조합이니 **묶음 전체를 풀어
    둔다**."""
    updates = _plan(
        [
            _candidate(position=(Position.ETC,)),
            _candidate(position=(Position.ASSOCIATE_PASTOR,)),
            _candidate(position=(Position.EVANGELIST,)),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 3
    assert DedupState.DUPLICATE not in _states(updates)


def test_a_position_variant_without_a_shared_mailbox_is_never_merged() -> None:
    updates = _plan(
        [
            _candidate(position=(Position.ETC,), contact_email="one@example.kr"),
            _candidate(position=(Position.EVANGELIST,), contact_email="other@example.kr"),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 2


def test_a_position_variant_with_clashing_addresses_is_never_merged() -> None:
    """주소 거부권은 직분 변형에도 그대로 — 같은 메일을 쓰는 다른 두 곳이면 다른 교회다."""
    updates = _plan(
        [
            _candidate(position=(Position.ETC,), city="남양주시", address="묵현로 11-1"),
            _candidate(position=(Position.EVANGELIST,), city="성남시 중원구", address="금빛로 85"),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 2


def test_name_and_position_may_drift_at_the_same_time() -> None:
    """칸마다 충돌만 없으면 된다 — 같은 교회라는 근거는 메일함이 따로 댄다."""
    updates = _plan(
        [
            _candidate(church_name="남광교회", position=(Position.ETC,)),
            _candidate(church_name="광주남광교회", position=(Position.EVANGELIST,)),
        ]
    )

    assert {update.dedup_key.rsplit(":", 2)[0] for update in updates} == {
        "광주남광교회:JEONNAM:EVANGELIST"
    }


def test_the_master_names_its_positions_before_it_fills_its_blanks() -> None:
    """운영자 결정 2026-09-02 — 직분 창구가 `기타`와 `전도사`를 한 자리로 묶은 뒤, 빈 칸 수로만
    대표를 고르면 사례비 한 칸 더 채운 `기타` 쪽이 남는다. 지원자가 거르는 칸이 앞이다."""
    fuller_but_vague = _candidate(
        position=(Position.ETC,), pay_note="월 100만원", benefit_note="사택"
    )
    named = _candidate(position=(Position.EVANGELIST,))

    updates = _by_id(_plan([fuller_but_vague, named]))

    assert updates[str(named.draft.id)].dedup_state is DedupState.MASTER
    assert updates[str(fuller_but_vague.draft.id)].dedup_state is DedupState.DUPLICATE


# ── 마감 지난 글은 다툼에서 빠진다 (2026-09-02) ──────────────────────


def test_an_expired_master_hands_the_seat_to_the_live_repost() -> None:
    """실측 2026-09-02 — 2곳이 뽑고 있는데 min_job엔 안 보였다: 대표는 마감으로 CLOSED,
    마감을 새로 단 재공고는 공개된 대표에 밀려 영영 `DUPLICATE`. 마감 지난 글은 원문 소멸과
    같이 **다툼에서 빠지고**, 재공고가 자리를 물려받아 등급대로 나간다."""
    expired = _candidate(
        on=date(2026, 7, 20),
        posted_at=date(2026, 7, 20),
        deadline=date(2026, 7, 27),
        published_job_id=new_id(),
    )
    repost = _candidate(
        on=date(2026, 8, 10), posted_at=date(2026, 8, 10), deadline=date(2026, 9, 13)
    )

    updates = _by_id(_plan([expired, repost]))

    assert str(expired.draft.id) not in updates, "빠진 행에는 아무것도 쓰지 않는다"
    alive = updates[str(repost.draft.id)]
    assert alive.dedup_state is DedupState.ALONE
    assert alive.verdict is not None and alive.verdict.review_status is ReviewStatus.APPROVED


def test_a_posting_that_closes_today_still_competes() -> None:
    """경계는 `deadline < today`다 — 마감 당일은 산다(min_job 노출 규칙과 같다)."""
    closing_today = _candidate(deadline=_TODAY, published_job_id=new_id())
    later = _candidate(on=date(2026, 8, 10), posted_at=date(2026, 8, 10))

    updates = _by_id(_plan([closing_today, later]))

    assert updates[str(closing_today.draft.id)].dedup_state is DedupState.MASTER
    assert updates[str(later.draft.id)].dedup_state is DedupState.DUPLICATE


def test_when_every_posting_has_expired_nothing_is_promoted() -> None:
    """빠지기만 한다 — 살아 있는 글이 없으면 아무것도 공개되지 않는다(내리는 것도 없다)."""
    first = _candidate(deadline=date(2026, 7, 27), published_job_id=new_id())
    second = _candidate(on=date(2026, 8, 1), posted_at=date(2026, 8, 1), deadline=date(2026, 8, 10))

    planned = plan([first, second], today=_TODAY)

    assert planned.updates == ()
    assert planned.expired == {str(first.draft.id), str(second.draft.id)}


def test_a_cross_post_made_before_the_deadline_dies_with_it() -> None:
    """⚠️ 실데이터에서 바로 잡은 것 — 형제가 있는 10자리 중 6자리가 이 모양이었다. 같은 청빙을 다른
    게시판에 마감 없이 올린 글은 재공고가 아니라 **같은 공고**다. 그것을 대표로 세우면 닫힌 청빙이
    되살아난다."""
    closed = _candidate(
        on=date(2026, 7, 20),
        posted_at=date(2026, 7, 20),
        deadline=date(2026, 7, 27),
        published_job_id=new_id(),
    )
    cross_post = _candidate(on=date(2026, 7, 22), posted_at=date(2026, 7, 22))

    planned = plan([closed, cross_post], today=_TODAY)

    assert planned.updates == ()
    assert str(cross_post.draft.id) in planned.expired


def test_a_repost_after_the_deadline_takes_the_seat() -> None:
    """마감 **뒤에** 올라온 글은 마감을 안 적었어도 재공고다 — 교회가 다시 뽑기 시작했다."""
    closed = _candidate(
        on=date(2026, 7, 20),
        posted_at=date(2026, 7, 20),
        deadline=date(2026, 7, 27),
        published_job_id=new_id(),
    )
    repost = _candidate(on=date(2026, 8, 1), posted_at=date(2026, 8, 1))

    updates = _by_id(_plan([closed, repost]))

    assert updates[str(repost.draft.id)].dedup_state is DedupState.ALONE


def test_a_sibling_with_its_own_live_deadline_survives_an_earlier_closing() -> None:
    """제 마감이 아직 남았으면 게시일과 상관없이 산다 — 교회가 살아 있다고 말한 글이다."""
    closed = _candidate(
        on=date(2026, 7, 20),
        posted_at=date(2026, 7, 20),
        deadline=date(2026, 7, 27),
        published_job_id=new_id(),
    )
    still_open = _candidate(
        on=date(2026, 7, 22), posted_at=date(2026, 7, 22), deadline=date(2026, 9, 30)
    )

    updates = _by_id(_plan([closed, still_open]))

    assert updates[str(still_open.draft.id)].dedup_state is DedupState.ALONE


def test_an_anchor_outlives_a_closed_crawler_posting() -> None:
    """앵커는 늘 산다 — 빠뜨리면 그 자리의 재공고가 교회가 직접 올린 공고 옆에 또 공개된다."""
    closed = _candidate(
        on=date(2026, 7, 20),
        posted_at=date(2026, 7, 20),
        deadline=date(2026, 7, 27),
        published_job_id=new_id(),
    )
    repost = _candidate(on=date(2026, 8, 1), posted_at=date(2026, 8, 1))
    anchor = _anchor(posted_at=date(2026, 7, 25))

    updates = _by_id(_plan([closed, repost], anchors=[anchor]))

    assert updates[str(repost.draft.id)].dedup_state is DedupState.DUPLICATE


# ── 자물쇠 셋 ─────────────────────────────────────────────────────


def test_the_lock_needs_all_three() -> None:
    assert seat_of(_draft()) == ("장성제일교회", "JEONNAM", "ASSOCIATE_PASTOR")


@pytest.mark.parametrize(
    "missing",
    [{"church_name": None}, {"region": None}, {"job_kind": (), "position": ()}],
    ids=["교회명", "지역", "직분"],
)
def test_a_missing_lock_means_no_judgement(missing: dict[str, object]) -> None:
    """⚠️ 근거가 없으면 **아무와도 견주지 않는다** — 중복이 남는 것보다 다른 교회를 합치는 것이
    훨씬 나쁘다(교회명 894종 중 70종이 두 지역 이상에 있다).

    실측 132건 중 2건이 여기 걸렸다(지역 없음).
    """
    draft = _draft(**missing)

    assert seat_of(draft) is None
    assert _plan([DedupCandidate(draft=draft, posted_on=_DAY)]) == ()


def test_a_general_job_is_locked_by_its_role() -> None:
    """일반직은 직분이 없고 직무가 있다 — 실측 `일심교회 사무간사` 2건이 이 모양이었다."""
    seat = seat_of(_draft(job_kind=(JobKind.GENERAL,), position=(), role="사무 간사"))

    assert seat == ("장성제일교회", "JEONNAM", "ROLE:사무간사")


def test_the_same_position_in_two_regions_is_not_the_same_seat() -> None:
    """⚠️ `온누리교회`는 서울·경기·대전·인천에 있다 — 지역이 키에 있어야 하는 이유다."""
    updates = _plan(
        [
            _candidate(church_name="온누리교회", region=Region.SEOUL),
            _candidate(church_name="온누리교회", region=Region.GYEONGGI),
        ]
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]


# ── 중복 확정 ─────────────────────────────────────────────────────


def test_the_same_seat_on_two_boards_keeps_one() -> None:
    """실측 `장성제일교회 부목사` — HTUS와 PCK에 같은 날 올라왔다."""
    updates = _plan([_candidate(), _candidate()])
    verdicts = {update.dedup_state: update for update in updates}

    assert set(verdicts) == {DedupState.MASTER, DedupState.DUPLICATE}
    duplicate = verdicts[DedupState.DUPLICATE]
    assert duplicate.verdict is not None
    assert duplicate.verdict.review_status is ReviewStatus.REJECTED
    assert duplicate.verdict.reject_reason is RejectReason.DUPLICATE


def test_every_row_in_a_group_shares_one_key() -> None:
    """키가 갈리면 admin이 "왜 이게 안 보이나"에 답할 수 없다."""
    updates = _plan([_candidate(), _candidate(), _candidate()])

    assert len({update.dedup_key for update in updates}) == 1
    assert _states(updates).count(DedupState.DUPLICATE) == 2


def test_the_key_says_which_seat_it_is() -> None:
    """사람이 읽을 수 있어야 한다 — 해시로 줄이면 왜 합쳐졌는지 답할 수 없다."""
    (update,) = _plan([_candidate(department=Department.CHILDREN)])

    assert update.dedup_key == "장성제일교회:JEONNAM:ASSOCIATE_PASTOR:CHILDREN:R1"


def test_a_posting_with_no_department_still_gets_a_key() -> None:
    """⚠️ 실측 69%가 부서를 말하지 않는다(담임목사는 원래 부서가 없다) — 그걸 판정에서 빼면
    가장 많이 교차게시되는 공고가 전부 중복으로 남는다."""
    (update,) = _plan([_candidate()])

    assert update.dedup_key.endswith(":-:R1")
    assert update.dedup_state is DedupState.ALONE


def test_the_master_carries_the_newest_posting_date() -> None:
    """계속 끌어올린다 = 아직 뽑고 있다. min_job이 3개월 지난 공고를 숨기므로 최신이 맞다."""
    updates = _by_id(
        _plan(
            [
                _candidate(on=date(2026, 7, 22), confidence=Confidence.MEDIUM),
                _candidate(on=date(2026, 7, 29), confidence=Confidence.MEDIUM),
            ]
        )
    )
    master = next(u for u in updates.values() if u.dedup_state is DedupState.MASTER)

    assert master.verdict is not None
    assert master.verdict.posted_at == date(2026, 7, 29)


def test_a_gone_master_leaves_the_seat_to_the_survivor() -> None:
    """원문이 사라진 행은 자리 다툼에서 빠진다(SPEC §4 gone 단계).

    실측 2026-08-30: 삭제 35건 중 27건이 다른 게시판에 살아있는 같은 자리를 갖고 있었다 —
    사라진 대표를 빼지 않으면 살아있는 쪽이 영영 중복으로 남아 그 자리가 비어 보인다.
    """
    gone = _candidate(source_gone_at=_NOW)
    survivor = _candidate()

    updates = _plan([gone, survivor])

    assert _by_id(updates).keys() == {str(survivor.draft.id)}
    (update,) = updates
    assert update.dedup_state is DedupState.ALONE  # 혼자 남았다 — 새 대표다


# ── 라운드 ────────────────────────────────────────────────────────


def test_a_repost_within_three_months_is_the_same_round() -> None:
    """실측 `담임목사청빙(평강교회)` — PCKWORLD에 7/22, 7/29 두 번 올라왔다."""
    updates = _plan(
        [_candidate(on=date(2026, 7, 22)), _candidate(on=date(2026, 7, 29))],
    )

    assert sorted(_states(updates), key=str) == [DedupState.DUPLICATE, DedupState.MASTER]


def test_a_gap_over_three_months_is_a_new_round() -> None:
    """⚠️ 다시 열린 자리는 **별개 공고**다 — 옛 묶음에 삼키면 새 공고가 사라진다."""
    updates = _plan(
        [_candidate(on=date(2026, 4, 3)), _candidate(on=date(2026, 8, 4))],
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]
    assert {update.dedup_key for update in updates} == {
        "장성제일교회:JEONNAM:ASSOCIATE_PASTOR:-:R1",
        "장성제일교회:JEONNAM:ASSOCIATE_PASTOR:-:R2",
    }


def test_the_round_boundary_is_counted_in_months_not_days() -> None:
    """⚠️ 90일 근사는 달마다 범위가 달라진다 — 5/4는 8/4의 정확히 3개월 전이라 같은 라운드다."""
    assert ROUND_MONTHS == 3

    updates = _plan([_candidate(on=date(2026, 5, 4)), _candidate(on=date(2026, 8, 4))])

    assert DedupState.DUPLICATE in _states(updates)


def test_rounds_chain_through_the_middle_posting() -> None:
    """4개월 벌어진 두 글 사이에 한 글이 있으면 **셋이 한 라운드**다(계속 뽑고 있었다)."""
    updates = _plan(
        [
            _candidate(on=date(2026, 4, 4)),
            _candidate(on=date(2026, 6, 4)),
            _candidate(on=date(2026, 8, 4)),
        ]
    )

    assert len({update.dedup_key for update in updates}) == 1


# ── 부서 ──────────────────────────────────────────────────────────


def test_different_departments_are_different_seats() -> None:
    """유초등부와 중고등부는 다른 자리다 — 합치면 한 자리가 사라진다."""
    updates = _plan(
        [
            _candidate(department=Department.CHILDREN),
            _candidate(department=Department.YOUTH),
        ]
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]


def test_a_department_named_on_one_side_only_is_the_same_seat() -> None:
    """⚠️ **침묵은 "다른 부서"가 아니라 안 적은 것이다**(2026-08-19 실측으로 고쳤다).

    같은 공고가 여러 게시판에 올라오면 **모델이 게시판마다 부서를 다르게 뽑는다** — 2주치
    694건에서 부서가 섞인 13묶음이 **전부 접수 이메일이 같았고**(= 같은 자리) 부서 값이 실제로
    서로 다른 묶음은 2개뿐이었다. 섞였다는 것만으로 검수로 보내면 53건이 헛검수다.
    """
    updates = _plan([_candidate(), _candidate(department=Department.WORSHIP)])

    assert DedupState.DUPLICATE in _states(updates)
    assert all("WORSHIP" in u.dedup_key for u in updates), "명시된 부서를 그 자리의 부서로 쓴다"


def test_a_department_named_on_one_side_still_waits_when_mailboxes_differ() -> None:
    """부서가 섞였고 **접수 메일함까지 갈리면** 가를 근거가 생긴다 — 그때는 사람이 본다."""
    updates = _plan(
        [
            _candidate(contact_email="one@x.kr"),
            _candidate(contact_email="two@x.kr", department=Department.WORSHIP),
        ]
    )

    assert _states(updates) == [DedupState.UNCERTAIN, DedupState.UNCERTAIN]


def test_two_named_departments_plus_silence_needs_a_person() -> None:
    """⚠️ 명시된 부서가 **둘 이상**인데 말하지 않은 글이 있으면, 그 글이 어느 자리에 붙는지
    알 방법이 없다 — 그건 사람이 정한다."""
    updates = _plan(
        [
            _candidate(department=Department.CHILDREN),
            _candidate(department=Department.YOUTH),
            _candidate(),
        ]
    )

    assert _states(updates) == [DedupState.UNCERTAIN] * 3


def test_only_the_others_wait_when_we_cannot_tell() -> None:
    """운영자 결정(2026-08-17): 대표는 그대로 내보내고 **나머지만** 검수로 돌린다.

    같은 자리였다면 어차피 하나만 공개돼야 하니 결과가 맞고, 다른 자리였다면 운영자가
    승인하면 된다. 전원을 돌리면 같은 자리인 경우에도 두 건을 보게 된다.
    """
    updates = _plan(
        [
            _candidate(department=Department.WORSHIP, description="찬양인도 사역자를 모십니다."),
            _candidate(department=Department.CHILDREN),
            _candidate(),
        ]
    )
    statuses = sorted(
        update.verdict.review_status.value for update in updates if update.verdict is not None
    )

    assert statuses == ["APPROVED", "PENDING", "PENDING"], "대표만 그대로 나가고 나머지가 기다린다"


def test_an_uncertain_group_can_be_found_by_the_shared_prefix() -> None:
    """키는 각자 제 부서를 말한다(거짓말하지 않는다) — 함께 찾는 것은 앞 3조각으로 한다."""
    updates = _plan(
        [
            _candidate(),
            _candidate(department=Department.WORSHIP),
            _candidate(department=Department.CHILDREN),
        ]
    )
    keys = {update.dedup_key for update in updates}

    assert keys == {
        "장성제일교회:JEONNAM:ASSOCIATE_PASTOR:-:R1",
        "장성제일교회:JEONNAM:ASSOCIATE_PASTOR:WORSHIP:R1",
        "장성제일교회:JEONNAM:ASSOCIATE_PASTOR:CHILDREN:R1",
    }
    assert all(key.startswith("장성제일교회:JEONNAM:ASSOCIATE_PASTOR:") for key in keys)


# ── 연락처 ────────────────────────────────────────────────────────


def test_a_different_address_means_a_different_church() -> None:
    """⚠️ 실측 2026-08-26: **같은 광역 안의 동명이교회**를 자물쇠가 못 가른다.

    `영광교회`가 강서구와 금천구에, `신광교회`가 관악구와 중구에 각각 있었다. 자물쇠는 지역을
    **광역(SEOUL)** 까지만 보므로 둘이 한 자리로 묶였고, 메일이 갈려 검수로 갔다 — 사람이 안
    보면 **한쪽 교회의 공고가 영영 묻힌다.** 주소가 다르면 애초에 다른 교회다.
    """
    updates = _plan(
        [
            _candidate(contact_email="hji2027@naver.com", city="강서구", address="강서로 412"),
            _candidate(contact_email="vogus2640@naver.com", city="금천구", address="금하로 793"),
        ]
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE], "둘 다 제 자리로 남는다"
    assert all(u.verdict is None or u.verdict.reject_reason is None for u in updates), (
        "어느 쪽도 거절되지 않는다"
    )


def test_the_same_address_settles_a_mailbox_that_changed_hands() -> None:
    """⚠️ 실측 2026-08-26: 검수 큐의 `UNCERTAIN` 101건 중 **87건이 주소가 하나**였다.

    같은 교회가 담당자를 바꿔 올리면 메일이 갈리는데, 그때 주소가 "같은 자리"라고 답한다.
    """
    updates = _plan(
        [
            _candidate(contact_email="acts0928@naver.com", city="진천군", address="진광로 100"),
            _candidate(contact_email="ysilj@naver.com", city="진천군", address="진광로 100"),
        ]
    )

    assert sorted(_states(updates), key=str) == [DedupState.DUPLICATE, DedupState.MASTER]


def test_an_address_written_at_another_scale_is_the_same_place() -> None:
    """⚠️ 게시판마다 적는 단위가 다르다 — `고성군` vs `고성군 고성읍`(실측).

    한쪽이 다른 쪽의 부분이면 같은 곳으로 본다. 아니면 표기 차이로 한 자리가 갈라진다.
    """
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="고성군", address="중앙로 25번길 12"),
            _candidate(
                contact_email="b@example.kr", city="고성군 고성읍", address="중앙로25번길 12"
            ),
        ]
    )

    assert sorted(_states(updates), key=str) == [DedupState.DUPLICATE, DedupState.MASTER]


def test_the_same_road_in_a_different_city_is_a_different_church() -> None:
    """⚠️ 도시와 주소를 **둘 다** 본다 — 길 이름만 견주면 다른 도시의 같은 길이 합쳐진다.

    `중앙로`·`대학로`처럼 흔한 길 이름은 도시마다 있다.
    """
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="진천군", address="중앙로 100"),
            _candidate(contact_email="b@example.kr", city="음성군", address="중앙로 100"),
        ]
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]


def test_a_different_road_in_the_same_city_is_a_different_church() -> None:
    """⚠️ 반대쪽도 본다 — 도시만 견주면 같은 구의 다른 교회가 합쳐진다.

    실측 `영광교회`가 그랬다면 강서구 두 곳이 한 자리가 됐을 것이다.
    """
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="강서구", address="강서로 412"),
            _candidate(contact_email="b@example.kr", city="강서구", address="화곡로 55"),
        ]
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]


def test_a_house_number_cut_in_half_is_not_the_same_address() -> None:
    """⚠️ **부분 포함만으로는 부족하다** — `강서로 41`이 `강서로 412`의 부분이라 다른 두 곳이
    합쳐진다. 잘린 자리가 숫자면 다른 주소다.

    검수 중에 코드를 다시 읽다 잡았다 — 실측에는 아직 없었지만 번지는 한 자리씩 흔한 값이다.
    """
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="강서구", address="강서로 41"),
            _candidate(contact_email="b@example.kr", city="강서구", address="강서로 412"),
        ]
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]


def test_a_house_number_cut_at_its_front_is_not_the_same_address() -> None:
    """⚠️ 잘리는 자리는 **앞뒤 둘 다** 본다 — `5번길 12`가 `25번길 12`의 부분이다."""
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="고성군", address="5번길 12"),
            _candidate(contact_email="b@example.kr", city="고성군", address="25번길 12"),
        ]
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("향교길 29", "의성읍 향교길 29"),
        ("지정로 125", "지정로 125 지축동 911"),
        ("상신로 31", "상신로 31번길"),
    ],
    ids=["앞에 읍", "뒤에 동", "번길이 더 붙음"],
)
def test_an_administrative_prefix_or_suffix_is_the_same_address(left: str, right: str) -> None:
    """⚠️ 실측(같은 자리 안의 주소 쌍 26개 중 21개): 표기 차이는 거의 다 읍·면·동이다.

    그래서 부분 포함을 버릴 수는 없다 — 숫자만 지키면 된다.
    """
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="의성군", address=left),
            _candidate(contact_email="b@example.kr", city="의성군", address=right),
        ]
    )

    assert sorted(_states(updates), key=str) == [DedupState.DUPLICATE, DedupState.MASTER]


def test_the_odd_one_leaves_and_the_rest_still_merge() -> None:
    """⚠️⚠️ **나눈 뒤에도 무리 안에서는 병합한다.**

    실측 2026-08-27: `함께하는교회`가 성남 3건(주소·메일 완전 동일)과 의정부 1건으로 한 자리에
    묶여 있었는데, 어긋난다고 전부 흩었더니 **성남 3건이 각각 승인돼 중복 공개**가 됐다.
    중복을 막으려고 만든 판정이 중복을 만든 셈이라 곧바로 고쳤다.
    """
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="강서구", address="강서로 412"),
            _candidate(contact_email="b@example.kr", city="강서구", address="강서로 412"),
            _candidate(contact_email="c@example.kr", city="금천구", address="금하로 793"),
        ]
    )

    from collections import Counter

    assert Counter(str(s.value) for s in _states(updates)) == Counter(
        {"MASTER": 1, "DUPLICATE": 1, "ALONE": 1}
    ), "같은 주소 둘은 합치고, 다른 주소 하나만 떨어져 나온다"


def test_a_member_without_an_address_is_not_dropped_when_the_rest_split() -> None:
    """⚠️ 주소를 모르는 초안을 **버리면 그 공고가 조용히 사라진다** — 어느 교회인지 모를 뿐이다.

    나뉜 무리 어디에도 넣을 수 없으니 사람이 본다.
    """
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="강서구", address="강서로 412"),
            _candidate(contact_email="b@example.kr", city="금천구", address="금하로 793"),
            _candidate(contact_email="c@example.kr"),
        ]
    )

    assert len(updates) == 3, "주소를 모르는 것도 판정을 받는다"
    from collections import Counter

    assert Counter(str(s.value) for s in _states(updates)) == Counter({"ALONE": 2, "UNCERTAIN": 1})


def test_an_anchor_without_an_address_does_not_kill_the_rule() -> None:
    """⚠️⚠️ **검수에서 잡은 결함이다.** 앵커(`jobs`)는 주소를 읽지 않아 늘 비어 있는데,
    전원이 주소를 알아야 판정한다고 두면 **이미 공개된 자리가 낀 묶음에서 규칙이 죽는다**.

    실측 2026-08-26: 영광교회·신광교회가 정확히 그 모양이었고, 규칙을 넣고도 둘 다
    `UNCERTAIN`에 남아 있었다 — 그 둘을 가르려고 만든 규칙인데 정작 안 먹었다.
    """
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="강서구", address="강서로 412"),
            _candidate(contact_email="b@example.kr", city="금천구", address="금하로 793"),
        ],
        anchors=[_anchor(contact_email="c@example.kr")],
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]


def test_an_address_only_one_side_wrote_still_goes_to_a_person() -> None:
    """⚠️ 견줄 수 없으면 판정하지 않는다 — 지금까지처럼 사람이 본다.

    앵커(`jobs`)는 주소를 아예 읽지 않으므로 이 길로 온다(§8: `jobs`는 앵커로만 본다).
    """
    updates = _plan(
        [
            _candidate(contact_email="a@example.kr", city="강서구", address="강서로 412"),
            _candidate(contact_email="b@example.kr"),
        ]
    )

    assert _states(updates) == [DedupState.UNCERTAIN, DedupState.UNCERTAIN]


def test_the_address_is_not_consulted_when_mailboxes_agree() -> None:
    """⚠️ 주소는 **메일이 갈렸을 때만** 본다.

    메일이 같으면 자물쇠가 이미 같은 교회라고 말한 것이라, 주소 표기가 흔들려도 합친다
    (SPEC §4.1: 전화·링크·우편을 어느 방향으로도 쓰지 않는 것과 같은 이유).
    """
    updates = _plan(
        [
            _candidate(contact_email="same@example.kr", address="강서로 412"),
            _candidate(contact_email="same@example.kr", address="금하로 793"),
        ]
    )

    assert sorted(_states(updates), key=str) == [DedupState.DUPLICATE, DedupState.MASTER]


def test_two_different_mailboxes_go_to_a_person() -> None:
    """⚠️ 실측 `광림교회`: 한 게시판에 같은 날 두 건이 올라왔는데 청장년부와 교회학교였고
    **접수 이메일이 달랐다**(`klmchwang93` / `yoon4970`). 부서가 둘 다 비어 있어 3단계는 못 잡는다.

    ⚠️ **자동으로 가르지 않는다**(운영자 결정 2026-08-19) — 담당자가 메일을 바꿔 올렸을 수도
    있어 확정할 수 없다. 자동으로 가르면 중복이 남고 자동으로 합치면 자리가 사라지므로 사람이
    정한다. 어느 쪽이든 대표 하나는 그대로 공개된다.
    """
    updates = _plan(
        [
            _candidate(contact_email="klmchwang93@gmail.com", contact_tel="010-4152-6410"),
            _candidate(contact_email="yoon4970@naver.com", contact_tel="010-7122-4970"),
        ]
    )

    assert _states(updates) == [DedupState.UNCERTAIN, DedupState.UNCERTAIN]
    assert all(u.verdict is None or u.verdict.reject_reason is None for u in updates), (
        "둘 다 거절되지 않는다"
    )


def test_a_channel_only_one_side_filled_is_not_evidence() -> None:
    """⚠️ 실측 3묶음(세상의빛이레·이리성산·장성제일)이 이 모양이다 — 전화는 같은데 이메일을
    한쪽만 적었다. 침묵을 어긋남으로 세면 진짜 교차게시가 갈라진다."""
    updates = _plan(
        [
            _candidate(contact_email=None, contact_tel="010-2923-2989"),
            _candidate(contact_email="apply@seire.org", contact_tel="010-2923-2989"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_listing_one_more_number_is_not_evidence() -> None:
    """⚠️ 실측 2026-08-17 `방배교회`: 한쪽은 `02-599-0056, 010-4874-9191`(대표번호+담당자),
    다른 쪽은 `010-4874-9191`만 적었다. 같은 자리인데 **통째로 견줘서 11건이 갈라졌다.**

    연락처는 조립 칸이라 원문에 둘이 적혀 있으면 둘 다 담긴다(SPEC §5.5b) — 조각으로 쪼개
    **겹치는 것이 있으면** 같은 곳으로 본다.
    """
    updates = _plan(
        [
            _candidate(contact_email=None, contact_tel="02-599-0056, 010-4874-9191"),
            _candidate(contact_email=None, contact_tel="010-4874-9191"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_a_different_phone_number_is_not_a_different_seat() -> None:
    """⚠️ **전화는 보지 않는다**(운영자 결정 2026-08-19 · 실측으로 고쳤다).

    자물쇠에서 이미 같은 교회임을 확인했으므로 전화가 겹친다는 것은 "같은 교회"라는 뜻일 뿐이고,
    반대로 다르다는 것도 자리를 가르지 못한다 — 게시판마다 **대표번호와 담당자 휴대폰을 달리
    적는다**(실측: `광진교회` 6건이 이메일은 같은데 번호가 셋으로 갈려 6자리가 됐다).
    """
    updates = _plan(
        [
            _candidate(contact_email=None, contact_tel="010-4152-6410"),
            _candidate(contact_email=None, contact_tel="02-2625-0761"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_a_number_broken_by_spaces_is_not_compared() -> None:
    """⚠️ `010 4874 9191`을 `010`·`4874`·`9191`로 읽으면 겹치는 번호가 없다고 판정된다 —
    그런 값은 **아예 세지 않는** 쪽이 안전하다(안 세면 중복이 남을 뿐이다)."""
    updates = _plan(
        [
            _candidate(contact_email=None, contact_tel="010 4874 9191"),
            _candidate(contact_email=None, contact_tel="010-4874-9191"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_two_emails_in_one_field_still_match() -> None:
    updates = _plan(
        [
            _candidate(contact_email="apply@seire.org, office@seire.org"),
            _candidate(contact_email="apply@seire.org"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_nothing_to_compare_still_merges() -> None:
    """실측 `장성제일교회` PCK 건은 연락처가 아예 없었다 — 4단계는 **막는 근거**만 본다."""
    updates = _plan(
        [
            _candidate(contact_email=None),
            _candidate(contact_email="shoutlord@hanmail.net"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_the_phone_is_compared_by_its_digits() -> None:
    """게시판마다 표기가 갈린다(`02-793-9686` / `027939686`) — 글자로 견주면 같은 곳이 갈린다."""
    updates = _plan(
        [
            _candidate(contact_email=None, contact_tel="02-793-9686"),
            _candidate(contact_email=None, contact_tel="027939686"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("http://www.dech.or.kr/", "www.dech.or.kr"),
        ("http://a.kr/apply", "https://a.kr/apply"),
    ],
    ids=["www와 스킴", "http와 https"],
)
def test_the_same_link_written_differently_is_one_place(left: str, right: str) -> None:
    """⚠️ 스킴·`www.`·끝 슬래시는 표기 차이일 뿐이다 — 그대로 견주면 **같은 자리가 갈린다**
    (실측 `안양동은교회`가 이 링크를 쓴다 · 2026-08-17 검수에서 잡았다)."""
    updates = _plan(
        [
            _candidate(contact_email=None, contact_link=left),
            _candidate(contact_email=None, contact_link=right),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_a_different_link_is_not_a_different_seat() -> None:
    """⚠️ **링크도 보지 않는다** — 표기와 오타가 갈린다(실측 `샘물교회`: `www.semmul.org` /
    `http://www.semmul.or`(오타) / `부곡교회`: 링크 칸에 교회 이름이 들어왔다)."""
    updates = _plan(
        [
            _candidate(contact_email=None, contact_link="https://semmul.org"),
            _candidate(contact_email=None, contact_link="https://semmul.or"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_a_contact_that_does_not_look_like_one_is_still_compared() -> None:
    """⚠️ 꼴이 안 맞는 값을 버리면 **그 채널이 조용히 무력해진다** — 서로 다른 곳인데 비교할
    것이 없어 묶여버린다. 못 쪼갠 값은 통째로 하나의 조각으로 둔다.

    실제로 이런 값이 온다: 모델이 `apply @ seire.org`처럼 공백을 끼워 넣으면 이메일 꼴이
    깨지는데, 검산은 원문에 그 글자가 있으면 통과시킨다(SPEC §5.5b).
    """
    updates = _plan(
        [
            _candidate(contact_email="apply @ seire.org"),
            _candidate(contact_email="office@other.org"),
        ]
    )

    assert _states(updates) == [DedupState.UNCERTAIN, DedupState.UNCERTAIN]


def test_the_postal_address_does_not_split_a_group() -> None:
    """⚠️ `contact_post`는 검산을 거치지 않는 조립 칸이다(SPEC §5.5b) — 어긋남이 모델 탓인지
    원문 탓인지 구분되지 않아 가르는 근거로 쓰지 않는다."""
    updates = _plan(
        [
            _candidate(contact_post="서울시 종로구 1-1"),
            _candidate(contact_post="서울시 강남구 2-2"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


# ── 대표 선정 ─────────────────────────────────────────────────────


def test_the_most_complete_draft_becomes_the_master() -> None:
    """운영자 결정(2026-08-17): 자동 승인된 것 > 빈 칸 적은 것 > 최신.

    ⚠️ 교차게시는 같은 날 여러 게시판에 올라와 **날짜가 자주 동점**이라, 최신만으로는 대표가
    정해지지 않는다. 포스터 공고가 대표가 되면 사람이 볼 건수가 늘어난다.
    """
    rich = _candidate(pay_note="월 250만원", benefit_note="사택 제공", headcount="1명")
    poor = _candidate(confidence=Confidence.MEDIUM)

    updates = _by_id(_plan([poor, rich]))

    assert updates[str(rich.draft.id)].dedup_state is DedupState.MASTER
    assert updates[str(poor.draft.id)].dedup_state is DedupState.DUPLICATE


def test_an_auto_approved_draft_wins_over_a_fuller_one_that_needs_review() -> None:
    """포스터 공고(medium)가 대표가 되면 검수가 하나 늘어난다 — 등급이 충실함보다 앞이다."""
    approved = _candidate()
    fuller = _candidate(
        confidence=Confidence.MEDIUM, pay_note="월 250만원", benefit_note="사택", role=None
    )

    updates = _by_id(_plan([fuller, approved]))

    assert updates[str(approved.draft.id)].dedup_state is DedupState.MASTER


def test_the_master_is_the_same_whichever_order_they_arrive_in() -> None:
    """⚠️ 순서에 따라 대표가 바뀌면 같은 데이터에서 다른 결과가 나온다(멱등이 깨진다)."""
    first, second, third = _candidate(), _candidate(), _candidate()

    forward = _by_id(_plan([first, second, third]))
    backward = _by_id(_plan([third, second, first]))

    assert {key: value.dedup_state for key, value in forward.items()} == {
        key: value.dedup_state for key, value in backward.items()
    }


# ── 사람이 손댄 행 ────────────────────────────────────────────────


def test_an_already_published_row_keeps_the_master_seat() -> None:
    """⚠️ 이미 공개된 공고를 나중에 중복으로 거절하면 **목록에서 사라진다**.

    비교에서 빼는 것이 아니라 대표로 세운다 — 빼면 새로 온 같은 자리 글이 또 공개된다.
    """
    published = _candidate(published_job_id=new_id(), confidence=Confidence.MEDIUM)
    fresh = _candidate(pay_note="월 250만원", benefit_note="사택")

    updates = _by_id(_plan([fresh, published]))

    assert updates[str(published.draft.id)].dedup_state is DedupState.MASTER
    assert updates[str(published.draft.id)].verdict is None, "라벨만 붙인다"
    assert updates[str(fresh.draft.id)].dedup_state is DedupState.DUPLICATE


def test_an_operator_edited_row_is_never_written_to() -> None:
    """운영자가 교단을 확정해둔 행 — 판정을 쓰면 사람이 한 일을 덮는다."""
    touched = _candidate(
        denomination_source=DenominationSource.OPERATOR,
        denomination="TONGHAP",
        reviewed_by="operator@minjob",
        review_status=ReviewStatus.APPROVED,
    )

    updates = _by_id(_plan([touched, _candidate()]))

    assert updates[str(touched.draft.id)].verdict is None


def test_two_rows_a_person_already_saw_are_left_to_the_person() -> None:
    """어느 쪽을 내릴지 코드가 고를 수 없다(둘 다 승인·게재됐을 수 있다)."""
    left = _candidate(published_job_id=new_id())
    right = _candidate(published_job_id=new_id())

    updates = _plan([left, right])

    assert _states(updates) == [DedupState.UNCERTAIN, DedupState.UNCERTAIN]
    assert all(update.verdict is None for update in updates)


# ── 다시 돌려도 같은 결과 ─────────────────────────────────────────


def test_a_rejection_we_made_is_judged_again_from_scratch() -> None:
    """⚠️ 규칙을 고쳐 다시 돌리면 **잘못 거절한 행이 되살아나야** 한다.

    지난 실행의 `DUPLICATE`는 우리 판정이라 매번 처음부터 다시 본다. 그래서 혼자 남은 중복
    행은 대표로 복귀하고 상태가 등급대로 되돌아온다.
    """
    rejected = _candidate(
        dedup_key="장성제일교회:JEONNAM:ASSOCIATE_PASTOR:-:R1",
        dedup_state=DedupState.DUPLICATE,
        review_status=ReviewStatus.REJECTED,
        reject_reason=RejectReason.DUPLICATE,
    )

    (update,) = _plan([rejected])

    assert update.dedup_state is DedupState.ALONE
    assert update.verdict is not None
    assert update.verdict.review_status is ReviewStatus.APPROVED  # 등급이 high였다
    assert update.verdict.reject_reason is None


def test_a_revived_row_gets_the_status_its_grade_asks_for() -> None:
    """등급이 medium이면 되살려도 검수 대기다 — `confidence`를 건드리지 않기 때문에 가능하다."""
    rejected = _candidate(
        confidence=Confidence.MEDIUM,
        dedup_key="장성제일교회:JEONNAM:ASSOCIATE_PASTOR:-:R1",
        dedup_state=DedupState.DUPLICATE,
        review_status=ReviewStatus.REJECTED,
        reject_reason=RejectReason.DUPLICATE,
    )

    (update,) = _plan([rejected])

    assert update.verdict is not None
    assert update.verdict.review_status is ReviewStatus.PENDING


@pytest.mark.parametrize(
    "reason", [RejectReason.HERESY, RejectReason.CLOSED, RejectReason.OPERATOR]
)
def test_a_settled_rejection_is_left_alone(reason: RejectReason) -> None:
    """⚠️ 이단·마감 거절 행이 대표가 되면 **다른 게시판의 살아 있는 같은 자리 공고가 그 밑에
    중복으로 묻힌다**. 운영자 거절도 사람의 결론이라 건드리지 않는다."""
    settled = _candidate(review_status=ReviewStatus.REJECTED, reject_reason=reason)

    updates = _plan([settled, _candidate()])

    assert _states(updates) == [DedupState.ALONE], "살아 있는 한 건만 판정된다"


def test_the_same_input_gives_the_same_plan() -> None:
    """멱등 — 두 번 돌려도 같은 판정이 나온다."""
    candidates = [_candidate(), _candidate(), _candidate(department=Department.YOUTH)]

    assert _plan(candidates) == _plan(candidates)


def test_applying_the_plan_twice_changes_nothing_the_second_time() -> None:
    """판정을 반영한 상태로 다시 돌려도 결론이 같아야 한다 — 아니면 매 실행 상태가 흔들린다."""
    first, second = _candidate(), _candidate()
    updates = _by_id(_plan([first, second]))

    applied = [_after(candidate, updates[str(candidate.draft.id)]) for candidate in (first, second)]

    again = _by_id(_plan(applied))

    assert {key: value.dedup_state for key, value in again.items()} == {
        key: value.dedup_state for key, value in updates.items()
    }


# ── 앵커: 이미 공개된 자리와의 대조 (SPEC §4.2) ──────────────────


def _anchor(*, on: date = _DAY, **overrides: object) -> JobAnchor:
    """이미 공개돼 지금 목록에 보이는 `jobs` 한 행. 기본은 초안과 **같은 자리**다."""
    base: dict[str, object] = {
        "job_id": new_id(),
        "church_name": "장성제일교회",
        "region": Region.JEONNAM,
        "position": (Position.ASSOCIATE_PASTOR,),
        "role": None,
        "department": None,
        "posted_at": on,
        "contact_email": "shoutlord@hanmail.net",
    }
    base.update(overrides)
    return JobAnchor(**base)  # type: ignore[arg-type]


def test_no_anchors_leaves_the_judgment_exactly_as_before() -> None:
    """⚠️ **회귀 그물.** 앵커를 받게 고쳤어도 앵커가 없으면 결과가 전과 같아야 한다 —
    694건으로 검증한 판정이 조용히 달라지면 안 된다."""
    candidates = [_candidate(), _candidate(), _candidate(on=_DAY.replace(day=2))]

    assert _plan(candidates) == _plan(candidates, anchors=[])


def test_an_anchor_makes_the_new_posting_a_duplicate() -> None:
    """이미 공개된 자리를 새 공고가 밀어내지 않는다(SPEC §4.2) — 앵커는 항상 대표다."""
    candidate = _candidate()

    (update,) = _plan([candidate], anchors=[_anchor()])

    assert update.review_data_id == candidate.draft.id
    assert update.dedup_state is DedupState.DUPLICATE
    assert update.verdict is not None
    assert update.verdict.reject_reason is RejectReason.DUPLICATE


def test_nothing_is_written_for_the_anchor_itself() -> None:
    """`jobs` 행이라 `review_data`에 쓸 칸이 없다 — 판정이 나가면 저장소가 없는 id를 찾는다."""
    candidate = _candidate()

    updates = _plan([candidate], anchors=[_anchor(), _anchor()])

    assert {update.review_data_id for update in updates} == {candidate.draft.id}


def test_an_anchor_alone_produces_nothing() -> None:
    """공개된 자리만 있고 새 공고가 없으면 할 일이 없다."""
    assert _plan([], anchors=[_anchor()]) == ()


def test_an_anchor_for_another_seat_does_not_interfere() -> None:
    candidate = _candidate()

    (update,) = _plan([candidate], anchors=[_anchor(church_name="다른교회")])

    assert update.dedup_state is DedupState.ALONE


def test_an_anchor_older_than_a_round_does_not_suppress_the_reposting() -> None:
    """⚠️ 3개월을 넘기면 **재공고**다 — 옛 공개가 새 자리를 삼키면 그 자리가 영영 안 뜬다."""
    candidate = _candidate(on=date(2026, 8, 1))

    (update,) = _plan([candidate], anchors=[_anchor(on=date(2026, 1, 1))])

    assert update.dedup_state is DedupState.ALONE


def test_a_different_mailbox_goes_to_a_human_instead_of_being_rejected() -> None:
    """⚠️ 접수 메일함이 다르면 **다른 자리일 수 있다** — 자동으로 거절하면 우리 공고가 사라진다.

    그래서 앵커에 `contact_email`을 담는다(2026-08-21). 없으면 이 검사가 통째로 무력해진다.
    """
    candidate = _candidate()

    (update,) = _plan([candidate], anchors=[_anchor(contact_email="another@hanmail.net")])

    assert update.dedup_state is DedupState.UNCERTAIN
    assert update.verdict is not None
    assert update.verdict.review_status is ReviewStatus.PENDING


def test_an_anchor_without_a_seat_cannot_suppress_anything() -> None:
    """자물쇠가 비면 앵커가 되지 못한다 — 중복이 남는 쪽이 안전하다(`seat_of`와 같은 규칙)."""
    candidate = _candidate()

    (update,) = _plan([candidate], anchors=[_anchor(region=None)])

    assert update.dedup_state is DedupState.ALONE


def test_the_anchor_beats_even_a_human_checked_draft() -> None:
    """앵커와 사람이 본 초안이 함께 있으면 **둘 다 손대지 않는다** — 정리도 사람이 한다."""
    reviewed = _candidate(reviewed_by="operator@minjob")

    (update,) = _plan([reviewed], anchors=[_anchor()])

    assert update.dedup_state is DedupState.UNCERTAIN
    assert update.verdict is None  # 사람이 손댄 행에는 라벨만 쓴다


def test_judging_with_anchors_is_idempotent() -> None:
    """같은 입력이면 같은 결과다 — 앵커가 끼어도 흔들리지 않는다."""
    candidates = [_candidate(), _candidate(on=_DAY.replace(day=3))]
    anchors = [_anchor()]

    assert _plan(candidates, anchors=anchors) == _plan(candidates, anchors=anchors)


# ── 사람이 승인한 행 (SPEC §8 · admin 은 review_status 한 칸만 쓴다) ──


def test_an_operator_approval_is_never_pushed_back_to_review() -> None:
    """⚠️ **실측으로 잡은 버그**(2026-08-21). admin은 승인할 때 `review_status`만 쓰고
    `reviewed_by`는 비워 둔다(SPEC §8) — 그래서 `is_operator_owned`로 걸리지 않는다.

    그 상태에서 등급이 정하는 값(`medium` → `PENDING`)을 그대로 쓰면 **승인이 검수 큐로
    되돌아가고**, 사람이 다시 승인해도 다음 실행이 또 되돌린다. 그 자리는 영영 공개되지 않는다.
    """
    approved = _candidate(confidence=Confidence.MEDIUM, review_status=ReviewStatus.APPROVED)

    (update,) = _plan([approved])

    assert update.dedup_state is DedupState.ALONE
    # 라벨만 쓴다 — 판정을 아예 건드리지 않으므로 승인이 그대로 남는다.
    assert update.verdict is None


def test_an_approval_by_a_human_is_recognised_without_reviewed_by() -> None:
    """자동 승인은 `high`에서만 일어난다 — `APPROVED`인데 `high`가 아니면 사람이 승인한 것이다.

    ⚠️ 이 추론이 없으면 admin이 `reviewed_by`를 안 쓰는 한(SPEC §8) 사람의 손길을 볼 수 없다.
    """
    assert _draft(
        confidence=Confidence.MEDIUM, review_status=ReviewStatus.APPROVED
    ).is_operator_owned
    assert _draft(confidence=Confidence.LOW, review_status=ReviewStatus.APPROVED).is_operator_owned
    # 자동 승인은 사람의 손길이 아니다 — 재구조화가 덮어도 된다.
    assert not _draft(
        confidence=Confidence.HIGH, review_status=ReviewStatus.APPROVED
    ).is_operator_owned


def test_a_human_approved_row_wins_the_master_seat() -> None:
    """SPEC §4.1 대표 순위 — **사람이 확인한 것 > 자동 승인된 것**.

    ⚠️ 밀리면 사람이 고친 값이 옆 게시판의 AI 초안으로 대체된다(SPEC §8이 적어 둔 위험).
    """
    human = _candidate(confidence=Confidence.MEDIUM, review_status=ReviewStatus.APPROVED)
    fresh = _candidate(confidence=Confidence.HIGH, on=_DAY.replace(day=5))

    updates = _by_id(_plan([human, fresh]))

    assert updates[str(human.draft.id)].dedup_state is DedupState.MASTER
    assert updates[str(fresh.draft.id)].dedup_state is DedupState.DUPLICATE


def test_a_pending_draft_still_stays_pending() -> None:
    """되돌리지 않는 것은 **승인뿐**이다 — 아직 검수 전인 행은 그대로 큐에 남아야 한다."""
    (update,) = _plan([_candidate(confidence=Confidence.MEDIUM)])

    assert update.verdict is not None
    assert update.verdict.review_status is ReviewStatus.PENDING


def test_an_approved_draft_can_still_be_rejected_as_a_duplicate() -> None:
    """⚠️ 막는 것은 **승인 → 검수 대기** 한 방향뿐이다.

    승인된 행이 중복으로 드러나면 거절이 맞다 — `reject_reason`이 남아 되돌릴 수 있다.
    """
    older = _candidate(confidence=Confidence.HIGH, review_status=ReviewStatus.APPROVED)
    newer = _candidate(
        confidence=Confidence.MEDIUM,
        review_status=ReviewStatus.APPROVED,
        on=_DAY.replace(day=2),
    )

    updates = _by_id(_plan([older, newer]))
    rejected = [u for u in updates.values() if u.dedup_state is DedupState.DUPLICATE]

    assert len(rejected) == 1
    assert rejected[0].verdict is not None
    assert rejected[0].verdict.review_status is ReviewStatus.REJECTED
    assert rejected[0].verdict.reject_reason is RejectReason.DUPLICATE


def test_an_approved_draft_in_an_uncertain_group_keeps_its_approval() -> None:
    """`UNCERTAIN`은 사람이 정할 자리다 — 그런데 이미 사람이 정한 것을 되돌리면 안 된다."""
    approved = _candidate(confidence=Confidence.MEDIUM, review_status=ReviewStatus.APPROVED)
    other = _candidate(contact_email="other@hanmail.net")

    updates = _by_id(_plan([approved, other]))
    mine = updates[str(approved.draft.id)]

    assert mine.dedup_state is DedupState.UNCERTAIN
    assert mine.verdict is None or mine.verdict.review_status is ReviewStatus.APPROVED


def test_our_own_duplicate_rejection_is_undone_even_after_a_person_touched_the_row() -> None:
    """⚠️ 이걸 못 하면 **dedup이 한 행 때문에 멈춘다**(2026-08-21 실측).

    중복으로 거절한 행에 사람의 표시가 붙고(교단 교정) 나중에 묶음이 흩어지면, 라벨만 쓰는
    경로가 `reject_reason=DUPLICATE` + `dedup_state=ALONE`이라는 **깨진 짝**을 만든다.
    중복 거절은 **우리가 내린 것**이라 우리가 되돌려도 된다 — 사람의 결론이 아니다.
    """
    ours = _candidate(
        review_status=ReviewStatus.REJECTED,
        reject_reason=RejectReason.DUPLICATE,
        dedup_state=DedupState.DUPLICATE,
        dedup_key="장성제일교회:JEONNAM:ASSOCIATE_PASTOR:-:R1",
        denomination_source=DenominationSource.OPERATOR,
        denomination=Denomination.TONGHAP,
    )

    (update,) = _plan([ours])

    assert update.dedup_state is DedupState.ALONE
    assert update.verdict is not None
    assert update.verdict.reject_reason is None
    # 되살아난 상태는 등급이 정한다 — 처음 저장될 때와 같은 값이다.
    assert update.verdict.review_status is ReviewStatus.APPROVED
    # 저장소도 같은 판단을 해야 한다 — 아니면 여기서 통과하고 저장에서 멈춘다.
    assert with_dedup(ours.draft, update).reject_reason is None


def test_a_rejection_a_person_made_never_enters_the_judgement() -> None:
    """`OPERATOR` 거절은 사람의 결론이라 **후보에서 아예 빠진다**(`_SETTLED_REASONS`).

    ⚠️ 되돌리기 예외가 `DUPLICATE`에만 걸리는 이유를 함께 지킨다 — 사람의 거절까지 되돌리면
    운영자가 내린 결론이 매 실행 되살아난다.
    """
    theirs = _candidate(
        review_status=ReviewStatus.REJECTED,
        reject_reason=RejectReason.OPERATOR,
        reviewed_by="operator@a-better.co.kr",
    )

    assert _plan([theirs]) == ()


def test_a_person_marked_row_still_keeps_its_duplicate_label() -> None:
    """⚠️ 예외는 **되돌릴 때만**이다 — 중복 라벨을 새로 붙이는 것은 여전히 라벨만 쓴다.

    그렇지 않으면 사람이 승인·공개한 행을 크롤러가 중복으로 거절해 목록에서 지운다(SPEC §4.1).
    """
    touched = _draft(
        review_status=ReviewStatus.REJECTED,
        reject_reason=RejectReason.DUPLICATE,
        dedup_state=DedupState.DUPLICATE,
        dedup_key="장성제일교회:JEONNAM:ASSOCIATE_PASTOR:-:R1",
        reviewed_by="operator@a-better.co.kr",
    )
    assert not touched.allows_dedup_verdict(DedupState.DUPLICATE)
    assert touched.allows_dedup_verdict(DedupState.ALONE)
