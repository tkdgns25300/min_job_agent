"""구조화 AI에게 **무엇을 묻고 답을 어떻게 읽는가**(SPEC §5).

전송(인증·재시도·타임아웃)은 `lib/gemini.py`가, 판정·저장은 `pipeline/structure.py`가 맡는다.
여기는 그 사이 — 프롬프트·출력 스키마·응답 검증만 담는다.

⚠️ **1단계는 4필드뿐이다**(ROADMAP 1-2 · 경로를 먼저 뚫는다). 33필드·이미지·enum 정규화는
2단계에서 이 파일이 자란다. 지금 다 설계하지 않는 이유: Gemini가 한국 교회 공고에 어떻게
반응하는지 아직 모른다.

⚠️ **모델 응답을 신뢰하지 않는다.** 스키마를 강제해도 값은 경계에서 다시 검증한다
(CLAUDE.md "경계에서 검증"). SDK의 `response.parsed`를 쓰지 않는 이유도 같다.

⚠️ **어투를 프롬프트가 정한다.** 안 정하면 공고마다 개조식·교회 1인칭·불릿·기도문이 섞여
나오고, 그건 채용 사이트에서 그대로 보인다. 2단계에서 33필드가 사실을 각자 칸으로 가져가면
`description`에는 **다른 칸에 안 들어가는 것**(교회 소개·사역 방향)만 남는다 — 그때 이 절을
"무엇을 담는가"까지 좁힌다. 1단계는 아직 4필드뿐이라 전반 요약이다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from google.genai import types

from minjob_ingest.domain import IsChurchRecruitment
from minjob_ingest.lib.gemini import GeminiClient
from minjob_ingest.models import MAX_DESCRIPTION_CHARS, JsonValue, SourceData

#: 본문이 비었을 때 프롬프트에 넣는 자리표시자. 빈 칸을 그냥 두면 모델이 앞 문단을 본문으로
#: 오해한다. ⚠️ 본문이 없는 것은 실패가 아니다 — 포스터 한 장이거나 첨부에 내용이 있다.
_NO_BODY: Final = "(본문 없음)"

#: `raw_meta`에서 프롬프트에 넣지 않는 키 — **게시판 UI 값과 중복뿐**이다.
#: 나머지는 전부 넣는다: `CSU`는 교단(`order_name`)·교회명·지역·사례가 **본문이 아니라 여기**
#: 있고 포스터 OCR보다 정확하다(SPEC §5.3).
#:
#: ⚠️ **사람 이름(`author`·`senior_pastor`)도 보낸다**(운영자 결정 2026-08-10). 가드레일 #4는
#: "제3자 개인정보를 **추출**하지 않는다"이지 모델에 맥락으로 주지 말라는 뜻이 아니다 —
#: 맥락이 적을수록 오추출이 늘어난다. 뽑힌 값이 저장·공개되는 것은 별개로 막는다
#: (1단계 출력 4필드에 사람 이름 칸이 없고, 프롬프트도 넣지 말라고 지시한다).
_META_NOISE_KEYS: Final = frozenset(
    {
        "views",
        "display_no",
        "has_attachment",
        "thumbnail",
        # 제목은 프롬프트가 이미 `제목:`으로 보낸다 — 같은 문장을 두 번 넣지 않는다.
        "list_title",
    }
)

#: 한 줄이어야 하는 필드(교회명·제목)의 상한. ⚠️ `description`만 막으면 원문이 `title`로
#: 흘러 공개된다(가드레일 #3 원문 재게시 금지) — "한 줄로 쓰라"는 지시는 지켜지지 않을 수 있다.
MAX_SHORT_TEXT_CHARS: Final = 200

#: 응답 스키마. `required`에 다 넣고 `nullable`을 허용한다 — 키를 빼는 것과 "값이 없다"를
#: 구분하기 위해서다(키가 빠지면 모델이 스키마를 안 따른 것이므로 실패로 본다).
RESPONSE_SCHEMA: Final = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "church_name": types.Schema(
            type=types.Type.STRING, nullable=True, max_length=MAX_SHORT_TEXT_CHARS
        ),
        "title": types.Schema(
            type=types.Type.STRING, nullable=True, max_length=MAX_SHORT_TEXT_CHARS
        ),
        "is_church_recruitment": types.Schema(
            type=types.Type.STRING, enum=[value.value for value in IsChurchRecruitment]
        ),
        "description": types.Schema(
            type=types.Type.STRING, nullable=True, max_length=MAX_DESCRIPTION_CHARS
        ),
    },
    required=["church_name", "title", "is_church_recruitment", "description"],
    property_ordering=["church_name", "title", "is_church_recruitment", "description"],
)

_PROMPT_TEMPLATE: Final = """\
너는 한국 개신교 채용(청빙) 공고에서 **적혀 있는 사실만** 뽑아 JSON으로 옮기는 도구다.
추측하지 않는다. 근거가 없으면 null 을 넣는다.

