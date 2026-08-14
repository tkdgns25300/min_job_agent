"""시각 생성·직렬화 단일 창구.

CLAUDE.md: "시간은 KST·ISO8601로 한 헬퍼에서만 생성한다(포맷 드리프트 방지)".
timestamptz 컬럼(SPEC §6)에 들어갈 값이라 **항상 타임존 인식** 이어야 한다 —
naive datetime을 저장하면 나중에 Supabase가 서버 로컬시간으로 해석해 조용히 어긋난다.

⚠️ **KST로 바꾼 것은 표기이고 순간이 아니다**(2026-08-05 · 운영자 결정). `...Z`(UTC)와
`+09:00`(KST)은 **같은 순간의 다른 표기**이고, Postgres `timestamptz`는 둘을 동일하게
저장한다. 바뀌는 것은 사람이 파일·로그를 열었을 때 보이는 값뿐이다 — 운영자·게시판·공고가
모두 한국 시간을 쓰므로 그쪽에 맞춘다.

⚠️ **오프셋을 떼면 안 된다.** `+09:00` 없는 naive KST는 "언제인지 모르는 값"이고, DB가
서버 시간대로 해석해 9시간 어긋난다. 그래서 `ensure_kst`가 naive를 거부한다.

`date` 컬럼(`posted_on`·`deadline`)은 시간대가 없다 — 게시판이 이미 KST로 표시한 날짜라
**변환 대상이 아니다**(하루씩 밀면 백필 컷오프가 어긋난다).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo

#: 저장·표시 기준 시간대. 게시판 31곳과 운영자가 모두 한국이다.
KST: Final = ZoneInfo("Asia/Seoul")


def kst_now() -> datetime:
    """현재 시각(KST, 마이크로초 유지)."""
    return datetime.now(KST)


def ensure_kst(value: datetime) -> datetime:
    """타임존 인식 KST로 **정규화한 값을 반환**한다. naive면 거부.

    호출자는 반환값을 써야 한다 — 검사만 하고 원본을 저장하면 소스마다 다른 오프셋이 그대로
    남아 날짜 경계 비교(백필 컷오프·§7 경보)와 로그가 어긋난다. `Z`로 들어온 값도 여기서
    `+09:00`으로 바뀐다(같은 순간).
    """
    if value.tzinfo is None:
        raise ValueError(f"naive datetime은 저장할 수 없음(시간대 명시 필요): {value!r}")
    return value.astimezone(KST)


def to_iso(value: datetime) -> str:
    """저장용 ISO8601(KST, `+09:00` 오프셋)."""
    return ensure_kst(value).isoformat()


def parse_iso(text: str) -> datetime:
    """오프셋 있는 ISO8601을 되읽는다. 오프셋이 없으면 거부.

    ⚠️ 과거에 저장한 `...Z`(UTC)도 그대로 읽힌다 — 같은 순간이므로 KST로 정규화된다.
    """
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as err:
        raise ValueError(f"ISO8601 시각이 아님: {text!r}") from err
    return ensure_kst(parsed)


def to_iso_date(value: date) -> str:
    """저장용 날짜(`YYYY-MM-DD`). `datetime`은 거부 — date 컬럼에 시각이 섞이면 안 된다."""
    require_plain_date(value)
    return value.isoformat()


def parse_iso_date(text: str) -> date:
    """`YYYY-MM-DD`를 되읽는다."""
    try:
        return date.fromisoformat(text.strip())
    except ValueError as err:
        raise ValueError(f"YYYY-MM-DD 날짜가 아님: {text!r}") from err


def board_today() -> date:
    """게시판이 보는 "오늘"(KST).

    게시판은 한국 시간으로 날짜를 표시하므로, 연도 없는 `MM-DD`를 되살릴 때의 기준도 KST다.
    저장 시각도 이제 KST라 `kst_now().date()`와 같지만, **의도가 다른 두 값**이므로 이름을
    유지한다 — 게시판 표기를 읽는 자리에서 이 함수를 부르면 왜 KST인지가 코드에 남는다.
    """
    return kst_now().date()


def require_plain_date(value: date) -> date:
    """`datetime`이 아닌 **순수 `date`가 실제로 있는지** 확인한다.

    `datetime`은 `date`의 서브클래스라 타입 검사·런타임 모두 통과한다 →
    `posted_at`에 시각이 들어가 `YYYY-MM-DD` 컬럼과 어긋난다.

    ⚠️ `None`도 막는다. 어댑터가 주는 값은 외부 입력이고, 타입만으로 필수를 선언하면
    런타임에는 조용히 통과한다 — `posted_on=None`인 행이 저장되면 그 공고는 만료 판정
    (SPEC §9)에서 빠지고 원장·구조화 두 조회가 갈린다(2026-08-14 검수).
    """
    if value is None:
        raise ValueError("date 컬럼이 비어 있음")
    if isinstance(value, datetime):
        raise ValueError(f"date 컬럼에 datetime을 넣을 수 없음: {value!r}")
    return value
