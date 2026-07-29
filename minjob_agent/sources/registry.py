"""소스 레지스트리 — `config/sources.json` 로드·검증.

"어디를 어떻게 접속하나"는 코드가 아니라 데이터다(CLAUDE.md 3층 분리).
이 모듈은 그 데이터를 읽어 검증된 `SourceConfig`로만 내보낸다 — 게시판 URL·셀렉터를
파이프라인 코드에 하드코딩하지 않기 위한 유일한 창구.

config 값은 2026-07-29 라이브 2차 검증 결과이며 **전송 사실의 정본**이다(CLAUDE.md 정본 순서).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import MISSING, dataclass, fields
from enum import StrEnum
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from minjob_agent.domain import Denomination, Encoding, FetchTier, normalize_source_key
from minjob_agent.paths import DEFAULT_SOURCES_PATH

_ID_PLACEHOLDER = "{id}"
_PLACEHOLDER_PATTERN = re.compile(r"\{[^}]*\}")


class ConfigError(Exception):
    """config가 계약을 위반했을 때. 파이프라인을 시작하기 전에 즉시 실패시킨다."""


@dataclass(frozen=True, slots=True)
class SourceFlags:
    """전송 함정 플래그. 전부 2026-07-29 실측 근거가 있다(사유는 `fetch_note`)."""

    #: apex가 무응답/404/인증서 불일치 → www 호스트 필수
    www_required: bool = False
    #: https 미지원 → http로만 접속
    http_only: bool = False
    #: 기본 UA로는 차단/빈 응답 → 브라우저 UA 필수
    spoof_ua: bool = False
    #: 인증서 체인 오류 → TLS 검증 무시 필요
    insecure_tls: bool = False
    #: 상세가 세션 쿠키를 요구 → 목록 GET으로 쿠키 확보 후 상세
    needs_session: bool = False
    #: 본문이 이미지뿐 → 빈 raw_text가 정상(파싱 실패로 오판 금지)
    image_only: bool = False
    #: 잘못된 요청·없는 글에도 200 → 성공을 본문 내용으로 판정
    soft_200: bool = False


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """게시판 1곳의 전송 설정."""

    key: str
    board_name: str
    #: 참고 힌트일 뿐 확정 근거가 아니다. 교단은 공고에서 확정한다(SPEC §5.3).
    #: None = 초교파(게시판에 여러 교단이 섞임).
    denomination_hint: Denomination | None
    enabled: bool
    fetch_tier: FetchTier
    encoding: Encoding
    list_url: str
    #: 라이브 검증 메모(세션·soft 실패·공지행·pagination). 재취득 불가 — 지우지 않는다.
    fetch_note: str
    #: `{id}`를 external_id로 치환해 상세 URL을 만든다.
    #: None = 템플릿으로 만들 수 없는 소스(API 호출·경로에 다른 가변 id가 섞임)
    #: → 목록에서 얻은 링크를 그대로 쓴다.
    detail_pattern: str | None = None
    #: `enabled: false`인 이유. 제외는 삭제가 아니라 비활성 + 사유로 남긴다(CLAUDE.md Registry).
    disabled_reason: str | None = None
    flags: SourceFlags = SourceFlags()

    @property
    def is_interdenominational(self) -> bool:
        """초교파 게시판인가(default 교단 없음)."""
        return self.denomination_hint is None


#: 스키마는 dataclass에서 파생한다 — 필드를 늘릴 때 목록을 따로 고치다 어긋나지 않게.
_REQUIRED_FIELDS = frozenset(f.name for f in fields(SourceConfig) if f.default is MISSING)
_OPTIONAL_FIELDS = frozenset(f.name for f in fields(SourceConfig) if f.default is not MISSING)
_FLAG_NAMES = frozenset(f.name for f in fields(SourceFlags))


def load_sources(path: Path | None = None) -> tuple[SourceConfig, ...]:
    """config를 읽어 검증된 소스 목록을 반환한다. 위반이 하나라도 있으면 `ConfigError`."""
    target = DEFAULT_SOURCES_PATH if path is None else path
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(f"config를 읽을 수 없음: {target} ({err})") from err

    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as err:
        raise ConfigError(f"config JSON 파싱 실패: {target} ({err})") from err

    document = _as_object(raw, str(target))
    unknown_top = set(document) - {"sources"}
    if unknown_top:
        raise ConfigError(f"{target}: 알 수 없는 최상위 필드 {sorted(unknown_top)}")
    rows = _as_list(document.get("sources"), "sources")
    if not rows:
        raise ConfigError("sources가 비어 있음")

    sources = tuple(
        _parse_source(_as_object(row, f"sources[{index}]"), index) for index, row in enumerate(rows)
    )
    _check_unique_keys(sources)
    return sources


def enabled_sources(sources: Sequence[SourceConfig]) -> tuple[SourceConfig, ...]:
    """크롤 대상만. 제외된 소스는 삭제하지 않고 `enabled: false`로 남긴다(이력 보존)."""
    return tuple(s for s in sources if s.enabled)


def find_source(sources: Sequence[SourceConfig], key: str) -> SourceConfig | None:
    """source_key로 조회. 키는 대문자가 저장값이다."""
    wanted = key.upper()
    return next((s for s in sources if s.key == wanted), None)


def detail_url(source: SourceConfig, external_id: str) -> str:
    """`detail_pattern`에 external_id를 넣어 절대 URL로 만든다.

    템플릿이 없는 소스(`detail_pattern is None`)는 목록에서 얻은 링크를 그대로 써야 하므로
    여기서 URL을 만들 수 없다 → `ValueError`. 어댑터가 분기하지 않고 지나치는 것을 막는다.
    """
    if source.detail_pattern is None:
        raise ValueError(f"{source.key}: detail_pattern이 없어 URL을 만들 수 없음(목록 링크 사용)")
    filled = source.detail_pattern.replace(_ID_PLACEHOLDER, external_id)
    if filled.startswith(("http://", "https://")):
        return filled
    origin = urlsplit(source.list_url)
    return f"{origin.scheme}://{origin.netloc}{filled}"


# ── 검증 헬퍼 — 경계에서 좁힌다(외부 파일을 신뢰하지 않는다) ──────────────────


def _as_object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{what}: JSON 객체가 아님")
    # JSON 객체의 키는 항상 문자열이라 재검사 없이 좁힌다.
    return dict(value)


def _as_list(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise ConfigError(f"{what}: JSON 배열이 아님")
    return value


def _require_str(row: dict[str, object], field_name: str, what: str) -> str:
    value = row.get(field_name)
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError(f"{what}.{field_name}: 비어있지 않은 문자열이어야 함")
    return value.strip()


def _require_bool(row: dict[str, object], field_name: str, what: str) -> bool:
    value = row.get(field_name)
    if not isinstance(value, bool):
        raise ConfigError(f"{what}.{field_name}: true/false여야 함")
    return value


def _parse_source(row: dict[str, object], index: int) -> SourceConfig:
    label = _row_label(row, index)

    unknown = set(row) - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
    if unknown:
        raise ConfigError(f"{label}: 알 수 없는 필드 {sorted(unknown)}")
    missing = _REQUIRED_FIELDS - set(row)
    if missing:
        raise ConfigError(f"{label}: 필수 필드 누락 {sorted(missing)}")

    key = _parse_key(row, label)
    label = f"sources[{index}]({key})"
    list_url = _parse_list_url(row, label)
    flags = _parse_flags(row.get("flags"), label)
    _check_flags_match_url(list_url, flags, label)
    enabled = _require_bool(row, "enabled", label)

    return SourceConfig(
        key=key,
        board_name=_require_str(row, "board_name", label),
        denomination_hint=_parse_hint(row.get("denomination_hint"), label),
        enabled=enabled,
        fetch_tier=_parse_enum(FetchTier, row, "fetch_tier", label),
        encoding=_parse_enum(Encoding, row, "encoding", label),
        list_url=list_url,
        fetch_note=_require_str(row, "fetch_note", label),
        detail_pattern=_parse_detail_pattern(row.get("detail_pattern"), label),
        disabled_reason=_parse_disabled_reason(row.get("disabled_reason"), enabled, label),
        flags=flags,
    )


def _row_label(row: dict[str, object], index: int) -> str:
    """오류 메시지용 위치 — 31행 파일에서 배열 첨자만 주면 운영자가 세어야 한다."""
    key = row.get("key")
    return f"sources[{index}]({key})" if isinstance(key, str) else f"sources[{index}]"


def _parse_key(row: dict[str, object], what: str) -> str:
    key = _require_str(row, "key", what)
    # 형식 규칙은 domain에 한 벌만 둔다(레코드도 같은 규칙을 쓴다).
    try:
        return normalize_source_key(key)
    except ValueError as err:
        raise ConfigError(f"{what}.key: {err}") from err


def _parse_hint(value: object, what: str) -> Denomination | None:
    """None = 초교파. UNKNOWN은 공고 판정 결과 전용이라 힌트로 쓸 수 없다."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{what}.denomination_hint: 문자열 또는 null이어야 함")
    try:
        hint = Denomination(value)
    except ValueError as err:
        raise ConfigError(f"{what}.denomination_hint: 알 수 없는 교단 {value!r}") from err
    if hint is Denomination.UNKNOWN:
        raise ConfigError(f"{what}.denomination_hint: UNKNOWN은 힌트로 쓸 수 없음(초교파는 null)")
    return hint


