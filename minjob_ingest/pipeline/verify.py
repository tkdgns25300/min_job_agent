"""모델 답이 **원문에 실제로 있나**를 검사한다. 없으면 그 칸만 비운다.

`normalize.py`가 "코드가 대신 한다"면 여기는 "모델이 하되 코드가 검산한다"이다. 둘을 나눈
이유는 측정이다 — 지역·금액처럼 맥락이 필요 없는 변환은 코드가 대신할 수 있지만, 직분·부서는
코드가 대신하면 **모델보다 많이 틀린다**(실측 72% · 그중 대부분이 담임목사 오검출).
대신할 수 없으면 검산이라도 해야 한다.

무엇을 잡나 — 실측(실제 모델 답 20건):

    원문   "게시판: DAESHIN"
    모델   raw_denomination = "DAESHIN"     ← 게시판 키가 교단 칸에 들어갔다. **비운다**

무엇을 잡지 않나 — 프롬프트가 시킨 재구성:

    원문   "제출서류: … 가족관계증명서, 주민등록 등본 각1통"
    모델   ["가족관계증명서 1통", "주민등록 등본 1통"]     ← **맞는 답**이다. 세기만 한다

⚠️ 처음에는 둘을 같이 비웠고, 그때 54개를 비워 그중 **진짜 오류는 1개**였다. 칸의 성질로
가른 뒤에는 3개를 비운다.

⚠️ **공고를 버리지 않고 그 칸만 비운다.** 운영자 기준이 "빈 칸 > 틀린 값"이다.

⚠️ **그림·PDF가 있는 공고에서는 비우지 않는다**(실측 257건 · 8%). 포스터에만 있는 값은 본문에
없는 것이 정상이라 지어낸 것과 구분할 수단이 없다 — 세어서 운영자에게 넘긴다. 여기서 비우면
포스터 공고 117건이 통째로 빈 채 저장된다.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from minjob_ingest.domain import Department, EmploymentType, Position, Qualification
from minjob_ingest.models import SourceData
from minjob_ingest.pipeline.extraction import Extraction, meta_lines

#: 근거가 그 값을 **뒷받침하나**. 값마다 (있어야 할 낱말, 있으면 안 되는 낱말)이다.
#:
#: ⚠️ 이 표는 **분류를 다시 하지 않는다** — 근거가 그 칸과 아무 상관 없는 글자일 때만 잡는다.
#: 분류를 코드로 대신하려던 시도는 실측 72%에 그쳤다. 여기서 하는 일은 "`부목사 1명`이라
#: 해놓고 SENIOR_PASTOR를 고르지는 않았나"까지다.
#:
#: ⚠️ 배제 낱말이 있는 이유: 낱말이 서로를 품는다. `준전임`은 `전임`을 품어서 배제가 없으면
#: `준전임 사역자`가 FULL_TIME을 뒷받침한다(실측 확인).
#:
#: ⚠️ **영문 공고가 있다**(실측 17건 · CSU 9 · WGST 5 · TTGU 2 · PUTS 1). 한글만 두면 그
#: 공고들은 근거가 멀쩡해도 전부 지워진다 — `Experience in secretarial work`가 경력이 아닌
#: 것이 된다. 낱말은 `_squeeze`로 공백이 지워진 뒤 소문자로 견주므로 `parttime`처럼 붙여서도
#: 적는다.
#:
#: ⚠️ `Position.ETC`·`Department.ETC`는 표에 없다 — "그 밖"이라 뒷받침할 낱말이 없다(통과).
#: 그 둘 말고 빠진 값이 생기면 `supports`가 조용히 통과시키므로 적합성 테스트가 막는다.
_SUPPORTING_WORDS: Final[dict[StrEnum, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    Position.SENIOR_PASTOR: (("담임", "위임", "원로", "seniorpastor", "leadpastor"), ()),
    # ⚠️ `담임목사`가 `목사`를 품는다 — 배제하지 않으면 담임 청빙이 부목사로 저장된다.
    Position.ASSOCIATE_PASTOR: (
        ("목사", "associatepastor", "assistantpastor"),
        ("담임", "위임", "원로", "seniorpastor", "leadpastor"),
    ),
    Position.EVANGELIST: (("전도사", "evangelist"), ()),
    Position.LICENSED_MINISTER: (("강도사",), ()),
    Department.INFANT: (("영아", "유아", "유치", "미취학", "infant", "preschool", "toddler"), ()),
    Department.CHILDREN: (("유년", "초등", "아동", "어린이", "children", "elementary"), ()),
    Department.YOUTH: (
        ("중고등", "청소년", "중등", "고등", "중학", "고교", "youth", "teen"),
        (),
    ),
    Department.YOUNG_ADULT: (("청년", "대학", "youngadult", "college", "campus"), ()),
    Department.DISTRICT: (("교구", "심방"), ()),
    Department.WORSHIP: (
        ("찬양", "예배", "성가", "지휘", "반주", "미디어", "음악", "worship", "music"),
        (),
    ),
    Department.ADMIN: (
        ("행정", "사무", "관리", "administrative", "administration", "secretarial", "office"),
        (),
    ),
    # ⚠️ `상근`·`정규직`이 없으면 `평일은 상근직으로 8시간`(실측 9건)이 걸러진다.
    EmploymentType.FULL_TIME: (
        ("전임", "풀타임", "상근", "정규직", "full"),
        ("준전임", "반전임", "parttime", "part-time"),
    ),
    EmploymentType.SEMI_FULL_TIME: (("준전임", "반전임", "세미"), ()),
    EmploymentType.PART_TIME: (("파트", "part"), ()),
    Qualification.SEMINARIAN: (
        ("신대원", "신학대학원", "신학교", "재학", "졸업", "신학", "seminary", "m.div", "mdiv"),
        (),
    ),
    Qualification.ORDAINED: (("안수", "목사", "ordained", "ordination"), ()),
    # ⚠️ `경험`이 없으면 `사역 경험이 있는 분`(실측 290건)이 걸러진다.
    Qualification.EXPERIENCED: (("경력", "경험", "experience", "experienced"), ()),
    Qualification.ENTRY: (("신입", "졸업예정", "초임", "entrylevel"), ()),
    Qualification.ANY: (("무관", "제한", "누구나", "anyone", "regardlessof"), ()),
}

#: 근거에 **모집한다는 말**까지 있어야 하는 값. **담임 계열 하나뿐이다.**
#:
#: ⚠️ 낱말만으로는 `담임목사: 박은제`(연락처)와 `담임목사 청빙`(모집)을 못 가른다. 둘 다
#: `담임`을 담고 둘 다 원문에 있다 — 실측 **1,336건**이 `담임목사:` 꼴을 담고 있어 오검출
#: 표면이 크고, 담임 청빙과 부교역자 청빙은 전혀 다른 자리라 되돌릴 수 없다.
#:
#: ⚠️ **담임에만 건다.** 부목사·전도사는 연락처에 서명으로 등장하는 일이 거의 없고, 근거가
#: `부목사 1명`처럼 모집어 없이 오는 것이 정상이다 — 거기까지 걸면 맞는 값을 잃는다.
#:
#: ⚠️ 대가: 제목이 `강릉주사랑교회 담임목사`처럼 모집어 없이 끝나는 공고 5건(226건 중 2%)이
#: 비워진다. 빈 칸은 검수가 채울 수 있고 틀린 담임 청빙은 그대로 공개된다.
_NEEDS_RECRUITING_WORD: Final = frozenset({Position.SENIOR_PASTOR})

#: 모집한다는 말. 실측 담임 청빙 226건 중 221건(97%)이 제목에 이 중 하나를 쓴다.
#:
#: ⚠️ **`명`을 넣지 않는다.** 흔한 음절이라 소`명`교회·개`명`교회·증`명`서·성도 400`명`이 전부
#: 통과한다 — 실측 117줄이 모집과 무관한데 `명` 하나로 담임 청빙을 뒷받침했다.
#: ⚠️ `초청`도 뺐다 — `담임목사 초청 서신`(담임목사가 쓴 글)이 통과했다(실측 3줄).
_RECRUITING_WORDS: Final = ("청빙", "초빙", "모집", "채용", "구함", "구합", "모십", "찾습")

#: 검산에서 무시할 글자. 원문과 모델 답이 공백·줄바꿈만 다른 경우가 흔하다.
_WHITESPACE: Final = re.compile(r"\s+")

#: 원문 칸 사이 경계. 값에 나올 수 없는 글자여야 한다.
_BOUNDARY: Final = "\x00"

#: 전화번호를 숫자열로 견줄 때 지울 것 · 링크에서 뗄 스킴.
_DIGITS: Final = re.compile(r"\D")
_NOT_ALNUM: Final = re.compile(r"[^0-9A-Za-z가-힣]")
_SCHEME: Final = re.compile(r"^https?://")

#: 제목의 괄호 묶음. 담임목사 **이름**이 여기 들어간다 — 뽑는 자리와 구별해야 한다.
_PARENTHESES: Final = re.compile("[(\\[<\uff08][^)\\]>\uff09]*[)\\]>\uff09]")

#: 번호 하나로 볼 덩어리 — 숫자로 시작해 숫자·하이픈·점만 이어진 곳. 쉼표·괄호·공백·한글에서
#: 끊긴다(그래야 번호 둘이 한 덩어리로 붙지 않는다).
_PHONE_CHUNK: Final = re.compile(r"[0-9][0-9.\-]{5,}")

#: 스팸을 피하려 한글로 쓴 숫자(실측 16건 — `010-2720-구육구이`·`010-오18칠-칠칠오오`).
#: ⚠️ 프롬프트가 이걸 숫자로 되돌리라 시키므로, **원문 쪽만** 같이 되돌린다. 안 하면 시킨
#: 대로 한 답이 늘 "원문에 없다"가 된다.
#:
#: ⚠️ **모델 답에는 절대 적용하지 않는다.** 여기 글자들은 이름에 흔해서(`목사`의 `사`=4 ·
#: `송준영`의 `영`=0) 답에 붙은 담당자 이름이 숫자로 둔갑한다 — 그런데 이름을 떼지 말라고
#: 시킨 것도 프롬프트다. 실측 2026-08-14: 전화번호 4건이 전부 이것 때문에 지워졌고
#: (`010-2285-1151 (김준수 목사)` → `010228511514`) 원문에는 멀쩡히 있었다.
_KOREAN_DIGITS: Final = {
    "공": "0",
    "영": "0",
    "일": "1",
    "이": "2",
    "삼": "3",
    "사": "4",
    "오": "5",
    "육": "6",
    "칠": "7",
    "팔": "8",
    "구": "9",
}


@dataclass(frozen=True, slots=True)
class Dropped:
    """검산이 버린 값 하나. **칸 이름만으로는 과검을 검수할 수 없다**(2026-08-14 실측).

    `field`는 칸, `value`는 모델이 답한 값, `evidence`는 모델이 댄 근거(대문자 값을 고르는
    칸에만 있다). 코드가 만든 값(사례비·지역·마감)은 `value`에 **그 값을 만든 원문 조각**이
    들어간다 — 버려진 것이 그 조각이기 때문이다.
    """

    field: str
    value: str
    evidence: str | None = None


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """한 공고의 검산 결과. **비운 칸을 세어 리포트로 올린다** — 조용히 지우지 않는다."""

    #: 비운 **값**마다 그 칸 이름. ⚠️ 칸 단위가 아니라 값 단위다 — `unverifiable`과 단위가
    #: 달라 리포트에서 그림 공고가 세 배 나빠 보이던 문제가 있었다.
    scrubbed: tuple[str, ...] = ()
    #: 비운 값 하나하나 — **무엇을 왜 버렸나**. `scrubbed`가 개수라면 이건 내용이다.
    #: ⚠️ `--out` 미리보기에만 실린다. 지어낸 값일 수 있으므로 `review_data`로 가지 않는다.
    dropped: tuple[Dropped, ...] = ()
    #: 원문에서 못 찾았지만 **비우지 않은** 값의 수. 그림·PDF 공고에서만 생긴다.
    unverifiable: int = 0
    #: 조립 칸에서 원문과 어긋난 **값의 수**. ⚠️ **"잘못이 아니다"가 아니다** — 프롬프트가
    #: 이으라고 시킨 결과일 수도, 지어낸 것일 수도 있고 **코드는 둘을 구분하지 못한다**.
    #: 비우지 않는 이유는 실측에서 조립이 압도적이어서일 뿐이다(23개 중 지어낸 것 0개).
    #: 이 숫자는 `confidence`를 낮추는 근거이자 프롬프트 수정의 신호다.
    unchecked: int = 0
    unchecked_fields: Mapping[str, int] = field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        return not self.scrubbed and not self.unverifiable


def verify(
    record: SourceData, extraction: Extraction, *, media_sent: bool
) -> tuple[Extraction, VerifyReport]:
    """모델 답을 원문과 대조해 못 믿을 칸을 비운다.

    ⚠️ **비우는 칸과 세기만 하는 칸이 다르다.** 갈리는 기준은 "프롬프트가 조립을 시켰나"다.

    **비운다 — 원문에서 한 조각을 그대로 옮기는 칸**
    `church_name`·`raw_denomination`·`role`·`start_timing`·`work_days`·`contact_*`, 그리고
    근거로 검산하는 대문자 값 넷과 코드가 만든 값들. 실측 20건에서 42개 중 **1개만 비웠고
    그 1개가 진짜 오류**였다(`raw_denomination = "DAESHIN"` — 게시판 키가 교단 칸에 들어갔다).

    **세기만 한다 — 프롬프트가 여러 조각을 이으라고 시킨 칸**
    `headcount`(`1.부목사 2.교육목사`) · `housing_note`·`pay_note`·`benefit_note`(자리마다 다른
    처우를 한 줄로) · 목록 다섯 칸(`항목 하나에 한 가지씩` — 표와 문장을 항목으로 편다).
    ⚠️ 실측: 목록 칸에서 23개가 어긋났는데 **지어낸 것은 0개**였다. `각1통`을 `1통` 둘로
    나누고 표의 `공통` 열을 항목마다 붙인 것들이라 **모델이 옳았다** — 시켜놓고 벌하지 않는다.

    **아예 보지 않는다**
    `is_church_recruitment`·`job_kind`(글 전체를 읽는 판단이라 가리킬 한 곳이 없다) ·
    `description`(새로 쓰는 글) · `housing_provided`(참·거짓이라 대조할 글자가 없다).
    ⚠️ `housing_provided`와 `housing_note`가 둘 다 안 걸러진다 — **사택 이야기 전체가 무검증**이다.
    `confidence` 규칙이 붙을 때까지 남는 구멍이다.

    `region`·`city`·`pay_*`·`deadline`은 **코드가 만든 값이지만 검산한다** — 그 값을 만든 원문
    조각(`Evidence`)이 원문에 있어야 남긴다. 숫자·날짜만 보면 지어낸 값도 멀쩡해 보인다.
    """
    # ⚠️ 면제는 **모델이 실제로 그림을 봤을 때만**이다. URL 목록으로 정하면 `file:///C:\…`만
    #    있는 공고 5건(가져올 수 없어 아무것도 안 보냈다)까지 면제된다.
    parts = _source_parts(record)
    checker = _Checker(
        haystack=_BOUNDARY.join(parts),
        title=record.title,
        parts=parts,
        may_scrub=not media_sent,
    )
    known = extraction.evidence
    # ⚠️ 코드가 만든 값(사례비·지역)은 **그 근거가 원문에 있을 때만** 남긴다 — 모델이 금액
    #    표현을 지어내면 3200이라는 숫자 자체는 멀쩡해 보여서 아래 검산으로는 안 걸린다.
    pay_grounded = checker.grounds("pay_amount", known.pay_amount)
    place_grounded = checker.grounds("location", known.location)
    date_grounded = checker.grounds("deadline", known.deadline)
    # ⚠️ 조립 칸은 **세기만 한다** — 어긋나는 것이 정상이라 비우면 맞는 값을 잃는다. 그래도
    #    숫자는 남겨 프롬프트를 고쳤을 때 나아졌는지가 리포트에 보이게 한다.
    # ⚠️ `role`은 **원문에서 오려낸 조각이 아니라 짧게 고쳐 쓴 직무명**이다(min_job DATA.md §3:
    #    자유 텍스트 · 통제 목록 아님). 글자 대조로 비우면 `교회 시설관리`를 `시설·관리`로 줄인
    #    맞는 답이 지워지고 fallback `기타`로 떨어진다(실측 CSU/1117858).
    # ⚠️ `work_days`도 조립 칸이다 — 준전임과 파트가 근무일을 따로 적는 공고가 **54건**이고
    #    한 문자열에 담으려면 이을 수밖에 없다. 프롬프트가 이으라고 시켜놓고 벌하지 않는다.
    for name in (
        "role",
        "work_days",
        "headcount",
        "housing_note",
        "pay_note",
        "benefit_note",
        "contact_post",
    ):
        checker.tally(name, getattr(extraction, name))
    for name in ("requirements", "preferred", "required_docs", "optional_docs", "process_steps"):
        checker.tally_items(name, getattr(extraction, name))
    verified = replace(
        extraction,
        start_timing=checker.text("start_timing", extraction.start_timing),
        church_name=checker.text("church_name", extraction.church_name),
        raw_denomination=checker.text("raw_denomination", extraction.raw_denomination),
        contact_email=checker.punctuated("contact_email", extraction.contact_email),
        contact_tel=checker.digits("contact_tel", extraction.contact_tel),
        contact_link=checker.punctuated("contact_link", extraction.contact_link),
        position=checker.choices("position", extraction.position, known.position_items),
        department=checker.choice("department", extraction.department, known.department),
        employment_type=checker.choice(
            "employment_type", extraction.employment_type, known.employment_type
        ),
        qualification=checker.choice(
            "qualification", extraction.qualification, known.qualification
        ),
        pay_min=extraction.pay_min if pay_grounded else None,
        pay_max=extraction.pay_max if pay_grounded else None,
        pay_period=extraction.pay_period if pay_grounded else None,
        region=extraction.region if place_grounded else None,
        city=extraction.city if place_grounded else None,
        deadline=extraction.deadline if date_grounded else None,
    )
    return verified, checker.report()


def supports(value: StrEnum, evidence: str, *, title: str = "") -> bool:
    """근거가 그 값을 뒷받침하나. 뒷받침할 낱말이 정해지지 않은 값(`ETC`)은 통과.

    ⚠️ 직분은 낱말만으로 부족하다 — `담임목사: 박은제`(연락처)와 `담임목사 청빙`(모집)이
    둘 다 `담임`을 담는다. 그래서 `_NEEDS_RECRUITING_WORD`인 값은 **모집한다는 말**까지 본다.

    ⚠️ 모집 목록은 그 말을 안 달고 온다 — `담임목사 1명`이 그렇다(실측 NAZARENE/123).
    그때는 **제목이 대신 말해준다**: 괄호를 걷어낸 제목에 담임계열과 모집어가 함께 있으면
    그 공고는 담임을 뽑는 공고다. 괄호를 걷는 이유는 거기가 사람 이름 자리이기 때문이다 —
    `현대교회(담임목사 박건욱)에서 사무간사님을 모십니다`는 담임을 뽑지 않는다(실측 7건).
    """
    entry = _SUPPORTING_WORDS.get(value)
    if entry is None:
        return True
    required, excluded = entry
    squeezed = _squeeze(evidence).lower()
    if any(word in squeezed for word in excluded):
        return False
    if not any(word in squeezed for word in required):
        return False
    if value in _NEEDS_RECRUITING_WORD:
        return _recruits(squeezed) or _title_recruits(title, required)
    return True


def _recruits(text: str) -> bool:
    return any(word in text for word in _RECRUITING_WORDS)


def _title_recruits(title: str, required: tuple[str, ...]) -> bool:
    """제목이 "이 직분을 뽑는다"고 말하나. **괄호 안은 보지 않는다**(사람 이름 자리)."""
    bare = _squeeze(_PARENTHESES.sub("", title))
    return any(word in bare for word in required) and _recruits(bare)


@dataclass
class _Checker:
    """값을 하나씩 보며 살릴지 비울지 정하고, 그 사이 집계를 모은다.

    ⚠️ `may_scrub`이 거짓이면 **아무것도 비우지 않고 세기만 한다** — 그림·PDF 공고에서는
    본문에 없는 것이 정상이다.
    """

    haystack: str
    #: 게시판 제목. 모집 여부를 근거만으로 못 가릴 때 본다(`supports`).
    title: str
    #: 칸별 원문. 숫자 대조가 칸을 넘나들지 않게 나눠 둔다(`digits`).
    parts: tuple[str, ...]
    may_scrub: bool
    _scrubbed: list[str] = field(default_factory=list)
    _dropped: list[Dropped] = field(default_factory=list)
    _unverifiable: int = 0
    _unchecked: int = 0
    _unchecked_fields: dict[str, int] = field(default_factory=dict)

    def text(self, name: str, value: str | None) -> str | None:
        if value is None or self._found(value):
            return value
        return None if self._note((Dropped(name, value),)) else value

    def digits(self, name: str, value: str | None) -> str | None:
        """전화번호 — **숫자만** 견준다. 프롬프트가 `010-2720-구육구이`를 되돌리라 시켜서
        글자로는 원문과 다르지만, 되돌린 뒤의 숫자열은 원문 숫자열 안에 있어야 한다."""
        if value is None:
            return None
        # ⚠️ 빈 문자열은 어디에나 있다 — `없음`·`교회로 문의`가 전화번호로 통과하던 구멍.
        # ⚠️ **번호 덩어리마다 따로 본다.** 한 칸에 번호가 둘인 공고가 흔한데
        #    (`032-515-5004(사무실) / 010-7669-4035 (담당자)` — 실측 5건 중 4건) 통째로 이으면
        #    그 숫자열은 어느 원문에도 없다. 프롬프트가 둘 다 담으라고 시켜놓고 벌하지 않는다.
        wanted = _phone_numbers(value)
        # ⚠️ 칸마다 따로 본다 — 숫자만 남기면 칸 사이 구분자도 지워져 본문 끝 `…1151`과
        #    다음 칸 앞 `4…`가 이어져 `11514`가 "있다"가 된다.
        haystacks = [_digits_of_source(part) for part in self.parts]
        if wanted and all(any(number in hay for hay in haystacks) for number in wanted):
            return value
        return None if self._note((Dropped(name, value),)) else value

    def punctuated(self, name: str, value: str | None) -> str | None:
        """이메일·링크 — **글자와 숫자만** 견준다(`https://`·`.`·`,`를 지운다).

        ⚠️ 실측: 원문이 `홈페이지:www,guryejungangchurch.com`(쉼표 오타)인데 모델이 점으로
        고쳤다. 그대로 견주면 **고친 답이 버려지고 깨진 URL을 그대로 옮긴 답이 살아남는다** —
        검산이 더 나쁜 답을 고르게 된다. 이메일도 같다 — 원문 `tmlee153@naver.,com`(쉼표
        오타)을 모델이 고쳤고, 글자 그대로 견주면 **고친 답이 버려진다**(실측 SJS/50075).
        """
        if value is None or _alnum(_SCHEME.sub("", value)) in _alnum(self.haystack):
            return value
        return None if self._note((Dropped(name, value),)) else value

    def tally(self, name: str, value: str | None) -> None:
        """비우지 않고 **세기만** 한다.

        프롬프트가 조립을 시킨 칸이라 어긋나도 모델 잘못이 아니다.
        """
        if value is not None and not self._found(value):
            self._unchecked += 1
            self._unchecked_fields[name] = self._unchecked_fields.get(name, 0) + 1

    def tally_items(self, name: str, values: tuple[str, ...]) -> None:
        for value in values:
            self.tally(name, value)

    def choice[E: StrEnum](self, name: str, chosen: E | None, evidence: str | None) -> E | None:
        if chosen is None:
            return None
        if self._grounded(evidence) and supports(chosen, evidence or "", title=self.title):
            return chosen
        return None if self._note((Dropped(name, chosen.value, evidence),)) else chosen

    def choices[E: StrEnum](
        self, name: str, chosen: tuple[E, ...], evidence: Sequence[str]
    ) -> tuple[E, ...]:
        """값마다 **자기 근거**로 검산한다.

        ⚠️ 근거 하나로 여러 값을 보던 때는 맞는 직분이 통째로 지워졌다(실측 CSU 10건 중 4건 ·
        `부목사·전도사·강도사·기타` → `기타`). 한 조각이 네 직분을 동시에 뒷받침할 수 없다.
        """
        kept: list[E] = []
        dropped: list[Dropped] = []
        for index, value in enumerate(chosen):
            found = evidence[index] if index < len(evidence) else None
            if self._grounded(found) and supports(value, found or "", title=self.title):
                kept.append(value)
            else:
                dropped.append(Dropped(name, value.value, found))
        if not dropped:
            return chosen
        return tuple(kept) if self._note(tuple(dropped)) else chosen

    def grounds(self, name: str, evidence: str | None) -> bool:
        """근거가 원문에 있나. 없으면 세어 두고 거짓을 준다(부르는 쪽이 파생값을 비운다).

        ⚠️ 근거가 `None`인 것은 **탓하지 않는다** — 모델이 값 자체를 안 낸 경우이고, 그때는
        파생값도 이미 비어 있다.
        """
        if evidence is None:
            return True
        if self._found(evidence):
            return True
        return not self._note((Dropped(name, evidence),))

    def report(self) -> VerifyReport:
        return VerifyReport(
            scrubbed=tuple(self._scrubbed),
            dropped=tuple(self._dropped),
            unverifiable=self._unverifiable,
            unchecked=self._unchecked,
            unchecked_fields=dict(self._unchecked_fields),
        )

    def _grounded(self, evidence: str | None) -> bool:
        """근거를 댔고 그 글자가 원문에 있나. 둘 다 "가리킬 데가 없다"로 같이 다룬다."""
        return bool(evidence) and self._found(evidence or "")

    def _found(self, value: str) -> bool:
        squeezed = _squeeze(value)
        return bool(squeezed) and squeezed in self.haystack

    def _note(self, dropped: Sequence[Dropped]) -> bool:
        """비울 수 있으면 **버린 값까지** 적고 True. 아니면 개수만 세고 False(값을 그대로 둔다).

        ⚠️ 버린 값을 남기는 이유: 칸 이름만 있으면 "모델이 뭐라고 답했길래 지웠나"를 알 수
        없어 **과검을 검수할 방법이 없다**(실측 2026-08-14 · 전화번호·교단이 왜 지워졌는지
        끝내 못 밝혔다). 저장되는 곳은 `--out` 미리보기 파일뿐이고 `review_data`에는 안 간다.
        """
        if not self.may_scrub:
            self._unverifiable += len(dropped)
            return False
        self._scrubbed.extend(item.field for item in dropped)
        self._dropped.extend(dropped)
        return True


def _haystack(record: SourceData) -> str:
    """`_source_parts`를 한 덩어리로 이은 것. 글자 대조가 쓴다.

    ⚠️ 칸 사이를 **값에 나올 수 없는 글자**로 막는다. 공백으로 잇고 공백을 지우면 칸이 붙어,
    본문 끝 `…1685`와 제목 앞 `성원교회`가 이어져 `1685성원교회`가 "있다"가 된다(실측).
    """
    return _BOUNDARY.join(_source_parts(record))


def _source_parts(record: SourceData) -> tuple[str, ...]:
    """대조할 원문을 **칸별로**. 공백을 지워 각각 한 덩어리로 만든다.

    ⚠️ 게시판 필드도 넣는다: `CSU`는 교회명·교단·사례비가 본문이 아니라 거기 있다(730건).
    ⚠️ 게시판 필드는 프롬프트가 걸러낸 것(`아래참조`류)까지 전부 넣는다 — 여기는 "모델이
    지어냈나"만 보는 자리이고, 무엇을 보여줄지는 프롬프트가 이미 정했다.

    ⚠️ **프롬프트가 붙이는 한글 라벨까지 넣는다.** 모델은 자기가 본 줄을 그대로 오려내므로
    근거가 `모집부서: 장년 교구`로 온다 — 원본 값(`장년 교구`)만 보면 "원문에 없다"가 되어
    맞는 답이 지워진다(실측 2026-08-14 CSU/1117877 department·qualification).
    """
    parts = [
        record.raw_text,
        record.title,
        *(str(value) for value in record.raw_meta.values() if value is not None),
        *(f"{label}: {value}" for label, value in meta_lines(record.raw_meta)),
        *(item.name for item in record.attachments),
    ]
    return tuple(_squeeze(part) for part in parts)


def _squeeze(text: str) -> str:
    return _WHITESPACE.sub("", text)


def _alnum(text: str) -> str:
    """구두점을 지운 꼴. `www,x.com`과 `www.x.com`이 같아진다(원문 쉼표 오타 · 실측 1건)."""
    return _NOT_ALNUM.sub("", text).lower()


def _phone_numbers(value: str) -> tuple[str, ...]:
    """답에 담긴 전화번호를 하나씩. 쉼표·괄호·한글이 번호를 가른다."""
    return tuple(_DIGITS.sub("", chunk) for chunk in _PHONE_CHUNK.findall(_ascii_forms(value)))


def _digits(text: str) -> str:
    """모델 답에서 뽑는 숫자열. **한글은 되돌리지 않는다**(위 `_KOREAN_DIGITS` 경고)."""
    return _DIGITS.sub("", _ascii_forms(text))


def _digits_of_source(text: str) -> str:
    """원문에서 뽑는 숫자열. 한글로 가린 숫자를 되돌린 **뒤** 숫자만 남긴다."""
    restored = "".join(_KOREAN_DIGITS.get(char, char) for char in _ascii_forms(text))
    return _DIGITS.sub("", restored)


def _ascii_forms(text: str) -> str:
    r"""전각 숫자를 반각으로. ⚠️ `\d`는 전각도 숫자로 세지만 **글자가 달라** 견주면 어긋난다 —
    실측 PUTS/157669는 전화번호 한 자리가 전각(U+FF16)이라 맞는 번호가 통째로 지워졌다."""
    return unicodedata.normalize("NFKC", text)
