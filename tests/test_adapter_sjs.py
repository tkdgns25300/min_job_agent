"""SJS 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source SJS` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import sjs
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "SJS"
#: 실측: tr 16 = 헤더 1 + 공지 1 + 공고 14.
_EXPECTED_POSTINGS: Final = 14
_NOTICE_TITLE: Final = "게시판 목적에 맞지 않는 게시글은 사전 동의 없이 삭제됩니다."

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="SJS fixture 없음 — `minjob-ingest snapshot --source SJS`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "SJS")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return sjs.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


def _row(*, no: str = "12686", ident: str = "50069", title: str = "가", row_class: str = "") -> str:
    return (
        f'<tr{row_class}><td class="index">{no}</td>'
        f'<td class="left board_tit"><a href="/ht_ml/w_04ed/4600.php?pagetype=&amp;bbs_idx={ident}'
        f'&amp;pageno=1&amp;pagekind=c&amp;bbsid=main4600">{title}</a></td>'
        f'<td class="date">2026-08-04</td><td class="hit">11</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="board_table">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건과 헤더를 빼고 공고 14건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다."""
    first = refs[0]
    assert first.external_id == "50069"
    assert first.title == "유아유치부를 담당할 교역자(교육전도사 혹은 교육목사)를 청빙합니다."
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == (
        "https://sjs.ac.kr/ht_ml/w_04ed/4600.php?bbs_idx=50069&pagekind=c&bbsid=main4600"
    )
    assert first.list_meta["display_no"] == "12686"


def test_external_id_survives_the_parameter_order(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 목록 href는 `?pagetype=&bbs_idx=…&pageno=1`이다 — `detail_pattern` 접두사와 순서가 다르다.

    URL 접두사로 id를 뽑으면 15행 전부가 실패하고, 마지막 조각을 쓰면 전부 같은 값이 된다.
    """
    ids = [ref.external_id for ref in refs]
    assert ids[:3] == ["50069", "50068", "50063"]
    assert all(ident.isdigit() for ident in ids)


def test_pinned_notice_has_two_independent_markers(source: SourceConfig) -> None:
    """공지는 `tr.notice`이면서 번호 자리에 `공지` 아이콘이 온다 — 하나가 바뀌어도 걸러진다."""
    by_class = _row(row_class=' class="notice"')
    by_icon = _row(no='<img alt="공지" src="../img/board_skin/notice_i.png"/>', ident="46382")
    assert len(sjs.parse_list(_list_html(by_class, by_icon, _row()), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        sjs.parse_list(_list_html(by_class, by_icon), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        sjs.parse_list(_list_html(_row().replace(">2026-08-04<", "><")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_view_content(refs: tuple[PostingRef, ...], detail_html: str) -> None:
    """본문은 `td.view_content`다(실측 433자 · 교단이 본문에 명시된다)."""
    raw = sjs.parse_detail(detail_html, refs[0])
    assert "*교단명: 예장통합" in raw.raw_text
    assert "모집부서" in raw.raw_text
    assert raw.image_urls == ()


def test_writer_mailto_is_not_an_attachment(refs: tuple[PostingRef, ...], detail_html: str) -> None:
    """⚠️ 작성자가 `mailto:` 링크이고 `링크` 행에는 빈 앵커가 있다(실측).

    첨부 범위를 상세 표 전체로 넓히면 메일 주소가 첨부 파일로 저장된다. `첨부파일` 행
    (`td.attached`)은 첨부가 없어도 빈 칸으로 존재하므로 그 안만 본다.
    """
    raw = sjs.parse_detail(detail_html, refs[0])
    assert raw.attachments == ()
    assert "mailto" not in raw.raw_text
