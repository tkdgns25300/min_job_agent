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

⚠️ **지역은 여기 없다**(2026-08-16 · SPEC §5.5b). `안동시`가 어느 광역인지는 글자가 아니라
지리 지식이라 표로 담으려면 동 이름까지 3,500줄이 된다 → **모델이 답하고 `verify`가 검산한다.**
광역 이름을 글자로 찾던 `place_of`는 그래서 삭제됐다.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from minjob_ingest.domain import Region, StipendPeriod
from minjob_ingest.models import JsonValue

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

#: 원문이 **범위로 적은 표시**. 이것이 있을 때만 최대값을 만든다(`90~120만원`·`95-100만원`).
#: ⚠️ 이 표시가 없는데 금액이 여럿이면 **범위가 아니다** — 자리마다 다른 금액이거나
#: (`전도사 월 160, 강도사 월 170, 목사 월 180`) 사례비가 아닌 돈이 섞인 것이다
#: (`파트전도사 110만원, 최대 450만원 장학금`). 실측 19건 중 9건이 그 모양이었고, 앞의 둘만
#: 집어 `160~170`(180 유실)·`110~450`(장학금을 사례비로)이 저장됐다.
_PAY_RANGE: Final = re.compile(r"\d[\d,]*\s*(?:만원|만)?\s*[~\u223c\u301c\u2013\u2014-]\s*\d")

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

#: 담임목사를 담는 게시판 필드. 실측(2,391건)에서 이 칸이 있는 게시판은 `CSU` 한 곳(646건)이고
#: 값의 61%가 이름만, 나머지는 `○○○ 목사`·`○○○담임목사님`처럼 직함이 붙거나 `본문 참조`다.
_PASTOR_KEYS: Final = ("senior_pastor",)

#: 이름 뒤에 붙는 직함. 앞에 `담임`·`위임`이 오기도 하고(`○○○ 담임목사`) 공백 없이 붙기도
#: 한다(`○○○목사`). ⚠️ `님`까지 뗀다 — `○○○ 목사님`이 실측에 있다.
#: ⚠️ **실측에서 본 것만** 넣는다(`_TITLE_OPEN`과 같은 규율) — `협동`을 넣었다가 뺐다(담임 자리 0건).
_PASTOR_TITLE: Final = r"(?:담임|위임|원로|군종)?\s*목사님?"
_PASTOR_TITLE_TAIL: Final = re.compile(_PASTOR_TITLE + r"$")

#: 이름 하나의 최종 모양 — 공백을 지운 뒤 한글 2~4자.
_PASTOR_PLAIN_NAME: Final = re.compile(r"[가-힣]{2,4}")

#: 이름으로 볼 글자 — 한글 2~4자. `조 상 용`처럼 **한 글자씩 띄어 쓴** 꼴(실측 9건)도 받는다.
#: ⚠️ `청빙 공고`(2+2)는 두 번째 꼴에 걸리지 않는다 — 한 글자씩 띈 셋만 받기 때문이다.
_PASTOR_NAME: Final = r"(?P<name>[가-힣]{2,4}|[가-힣] [가-힣] [가-힣])"

