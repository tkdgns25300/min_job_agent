"""SUNGKYUL 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source SUNGKYUL` 로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import sungkyul
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "SUNGKYUL"
#: 실측: tr 18 = 헤더 1 + 공지 2 + 공고 15.
_EXPECTED_POSTINGS: Final = 15
_NOTICE_TITLES: Final = ("일반 광고성의 글은 삭제합니다", "게시판 입력 오류 관련 공지")

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="SUNGKYUL fixture 없음 — `minjob-ingest snapshot --source SUNGKYUL`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "SUNGKYUL")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return sungkyul.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


def _row(
    *,
    no: str = "4854",
    uid: str = "8185",
    articlenum: str = "4853",
    date_text: str = "2026.08.02",
) -> str:
    href = (
        f"/NOS-Board/bbs.php?uid={uid}&key=&keyfield=&idx=com9"
        f"&retype=view&page=1&articlenum={articlenum}"
    )
    return (
        f'<tr><td class="nope">{no}</td>'
        f'<td class="board-tit"><a href="{href}">가</a></td>'
        f"<td>홍길동</td><td>{date_text}</td>"
        f'<td class="nope">1</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="boardlist01">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notices_are_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 2건은 제외하고 공고 15건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    titles = {ref.title for ref in refs}
    for notice in _NOTICE_TITLES:
        assert notice not in titles


def test_the_id_is_the_uid_not_the_display_number_or_articlenum(
    refs: tuple[PostingRef, ...],
) -> None:
    """⚠️ 표시번호 4854 · `articlenum` 4853 · `uid` 8185가 **셋 다 다르다**(실측).

    원장 키가 되어야 하는 것은 DB 고유값인 `uid`다.
    """
    first = refs[0]
    assert first.external_id == "8185"
    assert first.list_meta["display_no"] == "4854"
    assert first.title == "구성교회에서 파트 사역자를 찾습니다."
    assert first.posted_on == date(2026, 8, 2)


def test_the_detail_url_uses_the_www_host_even_though_the_list_is_on_the_apex(
    refs: tuple[PostingRef, ...],
) -> None:
    """⚠️ 목록 href는 상대 경로라 그대로 절대화하면 apex가 붙어 호스트가 갈린다."""
    assert refs[0].url == (
        "https://www.sungkyul.org/NOS-Board/bbs.php?uid=8185&idx=com9&retype=view"
    )
    assert all(ref.url.startswith("https://www.sungkyul.org/") for ref in refs)


def test_a_notice_marked_only_in_the_href_is_still_excluded(source: SourceConfig) -> None:
    """공지 신호가 둘이다(번호 칸 + `articlenum=공지`) — 하나가 바뀌어도 걸러야 한다."""
    hidden_notice = _row(no="631", uid="631", articlenum="공지")
    assert len(sungkyul.parse_list(_list_html(hidden_notice, _row()), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        sungkyul.parse_list(_list_html(hidden_notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        sungkyul.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_keeps_the_full_title_that_the_list_truncates(
    refs: tuple[PostingRef, ...], detail_html: str
) -> None:
    """⚠️ 목록 제목은 26자에서 잘린다(실측 `…청빙합니...`) — 전체 제목은 상세에만 있다.

    그래서 본문 앞에 제목을 붙여 보존한다. 같은 칸에 든 게시일은 섞이지 않아야 한다.
    """
    raw = sungkyul.parse_detail(detail_html, refs[0])
    assert raw.raw_text.startswith("구성교회에서 파트 사역자를 찾습니다.")
    assert "2026-08-02" not in raw.raw_text.splitlines()[0]
    assert "구성교회는 2017년에 설립한 교회로" in raw.raw_text


def test_prev_next_links_are_not_attachments(
    refs: tuple[PostingRef, ...], detail_html: str
) -> None:
    """⚠️ 이전글·다음글이 첨부와 **같은 표**에 있다 — 범위를 넓히면 그 링크가 첨부가 된다."""
    raw = sungkyul.parse_detail(detail_html, refs[0])
    assert raw.attachments == ()
    assert raw.image_urls == ()
