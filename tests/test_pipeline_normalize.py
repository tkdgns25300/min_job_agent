"""변환 테스트 — 모델에게 시키지 않기로 한 것들.

⚠️ **여기가 이 모듈의 존재 이유다.** 같은 변환을 모델에 맡겼을 때는 실행마다 답이 달랐고
(실측 `연봉 3,200이상` → Flash 3200 / Flash-Lite 267) 검증하려면 유료 호출이 필요했다.
이 테스트들은 Gemini를 부르지 않는다.
"""

from __future__ import annotations

import pytest

from minjob_ingest.domain import StipendPeriod
from minjob_ingest.models import JsonValue
from minjob_ingest.pipeline.normalize import (
    address_or_none,
    clean_title,
    closed_by_board,
    pay_note_of,
    pay_of,
    period_of,
)

# ── 사례비 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "low", "high"),
    [
        ("연봉 3,200이상", 3200, None),
        ("월 250만원", 250, None),
        ("2,500,000원", 250, None),
        ("월 250~300만원", 250, 300),
        ("약 3천만원", 3000, None),
        ("월 100", 100, None),
        ("교회 내규에 따름", None, None),
        ("", None, None),
        (None, None, None),
    ],
    ids=["연봉", "월", "원 단위", "범위", "천만", "단위 없음", "금액 아님", "빈 값", "null"],
)
def test_a_pay_phrase_becomes_manwon(given: str | None, low: int | None, high: int | None) -> None:
    """⚠️ 실측: 같은 `연봉 3,200이상`을 Flash는 3200, Flash-Lite는 267로 답했다.

    12배 차이가 나는 이유는 모델에게 **산술**을 시켰기 때문이다. 금액 표현을 고르는 것만
    맡기고 환산은 여기서 한다 — 그러면 답이 흔들릴 자리가 없다.
    """
    assert pay_of(given) == (low, high)


@pytest.mark.parametrize(
    ("given", "why"),
    [
        (
            "전도사 월 160, 강도사 월 170, 목사 월 180",
            "자리가 셋이라 앞의 둘만 남으면 180이 사라진다",
        ),
        ("110만원 (졸업자 : 120만원)", "조건이 다른 두 금액이지 협상 범위가 아니다"),
        ("1년차 파트전도사 110만원, 최대 450만원 장학금 지급", "450은 장학금이다"),
        ("교회에서 정한 연봉제(전임전도사는 3000만원, 부목사는 3600만원)", "자리가 둘이다"),
    ],
    ids=["금액 셋", "조건부", "장학금 섞임", "자리 둘"],
)
def test_amounts_without_a_range_mark_are_not_a_range(given: str, why: str) -> None:
    """⚠️ **원문이 범위라고 적지 않았으면 범위로 만들지 않는다**(2026-08-20 실측으로 고쳤다).

    앞의 두 금액을 최소·최대로 삼던 규칙이 실데이터에서 틀린 값을 만들었다: `160~170`으로
    저장돼 **목사 180이 사라졌고**, 장학금 450이 **사례비 최대**가 됐다(화면에 `110~450만원`).
    19건 중 9건이 이 모양이었다.

    금액을 비우는 대신 `pay_note`가 원문을 담으므로(`pay_note_of`) 지원자가 보는 정보는
    오히려 자세하다 — 빈 칸이 아니라 원문이다.
    """
    assert pay_of(given) == (None, None), why


@pytest.mark.parametrize(
    ("given", "low", "high"),
    [
        ("90~120만원(신대원생 등록금 일부 지원)", 90, 120),
        ("95-100만원", 95, 100),
        ("연 3400~3600 및 전세금 일부 지원가능", 3400, 3600),
    ],
    ids=["물결", "붙임표", "연봉 범위"],
)
def test_a_range_the_source_wrote_is_kept(given: str, low: int, high: int) -> None:
    """교회가 `~`·`-`로 범위를 적었으면 그건 실제 범위다 — 실측 19건 중 7건이 이 모양이다."""
    assert pay_of(given) == (low, high)