#: 본문에서 담임목사를 말하는 자리(실측 2,569조각의 생김새에서 뽑았다):
#:   `담임목사 : ○○○`(443) · `담임목사 : ○○○ 목사`(312) · `담임목사 ○○○`(274 · 구분자 없음)
#:   `담임목회자 : ○○○위임목사`(8 · 직함이 붙어 있음) · `담임목사: 위임목사 ○○○`(직함이 앞)
#: ⚠️ **공백은 가로만**(`[ \t]`) — `\s`를 쓰면 줄을 넘어 **다음 칸의 값**을 이름으로 읽는다
#:    (실측: `담임목사 :⏎김녕교회`의 교회명 · `담임목사⏎⏎모집인원`의 다음 머리말).
#: ⚠️ **직함은 소유 한정자(`?+`)로 먹는다** — 되돌아가면 `담임목사님과`에서 `님과`가 이름이 된다.
#: ⚠️ 뒤 경계에 `/`가 있다 — `담임목사 ○○○ / 원로목사 ○○○`(실측).
#: ⚠️ **구분자가 없는 꼴은 이 정규식만으로 부족하다** — `#담임목사 소개`·`2. 담임목사 처우`·
#:    `샘물교회 담임목사 청빙공고`가 줄끝 조건을 통과한다. `senior_pastor_of`가 **줄 첫머리나
#:    여는 괄호 뒤**에서 시작한 것만, **3자 이상**만, **존칭 `님`이 없을 때만** 받는다(실측: 이름은
#:    `담임목사 ○○○`처럼 제 줄에 홀로 오고 머리말은 앞에 번호·교회명이 붙으며, `담임목사님` 뒤에는
#:    이름이 아니라 조사·낱말이 온다 — `담임목사님과`·`(담임목사님 이메일)`).
_PASTOR_IN_TEXT: Final = re.compile(
    r"담임[ \t]*(?P<title>목사님|목사|목회자)?+[ \t]*(?:명)?+[ \t]*"
    r"(?:(?P<sep>[:\uff1a\-\u2013\u2014])[ \t]*(?:"
    + _PASTOR_TITLE
    + r"[ \t]*)?+)?"
    + _PASTOR_NAME
    + r"(?=[ \t]*(?:"
    + _PASTOR_TITLE
    + r"|[)\uff09(\uff08,/]|$))",
    re.MULTILINE,
)

#: 구분자 없는 꼴이 **필드로 시작했다**고 볼 바로 앞 글자. 줄 첫머리(82건) 말고 실측에 있는 것은
#: 여는 괄호 `(○○○교회, 담임목사 ○○○)`(24) · 쉼표 `(…, 노회, 담임목사 ○○○)`(30) · 닫는 괄호
#: `○○교회(어느지방) 담임목사 ○○○`(4)다. 마침표(`2. 담임목사 처우`)·붙임표(`-담임목사 이메일`)
#: 뒤는 머리말이라 넣지 않는다. ⚠️ `/`·`:`는 실측 0건이라 넣지 않는다.
_PASTOR_FIELD_OPENERS: Final = frozenset("(\uff08)\uff09,")

#: 구분자 없는 꼴이 받는 최소 글자 수. 2자 머리말(`소개`·`처우`)을 막는다 — 2자 이름은 구분자가
#: 있을 때만 받는다(실측: `담임목사 : 허웅`은 있고 `담임목사 허웅`은 없다).
_PASTOR_BARE_MIN_LEN: Final = 3

#: 이름 자리에 왔지만 이름이 아닌 값. **실측에서 걸린 것만** 둔다 — 구분자 뒤라 구조로는
#: 못 막는다(`담임목사 : 없음`·`담임목사 : 공석(안계심)`·`담임목사 : 아래`·
#: `담임목사 :청빙(임시당회장 ○○○목사)` — 자리가 비어 뽑는 중이라는 뜻이다).
_PASTOR_STOPWORDS: Final = frozenset({"없음", "공석", "아래", "청빙"})

#: 값이 아닌 값 — `본문 참조`·`아래 내용 참조`·`하단 참조`·`-`. 이름이 아니라 "다른 데 보라"다.
_PASTOR_PLACEHOLDER: Final = re.compile(r"참조|^-$")

#: 제목 앞뒤에 붙는 묶음. `[]` 434건 · `()` 298건 · `<>` 11건이 실측이고, 2026-08-22에
#: **별표(`★끌올★`)** 가 더해졌다(1주치 449건 중 1건) — 같은 글자가 양쪽에 오는 꼴이라
#: 여닫이 목록에 함께 넣는다.
#: ⚠️ **꼬리표도 본다** — `…청빙합니다.(끌어올림)`이 실측 24건이고, 앞만 보면 그대로 실려 간다.
#: ⚠️ **실측에서 본 글자만 넣는다.** `☆`를 함께 넣었다가 뺐다(2026-08-22) — 화이트리스트라
#: 안전하다는 것이 근거였지만, 그 논리면 아무 글자나 넣어도 되고 다음 사람이 어느 것이
#: 실측이고 어느 것이 추측인지 구별할 수 없게 된다. 실제로 오면 그때 넣는다.
_TITLE_OPEN: Final = r"(\[<★"
_TITLE_CLOSE: Final = r")\]>★"
_TITLE_PREFIX: Final = re.compile(
    rf"^\s*[{_TITLE_OPEN}]\s*([^{_TITLE_CLOSE}]{{1,16}})\s*[{_TITLE_CLOSE}]\s*"
)
_TITLE_SUFFIX: Final = re.compile(
    rf"\s*[{_TITLE_OPEN}]\s*([^{_TITLE_CLOSE}]{{1,16}})\s*[{_TITLE_CLOSE}]\s*$"
)

