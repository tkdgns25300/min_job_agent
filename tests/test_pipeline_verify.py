"""검산 테스트 — 모델 답이 원문에 있나.

⚠️ **이 모듈이 있는 이유가 여기 있다.** 실측 284개 값 중 44개(15%)를 모델이 고쳐 썼고,
그것을 알아내려면 원문과 손으로 대조해야 했다. 여기서는 Gemini 없이 검사된다.
"""

from __future__ import annotations

from collections.abc import Callable
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
    title: str = "성원교회 부목사 청빙",
    raw_text: str = _BODY,
    raw_meta: dict[str, JsonValue] | None = None,
    image_urls: tuple[str, ...] = (),
    attachments: tuple[Attachment, ...] = (),
) -> SourceData:
    return SourceData(
        source_key="DAESHIN",
        external_id="37",
        source_url="https://example.kr/board/37",
        title=title,
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


def _department_evidence(text: str) -> Evidence:
    return Evidence(department=text)


def _employment_evidence(text: str) -> Evidence:
    return Evidence(employment_type=text)


def _qualification_evidence(text: str) -> Evidence:
    return Evidence(qualification=text)


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
    verified, report = verify(_record(), _extraction(raw_denomination="원문에없는교단"))

    assert verified.raw_denomination is None
    assert "raw_denomination" in report.scrubbed


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
            evidence=Evidence(position_items=("부목사 1명",)),
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
            evidence=Evidence(position_items=("담임목사를 청빙합니다",)),
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
            evidence=Evidence(position_items=("부목사 1명",)),
        ),
    )

    assert verified.position == ()


def test_only_the_unsupported_value_leaves_a_multi_valued_choice() -> None:
    verified, _ = verify(
        _record(),
        _extraction(
            position=(Position.ASSOCIATE_PASTOR, Position.SENIOR_PASTOR),
            evidence=Evidence(position_items=("부목사 1명",)),
        ),
    )

    assert verified.position == (Position.ASSOCIATE_PASTOR,)


def test_etc_needs_no_supporting_word() -> None:
    """`그 밖`이라 뒷받침할 낱말이 없다 — 근거가 원문에 있으면 통과."""
    verified, _ = verify(
        _record(),
        _extraction(position=(Position.ETC,), evidence=Evidence(position_items=("부목사 1명",))),
    )

    assert verified.position == (Position.ETC,)


@pytest.mark.parametrize(
    ("field_name", "value", "evidence_text", "with_evidence"),
    [
        ("department", Department.YOUTH, "중고등부", _department_evidence),
        ("employment_type", EmploymentType.FULL_TIME, "전임", _employment_evidence),
        (
            "qualification",
            Qualification.SEMINARIAN,
            "총회인준 신학대학원 졸업자",
            _qualification_evidence,
        ),
    ],
    ids=["department", "employment_type", "qualification"],
)
def test_single_valued_choices_are_checked_the_same_way(
    field_name: str,
    value: object,
    evidence_text: str,
    with_evidence: Callable[[str], Evidence],
) -> None:
    grounded, _ = verify(
        _record(), _extraction(**{field_name: value}, evidence=with_evidence(evidence_text))
    )
    invented, report = verify(
        _record(), _extraction(**{field_name: value}, evidence=with_evidence("원문에 없는 근거"))
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
        record, _extraction(raw_denomination="예장통합", church_name="지어낸교회"), media_sent=True
    )

    assert verified.raw_denomination == "예장통합"
    assert verified.church_name == "지어낸교회"
    assert report.scrubbed == ()
    assert report.unverifiable == 2


def test_a_posting_with_a_pdf_is_never_scrubbed() -> None:
    record = _record(
        attachments=(Attachment(name="청빙공고문.pdf", url="https://e.kr/a.pdf"),),
    )

    verified, report = verify(record, _extraction(raw_denomination="예장통합"), media_sent=True)

    assert verified.raw_denomination == "예장통합"
    assert report.unverifiable == 1


