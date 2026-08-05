"""STS 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source STS` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import sts
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "STS"
#: 실측: tr 17 = 헤더 1(th) + 여백행 1(`tr.jTh2`) + 공고 15. 고정공지는 지금 0건이다.
_EXPECTED_POSTINGS: Final = 15

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="STS fixture 없음 — `minjob-ingest snapshot --source STS`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "STS")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return sts.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(*, no: str = "111", ident: str = "7319", title: str = "가", tail: str = "") -> str:
    return (
        f'<tr><td class="jNum">{no}</td>'
        f'<td class="jSubject"><div><a href="/main/sub.html?Mode=view&amp;boardID=www38'
        f'&amp;num={ident}&amp;page=&amp;keyfield=&amp;key=&amp;bCate=">{title}</a></div>{tail}</td>'
        f'<td class="jWriter">eyJjdCI6Iuydtes=</td>'
        f'<td class="jDate">2026.06.22</td><td class="jView">100</td></tr>'
    )


#: 헤더 바로 아래 여백행. `td`가 있어 데이터 행으로 보이지만 링크도 표시번호도 없다(실측).
_SPACER_ROW: Final = '<tr class="jTh2"><td colspan="4"></td></tr>'


def _list_html(*rows: str) -> str:
    return f'<table class="jmboardskin1">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_postings_are_counted_without_the_spacer_row(refs: tuple[PostingRef, ...]) -> None:
    """여백행·헤더를 뺀 15건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert all(ref.title for ref in refs)


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "7319"
    assert first.title == "서울신광교회 파트&전임 동역자 모집 공고"
    assert first.posted_on == date(2026, 6, 22)
    assert first.url == "https://sts.ac.kr/main/sub.html?Mode=view&boardID=www38&num=7319"


def test_title_excludes_the_mobile_paragraph(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ `td.jSubject`에는 모바일용 `<p>`(ANYSECURE 암호문·등록일·조회수)가 함께 있다.

    칸 전체를 제목으로 읽으면 `"…모집 공고 eyJjdCI6… | 2026.06.22 | 조회 100"`이 되어
    구조화가 엉뚱한 문자열을 교회명으로 본다.
    """
    for ref in refs:
        assert "eyJjdCI6" not in ref.title
        assert "조회" not in ref.title
    assert refs[0].list_meta["views"] == 100


def test_display_number_is_not_the_external_id(refs: tuple[PostingRef, ...]) -> None:
    """표시번호(111)와 원장 키(num=7319)는 다르다 — 표시번호는 게시판이 다시 매긴다."""
    assert refs[0].list_meta["display_no"] == "111"
    assert refs[0].external_id != refs[0].list_meta["display_no"]


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_spacer_row_is_skipped_not_crashed(source: SourceConfig) -> None:
    """여백행은 건너뛰고, 그것만 있으면 **에러**다(조용한 0건 금지)."""
    assert len(sts.parse_list(_list_html(_SPACER_ROW, _row()), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        sts.parse_list(_list_html(_SPACER_ROW), source)


def test_pinned_notice_row_is_excluded(source: SourceConfig) -> None:
    """고정공지는 표시번호 자리에 `공지`가 들어간다 — 숫자가 아니면 공고가 아니다."""
    notice = _row(no="공지", ident="7000", title="게시판 이용안내")
    refs = sts.parse_list(_list_html(notice, _row()), source)
    assert [ref.external_id for ref in refs] == ["7319"]


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        sts.parse_list(_list_html(_row().replace(">2026.06.22<", "><")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_view_content(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `div.mdView_cont`다 — 실측 688자, 이미지·첨부 없는 순수 텍스트 공고."""
    raw = sts.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "서울신광교회" in raw.raw_text
    assert "모집분야" in raw.raw_text
    assert raw.image_urls == ()
    assert raw.attachments == ()


def test_skin_footer_is_not_mistaken_for_content(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 상세 페이지 푸터에 SNS 아이콘 이미지와 PREV/NEXT 링크가 있다.

    본문 범위를 넓히면 그 아이콘이 `image_urls`로, 이동 링크가 첨부로 저장된다.
    """
    raw = sts.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert all("icon_sns" not in url for url in raw.image_urls)
    assert "SNS내보내기" not in raw.raw_text
