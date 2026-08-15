"""공고가 적어 놓은 교단 표기를 **교단 key로 확정**한다(SPEC §5.3 ① · CONTRACT §2c).

`normalize.py`와 같은 자리다 — **모델을 부르지 않는다.** 같은 글자에서 실행마다 같은 값이
나오고 유료 호출 없이 테스트된다. 다만 순수한 글자 변환은 아니다: 그 표기가 원문의
**어디에** 적혀 있었나까지 본다(아래 두 경고). 그래서 `record`를 함께 받는다.

무엇을 확정하나 — **명시된 것만**이다:

    "예장 합동"          → HAPDONG · stated
    "기독교대한성결교회"  → SEONGGYUL · stated
    "본문 참조"           → UNKNOWN · unknown   (교단 표기가 아니다)
    None                 → UNKNOWN · unknown

⚠️ **못 알아보면 지어내지 않는다.** 표에 없는 글자는 `UNKNOWN`이고, `ETC`로 밀어 넣지 않는다 —
`ETC`는 "그 외 교단"이라는 **주장**이라 `아래 참조` 같은 값이 그리로 가면 거짓이 된다.
같은 이유로 `초교파`도 표에 없다: 소속이 없다고 **적힌** 값이라 SPEC §5.3의 `NULL = 미상
또는 무소속`이 답이다(실측 4건). `독립`은 CONTRACT §2c가 `ETC`로 못박아 그것을 따른다.
`UNKNOWN`은 그대로 두는 것이 정책이다(승격 전 해소 규칙은 2026-08-06에 철회됐다).

⚠️ **원문에 없는 표기로는 확정하지 않는다.** 모델이 지어낸 값과 그림에만 있어 확인할 수 없는
값을 코드는 구분하지 못한다 — 둘 다 근거가 없으므로 확정하지 않는다.

실측(2026-08-15): 게시판 교단 칸 730건 중 **0건**이 여기 걸린다(그 칸 자체가 원문이다).
본문이 그림뿐인 공고 177건 중 교단 표기가 게시판 메타에 있는 68건은 그대로 확정되고,
**나머지 109건(`PCKWORLD` 60 전부 · `CALVIN` 25 …)은 확정되지 않는다** — 포스터를 읽은
값이 맞는지 코드가 확인할 방법이 없기 때문이다. 그 109건은 `raw_denomination`(원표기)만
남기고 운영자에게 간다. **빈 칸이 틀린 교단보다 낫다**는 것이 이 리포의 기준이다.

⚠️ **자격 요건에 적힌 교단 이름은 그 교회의 교단이 아니다.** `② 기타 교단 정규 신대원
졸업자(장신, 침신, 백석, 고신 등)`처럼 **받아주는 신대원 목록**이 흔한데(실측 298건 ·
`대신` 50 · `고신` 46 · `감리` 43), 모델이 그 줄을 집으면 합동 교회가 고신이 된다.
그래서 표기가 자격 줄에**만** 있으면 확정하지 않는다.

⚠️ **교단 칸에 온 것이 문장이면 확정하지 않는다.** 게시판 교단 칸은 교회가 직접 채우는
자유 입력 칸이라 설명을 적는 교회가 있다(실측 2건). 문장 안의 교단 이름은 그 교회의
소속이 아닐 수 있다 — `미주한인예수교장로회/ 합동교단과 교류 교단입니다.`의 교단은 KAPC다.

⚠️ **이 세 규칙 밖으로 나가지 않는다.** `교류`·`협력` 같은 낱말을 표로 만들면 `연합`·`자매`가
뒤따라 붙어 끝나지 않고, 그러면 왜 이 공고만 교단이 비었는지 아무도 설명하지 못한다.
표에 낱말을 더할 때는 **실측 근거를 함께 적는다**(아래 `_QUALIFICATION_WORDS` 주석).

⚠️ **명부 대조(`registry`)와 AI 추정(`ai_guess`)은 여기 없다.** 명부는 아직 없고, 추정은
프롬프트가 시키지 않는다 — 둘 다 붙으면 근거 값만 달라지고 이 함수의 자리는 그대로다.
"""

from __future__ import annotations

import re
from typing import Final

from minjob_ingest.domain import Denomination, DenominationSource
from minjob_ingest.models import SourceData
from minjob_ingest.pipeline.normalize import squeeze

