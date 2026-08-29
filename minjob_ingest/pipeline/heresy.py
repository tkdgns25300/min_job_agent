"""이단 참고 목록과 공고를 대조한다 — 걸리면 **만들면서 거절**한다(SPEC §5.4).

`denomination.py`와 같은 자리다 — **모델을 부르지 않는다.** 같은 글자에서 같은 답이 나오고,
유료 호출 없이 테스트된다.

⚠️ **우리가 이단을 판정하지 않는다.** 목록은 "어느 교단이 언제 무엇이라 했다"의 모음이고,
이 모듈은 그 목록에 **이름이 있나**만 본다. 공개 낙인·자동 삭제는 하지 않는다(SPEC §5.4).

무엇을 대조하나 — **교회명과 교단 표기 둘뿐**이다:

    church_name == 목록의 이름/별칭   → 거절
    raw_denomination == 목록의 이름   → 거절 (단체명이 교단 칸에 오는 경우)

⚠️ **정확일치만 쓴다.** 부분일치로 하면 **대부분 이름만 겹친 남의 교회**가 걸린다(SPEC §5.4의
실측 · 재현 방식에 따라 17~50건) — 목록에 `○○교회`가 있으면 `△△○○교회`·`○○제일교회`가
전부 걸리는데 서로 다른 교회다. 대신 놓치는 것이 있다: **앞에 지역을 붙여 쓴 같은 교회는
걸리지 않는다**(실측 1건 — 같은 주소·같은 공고가 표기 하나로 갈렸다).

⚠️ **담임목사는 대조 대상이 아니라 판별 근거다**(2026-08-28). 목록 122항목 중 84개가 **사람
이름**이고 그 별칭이 교회명이다 — 즉 목록은 "누가 담임인 교회인가"로 판별하도록 되어 있는데
코드는 교회명만 봤다. 교회명이 걸린 뒤 공고의 담임(`normalize.senior_pastor_of` · 게시판 폼
또는 본문)을 함께 본다:

    담임 == 목록 항목(또는 별칭)  → 확정 거절
    담임 != 목록 항목             → **통과**(표시를 세우지 않는다)
    담임을 모른다                 → 사람이 본다

⚠️⚠️ **담임이 다르면 통과시킨다**(운영자 결정 2026-08-29 · 앞선 "그래도 사람이 본다"에서
변경). 무고한 같은 이름 교회가 반복해서 검수 큐에 올라오는 비용이 이득보다 컸다 — 실측:
두 달간 이 경로로 자동 거절된 것은 **0건**이고, 걸린 4교회 17건이 전부 이름만 같은 다른
교회였다. **알고 지는 위험**: 목록에 오른 교회가 담임을 갈면 이 규칙을 그대로 지나간다
(ROADMAP 고도화 · 목록에 `region`을 채우면 닫힌다).

⚠️ **통과시켜도 근거는 남긴다** — `heresy_flag`는 세우지 않고(min_job에 배지가 뜨면 안 된다)
`heresy_evidence`에만 적는다. 없으면 "왜 이 교회가 공개됐지?"에 답할 수 없다(SPEC §5.4).

⚠️ **이 파일에 실제 교회·사람 이름을 적지 않는다.** 목록을 커밋하지 않는 이유(실명 자료)가
주석으로 새면 그 조치가 무의미해지고, 무고한 실존 교회가 공개 리포에서 "이단 목록 항목"으로
읽힌다. 아래 `안식교`류는 수십 년간 공개된 **단체명**이라 예외로 둔다.

⚠️ **본문 전체를 뒤지지 않는다.** 목록의 절반이 세 글자 사람 이름이라, 본문에서 찾으면
동명이인이 무더기로 걸린다. 그렇다고 짧은 이름을 표에서 빼지도 않는다 — `안식교`·`구원파`·
`통일교`·`몰몬교`가 전부 세 글자 **단체명**이라 길이로는 갈리지 않는다. 대신 **대조하는
칸을 둘로 제한**해서 막는다: 교회명 칸에 사람 이름이 오는 공고는 없다.

⚠️ **지역이 있으면 지역까지 봐야 거절한다**(2026-08-16). 목록 원본이 `아무개(춘천 ○○교회)`
처럼 지역을 적어 둔 항목이 5개 있는데, 그걸 버리면 **전국의 같은 이름 교회가 전부 걸린다.**

⚠️ **지역을 확인하지 못한 교회명은 거절이 아니라 검수로 보낸다**(2026-08-19 · 실측으로 고쳤다).
목록 122항목 중 **117개(96%)에 지역이 없어** 사실상 이름만으로 거절하고 있었고, 실제로 예장합동
소속 교회가 **이름만 같은 목록 항목** 때문에 자동 거절됐다(옛 원장에서 같은 이름이 21건).
자동 거절은 검수 큐에도 뜨지 않아 **무고한 교회가 아무도 모르게 사라진다.**

    거절까지 하는 경우 = 지역까지 맞았다  또는  동명이 생길 수 없는 이름이다
                         └ 지역 없는 항목의 이름 228개 중 176개가 단체·사람 이름이다
    검수로 보내는 경우 = 지역을 확인 못 한 **개별 교회명** (52개가 이 꼴)

⚠️ **어느 쪽이든 공개되지 않는다.** 검수로 보내는 것은 봐주는 것이 아니라 **사람이 정하게**
하는 것이다(`heresy_flag`와 근거는 그대로 남고 등급이 `medium`이 되어 `PENDING`에 뜬다).
동명이 생기는 것은 교회 이름뿐이다 — `레마선교회`가 교회명 칸에 있으면 그건 그 단체다.

⚠️ **이건 바닥이지 보장이 아니다.** 이름을 바꾸거나 위장 명칭을 쓰면 정확일치로 못 잡는다.
"목록에 있는 이름은 막는다"까지가 이 기능이 하는 일이다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from minjob_ingest.domain import Region
from minjob_ingest.pipeline.normalize import name_key, squeeze

#: 항목 하나가 가질 수 있는 필드. 그 밖이 오면 오타이거나 계약이 바뀐 것이다.
#: ⚠️ `city`·`note`는 **사람이 읽는 메모**다 — 받아만 두고 대조에는 쓰지 않는다(지역까지가
#: 계약이다 · SPEC §5.4). 대조를 좁힌다고 오해하지 않도록 여기 적어 둔다.
_ENTRY_FIELDS: Final = frozenset({"name", "aliases", "ruled_by", "region", "city", "note"})

#: 최상위 필드. 목록 자체의 출처·시점·정책이 함께 온다(감사용).
_TOP_FIELDS: Final = frozenset(
    {"version", "source_url", "captured_on", "policy", "note", "entries"}
)

#: 읽을 수 있는 목록 판. ⚠️ 옛 판(v1)에는 **지역 칸이 없어** 전국의 같은 이름이 걸렸다 —
#: 조용히 읽으면 아무도 그 사실을 모른다. 이웃(`store/json_store.py`)과 같이 **정확히 일치**를
#: 요구한다: 스키마가 바뀌면 코드도 함께 바뀌어야 한다.
_FILE_VERSION: Final = 2

#: **개별 교회를 가리키는 이름**인가. 이 꼴이면 동명이교회가 흔하다(실측: 흔한 교회명 하나가
#: 원장에 21건). 지역 없는 항목의 이름 228개 중 **52개가 이 꼴**이고 나머지 176개는 단체·사람
#: 이름이라 동명이 없다.
#: ⚠️ **`선교회`를 빼야 한다** — `레마선교회`·`다미선교회`처럼 단체명도 글자로는 `교회`로
#: 끝난다(실측 6개). 안 빼면 단체명까지 검수로 새고, 그건 이단을 봐주는 쪽으로 틀리는 것이다.
_CHURCH_NAME: Final = re.compile(r"(?<!선)교회$")

#: 지역을 못 보고 이름만 맞았다는 표시. **운영자가 되짚을 유일한 실마리다.**
#: ⚠️ 못 본 이유가 둘이라 갈라 적는다 — 목록에 지역이 없는 것과 공고에 지역이 없는 것은
#: 손쓸 방법이 다르다(앞은 목록을 채우면 되고, 뒤는 그 공고를 봐야 한다).
#: ⚠️ **주어를 적는다**(2026-08-28 · 검수에서 잡혔다). `지역 확인 불가(목록에 지역 없음)`은
#:    "우리가 지역을 못 긁었다"로 읽혔다 — 지역은 멀쩡히 있었고 없는 것은 **이단 목록 쪽**이었다.
#:    min_job 검수 화면에 그대로 나가는 문장이라 무슨 목록인지까지 적는다.
NO_REGION_NOTE: Final = "같은 교회인지 대조하지 못했다"
_NO_REGION_IN_LIST: Final = f"⚠️ 이단 목록에 지역이 없어 {NO_REGION_NOTE}"
_NO_REGION_IN_POSTING: Final = f"⚠️ 이 공고에 지역이 없어 {NO_REGION_NOTE}"

#: 근거 첫머리에 쓰는 칸 이름. 컬럼명(`church_name`)은 운영자가 읽는 말이 아니다.
_FIELD_LABELS: Final = {"church_name": "교회명", "raw_denomination": "교단 표기"}


class HeresyRefError(Exception):
    """목록이 없거나 계약을 위반했을 때. 유료 호출을 시작하기 전에 즉시 실패시킨다."""


@dataclass(frozen=True, slots=True)
class HeresyEntry:
    """목록 한 줄. `region`이 있으면 그 지역의 그 이름만 가리킨다."""

    name: str
    aliases: tuple[str, ...]
    ruled_by: tuple[str, ...]
    region: Region | None = None


@dataclass(frozen=True, slots=True)
class HeresyRef:
    """대조에 쓸 목록. **이름 → 항목들** 표를 미리 만들어 둔다(공고마다 다시 만들지 않는다).

    ⚠️ **한 이름에 항목이 여럿일 수 있다.** 실측 122항목·252이름 중 이름 13개가 겹치고, 그중
    2항목은 모든 이름이 앞 항목과 겹친다. 앞선 것 하나만 두면 그 2항목이 **영영 대조되지
    않고**, 겹친 항목의 지역이 서로 다를 때 삽입 순서가 판정을 바꾼다.
    """

    entries: tuple[HeresyEntry, ...]
    by_name: Mapping[str, tuple[HeresyEntry, ...]]

    @classmethod
    def of(cls, entries: tuple[HeresyEntry, ...]) -> HeresyRef:
        table: dict[str, list[HeresyEntry]] = {}
        for entry in entries:
            for key in {_key(name) for name in (entry.name, *entry.aliases)}:
                table.setdefault(key, []).append(entry)
        return cls(entries=entries, by_name={k: tuple(v) for k, v in table.items()})


@dataclass(frozen=True, slots=True)
class HeresyMatch:
    """왜 거절했나. `evidence`가 그대로 `review_data.heresy_evidence`가 된다."""

    entry: HeresyEntry
    matched: str
    field: str
    #: 공고의 지역. 목록에 지역이 있어도 이 값이 없으면 **확인한 것이 아니다**.
    posting_region: Region | None = None
    #: 공고의 담임목사(`normalize.senior_pastor_of`). 목록 항목이 **사람 이름**일 때 그 사람인지
    #: 가른다 — 목록 122항목 중 84개가 사람 이름이고 그 별칭이 교회명이다.
    senior_pastor: str | None = None

    @property
    def is_conclusive(self) -> bool:
        """**거절까지 할 수 있나.** 아니면 사람이 본다(공개는 어느 쪽이든 안 된다).

        거절은 셋 중 하나일 때만 한다:
        ① **지역까지 맞았다** — 목록과 공고 양쪽에 지역이 있고 같다(`_applies`가 그런 항목만
           고른다). 목록에만 있고 공고에 없으면 **확인한 것이 아니다.**
        ② **동명이 생길 수 없는 이름이다** — 단체·사람 이름(지역 없는 항목의 이름 228개 중 176개).
        ③ **담임목사가 목록의 그 사람이다**(2026-08-28) — 교회명은 같은데 지역을 못 본 경우에도
           담임이 목록 항목(또는 그 별칭)과 같은 이름이면 그 교회다.

        ⚠️ **③은 거절만 정한다.** 담임이 **다를 때** 통과시키는 것은 별개 판정이고
        `clears_by_senior_pastor`가 답한다 — 거절과 통과가 한 성질에서 나오면, 담임을
        모르는 건(통과도 거절도 아니다)이 어느 한쪽으로 쏠린다.

        ⚠️ 지역을 못 본 교회명으로 거절하면 무고한 교회가 **검수 큐에도 안 뜨고** 사라진다
        (2026-08-19 실측: 예장합동 교회가 이름만 같아서 걸렸다).
        """
        if self.entry.region is not None and self.posting_region is not None:
            return True
        if self.names_the_senior_pastor:
            return True
        return _CHURCH_NAME.search(squeeze(self.matched)) is None

    @property
    def clears_by_senior_pastor(self) -> bool:
        """공고의 담임이 목록 항목과 **다른 사람**이라 통과시킬 수 있나(운영자 결정 2026-08-29).

        ⚠️ **담임을 모르면 통과가 아니다** — 모르는 것과 다른 것은 다르다.
        """
        return self.senior_pastor is not None and not self.names_the_senior_pastor

    @property
    def names_the_senior_pastor(self) -> bool:
        """공고의 담임목사가 목록 항목의 이름(또는 별칭) 중 하나인가.

        ⚠️ 별칭까지 본다 — 단체 항목은 별칭에 **대표자 이름**을 갖는다. 별칭의 교회명은 사람
        이름과 같을 수 없어 오탐이 나지 않는다.
        """
        if self.senior_pastor is None:
            return False
        wanted = name_key(self.senior_pastor)
        return any(name_key(name) == wanted for name in (self.entry.name, *self.entry.aliases))

    @property
    def evidence(self) -> str:
        """운영자가 3초에 확인할 수 있는 한 줄 — 무엇이 어디와 같았고, 담임은 누구고, 지역은 봤나.

        ⚠️ 지역을 못 본 건은 그렇다고 **적어 둔다** — 이 표시가 없으면 무고한 교회가
        걸렸을 때 되짚을 방법이 없다.
        ⚠️ **담임목사를 적는다**(2026-08-28). 목록 항목이 사람 이름이라 검수의 갈림길은 "이
        공고의 담임이 그 사람인가"인데, 그 값이 근거에 없어 운영자가 매번 찾아봐야 했다.
        """
        ruled = ",".join(self.entry.ruled_by) or "규정 기관 미상"
        parts = [
            f"{_FIELD_LABELS[self.field]} '{self.matched}'가 "
            f"이단 목록의 「{self.entry.name}」와 일치",
            f"규정: {ruled}",
            self._pastor_note(),
            self._region_note(),
        ]
        return " · ".join(parts)

    def _pastor_note(self) -> str:
        if self.senior_pastor is None:
            return "이 공고 담임목사: 미상"
        if self.names_the_senior_pastor:
            return f"이 공고 담임목사: {self.senior_pastor} (이단 목록 항목과 같은 이름)"
        return (
            f"이 공고 담임목사: {self.senior_pastor} — 이단 목록 항목과 다른 이름이라"
            " 이름만 같은 다른 교회로 보고 통과시켰다"
        )

    def _region_note(self) -> str:
        if self.entry.region is None:
            return _NO_REGION_IN_LIST
        if self.posting_region is None:
            return _NO_REGION_IN_POSTING
        return f"지역 일치: {self.entry.region.value}"


def screen(
    church_name: str | None,
    raw_denomination: str | None,
    region: Region | None,
    ref: HeresyRef,
    *,
    senior_pastor: str | None = None,
) -> HeresyMatch | None:
    """목록에 걸리면 `HeresyMatch`, 아니면 `None`.

    `region`은 공고의 지역이다. 목록 항목에 지역이 있는데 **다르면 다른 교회이므로**
    거절하지 않는다 — 이단을 봐주는 것이 아니라 애초에 그 교회가 아니다.

    `senior_pastor`는 공고의 담임목사(`normalize.senior_pastor_of`)다. **대조 대상이 아니다** —
    걸린 뒤 그 사람인지 가르는 데만 쓴다(`HeresyMatch.is_conclusive` ③). 본문에서 사람 이름을
    찾아 걸면 동명이인이 무더기로 걸린다(모듈 docstring).
    """
    for field, value in (("church_name", church_name), ("raw_denomination", raw_denomination)):
        if value is None:
            continue
        entry = _applies(ref.by_name.get(_key(value), ()), region)
        if entry is None:
            continue
        return HeresyMatch(
            entry=entry,
            matched=value.strip(),
            field=field,
            posting_region=region,
            senior_pastor=senior_pastor,
        )
    return None


def _applies(candidates: tuple[HeresyEntry, ...], region: Region | None) -> HeresyEntry | None:
    """그 이름으로 등재된 항목들 중 **이 공고에 해당하는** 것. 없으면 `None`.

    지역이 맞는 항목을 먼저 고른다 — 지역 없는 항목이 앞에 있다고 해서 "확인 못 했다"고
    적으면 확인할 수 있었던 것을 버리는 셈이다.
    """
    if region is not None:
        exact = next((e for e in candidates if e.region is region), None)
        if exact is not None:
            return exact
    return next((e for e in candidates if e.region is None or region is None), None)


def load_ref(path: Path) -> HeresyRef:
    """목록을 읽어 검증된 `HeresyRef`로만 내보낸다. 위반이 하나라도 있으면 `HeresyRefError`.

    ⚠️ **없으면 조용히 넘어가지 않는다.** 목록 없이 돌면 이단으로 규정된 교회의 공고가
    검수 큐에 그대로 올라가고, 아무도 그 사실을 모른다.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise HeresyRefError(f"이단 참고 목록을 읽을 수 없음: {path} ({err})") from err
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as err:
        raise HeresyRefError(f"이단 참고 목록 JSON 파싱 실패: {path} ({err})") from err

    document = _as_object(raw, str(path))
    unknown = set(document) - _TOP_FIELDS
    if unknown:
        raise HeresyRefError(f"{path}: 알 수 없는 최상위 필드 {sorted(unknown)}")
    _check_version(document.get("version"), str(path))
    rows = document.get("entries")
    if not isinstance(rows, list) or not rows:
        raise HeresyRefError(f"{path}: entries가 비어 있거나 배열이 아님")
    return HeresyRef.of(
        tuple(_parse_entry(_as_object(row, f"entries[{i}]"), i) for i, row in enumerate(rows))
    )


