"""등급 테스트 — 어떤 초안을 사람이 안 보고 공개하나(SPEC §5.7).

`high`는 **사람을 거치지 않고 공개된다.** 여기서 규칙이 느슨해지면 확인 안 된 값이
그대로 나간다. 모델도 네트워크도 없다 — 조립된 레코드만 본다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from typing import Final

import pytest

from minjob_ingest.clock import KST
from minjob_ingest.domain import (
    Confidence,
    DenominationSource,
    IsChurchRecruitment,
    JobKind,
    Position,
    ReviewStatus,
)
from minjob_ingest.models import ReviewData, new_id
from minjob_ingest.pipeline.confidence import PROMOTION_FIELDS, grade, missing_for_promotion

_NOW: Final = datetime(2026, 8, 17, 9, 0, tzinfo=KST)


def _draft(**overrides: object) -> ReviewData:
    """승격 6칸이 **다 찬** 초안. 검사마다 한 칸씩 비워 본다."""
    base: dict[str, object] = {
        "source_data_id": new_id(),
        "run_id": new_id(),
        "source_url": "https://example.kr/board/1",
        "is_church_recruitment": IsChurchRecruitment.YES,
        "confidence": Confidence.LOW,
        "denomination_source": DenominationSource.UNKNOWN,
        "job_kind": (JobKind.MINISTRY,),
        "position": (Position.ASSOCIATE_PASTOR,),
        "title": "성원교회에서 부목사를 청빙합니다",
        "description": "성원교회가 부목사를 청빙합니다.",
        "church_name": "성원교회",
        "contact_email": "church@example.kr",
        "posted_at": date(2026, 8, 17),
        "created_at": _NOW,
    }
    base.update(overrides)
    return ReviewData(**base)  # type: ignore[arg-type]


def _graded(
    draft: ReviewData, *, media_sent: bool = False, media_missed: bool = False
) -> Confidence:
    return grade(draft, media_sent=media_sent, media_missed=media_missed)


# ── 승격 6칸 ─────────────────────────────────────────────────────


def test_a_complete_draft_needs_no_one_to_look_at_it() -> None:
    assert _graded(_draft()) is Confidence.HIGH
    assert missing_for_promotion(_draft()) == ()


@pytest.mark.parametrize(
    ("blanked", "reported"),
    [
        ({"church_name": None}, "church_name"),
        ({"title": None}, "title"),
        ({"job_kind": (), "position": ()}, "job_kind"),
        ({"description": None}, "description"),
        ({"contact_email": None}, "contact"),
    ],
    ids=lambda value: str(value)[:28],
)
def test_a_missing_promotion_field_needs_a_person(
    blanked: dict[str, object], reported: str
) -> None:
    """⚠️ 승격 6칸(SPEC §6) 중 하나라도 비면 **min_job이 공개를 거부한다** — 사람이 채워야 한다.

    빈 칸 이름을 함께 돌려준다: 개수만 있으면 프롬프트를 어디부터 고칠지 알 수 없다.
    """
    draft = _draft(**blanked)

    assert _graded(draft) is Confidence.LOW
    assert reported in missing_for_promotion(draft)


def test_a_role_stands_in_for_a_position() -> None:
    """일반직은 직분이 없고 직무가 있다 — 넷째 칸은 **둘 중 하나**면 찬다(min_job CHECK ①)."""
    draft = _draft(job_kind=(JobKind.GENERAL,), position=(), role="행정·사무")

    assert missing_for_promotion(draft) == ()


@pytest.mark.parametrize(
    "field_name",
    ["contact_email", "contact_tel", "contact_link", "contact_post"],
    ids=lambda value: str(value),
)
def test_any_one_contact_is_enough(field_name: str) -> None:
    """min_job `APPLY_METHODS`가 닫힌 4키라 **하나만** 있으면 지원할 수 있다(CHECK ②)."""
    only_one: dict[str, object] = dict.fromkeys(
        ("contact_email", "contact_tel", "contact_link", "contact_post"), None
    )
    only_one[field_name] = "값"

    assert missing_for_promotion(_draft(**only_one)) == ()


def test_the_source_url_does_not_count_as_a_contact() -> None:
    """⚠️ `source_url`은 항상 있다 — 세면 연락처 제약이 늘 참이 되어 무의미해진다(SPEC §6)."""
    draft = _draft(contact_email=None)

    assert "contact" in missing_for_promotion(draft)
    assert _graded(draft) is Confidence.LOW


def test_every_promotion_field_is_reachable() -> None:
    """⚠️ 이름 하나를 오타내면 그 칸은 영영 검사되지 않는다 — 드리프트를 여기서 잡는다."""
    reported = {
        name
        for blanked in (
            {"church_name": None},
            {"title": None},
            {"job_kind": (), "position": ()},
            {"description": None},
            {"contact_email": None},
        )
        for name in missing_for_promotion(_draft(**blanked))
    }

    assert reported == set(PROMOTION_FIELDS)


def test_the_position_or_role_check_is_a_guard_not_a_live_rule() -> None:
    """⚠️ 이 칸은 **레코드 불변식이 이미 보장한다** — `job_kind`에 MINISTRY가 있으면 직분이,
    GENERAL이 있으면 직무가 있어야 레코드가 만들어진다(`models._check_job_kind`).

    그래서 `job_kind`가 비지 않는 한 이 칸은 빌 수 없다. 지우지 않는 이유는 **불변식이
    바뀌면 승격이 조용히 깨지기 때문**이다 — 그때 이 검사가 잡는다.
    """
    with pytest.raises(ValueError, match="role이 어긋남"):
        _draft(job_kind=(JobKind.GENERAL,), position=(), role=None)

    assert missing_for_promotion(_draft(job_kind=(), position=())) == (
        "job_kind",
        "position_or_role",
    )


# ── 사람이 봐야 하는 나머지 이유 ─────────────────────────────────


def test_an_uncertain_posting_needs_a_person() -> None:
    """게이트1 `UNCERTAIN`은 개교회인지 판단이 필요하다 — 레코드 불변식도 `low`를 요구한다."""
    assert _graded(_draft(is_church_recruitment=IsChurchRecruitment.UNCERTAIN)) is Confidence.LOW


def test_a_posting_whose_image_never_arrived_needs_a_person() -> None:
    """⚠️ 그림을 **못 받은** 것은 보낸 것과 다르다 — 내용 자체가 없을 수 있다(SPEC §3)."""
    assert _graded(_draft(), media_missed=True) is Confidence.LOW


def test_a_poster_posting_is_looked_at_but_not_repaired() -> None:
    """⚠️ 그림을 보낸 공고는 `verify`가 비우지 않고 세기만 한다 — **어느 칸도 원문과 대조된 적이
    없다**(SPEC §5.5b). 자동 승인하면 "확인했다"는 말이 거짓이 된다.

    실측: 사람이 볼 24건 중 21건(88%)이 이 경우다 — 전량 환산 ≈554건 중 대부분.
    """
    assert _graded(_draft(), media_sent=True) is Confidence.MEDIUM


def test_a_posting_whose_only_contact_is_a_postal_address_needs_a_person() -> None:
    """⚠️ `contact_post`는 **원문 대조를 거치지 않는 유일한 연락처**다(SPEC §5.5b) — 나머지
    셋은 원문에 없으면 `verify`가 비운다.

    이것뿐이면 지원 경로 전체가 확인된 적 없는 값이라 자동 승인하지 않는다. 실측 132건 중
    0건이라 검수량은 늘지 않는다(`contact_post`가 있는 14건은 전부 다른 연락처를 함께 가졌다).
    """
    only_post: dict[str, object] = dict.fromkeys(
        ("contact_email", "contact_tel", "contact_link"), None
    )
    only_post["contact_post"] = "서울시 종로구 …"

    assert missing_for_promotion(_draft(**only_post)) == (), "승격은 된다 — 검수만 거친다"
    assert _graded(_draft(**only_post)) is Confidence.MEDIUM


def test_a_postal_address_beside_another_contact_is_fine() -> None:
    """실측 14건이 이 모양이다 — 대조된 연락처가 함께 있으면 확인할 것이 없다."""
    assert _graded(_draft(contact_post="서울시 종로구 …")) is Confidence.HIGH


def test_a_posting_on_the_heresy_list_never_goes_out_by_itself() -> None:
    """⚠️ 이단 목록에 걸린 공고는 **자동 공개되면 안 된다**(SPEC §5.4 · 2026-08-19).

    지역까지 확인해 거절한 건은 어차피 `REJECTED`가 되지만, 지역을 못 본 교회명은 동명이교회일
    수 있어 사람이 정한다 — 그 행이 `high`로 나가면 무고한 교회를 이단으로 공개하거나(표시가
    함께 나가면) 확인 안 된 판정을 공개하는 셈이다.
    """
    draft = _draft(heresy_flag=True, heresy_evidence="목록: 아무개 · ⚠️ 지역 확인 불가")

    assert _graded(draft) is Confidence.MEDIUM


# ── 제목 대조는 넣었다가 뺐다 ─────────────────────────────────────


def test_a_title_that_names_the_church_differently_is_not_a_reason_to_look() -> None:
    """⚠️ 제목·교회명 대조 규칙을 넣었다가 뺐다(2026-08-17).

    제목은 모델이 만들 수 없는 유일한 두 번째 출처라 기대했지만, 실측 138건에서 **적발 3건이
    전부 오탐**이었다 — 대괄호가 부분일치를 깨거나(`[대구] 영광교회` vs `대구영광교회`),
    같은 교회를 달리 적었거나(`대전한밭제일장로교회` vs `한밭제일교회`), 제목의 `교회`가
    일반명사였다. **참 적발 0 · 헛검수 3.** 다시 넣으려면 실측 근거를 먼저 가져온다.
    """
    assert _graded(_draft(title="[대전]대전한밭제일장로교회 청빙 공고")) is Confidence.HIGH
    assert _graded(_draft(title="교회 후임자 구합니다.")) is Confidence.HIGH


# ── 등급이 검수 상태를 정한다 ────────────────────────────────────


def test_the_grade_is_one_of_the_three_agreed_values() -> None:
    """⚠️ min_job과의 계약이라 값을 늘릴 수 없다(CONTRACT §1)."""
    grades = {
        _graded(_draft()),
        _graded(_draft(), media_sent=True),
        _graded(_draft(church_name=None)),
    }

    assert grades == {Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW}


def test_a_high_draft_is_still_a_pending_record_on_its_own() -> None:
    """⚠️ 등급은 상태를 **직접** 바꾸지 않는다 — `build_draft`가 옮긴다(SPEC §5.7).

    여기서 상태까지 정하면 거절(이단·마감)보다 앞설 수 있다.
    """
    assert _draft().review_status is ReviewStatus.PENDING
    assert replace(_draft(), confidence=Confidence.HIGH).review_status is ReviewStatus.PENDING
