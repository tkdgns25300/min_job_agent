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

⚠️ **이 파일에 실제 교회·사람 이름을 적지 않는다.** 목록을 커밋하지 않는 이유(실명 자료)가
주석으로 새면 그 조치가 무의미해지고, 무고한 실존 교회가 공개 리포에서 "이단 목록 항목"으로
읽힌다. 아래 `안식교`류는 수십 년간 공개된 **단체명**이라 예외로 둔다.

⚠️ **본문 전체를 뒤지지 않는다.** 목록의 절반이 세 글자 사람 이름이라, 본문에서 찾으면
동명이인이 무더기로 걸린다. 그렇다고 짧은 이름을 표에서 빼지도 않는다 — `안식교`·`구원파`·
`통일교`·`몰몬교`가 전부 세 글자 **단체명**이라 길이로는 갈리지 않는다. 대신 **대조하는
칸을 둘로 제한**해서 막는다: 교회명 칸에 사람 이름이 오는 공고는 없다.

⚠️ **지역이 있으면 지역까지 봐야 거절한다**(2026-08-16). 목록 원본이 `아무개(춘천 ○○교회)`
처럼 지역을 적어 둔 항목이 5개 있는데, 그걸 버리면 **전국의 같은 이름 교회가 전부 걸린다.**
지역이 없는 항목(대부분)은 이름만 보고 거절하되, 근거에 "지역 확인 불가"를 남긴다 —
운영자가 되짚을 수 있는 유일한 실마리다.

⚠️ **이건 바닥이지 보장이 아니다.** 이름을 바꾸거나 위장 명칭을 쓰면 정확일치로 못 잡는다.
"목록에 있는 이름은 막는다"까지가 이 기능이 하는 일이다.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from minjob_ingest.domain import Region
from minjob_ingest.pipeline.normalize import squeeze

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

#: 지역을 못 보고 이름만으로 거절했다는 표시. **운영자가 되짚을 유일한 실마리다.**
#: ⚠️ 못 본 이유가 둘이라 갈라 적는다 — 목록에 지역이 없는 것과 공고에 지역이 없는 것은
#: 손쓸 방법이 다르다(앞은 목록을 채우면 되고, 뒤는 그 공고를 봐야 한다).
NO_REGION_NOTE: Final = "지역 확인 불가"
_NO_REGION_IN_LIST: Final = f"⚠️ {NO_REGION_NOTE}(목록에 지역 없음)"
_NO_REGION_IN_POSTING: Final = f"⚠️ {NO_REGION_NOTE}(공고에 지역 없음)"


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

    @property
    def evidence(self) -> str:
        """운영자가 3초에 확인할 수 있는 한 줄.

        ⚠️ 지역을 못 본 건은 그렇다고 **적어 둔다** — 이 표시가 없으면 무고한 교회가
        걸렸을 때 되짚을 방법이 없다.
        """
        ruled = ",".join(self.entry.ruled_by) or "규정 기관 미상"
        parts = [f"{self.field}={self.matched}", f"목록: {self.entry.name}", f"규정: {ruled}"]
        parts.append(self._region_note())
        return " · ".join(parts)

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
) -> HeresyMatch | None:
    """목록에 걸리면 `HeresyMatch`, 아니면 `None`.

    `region`은 공고의 지역이다. 목록 항목에 지역이 있는데 **다르면 다른 교회이므로**
    거절하지 않는다 — 이단을 봐주는 것이 아니라 애초에 그 교회가 아니다.
    """
    for field, value in (("church_name", church_name), ("raw_denomination", raw_denomination)):
        if value is None:
            continue
        entry = _applies(ref.by_name.get(_key(value), ()), region)
        if entry is None:
            continue
        return HeresyMatch(entry=entry, matched=value.strip(), field=field, posting_region=region)
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
    """대조용 열쇠 — 유니코드 정규화 + 공백 제거 + 소문자.

    ⚠️ **NFKC를 거른다**(`verify`와 같은 기준). 분해형(NFD)이나 전각으로 오면 눈에 같아 보이는
    이름이 목록을 그냥 통과한다. 원장 3,188건의 교회명·교단 칸에서는 아직 관측되지 않았지만
    (전부 NFC), 첨부 파일명에 NFD를 쓰는 게시판이 있어 본문에도 언제든 올 수 있다.
    """
    return squeeze(unicodedata.normalize("NFKC", name)).lower()
