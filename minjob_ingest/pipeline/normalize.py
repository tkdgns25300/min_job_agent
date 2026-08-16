"""게시판 표현 → 저장값. **모델에게 시키지 않는 변환만** 모은다.

프롬프트가 길어진 가장 큰 이유는 모델에게 *뽑기*와 *변환*을 함께 시켰기 때문이다. 둘은
성질이 다르다:

- **뽑기**는 맥락이 필요하다 — "담임목사"가 뽑는 자리를 가리키는지, 연락처에 적힌 사람인지는
  글을 읽어야 안다(실측: 키워드로 하면 18건 중 8건을 틀리고 그중 6건이 담임목사 오검출).
- **변환**은 맥락이 필요 없다 — `전북특별자치도 전주시`는 언제나 `JEONBUK`이고 `연봉 3,200`은
  언제나 3200만원이다. 여기에 모델을 쓰면 **값이 실행마다 흔들린다**(실측: Flash 3200 /
  Flash-Lite 267 — 같은 글자를 보고 12배 다른 답).

그래서 변환은 이 모듈이 한다. 얻는 것은 값이 흔들리지 않는 것과, **Gemini 없이 검증되는
것**이다.

⚠️ **지역은 예외가 됐다**(2026-08-16 · SPEC §5.5b). `안동시`가 어느 광역인지는 글자가 아니라
지리 지식이라, 표로 담으려면 동 이름까지 3,500줄이 된다 → 모델이 답하고 코드는 검산만 한다.
여기 남은 `place_of`는 그 교체를 유료 표본으로 검증할 때까지의 현행 경로다(ROADMAP 1-2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from minjob_ingest.domain import Region, StipendPeriod
from minjob_ingest.models import JsonValue

#: 광역 표기 → `Region`. **긴 이름을 먼저** 둔다 — `충청북도`가 `충북`보다 앞서야 한다.
_WIDE: Final = (
    ("서울", Region.SEOUL),
    ("경기", Region.GYEONGGI),
    ("인천", Region.INCHEON),
    ("강원", Region.GANGWON),
    ("충청북도", Region.CHUNGBUK),
    ("충북", Region.CHUNGBUK),
    ("충청남도", Region.CHUNGNAM),
    ("충남", Region.CHUNGNAM),
    ("대전", Region.DAEJEON),
    ("세종", Region.SEJONG),
    ("경상북도", Region.GYEONGBUK),
    ("경북", Region.GYEONGBUK),
    ("경상남도", Region.GYEONGNAM),
    ("경남", Region.GYEONGNAM),
    ("대구", Region.DAEGU),
    ("울산", Region.ULSAN),
    ("부산", Region.BUSAN),
    ("전라북도", Region.JEONBUK),
    ("전북", Region.JEONBUK),
    ("전라남도", Region.JEONNAM),
    ("전남", Region.JEONNAM),
    ("광주", Region.GWANGJU),
    ("제주", Region.JEJU),
)

#: 광역 이름만 모은 것. 시·군·구를 고를 때 **광역과 겹치는 토막을 걸러내는** 데 쓴다
#: (`대구 수성구`에서 `대구`가 `~구`로 잡히면 도시가 광역이 된다).
_WIDE_NAMES: Final = frozenset(name for name, _ in _WIDE)

#: 주소로 볼 수 있는 모양. **원문에 있다고 주소인 것은 아니다** — 게시판 주소 칸 730건 중
#: 196건(27%)이 `1층 사무실`·`219`·`구례중앙교회`처럼 주소가 아닌 값이고, 그 글자는 원문에
#: 있으므로 `verify`의 존재 검사를 그대로 통과한다. 교단의 "문장이면 이름이 아니다"와 같은
#: 방어다.
#:
#: ⚠️ **번호를 요구한다.** 도로명·지번은 번호가 있어야 한 곳을 가리킨다 — `인평1길 북삼교회`는
#: 길까지만 있어 버린다(실측 1건). 그 대신 `481-13`처럼 **동 없는 번지**도 함께 버려진다
#: (실측 6건) — 도시를 모르면 좌표가 안 나오므로 어차피 쓸 수 없다.
_ROAD: Final = re.compile(r"[가-힣A-Za-z0-9]+\s?(?:로|길)\s*\d")

#: 지번. **실측으로 값을 하는 것만 둔다**(게시판 주소 칸 730건): `동` 20건 · `리` 4건 ·
#: `읍` 1건(`평택시 안중읍 837-13`)이 이 규칙 덕에 남는다.
#: ⚠️ **`가`·`면`은 뺐다 — 살려낸 것이 0건인데 남의 말을 주소로 읽는다**: `휴가 10일`·
#: `지원하시면 50만원`이 걸린다(원문에 있는 글자라 `verify`의 존재 검사도 통과한다).
_JIBUN: Final = re.compile(r"[가-힣]+(?:동|리|읍)\s*\d")

#: 시·군. `전라북도`가 걸리지 않게 `도`가 뒤따르는 경우를 뺀다.
_CITY: Final = re.compile(r"([가-힣]{2,10}(?:시|군))(?![도])")

#: 자치구. 시·군이 없을 때만 쓴다(`서울 관악구`).
_DISTRICT: Final = re.compile(r"([가-힣]{1,6}구)(?![가-힣])")

#: 금액 하나 — **앞의 말 · 숫자 · 뒤의 단위**를 함께 잡는다. 세 조각이 다 필요하다:
#: 앞말이 주기를 정하고(`월 250만원`), 뒷단위가 돈인지 아닌지를 정한다(`24평`은 돈이 아니다).
#: ⚠️ 단위는 **아는 것만 나열한다**. `[가-힣]{1,3}`처럼 아무 글자나 단위로 먹으면
#: `3,200이상`의 `이상`이 단위가 되어 금액이 통째로 사라진다. 목록은 실측에서 뽑았다
#: (숫자 뒤에 실제로 오는 글자 상위 30가지).
_AMOUNT: Final = re.compile(
    r"(?P<lead>[가-힣]{0,4})?\s*(?P<value>\d[\d,]*)\s*"
    r"(?P<unit>천만원|천만|억원|억|만원|만|원|대보험|퍼센트|개월|호봉|시간|"
    r"유로|달러|엔|평|회|대|주|번|일|명|년|%)?"
)

#: 금액 **앞**에 오면 사례비가 아니라는 말. `사택 전세 지원 5천만원`(실측 10건)의 5,000이
#: 사례비 범위로 들어오는 것을 막는다 — 보증금은 사례비가 아니다.
_NOT_PAY_LEADS: Final = (
    "보증금",
    "전세",
    "사택",
    "지원금",
    "건축",
    "헌금",
    "등록금",
    "학비",
    "상여",
)

#: 돈을 뜻하는 단위. **빈 문자열도 돈이다** — `월 100`·`연봉 3600`처럼 단위 없이 쓴다.
#: ⚠️ 이 밖의 단위가 붙은 숫자는 금액이 아니다. 없으면 `아파트(24평)`가 24만원,
#: `국민연금 50% 지원`이 50만원, `4대보험`이 4만원이 된다(실측 `%`만 23건).
_MONEY_UNITS: Final = frozenset({"천만원", "천만", "억원", "억", "만원", "만", "원", ""})

#: 단위 없는 숫자가 이 값을 넘으면 원 단위로 적힌 것이다(`2500000` → 250만원).
_WON_SCALE: Final = 100_000

#: 사례비로 볼 하한(만원). ⚠️ 이게 없으면 **목록 번호가 금액이 된다** — `(1) 사례비는 교회
#: 내규에 따릅니다`가 1만원으로 읽혔다(실측). 사례비가 월 10만원 아래일 수는 없다.
MIN_PAY_MANWON: Final = 10

#: 사례비 상한(만원). 실측 최대 연봉이 4,100만원이라 1억이면 두 배 넘는 여유다.
#: ⚠️ 이전 값(10억)은 아무것도 못 걸렀다 — `사택 보증금 5,000만원`이 그대로 통과했다.
MAX_PAY_MANWON: Final = 10_000

#: 금액 **앞**에 붙어 주기를 말하는 낱말. ⚠️ 글 전체가 아니라 **그 금액 바로 앞**을 본다 —
#: `월 300만원 (연봉 3,600만원)`에서 글 전체를 보면 `연봉`이 이겨 월급이 연봉이 된다(실측 8건).
_YEARLY_LEADS: Final = ("연봉", "년봉", "연")
_MONTHLY_LEADS: Final = ("월급", "월사례", "월평균", "매월", "월")

#: 주기를 말하지 않은 금액의 경계(만원). 실측: 원문이 `월`이라 한 금액은 14~300만원,
#: `연`이라 한 금액은 3,200~4,100만원 — **겹치는 값이 하나도 없다**(사이 구간 0건).
#:
#: ⚠️ 500~1,000 사이는 비워둔다 — 애매한 값에 주기를 찍는 것보다 빈 칸이 낫다.
_MONTHLY_CEILING: Final = 500
_YEARLY_FLOOR: Final = 1_000

#: 상태 필드가 "끝났다"고 말하는 말. 이 필드는 **표시**라 한 단어면 충분하다(`완료`·`마감`).
#: ⚠️ **공백을 지우고 견준다** — `구인 완료`·`구인완료`가 둘 다 쓰인다(실측).
_CLOSED_MARKERS: Final = ("완료", "마감")

#: 제목이 "끝났다"고 말하는 말. ⚠️ **제목에는 `완료`·`마감`만으로는 부족하다** — 제목은 문장이라
#: `조기 마감 될 수 있습니다`(실측 3건)·`6/19 마감`(마감일 안내)이 섞인다. 끝났다고 **단정한**
#: 형태만 받는다.
_CLOSED_TITLE_MARKERS: Final = (
    "청빙완료",
    "초빙완료",
    "구인완료",
    "모집완료",
    "채용완료",
    "청빙이완료",
    "완료되었",
    "마감되었",
    "[마감]",
    "(마감)",
)

#: 마감 표시를 담는 게시판 필드. ⚠️ **본문은 보지 않는다** — `서류는 채용 완료 후 폐기합니다`
#: 같은 안내 문구가 본문에 흔해서(실측 370건) 본문까지 보면 대부분을 잘못 거절한다.
_STATUS_KEYS: Final = ("status", "category", "classification")

#: 제목 앞뒤에 붙는 괄호 묶음. `[]` 434건 · `()` 298건 · `<>` 11건이 실측 전부다.
#: ⚠️ **꼬리표도 본다** — `…청빙합니다.(끌어올림)`이 실측 24건이고, 앞만 보면 그대로 실려 간다.
_TITLE_PREFIX: Final = re.compile(r"^\s*[(\[<]\s*([^)\]>]{1,16})\s*[)\]>]\s*")
_TITLE_SUFFIX: Final = re.compile(r"\s*[(\[<]\s*([^)\]>]{1,16})\s*[)\]>]\s*$")

#: 제목에서 **뗄** 표시. ⚠️ **화이트리스트다** — 괄호를 만나면 무조건 벗기는 것이 아니라
#: 안의 낱말이 이 목록에 있을 때만 뗀다.
#:
#: 실측 3,188건의 머리표는 199가지 723건이고 그중 뗄 것은 343건이다. 나머지는
#: `(대전)`·`(청빙완료)`·`(GOODTV)`·`(초교파)`처럼 **공고 정보**라 남긴다 — 괄호를 무조건
#: 벗기면 지역이 사라지고, 그건 모델이 제목을 다듬을 때 실제로 하던 실수다.
#:
#: ⚠️ 견줄 때 공백과 쉼표를 지운다 — `수정, 끌어올림`·`수정 끌어올림`이 둘 다 쓰인다.
_LIFT_MARKERS: Final = frozenset({"끌어올림", "답글", "수정끌어올림", "끌어올림및수정"})


def place_of(text: str | None) -> tuple[Region | None, str | None]:
    """지역 표기 → (광역, 시·군·구). 게시판 지역 필드 730건에서 둘 다 100% 나온다.

    ⚠️ **시·군이 구보다 앞선다** — `전주시 완산구`는 `전주시`다. 구 이름만으로는 어느 광역인지
    알 수 없어서(`중구`가 여러 곳에 있다) 도시로서 쓸모가 적다.
    """
    if not text:
        return None, None
    stripped = text.strip()
    region = next((value for name, value in _WIDE if name in stripped), None)
    return region, _city_of(stripped)


@dataclass(frozen=True, slots=True)
class _Money:
    """글에서 찾은 금액 하나 — 만원 단위 값과 **그 앞에 붙어 있던 말**."""

    manwon: int
    lead: str


def pay_of(text: str | None) -> tuple[int | None, int | None]:
    """모델이 골라준 **사례비 금액 표현** → (최소, 최대) 만원.

    모델은 어느 숫자가 사례비인지만 고르고(맥락), 단위 환산은 여기서 한다(산술).
    ⚠️ 모델에게 환산까지 시켰더니 `연봉 3,200이상`을 267(월로 나눈 값)로 돌려준 적이 있다.

    ⚠️ **최대는 최소와 같은 주기여야 한다** — `부목사 기준 320만원 + 사택 전세 지원 5천만원`에서
    5,000을 최대로 삼으면 화면에 `월 320~5,000만원`이 뜬다(실측).
    """
    found = _money_in(text)
    if not found:
        return None, None
    low = found[0].manwon
    ceiling = MAX_PAY_MANWON if low > _MONTHLY_CEILING else _MONTHLY_CEILING
    high = next((item.manwon for item in found[1:] if low < item.manwon <= ceiling), None)
    return low, high


def period_of(text: str | None) -> StipendPeriod | None:
    """같은 사례비 표현 → 월급인가 연봉인가.

    ⚠️ **모델에게 묻지 않는다.** 이 칸이 뒤집히면 `월 250만원`이 `연 250만원`이 되어 금액이
    12배 어긋난다 — 관측으로 "지금은 맞다"를 믿기에는 손해가 너무 크다.

    두 가지로 정한다:
    ① **고른 금액 바로 앞의 말**이 정한다(`월 250만원` → 월).
       ⚠️ 글 전체에서 낱말을 찾으면 안 된다 — `월 300만원 (연봉 3,600만원)`에서 `연봉`이 이겨
       월급 300만원이 연봉 300만원이 된다(실측 8건 · 12배 오차).
    ② 앞말이 없으면 **크기**로(`110` → 월 / `3,500` → 연). 실측에서 월 사례비는 300만원을
       넘지 않고 연봉은 3,200만원 아래로 내려가지 않아 경계가 겹치지 않는다.

    ⚠️ 둘 다 아니면 `None`이다 — 애매한 금액에 주기를 찍는 것보다 빈 칸이 낫다.
    """
    found = _money_in(text)
    if not found:
        return None
    stated = _stated_period(found[0].lead)
    if stated is not None:
        return stated
    low = found[0].manwon
    if low <= _MONTHLY_CEILING:
        return StipendPeriod.MONTH
    return StipendPeriod.YEAR if low >= _YEARLY_FLOOR else None


def _stated_period(lead: str) -> StipendPeriod | None:
    """금액 앞의 말이 주기를 말하나. ⚠️ **연을 먼저 본다** — `연봉`이 `월`보다 길고 구체적이다."""
    if any(word in lead for word in _YEARLY_LEADS):
        return StipendPeriod.YEAR
    if any(word in lead for word in _MONTHLY_LEADS):
        return StipendPeriod.MONTH
    return None


def _money_in(text: str | None) -> list[_Money]:
    """돈으로 볼 수 있는 금액만, 원문 순서대로. 앞의 것이 사례비다."""
    if not text:
        return []
    return [item for item in _amounts(text) if MIN_PAY_MANWON <= item.manwon <= MAX_PAY_MANWON]


def address_or_none(text: str | None) -> str | None:
    """주소 모양이면 그대로, 아니면 `None`.

    실측(게시판 주소 칸 730건): 남긴 534건은 전부 정상이고 **주소가 아닌 값은 하나도 살아남지
    않았다**. 버린 196건 중 진짜 주소는 해외 5건(호주 2·뉴질랜드·이스탄불·상하이)뿐이다 —
    한국 도로명·지번 모양만 보므로 걸러진다. **지도 연동이 국내용이라 그대로 둔다**
    (SPEC §5.5b · 넣으려면 라틴 주소 분기를 따로 만들어야 한다).

    ⚠️ **이 규칙은 그물이지 자물쇠가 아니다.** `로`는 조사이기도 하고(`사례비로 100만원`)
    `동`은 낱말 안에도 있어서(`아동1부 교역자`), 주소가 아닌 말이 통과할 수 있다. 수량 단위를
    배제하는 규칙을 붙여 봤더니 **진짜 주소 4건을 잃어서**(`대청로 8 부산영락교회`류) 넣지
    않았다 — 무엇이 주소인지 고르는 것은 프롬프트의 몫이고, 여기는 명백한 비주소를 걷어낸다.
    """
    if text is None:
        return None
    # ⚠️ `strip()`은 BOM(U+FEFF)을 지우지 않는다 — 게시판이 붙여 보낸 실측 1건이 있다.
    value = text.strip().lstrip("\ufeff").strip()
    if not value:
        return None
    return value if _ROAD.search(value) or _JIBUN.search(value) else None


def clean_title(title: str) -> str:
    """게시판 제목 → 저장할 제목. **머리표만 뗀다.**

    ⚠️ **모델에게 묻지 않는다.** 게시판이 준 제목이 이미 원문이고, 목록 페이지에서 받으므로
    포스터 공고에도 항상 있다. 모델에게 맡겼더니 20건 중 6건이 **끝의 마침표를 지웠다** —
    지시하지 않은 손질이고, 원문을 그대로 옮기기로 한 칸에서 그러면 안 된다.

    ⚠️ **뗄 수 없으면 그대로 둔다.** 처음 보는 머리표가 와도 원문이 남는다 — 잘못 떼는 것보다
    안 떼는 쪽이 낫다(`(대전)`을 벗기면 지역이 사라진다).

    ⚠️ **끝의 말줄임(`...`)도 남긴다.** 프롬프트에는 "빼라"고 적혀 있었는데 실측이 그게
    틀렸다고 말한다 — 말줄임으로 끝나는 56건의 앞 글자가 `니`·`고`·`감`처럼 단어 중간이라
    **게시판이 목록에서 자른 표시**다. 떼면 잘린 제목이 온전한 것처럼 보인다.
    (전체 제목은 56건 모두 `raw_html`에 남아 있어 재수집 없이 되살릴 수 있다.)
    """
    remaining = title.strip()
    while (stripped := _without_marker(remaining)) is not None:
        remaining = stripped
    # ⚠️ 표시만 있는 제목이면 벗기지 않는다. `ReviewData`는 빈 제목을 막지 않아서(실측:
    #    `ReviewData(title="")`도 만들어진다) 여기서 비우면 **빈 제목이 조용히 저장된다**.
    return remaining or title.strip()


def _without_marker(title: str) -> str | None:
    """앞이나 뒤의 표시를 하나 뗀 제목. 뗄 것이 없으면 `None`."""
    for pattern in (_TITLE_PREFIX, _TITLE_SUFFIX):
        found = pattern.search(title)
        if found is not None and _marker(found.group(1)) in _LIFT_MARKERS:
            return (title[: found.start()] + title[found.end() :]).strip()
    return None


def _marker(text: str) -> str:
    """표시를 견줄 꼴로. 공백·쉼표는 표기 차이일 뿐이다(`수정, 끌어올림` = `수정 끌어올림`)."""
    return squeeze(text).replace(",", "").lower()


def closed_by_board(title: str, raw_meta: Mapping[str, JsonValue]) -> bool:
    """게시판이 스스로 "끝났다"고 표시했나. **모델에게 묻지 않는다** — 글자만 보면 된다.

    ⚠️ 상태 필드와 제목은 **어휘가 다르다**. 상태 필드는 표시라 `완료` 한 단어로 충분하지만,
    제목은 문장이라 `조기 마감 될 수 있습니다`처럼 마감이 아닌 말이 섞인다.
    """
    status = squeeze(" ".join(str(raw_meta.get(key) or "") for key in _STATUS_KEYS))
    if any(marker in status for marker in _CLOSED_MARKERS):
        return True
    return any(marker in squeeze(title) for marker in _CLOSED_TITLE_MARKERS)


def squeeze(text: str) -> str:
    """공백을 전부 없앤다. **원문과 답을 견줄 때 쓰는 단일 창구**(`verify`도 이걸 쓴다) —
    게시판이 넣는 공백은 줄바꿈·전각·연속이 뒤섞여 있어 그대로 비교하면 늘 어긋난다."""
    return "".join(text.split())


def _city_of(text: str) -> str | None:
    for pattern in (_CITY, _DISTRICT):
        for found in pattern.findall(text):
            name = str(found)
            if not _overlaps_wide(name):
                return name
    return None


def _overlaps_wide(name: str) -> bool:
    """광역 이름을 품은 토막인가. `대구`(→`~구`)·`전남광주통합특별시`(→`~시`) 같은 것들이다."""
    return any(wide in name for wide in _WIDE_NAMES)


def _amounts(text: str) -> list[_Money]:
    """글에 나온 **돈**만 만원 단위로. 순서는 원문 그대로 둔다(앞의 것이 사례비다).

    ⚠️ 단위가 돈이 아니면 버린다 — `24평`·`50%`·`연15일`·`3회`는 금액이 아닌데 걸러내지
    않으면 사례비 자리에 들어앉는다(실측: `아파트(24평)` → 월 24만원).
    """
    squeezed = text.replace(" ", "").replace(",", "")
    found: list[_Money] = []
    for match in _AMOUNT.finditer(squeezed):
        unit = match.group("unit") or ""
        lead = match.group("lead") or ""
        if unit not in _MONEY_UNITS or any(word in lead for word in _NOT_PAY_LEADS):
            continue
        found.append(_Money(manwon=_manwon(int(match.group("value")), unit), lead=lead))
    return found


def _manwon(value: int, unit: str) -> int:
    if unit.startswith("억"):
        return value * 10_000
    if unit.startswith("천만"):
        return value * 1000
    if unit.startswith("만"):
        return value
    # `원`으로 끝나거나 단위가 없는 큰 수는 원 단위다 — 만원으로 내린다.
    if unit == "원" or value >= _WON_SCALE:
        return value // 10_000
    return value
