"""KOSIN_TH 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source KOSIN_TH` 로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import kosin_th
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "KOSIN_TH"
#: 실측: tr 17 = 헤더 1 + 공지 1 + 공고 15.
_EXPECTED_POSTINGS: Final = 15
_NOTICE_TITLE: Final = "사역자 청빙 공고 게시 요청 방법"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="KOSIN_TH fixture 없음 — `minjob-ingest snapshot --source KOSIN_TH`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "KOSIN_TH")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return kosin_th.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    no: str = "431",
    ident: str = "304339",
    title: str = "가",
    date_text: str = "2026-06-05",
    row_class: str = "child_1",
) -> str:
    return (
        f'<tr class="{row_class}"><td class="f-num num"><p>{no}</p></td>'
        f'<td class="f-tit subject"><p><a href="/th/index.php?pCode=MN6000030&amp;mode=view'
        f'&amp;idx={ident}"><span>{title}</span></a></p></td>'
        f'<td class="f-nm writer"><p>신학과</p></td>'
        f'<td class="f-date date"><p>{date_text}</p></td>'
        f'<td class="f-hits read"><p>161</p></td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="isDataList board-list-table">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 15건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·정규 URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "304339"
    assert first.title == "영양서부교회 사역자 청빙공고"
    assert first.posted_on == date(2026, 6, 5)
    assert first.url == (
        "https://best.kosin.ac.kr/th/index.php?pCode=MN6000030&mode=view&idx=304339"
    )
    # 표시번호(431)와 원장 키(idx)는 다르다 — 표시번호는 게시판이 다시 매긴다.
    assert first.list_meta["display_no"] == "431"


def test_other_denominations_and_non_calls_are_kept(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 이 게시판에는 타 교단 공고와 청빙이 아닌 글이 섞여 있다 — **걸러내지 않는다**.

    제목으로 자르면 진짜 청빙을 조용히 잃는다. 교단은 공고별 판정(SPEC §5.3), 청빙 여부는 게이트1.
    """
    titles = " ".join(ref.title for ref in refs)
    assert "예장통합" in titles  # 게시판 힌트(GOSIN)와 다른 교단이 그대로 남아 있다
    assert "국비지원" in titles  # 청빙이 아닌 취업 홍보도 남는다


def test_notice_row_is_detected_by_class_and_by_number(source: SourceConfig) -> None:
    """공지 신호가 둘이다(`tr.isnotice` · 번호 칸이 `공지` 이미지라 숫자가 없다).

    ⚠️ 행을 **둘** 둔다 — 데이터행 하나뿐인 페이지는 "끝을 넘겼다"로 보기 때문이다
    (`_END_OF_LIST_ROWS`). 실제 페이지는 12~16행이라 이쪽이 실측에 가깝다.
    """
    by_class = _row(row_class="isnotice")
    by_icon = _row(no='<img alt="공지" src="/_Img/Board/default/icon_notice.png"/>')
    for notice in (by_class, by_icon):
        with pytest.raises(ParseError, match="전부 걸러짐"):
            kosin_th.parse_list(_list_html(notice, notice), source)


def test_untitled_row_is_skipped_instead_of_failing_the_board(source: SourceConfig) -> None:
    """⚠️ 실측(2026-08-25): 18페이지 176번(2019-11-18)은 **게시판에서도 제목이 빈칸**이다.

    `PostingRef`가 빈 제목을 거부하므로, 건너뛰지 않으면 그 한 행이 게시판 전체를 실패시킨다.
    """
    refs = kosin_th.parse_list(_list_html(_row(), _row(no="176", ident="176", title="")), source)
    assert [ref.external_id for ref in refs] == ["304339"]


def test_all_rows_untitled_is_still_an_error(source: SourceConfig) -> None:
    """⚠️ 전부 빈 제목이면 셀렉터가 깨진 것이다 — 건너뛰기가 그 신호를 삼키면 안 된다."""
    with pytest.raises(ParseError, match="전부 걸러짐"):
        kosin_th.parse_list(_list_html(_row(title=""), _row(ident="9", title="")), source)