#: 괄호 **없이** 구분기호만 붙는 머리표 — `끌어올림- 청소년부 교육목사님 청빙합니다.`
#: 실측 725건 중 1건(0.1%)이고 한 교회가 계속 그 꼴로 올린다. 드물지만 그대로 두면 공개 목록
#: 제목 앞에 남는다(2026-08-21 실제 공개에서 눈에 띄었다).
#: ⚠️ **괄호 형태와 같은 화이트리스트를 쓴다** — 목록에 없는 말은 건드리지 않으므로
#: `대구성북교회- 부목사 청빙` 같은 제목은 그대로 남는다.
#: ⚠️ 구분기호에 붙임표·en/em 대시·콜론을 함께 넣는다 — 게시판마다 다르게 쓴다.
_TITLE_DELIMITERS: Final = "-\u2013\u2014:"
_TITLE_BARE_PREFIX: Final = re.compile(
    r"^\s*([^\s()\[\]<>]{1,16})\s*[" + _TITLE_DELIMITERS + r"]\s*"
)

#: 괄호 **없이** 뒤에 붙는 꼬리표 — `[…모십니다] 끌어올림`. 실측 2,270건 중 1건(2026-08-26,
#: 운영자가 검수 화면에서 발견). 머리표 쪽과 같은 이유로 둔다: 드물지만 그대로 두면 공개 목록
#: 제목 끝에 남는다.
#: ⚠️ **앞에 공백이나 구분기호를 요구한다** — 없으면 `청빙끌어올림`처럼 낱말 중간을 자른다.
#: ⚠️ 화이트리스트를 지나므로 `부목사 청빙`의 `청빙`은 건드리지 않는다.
_TITLE_BARE_SUFFIX: Final = re.compile(
    r"[" + _TITLE_DELIMITERS + r"\s]\s*([^\s()\[\]<>]{1,16})\s*$"
)

#: 제목에서 **뗄** 표시. ⚠️ **화이트리스트다** — 괄호를 만나면 무조건 벗기는 것이 아니라
#: 안의 낱말이 이 목록에 있을 때만 뗀다.
#:
#: 실측 3,188건의 머리표는 199가지 723건이고 그중 뗄 것은 343건이다. 나머지는
#: `(대전)`·`(청빙완료)`·`(GOODTV)`·`(초교파)`처럼 **공고 정보**라 남긴다 — 괄호를 무조건
#: 벗기면 지역이 사라지고, 그건 모델이 제목을 다듬을 때 실제로 하던 실수다.
#:
#: ⚠️ 견줄 때 공백과 쉼표를 지운다 — `수정, 끌어올림`·`수정 끌어올림`이 둘 다 쓰인다.
#: ⚠️ 2026-08-22 실측(288건)으로 셋을 더했다 — `끌올`(축약) · `다시올림` · 그리고 별표에 싸인
#: `끌올`. 같은 실측에서 **남긴** 것이 28건이다: 지역 15(`[군산]`·`(부산)`) · 교회명 8 ·
#: `[재공고]` 4. 재공고는 게시판 끌어올림 표시가 아니라 **그 공고의 사실**이라 남긴다
#: (`(청빙완료)`를 남기는 것과 같은 성격).
_LIFT_MARKERS: Final = frozenset(
    {"끌어올림", "끌올", "다시올림", "답글", "수정끌어올림", "끌어올림및수정"}
)