def test_a_second_amount_alone_still_needs_the_mark() -> None:
    """⚠️ 금액이 하나면 범위 표시가 없어도 그대로 쓴다 — 규칙이 넓어져 정상 공고를 비우면 안 된다."""
    assert pay_of("월 250만원") == (250, None)
    assert pay_of("부목사 기준 320만원 + 사택 전세 지원 5천만원") == (320, None), (
        "사례비가 아닌 돈은 애초에 후보에서 빠지므로 금액이 하나다"
    )


# ── 금액을 못 쓸 때 원문을 남긴다 ────────────────────────────────


@pytest.mark.parametrize(
    ("amount", "note", "expected"),
    [
        ("월 250만원", "교회 내규", "교회 내규"),
        ("전도사 월 160, 강도사 월 170", None, "전도사 월 160, 강도사 월 170"),
        ("전도사 월 160, 강도사 월 170", "내규", "내규 · 전도사 월 160, 강도사 월 170"),
        (
            "교회 연봉제(전임 3000, 부목사 3600)",
            "교회 연봉제",
            "교회 연봉제(전임 3000, 부목사 3600)",
        ),
        (None, "면접 시 협의", "면접 시 협의"),
        (None, None, None),
    ],
    ids=["금액 살아있음", "설명 없음", "둘 다", "한쪽이 포함", "금액 없음", "빈 값"],
)
def test_the_pay_phrase_survives_as_a_note(
    amount: str | None, note: str | None, expected: str | None
) -> None:
    """⚠️ 이게 없으면 금액을 비운 공고에서 **사례비가 통째로 사라진다** — 모델이 뽑은 표현은
    `pay_amount`에 있지만 그 칸은 저장되지 않는다(만원 환산의 입력일 뿐이다).

    ⚠️ 한쪽이 다른 쪽에 들어 있으면 긴 것만 남긴다 — 모델이 같은 문장을 두 칸에 나눠 담는 일이
    흔해서 그대로 이으면 같은 말이 두 번 보인다.
    """
    assert pay_note_of(amount, note) == expected


def test_an_absurd_amount_is_dropped() -> None:
    """⚠️ 상수로 입력을 만들면 안 된다 — 상한을 10배로 바꿔도 통과하는 항진 테스트가 된다.

    실제 최대 연봉이 4,100만원이라 2억은 사례비일 수 없다.
    """
    assert pay_of("20000만원") == (None, None)


@pytest.mark.parametrize(
    ("given", "why"),
    [
        ("국민연금 50% 지원, 의료보험 100% 지원", "%는 비율이다"),
        ("교회 내규에 따름(보너스 200%, 명절떡값)", "상여 비율이다"),
        ("연차(연15일) 제공", "일은 날수다"),
        ("4대보험, 명절상여금", "4대보험의 4는 금액이 아니다"),
        ("최저시급 10,320원", "시급은 월 사례비가 아니다"),
        ("(1) 사례비는 교회 내규에 따릅니다", "목록 번호다"),
        ("월 700유로", "외화는 만원이 아니다"),
    ],
    ids=["%", "상여율", "일", "4대보험", "시급", "목록번호", "외화"],
)
def test_a_number_that_is_not_money_is_ignored(given: str, why: str) -> None:
    """⚠️ 단위를 안 보면 아무 숫자나 사례비가 된다.

    실측: `아파트(24평)` → 월 24만원 · `국민연금 50% 지원` → 연 50만원 · `연차(연15일)` →
    연 15만원. 셋 다 원문이 금액을 **말한 적이 없는** 공고다.
    """
    assert pay_of(given)[0] is None, why


def test_a_floor_area_is_skipped_and_the_real_amount_is_taken() -> None:
    """⚠️ 실측: `교회 인근 아파트(24평)를 제공… 연봉 3,600` 이 **월 24만원**으로 저장됐다.

    맨 앞 숫자를 쓰기 때문에 넓이가 사례비 자리를 차지했다 — 단위를 보면 건너뛴다.
    """
    assert pay_of("교회 인근 아파트(24평)를 제공해 드립니다. 연봉 3,600") == (3600, None)


