"""UHS 어댑터 — **이 게시판을 열어봐야 아는 실측값만** 둔다.

구조적 검사(조용한 0건·페이징·id 유일·상세 증거)는 `test_adapter_conformance.py`가 31곳 전부에
적용한다. 여기 중복해서 쓰지 않는다.

fixture는 2026-08-04 실측본이고 커밋되지 않는다 —
`minjob-ingest snapshot --source UHS` 으로 받는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import uhs
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "UHS"
#: 실측: tr 12 = 헤더 1 + 공지 1 + 공고 10.
_EXPECTED_POSTINGS: Final = 10
_NOTICE_TITLE_PART: Final = "교역자 청빙 게시판 이용 방법 안내"
#: ⚠️ config에 `soft_200`이 없지만 실제로는 없는 상세를 HTTP 200 + 이 쉘로 준다(실측).
_ERROR_SHELL: Final = (
    "<!doctype html><html><head><title>알림메세지</title></head><body>"
    '<div class="message_area"><h2>알립니다.</h2>'
    "<p>원하시는 페이지를 찾을 수가 없습니다.</p></div></body></html>"
)

pytestmark = pytest.mark.skipif(
    not (_FIXTURES / "list.html").exists(),
    reason="UHS fixture 없음 — `minjob-ingest snapshot --source UHS`",
)


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "UHS")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return uhs.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


def _row(*, no: str = "2326", ident: str = "9467", cls: str = "", inner: str = "") -> str:
    return (
        f'<tr{cls}><td class="TdNumber">{no}</td>'
        f'<td class="al-left lin-el">'
        f'<a class="artclLinkView" href="/bbs/gsthe/183/{ident}/artclView.do">'
        f"<strong>[서울/중구용산] 상동교회 담임목사 청빙 공고(수정)</strong>{inner}</a></td>"
        f'<td class="TdWriter">백서현</td><td class="TdDate">2026.07.29</td>'
        f'<td class="TdAccess">100</td><td class="TdAtchFile"></td></tr>'
    )


def _list_html(*rows: str) -> str:
    return f'<div class="tableWrap"><table>{"".join(rows)}</table></div>'


# ── 목록 ─────────────────────────────────────────────────────────


def test_notice_is_excluded_and_postings_counted(refs: tuple[PostingRef, ...]) -> None:
    """고정공지 1건은 제외하고 공고 10건만 남는다(실측)."""
    assert len(refs) == _EXPECTED_POSTINGS
    assert all(_NOTICE_TITLE_PART not in ref.title for ref in refs)


def test_first_row_is_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    """id(artclNo)·제목·게시일·정규 URL이 실측과 같아야 한다."""
    first = refs[0]
    assert first.external_id == "9467"
    assert first.title == "[서울/중구용산] 상동교회 담임목사 청빙 공고(수정)"
    assert first.posted_on == date(2026, 7, 29)
    assert first.url == "https://www.uhs.ac.kr/bbs/gsthe/183/9467/artclView.do"
    assert first.list_meta["display_no"] == "2326"


def test_the_new_badge_is_not_part_of_the_title(source: SourceConfig) -> None:
    """⚠️ "새글" 배지 클래스가 BU와 다르다(`new-ba` vs `newArtcl`) — 같은 CMS인데 스킨이 갈린다.

    떼지 않으면 배지가 사라질 때 같은 글의 제목이 달라져 원장 경보가 헛울린다.
    """
    refs = uhs.parse_list(_list_html(_row(inner='<span class="new-ba">새글</span>')), source)
    assert refs[0].title == "[서울/중구용산] 상동교회 담임목사 청빙 공고(수정)"


# ── 셀렉터가 빗나갔을 때 ─────────────────────────────────────────


def test_notice_row_class_and_empty_number_both_filter(source: SourceConfig) -> None:
    """공지는 `tr.key-notice`이고 표시번호 칸이 빈 배지(`span.key-noti`)다.

    두 신호를 독립적으로 본다 — 하나가 바뀌어도 공지가 공고로 새지 않는다.
    """
    for notice in (_row(cls=' class="key-notice"'), _row(no='<span class="key-noti"></span>')):
        with pytest.raises(ParseError, match="전부 걸러짐"):
            uhs.parse_list(_list_html(notice), source)


def test_missing_date_cell_is_rejected(source: SourceConfig) -> None:
    """날짜가 없으면 `--months` 범위가 조용히 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        uhs.parse_list(_list_html(_row().replace(">2026.07.29<", "><")), source)


def test_a_soft_200_error_shell_is_not_a_posting(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ config에 `soft_200`이 없는데 실제로는 그렇게 동작한다 — 없는 상세가 200 + 에러 쉘이다.

    상태코드로 성공을 판정하면 빈 레코드가 저장된다. 본문 셀렉터가 그 판정을 겸한다.
    """
    with pytest.raises(ParseError, match="상세 본문"):
        uhs.parse_detail(_ERROR_SHELL, refs[0])


# ── 상세 ─────────────────────────────────────────────────────────


def test_detail_body_excludes_the_meta_block(refs: tuple[PostingRef, ...]) -> None:
    """본문은 `div.dataView`다(실측 983자) — 제목·작성일 블록(`div.infoWrap`)은 형제다."""
    raw = uhs.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert "기독교대한감리회 서울연회 중구용산지방회 상동교회" in raw.raw_text
    assert "지원자격" in raw.raw_text
    assert "조회수" not in raw.raw_text
    assert raw.image_urls == ()


def test_attachments_come_from_the_file_block_with_real_names(
    refs: tuple[PostingRef, ...],
) -> None:
    """첨부 2건(실측). 다운로드 URL에는 파일명이 없어 **링크 텍스트**가 파일명이다.

    목록의 첨부 표시(`has_attachment`)와 교차 확인되므로 셀렉터가 빗나가면 `ParseError`가 난다.
    """
    raw = uhs.parse_detail((_FIXTURES / "detail.html").read_text(encoding="utf-8"), refs[0])
    assert refs[0].list_meta["has_attachment"] is True
    assert [attachment.name for attachment in raw.attachments] == [
        "담임목사 청빙 재공고문_20260726.pdf",
        "개인정보 수집·이용 동의서_20260708.pdf",
    ]
    # 미리보기 버튼은 `<input>`이라 첨부로 잡히지 않는다 — 잡히면 개수가 4가 된다.
    assert len(raw.attachments) == 2