def _check_version(value: object, what: str) -> None:
    """계약이 바뀌면 버전이 오른다 — 옛 파일을 조용히 읽으면 지역 칸 없이 도는 줄 모른다."""
    if not isinstance(value, int) or isinstance(value, bool) or value != _FILE_VERSION:
        raise HeresyRefError(f"{what}: version이 {_FILE_VERSION}이어야 함 ({value!r})")


def _parse_entry(row: dict[str, object], index: int) -> HeresyEntry:
    label = f"entries[{index}]"
    unknown = set(row) - _ENTRY_FIELDS
    if unknown:
        raise HeresyRefError(f"{label}: 알 수 없는 필드 {sorted(unknown)}")
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HeresyRefError(f"{label}.name: 비어있지 않은 문자열이어야 함")
    return HeresyEntry(
        name=name.strip(),
        aliases=_str_tuple(row.get("aliases"), f"{label}.aliases"),
        ruled_by=_str_tuple(row.get("ruled_by"), f"{label}.ruled_by"),
        region=_parse_region(row.get("region"), f"{label}.region"),
    )


def _parse_region(value: object, what: str) -> Region | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HeresyRefError(f"{what}: 문자열 또는 null이어야 함")
    try:
        return Region(value)
    except ValueError as err:
        raise HeresyRefError(f"{what}: 알 수 없는 지역 {value!r}") from err