def test_a_deposit_is_not_a_stipend() -> None:
    """⚠️ 실측 10건: `부목사 기준 320만원 + 사택 전세 지원 5천만원` — 5,000이 최대가 되면
    화면에 `월 320~5,000만원`이 뜬다. 보증금은 사례비가 아니다."""
    assert pay_of("부목사 기준 320만원 + 사택 전세 지원 5천만원") == (320, None)


def test_the_upper_bound_shares_the_period_of_the_lower() -> None:
    """월 사례비의 최대가 연봉 크기일 수는 없다."""
    assert pay_of("월 250만원, 사택지원금 3,000만원") == (250, None)


def test_a_lower_second_amount_is_not_a_range() -> None:
    """`월 250만원, 상여금 100만원`에서 100은 최대가 아니다."""
    assert pay_of("월 250만원, 상여금 100만원") == (250, None)


# ── 마감 여부 ────────────────────────────────────────────────────


def _meta(**values: JsonValue) -> dict[str, JsonValue]:
    return dict(values)


@pytest.mark.parametrize(
    ("title", "meta"),
    [
        ("성원교회 부교역자 청빙 (청빙완료)", {}),
        ("부교역자 청빙", _meta(status="구인 완료")),
        ("부교역자 청빙", _meta(category="초빙완료")),
        ("부교역자 청빙", _meta(classification="청빙완료")),
        ("[마감]서울 수유동교회에서 파트 교역자를 청빙합니다", {}),
        ("개명교회와 함께 사역할 동역자를 청빙합니다.(마감되었습니다.)", {}),
        ("영일만교회 부교역자 청빙이 완료되었습니다. 지원해 주셔서 감사드립니다.", {}),
        ("[초빙완료] 대구제이교회에서 부목사님을 청빙합니다", {}),
    ],
    ids=[
        "제목 청빙완료",
        "status",
        "category",
        "classification",
        "[마감]",
        "마감되었습니다",
        "완료되었습니다",
        "[초빙완료]",
    ],
)
def test_a_board_marked_as_finished_is_closed(title: str, meta: dict[str, JsonValue]) -> None:
    """⚠️ 그대로 두면 `jobs.status` 기본값이 `OPEN`이라 이미 채워진 자리가 공개된다."""
    assert closed_by_board(title, meta) is True


@pytest.mark.parametrize(
    "title",
    [
        "하늘담은교회 부목사님과 유치부 교역자 청빙(조기 마감 될 수 있습니다.)",
        "부목사(교구목사)님을 모십니다.(조기 마감 될 수 있습니다.)",
        "여성목회자 역량강화 지원사업(6/19 마감)",
    ],
    ids=["조기 마감 될 수 있다", "조기 마감 2", "마감일 안내"],
)
def test_a_title_that_merely_mentions_a_deadline_is_not_closed(title: str) -> None:
    """⚠️ **제목에서는 `마감` 한 단어로 판정하지 않는다.**

    제목은 문장이라 `조기 마감 될 수 있습니다`(실측 3건)·`6/19 마감`(마감일 안내)이 섞인다.
    한 단어로 보던 때 이 3건이 잘못 거절됐다 — 끝났다고 **단정한** 형태만 받는다.
    """
    assert closed_by_board(title, {}) is False


def test_a_status_field_needs_only_one_word() -> None:
    """상태 필드는 문장이 아니라 표시다 — `완료` 한 단어로 충분하다(실측 `완료`·`구인 완료`)."""
    assert closed_by_board("부교역자 청빙", _meta(status="완료")) is True


