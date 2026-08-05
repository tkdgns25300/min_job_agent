"""레지스트리 로더 테스트 — 네트워크를 타지 않는다(CLAUDE.md 가드레일 #7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minjob_ingest.domain import Denomination, Encoding, FetchTier
from minjob_ingest.sources.registry import (
    ConfigError,
    SourceConfig,
    detail_url,
    enabled_sources,
    find_source,
    load_sources,
)

EXPECTED_SOURCE_COUNT = 31

#: 라이브 검증(2026-07-29)으로 확정된 플래그 배치 — 하나라도 빠지면 그 게시판이 조용히 실패한다.
EXPECTED_FLAG_OWNERS = {
    "www_required": {"KWANGSHIN", "BPU", "HANIL", "KTS", "MTU", "UHS"},
    "http_only": {"CALVIN", "WGST"},
    "spoof_ua": {"MTU"},
    # 2026-08-04 실측 추가: Python의 기본 TLS로는 셋 다 연결 실패한다(curl은 성공 — macOS
    # 키체인이 중간 인증서를 갖고 있어서다). DAESHIN·KTS는 중간 인증서 누락,
    # PUTS는 cipher 보안수준이 서버보다 높아 핸드셰이크 자체가 안 된다.
    "insecure_tls": {"DAESHIN", "KTS", "PUTS"},
    "needs_session": {"CALVIN", "CSU"},
    # CALVIN 추가(2026-08-04 실측): 본문 텍스트가 항상 0자이고 내용이 인라인
    # `data:image/png;base64` 한 장(약 150KB)에 들어 있다.
    "image_only": {"PCKWORLD", "CALVIN"},
    # UHS·SUNGKYUL 추가(2026-08-04 실측): 잘못된 상세 id에도 HTTP 200 + 껍데기를 준다.
    "soft_200": {"BU", "KAICAM", "UHS", "SUNGKYUL"},
}


@pytest.fixture(scope="module")
def real_sources() -> tuple[SourceConfig, ...]:
    """실제 config를 한 번만 읽는다."""
    return load_sources()


def _valid_row() -> dict[str, object]:
    return {
        "key": "YTUS",
        "board_name": "영남신대 취업/초빙",
        "denomination_hint": "TONGHAP",
        "enabled": True,
        "fetch_tier": "static",
        "encoding": "utf-8",
        "list_url": "https://www.ytus.ac.kr/board/list/trXXR",
        "detail_pattern": "/board/view/trXXR/{id}",
        "fetch_note": "공지행 skip",
    }


def _write(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    target = tmp_path / "sources.json"
    target.write_text(json.dumps({"sources": rows}), encoding="utf-8")
    return target


# ── 실제 config: 이식 자산이 유실되지 않았는지 고정 ──────────────────


def test_real_config_loads_all_sources(real_sources: tuple[SourceConfig, ...]) -> None:
    assert len(real_sources) == EXPECTED_SOURCE_COUNT


def test_real_config_keys_match_contract(real_sources: tuple[SourceConfig, ...]) -> None:
    # CONTRACT §4의 31곳. 키가 바뀌면 저장된 원장·헬스와 연결이 끊긴다.
    assert {s.key for s in real_sources} == {
        "DAESHIN", "CALVIN", "KWANGSHIN", "CSU",
        "YTUS", "PUTS", "HTUS", "BPU", "PCK", "SJS", "PCKWORLD", "HANIL",
        "BU", "PGAK", "KTS", "KOSIN_TH", "HAPSHIN",
        "MTU", "UHS", "MOKWON", "HANSEI", "STS",
        "KBTUS", "KOREABAPTIST", "KEHC", "SUNGKYUL",
        "KAICAM", "NAZARENE", "TTGU", "ACTS", "WGST",
    }  # fmt: skip


def test_real_config_flags_are_exactly_as_verified(real_sources: tuple[SourceConfig, ...]) -> None:
    for flag_name, owners in EXPECTED_FLAG_OWNERS.items():
        actual = {s.key for s in real_sources if getattr(s.flags, flag_name)}
        assert actual == owners, f"{flag_name} 소유 게시판이 달라짐"


def test_real_config_json_tier_sources(real_sources: tuple[SourceConfig, ...]) -> None:
    assert {s.key for s in real_sources if s.fetch_tier is FetchTier.JSON} == {"CSU", "HANIL"}


def test_real_config_has_no_headless_source(real_sources: tuple[SourceConfig, ...]) -> None:
    # 2026-07-29 실측: 31곳 모두 정적/JSON → 브라우저 자동화 불필요.
    assert all(s.fetch_tier is not FetchTier.HEADLESS for s in real_sources)


def test_real_config_interdenominational_sources(real_sources: tuple[SourceConfig, ...]) -> None:
    # 초교파(횃불·아신대·WGST)는 default 교단이 없다 — 교단은 공고에서 판정한다.
    assert [s.key for s in real_sources if s.is_interdenominational] == ["TTGU", "ACTS", "WGST"]


def test_real_config_keeps_fetch_notes(real_sources: tuple[SourceConfig, ...]) -> None:
    # 라이브 검증 메모는 재취득 불가 자산 — 요약·삭제되면 어댑터 구현자가 함정을 다시 밟는다.
    short = {s.key for s in real_sources if len(s.fetch_note) < 20}
    assert not short, f"fetch_note가 너무 짧음(유실 의심): {short}"


def test_real_config_sources_without_detail_template(
    real_sources: tuple[SourceConfig, ...],
) -> None:
    # CSU=API 호출, HANSEI=경로에 카테고리 id가 섞여 템플릿 불가 → 목록 링크를 그대로 쓴다.
    assert {s.key for s in real_sources if s.detail_pattern is None} == {"CSU", "HANSEI"}


def test_real_config_detail_patterns_are_usable(real_sources: tuple[SourceConfig, ...]) -> None:
    for source in real_sources:
        if source.detail_pattern is None:
            continue
        built = detail_url(source, "12345")
        assert built.startswith(("http://", "https://")), source.key
        assert "{" not in built, f"{source.key}: 치환되지 않은 자리표시자"
        assert "12345" in built, source.key


def test_euc_kr_sources_decode_with_cp949(real_sources: tuple[SourceConfig, ...]) -> None:
    euc_kr = [s for s in real_sources if s.encoding is Encoding.EUC_KR]
    assert {s.key for s in euc_kr} == {"PUTS", "HTUS", "SJS", "ACTS"}
    assert all(s.encoding.python_codec == "cp949" for s in euc_kr)


# ── 조회·URL 헬퍼 ────────────────────────────────────────────────


def test_find_source_is_case_insensitive(real_sources: tuple[SourceConfig, ...]) -> None:
    found = find_source(real_sources, "ytus")
    assert found is not None
    assert found.key == "YTUS"


def test_find_source_returns_none_for_unknown(real_sources: tuple[SourceConfig, ...]) -> None:
    assert find_source(real_sources, "NOPE") is None


def test_detail_url_joins_relative_pattern(tmp_path: Path) -> None:
    source = load_sources(_write(tmp_path, [_valid_row()]))[0]
    assert detail_url(source, "42") == "https://www.ytus.ac.kr/board/view/trXXR/42"


def test_detail_url_keeps_absolute_pattern(tmp_path: Path) -> None:
    row = {**_valid_row(), "detail_pattern": "https://www.sungkyul.org/board?uid={id}"}
    source = load_sources(_write(tmp_path, [row]))[0]
    assert detail_url(source, "7") == "https://www.sungkyul.org/board?uid=7"


def test_detail_url_rejects_source_without_pattern(tmp_path: Path) -> None:
    row = _valid_row()
    del row["detail_pattern"]
    source = load_sources(_write(tmp_path, [row]))[0]
    with pytest.raises(ValueError, match="detail_pattern"):
        detail_url(source, "1")


def test_enabled_sources_filters_disabled(tmp_path: Path) -> None:
    rows = [
        _valid_row(),
        {**_valid_row(), "key": "PROK", "enabled": False, "disabled_reason": "2025-08 이후 공고 0"},
    ]
    sources = load_sources(_write(tmp_path, rows))
    assert len(sources) == 2
    assert [s.key for s in enabled_sources(sources)] == ["YTUS"]


# ── 검증 실패: 잘못된 config는 크롤 전에 죽어야 한다 ───────────────


def test_rejects_lowercase_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="영문 대문자"):
        load_sources(_write(tmp_path, [{**_valid_row(), "key": "ytus"}]))


def test_rejects_korean_key(tmp_path: Path) -> None:
    # "영남".upper() == "영남"이라 대문자 검사만으로는 통과한다 → 정규식으로 막는다.
    with pytest.raises(ConfigError, match="영문 대문자"):
        load_sources(_write(tmp_path, [{**_valid_row(), "key": "영남"}]))


def test_rejects_digit_leading_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="영문 대문자"):
        load_sources(_write(tmp_path, [{**_valid_row(), "key": "1ST"}]))


def test_rejects_duplicate_keys(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="중복"):
        load_sources(_write(tmp_path, [_valid_row(), _valid_row()]))


def test_rejects_unknown_field(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="알 수 없는 필드"):
        load_sources(_write(tmp_path, [{**_valid_row(), "selector": ".row"}]))


def test_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    target = tmp_path / "sources.json"
    target.write_text(json.dumps({"version": 2, "sources": [_valid_row()]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="최상위 필드"):
        load_sources(target)


@pytest.mark.parametrize("field_name", ["key", "board_name", "list_url", "fetch_note", "enabled"])
def test_rejects_missing_required_field(tmp_path: Path, field_name: str) -> None:
    row = _valid_row()
    del row[field_name]
    with pytest.raises(ConfigError, match="필수 필드 누락"):
        load_sources(_write(tmp_path, [row]))


def test_rejects_whitespace_only_fetch_note(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="fetch_note"):
        load_sources(_write(tmp_path, [{**_valid_row(), "fetch_note": "   "}]))


def test_rejects_non_bool_enabled(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="true/false"):
        load_sources(_write(tmp_path, [{**_valid_row(), "enabled": "true"}]))


def test_rejects_non_object_row(tmp_path: Path) -> None:
    target = tmp_path / "sources.json"
    target.write_text(json.dumps({"sources": [1]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON 객체가 아님"):
        load_sources(target)


def test_rejects_unknown_denomination_hint(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="알 수 없는 교단"):
        load_sources(_write(tmp_path, [{**_valid_row(), "denomination_hint": "KIJANG"}]))


def test_rejects_unknown_as_hint(tmp_path: Path) -> None:
    # UNKNOWN은 공고 판정 결과 전용 — 게시판 힌트로 쓸 수 없다(초교파는 null).
    with pytest.raises(ConfigError, match="UNKNOWN"):
        load_sources(_write(tmp_path, [{**_valid_row(), "denomination_hint": "UNKNOWN"}]))


def test_accepts_null_hint_as_interdenominational(tmp_path: Path) -> None:
    sources = load_sources(_write(tmp_path, [{**_valid_row(), "denomination_hint": None}]))
    assert sources[0].is_interdenominational


@pytest.mark.parametrize("bad_value", ["EUC-KR", "utf8", "cp949"])
def test_rejects_unknown_encoding(tmp_path: Path, bad_value: str) -> None:
    with pytest.raises(ConfigError, match="허용값 아님"):
        load_sources(_write(tmp_path, [{**_valid_row(), "encoding": bad_value}]))


def test_rejects_unknown_fetch_tier(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="허용값 아님"):
        load_sources(_write(tmp_path, [{**_valid_row(), "fetch_tier": "browser"}]))


def test_rejects_detail_pattern_without_placeholder(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="자리표시자"):
        load_sources(_write(tmp_path, [{**_valid_row(), "detail_pattern": "/board/view"}]))


def test_rejects_detail_pattern_with_extra_placeholder(tmp_path: Path) -> None:
    # 치환되지 않는 자리표시자가 남으면 쓰레기 URL로 실제 사이트에 요청이 나간다.
    row = {**_valid_row(), "detail_pattern": "/bbs/{catId}/{id}/view.do"}
    with pytest.raises(ConfigError, match="자리표시자"):
        load_sources(_write(tmp_path, [row]))


def test_rejects_prose_detail_pattern(tmp_path: Path) -> None:
    row = {**_valid_row(), "detail_pattern": "(목록 링크에서 추출 — /bbs/{id}/view.do)"}
    with pytest.raises(ConfigError, match="시작해야 함"):
        load_sources(_write(tmp_path, [row]))


def test_allows_missing_detail_pattern(tmp_path: Path) -> None:
    row = _valid_row()
    del row["detail_pattern"]
    assert load_sources(_write(tmp_path, [row]))[0].detail_pattern is None


def test_rejects_unknown_flag(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="알 수 없는 플래그"):
        load_sources(_write(tmp_path, [{**_valid_row(), "flags": {"retry": True}}]))


def test_rejects_non_bool_flag(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="true/false"):
        load_sources(_write(tmp_path, [{**_valid_row(), "flags": {"www_required": "yes"}}]))


def test_accepts_explicitly_false_flag(tmp_path: Path) -> None:
    row = {**_valid_row(), "flags": {"www_required": False}}
    assert load_sources(_write(tmp_path, [row]))[0].flags.www_required is False


def test_rejects_http_only_flag_with_https_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="http_only인데"):
        load_sources(_write(tmp_path, [{**_valid_row(), "flags": {"http_only": True}}]))


def test_accepts_http_url_with_http_only_flag(tmp_path: Path) -> None:
    row = {
        **_valid_row(),
        "list_url": "http://calvin.ac.kr/main/boardList.do",
        "flags": {"http_only": True},
    }
    assert load_sources(_write(tmp_path, [row]))[0].flags.http_only


def test_rejects_plain_http_url_without_flag(tmp_path: Path) -> None:
    row = {**_valid_row(), "list_url": "http://calvin.ac.kr/main/boardList.do"}
    with pytest.raises(ConfigError, match="https가 아님"):
        load_sources(_write(tmp_path, [row]))


def test_rejects_www_required_without_www_host(tmp_path: Path) -> None:
    row = {**_valid_row(), "list_url": "https://ytus.ac.kr/board", "flags": {"www_required": True}}
    with pytest.raises(ConfigError, match="www"):
        load_sources(_write(tmp_path, [row]))


def test_rejects_relative_list_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="절대 URL"):
        load_sources(_write(tmp_path, [{**_valid_row(), "list_url": "/board/list"}]))


def test_rejects_credentials_in_list_url(tmp_path: Path) -> None:
    row = {**_valid_row(), "list_url": "https://admin:secret@www.ytus.ac.kr/board"}
    with pytest.raises(ConfigError, match="자격증명"):
        load_sources(_write(tmp_path, [row]))


def test_rejects_malformed_url_as_config_error(tmp_path: Path) -> None:
    # urlsplit은 ValueError를 던진다 → 경계에서 ConfigError로 바꿔야 CLI가 traceback을 안 뿜는다.
    with pytest.raises(ConfigError):
        load_sources(_write(tmp_path, [{**_valid_row(), "list_url": "https://[::1"}]))


def test_requires_disabled_reason_when_disabled(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="disabled_reason"):
        load_sources(_write(tmp_path, [{**_valid_row(), "enabled": False}]))


def test_rejects_disabled_reason_when_enabled(tmp_path: Path) -> None:
    row = {**_valid_row(), "disabled_reason": "그냥"}
    with pytest.raises(ConfigError, match="disabled_reason"):
        load_sources(_write(tmp_path, [row]))


def test_rejects_broken_json(tmp_path: Path) -> None:
    target = tmp_path / "sources.json"
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="파싱 실패"):
        load_sources(target)


def test_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="읽을 수 없음"):
        load_sources(tmp_path / "nope.json")


def test_rejects_empty_sources(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="비어 있음"):
        load_sources(_write(tmp_path, []))


def test_error_message_names_the_source_key(tmp_path: Path) -> None:
    # 31행 파일에서 배열 첨자만 주면 운영자가 줄을 세어야 한다.
    with pytest.raises(ConfigError, match="YTUS"):
        load_sources(_write(tmp_path, [{**_valid_row(), "encoding": "nope"}]))


def test_denomination_publishable_excludes_unknown() -> None:
    from minjob_ingest.domain import PUBLISHABLE_DENOMINATIONS

    assert Denomination.UNKNOWN not in PUBLISHABLE_DENOMINATIONS
    # 순서가 고정돼야 프롬프트·메시지 출력이 실행마다 흔들리지 않는다.
    assert (
        tuple(d for d in Denomination if d is not Denomination.UNKNOWN) == PUBLISHABLE_DENOMINATIONS
    )