def test_page_past_the_end_is_the_end_not_a_failure(source: SourceConfig) -> None:
    """⚠️ 실측: 공고는 29페이지에서 끝나는데 30페이지 이후에도 **고정공지가 계속 렌더된다**.

    그 한 행을 "전부 걸러짐"으로 보면 끝까지 훑는 백필이 늘 실패로 끝난다. 빈 결과를 주면
    수집 루프가 `within_cutoff == 0`으로 멈춘다.
    """
    notice_only = _row(row_class="isnotice", title=_NOTICE_TITLE)
    assert kosin_th.parse_list(_list_html(notice_only), source) == ()


def test_full_page_of_notices_is_still_an_error(source: SourceConfig) -> None:
    """⚠️ 끝 판정을 **데이터행 1개**로 좁히는 이유 — 꽉 찬 페이지가 전부 걸러지면 셀렉터 깨짐이다.

    YTUS에서 `td.num` 클래스가 바뀌자 공고 18건이 전부 공지로 판정돼 조용히 0건이 됐다.
    """
    with pytest.raises(ParseError, match="전부 걸러짐"):
        kosin_th.parse_list(_list_html(*(_row(row_class="isnotice"),) * 2), source)


def test_row_without_a_title_link_still_fails(source: SourceConfig) -> None:
    """⚠️ 링크가 **아예 없는** 행은 건너뛰지 않는다 — 빈 제목과 달리 셀렉터 깨짐이다."""
    linkless = (
        '<tr class="child_1"><td class="f-num num"><p>431</p></td>'
        '<td class="f-tit subject"><p>제목만 있고 링크가 없다</p></td>'
        '<td class="f-date date"><p>2026-06-05</p></td></tr>'
    )
    with pytest.raises(ParseError, match="상세 링크가 없음"):
        kosin_th.parse_list(_list_html(linkless), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        kosin_th.parse_list(_list_html(_row(date_text="")), source)


def test_page_two_inserts_pg_into_the_detail_href(source: SourceConfig) -> None:
    """⚠️ 2페이지부터 상세 href가 `?pCode=…&pg=2&mode=view&idx=…`가 된다(실측).

    config `detail_pattern` 접두사로 자르면 **2페이지 이후 전 행이 탈락**한다 — 1페이지만 보면
    절대 드러나지 않는다. 파라미터 이름으로 뽑으니 `pg`가 끼어도 같은 id가 나와야 한다.
    """
    paged = _row().replace(
        "pCode=MN6000030&amp;mode=view", "pCode=MN6000030&amp;pg=2&amp;mode=view"
    )
    refs = kosin_th.parse_list(_list_html(paged), source)
    assert refs[0].external_id == "304339"
    assert "pg=2" not in refs[0].url  # 정규형에는 페이지 상태가 남지 않는다


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_posting_text_only(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `div.board-view-contents`다(실측 421자) — 사이트 내비게이션이 섞이면 안 된다."""
    raw = kosin_th.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "*교단명: 예장합동" in raw.raw_text
    assert "▶모집부서: 유초등부 담당, 행정 보조" in raw.raw_text
    assert "커뮤니티" not in raw.raw_text


def test_attached_poster_arrives_as_an_image_not_a_download(
    refs: tuple[PostingRef, ...],
) -> None:
    """⚠️ 첨부에 다운로드 링크가 없다 — `mode=fv` 이미지로만 렌더된다(실측 1장).

    앵커만 찾는 코드로는 포스터형 공고의 내용을 통째로 잃는다. 본문의 교회 홈페이지 링크가
    첨부로 오인되지도 않아야 하고, 첨부 컨테이너가 바뀌면(목록 아이콘과 어긋나면) 실패해야 한다.
    """
    detail = (_FIXTURES / "detail.html").read_text(encoding="utf-8")
    raw = kosin_th.parse_detail(detail, refs[0])
    assert len(raw.image_urls) == 1
    assert "mode=fv&idx=304339" in raw.image_urls[0]
    assert raw.attachments == ()
    assert all("yysb.co.kr" not in url for url in raw.image_urls)
    with pytest.raises(ParseError, match="첨부 표시가 있는데"):
        kosin_th.parse_detail(detail.replace("board-view-files", "renamed-files"), refs[0])