@pytest.mark.parametrize(
    ("title", "body_like"),
    [
        ("부교역자 청빙", "서류는 채용 완료 후 폐기합니다"),
        ("부교역자 청빙", "초빙 완료 시까지 접수합니다"),
    ],
    ids=["서류 폐기 안내", "완료 시까지"],
)
def test_body_wording_never_closes_a_posting(title: str, body_like: str) -> None:
    """⚠️ 실측 370건이 본문에 `완료`를 담는다 — 본문까지 보면 대부분을 잘못 거절한다.

    `closed_by_board`는 **본문을 인자로 받지도 않는다** — 실수로 넘길 자리가 없어야 한다.
    """
    assert closed_by_board(title, _meta(author=body_like)) is False


def test_spacing_does_not_hide_the_marker() -> None:
    """`구인 완료`·`구인완료`가 둘 다 쓰인다(실측)."""
    assert closed_by_board("부교역자 청빙", _meta(status="구 인 완 료")) is True


def test_an_open_posting_stays_open() -> None:
    assert closed_by_board("성원교회 부교역자 청빙", _meta(status="진행중")) is False


# ── 제목 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "want"),
    [
        (
            "(끌어올림)대구 대동교회에서 전임사역자를 모십니다!",
            "대구 대동교회에서 전임사역자를 모십니다!",
        ),
        ("[끌어올림] 반야월교회에서 동역자를 모십니다.", "반야월교회에서 동역자를 모십니다."),
        ("<끌어올림> 하늘담은교회 부목사님 청빙", "하늘담은교회 부목사님 청빙"),
        ("[답글]청빙이 완료되었습니다.", "청빙이 완료되었습니다."),
        ("(끌어올림)[답글] 두 겹도 뗀다", "두 겹도 뗀다"),
    ],
    ids=["(끌어올림)", "[끌어올림]", "<끌어올림>", "[답글]", "두 겹"],
)
def test_a_lift_marker_is_removed(given: str, want: str) -> None:
    """`(끌어올림)` 실측 340건 — 같은 글을 다시 올리려고 붙인 표시라 공고 내용이 아니다."""
    assert clean_title(given) == want


@pytest.mark.parametrize(
    "given",
    [
        "(대전) 대전한빛교회에서 교역자를 모십니다",
        "[서울] 구로동광교회 교역자를 정중히 모십니다",
        "(청빙완료) 동화중학교 학원선교사 청빙공고",
        "(GOODTV) 방송 엔지니어 모집",
        "(초교파) 선교단체 간사 모집",
        "[경기도 용인시] 새로운교회 부목사 청빙",
    ],
    ids=["지역(대전)", "지역[서울]", "상태", "출처", "초교파", "긴 지역"],
)
def test_a_marker_that_is_not_a_lift_stays(given: str) -> None:
    """⚠️ **화이트리스트다.** 괄호를 만나면 무조건 벗기는 것이 아니다 — 실측 머리표 201가지 중
    뗄 것은 5가지뿐이고 나머지는 지역·상태·출처처럼 공고 정보다.

    모델에게 "앞뒤 괄호를 뺀다"고 시켰을 때 `(수성노회)`가 통째로 사라진 적이 있다.
    """
    assert clean_title(given) == given


@pytest.mark.parametrize(
    ("given", "want"),
    [
        (
            "끌어올림- 청소년부(중고등부) 교육목사님 청빙합니다.",
            "청소년부(중고등부) 교육목사님 청빙합니다.",
        ),
        ("끌어올림-제목없이붙음", "제목없이붙음"),
        ("답글: 부목사 청빙", "부목사 청빙"),
    ],
    ids=["구분기호+공백", "구분기호만", "콜론"],
)
def test_a_lift_marker_without_brackets_is_also_removed(given: str, want: str) -> None:
    """괄호 없이 붙는 꼴도 뗀다 — `끌어올림- 제목`(실측 725건 중 1건 · 한 교회가 계속 그 꼴).

    ⚠️ 드물지만 그대로 두면 **공개 목록 제목 앞에 남는다**(2026-08-21 실제 공개에서 보였다).
    """
    assert clean_title(given) == want