## 뽑을 것
- church_name: 공고를 낸 **교회 이름**을 공고에 적힌 그대로. 교회가 아니라 기관(선교단체·
  학교·방송사 등)이 낸 공고면 그 기관 이름. 어디가 냈는지 알 수 없으면 null.
- title: 무엇을 뽑는 공고인지 한 줄. 게시판 제목이 그대로 쓸 만하면 그대로 써도 된다.
- is_church_recruitment: 셋 중 하나
    YES        개교회(지역 교회)가 사역자·직원을 뽑는 공고
    NO         기관 채용(선교단체·학교·방송사·병원 등) 또는 **채용 공고가 아닌 글**
    UNCERTAIN  개교회인지 애매하다(군종·원목·기관 겸직 등 경계)
- description: 공고 내용 요약. **원문을 그대로 옮기지 말고 줄여서** 쓴다. {max_description}자 이내.

## description 어투 — 공고마다 달라지면 채용 사이트에서 그대로 티가 난다
- **`~합니다` 평서문**으로 끝낸다. 개조식(`~함`·`~모집`)으로 끝내지 않는다.
- **교회를 3인칭으로** 쓴다 — `저희 교회는`처럼 교회가 말하는 투로 쓰지 않는다.
- **교회의 성격·사역 방향·입지는 남긴다** — 사역자가 지원 여부를 정하는 데 쓰는 정보다
  (`말씀중심·기도중심·선교중심`·`전원 속 도심교회`·`설립 51년`). 교회 표어도 성격이다.
- 정보가 없는 인사·축복·기도 문구만 뺀다(`주님의 은혜가 충만하시길`·`샬롬`).
- 머리기호·번호 목록·이모지·표를 쓰지 않는다. **줄바꿈 없이 이어지는 문장**으로 쓰되
  문장이 끝나면 **한 칸 띄운다**(`~입니다.제출` 처럼 붙여 쓰지 않는다).
- 원문에 없는 말을 보태지 않는다. 줄이는 것이지 쓰는 것이 아니다.

## 규칙
- 아래 `게시판 필드`가 본문과 어긋나면 **게시판 필드를 믿는다**(게시판이 폼으로 받은 값이다).
- 본문이 없어도 제목·게시판 필드·첨부 파일명만으로 판단한다.
- 사람 이름·연락처는 어느 필드에도 넣지 않는다.

