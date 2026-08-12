"""BU 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source BU` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import bu
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "BU"
#: 실측: tr 15 = 헤더 1 + 공지 4 + 공고 10.
_EXPECTED_POSTINGS: Final = 10
#: ⚠️ `soft_200` — 없는 상세도 HTTP 200으로 이 쉘을 준다(실측본을 줄인 것).
_ERROR_SHELL: Final = (
    "<!doctype html><html><head><title>알림메세지</title></head><body>"
    '<div class="message_area"><div class="message_box"><h1>Message</h1>'
    "<h2>알립니다.</h2><p>원하시는 페이지를 찾을 수가 없습니다.</p>"
    "</div></div></body></html>"
)

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="BU fixture 없음 — `minjob-ingest snapshot --source BU`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "BU")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return bu.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(*, no: str = "2459", ident: str = "58332", cls: str = "", inner: str = "") -> str:
    return (
        f'<tr{cls}><td class="_artclTdNum">{no}</td>'
        f'<td class="_artclTdTitle">'
        f'<a href="/bbs/graduateschool/1110/{ident}/artclView.do">'
        f"<span>[경기 부천시] 우리비전교회</span>{inner}</a></td>"
        f'<td class="_artclTdWriter">교목실</td>'
        f'<td class="_artclTdRdate">2026.08.04</td>'
        f'<td class="_artclTdAccess">200</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="artclTable">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notices_are_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 공지 4건은 **모든 페이지에 반복**된다 — 걸러내지 않으면 페이지마다 재수집된다."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert all("일반공지" not in ref.title for ref in refs)


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id(artclNo)·제목·게시일·정규 URL이 실측과 같아야 한다."""
    first = refs[0]
    assert first.external_id == "58332"
    assert first.title == "[경기 부천시] 우리비전교회"
    assert first.posted_on == date(2026, 8, 4)
    assert first.url == "https://community.bu.ac.kr/bbs/graduateschool/1110/58332/artclView.do"


def test_the_new_badge_is_not_part_of_the_title(source: SourceConfig) -> None:
    """⚠️ 제목 앵커 안에 "새글" 배지가 들어 있다.

    떼지 않으면 며칠 뒤 배지가 사라질 때 **같은 글의 제목이 달라져** 원장 경보가 헛울린다.
    """
    refs = bu.parse_list(_list_html(_row(inner='<span class="newArtcl">새글</span>')), source)
    assert refs[0].title == "[경기 부천시] 우리비전교회"


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_notice_row_class_and_non_numeric_number_both_filter(source: SourceConfig) -> None:
    """공지는 `tr.headline`이고 표시번호 칸에 "일반공지"가 온다 — 두 신호를 독립적으로 본다."""
    for notice in (_row(cls=' class="headline"'), _row(no="일반공지")):
        with pytest.raises(ParseError, match="전부 걸러짐"):
            bu.parse_list(_list_html(notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        bu.parse_list(_list_html(_row().replace(">2026.08.04<", "><")), source)


def test_the_soft_200_error_shell_is_not_a_posting(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 잘못된 상세 경로가 **HTTP 200 + 에러 쉘**로 온다(`/bbs` 프리픽스를 빼면 재현).

    상태코드로 성공을 판정하면 빈 레코드가 저장된다 — 본문 셀렉터가 그 판정을 겸한다.
    """
    with pytest.raises(ParseError, match="상세 본문"):
        bu.parse_detail(_ERROR_SHELL, refs[0])


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_article_div(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `div.artclView` 한 곳이다(실측 663자) — 양식형 공고라 라벨이 붙어 있다."""
    raw = bu.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "교회명 : 우리비전교회" in raw.raw_text
    assert "교단명 : 예장 백석" in raw.raw_text
    assert raw.image_urls == ()


def test_links_inside_the_body_are_not_attachments(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 본문에 이메일·홈페이지 링크가 그대로 있다(`https://hwapyungsong@naver.com/` 실측).

    첨부 컨테이너를 본문으로 잡으면 그것들이 첨부로 저장돼 구조화가 엉뚱한 URL을 읽는다.
    첨부는 `dd.artclInsert`에서만 찾는다.
    """
    raw = bu.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert raw.attachments == ()
    assert refs[0].list_meta["has_attachment"] is False


def test_attachment_bearing_posting_is_measured() -> None:
    """첨부가 달린 실제 공고로 셀렉터를 고정한다(2026-08-05 실측 · `detail_file.html`).

    ⚠️ 표본 공고에 첨부가 없으면 셀렉터가 틀려도 "정상인데 첨부 0개"로 통과한다 —
    그래서 첨부 있는 공고를 따로 받아 여기서 못을 박는다.

    ⚠️ **이 게시판에서 첨부를 가진 것은 고정공지뿐이다**(공고 40건 표본 0건 · 어댑터 docstring).
    공지는 `parse_list`가 걸러내므로 ref를 손으로 만든다 — 그래서 `has_attachment` 대조가
    성립하는 방향(표시 있음 + 첨부 있음)도 여기서 같이 확인된다.

    ⚠️ 셀렉터의 `div.artclItem.viewForm` 한정이 **빠지면 안 된다** — 이전글·다음글도 같은
    `dd.artclInsert`이고 `javascript:jf_naviArtclView(...)`를 href로 갖는다(실측). 한정을 지우면
    첨부가 3개로 늘고 그 중 2개가 다운로드할 수 없는 JS 호출이 된다.
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("detail_file.html 없음 — `--url .../graduateschool/1110/56059/artclView.do`")
    ref = PostingRef(
        external_id="56059",
        url="https://community.bu.ac.kr/bbs/graduateschool/1110/56059/artclView.do",
        title="백석ABA센터 연구원 모집 공고",
        posted_on=date(2026, 4, 13),
        list_meta={"has_attachment": True},
    )
    raw = bu.parse_detail(path.read_text(encoding="utf-8"), ref)
    assert len(raw.attachments) == 1, "이전글·다음글이 첨부로 섞였다"
    only = raw.attachments[0]
    assert only.name == "백석ABA센터 연구원 지원양식.hwp"
    # ⚠️ 다운로드 URL의 숫자는 **글번호가 아니라 파일 id**다(글 56059 / 파일 54200) —
    # 글번호로 착각해 URL을 재조립하면 엉뚱한 파일을 받는다.
    assert only.url == "https://community.bu.ac.kr/bbs/graduateschool/1110/54200/download.do"
    assert only.is_image is False  # HWP를 Gemini에 이미지로 보내지 않는다