def _parse_enum[E: StrEnum](
    enum_type: type[E], row: dict[str, object], field_name: str, what: str
) -> E:
    value = _require_str(row, field_name, what)
    try:
        return enum_type(value)
    except ValueError as err:
        allowed = sorted(member.value for member in enum_type)
        raise ConfigError(f"{what}.{field_name}: {value!r}는 허용값 아님 (허용 {allowed})") from err


def _parse_list_url(row: dict[str, object], what: str) -> str:
    value = _require_str(row, "list_url", what)
    parts = _split_url(value, f"{what}.list_url")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"{what}.list_url: http(s) 절대 URL이어야 함 (받은 값 {value!r})")
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{what}.list_url: URL에 자격증명을 넣지 않는다(비밀은 env로)")
    return value


def _parse_detail_pattern(value: object, what: str) -> str | None:
    """None/생략 = 템플릿 없음(목록 링크 사용). 있으면 **실제로 치환 가능한 URL**이어야 한다."""
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError(f"{what}.detail_pattern: 문자열이거나 생략/null이어야 함")
    pattern = value.strip()
    if not pattern.startswith(("/", "http://", "https://")):
        # 설명 문장을 넣어두면 치환 결과가 쓰레기 URL이 되어 실제 사이트로 요청이 나간다.
        raise ConfigError(
            f"{what}.detail_pattern: '/' 또는 http(s)로 시작해야 함 (받은 값 {pattern!r})"
        )
    placeholders = set(_PLACEHOLDER_PATTERN.findall(pattern))
    if placeholders != {_ID_PLACEHOLDER}:
        raise ConfigError(
            f"{what}.detail_pattern: 자리표시자는 {_ID_PLACEHOLDER} 하나여야 함 "
            f"(받은 값 {sorted(placeholders)})"
        )
    return pattern


