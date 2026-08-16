"""NAZARENE 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source NAZARENE` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import nazarene
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "NAZARENE"
#: 실측: li 15 = 공고 15 (헤더 행도 공지도 없다 · 잠긴 글도 1·2페이지에 없었다).
_EXPECTED_POSTINGS: Final = 15

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="NAZARENE fixture 없음 — `minjob-ingest snapshot --source NAZARENE`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "NAZARENE")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return nazarene.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    no: str = "112",
    ident: str = "124",
    title: str = "가",
    posted: str = "2026.07.06",
    mark: str = "",
) -> str:
    return (
        '<li class="d-md-table-row">'
        f'<div class="d-md-table-cell"><span class="sr-only">번호</span>{no}</div>'
        '<div class="d-md-table-cell"><div class="na-title"><div class="na-item">'
        f'{mark}<a class="na-subject" href="https://na.or.kr/ccall/{ident}">'
        f'<span class="na-icon na-hot"></span> {title}</a></div></div></div>'
        '<div class="d-md-table-cell"><span class="sr-only">등록자</span>'
        '<span class="sv_member">아무개</span></div>'
        f'<div class="d-md-table-cell"><span class="sr-only">등록일</span>{posted}</div>'
        '<div class="d-md-table-cell"><span class="sr-only">조회</span>7</div>'
        '<div class="clearfix"></div></li>'
    )


def _list_html(*rows: str) -> str:
    return f'<ul class="na-table d-md-table w-100">{"".join(rows)}</ul>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_all_rows_are_postings(refs: tuple[PostingRef, ...]) -> None:
    """이 게시판은 고정공지가 없다 — 15건 전부 공고다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "124"
    assert first.title == "우천교회에서 함께 동역 할 교역자를 청빙합니다"
    assert first.posted_on == date(2026, 7, 6)
    assert first.url == "https://na.or.kr/ccall/124"
    # 표시번호(112)와 원장 키(124)는 다르다 — 표시번호는 게시판이 다시 매긴다.
    assert first.list_meta["display_no"] == "112"


