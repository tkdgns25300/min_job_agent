"""MOKWON 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다(가드레일 #11) —
`minjob-ingest snapshot --source MOKWON` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import mokwon
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "MOKWON"
#: 실측: tr 16 = 헤더 1 + 공지 1 + 공고 14.
_EXPECTED_POSTINGS: Final = 14
_NOTICE_TITLE: Final = "※ 사역지 정보 게시물 작성 방법 ※"
#: 실측 첫 행의 `no` — **32자리 hex**다(숫자가 아니다).
_FIRST_ID: Final = "501103573814a8ef882b3f885d1fb33b"

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="MOKWON fixture 없음 — `minjob-ingest snapshot --source MOKWON`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "MOKWON")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return mokwon.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(
    *,
    no: str = "695",
    ident: str = _FIRST_ID,
    title: str = "가",
    date_text: str = "2026-05-18",
    row_class: str = "",
    atch: str = "",
) -> str:
    attr = f' class="{row_class}"' if row_class else ""
    return (
        f'<tr{attr}><td class="ntt_no">{no}</td>'
        f'<td class="title"><a href="?mode=V&amp;no={ident}&amp;GotoPage=1">{title}</a></td>'
        f'<td class="wrt">윤**</td><td class="inq_cnt">88</td>'
        f'<td class="reg_date">{date_text}</td><td class="atch_nm">{atch}</td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<table class="board_list">{"".join(rows)}</table>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """공지 1건은 제외하고 공고 14건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert _NOTICE_TITLE not in {ref.title for ref in refs}


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id·제목·게시일·정규 URL이 실측과 같아야 한다 — 칸이 밀리면 여기서 드러난다."""
    first = refs[0]
    assert first.external_id == _FIRST_ID
    assert first.title == "[서울/중구용산] 일신교회 풀타임(수련목 가능) 전도사님을 모십니다."
    assert first.posted_on == date(2026, 5, 18)
    assert first.url == (f"https://mokwon.ac.kr/mt1954/html/sub06/0602.html?mode=V&no={_FIRST_ID}")
    # 목록 href의 `GotoPage=1`은 정규형에 남지 않는다 — 같은 글의 URL이 페이지마다 달라지면 안 된다.
    assert "GotoPage" not in first.url


def test_the_external_id_is_hex_not_a_number(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ `no`는 32자리 hex다 — 숫자 검사를 걸면 전 행이 탈락한다.

    숫자인 쪽은 표시번호(`td.ntt_no` = 695)이고 그건 게시판이 다시 매기는 값이라 원장 키가 아니다.
    """
    assert not refs[0].external_id.isdigit()
    assert len(refs[0].external_id) == 32
    assert refs[0].list_meta["display_no"] == "695"


def test_a_changed_id_shape_is_rejected(source: SourceConfig) -> None:
    """id 형태가 hex가 아니게 되면 링크 규칙이 바뀐 것이다 — 조용히 넘기지 않는다."""
    with pytest.raises(ParseError, match="hex"):
        mokwon.parse_list(_list_html(_row(ident="12345")), source)


def test_notice_row_is_detected_by_class_and_by_number(source: SourceConfig) -> None:
    """공지 신호가 둘이다(`tr.bbs_notice` · 표시번호 `공지`) — 하나가 바뀌어도 걸린다."""
    for notice in (_row(row_class="bbs_notice"), _row(no="공지")):
        with pytest.raises(ParseError, match="전부 걸러짐"):
            mokwon.parse_list(_list_html(notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        mokwon.parse_list(_list_html(_row(date_text="")), source)


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_is_the_posting_content_only(refs: tuple[PostingRef, ...]) -> None:
    """본문은 카드 안쪽 `div.bbs--view--content`다(실측 538자).

    상세 페이지에는 사이트 내비게이션·학과 메뉴가 함께 있어 범위를 좁혀야 한다.
    """
    raw = mokwon.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "기독교대한감리회 서울연회 중구용산지방 일신교회" in raw.raw_text
    assert "6. 제출서류" in raw.raw_text
    assert "학과안내" not in raw.raw_text
    # 이 공고는 본문만 있다(목록 `td.atch_nm`가 15행 전부 빈칸 · 실측).
    assert raw.attachments == ()
    assert raw.image_urls == ()


def test_the_list_attachment_cell_is_the_cross_check_signal(source: SourceConfig) -> None:
    """`td.atch_nm`가 상세 첨부 셀렉터를 검증하는 **독립 신호**다(2026-08-05 실측 8페이지).

    빈 칸이면 `False`, 첨부가 있으면 PCMS가 이 `a > span.bd_file_icon`을 넣는다 — 아래 마크업은
    실측본 그대로다. 이 판정이 죽으면 상세 셀렉터가 빗나가도 아무도 모른다.
    """
    icon = (
        f'<a href="#DownList{_FIRST_ID}"'
        f" onclick=\"DownloadFile('mt1954', 'mt1954_0602', '{_FIRST_ID}', '');\">"
        '<span class="bd_file_icon icon04">첨부파일있음</span></a>'
    )
    marked = mokwon.parse_list(_list_html(_row(atch=icon)), source)
    assert marked[0].list_meta["has_attachment"] is True
    plain = mokwon.parse_list(_list_html(_row()), source)
    assert plain[0].list_meta["has_attachment"] is False


def test_attachment_bearing_posting_is_measured() -> None:
    """첨부가 달린 실제 공고로 셀렉터를 고정한다(2026-08-05 실측 · `detail_file.html`).

    ⚠️ 표본 공고에 첨부가 없으면 셀렉터가 틀려도 "정상인데 첨부 0개"로 통과한다 —
    1·2·20페이지 45행이 전부 빈칸이라 8페이지까지 뒤져 첨부 있는 공고를 받았다.

    ⚠️ **공고 카드(`div.bbs--view`)를 첨부 범위로 쓰면 안 된다** — 이 공고의 본문에는 교회
    홈페이지와 `mailto:` 링크가 있어 카드 범위로는 첨부가 5개가 아니라 7개가 됐다(실측).
    첨부는 본문의 형제인 `div.bbs--view--file`에서만 온다.
    """
    path = _FIXTURES / "detail_file.html"
    if not path.exists():
        pytest.skip("detail_file.html 없음 — `--url ...?mode=V&no=8952a64d…cf57071a`")
    ident = "8952a64d9f51a16c20a07301cf57071a"
    ref = PostingRef(
        external_id=ident,
        url=f"https://mokwon.ac.kr/mt1954/html/sub06/0602.html?mode=V&no={ident}",
        title="대전 보리떡교회에서 파트 교역자(청소년/청년부)를 모십니다.",
        posted_on=date(2024, 11, 5),
        list_meta={"has_attachment": True},
    )
    raw = mokwon.parse_detail(path.read_text(encoding="utf-8"), ref)
    assert [found.name for found in raw.attachments] == [
        "사역계획및비전.hwp",
        "신앙간증문.hwp",
        "이력서.hwp",
        "자기소개서.hwp",
        "목회자추천서.hwp",
    ]
    # 다운로드는 같은 파일의 `mode=D` + `file_id`다 — 본문 링크(홈페이지·mailto)가 섞이면 깨진다.
    assert all(f"?mode=D&no={ident}&file_id=" in found.url for found in raw.attachments)
    assert not any(found.is_image for found in raw.attachments)
