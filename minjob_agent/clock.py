"""시각 생성·직렬화 단일 창구.

CLAUDE.md: "시간은 UTC·ISO8601로 한 헬퍼에서만 생성한다(포맷 드리프트 방지)".
timestamptz 컬럼(SPEC §6)에 들어갈 값이라 **항상 타임존 인식(UTC)** 이어야 한다 —
naive datetime을 저장하면 나중에 Supabase가 서버 로컬시간으로 해석해 조용히 어긋난다.

`date` 컬럼(`posted_at`·`deadline`)도 여기서 다룬다 — serde가 따로 포맷하면
"한 헬퍼에서만"이 하루 만에 무너진다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

_ISO_SUFFIX_UTC = "Z"
_ISO_OFFSET_UTC = "+00:00"


def utc_now() -> datetime:
    """현재 시각(UTC, 마이크로초 유지)."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """타임존 인식 UTC로 **정규화한 값을 반환**한다. naive면 거부.

    호출자는 반환값을 써야 한다 — 검사만 하고 원본을 저장하면 `+09:00`이 그대로 남아
    날짜 경계 비교(백필 컷오프·§7 경보)와 로그가 로컬시간으로 어긋난다.
    """
    if value.tzinfo is None:
        raise ValueError(f"naive datetime은 저장할 수 없음(UTC 명시 필요): {value!r}")
    return value.astimezone(UTC)


def to_iso(value: datetime) -> str:
    """저장용 ISO8601(UTC, `Z` 접미)."""
    # astimezone(UTC) 후 isoformat()은 항상 `+00:00`으로 끝난다.
    return ensure_utc(value).isoformat().removesuffix(_ISO_OFFSET_UTC) + _ISO_SUFFIX_UTC


def parse_iso(text: str) -> datetime:
    """`to_iso` 출력(및 오프셋 있는 ISO8601)을 되읽는다. 오프셋이 없으면 거부."""
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as err:
        raise ValueError(f"ISO8601 시각이 아님: {text!r}") from err
    return ensure_utc(parsed)


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


def require_plain_date(value: date) -> date:
    """`datetime`이 아닌 순수 `date`인지 확인한다.

    `datetime`은 `date`의 서브클래스라 타입 검사·런타임 모두 통과한다 →
    `posted_at`에 시각이 들어가 `YYYY-MM-DD` 컬럼과 어긋난다.
    """
    if isinstance(value, datetime):
        raise ValueError(f"date 컬럼에 datetime을 넣을 수 없음: {value!r}")
    return value