def test_a_hwp_attachment_does_not_exempt_the_posting() -> None:
    """⚠️ HWP는 모델에 보내지 않는다 — 거기서 왔을 수가 없으니 면제 사유가 아니다."""
    record = _record(attachments=(Attachment(name="이력서양식.hwp", url="https://e.kr/a.hwp"),))

    verified, report = verify(record, _extraction(raw_denomination="예장통합"))

    assert verified.raw_denomination is None
    assert "raw_denomination" in report.scrubbed


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
    ("field_name", "value", "evidence_text", "with_evidence"),
    [
        ("department", Department.YOUTH, "총회인준 신학대학원 졸업자", _department_evidence),
        ("employment_type", EmploymentType.FULL_TIME, "중고등부", _employment_evidence),
        ("qualification", Qualification.ORDAINED, "중고등부", _qualification_evidence),
    ],
    ids=["부서 ← 자격 문구", "고용형태 ← 부서 문구", "자격 ← 부서 문구"],
)
def test_evidence_from_the_wrong_field_does_not_support_a_choice(
    field_name: str,
    value: object,
    evidence_text: str,
    with_evidence: Callable[[str], Evidence],
) -> None:
    """⚠️ 근거가 **원문에 있기만 하면** 통과하던 구멍을 막는다.

    모델이 아무 문장이나 근거로 붙여도 그 칸의 낱말이 없으면 뒷받침하지 못한다.
    """
    verified, report = verify(
        _record(), _extraction(**{field_name: value}, evidence=with_evidence(evidence_text))
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
            position=(Position.SENIOR_PASTOR,),
            evidence=Evidence(position_items=("담임목사: 박은제",)),
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
            position=(Position.SENIOR_PASTOR,),
            evidence=Evidence(position_items=("담임목사를 청빙합니다",)),
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
    "church_name",
    "raw_denomination",
    "contact_email",
)

#: 세기만 한다 — 프롬프트가 여러 조각을 이으라고 시킨 칸.
#: ⚠️ `role`은 조립이 아니라 **짧게 고쳐 쓴 직무명**이라 여기 있다(min_job DATA.md §3: 자유
#: 텍스트 · 통제 목록 아님). 글자 대조로 비우면 `교회 시설관리`를 `시설·관리`로 줄인 맞는
#: 답이 지워지고 fallback `기타`로 떨어진다(실측 CSU/1117858).
_COUNTED: Final = (
    "role",
    #: ⚠️ 자리마다 부임 시기를 따로 적는 공고가 244건이다 — 한 칸에 담으려면 이어야 한다.
    "start_timing",
    #: ⚠️ 준전임·파트가 근무일을 따로 적는 공고가 54건이다 — 한 칸에 담으려면 이을 수밖에
    #: 없고, 이으라고 시킨 것도 프롬프트다.
    "work_days",
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
            evidence=Evidence(position_items=("담임목사를 청빙합니다",)),
        ),
    )

    assert verified.position == ()
    assert "position" in report.scrubbed


# ── 직분마다 자기 근거 ───────────────────────────────────────────


def test_each_position_is_checked_against_its_own_evidence() -> None:
    """⚠️ 근거 하나로 여러 직분을 보던 때는 **맞는 직분이 통째로 지워졌다**(실측 CSU 10건 중 4건).

    한 조각이 부목사·전도사·강도사를 동시에 뒷받침할 수 없어 `기타`만 남았다.
    """
    record = _record(
        raw_text=(
            "1.청빙분야 ① 전임부목사: 교구 및 청소년부 (1명)"
            " ② 전임여전도사: 교구 및 새가족사역 (1명)"
        )
    )

    verified, report = verify(
        record,
        _extraction(
            position=(Position.ASSOCIATE_PASTOR, Position.EVANGELIST),
            evidence=Evidence(
                position_items=(
                    "① 전임부목사: 교구 및 청소년부 (1명)",
                    "② 전임여전도사: 교구 및 새가족사역 (1명)",
                )
            ),
        ),
    )

    assert verified.position == (Position.ASSOCIATE_PASTOR, Position.EVANGELIST)
    assert report.scrubbed == ()


def test_only_the_position_whose_evidence_fails_is_dropped() -> None:
    """맞는 값은 남고 근거 없는 값만 떨어진다 — 하나가 나머지를 끌고 내려가지 않는다."""
    record = _record(raw_text="① 전임부목사: 교구 (1명)")

    verified, report = verify(
        record,
        _extraction(
            position=(Position.ASSOCIATE_PASTOR, Position.EVANGELIST),
            evidence=Evidence(
                position_items=("① 전임부목사: 교구 (1명)", "원문에 없는 전도사 근거")
            ),
        ),
    )

    assert verified.position == (Position.ASSOCIATE_PASTOR,)
    assert report.scrubbed == ("position",)