def _str_tuple(value: object, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise HeresyRefError(f"{what}: JSON 배열이어야 함")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HeresyRefError(f"{what}: 비어있지 않은 문자열만 담을 수 있음 ({item!r})")
        out.append(item.strip())
    return tuple(out)


def _as_object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HeresyRefError(f"{what}: JSON 객체가 아님")
    return {str(key): item for key, item in value.items()}


def _key(name: str) -> str:
    """대조용 열쇠 — `normalize.name_key`(유니코드 정규화 + 괄호 제거 + 공백·기호 제거 + 소문자).

    ⚠️ **괄호·기호까지 뗀다**(2026-08-28 · 운영자 결정). `○○교회(창원)`·`○○·교회`·`○○-교회`가
    글자만 다르게 적힌 같은 이름인데 그대로 견주면 목록을 통과한다. 실측 2,402건에서는 새로
    걸리는 것이 0건이었지만, 같은 이름을 다르게 적은 것뿐이라 넣어도 잃는 것이 없다.
    ⚠️ `dedup`의 자물쇠 키와 **같은 정규식**을 쓴다(`NAME_BRACKETS`·`NAME_NOISE`) — 한쪽만
    고치면 같은 교회가 한 곳에서는 같고 다른 곳에서는 다른 이름이 된다.
    """
    return name_key(name)
