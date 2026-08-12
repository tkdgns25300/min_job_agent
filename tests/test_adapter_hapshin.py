"""HAPSHIN 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source HAPSHIN` 으로 받는다. 첨부가 달린 상세 `detail_file.html`은
`--url "https://hapdong.ac.kr/bbs/board.php?bo_table=e03&wr_id=15254" --name detail_file.html`
으로 받는다(2026-08-05 실측).

💡 **첨부 달린 공고를 찾는 방법**(목록에 첨부 표시가 없다): 그누보드의 `wr_file` 검색이
알려준다 — `…/board.php?bo_table=e03&sop=and&sfl=wr_file&stx=1` 이 첨부 1개 이상인 글만
돌려준다(2026-08-05 실측 15건). 상세를 무작정 뒤지지 않아도 된다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.clock import board_today
from minjob_ingest.sources.adapters import hapshin
from minjob_ingest.sources.adapters.base import ParseError, PostingRef

# 그누보드 계열 공용 헬퍼는 지금 kts.py에 있다(base.py 공용화 후보).
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "HAPSHIN"
#: 실측: tr 17 = 헤더 1 + 공지 1 + 공고 15.
_EXPECTED_POSTINGS: Final = 15
_NOTICE_TITLE_PART: Final = "홈페이지 아이디 및 비밀번호 분실"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="HAPSHIN fixture 없음 — `minjob-ingest snapshot --source HAPSHIN`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "HAPSHIN")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return hapshin.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


def _row(
    *, no: str = "10972", ident: str = "15264", title: str = "가", posted: str = "08.02"
) -> str:
    return (
        f'<tr><td class="text-center font-11"><span class="en">{no}</span></td>'
        '<td class="list-subject">'
        f'<a href="https://hapdong.ac.kr:443/bbs/board.php?bo_table=e03&amp;wr_id={ident}">'
        f'<span class="wr-icon wr-new"></span> {title}</a></td>'
        '<td><b><span class="sv_member">아무개</span></b></td>'
        f'<td class="text-center en font-11">{posted}</td>'
        '<td class="text-center en font-11">7</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="table div-table list-pc bg-white">{"".join(rows)}</table>'


#: 공지는 제목 칸에 `notice` 클래스가 붙고 번호 대신 아이콘이 들어간다(실측).
_NOTICE_ROW: Final = (
    _row(no='<span class="wr-icon wr-notice"></span>', ident="9486")
    .replace('class="list-subject"', 'class="list-subject notice"')
    .replace('<span class="en">', "")
    .replace("</span></td>", "</td>", 1)
)


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 15건만 남는다(실측 · `fetch_note`의 "공지 6개"는 옛 값)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert all(_NOTICE_TITLE_PART not in ref.title for ref in refs)


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == "15264"
    assert (
        first.title
        == "안양일심교회(예장고신, 경기중부노회)에서 파트로 사역할 여전도사님을 모십니다."
    )
    # 목록 href에는 `:443`이 붙어 있다 — 저장되는 URL은 정규형이어야 한다.
    assert first.url == "https://hapdong.ac.kr/bbs/board.php?bo_table=e03&wr_id=15264"
    assert first.list_meta["display_no"] == "10972"
    assert first.list_meta["author"] == "찬솔아빠"


def test_dotted_month_day_is_restored_to_a_real_date(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 이 게시판의 연도 없는 날짜는 **점 구분**(`08.02`)이다 — 하이픈만 받으면 통째로 실패한다.

    오늘 글은 시각만(`15:36`) 나온다.
    """
    assert refs[0].list_meta["list_date"] == "15:36"
    assert refs[0].posted_on == board_today()
    assert refs[2].list_meta["list_date"] == "08.02"
    assert refs[2].posted_on == date(2026, 8, 2)


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_notice_is_marked_on_the_subject_cell(source: SourceConfig) -> None:
    """⚠️ 공지 표시가 `tr.bo_notice`가 아니라 **제목 칸의 `notice` 클래스**다(실측)."""
    assert len(hapshin.parse_list(_list_html(_NOTICE_ROW, _row(ident="15263")), source)) == 1
    with pytest.raises(ParseError, match="전부 걸러짐"):
        hapshin.parse_list(_list_html(_NOTICE_ROW), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        hapshin.parse_list(_list_html(_row(posted="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_view_content(refs: tuple[PostingRef, ...], detail_html: str) -> None:
    """본문은 `div.view-content`다(실측 575자 · HWP에서 붙인 `<span>` 더미)."""
    raw = hapshin.parse_detail(detail_html, refs[0])
    assert "파트타임 여전도사 또는 기관 사역자" in raw.raw_text
    assert "부임시기 2027년 1월" in raw.raw_text
    assert raw.attachments == ()


def test_missing_no_attach_marker_demands_attachments(
    refs: tuple[PostingRef, ...], detail_html: str
) -> None:
    """⚠️ 목록에 첨부 표시가 없어 머리 패널의 `no-attach`가 **유일한** 교차 신호다.

    그 표시가 없는데 첨부가 0건이면 다운로드 링크 셀렉터가 빗나간 것이다.
    """
    with_attachment = detail_html.replace(" no-attach", "", 1)
    with pytest.raises(ParseError, match="첨부가 있다고 표시됐는데"):
        hapshin.parse_detail(with_attachment, refs[0])


def test_attachment_bearing_posting_is_measured(refs: tuple[PostingRef, ...]) -> None:
    """첨부가 달린 실제 공고로 셀렉터를 고정한다(2026-08-05 실측 · wr_id 15254).

    ⚠️ **링크 텍스트를 그대로 파일명으로 쓰면 안 된다** — 다운로드 횟수·크기·등록일이 섞여
    `4 이력서_자기소개서.hwp (93.5K) 7일전`이 되고 확장자가 끝에 오지 않아 `is_image`가 항상
    거짓이 된다. URL(`download.php?…&no=0`)에는 파일명이 없어 다른 출처가 없다.
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("detail_file.html 없음 — 모듈 docstring의 `--url`로 받는다")
    ref = next(found for found in refs if found.external_id == "15254")
    raw = hapshin.parse_detail(path.read_text(encoding="utf-8"), ref)
    # `:443`은 게시판이 href에 그대로 내려주는 값이다(목록 URL과 달리 정규화하지 않는다).
    assert [(a.name, a.url) for a in raw.attachments] == [
        (
            "이력서_자기소개서.hwp",
            "https://hapdong.ac.kr:443/bbs/download.php?bo_table=e03&wr_id=15254&no=0",
        )
    ]


def test_the_image_attachment_box_is_read_even_though_it_sits_outside_the_body() -> None:
    """⚠️ 이미지 첨부는 본문의 **형제** 상자(`div.view-img`)에 온다 — KTS가 이걸 빼먹어 잃었다.

    실측 4건은 전부 비어 있었지만 스킨은 그 상자를 항상 렌더한다(`a.view_image` 팝업 핸들러도
    함께 내려온다) → 상자가 채워지는 날 조용히 잃지 않도록 여기서 못을 박는다.
    """
    ref = PostingRef(
        external_id="1",
        url="https://hapdong.ac.kr/bbs/board.php?bo_table=e03&wr_id=1",
        title="가",
    )
    html = (
        '<div class="view-wrap"><div class="panel panel-default view-head"></div>'
        '<div class="view-img">'
        '<a href="/bbs/view_image.php?fn=poster.jpg" class="view_image">'
        '<img src="/data/file/e03/poster.jpg"></a></div>'
        '<div class="view-content"><p>본문</p></div></div>'
    )
    raw = hapshin.parse_detail(html, ref)
    assert [(a.name, a.is_image) for a in raw.attachments] == [("poster.jpg", True)]
    assert raw.image_urls == ("https://hapdong.ac.kr/data/file/e03/poster.jpg",)


def test_a_related_link_is_not_a_missing_attachment(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ **전량 수집에서 정상 공고 7건을 버렸다**(2026-08-05).

    그누보드는 첨부가 없을 때 머리 패널에 `no-attach`를 붙이는데, 그 판정은 **파일과 링크를
    함께** 본다. 작성자가 파일 없이 URL만 붙이면(교회 카페·홈페이지가 흔하다) `no-attach`가
    떨어지고, 우리는 그것을 "파일이 있다"로 읽어 없는 파일을 찾다가 공고를 실패시켰다.
    """
    head = (
        '<div class="panel panel-default view-head">'
        '<div class="list-group"><a class="list-group-item" '
        'href="https://hapdong.ac.kr/bbs/link.php?bo_table=e03&amp;wr_id=15242&amp;no=1">'
        '<span class="label view-cnt">49</span><i class="fa fa-link"></i>'
        " https://cafe.daum.net/peace5851</a></div></div>"
    )
    body = '<div class="view-content">' + "가" * 300 + "</div>"
    raw = hapshin.parse_detail(f'<div class="view-wrap">{head}{body}</div>', refs[0])
    assert raw.attachments == ()
    assert raw.raw_text  # 공고는 그대로 수집된다


def test_a_missing_attachment_is_still_an_error(refs: tuple[PostingRef, ...]) -> None:
    """반대편 — 링크도 파일도 없는데 `no-attach`가 없으면 셀렉터가 빗나간 것이다."""
    head = '<div class="panel panel-default view-head"><div class="panel-heading"></div></div>'
    body = '<div class="view-content">' + "가" * 300 + "</div>"
    with pytest.raises(ParseError, match="첨부가 있다고 표시됐는데"):
        hapshin.parse_detail(f'<div class="view-wrap">{head}{body}</div>', refs[0])