def test_a_position_without_its_own_evidence_is_dropped() -> None:
    """⚠️ 근거가 값보다 적으면 남는 값은 검산되지 않은 것이다 — 통과시키면 검사가 무의미해진다."""
    record = _record(raw_text="① 전임부목사: 교구 (1명)")

    verified, report = verify(
        record,
        _extraction(
            position=(Position.ASSOCIATE_PASTOR, Position.EVANGELIST),
            evidence=Evidence(position_items=("① 전임부목사: 교구 (1명)",)),
        ),
    )

    assert verified.position == (Position.ASSOCIATE_PASTOR,)
    assert report.scrubbed == ("position",)


# ── 버린 값을 남긴다 ─────────────────────────────────────────────


def test_a_dropped_value_is_recorded_with_what_the_model_answered() -> None:
    """⚠️ 칸 이름만 남기면 **과검을 검수할 수 없다** — 실측에서 여기서 검수가 막혔다.

    전화번호·교단이 왜 지워졌는지 끝내 못 밝혔고, 원인은 모델 답을 안 남긴 것이었다.
    """
    _, report = verify(_record(), _extraction(church_name="원문에없는교회"))

    assert [(item.field, item.value) for item in report.dropped] == [
        ("church_name", "원문에없는교회")
    ]


def test_a_dropped_choice_carries_the_evidence_that_failed() -> None:
    """근거까지 남겨야 "값이 틀렸나 근거가 틀렸나"를 가릴 수 있다."""
    _, report = verify(
        _record(),
        _extraction(
            department=Department.YOUTH, evidence=Evidence(department="원문에 없는 중고등부 근거")
        ),
    )

    dropped = report.dropped[0]
    assert (dropped.field, dropped.value, dropped.evidence) == (
        "department",
        Department.YOUTH.value,
        "원문에 없는 중고등부 근거",
    )


def test_nothing_is_recorded_as_dropped_when_nothing_is_blanked() -> None:
    """그림 공고는 비우지 않으므로 버린 값도 없다 — 세기만 한 것을 버렸다고 적지 않는다."""
    _, report = verify(
        _record(image_urls=("https://example.kr/poster.jpg",)),
        _extraction(church_name="포스터에만 있는 교회"),
        media_sent=True,
    )

    assert report.dropped == ()
    assert report.unverifiable == 1


def test_a_name_beside_a_phone_number_does_not_become_digits() -> None:
    """⚠️ `목사`의 `사`가 4로, `송준영`의 `영`이 0으로 바뀌던 구멍.

    한글 숫자 되돌리기는 **원문에만** 건다 — 이름을 떼지 말라고 시킨 것도 프롬프트인데
    답에까지 걸면 전화번호가 늘 "원문에 없다"가 된다(실측 2026-08-14 · 4건 전부 이것).
    """
    record = _record(raw_text="문의 김준수 목사 010-2285-1151")

    verified, report = verify(record, _extraction(contact_tel="010-2285-1151 (김준수 목사)"))

    assert verified.contact_tel == "010-2285-1151 (김준수 목사)"
    assert report.scrubbed == ()


def test_a_number_hidden_in_hangul_still_matches_the_answer() -> None:
    """원문 쪽 되돌리기는 그대로 산다 — 프롬프트가 `구육구이`를 숫자로 펴라고 시킨다."""
    record = _record(raw_text="연락처 010-2720-구육구이")

    verified, report = verify(record, _extraction(contact_tel="010-2720-9692"))

    assert verified.contact_tel == "010-2720-9692"
    assert report.scrubbed == ()


def test_evidence_quoted_with_the_label_we_showed_the_model_is_accepted() -> None:
    """⚠️ 모델은 **자기가 본 줄**을 오려낸다 — 게시판 칸은 `모집부서: 장년 교구`로 보여준다.

    원본 값(`장년 교구`)만 대조하면 맞는 답이 지워진다(실측 CSU/1117877).
    """
    record = _record(raw_text="교회 소개", raw_meta={"ministry_dept": "장년 교구"})

    verified, report = verify(
        record,
        _extraction(
            department=Department.DISTRICT, evidence=Evidence(department="모집부서: 장년 교구")
        ),
    )

    assert verified.department is Department.DISTRICT
    assert report.scrubbed == ()


