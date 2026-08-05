"""KEHC 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source KEHC` 로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import kehc
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "KEHC"
#: 실측: 데이터 행 50 = 공지 2 + 비밀글 1 + 공고 47.
_EXPECTED_POSTINGS: Final = 47
_NOTICE_TITLES: Final = ("※ 구인구직 게시판 안내 사항", "청빙 게시글에 대한 참고내용")
#: 비밀글(`ico_lock.gif`) — 상세가 빈 페이지라 수집 대상이 아니다.
_LOCKED_ID: Final = "27561"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="KEHC fixture 없음 — `minjob-ingest snapshot --source KEHC`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "KEHC")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return kehc.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    no: str = "7964",
    ident: str = "27558",
    date_text: str = "26.08.04",
    link_class: str = "",
    extra: str = "",
) -> str:
    return (
        f"<tr><td><span>{no}</span></td><td><span>진행중</span></td><td>전도사</td>"
        f'<td class="ellipsis"><a class="{link_class}"'
        f' href="javascript:read_post({ident})">가</a>{extra}</td>'
        f"<td>홍길동</td><td>{date_text}</td><td>228</td></tr>"
    )


def _list_html(*rows: str) -> str:
    return f'<div class="sub_text02 board"><table>{"".join(rows)}</table></div>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notices_and_the_locked_posting_are_excluded(refs: tuple[PostingRef, ...]) -> None:
    """공지 2건 + 비밀글 1건을 빼고 47건이 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    titles = {ref.title for ref in refs}
    for notice in _NOTICE_TITLES:
        assert notice not in titles
    assert _LOCKED_ID not in {ref.external_id for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id는 `read_post({id})` 안의 값이다 — 표시번호(7964)와 다르다."""
    first = refs[0]
    assert first.external_id == "27558"
    assert first.title == "세종공주지방회 전의성결교회에서 함께 동역할 교육전도사님을 청빙합니다."
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == "https://kehc.org/home/recruit/read_post/27558"
    assert first.list_meta["display_no"] == "7964"
    assert first.list_meta["category"] == "전도사"


def test_two_digit_years_get_a_century(source: SourceConfig) -> None:
    """⚠️ 날짜가 `YY.MM.DD`다 — 세기를 붙이지 않으면 ISO가 아니라서 전 행이 거부된다(실측)."""
    old = kehc.parse_list(_list_html(_row(date_text="17.09.27")), source)
    assert old[0].posted_on == date(2017, 9, 27)


def test_pagination_steps_by_the_row_count_not_by_one(source: SourceConfig) -> None:
    """⚠️ 경로의 숫자는 페이지가 아니라 **행 offset**이다(실측: 0·50·100…7950).

    1씩 올리면 2페이지 요청이 1페이지의 두 번째 행부터 겹치게 나와 깊은 글에 못 닿는다.
    """
    assert kehc.list_request(source, 1).url.endswith("/page/0")
    assert kehc.list_request(source, 2).url.endswith("/page/50")
    assert kehc.list_request(source, 3).url.endswith("/page/100")


def test_a_locked_row_is_dropped_with_a_loud_failure_when_it_is_the_only_one(
    source: SourceConfig,
) -> None:
    """⚠️ 비밀글의 상세는 200 + 빈 페이지다(실측 27561) — 담으면 매 실행 상세 파싱이 실패한다.

    비공개 글이라 우회하지 않고(가드레일 #1) 목록에서 뺀다. 전부 그렇게 되면 실패로 알린다.
    """
    lock = '<img src="/design_skins/default/plugin_views/board/kehc_job/images/ico_lock.gif"/>'
    locked = _row(ident="27561", extra=lock)
    kept = kehc.parse_list(_list_html(locked, _row()), source)
    assert [ref.external_id for ref in kept] == ["27558"]
    with pytest.raises(ParseError, match="전부 걸러짐"):
        kehc.parse_list(_list_html(locked), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        kehc.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_data_content_and_not_the_embedded_list(
    refs: tuple[PostingRef, ...],
) -> None:
    """⚠️ 상세 페이지가 목록 50행을 또 품고 있다(`table.read_post_align`).

    또 본문에는 Cloudflare 이메일 난독화 링크가 있어, 첨부를 본문에서 찾으면 그것이
    첨부로 저장된다(실측 627자).
    """
    raw = kehc.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "· 교회명 : 전의성결교회" in raw.raw_text
    assert "94년의 역사를 이어온" in raw.raw_text
    assert "청빙 게시글에 대한 참고내용" not in raw.raw_text  # 아래 목록의 공지
    assert raw.attachments == ()