def test_screen_reader_labels_are_not_part_of_the_values(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 칸마다 `span.sr-only` 라벨이 숨어 있다 — 걷어내지 않으면 `"등록일 2026.07.06"`이 된다."""
    assert refs[0].list_meta["list_date"] == "2026.07.06"
    assert refs[0].list_meta["author"] == "pissmh"
    assert refs[0].list_meta["views"] == 728


def test_page_two_links_drop_the_page_parameter(source: SourceConfig) -> None:
    """⚠️ 2페이지 상세 href에 `?page=2`가 붙는다 — 그대로 쓰면 같은 글의 URL이 페이지마다 갈린다."""
    html = (_FIXTURES / "list_page2.html").read_text(encoding="utf-8")
    assert "/ccall/108?page=2" in html  # fixture가 실제로 그 함정을 담고 있는지 먼저 확인
    second = nazarene.parse_list(html, source)
    assert second[0].external_id == "108"
    assert second[0].url == "https://na.or.kr/ccall/108"


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_member_only_rows_are_skipped_not_unlocked(source: SourceConfig) -> None:
    """⚠️ 잠긴 글(`.fa-lock`)은 **건너뛴다** — 우회하지 않는다.

    실측 1·2페이지에는 잠긴 글이 없어 합성 HTML로 검증한다.
    """
    locked = _row(no="111", ident="123", mark='<i class="fa fa-lock"></i>')
    kept = nazarene.parse_list(_list_html(locked, _row()), source)
    assert [ref.external_id for ref in kept] == ["124"]
    with pytest.raises(ParseError, match="전부 걸러짐"):
        nazarene.parse_list(_list_html(locked), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        nazarene.parse_list(_list_html(_row(posted="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_gnuboard_content_div(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `#bo_v_con`이다(실측 352자). "관련자료"의 다른 공고 링크가 첨부로 새면 안 된다."""
    raw = nazarene.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "우천교회에서 하나님과 영혼들을 섬기며" in raw.raw_text
    assert "1.청빙분야" in raw.raw_text
    assert raw.attachments == ()
    assert raw.image_urls == ()


# ── 첨부 ─────────────────────────────────────────────────────────


def _flagged(external_id: str) -> PostingRef:
    """목록에서 첨부 표시가 있던 공고처럼 만든 ref. 상세와 대조하는 신호를 살려 둔다."""
    return PostingRef(
        external_id=external_id,
        url=f"https://na.or.kr/ccall/{external_id}",
        title="첨부 있는 공고",
        list_meta={"has_attachment": True},
    )


def test_the_list_marks_which_postings_have_files(source: SourceConfig) -> None:
    """✅ 목록에 첨부 표시(`span.na-ticon.na-file`)가 있다 — 상세 셀렉터를 대조하는 독립 신호다.

    실측: 1페이지 0건 · 2페이지 1건(97). 신호가 사라지면 첨부 유실을 아무도 못 잡는다.
    """
    page2 = nazarene.parse_list((_FIXTURES / "list_page2.html").read_text(encoding="utf-8"), source)
    flagged = [ref.external_id for ref in page2 if ref.list_meta.get("has_attachment")]
    assert flagged == ["97"]


def test_non_image_attachment_comes_from_the_related_section() -> None:
    """⚠️ `#bo_v_file`은 이 스킨에 없다 — 첨부는 "관련자료"(`#bo_v_data`)에 있다(78 실측).

    그것만 보던 예전 코드는 3.5M PDF를 통째로 잃었다. 파일명은 앵커의 첫 텍스트 노드뿐이라
    `get_text`로 읽으면 `…pdf 파일크기 (3.5M) 124 회 다운로드`가 된다.
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("detail_file.html 없음 — `snapshot --url https://na.or.kr/ccall/78`")
    raw = nazarene.parse_detail(path.read_text(encoding="utf-8"), _flagged("78"))
    assert len(raw.attachments) == 1
    attachment = raw.attachments[0]
    # ⚠️ 파일명에 목사 실명이 들어 있어 그대로 단언하지 않는다(공개 리포 · fixture 마스킹 규칙).
    #    확인할 것은 **앵커 첫 텍스트 노드만 읽었나**이므로 앞뒤와 길이로 충분하다.
    assert attachment.name.startswith("예산산성교회 실행제직회 회의록")
    assert attachment.name.endswith("청빙.pdf"), "`파일크기 (3.5M) …` 꼬리가 붙으면 안 된다"
    assert attachment.url == "https://na.or.kr/bbs/download.php?bo_table=ccall&wr_id=78&no=0"
    assert attachment.is_image is False


def test_image_attachment_keeps_the_full_size_url() -> None:
    """⚠️ 이미지 첨부(`#bo_v_img`)의 본문 `<img>`는 **600px 썸네일**이다(97 실측).

    원본은 `a.view_image`의 href(`view_image.php?…&fn=<파일명>.jpg`)뿐이라 첨부로 담아야
    포스터형 공고를 읽을 수 있다. 파일명은 쿼리에서 복원되고 `is_image`가 참이어야 한다.
    """
    path = _FIXTURES / "detail_image.html"
    if not path.exists():
        pytest.skip("detail_image.html 없음 — `snapshot --url https://na.or.kr/ccall/97`")
    raw = nazarene.parse_detail(path.read_text(encoding="utf-8"), _flagged("97"))
    assert len(raw.attachments) == 1
    attachment = raw.attachments[0]
    assert attachment.name.endswith(".jpg")
    assert "view_image.php" in attachment.url and "fn=" in attachment.url
    assert attachment.is_image is True
    # 썸네일은 증거로 남지만 그것만으로는 내용을 읽을 수 없다.
    assert any("thumb-" in url for url in raw.image_urls)


def test_a_flagged_posting_without_attachments_is_an_error() -> None:
    """목록이 "첨부 있음"이라 했는데 못 찾으면 셀렉터가 빗나간 것이다 — 조용히 통과시키지 않는다."""
    with pytest.raises(ParseError, match="첨부 표시가 있는데"):
        nazarene.parse_detail(
            (_FIXTURES / "detail.html").read_text(encoding="utf-8"), _flagged("124")
        )


def test_related_postings_are_not_attachments(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 첨부와 이전글/다음글이 **같은 표**에 있다 — 클래스로 갈리지 않으면 링크가 첨부로 샌다."""
    mixed = (
        '<div id="bo_v_con">본문</div>'
        '<section id="bo_v_data"><ul><li><a href="https://na.or.kr/ccall/123">다음글</a></li>'
        '<li><a class="view_file_download" href="/bbs/download.php?no=0">양식.hwp'
        '<span class="sr-only">파일크기</span> (1.2M)</a></li></ul></section>'
    )
    raw = nazarene.parse_detail(mixed, refs[0])
    assert [a.name for a in raw.attachments] == ["양식.hwp"]
