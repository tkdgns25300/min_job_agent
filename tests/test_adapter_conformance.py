"""등록된 모든 어댑터를 같은 기준으로 검사한다 — 31곳의 통일성을 사람 규율이 아니라 기계가 지킨다.

게시판마다 파일을 따로 두는 대신(CLAUDE.md 3층 분리) **반복되는 검증을 여기 한 곳에 모은다**.
게시판별 테스트 파일에는 그 게시판을 열어봐야 아는 **실측값만** 남기고, 아래 구조적 검사는
새 어댑터를 등록하는 순간 자동으로 적용된다.

여기서 잡는 것은 전부 **조용한 실패**다 — 예외 없이 0건이 되는 부류. YTUS에서 실제로 겪었다:
`td.num` 클래스가 바뀌자 18건이 0건이 되면서 아무 예외도 나지 않았고, 2페이지 URL이
`/page/2`로 끝나 20건의 external_id가 전부 `"2"`가 됐다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters.base import (
    ParseError,
    PostingRef,
    normalized_text,
    parse_html,
)
from minjob_ingest.sources.adapters.registry import Adapter, find_adapter, implemented_keys
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

FIXTURE_ROOT: Final = Path(__file__).parent / "fixtures"
LIST_FIXTURE: Final = "list.html"
PAGE2_FIXTURE: Final = "list_page2.html"
DETAIL_FIXTURE: Final = "detail.html"

#: 목록 자리에 들어올 수 있는 쓰레기 입력. 전부 `ParseError`여야 한다 — 빈 결과로 흘리면
#: "게시판이 조용하네"로 오해하고, 게시판이 개편된 것을 아무도 모른다.
_NOT_A_LIST: Final = {
    "빈 문자열": "",
    "빈 페이지": "<html><body></body></html>",
    "로그인 폼": '<html><body><form><input type="password"></form></body></html>',
    "에러 쉘": "<html><body><h1>500 Internal Server Error</h1></body></html>",
}

_KEYS: Final = implemented_keys()


def _adapter_and_source(key: str) -> tuple[Adapter, SourceConfig]:
    source = find_source(load_sources(None), key)
    assert source is not None, f"{key}: config/sources.json 에 없다"
    return find_adapter(key), source


def _fixture(key: str, name: str) -> str | None:
    path = FIXTURE_ROOT / key / name
    return path.read_text(encoding="utf-8") if path.exists() else None


def _require_fixture(key: str, name: str) -> str:
    html = _fixture(key, name)
    if html is None:
        pytest.skip(f"{key}/{name} 없음 — `minjob-ingest snapshot --source {key}` 로 받는다")
    return html


# ── 형태 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", _KEYS)
def test_module_file_is_named_after_the_key(key: str) -> None:
    """파일명 = `source_key` 소문자(CLAUDE.md). 에러가 `PUTS:`로 나오면 열 파일이 하나여야 한다."""
    adapter, _ = _adapter_and_source(key)
    # 어댑터는 **모듈**이라 `__file__`이 있다(Protocol 타입이어서 `inspect.getfile`은 못 쓴다).
    source_file = getattr(adapter, "__file__", None)
    assert source_file is not None, f"{key}: 모듈이 아니다 — 어댑터는 모듈이어야 한다"
    assert Path(str(source_file)).stem == key.lower()


@pytest.mark.parametrize("key", _KEYS)
def test_the_key_matches_config_exactly(key: str) -> None:
    """어댑터가 선언한 키가 **config에 저장된 형태와 글자 그대로** 같아야 한다.

    ⚠️ 레지스트리 키와 비교하면 헛돈다 — 레지스트리는 `ADAPTERS = {ytus.SOURCE_KEY: ytus}`로
    어댑터의 선언을 그대로 키로 쓰므로, 소문자로 잘못 써도 양쪽이 함께 틀려 일치한다.
    `source_key`는 영어 대문자가 계약이다(CLAUDE.md) — 저장값·로그·config가 모두 그 형태다.
    """
    _, source = _adapter_and_source(key)
    assert key == key.upper(), f"{key}: source_key는 대문자여야 한다"
    assert source.key == key, f"어댑터 선언 {key!r} ≠ config {source.key!r}"


@pytest.mark.parametrize("key", _KEYS)
def test_provides_the_whole_contract(key: str) -> None:
    adapter, _ = _adapter_and_source(key)
    for name in ("list_request", "parse_list", "parse_detail"):
        assert callable(getattr(adapter, name, None)), f"{key}: {name} 없음"


@pytest.mark.parametrize("key", _KEYS)
def test_pages_are_requested_differently(key: str) -> None:
    """페이징이 구현되지 않으면 1페이지를 반복 요청하고 **깊은 글에 영구히 도달하지 못한다**.

    `collect`가 페이지 경계 중복을 조용히 걸러내므로 증상이 "그 게시판은 원래 얕다"로 보인다.
    """
    adapter, source = _adapter_and_source(key)
    first, second = adapter.list_request(source, 1), adapter.list_request(source, 2)
    assert (first.url, first.form) != (second.url, second.form)


@pytest.mark.parametrize("key", _KEYS)
def test_page_zero_is_rejected(key: str) -> None:
    adapter, source = _adapter_and_source(key)
    with pytest.raises(ValueError):
        adapter.list_request(source, 0)


# ── 조용한 0건 차단 ──────────────────────────────────────────────


@pytest.mark.parametrize("key", _KEYS)
@pytest.mark.parametrize("html", _NOT_A_LIST.values(), ids=list(_NOT_A_LIST))
def test_a_non_list_is_an_error(key: str, html: str) -> None:
    """목록이 아닌 것을 받으면 **에러**여야 한다. 빈 결과로 흘리면 개편을 아무도 모른다."""
    adapter, source = _adapter_and_source(key)
    with pytest.raises(ParseError):
        adapter.parse_list(html, source)


# ── fixture 기반 (없으면 skip) ───────────────────────────────────


@pytest.mark.parametrize("key", _KEYS)
def test_fixture_list_yields_usable_refs(key: str) -> None:
    adapter, source = _adapter_and_source(key)
    refs = adapter.parse_list(_require_fixture(key, LIST_FIXTURE), source)
    assert refs, f"{key}: 실측 fixture에서 0건 — 셀렉터가 빗나갔다"
    ids = [ref.external_id for ref in refs]
    assert len(set(ids)) == len(ids), f"{key}: external_id 중복 {sorted(ids)}"
    for ref in refs:
        assert ref.external_id.strip(), f"{key}: 빈 external_id"
        assert ref.title.strip(), f"{key}: 빈 제목 ({ref.external_id})"
        assert ref.url.startswith("http"), f"{key}: 상대 URL {ref.url!r} — 절대 URL이어야 한다"


@pytest.mark.parametrize("key", _KEYS)
def test_dates_are_parsed_when_the_board_has_them(key: str) -> None:
    """config가 날짜가 있다고 한 게시판은 **전 행에 날짜가 있어야** 한다.

    없으면 `--months` 범위가 조용히 무의미해진다(그 행은 절대 컷오프에 걸리지 않는다).
    날짜가 정말 없는 게시판은 config `list_has_dates: false`로 표시한다.
    """
    adapter, source = _adapter_and_source(key)
    refs = adapter.parse_list(_require_fixture(key, LIST_FIXTURE), source)
    dated = [ref for ref in refs if ref.posted_on is not None]
    if source.list_has_dates:
        assert len(dated) == len(refs), f"{key}: 날짜 없는 행 {len(refs) - len(dated)}건"
    else:
        assert not dated, f"{key}: list_has_dates=false인데 날짜가 나온다 — config를 고친다"


@pytest.mark.parametrize("key", _KEYS)
def test_second_page_holds_different_postings(key: str) -> None:
    """⚠️ YTUS에서 2페이지 20건의 id가 전부 `"2"`였다 — URL 끝이 `/page/2`였기 때문이다.

    한 페이지만 보면 절대 드러나지 않는 버그라, 2페이지 fixture가 있으면 반드시 대조한다.
    """
    adapter, source = _adapter_and_source(key)
    page2 = _fixture(key, PAGE2_FIXTURE)
    if page2 is None:
        pytest.skip(f"{key}/{PAGE2_FIXTURE} 없음")
    first = adapter.parse_list(_require_fixture(key, LIST_FIXTURE), source)
    second = adapter.parse_list(page2, source)
    assert second, f"{key}: 2페이지에서 0건"
    shared = {ref.external_id for ref in first} & {ref.external_id for ref in second}
    assert not shared, f"{key}: 1·2페이지가 같은 id를 준다 {sorted(shared)}"


@pytest.mark.parametrize("key", _KEYS)
def test_fixture_detail_carries_evidence(key: str) -> None:
    """상세는 **본문·이미지·첨부 중 하나는** 줘야 한다 — 셋 다 없으면 증거 없는 레코드다."""
    adapter, source = _adapter_and_source(key)
    detail = _fixture(key, DETAIL_FIXTURE)
    if detail is None:
        pytest.skip(f"{key}/{DETAIL_FIXTURE} 없음")
    refs = adapter.parse_list(_require_fixture(key, LIST_FIXTURE), source)
    raw = adapter.parse_detail(detail, refs[0])
    assert raw.ref is refs[0]
    assert raw.raw_text.strip() or raw.image_urls or raw.attachments, (
        f"{key}: 본문·이미지·첨부가 모두 없다"
    )


#: `raw_html`을 담지 않는 소스와 그 이유. **비어 있는 것이 정상인 곳만** 여기 적는다 —
#: 목록에 없으면 아래 테스트가 실패해 "어댑터가 배선을 잊었다"를 드러낸다.
_WHITESPACE: Final = re.compile(r"[\s\u00a0]+")


def _squashed(text: str) -> str:
    """공백을 전부 지운 문자열. 정규화 차이를 무시하고 내용만 비교한다."""
    return _WHITESPACE.sub("", text)


_NO_STRUCTURE: Final = {
    # 상세가 포스터 `<img>` 한 장뿐이고 본문 컨테이너가 없다(config `image_only`).
    "PCKWORLD",
}


@pytest.mark.parametrize("key", _KEYS)
def test_detail_keeps_the_body_structure(key: str) -> None:
    """본문이 있으면 **구조도 함께** 담아야 한다(`raw_html` · SPEC §6).

    ⚠️ 이 테스트가 없으면 새 어댑터가 조용히 배선을 빠뜨리고, 그 게시판만 링크 `href`와 표
    대응을 잃는다. 그 사실은 몇 주 뒤 `church_links`가 비어 있을 때 드러나고, 그때는 이미
    수집이 끝나 **재수집 90분**을 써야 한다(2026-08-05에 그렇게 됐다).
    """
    adapter, source = _adapter_and_source(key)
    detail = _fixture(key, DETAIL_FIXTURE)
    if detail is None:
        pytest.skip(f"{key}/{DETAIL_FIXTURE} 없음")
    refs = adapter.parse_list(_require_fixture(key, LIST_FIXTURE), source)
    raw = adapter.parse_detail(detail, refs[0])
    if key in _NO_STRUCTURE:
        assert not raw.raw_html, f"{key}: 구조를 담지 않기로 한 소스인데 값이 있다 — 목록을 고친다"
        return
    assert raw.raw_html, f"{key}: raw_html이 비었다 — `structural_html(body)` 배선 확인"
    assert not raw.raw_html.startswith("<html>"), f"{key}: 문서 껍데기가 저장된다"
    # 구조는 본문과 **같은 요소**를 담아야 한다 — 다른 곳을 가리키면 배선이 어긋난 것이다.
    # ⚠️ 문자열을 그대로 비교하면 안 된다: `raw_text`는 정규화되고(nbsp→공백·공백 접기)
    # `raw_html`은 원문을 유지한다. 낱말 하나로 보는 것도 안 된다 — 라벨과 값이 태그 경계로
    # 갈리기 때문이다(`홈페이지:<a>http://…</a>` · HAPSHIN 실측).
    # → **태그를 벗겨 공백을 지운 뒤** 한쪽이 다른 쪽을 포함하는지 본다. 어느 방향이든 되는
    #   이유: 제목을 앞에 붙이는 어댑터(SUNGKYUL)와 본문 앞 상자를 합치는 어댑터(PCK)가 있다.
    from_html = _squashed(normalized_text(parse_html(raw.raw_html)))
    from_text = _squashed(raw.raw_text)
    if from_text:
        assert from_text in from_html or from_html in from_text, (
            f"{key}: raw_html이 본문과 다른 곳을 가리킨다"
        )


@pytest.mark.parametrize("key", _KEYS)
def test_detail_keeps_the_listing_ref(key: str) -> None:
    """상세 파서가 ref를 갈아치우면 원장 키가 어긋나 같은 글을 계속 새 글로 넣는다."""
    adapter, _ = _adapter_and_source(key)
    detail = _fixture(key, DETAIL_FIXTURE)
    if detail is None:
        pytest.skip(f"{key}/{DETAIL_FIXTURE} 없음")
    ref = PostingRef(external_id="probe-1", url="https://example.com/1", title="탐침")
    assert adapter.parse_detail(detail, ref).ref is ref


# ── 커버리지 ─────────────────────────────────────────────────────


def test_at_least_one_adapter_is_verified_against_a_fixture(
    adapter_fixture_coverage: tuple[tuple[str, ...], tuple[str, ...]],
) -> None:
    """⚠️ fixture가 전부 없으면 위 검사들이 **모두 skip되고 초록불**이 된다 — 검증 0건인데 통과다.

    `tests/fixtures/`는 커밋되지 않으므로 새 클론에서 실제로 이 상태가 된다.
    """
    have, missing = adapter_fixture_coverage
    assert have, (
        "fixture가 하나도 없다 — 어댑터 검증이 전부 건너뛰어졌다.\n"
        f"→ `minjob-ingest snapshot` 으로 받는다. 없는 곳: {list(missing)}"
    )