@pytest.mark.parametrize(
    "given",
    [
        "대구성북교회- 부목사 청빙",
        "수정 및 끌어올림- 제목",
        "2026-08 교역자 청빙",
    ],
    ids=["교회명", "앞 낱말이 목록 밖", "날짜"],
)
def test_a_bare_word_that_is_not_a_lift_stays(given: str) -> None:
    """⚠️ **구분기호가 있어도 화이트리스트를 지난다** — 아니면 교회명·날짜가 사라진다.

    괄호 형태와 **같은 목록**을 쓰는 것이 그 보장이다.
    """
    assert clean_title(given) == given


def test_the_title_keeps_its_final_punctuation() -> None:
    """⚠️ 이 함수가 생긴 이유다 — 모델에게 맡겼더니 20건 중 6건이 끝의 마침표를 지웠다.

    지시하지 않은 손질이고, 원문을 그대로 옮기기로 한 칸에서 그러면 안 된다.
    """
    assert clean_title("대구한일교회에서 파트 사역자를 모십니다.").endswith("모십니다.")


def test_a_title_that_is_only_a_marker_is_left_alone() -> None:
    """⚠️ 빈 제목은 레코드 불변식이 거부한다 — 벗겨서 아무것도 안 남으면 그대로 둔다."""
    assert clean_title("(끌어올림)") == "(끌어올림)"


def test_surrounding_whitespace_is_trimmed() -> None:
    assert clean_title("  성원교회 청빙  ") == "성원교회 청빙"


def test_a_truncation_ellipsis_is_kept() -> None:
    """⚠️ 프롬프트는 "끝 말줄임을 빼라"고 했는데 **실측이 그게 틀렸다고 말한다**.

    말줄임으로 끝나는 56건의 앞 글자가 `니`·`고`·`감`처럼 단어 중간이다 — 게시판이 목록에서
    자른 표시이지 장식이 아니다. 떼면 잘린 제목이 온전한 것처럼 보인다.
    """
    cut = "대구동성교회에서 사무간사 및 뱡송간사를 모집합니..."

    assert clean_title(cut) == cut


def test_a_lift_marker_is_removed_from_a_truncated_title() -> None:
    """머리표를 떼는 것과 말줄임을 남기는 것은 별개다."""
    assert clean_title("(끌어올림)대구대동교회에서 전임교역자를 모십니...") == (
        "대구대동교회에서 전임교역자를 모십니..."
    )


# ── 사례비 주기 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "want"),
    [
        ("연봉 250만원", StipendPeriod.YEAR),
        ("연 300만원", StipendPeriod.YEAR),
        ("년봉 400", StipendPeriod.YEAR),
        ("월 3,500만원", StipendPeriod.MONTH),
        ("월급 3200", StipendPeriod.MONTH),
        ("매월 3,600만원", StipendPeriod.MONTH),
    ],
    ids=["연봉", "연", "년봉", "월", "월급", "매월"],
)
def test_a_stated_period_wins(given: str, want: StipendPeriod) -> None:
    """표현이 말하면 그것이다 — 크기를 따지기 전에 먼저 본다.

    ⚠️ **금액을 일부러 반대쪽으로 골랐다.** `연봉 3,500만원`처럼 크기까지 같은 답을 주는 예로
    쓰면 이 테스트는 아무것도 증명하지 못한다 — 실제로 그랬고, 낱말 검사를 통째로 지워도
    스위트가 통과했다.
    """
    assert period_of(given) is want


def test_the_word_next_to_the_amount_decides_not_the_whole_text() -> None:
    """⚠️ 실측 8건: `월 300만원 (연봉 3,600만원)`이 **연 300만원**으로 저장됐다.

    글 전체에서 낱말을 찾으면 뒤에 있는 `연봉`이 앞의 `월`을 이긴다 — 12배 오차다.
    고른 금액 **바로 앞**의 말만 본다.
    """
    assert period_of("- 월 300만원 (연봉 3,600만원)") is StipendPeriod.MONTH
    assert pay_of("- 월 300만원 (연봉 3,600만원)") == (300, None)