#: 표기 → 교단 key. **순서에 기대지 않는다** — 걸린 낱말이 다른 낱말 안에 들어 있으면 버린다
#: (`합동신학`이 걸리면 그 안의 `합동`은 안 센다 · CONTRACT §2c ⚠️). 순서를 규칙으로 두면
#: 줄 하나를 옮겨 적는 순간 합신 교회가 합동이 되는데, 그건 리뷰로 못 막는다.
#:
#: ⚠️ CONTRACT §2c는 충돌을 "긴 표기 우선"으로 적지만 여기서는 **포함 관계**로 가른다.
#: 대부분 같은 답이고, 갈리는 자리에서는 이쪽이 안전하다: `예장합동 … 합동신학대학원
#: 졸업자 우대`를 길이로 고르면 지원자의 학교(합신)가 그 교회의 교단이 된다.
#:
#: ⚠️ 실측 730건이 78가지 표기로 갈린다(`대한예수교 장로회 합동`·`예장합동`·`장로교 합동`…).
#: 공백을 지우고 부분일치로 본다 — 표기를 다 적으려 하면 다음 게시판에서 또 샌다.
_ALIASES: Final[tuple[tuple[str, Denomination], ...]] = (
    # ── 다른 교단 이름을 품는 것들. 짧은 쪽보다 먼저 걸려야 하는 게 아니라,
    #    **함께 걸린 뒤 짧은 쪽이 버려진다**(위 포함 관계) ──
    ("합동신학", Denomination.HAPSIN),
    ("합동정통", Denomination.BAEKSEOK),
    ("합동보수", Denomination.ETC),
    ("백석대신", Denomination.BAEKSEOK),
    ("예장백석", Denomination.BAEKSEOK),
    ("백석", Denomination.BAEKSEOK),
    # ── 9대형 ──
    ("예장합동", Denomination.HAPDONG),
    ("합동", Denomination.HAPDONG),
    ("예장통합", Denomination.TONGHAP),
    ("통합", Denomination.TONGHAP),
    ("기독교대한감리회", Denomination.GAMLI),
    ("예수교대한감리회", Denomination.GAMLI),
    ("기감", Denomination.GAMLI),
    ("감리", Denomination.GAMLI),
    ("여의도순복음", Denomination.SUNBOK),
    ("하나님의성회", Denomination.SUNBOK),
    ("순복음", Denomination.SUNBOK),
    ("기하성", Denomination.SUNBOK),
    ("기독교한국침례회", Denomination.BAPTIST),
    ("침례", Denomination.BAPTIST),
    ("기침", Denomination.BAPTIST),
    ("기독교대한성결교회", Denomination.SEONGGYUL),
    ("예수교대한성결교회", Denomination.SEONGGYUL),
    ("성결", Denomination.SEONGGYUL),
    ("기성", Denomination.SEONGGYUL),
    ("예성", Denomination.SEONGGYUL),
    ("고려신학", Denomination.GOSIN),
    ("예장고신", Denomination.GOSIN),
    ("고신", Denomination.GOSIN),
    ("예장합신", Denomination.HAPSIN),
    ("합신", Denomination.HAPSIN),
    # ── ETC: 9대형 밖이라고 **적힌** 것만. 못 알아본 글자는 여기 오지 않는다 ──
    ("한국기독교장로회", Denomination.ETC),
    ("기장", Denomination.ETC),
    # ⚠️ `대한기독교나사렛성결회`는 `나사렛`(ETC)과 `성결`(SEONGGYUL) 둘 다 걸려 갈린다.
    #    통째로 적어야 포함 관계가 나머지를 버린다 — `NAZARENE` 3건 전부가 이 표기다.
    ("나사렛성결", Denomination.ETC),
    ("나사렛", Denomination.ETC),
    ("루터", Denomination.ETC),
    ("그리스도의교회", Denomination.ETC),
    ("독립", Denomination.ETC),
    # ⚠️ 독립교회들의 연합체. 풀어 쓴 `한국독립교회선교단체연합회`는 `독립`으로 이미 걸리는데
    #    약칭만 적은 공고가 답이 갈렸다 — **같은 단체가 표기에 따라 다른 답을 내면 안 된다**.
    #    본문 등장 36회(`KAICAM` 27 · `카이캄` 9) 전부 소속을 말하는 자리다(실측).
    ("카이캄", Denomination.ETC),
    # ⚠️ 예장 브니엘총회(군소 예장 → ETC · CONTRACT §2c). 본문 12회 전부 교단 표기다.
    #    ⚠️ 같은 군소 예장이어도 **`계신`(예장 계신)은 넣지 않는다** — `계시는`의 준말이
    #    본문에 27번 나온다(`사명이 계신 분`·`쉬고 계신 사역자`). 표기 3건을 얻자고
    #    존댓말을 교단으로 읽을 수는 없다. 그 3건은 `UNKNOWN`으로 두고 운영자가 정한다.
    ("브니엘", Denomination.ETC),
    # ⚠️ `예장개혁`은 통째로 적는다 — `개혁`만 두면 본문에 54번 나오는 `개혁주의 신학`이
    #    교단으로 읽힌다(실측). `대신`은 교단 칸에 그 표기 그대로 온다(실측 1건).
    ("예장개혁", Denomination.ETC),
    ("대신", Denomination.ETC),
    ("호헌", Denomination.ETC),
)

