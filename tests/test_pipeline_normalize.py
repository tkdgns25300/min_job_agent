"""변환 테스트 — 모델에게 시키지 않기로 한 것들.

⚠️ **여기가 이 모듈의 존재 이유다.** 같은 변환을 모델에 맡겼을 때는 실행마다 답이 달랐고
(실측 `연봉 3,200이상` → Flash 3200 / Flash-Lite 267) 검증하려면 유료 호출이 필요했다.
이 테스트들은 Gemini를 부르지 않는다.
"""

from __future__ import annotations

import pytest

from minjob_ingest.domain import Region
from minjob_ingest.models import JsonValue
from minjob_ingest.pipeline.normalize import MAX_PAY_MANWON, closed_by_board, pay_of, place_of

# ── 지역 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "region", "city"),
    [
        ("전북특별자치도 전주시 완산구 삼천동2가", Region.JEONBUK, "전주시"),
        ("경북 문경시 점촌동", Region.GYEONGBUK, "문경시"),
        ("경기 광명시 소하동", Region.GYEONGGI, "광명시"),
        ("서울 관악구 신림동", Region.SEOUL, "관악구"),
        ("충청남도 천안시 서북구", Region.CHUNGNAM, "천안시"),
        ("제주특별자치도 서귀포시", Region.JEJU, "서귀포시"),
        ("강원 홍천군", Region.GANGWON, "홍천군"),
        ("대구", Region.DAEGU, None),
        ("수성구", None, "수성구"),
        ("", None, None),
        (None, None, None),
    ],
    ids=[
        "시+구 → 시",
        "경북",
        "경기",
        "구만 있음",
        "긴 이름이 먼저",
        "제주",
        "군",
        "광역만",
        "광역 없음",
        "빈 값",
        "null",
    ],
)
def test_a_place_becomes_a_region_and_a_city(
    given: str | None, region: Region | None, city: str | None
) -> None:
    """⚠️ **시·군이 구보다 앞선다** — `전주시 완산구`는 `전주시`다.

    구 이름만으로는 어느 광역인지 알 수 없어서(`중구`가 여러 곳에 있다) 도시로 쓸모가 적다.
    """
    assert place_of(given) == (region, city)


def test_a_wide_name_is_not_mistaken_for_a_district() -> None:
    """⚠️ `대구`는 `~구`로 끝난다 — 거르지 않으면 광역이 시·군·구 칸에 들어간다."""
    assert place_of("대구 수성구") == (Region.DAEGU, "수성구")


def test_the_longer_wide_name_wins() -> None:
    """`충청북도`가 `충북`보다 먼저 걸려야 한다 — 순서가 뒤집히면 못 찾는다."""
    assert place_of("충청북도 청주시")[0] is Region.CHUNGBUK


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


def test_an_absurd_amount_is_dropped() -> None:
    """⚠️ 사택 보증금·건축 헌금을 골라오면 사례비가 아니다 — 빈 칸이 낫다."""
    assert pay_of(f"{MAX_PAY_MANWON + 1}만원") == (None, None)


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