#: 이름에서 떼는 것 — 괄호 안(지역·교단 표기)과 공백·기호.
#: `[군산] 개복교회(전북 군산)` → `개복교회` · `세상의 빛 이레교회` → `세상의빛이레교회`
#: ⚠️ `dedup`(자물쇠 키)과 `heresy`(목록 대조)가 **같은 정규식**을 쓴다 — 한쪽만 고치면 같은
#:    교회가 한 곳에서는 같고 다른 곳에서는 다른 이름이 된다.
NAME_BRACKETS: Final = re.compile(r"[\uff08(\[\u3010][^)\uff09\]\u3011]*[)\uff09\]\u3011]")
NAME_NOISE: Final = re.compile(r"""[\s\u00b7\u30fb.,'"!?~\-\u2013\u2014_/]""")


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

    ⚠️ **범위는 원문이 범위라고 적었을 때만 만든다**(`_PAY_RANGE`). 금액이 여럿인데 그 표시가
    없으면 **둘 다 비우고** 원문을 `pay_note`가 담는다 — 그 자리에서 금액을 고르면 틀린 값이
    공개된다(실측: `160~170`으로 목사 180이 사라지고, 장학금 450이 사례비 최대가 됐다).
    빈 칸이 아니라 원문이 남으므로 지원자가 보는 정보는 오히려 자세하다.

    ⚠️⚠️ **주기를 못 정한 금액도 비운다**(2026-08-21 · min_job 지적). 금액 하나만으로는 월급인지
    연봉인지 말할 수 없고, `jobs.pay_period`가 `NOT NULL DEFAULT 'MONTH'`라 **주기 없이 금액만
    내보내면 연봉이 월급으로 굳는다**(12배). 위험 구간은 `501~999`만원 — 그 아래는 월,
    그 위는 연으로 정해진다(`period_of`). 지금까지 그런 행이 0건이었던 것은 사역직 게시판이
    주기를 대개 적기 때문이고, **일반직 소스가 붙으면 깨질 관찰**이라 규칙으로 못 박는다.
    """
    found = _money_in(text)
    if not found:
        return None, None
    if period_of(text) is None:
        return None, None
    low = found[0].manwon
    ceiling = MAX_PAY_MANWON if low > _MONTHLY_CEILING else _MONTHLY_CEILING
    others = [item.manwon for item in found[1:] if low < item.manwon <= ceiling]
    if not others:
        return low, None
    if _PAY_RANGE.search((text or "").replace(" ", "")) is None:
        return None, None
    return low, others[0]


def pay_note_of(amount: str | None, note: str | None) -> str | None:
    """저장할 사례비 설명. **금액을 쓸 수 없을 때 그 표현을 여기로 옮긴다.**

    ⚠️ `pay_of`가 금액을 비우는 경우(범위 표시 없이 금액이 여럿 · 위)에 이 함수가 없으면
    **사례비가 통째로 사라진다** — 모델이 뽑은 표현은 `pay_amount`에 있는데 그 칸은 저장되지
    않는다(만원 환산의 입력일 뿐이다). 실측 19건 중 9건이 그 모양이었다.

    모델이 준 설명(`교회 내규에 따름`)이 이미 있으면 **그것을 앞에 두고** 금액 표현을 잇는다 —
    둘 다 원문이고 어느 쪽도 버릴 이유가 없다. ⚠️ 한쪽이 다른 쪽에 들어 있으면 긴 것만 남긴다:
    모델이 같은 문장을 두 칸에 나눠 담는 일이 흔해서(`교회에서 정한 연봉제` / `교회에서 정한
    연봉제(전임 3000, 부목사 3600)`) 그대로 이으면 같은 말이 두 번 보인다.
    """
    if pay_of(amount) != (None, None):
        return note
    parts = [value.strip() for value in (note, amount) if value and value.strip()]
    kept = [
        value
        for index, value in enumerate(parts)
        if not any(value in other for position, other in enumerate(parts) if position != index)
    ]
    joined = " · ".join(dict.fromkeys(kept or parts[:1]))
    return joined or None


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


#: 광역 이름이 `city` 앞에 겹쳐 오는 8곳. ⚠️ **특별·광역시만 담는다** — 실측 434건에서 겹침은
#: 여기서만 일어났고(`서울시 송파구`·`대구광역시 달서구`), **도 지역은 겹치지 않았다**
#: (`GYEONGGI`+`성남시`는 도시 이름이지 도 이름이 아니다).
#:
#: ⚠️ **`region`을 기준으로만 뗀다.** 실측에 `GYEONGGI`+`광주시`(경기도 광주시)가 있다 —
#: 이름만 보고 떼면 그 도시가 사라진다. 그 행의 광역과 같을 때만 겹침이다.
_METRO_NAMES: Final[Mapping[Region, str]] = {
    Region.SEOUL: "서울",
    Region.BUSAN: "부산",
    Region.DAEGU: "대구",
    Region.INCHEON: "인천",
    Region.GWANGJU: "광주",
    Region.DAEJEON: "대전",
    Region.ULSAN: "울산",
    Region.SEJONG: "세종",
}

#: 광역 이름에 붙는 꼬리. 실측 세 꼴이 전부다(`서울`·`서울시`·`서울특별시`).
_METRO_SUFFIXES: Final = ("특별시", "광역시", "시", "")


#: 접수 이메일 한 개. **`dedup`(자물쇠를 가르는 메일함)이 같은 정규식을 쓴다** — 사본을 두면
#: 여기서 남긴 주소를 그쪽이 못 뽑아 같은 자리가 갈린다(`NAME_BRACKETS`와 같은 이유).
#: ⚠️ `verify._EMAIL_TOKEN`은 **일부러 더 느슨하다** — 그쪽은 "원문에 이 조각이 있나"를 찾는
#:    쪽이라 좁히면 있는 것을 못 찾는다. 여기는 반대로 **주소가 아닌 글자를 버리는** 쪽이다.
MAIL_TOKEN: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: 주소가 여럿일 때 잇는 글자. 원문은 `/`·`,`·공백으로 제각각 적는다(실측).
_EMAIL_JOINER: Final = ", "


def emails_only(value: str | None) -> str | None:
    """접수 이메일에서 **주소만** 남긴다. 여럿이면 여럿 다 남긴다.

    ⚠️ **모델이 틀린 것이 아니다** — 프롬프트가 원문 조각을 그대로 옮기라고 시켰고 원문이
    `메일주소: sunkuk47@gmail.com (행정목사 전선국)`이면 그렇게 쓰는 것이 맞다. `verify.email`도
    같은 이유로 주소만 견준다("이름을 떼지 말라고 시킨 것이 프롬프트다"). **글자만 보면 되는
    변환은 코드 몫**이다(CLAUDE.md · `city_without_region`과 같은 자리).

    ⚠️ 이름이 붙은 채로 두면 **min_job이 `mailto:`로 쓸 때 깨진다** — 실측 2026-09-04:
    이메일이 있는 2,722건 중 37건이 순수 주소가 아니었고 그중 11건이 이미 공개돼 있었다.

    ⚠️⚠️ **주소가 여럿이면 버리지 않는다**(실측 37건 중 **15건**). `KKT59123@daum.net /
    KYL0021@daum.net`은 교회가 두 곳으로 받는다는 뜻이라 하나만 남기면 정보를 버리는 것이다.
    구분자만 통일한다.

    ⚠️⚠️ **하나도 못 뽑으면 원래 값을 그대로 둔다**(실측 1건 · `holyland22@hanmail,net` —
    교회가 점 대신 쉼표를 적었다). 비우면 그 공고는 연락처가 없어져 승격 게이트에 걸린다.
    오타를 고치는 것은 지어내는 것이고, 여기서 할 일이 아니다.

    ⚠️ 담당자 이름은 이 칸에서만 사라진다 — 원문(`source_data.raw_text` · write-once)과
    지원 절차(`process_steps`)에 남는다.
    """
    if value is None:
        return None
    found = MAIL_TOKEN.findall(value)
    return _EMAIL_JOINER.join(dict.fromkeys(found)) if found else value


def city_without_region(city: str | None, region: Region | None) -> str | None:
    """`city` 앞에 겹쳐 온 광역 이름을 뗀다. 뗄 것이 없으면 그대로.

    ⚠️ **모델이 틀린 것이 아니다** — 프롬프트가 `시·군·구 표기를 원문 글자 그대로`라고 시켰고,
    원문이 `서울시 송파구`면 그렇게 쓰는 것이 맞다. **겹침을 없애는 것은 코드 몫**이다
    (CLAUDE.md: 맥락 없이 글자만 보면 되는 변환).

    ⚠️ 실측 434건 중 **118건(27%)** 이 겹쳐 있었고 표기가 넷으로 갈렸다(`서울시`·`서울특별시`·
    `대구`·`대구시`). 그대로 두면 min_job이 `region`과 나란히 보여줄 때 **"서울 · 서울시 송파구"**
    가 되고, 도시로 묶는 화면에서 같은 곳이 여러 항목으로 갈린다.

    ⚠️ **떼고 나서 아무것도 안 남으면 `None`이다** — `서울시`만 온 행은 도시 정보가 없다.

    ⚠️⚠️ **광역 이름 뒤가 공백이어야 뗀다**(2026-08-22 실측으로 잡았다). 붙어 있으면 그 이름의
    일부다 — `부산진구`는 부산의 자치구이고 `부산`을 떼면 **`진구`라는 없는 지명**이 된다.
    실측 434건에 그 행이 1건 있었다. 같은 꼴이 앞으로도 온다(`부산진구`가 그 부류의 전부는
    아닐 수 있다) — 그래서 이름을 열거하지 않고 **경계로** 막는다.
    """
    if city is None or region is None:
        return city
    name = _METRO_NAMES.get(region)
    if name is None:
        return city
    for suffix in _METRO_SUFFIXES:
        head = f"{name}{suffix}"
        if not city.startswith(head):
            continue
        rest = city[len(head) :]
        if rest and not rest[0].isspace():
            # `부산진구` — 광역 이름처럼 시작하지만 그 자체가 도시 이름이다.
            continue
        return rest.strip() or None
    return city


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
    안 떼는 쪽이 낫다(`(대전)`을 벗기면 지역이 사라진다). 괄호 없이 붙는 꼴(`끌어올림- 제목`)도
    같은 화이트리스트를 지나므로, 목록에 없는 말은 구분기호가 있어도 남는다.

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
    for pattern in (_TITLE_PREFIX, _TITLE_SUFFIX, _TITLE_BARE_PREFIX, _TITLE_BARE_SUFFIX):
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


def senior_pastor_of(raw_text: str, raw_meta: Mapping[str, JsonValue]) -> str | None:
    """담임목사 이름 — 게시판 폼에 있으면 그것, 없으면 본문에서. **모델에게 묻지 않는다.**

    이단 대조(`heresy`)가 목록 항목(사람 이름)과 견주는 값이다. 글자만 보면 되는 변환이라
    코드의 몫이고(모듈 docstring), 유료 호출 없이 검증된다.

    ⚠️ **모양이 어긋나면 `None`이다** — 두 사람이 적혔거나(`공동담임`) 이름 자리에 다른 말이
    왔으면 비운다. 이 값은 근거에 그대로 실리고 **확정 거절의 조건**이 되므로 빈 칸이 틀린
    값보다 낫다.

    ⚠️ **폼이 이긴다.** 본문의 `담임`은 절반이 다른 뜻이다(`담임목사 추천서`·`담임목사님과
    함께` · 실측 2,569조각) — 폼은 그 칸의 뜻이 하나다. 폼이 `본문 참조`라고 하면 그 말대로
    본문을 본다. 폼에 진짜 값이 있는데 모양이 어긋나면(두 사람) 본문으로 내려가지 않는다 —
    본문도 같은 두 사람을 적었을 것이고, 폼이 정한 답(없음)을 본문이 뒤집을 이유가 없다.
    """
    for key in _PASTOR_KEYS:
        value = raw_meta.get(key)
        if isinstance(value, str) and value.strip() and not _PASTOR_PLACEHOLDER.search(value):
            return _pastor_from_form(value)
    for found in _PASTOR_IN_TEXT.finditer(raw_text):
        name = _pastor_name(found.group("name"))
        if name is None:
            continue
        if found.group("sep") is None and not _is_bare_pastor_field(raw_text, found, name):
            continue
        return name
    return None


def _is_bare_pastor_field(text: str, found: re.Match[str], name: str) -> bool:
    """구분자 없는 `담임목사 ○○○`이 **필드**인가 — 줄 첫머리·여는 괄호 뒤에서 시작하고, 3자
    이상이고, 존칭이 없다.

    `#담임목사 소개`·`2. 담임목사 처우`·`샘물교회 담임목사 청빙공고`는 앞에 다른 글자가 있고,
    `담임목사 ○○○`·`(담임목사 ○○○)`는 없다(실측). 2자는 받지 않는다 — `소개`·`처우`·`이멜`.
    ⚠️ `담임목사님 ○○○`은 받지 않는다 — 존칭 뒤에는 이름이 아니라 낱말이 온다(실측:
    `(담임목사님 이메일)`). 구분자가 있으면 상관없다(`담임목사님: ○○○`은 필드다).
    """
    if len(name) < _PASTOR_BARE_MIN_LEN or found.group("title") == "목사님":
        return False
    start = found.start()
    before = text[:start].rstrip(" \t")
    return not before or before[-1] in _PASTOR_FIELD_OPENERS or before.endswith("\n")


def _pastor_from_form(value: str) -> str | None:
    """폼 값 → 이름. 괄호 안(`(임시당회장)`)을 떼고 직함을 뗀다.

    ⚠️ **쉼표·빗금이 있으면 두 사람이다**(`○○○(온누리), △△△(새순)` · 공동담임) — 한 사람을
    골라 적으면 틀린 값이 근거에 실린다. 비운다.
    """
    bare = NAME_BRACKETS.sub("", value)
    if any(mark in bare for mark in ",/·"):
        return None
    return _pastor_name(_PASTOR_TITLE_TAIL.sub("", squeeze(bare)))


def _pastor_name(text: str) -> str | None:
    """이름으로 볼 수 있으면 그 이름, 아니면 `None`. **한글 2~4자**만 이름이다."""
    name = squeeze(text)
    if not _PASTOR_PLAIN_NAME.fullmatch(name) or name in _PASTOR_STOPWORDS:
        return None
    return name


def squeeze(text: str) -> str:
    """공백을 전부 없앤다. **원문과 답을 견줄 때 쓰는 단일 창구**(`verify`도 이걸 쓴다) —
    게시판이 넣는 공백은 줄바꿈·전각·연속이 뒤섞여 있어 그대로 비교하면 늘 어긋난다."""
    return "".join(text.split())


def name_key(name: str) -> str:
    """이름을 **같은 이름인지 견줄 열쇠**로 — 유니코드 정규화 + 괄호 제거 + 공백·기호 제거 + 소문자.

    `heresy`가 목록과 대조할 때 쓴다. 게시판마다 같은 교회를 `서머나 교회`·`서머나교회(창원)`·
    `서머나·교회`로 적고, 그대로 견주면 **이름을 조금만 다르게 적어도 목록을 통과한다.**

    ⚠️ **NFKC를 거른다**(`verify`와 같은 기준). 분해형(NFD)이나 전각으로 오면 눈에 같아 보이는
    이름이 다른 글자다 — 첨부 파일명에 NFD를 쓰는 게시판이 있어 본문에도 언제든 올 수 있다.

    ⚠️ **`dedup`의 자물쇠 키는 이 함수를 쓰지 않는다**(`normalize_church_name`). 같은 정규식을
    쓰되 NFKC·소문자는 하지 않는다 — 키를 바꾸면 이미 판정된 2,300여 건이 전부 다시 판정된다.
    그 전환은 실측을 두고 따로 결정한다.
    """
    folded = unicodedata.normalize("NFKC", name)
    return NAME_NOISE.sub("", NAME_BRACKETS.sub("", folded)).lower()


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