#: 영문 표기. ⚠️ **낱말 경계를 본다** — 부분일치로 두면 게시판 키 `PCKWORLD`가 `PCK`(통합)로
#: 읽힌다. 한글은 조사가 붙어 경계를 못 잡으므로 위 표에서 부분일치로 둔다.
_LATIN_ALIASES: Final[tuple[tuple[str, Denomination], ...]] = (
    ("gapck", Denomination.HAPDONG),
    ("pck", Denomination.TONGHAP),
    ("prok", Denomination.ETC),
    ("agk", Denomination.SUNBOK),
    ("kaicam", Denomination.ETC),
)

#: 두 표를 하나로 합친 **찾는 방법**. 한글은 부분일치, 영문은 낱말 경계 — 표기(근거로 남길
#: 글자)와 패턴(찾을 방법)을 함께 들고 다닌다.
#:
#: ⚠️ **값을 찾을 때와 원문에서 근거를 찾을 때가 같은 패턴이어야 한다.** 영문 경계를 값에만
#: 쓰고 원문 대조를 부분일치로 두면, 모델이 답한 `PCK`(통합)를 원문의 **`GAPCK`(합동)** 이
#: 뒷받침한다 — 다른 교단으로 `stated` 확정된다. 원장에 `pckyesan`·`pckworld`·`hanyoungpck`
#: 이 실제로 있다.
_MATCHERS: Final[tuple[tuple[re.Pattern[str], str, Denomination], ...]] = (
    *((re.compile(re.escape(alias)), alias, key) for alias, key in _ALIASES),
    *((re.compile(rf"(?<![a-z]){alias}(?![a-z])"), alias, key) for alias, key in _LATIN_ALIASES),
)

#: 이 낱말이 있는 줄은 **자격 요건**이다 — 거기 적힌 교단 이름은 받아주는 신대원 목록이지
#: 그 교회의 교단이 아니다. `본 교단(예장합동) 신대원 졸업자`처럼 진짜 교단이 자격 줄에만
#: 있는 공고도 함께 놓치지만, 그때 답은 빈 칸이지 틀린 교단이 아니다.
#:
#: ⚠️ **낱말마다 실측 근거가 있다**(2026-08-15 · 원장 3,188건 · 자격 줄에만 교단 표기가
#: 있는 298건 기준). 두 가지를 함께 본다 — 그 낱말만이 막고 있는 수(빼면 되살아나는 수)와
#: 그 낱말 하나로 막히는 수:
#:
#:     졸업 9/248 · 재학 8/59 · 신대원 7/91 · 자격 7/31 · 학력 5/8 · 신학대학원 3/148
#:     인준 0/87   ← 겹치지만 자격 줄 87개에 실제로 있다. 겹친다고 빼지 않는다
#:     출신 0/0    ← 둘 다 0이라 뺐다. 아무 줄도 막고 있지 않았다
#:
#: 짐작으로 낱말을 늘리면 표가 끝없이 자라고 **어느 줄이 왜 막혔는지 아무도 설명하지 못한다**.
_QUALIFICATION_WORDS: Final = (
    "신대원",
    "신학대학원",
    "졸업",
    "재학",
    "자격",
    "학력",
    "인준",
)

