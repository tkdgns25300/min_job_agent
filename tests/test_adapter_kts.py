"""KTS 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source KTS` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.clock import board_today
from minjob_ingest.sources.adapters import base, kts
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "KTS"
#: 실측: tr 17 = 헤더 1 + 공지 1 + 공고 15.
_EXPECTED_POSTINGS: Final = 15
_NOTICE_TITLE: Final = (
    "타교단 및 학사생 모집, 사무간사, 관리집사, 임대등의 게시물은 삭제되오니 널리 양해 해 주십시오"
)
#: 연도 복원 검증용 기준일(실측 fixture를 받은 날).
_TODAY: Final = date(2026, 8, 4)

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="KTS fixture 없음 — `minjob-ingest snapshot --source KTS`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "KTS")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return kts.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *, no: str = "10976", ident: str = "31528", title: str = "가", posted: str = "08-03"
) -> str:
    return (
        f'<tr><td class="td_num2">{no}</td>'
        '<td class="td_subject"><div class="bo_tit">'
        f'<a href="https://www.kts.ac.kr/home/pinvit/{ident}">{title}</a></div></td>'
        '<td class="td_name sv_use"><span class="sv_guest">아무교회</span></td>'
        f'<td class="td_num">7</td><td class="td_datetime">{posted}</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<div class="tbl_head01"><table>{"".join(rows)}</table></div>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 15건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "31528"
    assert first.title == "충주양문교회에서 교역자를 청빙합니다."
    assert first.url == "https://www.kts.ac.kr/home/pinvit/31528"
    # 표시번호(끌어올림으로 다시 매겨지는 값)를 원장 키로 쓰면 안 된다.
    assert first.list_meta["display_no"] == "10976"
    assert first.list_meta["list_date"] == "15:58"
    assert first.posted_on == board_today()  # `15:58` = 오늘 쓴 글


@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        ("15:58", _TODAY),  # 오늘 글은 시각만 나온다
        ("08-03", date(2026, 8, 3)),  # 올해 안쪽
        ("09-26", date(2025, 9, 26)),  # 올해로 두면 미래 → 작년
        ("2022.09.23", date(2022, 9, 23)),  # 연도까지 있는 표기
    ],
)
def test_the_year_is_restored_from_a_yearless_cell(shown: str, expected: date) -> None:
    """⚠️ 목록에 연도가 없다 — 복원 규칙이 어긋나면 `--months` 컷오프가 통째로 틀린다."""
    assert base.gnuboard_list_date(shown, source_key="KTS", cell="c", today=_TODAY) == expected


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_notice_row_is_dropped_by_class_and_by_number(source: SourceConfig) -> None:
    """공지는 `tr.bo_notice`이고 번호 칸이 `공지`다 — 두 신호 중 하나만 남아도 걸러야 한다."""
    by_class = _row(no="10975", ident="31527").replace("<tr>", '<tr class="bo_notice">')
    by_number = _row(no="공지", ident="13415")
    assert len(kts.parse_list(_list_html(by_class, by_number, _row()), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        kts.parse_list(_list_html(by_class, by_number), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        kts.parse_list(_list_html(_row(posted="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_gnuboard_content_div(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `#bo_v_con`이다(실측 782자 · 첨부 없는 공고)."""
    raw = kts.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "충주양문교회에서 함께 사역하며" in raw.raw_text
    assert "제출서류" in raw.raw_text
    assert raw.attachments == ()
    assert raw.image_urls == ()


def test_listed_attachment_icon_must_show_up_in_the_detail(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 목록의 첨부 아이콘(실측 3행)은 상세 첨부 셀렉터를 검증하는 **독립 신호**다.

    이 교차 확인이 없으면 `#bo_v_file`이 바뀌어도 "본문 있으니 정상"으로 통과한다.
    """
    flagged = PostingRef(
        external_id=refs[0].external_id,
        url=refs[0].url,
        title=refs[0].title,
        posted_on=refs[0].posted_on,
        list_meta={"has_attachment": True},
    )
    with pytest.raises(ParseError, match="첨부 표시가 있는데"):
        kts.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), flagged)


def test_attachment_bearing_posting_is_measured(
    refs: tuple[PostingRef, ...],
) -> None:
    """첨부가 달린 실제 공고로 셀렉터를 고정한다(2026-08-05 실측 · `detail_file.html`).

    ⚠️ 표본 공고에 첨부가 없으면 셀렉터가 틀려도 "정상인데 첨부 0개"로 통과한다 —
    그래서 첨부 있는 공고를 따로 받아 여기서 못을 박는다.
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("detail_file.html 없음")
    marked = [ref for ref in refs if ref.list_meta.get("has_attachment")]
    assert marked, "목록에 첨부 표시된 공고가 없다 — 대조 신호가 사라졌다"
    raw = kts.parse_detail(path.read_text(encoding="utf-8"), marked[0])
    assert raw.attachments or raw.image_urls, "첨부·이미지를 하나도 못 찾았다"