def _parse_disabled_reason(value: object, enabled: bool, what: str) -> str | None:
    if value is None:
        if not enabled:
            raise ConfigError(f"{what}: enabled=false면 disabled_reason이 필요함(이력 보존)")
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError(f"{what}.disabled_reason: 비어있지 않은 문자열이어야 함")
    if enabled:
        raise ConfigError(f"{what}: enabled=true인데 disabled_reason이 있음")
    return value.strip()


def _parse_flags(value: object, what: str) -> SourceFlags:
    if value is None:
        return SourceFlags()
    row = _as_object(value, f"{what}.flags")
    unknown = set(row) - _FLAG_NAMES
    if unknown:
        raise ConfigError(f"{what}.flags: 알 수 없는 플래그 {sorted(unknown)}")
    # 키는 위에서 화이트리스트로 좁혔으므로 ** 전개가 안전하다(값 타입은 여기서 확인).
    return SourceFlags(**{name: _require_bool(row, name, f"{what}.flags") for name in row})


def _split_url(value: str, what: str) -> SplitResult:
    """`urlsplit`은 잘못된 authority에 `ValueError`를 던진다 → 경계에서 ConfigError로 바꾼다."""
    try:
        return urlsplit(value)
    except ValueError as err:
        raise ConfigError(f"{what}: URL 파싱 실패 ({err})") from err


def _check_flags_match_url(list_url: str, flags: SourceFlags, what: str) -> None:
    """플래그와 URL이 모순되면 크롤이 조용히 실패한다 → 로드 시 막는다."""
    parts = _split_url(list_url, f"{what}.list_url")
    if flags.http_only and parts.scheme != "http":
        raise ConfigError(f"{what}: http_only인데 list_url이 http가 아님")
    if not flags.http_only and parts.scheme != "https":
        raise ConfigError(f"{what}: http_only가 아닌데 list_url이 https가 아님")
    if flags.www_required and not (parts.hostname or "").startswith("www."):
        raise ConfigError(f"{what}: www_required인데 list_url 호스트에 www가 없음")


def _check_unique_keys(sources: Sequence[SourceConfig]) -> None:
    counts = Counter(s.key for s in sources)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    if duplicates:
        raise ConfigError(f"source_key 중복: {duplicates}")