def test_a_yearly_word_after_a_monthly_amount_does_not_flip_it() -> None:
    """`월 220만원 (연 200%의 상여금 별도)` — `연 200%`는 상여금 주기이지 사례비가 아니다."""
    assert period_of("7. 사례비: 월 220만원 (연 200%의 상여금 별도 지급)") is StipendPeriod.MONTH


@pytest.mark.parametrize(
    ("given", "want"),
    [
        ("110", StipendPeriod.MONTH),
        ("100만원", StipendPeriod.MONTH),
        ("400만원", StipendPeriod.MONTH),
        ("3,500만원", StipendPeriod.YEAR),
        ("3200", StipendPeriod.YEAR),
    ],
    ids=["110", "100만원", "400만원(월 최대)", "3500만원", "3200"],
)
def test_an_unstated_period_is_read_from_the_size(given: str, want: StipendPeriod) -> None:
    """⚠️ 실측 범위는 `normalize._MONTHLY_CEILING`·`_YEARLY_FLOOR` 주석이 정본이다 —
    **겹치는 값이 하나도 없다.** 그래서 크기가 주기를 말해준다."""
    assert period_of(given) is want


@pytest.mark.parametrize(
    "given",
    ["700만원", "교회 내규에 따름", "", None],
    ids=["애매한 구간", "금액 없음", "빈 값", "null"],
)
def test_an_ambiguous_amount_gets_no_period(given: str | None) -> None:
    """⚠️ 500~1,000만원은 월인지 연인지 모른다(실측 0건) — 찍는 것보다 빈 칸이 낫다.

    이 칸이 뒤집히면 금액이 12배 어긋난다.
    """
    assert period_of(given) is None


def test_a_month_word_that_is_not_a_period_is_ignored() -> None:
    """⚠️ `월차 12회`의 `월`은 주기가 아니다. 금액이 아닌 `12회`는 애초에 후보가 아니다."""
    assert period_of("연봉 250만원 (월차 12회)") is StipendPeriod.YEAR


@pytest.mark.parametrize(
    ("given", "want"),
    [("60만원", StipendPeriod.MONTH), ("3,500만원", StipendPeriod.YEAR)],
    ids=["작으면 월", "크면 연"],
)
def test_the_size_decides_when_no_word_is_given(given: str, want: StipendPeriod) -> None:
    """⚠️ 경계값을 상수로 쓰지 않는다 — 상수를 바꿔도 통과하는 테스트가 된다.

    실측: 월 사례비 20~350만원 · 연봉 3,000~4,100만원. 겹치는 값이 없다.
    """
    assert period_of(given) is want


def test_a_list_marker_is_not_an_amount() -> None:
    """⚠️ 실측: `(1) 사례비는 교회 내규에 따릅니다`가 **1만원**으로 읽혔다.

    사례비가 월 10만원 아래일 수는 없다 — 하한이 목록 번호를 걸러낸다.
    """
    assert pay_of("(1) 사례비는 교회 내규에 따릅니다. (2) 사택 제공됩니다") == (None, None)


def test_a_won_suffix_is_not_manwon() -> None:
    """⚠️ 실측: `최저시급 10,320원`이 **10,320만원**(연봉)으로 읽혔다.

    `원`이 붙었으면 원 단위다 — 단위 없는 큰 수만 원으로 보던 규칙에 구멍이 있었다.
    """
    assert pay_of("최저시급 10,320원") == (None, None)
    assert pay_of("월 사례 2,500,000원") == (250, None)


@pytest.mark.parametrize(
    ("given", "want"),
    [("450만원", StipendPeriod.MONTH), ("1,200만원", StipendPeriod.YEAR)],
    ids=["경계 바로 아래는 월", "경계 바로 위는 연"],
)
def test_the_size_boundary_holds_at_its_stated_values(given: str, want: StipendPeriod) -> None:
    """⚠️ 경계를 좁히면 이 두 값이 죽는다 — 경계가 500·1,000임을 여기서 못 박는다.

    실측 월 최대 350 · 연 최소 3,000이라 450·1,200은 안전지대 안쪽이다.
    """
    assert period_of(given) is want