#: 교단 칸에 온 값이 **이름이 아니라 문장**인가. 게시판 교단 칸은 교회가 직접 쓰는 자유 입력
#: 칸이라 `미주한인예수교장로회/ 합동교단과 교류 교단입니다.`처럼 설명을 적는 교회가 있다
#: (실측 2건 · 이 교회는 KAPC다). 문장 안에 있는 `합동`은 그 교회의 소속이 아니다.
#:
#: ⚠️ **낱말이 아니라 모양을 본다.** `교류`·`협력`을 표로 두면 `연합`·`자매`·`우호`가 뒤따라
#: 붙어 끝나지 않고, 실제로 `연합`은 `한국독립교회선교연합회`를 제 이름 때문에 막았다.
#: 종결어미와 마침표는 78가지 표기 중 **딱 이 두 값과 `..`(원래 UNKNOWN)** 에만 있다.
_SENTENCE: Final = re.compile(r"니다|\.")

#: 근거가 없을 때의 답. 세 값이 한 몸이라 따로 만들지 않는다.
_UNKNOWN: Final = (Denomination.UNKNOWN, DenominationSource.UNKNOWN, None)


def confirm(
    raw_denomination: str | None, record: SourceData
) -> tuple[Denomination, DenominationSource, str | None]:
    """(교단, 근거, 근거로 쓴 표기). 못 알아보면 `(UNKNOWN, unknown, None)`.

    돌려주는 세 번째 값이 `denomination_evidence`다 — **표에서 무엇에 걸렸나**를 남긴다.
    없으면 잘못 확정된 행을 봤을 때 표의 어느 줄이 문제인지 알 수 없다.

    `record`는 그 표기가 **어디에 적혀 있었나**를 보는 데 쓴다(모듈 설명의 두 경고).
    """
    if raw_denomination is None:
        return _UNKNOWN
    text = squeeze(raw_denomination).lower()
    if not text or _SENTENCE.search(text):
        return _UNKNOWN
    matched = [(pattern, alias, key) for pattern, alias, key in _MATCHERS if pattern.search(text)]
    # 다른 낱말에 **들어 있는** 것은 버린다 — `백석대신`이 걸렸으면 그 안의 `대신`은 근거가 아니다.
    standalone = [
        (pattern, alias, key)
        for pattern, alias, key in matched
        if not any(alias != longer and alias in longer for _, longer, _ in matched)
    ]
    if len({key for _, _, key in standalone}) != 1:
        # ⚠️ **갈리면 확정하지 않는다.** `대한예수교장로회 독립교회 (고신에서 독립함)`은
        #    고신에서 나온 독립교회인데 `고신`만 보면 고신으로 저장된다(실측 1건).
        #    무엇인지 모르면 `UNKNOWN`이 정답이다 — 운영자가 검수에서 정한다.
        return _UNKNOWN
    for pattern, alias, key in standalone:
        # 남은 표기는 전부 같은 key다 — 원문이 뒷받침하는 것 하나를 근거로 삼는다.
        if _confirms_this_church(pattern, record):
            return key, DenominationSource.STATED, alias
    return _UNKNOWN


def _confirms_this_church(pattern: re.Pattern[str], record: SourceData) -> bool:
    """그 표기가 원문에서 **이 교회의 소속**을 말하고 있나.

    두 가지를 함께 막는다 — 원문에 아예 없는 표기(근거 없음)와, 자격 요건 줄에만 있는
    표기(받아주는 신대원 목록). 자세한 이유와 실측은 모듈 설명에 있다.

    ⚠️ 값을 찾을 때 쓴 **그 패턴**으로 찾는다(위 `_MATCHERS` 경고).
    """
    where = [line for line in map(squeeze, _source_lines(record)) if pattern.search(line.lower())]
    return bool(where) and not all(_is_a_qualification_line(line) for line in where)


def _is_a_qualification_line(line: str) -> bool:
    """그 줄이 **지원 자격**을 적고 있나 — 거기 적힌 교단은 받아주는 신대원 목록이다."""
    return any(word in line for word in _QUALIFICATION_WORDS)


def _source_lines(record: SourceData) -> tuple[str, ...]:
    """줄 단위 원문 — 게시판 필드도 한 줄로 센다(`CSU`는 교단이 거기 있다)."""
    return (
        *(str(value) for value in record.raw_meta.values() if value is not None),
        *(line for line in record.raw_text.splitlines() if line.strip()),
    )