def test_a_full_width_digit_in_the_source_still_matches() -> None:
    """⚠️ `\\d`는 전각 숫자(U+FF16)도 세지만 반각과 **글자가 달라** 견주면 어긋난다.

    실측 PUTS/157669: 원문 전화번호 한 자리가 전각이라 맞는 번호가 통째로 지워졌다.
    """
    record = _record(raw_text="5. 전화번호 : 010-94\uff160-6018(윤경원 행정목사)")

    verified, report = verify(record, _extraction(contact_tel="010-9460-6018 (윤경원 행정목사)"))

    assert verified.contact_tel == "010-9460-6018 (윤경원 행정목사)"
    assert report.scrubbed == ()


def test_a_full_width_digit_in_the_answer_still_matches() -> None:
    """반대 방향도 막는다 — 모델이 전각으로 답해도 원문과 견줄 수 있어야 한다."""
    record = _record(raw_text="전화 010-9460-6018")

    answered = "010-94\uff160-6018"

    verified, report = verify(record, _extraction(contact_tel=answered))

    assert verified.contact_tel == answered
    assert report.scrubbed == ()


def test_a_phone_number_cannot_be_stitched_across_two_fields() -> None:
    """⚠️ 숫자만 남기면 칸 구분자도 지워진다 — 본문 끝과 다음 칸 앞이 이어붙어 **지어낸
    번호가 통과**한다. 칸마다 따로 봐야 막힌다."""
    record = _record(
        raw_text="자세한 내용은 첨부를 보세요",
        raw_meta={"phone": "010-2285", "views": "1151"},
    )

    verified, report = verify(record, _extraction(contact_tel="010-2285-1151"))

    assert verified.contact_tel is None
    assert report.scrubbed == ("contact_tel",)


def test_two_schedules_joined_into_one_field_are_not_blanked() -> None:
    """⚠️ 준전임과 파트가 근무일을 따로 적는 공고가 **54건**이다.

    한 문자열에 담으려면 이을 수밖에 없는데 그때 지우면 근무일이 통째로 사라진다
    (실측 PUTS/157668).
    """
    record = _record(
        raw_text=("* 준전임 또는 파트로 지원 가능합니다.\n준전임- 수, 금, 토, 주일\n파트- 토, 주일")
    )

    verified, report = verify(
        record, _extraction(work_days="준전임- 수, 금, 토, 주일 / 파트- 토, 주일")
    )

    assert verified.work_days == "준전임- 수, 금, 토, 주일 / 파트- 토, 주일"
    assert report.scrubbed == ()
    assert report.unchecked_fields == {"work_days": 1}


def test_two_phone_numbers_in_one_field_are_checked_one_by_one() -> None:
    """⚠️ 한 칸에 번호가 둘인 공고가 흔하다 — 실측 5건 중 4건.

    통째로 이으면 그 숫자열은 어느 원문에도 없어 **둘 다** 지워진다.
    """
    record = _record(raw_text="문의 032-515-5004(사무실), 담당자 010-7669-4035")
    answered = "032-515-5004(사무실) / 010-7669-4035 (담당자 : 송화평)"

    verified, report = verify(record, _extraction(contact_tel=answered))

    assert verified.contact_tel == answered
    assert report.scrubbed == ()


def test_one_invented_number_beside_a_real_one_is_still_caught() -> None:
    """⚠️ 덩어리마다 보되 **전부** 있어야 한다 — 하나만 맞으면 나머지가 무임승차한다."""
    record = _record(raw_text="문의 032-515-5004")

    verified, report = verify(
        record, _extraction(contact_tel="032-515-5004 / 010-7669-4035 (담당자)")
    )

    assert verified.contact_tel is None
    assert report.scrubbed == ("contact_tel",)


def test_an_email_typo_fixed_by_the_model_is_not_thrown_away() -> None:
    """⚠️ 원문 `naver.,com`(쉼표 오타)을 모델이 고쳤다 — 글자로 견주면 고친 답이 버려진다."""
    record = _record(raw_text="제출처 :tmlee153@naver.,com")

    verified, report = verify(record, _extraction(contact_email="tmlee153@naver.com"))

    assert verified.contact_email == "tmlee153@naver.com"
    assert report.scrubbed == ()