## 공고
게시판: {board}
제목: {title}
{meta_block}{attachment_block}본문:
{body}
"""


class ExtractionError(Exception):
    """모델이 돌려준 내용이 계약과 다를 때(JSON 아님·키 누락·타입 어긋남·요약 상한 초과).

    전송 실패(`GeminiError`)와 나눠 둔다 — 원인이 다르고, 상한 초과 리포트에서 운영자가
    "연결이 문제였나, 응답이 문제였나"를 구분할 수 있어야 한다.
    """


@dataclass(frozen=True, slots=True)
class Extraction:
    """모델이 뽑아낸 값. **판정은 하지 않는다** — `ReviewData` 조립은 `structure.py`가 한다."""

    is_church_recruitment: IsChurchRecruitment
    church_name: str | None = None
    title: str | None = None
    description: str | None = None


class GeminiExtractor:
    """`structure.Extractor` 프로토콜의 Gemini 구현.

    파이프라인은 이 클래스가 아니라 프로토콜에 의존한다 — 테스트가 네트워크를 타지 않게
    가짜를 끼우기 위해서다(가드레일 #7·#10).
    """

    def __init__(self, client: GeminiClient) -> None:
        self._client = client

    def extract(self, record: SourceData) -> Extraction:
        return parse_extraction(
            self._client.generate_structured_json(build_prompt(record), schema=RESPONSE_SCHEMA)
        )


def build_prompt(record: SourceData) -> str:
    """공고 1건을 프롬프트로. 게시판별로 나누지 않는다 — 차이는 이미 데이터에 있다."""
    return _PROMPT_TEMPLATE.format(
        max_description=MAX_DESCRIPTION_CHARS,
        board=record.source_key,
        title=record.title,
        meta_block=_meta_block(record.raw_meta),
        attachment_block=_attachment_block(record),
        body=record.raw_text.strip() or _NO_BODY,
    )


def parse_extraction(payload: str) -> Extraction:
    """모델 응답(JSON 텍스트) → `Extraction`. 계약과 다르면 `ExtractionError`."""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as err:
        raise ExtractionError(f"JSON으로 읽을 수 없음: {err}") from err
    if not isinstance(decoded, dict):
        raise ExtractionError(f"객체여야 함 ({type(decoded).__name__})")
    return Extraction(
        is_church_recruitment=_gate1(decoded),
        church_name=_short_text(decoded, "church_name"),
        title=_short_text(decoded, "title"),
        description=_summary(decoded),
    )


def _meta_block(raw_meta: Mapping[str, JsonValue]) -> str:
    lines = [
        f"- {key}: {value}"
        for key, value in raw_meta.items()
        if key not in _META_NOISE_KEYS and _has_content(value)
    ]
    return "" if not lines else "게시판 필드:\n" + "\n".join(lines) + "\n"


def _has_content(value: JsonValue) -> bool:
    return value is not None and str(value).strip() != ""


def _attachment_block(record: SourceData) -> str:
    """첨부 **이름만** 넣는다. 바이트를 읽는 것은 2단계다.

    ⚠️ 이름만으로도 값이 있다 — 본문·이미지가 없고 첨부만 있는 공고가 5건 있고(2026-08-10
    실측), `청빙공고문.hwp` 같은 이름이 그 공고가 무엇인지 말해준다. 빼면 그 5건은 제목만
    보고 판정된다.
    """
    names = [attachment.name for attachment in record.attachments]
    return "" if not names else "첨부: " + ", ".join(names) + "\n"


def _gate1(decoded: Mapping[str, object]) -> IsChurchRecruitment:
    """게이트1(SPEC §5.1). 허용값 밖은 `UNCERTAIN`으로 좁힌다 — 운영자에게 보내는 쪽이 안전하다.

    ⚠️ 다만 **키가 없거나 문자열이 아니면 실패**다. 그건 모델이 스키마를 아예 안 따른 것이라
    나머지 값도 믿을 수 없다 — `UNCERTAIN` 초안을 만들면 잘못된 응답이 검수 큐에 쌓인다.
    """
    value = decoded.get("is_church_recruitment")
    if not isinstance(value, str):
        raise ExtractionError(f"is_church_recruitment가 문자열이 아님 ({value!r})")
    try:
        return IsChurchRecruitment(value.strip().upper())
    except ValueError:
        return IsChurchRecruitment.UNCERTAIN


def _optional_text(decoded: Mapping[str, object], key: str) -> str | None:
    """문자열 또는 null. 빈 문자열·공백은 `None`으로 본다(게시판·모델 둘 다 흔하게 준다)."""
    if key not in decoded:
        raise ExtractionError(f"{key}가 응답에 없음")
    value = decoded[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExtractionError(f"{key}가 문자열이 아님 ({type(value).__name__})")
    return value.strip() or None


def _short_text(decoded: Mapping[str, object], key: str) -> str | None:
    """한 줄이어야 하는 값. 상한을 넘으면 실패로 본다 — `_summary`와 같은 이유다."""
    value = _optional_text(decoded, key)
    if value is not None and len(value) > MAX_SHORT_TEXT_CHARS:
        raise ExtractionError(
            f"{key}가 한 줄 상한을 넘음 ({len(value)}자 > {MAX_SHORT_TEXT_CHARS}자)"
        )
    return value


def _summary(decoded: Mapping[str, object]) -> str | None:
    """요약. 상한을 넘으면 **실패로 본다**(자르지 않는다).

    상한을 넘겼다는 것은 모델이 "줄여 쓰라"를 무시했다는 뜻이고, 그 내용은 원문 복사일
    가능성이 높다(가드레일 #3 원문 재게시 금지). 잘라서 저장하면 잘린 복사본이 남는다.
    """
    value = _optional_text(decoded, "description")
    if value is not None and len(value) > MAX_DESCRIPTION_CHARS:
        raise ExtractionError(f"요약이 상한을 넘음 ({len(value)}자 > {MAX_DESCRIPTION_CHARS}자)")
    return value
