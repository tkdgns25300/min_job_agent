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
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from minjob_ingest.domain import Region
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

#: 시·군. `전라북도`가 걸리지 않게 `도`가 뒤따르는 경우를 뺀다.
_CITY: Final = re.compile(r"([가-힣]{2,10}(?:시|군))(?![도])")

#: 자치구. 시·군이 없을 때만 쓴다(`서울 관악구`).
_DISTRICT: Final = re.compile(r"([가-힣]{1,6}구)(?![가-힣])")

#: 사례비 금액. `3천만` → 3000만원, `2,500,000`(원 단위) → 250만원.
_AMOUNT: Final = re.compile(r"(\d+)(천만|만)?")

#: 원 단위로 적혔다고 보는 하한. 만원 단위 사례비가 10만(=10억원)일 수는 없다.
_WON_SCALE: Final = 100_000

#: 사례비 상한(만원). 넘으면 사례비가 아니다 — 사택 보증금·건축 헌금 같은 다른 금액을
#: 골라온 것이므로 버린다. min_job 화면에 "5,000만원 월급"이 뜨는 것보다 빈 칸이 낫다.
MAX_PAY_MANWON: Final = 100_000

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


def pay_of(text: str | None) -> tuple[int | None, int | None]:
    """모델이 골라준 **사례비 금액 표현** → (최소, 최대) 만원.

    모델은 어느 숫자가 사례비인지만 고르고(맥락), 단위 환산은 여기서 한다(산술).
    ⚠️ 모델에게 환산까지 시켰더니 `연봉 3,200이상`을 267(월로 나눈 값)로 돌려준 적이 있다.
    """
    if not text:
        return None, None
    amounts = [value for value in _amounts(text) if 0 < value <= MAX_PAY_MANWON]
    if not amounts:
        return None, None
    low = amounts[0]
    high = next((value for value in amounts[1:] if value > low), None)
    return low, high


def closed_by_board(title: str, raw_meta: Mapping[str, JsonValue]) -> bool:
    """게시판이 스스로 "끝났다"고 표시했나. **모델에게 묻지 않는다** — 글자만 보면 된다.

    ⚠️ 상태 필드와 제목은 **어휘가 다르다**. 상태 필드는 표시라 `완료` 한 단어로 충분하지만,
    제목은 문장이라 `조기 마감 될 수 있습니다`처럼 마감이 아닌 말이 섞인다.
    """
    status = _squeeze(" ".join(str(raw_meta.get(key) or "") for key in _STATUS_KEYS))
    if any(marker in status for marker in _CLOSED_MARKERS):
        return True
    return any(marker in _squeeze(title) for marker in _CLOSED_TITLE_MARKERS)


def _squeeze(text: str) -> str:
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


def _amounts(text: str) -> list[int]:
    squeezed = text.replace(" ", "").replace(",", "")
    values: list[int] = []
    for match in _AMOUNT.finditer(squeezed):
        value, unit = int(match.group(1)), match.group(2)
        if unit == "천만":
            value *= 1000
        elif unit is None and value >= _WON_SCALE:
            value //= 10_000
        values.append(value)
    return values
