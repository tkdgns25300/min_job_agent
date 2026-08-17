"""중복 판정 테스트 — 어떤 두 글을 같은 자리로 보나(SPEC §4.1).

⚠️ 여기서 규칙이 느슨해지면 **진짜 공고가 사라진다**(다른 자리를 합치면 그 자리는 어디에도
안 보인다). 반대로 빡빡해지면 중복이 남는데, 그건 되돌릴 수 있다 — 검사도 그 비대칭을 따른다.

모델도 네트워크도 파일도 없다. 실제 게시판에서 나온 사례를 그대로 재현한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from typing import Final

import pytest

from minjob_ingest.clock import KST
from minjob_ingest.domain import (
    Confidence,
    DedupState,
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
from minjob_ingest.store.base import DedupCandidate, DedupUpdate

_NOW: Final = datetime(2026, 8, 17, 9, 0, tzinfo=KST)
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
    assert plan([DedupCandidate(draft=draft, posted_on=_DAY)]) == ()


def test_a_general_job_is_locked_by_its_role() -> None:
    """일반직은 직분이 없고 직무가 있다 — 실측 `일심교회 사무간사` 2건이 이 모양이었다."""
    seat = seat_of(_draft(job_kind=(JobKind.GENERAL,), position=(), role="사무 간사"))

    assert seat == ("장성제일교회", "JEONNAM", "ROLE:사무간사")


def test_the_same_position_in_two_regions_is_not_the_same_seat() -> None:
    """⚠️ `온누리교회`는 서울·경기·대전·인천에 있다 — 지역이 키에 있어야 하는 이유다."""
    updates = plan(
        [
            _candidate(church_name="온누리교회", region=Region.SEOUL),
            _candidate(church_name="온누리교회", region=Region.GYEONGGI),
        ]
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]


# ── 중복 확정 ─────────────────────────────────────────────────────


def test_the_same_seat_on_two_boards_keeps_one() -> None:
    """실측 `장성제일교회 부목사` — HTUS와 PCK에 같은 날 올라왔다."""
    updates = plan([_candidate(), _candidate()])
    verdicts = {update.dedup_state: update for update in updates}

    assert set(verdicts) == {DedupState.MASTER, DedupState.DUPLICATE}
    duplicate = verdicts[DedupState.DUPLICATE]
    assert duplicate.verdict is not None
    assert duplicate.verdict.review_status is ReviewStatus.REJECTED
    assert duplicate.verdict.reject_reason is RejectReason.DUPLICATE


def test_every_row_in_a_group_shares_one_key() -> None:
    """키가 갈리면 admin이 "왜 이게 안 보이나"에 답할 수 없다."""
    updates = plan([_candidate(), _candidate(), _candidate()])

    assert len({update.dedup_key for update in updates}) == 1
    assert _states(updates).count(DedupState.DUPLICATE) == 2


def test_the_key_says_which_seat_it_is() -> None:
    """사람이 읽을 수 있어야 한다 — 해시로 줄이면 왜 합쳐졌는지 답할 수 없다."""
    (update,) = plan([_candidate(department=Department.CHILDREN)])

    assert update.dedup_key == "장성제일교회:JEONNAM:ASSOCIATE_PASTOR:CHILDREN:R1"


def test_a_posting_with_no_department_still_gets_a_key() -> None:
    """⚠️ 실측 69%가 부서를 말하지 않는다(담임목사는 원래 부서가 없다) — 그걸 판정에서 빼면
    가장 많이 교차게시되는 공고가 전부 중복으로 남는다."""
    (update,) = plan([_candidate()])

    assert update.dedup_key.endswith(":-:R1")
    assert update.dedup_state is DedupState.ALONE


def test_the_master_carries_the_newest_posting_date() -> None:
    """계속 끌어올린다 = 아직 뽑고 있다. min_job이 3개월 지난 공고를 숨기므로 최신이 맞다."""
    updates = _by_id(
        plan(
            [
                _candidate(on=date(2026, 7, 22), confidence=Confidence.MEDIUM),
                _candidate(on=date(2026, 7, 29), confidence=Confidence.MEDIUM),
            ]
        )
    )
    master = next(u for u in updates.values() if u.dedup_state is DedupState.MASTER)

    assert master.verdict is not None
    assert master.verdict.posted_at == date(2026, 7, 29)


# ── 라운드 ────────────────────────────────────────────────────────


def test_a_repost_within_three_months_is_the_same_round() -> None:
    """실측 `담임목사청빙(평강교회)` — PCKWORLD에 7/22, 7/29 두 번 올라왔다."""
    updates = plan(
        [_candidate(on=date(2026, 7, 22)), _candidate(on=date(2026, 7, 29))],
    )

    assert sorted(_states(updates), key=str) == [DedupState.DUPLICATE, DedupState.MASTER]


def test_a_gap_over_three_months_is_a_new_round() -> None:
    """⚠️ 다시 열린 자리는 **별개 공고**다 — 옛 묶음에 삼키면 새 공고가 사라진다."""
    updates = plan(
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

    updates = plan([_candidate(on=date(2026, 5, 4)), _candidate(on=date(2026, 8, 4))])

    assert DedupState.DUPLICATE in _states(updates)


def test_rounds_chain_through_the_middle_posting() -> None:
    """4개월 벌어진 두 글 사이에 한 글이 있으면 **셋이 한 라운드**다(계속 뽑고 있었다)."""
    updates = plan(
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
    updates = plan(
        [
            _candidate(department=Department.CHILDREN),
            _candidate(department=Department.YOUTH),
        ]
    )

    assert _states(updates) == [DedupState.ALONE, DedupState.ALONE]


def test_a_department_named_on_one_side_only_needs_a_person() -> None:
    """⚠️ 실측 `태평중앙교회`: CALVIN은 `사역자 모집`이라고만 하고 KBTUS는 `찬양인도`라고 했다.

    같은 자리일 가능성이 높지만 **코드가 알 수 없다**. 연락처가 같아도 마찬가지다 — 같다는
    것은 같은 자리라는 증거가 아니다(교회 대표번호를 여러 자리에 쓴다).
    """
    updates = plan([_candidate(), _candidate(department=Department.WORSHIP)])

    assert _states(updates) == [DedupState.UNCERTAIN, DedupState.UNCERTAIN]


def test_only_the_others_wait_when_we_cannot_tell() -> None:
    """운영자 결정(2026-08-17): 대표는 그대로 내보내고 **나머지만** 검수로 돌린다.

    같은 자리였다면 어차피 하나만 공개돼야 하니 결과가 맞고, 다른 자리였다면 운영자가
    승인하면 된다. 전원을 돌리면 같은 자리인 경우에도 두 건을 보게 된다.
    """
    updates = plan(
        [
            _candidate(department=Department.WORSHIP, description="찬양인도 사역자를 모십니다."),
            _candidate(),
        ]
    )
    statuses = sorted(
        update.verdict.review_status.value for update in updates if update.verdict is not None
    )

    assert statuses == ["APPROVED", "PENDING"], "대표만 그대로 나가고 나머지가 기다린다"


def test_an_uncertain_pair_can_be_found_by_the_shared_prefix() -> None:
    """키는 각자 제 부서를 말한다(거짓말하지 않는다) — 함께 찾는 것은 앞 3조각으로 한다."""
    updates = plan([_candidate(), _candidate(department=Department.WORSHIP)])
    keys = {update.dedup_key for update in updates}

    assert keys == {
        "장성제일교회:JEONNAM:ASSOCIATE_PASTOR:-:R1",
        "장성제일교회:JEONNAM:ASSOCIATE_PASTOR:WORSHIP:R1",
    }
    assert all(key.startswith("장성제일교회:JEONNAM:ASSOCIATE_PASTOR:") for key in keys)


# ── 연락처 ────────────────────────────────────────────────────────


def test_two_seats_with_different_contacts_stay_apart() -> None:
    """⚠️ 실측 `광림교회`: MTU 한 게시판에 같은 날 두 건이 올라왔는데 청장년부와 교회학교였다.

    담당자·마감일·이메일이 전부 달랐다 — 합쳤다면 지원자가 다른 부서에 다른 담당자에게
    지원할 기회가 사라진다. 부서가 둘 다 비어 있어 3단계는 이걸 못 잡는다.
    """
    updates = plan(
        [
            _candidate(contact_email="klmchwang93@gmail.com", contact_tel="010-4152-6410"),
            _candidate(contact_email="yoon4970@naver.com", contact_tel="010-7122-4970"),
        ]
    )

    assert _states(updates) == [DedupState.SEPARATE, DedupState.SEPARATE]
    for update in updates:
        assert update.verdict is not None
        assert update.verdict.reject_reason is None, "둘 다 그대로 살아 있다"
        assert update.verdict.review_status is ReviewStatus.APPROVED


def test_a_channel_only_one_side_filled_is_not_evidence() -> None:
    """⚠️ 실측 3묶음(세상의빛이레·이리성산·장성제일)이 이 모양이다 — 전화는 같은데 이메일을
    한쪽만 적었다. 침묵을 어긋남으로 세면 진짜 교차게시가 갈라진다."""
    updates = plan(
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
    updates = plan(
        [
            _candidate(contact_email=None, contact_tel="02-599-0056, 010-4874-9191"),
            _candidate(contact_email=None, contact_tel="010-4874-9191"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_two_numbers_that_share_nothing_are_two_seats() -> None:
    """겹치는 번호가 하나도 없으면 지원할 곳이 다르다(실측 `광림교회`)."""
    updates = plan(
        [
            _candidate(contact_email=None, contact_tel="010-4152-6410"),
            _candidate(contact_email=None, contact_tel="010-7122-4970"),
        ]
    )

    assert _states(updates) == [DedupState.SEPARATE, DedupState.SEPARATE]


def test_a_number_broken_by_spaces_is_not_compared() -> None:
    """⚠️ `010 4874 9191`을 `010`·`4874`·`9191`로 읽으면 겹치는 번호가 없다고 판정된다 —
    그런 값은 **아예 세지 않는** 쪽이 안전하다(안 세면 중복이 남을 뿐이다)."""
    updates = plan(
        [
            _candidate(contact_email=None, contact_tel="010 4874 9191"),
            _candidate(contact_email=None, contact_tel="010-4874-9191"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_two_emails_in_one_field_still_match() -> None:
    updates = plan(
        [
            _candidate(contact_email="apply@seire.org, office@seire.org"),
            _candidate(contact_email="apply@seire.org"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_nothing_to_compare_still_merges() -> None:
    """실측 `장성제일교회` PCK 건은 연락처가 아예 없었다 — 4단계는 **막는 근거**만 본다."""
    updates = plan(
        [
            _candidate(contact_email=None),
            _candidate(contact_email="shoutlord@hanmail.net"),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_the_phone_is_compared_by_its_digits() -> None:
    """게시판마다 표기가 갈린다(`02-793-9686` / `027939686`) — 글자로 견주면 같은 곳이 갈린다."""
    updates = plan(
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
    updates = plan(
        [
            _candidate(contact_email=None, contact_link=left),
            _candidate(contact_email=None, contact_link=right),
        ]
    )

    assert DedupState.DUPLICATE in _states(updates)


def test_two_different_apply_links_are_two_seats() -> None:
    """지원 양식이 다르면 지원할 곳이 다르다."""
    updates = plan(
        [
            _candidate(contact_email=None, contact_link="https://forms.gle/abc"),
            _candidate(contact_email=None, contact_link="https://forms.gle/xyz"),
        ]
    )

    assert _states(updates) == [DedupState.SEPARATE, DedupState.SEPARATE]


def test_a_contact_that_does_not_look_like_one_is_still_compared() -> None:
    """⚠️ 꼴이 안 맞는 값을 버리면 **그 채널이 조용히 무력해진다** — 서로 다른 곳인데 비교할
    것이 없어 묶여버린다. 못 쪼갠 값은 통째로 하나의 조각으로 둔다.

    실제로 이런 값이 온다: 모델이 `apply @ seire.org`처럼 공백을 끼워 넣으면 이메일 꼴이
    깨지는데, 검산은 원문에 그 글자가 있으면 통과시킨다(SPEC §5.5b).
    """
    updates = plan(
        [
            _candidate(contact_email="apply @ seire.org"),
            _candidate(contact_email="office@other.org"),
        ]
    )

    assert _states(updates) == [DedupState.SEPARATE, DedupState.SEPARATE]


def test_the_postal_address_does_not_split_a_group() -> None:
    """⚠️ `contact_post`는 검산을 거치지 않는 조립 칸이다(SPEC §5.5b) — 어긋남이 모델 탓인지
    원문 탓인지 구분되지 않아 가르는 근거로 쓰지 않는다."""
    updates = plan(
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

    updates = _by_id(plan([poor, rich]))

    assert updates[str(rich.draft.id)].dedup_state is DedupState.MASTER
    assert updates[str(poor.draft.id)].dedup_state is DedupState.DUPLICATE


def test_an_auto_approved_draft_wins_over_a_fuller_one_that_needs_review() -> None:
    """포스터 공고(medium)가 대표가 되면 검수가 하나 늘어난다 — 등급이 충실함보다 앞이다."""
    approved = _candidate()
    fuller = _candidate(
        confidence=Confidence.MEDIUM, pay_note="월 250만원", benefit_note="사택", role=None
    )

    updates = _by_id(plan([fuller, approved]))

    assert updates[str(approved.draft.id)].dedup_state is DedupState.MASTER


def test_the_master_is_the_same_whichever_order_they_arrive_in() -> None:
    """⚠️ 순서에 따라 대표가 바뀌면 같은 데이터에서 다른 결과가 나온다(멱등이 깨진다)."""
    first, second, third = _candidate(), _candidate(), _candidate()

    forward = _by_id(plan([first, second, third]))
    backward = _by_id(plan([third, second, first]))

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

    updates = _by_id(plan([fresh, published]))

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

    updates = _by_id(plan([touched, _candidate()]))

    assert updates[str(touched.draft.id)].verdict is None


def test_two_rows_a_person_already_saw_are_left_to_the_person() -> None:
    """어느 쪽을 내릴지 코드가 고를 수 없다(둘 다 승인·게재됐을 수 있다)."""
    left = _candidate(published_job_id=new_id())
    right = _candidate(published_job_id=new_id())

    updates = plan([left, right])

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

    (update,) = plan([rejected])

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

    (update,) = plan([rejected])

    assert update.verdict is not None
    assert update.verdict.review_status is ReviewStatus.PENDING


@pytest.mark.parametrize(
    "reason", [RejectReason.HERESY, RejectReason.CLOSED, RejectReason.OPERATOR]
)
def test_a_settled_rejection_is_left_alone(reason: RejectReason) -> None:
    """⚠️ 이단·마감 거절 행이 대표가 되면 **다른 게시판의 살아 있는 같은 자리 공고가 그 밑에
    중복으로 묻힌다**. 운영자 거절도 사람의 결론이라 건드리지 않는다."""
    settled = _candidate(review_status=ReviewStatus.REJECTED, reject_reason=reason)

    updates = plan([settled, _candidate()])

    assert _states(updates) == [DedupState.ALONE], "살아 있는 한 건만 판정된다"


def test_the_same_input_gives_the_same_plan() -> None:
    """멱등 — 두 번 돌려도 같은 판정이 나온다."""
    candidates = [_candidate(), _candidate(), _candidate(department=Department.YOUTH)]

    assert plan(candidates) == plan(candidates)


def test_applying_the_plan_twice_changes_nothing_the_second_time() -> None:
    """판정을 반영한 상태로 다시 돌려도 결론이 같아야 한다 — 아니면 매 실행 상태가 흔들린다."""
    first, second = _candidate(), _candidate()
    updates = _by_id(plan([first, second]))

    applied = [_after(candidate, updates[str(candidate.draft.id)]) for candidate in (first, second)]

    again = _by_id(plan(applied))

    assert {key: value.dedup_state for key, value in again.items()} == {
        key: value.dedup_state for key, value in updates.items()
    }