def test_an_invented_email_is_still_caught() -> None:
    """⚠️ 게시판이 이메일을 가리면(`[email protected]`) 모델이 작성자 이름으로 지어낸다.

    실측 6건 — `임관혁` → `limkh81@naver.com` · `사수정` → `sujung012@naver.com`.
    """
    record = _record(raw_text="6. 제출 및 문의 - [email protected]")

    verified, report = verify(record, _extraction(contact_email="limkh81@naver.com"))

    assert verified.contact_email is None
    assert report.scrubbed == ("contact_email",)


def test_the_title_can_supply_the_recruiting_word_for_a_senior_pastor() -> None:
    """⚠️ 모집 목록은 모집한다는 말을 안 달고 온다 — `담임목사 1명`(실측 NAZARENE/123)."""
    record = _record(title="담임목사 청빙 공고 (기간연장 및 자격변경)", raw_text="담임목사 1명")

    verified, report = verify(
        record,
        _extraction(
            church_name=None,
            position=(Position.SENIOR_PASTOR,),
            evidence=Evidence(position_items=("담임목사 1명",)),
        ),
    )

    assert verified.position == (Position.SENIOR_PASTOR,)
    assert report.scrubbed == ()


def test_a_pastor_name_in_the_title_does_not_supply_it() -> None:
    """⚠️ 괄호 안은 사람 이름 자리다 — `현대교회(담임목사 박건욱)에서 사무간사님을 모십니다`는
    담임을 뽑지 않는다(실측 7건)."""
    record = _record(
        title="현대교회(담임목사 박건욱)에서 사무간사님을 모십니다",
        raw_text="현대교회 담임목사 박건욱",
    )

    verified, report = verify(
        record,
        _extraction(
            church_name=None,
            position=(Position.SENIOR_PASTOR,),
            evidence=Evidence(position_items=("담임목사 박건욱",)),
        ),
    )

    assert verified.position == ()
    assert report.scrubbed == ("position",)


def test_a_preschool_department_is_recognised() -> None:
    """`미취학`도 영유아부다 — 낱말이 빠져 있으면 맞는 부서가 지워진다(실측 TTGU/1107400)."""
    record = _record(raw_text="모집부서: 서빙고, 도곡 미취학 영역")

    verified, report = verify(
        record,
        _extraction(department=Department.INFANT, evidence=Evidence(department="미취학 영역")),
    )

    assert verified.department is Department.INFANT
    assert report.scrubbed == ()


# ── 영문 공고 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "evidence_text", "with_evidence"),
    [
        (
            Qualification.EXPERIENCED,
            "Experience in secretarial, bookkeeping and/or administrative work",
            _qualification_evidence,
        ),
        (Qualification.SEMINARIAN, "M.Div. from an accredited seminary", _qualification_evidence),
        (EmploymentType.PART_TIME, "This is a part-time position", _employment_evidence),
        (Department.ADMIN, "Administrative support for the Seoul office", _department_evidence),
    ],
    ids=["경력", "신대원", "파트", "행정"],
)
def test_an_english_posting_is_not_emptied_for_being_english(
    value: object, evidence_text: str, with_evidence: Callable[[str], Evidence]
) -> None:
    """⚠️ 영문 공고가 17건 있다(CSU 9 · WGST 5 · TTGU 2 · PUTS 1).

    낱말표가 한글뿐이면 근거가 멀쩡해도 그 공고들은 통째로 지워진다(실측 TTGU/1107404).
    """
    record = _record(raw_text=evidence_text)
    field_name = {
        "Qualification": "qualification",
        "EmploymentType": "employment_type",
        "Department": "department",
    }[type(value).__name__]

    verified, report = verify(
        record,
        _extraction(church_name=None, **{field_name: value}, evidence=with_evidence(evidence_text)),
    )

    assert getattr(verified, field_name) is value
    assert report.scrubbed == ()


def test_a_part_time_english_posting_does_not_support_full_time() -> None:
    """⚠️ 배제 낱말도 영문이어야 한다 — `part-time`이 FULL_TIME을 뒷받침하면 안 된다."""
    record = _record(raw_text="This is a part-time position")

    verified, report = verify(
        record,
        _extraction(
            church_name=None,
            employment_type=EmploymentType.FULL_TIME,
            evidence=_employment_evidence("This is a part-time position"),
        ),
    )

    assert verified.employment_type is None
    assert report.scrubbed == ("employment_type",)