def test_a_value_in_the_dead_band_gets_no_period() -> None:
    """500~1,000만원은 월인지 연인지 모른다 — 찍지 않는다."""
    assert period_of("700만원") is None


def test_an_amount_without_a_period_is_not_stored_at_all() -> None:
    """⚠️ **주기를 모르면 금액도 비운다**(2026-08-21 · min_job 지적).

    `jobs.pay_period`가 `NOT NULL DEFAULT 'MONTH'`라 주기 없이 금액만 내보내면 **연봉이
    월급으로 굳는다**(12배). 금액 하나만으로는 어느 쪽인지 말할 수 없다.
    """
    assert pay_of("700만원") == (None, None)
    assert pay_of("999만원") == (None, None)


def test_the_dropped_amount_survives_in_the_note() -> None:
    """정보가 사라지는 것이 아니다 — 원문 표현이 `pay_note`로 옮겨간다."""
    assert pay_note_of("700만원", None) == "700만원"


@pytest.mark.parametrize(
    "given",
    ["월 700만원", "연 700만원", "연봉 700만원"],
    ids=["월이라 적었다", "연이라 적었다", "연봉이라 적었다"],
)
def test_a_stated_period_rescues_a_dead_band_amount(given: str) -> None:
    """죽은 구간이라도 **원문이 주기를 말했으면** 금액을 쓴다 — 크기로만 판정하지 않는다."""
    assert pay_of(given) == (700, None)


def test_a_bare_number_in_won_is_scaled_down() -> None:
    """⚠️ `원` 없이 큰 수로 적는 게시판이 있다 — 250만원을 `2500000`으로 쓴다.

    경계를 올리면 이 값이 250만원이 아니라 250만 **만원**이 된다.
    """
    assert pay_of("사례비 2500000") == (250, None)


def test_a_mid_sized_bare_number_is_still_won() -> None:
    """⚠️ 앞 테스트의 2,500,000은 경계를 어디에 두든 원 단위다 — 경계를 증명하지 못한다.

    500,000처럼 **두 후보 경계 사이**에 있는 값이어야 100,000이 맞는 경계임을 보인다.
    """
    assert pay_of("사례비 500000") == (50, None)


# ── 주소 모양 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "given",
    [
        "전북 김제시 도작로 61",
        "신월로 57번길7",
        "지축동 911",
        "평택시 안중읍 837-13",
        "국사봉1 길 87",
    ],
    ids=["도로명", "번길", "지번", "읍번지", "길앞공백"],
)
def test_an_address_shaped_value_is_kept(given: str) -> None:
    """실측 게시판 주소 칸 730건 중 534건이 이 모양이고, 남긴 것은 전부 정상이었다."""
    assert address_or_none(given) == given


@pytest.mark.parametrize(
    "given",
    [
        "1층 사무실",
        "219",
        "구례중앙교회",
        "481-13",
        "인평1길 북삼교회",
        "카림애비뉴 214호",
        "  ",
        None,
    ],
    ids=["층", "숫자만", "교회명", "동없는번지", "번호없는길", "건물호수", "공백", "없음"],
)
def test_what_is_not_an_address_is_blanked(given: str | None) -> None:
    """⚠️ 이 값들은 **원문에 실제로 있어서** `verify`의 존재 검사를 그대로 통과한다.

    게시판 주소 칸 730건 중 196건(27%)이 이 모양이다 — 모양을 봐야 걸린다.
    """
    assert address_or_none(given) is None


def test_a_foreign_address_is_dropped_on_purpose() -> None:
    """⚠️ 한국 도로명·지번 모양만 본다 — 해외 주소 5건이 함께 버려진다(실측).

    지도 연동이 국내용이라 그대로 둔다. 넣으려면 라틴 주소 분기를 따로 만들어야 한다.
    """
    assert address_or_none("67 Cutler Road Jandakot WA 6164, Australia") is None
