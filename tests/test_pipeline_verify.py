"""검산 테스트 — 모델 답이 원문에 있나.

⚠️ **이 모듈이 있는 이유가 여기 있다.** 실측 284개 값 중 44개(15%)를 모델이 고쳐 썼고,
그것을 알아내려면 원문과 손으로 대조해야 했다. 여기서는 Gemini 없이 검사된다.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Final

import pytest

from minjob_ingest.clock import KST
from minjob_ingest.domain import (
    Department,
    EmploymentType,
    IsChurchRecruitment,
    Position,
    Qualification,
    Region,
    StipendPeriod,
)
from minjob_ingest.models import Attachment, JsonValue, SourceData, new_id
from minjob_ingest.pipeline.extraction import Evidence, Extraction
from minjob_ingest.pipeline.verify import _SUPPORTING_WORDS, VerifyReport
from minjob_ingest.pipeline.verify import verify as _verify


def verify(
    record: SourceData, extraction: Extraction, *, media_sent: bool = False
) -> tuple[Extraction, VerifyReport]:
    """`media_sent` 기본값을 **거짓**으로 둔다 — 대부분의 검사가 "비우는가"를 본다.

    ⚠️ 실제 호출자(`structure._judge`)는 이 인자를 반드시 넘긴다. 기본값은 테스트 편의다.
    """
    return _verify(record, extraction, media_sent=media_sent)


_NOW: Final = datetime(2026, 8, 12, 9, 0, tzinfo=KST)
_BODY: Final = (
    "성원교회에서 부목사 1명을 청빙합니다.\n"
    "모집부서: 중고등부 (전임)\n"
    "지원자격: 총회인준 신학대학원 졸업자\n"
    "제출서류: 이력서, 자기소개서, 가족관계증명서 각1통\n"
    "사례비: 월 250만원\n"
    "지역: 경북 문경시 점촌동"
)


def _record(
    *,
    raw_text: str = _BODY,
    raw_meta: dict[str, JsonValue] | None = None,
    image_urls: tuple[str, ...] = (),
    attachments: tuple[Attachment, ...] = (),
) -> SourceData:
    return SourceData(
        source_key="DAESHIN",
        external_id="37",
        source_url="https://example.kr/board/37",
        title="성원교회 부목사 청빙",
        run_id=new_id(),
        fetched_at=_NOW,
        raw_text=raw_text,
        raw_meta=raw_meta or {},
        image_urls=image_urls,
        attachments=attachments,
    )


def _extraction(**overrides: object) -> Extraction:
    base: dict[str, object] = {
        "is_church_recruitment": IsChurchRecruitment.YES,
        "church_name": "성원교회",
        "description": "성원교회가 부목사를 청빙합니다.",
    }
    base.update(overrides)
    return Extraction(**base)  # type: ignore[arg-type]


# ── 원문에 있는 값은 살아남는다 ──────────────────────────────────


def test_a_value_copied_from_the_body_survives() -> None:
    verified, report = verify(_record(), _extraction(headcount="부목사 1명"))

    assert verified.headcount == "부목사 1명"
    assert report.is_clean


def test_whitespace_differences_do_not_matter() -> None:
    """원문이 줄을 바꾼 자리에서 모델이 붙여 쓰는 일이 흔하다 — 글자만 견준다."""
    verified, report = verify(_record(), _extraction(headcount="부목사1명을청빙합니다"))

    assert verified.headcount == "부목사1명을청빙합니다"
    assert report.is_clean


def test_a_board_field_counts_as_the_source() -> None:
    """⚠️ `CSU`는 교회명·교단·사례비가 본문이 아니라 게시판 필드에 있다(730건)."""
    record = _record(raw_text="", raw_meta={"order_name": "예장합동"})

    verified, _ = verify(record, _extraction(raw_denomination="예장합동"))

    assert verified.raw_denomination == "예장합동"


def test_an_attachment_name_counts_as_the_source() -> None:
    record = _record(
        raw_text="",
        attachments=(Attachment(name="구례중앙교회 청빙공고.hwp", url="https://e.kr/a"),),
    )

    verified, _ = verify(record, _extraction(church_name="구례중앙교회"))

    assert verified.church_name == "구례중앙교회"


# ── 원문에 없는 값은 비운다 ──────────────────────────────────────


def test_a_recomposed_list_item_is_counted_not_dropped() -> None:
    """⚠️ **여기서 설계가 한 번 뒤집혔다.** 원문 `가족관계증명서, 주민등록 등본 각1통`을 모델이
    `가족관계증명서 1통` + `주민등록 등본 1통`으로 나눈 것은 **맞는 답**이다.

    실측 20건에서 목록 칸이 23개 어긋났는데 **지어낸 것은 0개**였다 — 전부 프롬프트가 시킨
    조립·분배였다. 비우면 맞는 값을 잃는다. 세어서 리포트로만 올린다.
    """
    verified, report = verify(
        _record(), _extraction(required_docs=("이력서", "가족관계증명서 1통"))
    )

    assert verified.required_docs == ("이력서", "가족관계증명서 1통")
    assert report.scrubbed == ()
    assert report.unchecked == 1
    assert report.unchecked_fields == {"required_docs": 1}


def test_an_aggregating_note_is_counted_not_dropped() -> None:
    """`benefit_note`는 자리마다 다른 처우를 **한 줄로 잇는** 칸이다(`전임: … / 교육: …`).

    실측: 이 칸에서 비워진 6건이 전부 조립이었고 지어낸 것은 없었다.
    """
    verified, report = verify(_record(), _extraction(benefit_note="4대 보험 / 퇴직금"))

    assert verified.benefit_note == "4대 보험 / 퇴직금"
    assert report.unchecked == 1


def test_an_invented_text_field_becomes_empty() -> None:
    verified, report = verify(_record(), _extraction(work_days="주 6일 근무"))

    assert verified.work_days is None
    assert "work_days" in report.scrubbed


def test_the_posting_itself_is_never_dropped() -> None:
    """검산은 칸을 비울 뿐 공고를 없애지 않는다."""
    verified, _ = verify(_record(), _extraction(work_days="지어낸 값", headcount="지어낸 값"))

    assert verified.is_church_recruitment is IsChurchRecruitment.YES
    assert verified.church_name == "성원교회"


# ── 대문자 값은 근거로 검산한다 ──────────────────────────────────


def test_a_choice_with_grounded_evidence_survives() -> None:
    verified, report = verify(
        _record(),
        _extraction(
            position=(Position.ASSOCIATE_PASTOR,),
            evidence=Evidence(position="부목사 1명"),
        ),
    )

    assert verified.position == (Position.ASSOCIATE_PASTOR,)
    assert report.is_clean


def test_a_choice_without_evidence_is_dropped() -> None:
    """근거를 못 대면 가리킬 데가 없다는 뜻이다."""
    verified, report = verify(_record(), _extraction(position=(Position.ASSOCIATE_PASTOR,)))

    assert verified.position == ()
    assert "position" in report.scrubbed


def test_evidence_that_is_not_in_the_source_is_dropped() -> None:
    """⚠️ 근거를 지어내는 경로를 막는다 — 값만 보면 멀쩡해 보인다."""
    verified, report = verify(
        _record(),
        _extraction(
            position=(Position.SENIOR_PASTOR,),
            evidence=Evidence(position="담임목사를 청빙합니다"),
        ),
    )

    assert verified.position == ()
    assert "position" in report.scrubbed


def test_evidence_that_does_not_support_the_value_is_dropped() -> None:
    """⚠️ 이것이 담임목사 오검출을 막는 자리다.

    실측(Flash-Lite 3건): 연락처의 `담임목사 김○○`을 모집 직분으로 읽었다. 근거가 원문에
    있어도 그 글자가 `담임`을 말하지 않으면 SENIOR_PASTOR를 뒷받침하지 못한다.
    """
    verified, _ = verify(
        _record(),
        _extraction(
            position=(Position.SENIOR_PASTOR,),
            evidence=Evidence(position="부목사 1명"),
        ),
    )

    assert verified.position == ()


def test_only_the_unsupported_value_leaves_a_multi_valued_choice() -> None:
    verified, _ = verify(
        _record(),
        _extraction(
            position=(Position.ASSOCIATE_PASTOR, Position.SENIOR_PASTOR),
            evidence=Evidence(position="부목사 1명"),
        ),
    )

    assert verified.position == (Position.ASSOCIATE_PASTOR,)


def test_etc_needs_no_supporting_word() -> None:
    """`그 밖`이라 뒷받침할 낱말이 없다 — 근거가 원문에 있으면 통과."""
    verified, _ = verify(
        _record(),
        _extraction(position=(Position.ETC,), evidence=Evidence(position="부목사 1명")),
    )

    assert verified.position == (Position.ETC,)


@pytest.mark.parametrize(
    ("field_name", "value", "evidence_text"),
    [
        ("department", Department.YOUTH, "중고등부"),
        ("employment_type", EmploymentType.FULL_TIME, "전임"),
        ("qualification", Qualification.SEMINARIAN, "총회인준 신학대학원 졸업자"),
    ],
    ids=["department", "employment_type", "qualification"],
)
def test_single_valued_choices_are_checked_the_same_way(
    field_name: str, value: object, evidence_text: str
) -> None:
    grounded, _ = verify(
        _record(),
        _extraction(**{field_name: value}, evidence=Evidence(**{field_name: evidence_text})),
    )
    invented, report = verify(
        _record(),
        _extraction(**{field_name: value}, evidence=Evidence(**{field_name: "원문에 없는 근거"})),
    )

    assert getattr(grounded, field_name) is value
    assert getattr(invented, field_name) is None
    assert field_name in report.scrubbed


# ── 코드가 만든 값은 그 근거로 검산한다 ──────────────────────────


def test_a_pay_amount_that_is_not_in_the_source_clears_the_numbers() -> None:
    """⚠️ 3200이라는 **숫자**만 보면 멀쩡하다 — 그 숫자를 만든 표현을 봐야 지어낸 걸 안다."""
    verified, report = verify(
        _record(),
        _extraction(
            pay_min=3200,
            pay_period=StipendPeriod.YEAR,
            evidence=Evidence(pay_amount="연봉 3,200이상"),
        ),
    )

    assert (verified.pay_min, verified.pay_period) == (None, None)
    assert "pay_amount" in report.scrubbed


def test_a_grounded_pay_amount_keeps_the_numbers() -> None:
    verified, report = verify(
        _record(),
        _extraction(
            pay_min=250, pay_period=StipendPeriod.MONTH, evidence=Evidence(pay_amount="월 250만원")
        ),
    )

    assert (verified.pay_min, verified.pay_period) == (250, StipendPeriod.MONTH)
    assert report.is_clean


def test_an_invented_location_clears_the_region() -> None:
    verified, report = verify(
        _record(),
        _extraction(
            region=Region.SEOUL, city="강남구", evidence=Evidence(location="서울 강남구 역삼동")
        ),
    )

    assert (verified.region, verified.city) == (None, None)
    assert "location" in report.scrubbed


def test_a_grounded_location_keeps_the_region() -> None:
    verified, _ = verify(
        _record(),
        _extraction(
            region=Region.GYEONGBUK, city="문경시", evidence=Evidence(location="경북 문경시 점촌동")
        ),
    )

    assert (verified.region, verified.city) == (Region.GYEONGBUK, "문경시")


# ── 그림·PDF 공고는 비우지 않는다 ────────────────────────────────


def test_a_posting_with_a_poster_is_never_scrubbed() -> None:
    """⚠️ 포스터에만 있는 값은 본문에 없는 것이 **정상**이다 — 지어낸 것과 구분할 수 없다.

    여기서 비우면 본문이 없는 포스터 공고 117건이 통째로 빈 채 저장된다.
    """
    record = _record(image_urls=("https://e.kr/poster.png",))

    verified, report = verify(
        record, _extraction(work_days="주 5일", church_name="지어낸교회"), media_sent=True
    )

    assert verified.work_days == "주 5일"
    assert verified.church_name == "지어낸교회"
    assert report.scrubbed == ()
    assert report.unverifiable == 2


def test_a_posting_with_a_pdf_is_never_scrubbed() -> None:
    record = _record(
        attachments=(Attachment(name="청빙공고문.pdf", url="https://e.kr/a.pdf"),),
    )

    verified, report = verify(record, _extraction(work_days="주 5일"), media_sent=True)

    assert verified.work_days == "주 5일"
    assert report.unverifiable == 1


def test_a_hwp_attachment_does_not_exempt_the_posting() -> None:
    """⚠️ HWP는 모델에 보내지 않는다 — 거기서 왔을 수가 없으니 면제 사유가 아니다."""
    record = _record(attachments=(Attachment(name="이력서양식.hwp", url="https://e.kr/a.hwp"),))

    verified, report = verify(record, _extraction(work_days="주 5일"))

    assert verified.work_days is None
    assert "work_days" in report.scrubbed


# ── 검산하지 않는 칸 ─────────────────────────────────────────────


def test_the_summary_is_not_checked_against_the_source() -> None:
    """`description`은 **새로 쓰는 글**이다 — 원문에 있을 리가 없다."""
    verified, report = verify(_record(), _extraction(description="성원교회가 부목사를 모십니다."))

    assert verified.description == "성원교회가 부목사를 모십니다."
    assert report.is_clean


def test_a_phone_is_checked_by_its_digits() -> None:
    """⚠️ 프롬프트가 `010-2720-구육구이`를 되돌리라 시킨다 — 글자로는 원문과 다르다.

    그래서 **숫자만** 견준다. 되돌린 뒤의 숫자열은 원문 숫자열 안에 있어야 한다.
    실측 20건 중 15건이 이 방식으로 확인된다.
    """
    record = _record(raw_text=_BODY + "\n문의: 010-2720-구육구이")

    grounded, report = verify(record, _extraction(contact_tel="010-2720-9692"))
    invented, _ = verify(record, _extraction(contact_tel="010-9999-0000"))

    assert grounded.contact_tel == "010-2720-9692"
    assert report.is_clean
    assert invented.contact_tel is None


def test_a_link_is_compared_by_letters_only() -> None:
    """스킴·구두점을 지우고 견준다 — 모델이 `https://`를 붙이거나 떼도 같은 링크다."""
    record = _record(raw_text=_BODY + "\n홈페이지 www.sungwon.or.kr")

    grounded, _ = verify(record, _extraction(contact_link="https://www.sungwon.or.kr"))
    invented, _ = verify(record, _extraction(contact_link="https://fake.example.kr"))

    assert grounded.contact_link == "https://www.sungwon.or.kr"
    assert invented.contact_link is None


def test_a_link_whose_typo_the_model_fixed_is_kept() -> None:
    """⚠️ 실측: 원문이 `홈페이지:www,guryejungangchurch.com`(쉼표 오타)인데 Flash 가 점으로
    고쳤고 Flash-Lite 는 오타를 그대로 옮겼다.

    글자를 그대로 견주면 **고친 답이 버려지고 깨진 URL 이 살아남는다** — 검산이 더 나쁜
    답을 고르게 된다.
    """
    record = _record(raw_text=_BODY + "\n홈페이지:www,guryejungangchurch.com")

    fixed, report = verify(record, _extraction(contact_link="www.guryejungangchurch.com"))

    assert fixed.contact_link == "www.guryejungangchurch.com"
    assert report.is_clean


def test_an_email_must_appear_in_the_source() -> None:
    """⚠️ 이메일은 고쳐 쓸 이유가 없다 — 실측 20건 중 19건이 원문에 글자 그대로 있었다."""
    record = _record(raw_text=_BODY + "\n지원: owenpej@naver.com")

    grounded, _ = verify(record, _extraction(contact_email="owenpej@naver.com"))
    invented, report = verify(record, _extraction(contact_email="fake@naver.com"))

    assert grounded.contact_email == "owenpej@naver.com"
    assert invented.contact_email is None
    assert "contact_email" in report.scrubbed


def test_the_gate_is_not_checked() -> None:
    """개교회인지 기관인지는 글 전체를 읽고 내리는 판단이라 가리킬 한 곳이 없다."""
    verified, report = verify(
        _record(), _extraction(is_church_recruitment=IsChurchRecruitment.UNCERTAIN)
    )

    assert verified.is_church_recruitment is IsChurchRecruitment.UNCERTAIN
    assert report.is_clean


def test_the_title_is_part_of_the_source() -> None:
    """⚠️ 제목에만 있는 값이 흔하다 — `반야월교회에서 전임부목사, 교육전도사를 청빙합니다`.

    haystack 에서 제목이 빠지면 그 값들이 통째로 비워진다.
    """
    record = _record(raw_text="자세한 내용은 첨부를 보세요.")

    verified, report = verify(record, _extraction(church_name="성원교회 부목사 청빙"))

    assert verified.church_name == "성원교회 부목사 청빙"
    assert report.is_clean


def test_a_value_with_no_evidence_field_is_not_blamed() -> None:
    """⚠️ 근거 칸이 `None`인 것은 **모델이 값을 안 낸 것**이다 — 파생값도 이미 비어 있다.

    이걸 실패로 보면 사례비·지역이 없는 공고마다 헛된 경보가 쌓인다.
    """
    verified, report = verify(_record(), _extraction())

    assert (verified.pay_min, verified.region) == (None, None)
    assert report.is_clean


@pytest.mark.parametrize(
    ("value", "evidence_text"),
    [
        (Department.YOUTH, "총회인준 신학대학원 졸업자"),
        (EmploymentType.FULL_TIME, "중고등부"),
        (Qualification.ORDAINED, "중고등부"),
    ],
    ids=["부서 ← 자격 문구", "고용형태 ← 부서 문구", "자격 ← 부서 문구"],
)
def test_evidence_from_the_wrong_field_does_not_support_a_choice(
    value: object, evidence_text: str
) -> None:
    """⚠️ 근거가 **원문에 있기만 하면** 통과하던 구멍을 막는다.

    모델이 아무 문장이나 근거로 붙여도 그 칸의 낱말이 없으면 뒷받침하지 못한다.
    """
    field_name = {
        "Department": "department",
        "EmploymentType": "employment_type",
        "Qualification": "qualification",
    }[type(value).__name__]

    verified, report = verify(
        _record(),
        _extraction(**{field_name: value}, evidence=Evidence(**{field_name: evidence_text})),
    )

    assert getattr(verified, field_name) is None
    assert field_name in report.scrubbed


def test_a_derived_value_without_its_evidence_is_kept_not_blamed() -> None:
    """⚠️ 근거 칸이 비었는데 파생값이 있는 상태는 `parse_extraction`으로는 만들어지지 않는다
    (파생값이 근거에서 나오므로). 그래도 계약으로 못 박는다 — 나중에 다른 경로가 생겨
    이 분기가 실패로 바뀌면 사례비·지역이 조용히 사라진다.
    """
    verified, report = verify(_record(), _extraction(pay_min=250, region=Region.GYEONGBUK))

    assert (verified.pay_min, verified.region) == (250, Region.GYEONGBUK)
    assert report.is_clean


def test_an_invented_deadline_is_dropped() -> None:
    """⚠️ 날짜만 보면 지어낸 것도 멀쩡하다 — `2026-09-30`은 그 자체로 흠이 없다.

    마감일이 틀리면 살아 있는 공고가 목록에서 사라지거나(이른 날짜) 끝난 공고가 남는다.
    """
    verified, report = verify(
        _record(), _extraction(deadline=date(2026, 9, 30), evidence=Evidence(deadline="2026-09-30"))
    )

    assert verified.deadline is None
    assert "deadline" in report.scrubbed


def test_a_grounded_deadline_survives() -> None:
    record = _record(raw_text=_BODY + "\n접수기한: 2026-08-31까지")

    verified, _ = verify(
        record,
        _extraction(deadline=date(2026, 8, 31), evidence=Evidence(deadline="2026-08-31까지")),
    )

    assert verified.deadline == date(2026, 8, 31)


@pytest.mark.parametrize(
    "enum_type",
    [Position, Department, EmploymentType, Qualification],
    ids=lambda value: str(value.__name__),
)
def test_every_choice_value_has_supporting_words(enum_type: type[StrEnum]) -> None:
    """⚠️ 표에 없는 값은 `supports`가 **조용히 통과**시킨다 — 검산이 그 값에만 사라진다.

    enum에 값을 더하면 이 테스트가 먼저 실패해 표를 같이 고치게 한다.
    `ETC`만 예외다: "그 밖"이라 뒷받침할 낱말이 없다.
    """
    uncovered = {member.value for member in enum_type if member not in _SUPPORTING_WORDS} - {"ETC"}

    assert uncovered == set(), f"{enum_type.__name__}: 낱말표에 없는 값 {sorted(uncovered)}"


def test_a_contact_line_does_not_justify_a_senior_pastor() -> None:
    """⚠️ **이것이 담임목사 오검출의 실제 모양이다.** 실측 1,336건이 `담임목사:` 꼴을 담는다.

    근거가 원문에 있고 `담임`도 담고 있어서 낱말 검사만으로는 통과한다 — 그래서 직분에는
    **모집한다는 말**까지 요구한다. 담임 청빙과 부교역자 청빙은 전혀 다른 자리다.
    """
    record = _record(raw_text=_BODY + "\n담임목사: 박은제\n문의: 053-753-1685")

    verified, report = verify(
        record,
        _extraction(
            position=(Position.SENIOR_PASTOR,), evidence=Evidence(position="담임목사: 박은제")
        ),
    )

    assert verified.position == ()
    assert "position" in report.scrubbed


def test_a_real_senior_pastor_call_survives() -> None:
    """반대쪽도 봐야 한다 — 진짜 담임 청빙까지 막으면 그 공고가 통째로 쓸모없어진다."""
    record = _record(raw_text="평강교회에서 담임목사를 청빙합니다.")

    verified, report = verify(
        record,
        _extraction(
            position=(Position.SENIOR_PASTOR,), evidence=Evidence(position="담임목사를 청빙합니다")
        ),
    )

    assert verified.position == (Position.SENIOR_PASTOR,)
    assert report.is_clean


# ── 어느 칸을 비우고 어느 칸을 세는가 ───────────────────────────
#
# ⚠️ **이 분할이 이 모듈의 설계 그 자체다.** 실측이 한 번 뒤집었다 — 처음에는 전부 비웠고,
# 그때 54개를 비워 그중 진짜 오류는 1개뿐이었다. 아래 두 목록이 그 결론이고, 테스트가
# 없으면 칸 하나가 조용히 반대편으로 옮겨가도 스위트가 통과한다.

#: 비운다 — 원문에서 한 조각을 그대로 옮기는 칸.
_BLANKED: Final = (
    "role",
    "start_timing",
    "work_days",
    "church_name",
    "raw_denomination",
    "contact_email",
)

#: 세기만 한다 — 프롬프트가 여러 조각을 이으라고 시킨 칸.
_COUNTED: Final = (
    "headcount",
    "housing_note",
    "pay_note",
    "benefit_note",
    "contact_post",
)

#: 세기만 한다 — 목록 칸(`항목 하나에 한 가지씩`이라 표와 문장을 항목으로 편다).
_COUNTED_LISTS: Final = (
    "requirements",
    "preferred",
    "required_docs",
    "optional_docs",
    "process_steps",
)


@pytest.mark.parametrize("field_name", _BLANKED, ids=lambda value: str(value))
def test_an_atomic_field_is_blanked_when_it_is_not_in_the_source(field_name: str) -> None:
    """원문에서 한 조각을 그대로 옮기는 칸은 못 찾으면 **비운다**."""
    verified, report = verify(_record(), _extraction(**{field_name: "원문에없는값"}))

    assert getattr(verified, field_name) is None
    assert field_name in report.scrubbed


@pytest.mark.parametrize("field_name", _COUNTED, ids=lambda value: str(value))
def test_an_aggregating_field_is_counted_not_blanked(field_name: str) -> None:
    """⚠️ 프롬프트가 조립을 시킨 칸은 **비우지 않는다** — 어긋나는 것이 정상이다.

    비우면 `가족관계증명서 각1통`을 나눈 것 같은 **맞는 답**을 잃는다.
    """
    verified, report = verify(_record(), _extraction(**{field_name: "원문에없는값"}))

    assert getattr(verified, field_name) == "원문에없는값"
    assert report.scrubbed == ()
    assert report.unchecked_fields == {field_name: 1}


@pytest.mark.parametrize("field_name", _COUNTED_LISTS, ids=lambda value: str(value))
def test_a_list_field_is_counted_not_blanked(field_name: str) -> None:
    """목록 칸도 같다 — 실측 23개가 어긋났는데 지어낸 것은 0개였다."""
    verified, report = verify(_record(), _extraction(**{field_name: ("원문에없는값",)}))

    assert getattr(verified, field_name) == ("원문에없는값",)
    assert report.scrubbed == ()
    assert report.unchecked_fields == {field_name: 1}


def test_the_two_groups_do_not_overlap() -> None:
    """한 칸이 비워지면서 동시에 세어지면 리포트가 두 번 센다."""
    assert not set(_BLANKED) & (set(_COUNTED) | set(_COUNTED_LISTS))


def test_a_scrubbed_value_is_counted_once_per_value_not_per_field() -> None:
    """⚠️ `scrubbed`와 `unverifiable`의 단위가 달라 그림 공고가 세 배 나빠 보이던 문제.

    직분 셋이 근거를 못 대면 3개로 센다 — 면제 공고에서 세는 방식과 같아야 한다.
    """
    three = (Position.ASSOCIATE_PASTOR, Position.EVANGELIST, Position.LICENSED_MINISTER)

    blanked, report = verify(_record(), _extraction(position=three))
    exempt, exempt_report = verify(_record(), _extraction(position=three), media_sent=True)

    assert blanked.position == ()
    assert len(report.scrubbed) == 3
    assert exempt.position == three
    assert exempt_report.unverifiable == 3


def test_a_contact_tel_with_no_digits_is_blanked() -> None:
    """⚠️ 빈 숫자열은 어디에나 있다 — `없음`·`교회로 문의`가 전화번호로 통과하던 구멍."""
    verified, report = verify(_record(), _extraction(contact_tel="교회로 문의"))

    assert verified.contact_tel is None
    assert "contact_tel" in report.scrubbed


def test_a_senior_pastor_line_does_not_justify_an_associate() -> None:
    """⚠️ `담임목사`가 `목사`를 품는다 — 배제하지 않으면 담임 청빙이 부목사로 저장된다.

    `준전임`⊃`전임`과 같은 문제이고, 되돌아오는 방향도 막아야 한다.
    """
    record = _record(raw_text="평강교회에서 담임목사를 청빙합니다.")

    verified, report = verify(
        record,
        _extraction(
            position=(Position.ASSOCIATE_PASTOR,),
            evidence=Evidence(position="담임목사를 청빙합니다"),
        ),
    )

    assert verified.position == ()
    assert "position" in report.scrubbed