def test_a_phone_number_written_with_spaces_is_matched() -> None:
    """⚠️ `010 4678 5484`처럼 띄어 쓴 공고가 있다(실측 4건).

    번호 덩어리를 숫자·하이픈으로만 잡았더니 이런 번호가 통째로 지워졌다.
    """
    record = _record(raw_text="문의 010 4678 5484 박찬경 목사")

    verified, report = verify(record, _extraction(contact_tel="010 4678 5484 박찬경 목사"))

    assert verified.contact_tel == "010 4678 5484 박찬경 목사"
    assert report.scrubbed == ()


def test_two_numbers_split_by_a_word_are_checked_separately() -> None:
    """공백을 번호 안에 넣었어도 한글·쉼표는 여전히 번호를 가른다."""
    record = _record(raw_text="사무실 02 1111 2222 · 담당 010 3333 4444")

    verified, report = verify(record, _extraction(contact_tel="02 1111 2222, 담당 010 3333 4444"))

    assert verified.contact_tel == "02 1111 2222, 담당 010 3333 4444"
    assert report.scrubbed == ()


def test_a_domain_written_in_hangul_matches_the_romanised_answer() -> None:
    """⚠️ 숫자와 같은 수법이다 — `doyu78@네이버.com`(실측 4건). 모델이 되돌린 답을 벌하지 않는다."""
    record = _record(raw_text="제출처 doyu78@네이버.com")

    verified, report = verify(record, _extraction(contact_email="doyu78@naver.com"))

    assert verified.contact_email == "doyu78@naver.com"
    assert report.scrubbed == ()


def test_a_student_department_is_recognised() -> None:
    """`학생부`도 중고등부다 — 낱말이 빠져 맞는 부서가 지워졌다(실측 HAPSHIN/15259)."""
    record = _record(raw_text="모집 내용 : 학생부 담당 교육목사")

    verified, report = verify(
        record,
        _extraction(
            church_name=None,
            department=Department.YOUTH,
            evidence=Evidence(department="학생부 담당"),
        ),
    )

    assert verified.department is Department.YOUTH
    assert report.scrubbed == ()


def test_a_contact_name_beside_an_email_does_not_break_the_match() -> None:
    """⚠️ 이름을 떼지 말라고 시킨 것이 프롬프트다 — 그런데 원문은 담당자와 주소를 따로 적는
    일이 많아(실측 552건) 통째로 견주면 시킨 대로 한 답이 늘 지워진다."""
    record = _record(
        raw_text="채용 담당자: 김민성 전도사에게 제출 (재정 매니저) minsung@lifespring.kr"
    )
    answered = "minsung@lifespring.kr (김민성 전도사)"

    verified, report = verify(record, _extraction(contact_email=answered))

    assert verified.contact_email == answered
    assert report.scrubbed == ()


def test_two_start_dates_joined_into_one_field_are_not_blanked() -> None:
    """⚠️ 자리마다 부임 시기를 따로 적는 공고가 244건이다(실측 HANIL/104524)."""
    record = _record(raw_text="1) 전임 부임시기 2026년 12월\n2) 파트 부임시기 2027년 1월")

    verified, report = verify(record, _extraction(start_timing="2026년 12월 / 2027년 1월"))

    assert verified.start_timing == "2026년 12월 / 2027년 1월"
    assert report.scrubbed == ()
    assert report.unchecked_fields == {"start_timing": 1}


def test_a_note_beside_a_link_does_not_break_the_match() -> None:
    """`zinu8151 (카카오톡id)`처럼 무엇인지 덧붙여도 링크 자체가 원문에 있으면 남긴다."""
    record = _record(raw_text="카톡 zinu8151 로 문의")

    verified, report = verify(record, _extraction(contact_link="zinu8151 (카카오톡id)"))

    assert verified.contact_link == "zinu8151 (카카오톡id)"
    assert report.scrubbed == ()


def test_one_real_address_does_not_carry_an_invented_one() -> None:
    """⚠️ 주소마다 따로 보되 **전부** 있어야 한다 — 하나만 맞으면 나머지가 무임승차한다."""
    record = _record(raw_text="제출처 real@example.kr")

    verified, report = verify(
        record, _extraction(contact_email="real@example.kr, made-up@example.kr")
    )

    assert verified.contact_email is None
    assert report.scrubbed == ("contact_email",)
