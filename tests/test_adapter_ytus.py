"""YTUS 어댑터 테스트 — 실제 게시판 HTML(fixture)로, 네트워크 없이.

fixture는 2026-08-04 실측본이며 개인정보를 마스킹했다. 사이트가 개편되면
이 테스트가 먼저 깨지는 것이 목적이다 — 조용히 0건이 되는 것보다 낫다.
"""

from __future__ import annotations

from datetime import date
from typing import Final

import pytest

from minjob_ingest.paths import PROJECT_ROOT
from minjob_ingest.sources.adapters import ytus
from minjob_ingest.sources.adapters.base import ParseError, PostingRef, parse_html
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = PROJECT_ROOT / "tests" / "fixtures" / "YTUS"
#: 실측값 — tr 21 = 헤더 1 + 공지 2 + 공고 18.
_EXPECTED_POSTINGS: Final = 18


def _list_html(*rows: str) -> str:
    """실측과 같은 컨테이너로 감싼 목록 HTML. 셀렉터가 컨테이너를 요구하므로 필수다."""
    return f'<div class="boardList"><table>{"".join(rows)}</table></div>'


def _row(
    *, num: str = "1", ident: str = "100", title: str = "가", rdate: str = "2026-08-04"
) -> str:
    return (
        f'<tr><td class="num">{num}</td>'
        f'<td class="title"><a href="/board/view/trXXR/{ident}">{title}</a></td>'
        f'<td class="author">교회</td><td class="rdate">{rdate}</td>'
        f'<td class="rnum">1</td></tr>'
    )


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "YTUS")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def list_html() -> str:
    return (_FIXTURES / "list.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def list_page2_html() -> str:
    return (_FIXTURES / "list_page2.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def detail_image_html() -> str:
    """이미지형 공고(25579) — 본문이 한 줄이고 내용이 jpeg 첨부에만 있다."""
    return (_FIXTURES / "detail_image.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def refs(list_html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    return ytus.parse_list(list_html, source)


# ── 페이지 URL ───────────────────────────────────────────────────


def test_first_page_is_the_configured_list_url(source: SourceConfig) -> None:
    request = ytus.list_request(source, 1)
    assert request.url == source.list_url
    assert request.form is None  # GET 게시판이다


def test_later_pages_append_the_page_segment(source: SourceConfig) -> None:
    # 실측: 쿼리 파라미터가 아니라 경로다(/page/2).
    assert ytus.list_request(source, 3).url == f"{source.list_url}/page/3"


def test_page_zero_is_rejected(source: SourceConfig) -> None:
    with pytest.raises(ValueError, match="page"):
        ytus.list_request(source, 0)


# ── 목록 파싱 ────────────────────────────────────────────────────


def test_parses_every_posting_row(refs: tuple[PostingRef, ...]) -> None:
    assert len(refs) == _EXPECTED_POSTINGS


def test_pinned_notices_are_excluded(refs: tuple[PostingRef, ...]) -> None:
    """고정공지는 공고가 아니다. 포함하면 매일 같은 두 건을 구조화해 돈을 쓴다."""
    notice_ids = {"9571", "6590"}  # 실측: 작성 유의사항 · 작성 양식
    assert notice_ids.isdisjoint({ref.external_id for ref in refs})


def test_external_id_comes_from_the_url_not_the_displayed_number(
    refs: tuple[PostingRef, ...],
) -> None:
    """실측에서 표시번호 16718 ≠ URL id 25581.

    표시번호를 원장 키로 쓰면 게시판이 번호를 다시 매길 때 같은 글을 새 글로 보거나 그 반대가
    된다.
    """
    newest = refs[0]
    assert newest.external_id == "25581"
    assert newest.list_meta["display_no"] == "16718"
    assert newest.url.endswith("/board/view/trXXR/25581")


def test_page_two_links_carry_a_page_suffix_and_still_parse(
    list_page2_html: str, source: SourceConfig
) -> None:
    """⚠️ 2페이지 상세 링크는 `/board/view/trXXR/25556/page/2` — **id 뒤에 페이지가 붙는다**.

    URL 마지막 조각을 id로 쓰면 20행 전부 `"2"`가 되어 한 글로 뭉개진다(실측에서 중복 가드가
    잡았다). id 위치는 `detail_pattern`이 알고 있으니 거기서 구한다.
    """
    refs = ytus.parse_list(list_page2_html, source)
    assert len(refs) == 20
    assert refs[0].external_id == "25556"
    assert len({ref.external_id for ref in refs}) == len(refs)


def test_source_url_is_canonical_regardless_of_the_page_found_on(
    list_page2_html: str, source: SourceConfig
) -> None:
    """`source_url`에 `/page/2`가 남으면 같은 글을 1페이지에서 찾았을 때와 값이 달라진다."""
    refs = ytus.parse_list(list_page2_html, source)
    assert refs[0].url == "https://www.ytus.ac.kr/board/view/trXXR/25556"
    assert all("/page/" not in ref.url for ref in refs)


def test_external_ids_are_unique(refs: tuple[PostingRef, ...]) -> None:
    assert len({ref.external_id for ref in refs}) == len(refs)


def test_detail_urls_are_absolute(refs: tuple[PostingRef, ...]) -> None:
    # 상대 href를 그대로 두면 fetch 층이 합쳐주긴 하지만, 저장되는 source_url이 깨진다.
    assert all(ref.url.startswith("https://www.ytus.ac.kr/") for ref in refs)


def test_posted_dates_are_parsed(refs: tuple[PostingRef, ...]) -> None:
    # 백필 컷오프(`--months N`)가 이 값만 보고 판단한다(SPEC §4).
    assert refs[0].posted_on == date(2026, 8, 4)
    assert all(ref.posted_on is not None for ref in refs)


def test_list_meta_carries_what_reuse_detection_needs(refs: tuple[PostingRef, ...]) -> None:
    """원장이 "이미 본 글"로 판정할 때 제목·날짜를 대조해 id 재사용을 잡는다(추가 요청 0건)."""
    meta = refs[0].list_meta
    assert meta["list_title"] == "안동도원교회에서 유년부전도사님 모십니다."
    assert meta["list_date"] == "2026-08-04"
    # ⚠️ 조회수는 **고정하지 않는다** — fixture를 다시 받을 때마다 늘어나 반드시 깨진다
    #    (실제로 15 → 43이 되어 깨졌다). 값이 아니라 **타입과 부호**만 본다.
    views = meta["views"]
    assert isinstance(views, int) and views >= 0


def test_bumped_posting_is_just_another_row(refs: tuple[PostingRef, ...]) -> None:
    """끌어올림 글도 같은 external_id를 가지므로 원장이 중복으로 걸러낸다(SPEC §4).

    fixture에 "끌어올림-"으로 시작하는 실제 행이 있다 — 특별 취급이 없어야 정상이다.
    """
    bumped = [ref for ref in refs if ref.title.startswith("끌어올림")]
    assert len(bumped) == 1
    assert bumped[0].external_id.isdigit()


# ── 목록 파싱 실패 ───────────────────────────────────────────────


def test_missing_table_is_an_error_not_an_empty_list(source: SourceConfig) -> None:
    """사이트 개편으로 테이블이 사라진 것과 공고 0건은 다르다.

    빈 리스트로 돌려주면 `source_health`에 "정상인데 0건"으로 남아 셀렉터가 깨진 걸 아무도
    모른다(SPEC §7 소프트 실패).
    """
    with pytest.raises(ParseError, match="셀렉터"):
        ytus.parse_list("<html><body><p>개편 중</p></body></html>", source)


def test_table_outside_the_expected_container_is_an_error(source: SourceConfig) -> None:
    """무자격 `table` 셀렉터는 문서 첫 테이블(검색창 등)을 집어 조용히 0건이 된다."""
    with pytest.raises(ParseError, match="셀렉터"):
        ytus.parse_list(f"<table>{_row()}</table>", source)


def test_table_without_posting_rows_is_legitimately_empty(source: SourceConfig) -> None:
    assert ytus.parse_list(_list_html("<tr><th>번호</th><th>제목</th></tr>"), source) == ()


def test_duplicate_external_id_is_rejected(source: SourceConfig) -> None:
    """하위 게시판이 섞이거나 id 추출이 틀리면 한 실행 안에서 중복이 보인다(SPEC §10)."""
    with pytest.raises(ParseError, match="중복"):
        ytus.parse_list(_list_html(_row(num="1"), _row(num="2")), source)


def test_non_numeric_url_tail_is_rejected(source: SourceConfig) -> None:
    with pytest.raises(ParseError, match="숫자가 아님"):
        ytus.parse_list(_list_html(_row(ident="abc")), source)


def test_unexpected_date_format_is_rejected(source: SourceConfig) -> None:
    """조용히 None으로 흘리면 백필 범위가 무의미해진다.

    ⚠️ `2026.08.04`(점)는 **거부하지 않는다** — 구분자는 게시판마다 `-`·`.`·`/`로 갈려서
    공용 파서가 셋 다 받는다(`require_date`). 값이 올바르게 나오면 문제가 아니다.
    실제로 못 읽는 형태만 실패해야 한다.
    """
    with pytest.raises(ParseError, match="게시일 형식"):
        ytus.parse_list(_list_html(_row(rdate="2026년 8월 4일")), source)


def test_empty_date_cell_is_rejected(source: SourceConfig) -> None:
    """YTUS는 전 행에 날짜가 있다 — 비면 셀렉터가 깨진 것이고, 백필 범위가 무의미해진다."""
    with pytest.raises(ParseError, match="게시일 칸"):
        ytus.parse_list(_list_html(_row(rdate="")), source)


def test_empty_number_marks_a_notice_even_without_the_class(source: SourceConfig) -> None:
    """공지 판정은 두 신호를 독립적으로 본다 — 게시판이 CSS 클래스를 바꿔도 걸려야 한다.

    실측에서 공지행은 `tr.notice-row`이면서 `td.num`이 비어 있다. 클래스만 믿으면 개편 한 번에
    공지 두 건이 매일 구조화돼 돈을 쓴다.
    """
    with pytest.raises(ParseError, match="전부 걸러짐"):
        ytus.parse_list(_list_html(_row(num="")), source)


def test_empty_title_is_rejected(source: SourceConfig) -> None:
    """빈 제목이 그대로 저장되면 검수 큐에서 무엇인지 알 수 없는 행이 된다."""
    with pytest.raises(ParseError, match="비어 있음"):
        ytus.parse_list(_list_html(_row(title="")), source)


def test_adapter_reports_images_as_found(refs: tuple[PostingRef, ...]) -> None:
    """어댑터는 페이지에 있는 대로 보고한다 — 중복 제거는 `SourceData`가 한다(한 곳에서만)."""
    html = (
        '<div class="boardViewContent">'
        '<img src="/upload/a.jpg"><img src="/upload/a.jpg"><img src="/upload/b.jpg">'
        "</div>"
    )
    raw = ytus.parse_detail(html, refs[0])
    assert raw.image_urls == (
        "https://www.ytus.ac.kr/upload/a.jpg",
        "https://www.ytus.ac.kr/upload/a.jpg",
        "https://www.ytus.ac.kr/upload/b.jpg",
    )


# ── 상세 파싱 ────────────────────────────────────────────────────


def test_detail_body_is_extracted_line_by_line(
    detail_html: str, refs: tuple[PostingRef, ...]
) -> None:
    """양식 게시판이라 항목별 줄이 살아야 AI가 읽기 쉽다."""
    raw = ytus.parse_detail(detail_html, refs[0])
    lines = raw.raw_text.splitlines()
    assert lines[0] == "교회명 : 도원교회"
    assert "교단명 : 통합" in lines


def test_denomination_is_stated_in_the_body(detail_html: str, refs: tuple[PostingRef, ...]) -> None:
    """교단이 본문에 명시돼 있어 구조화가 `stated`로 확정한다(SPEC §5.3 · AI 추정 불필요)."""
    raw = ytus.parse_detail(detail_html, refs[0])
    assert "교단명 : 통합" in raw.raw_text


def test_detail_keeps_the_ref(detail_html: str, refs: tuple[PostingRef, ...]) -> None:
    raw = ytus.parse_detail(detail_html, refs[0])
    assert raw.ref is refs[0]


def test_attachment_outside_the_body_is_collected(
    detail_image_html: str, refs: tuple[PostingRef, ...]
) -> None:
    """첨부 이미지는 본문의 **형제** 컨테이너에 렌더된다 — 본문만 훑으면 통째로 잃는다.

    실측 fixture(25579 삼성교회)는 본문이 한 줄이고 상세 내용이 첨부 포스터에만 있다. 이걸
    놓치면 Gemini가 핵심 정보를 못 보고, 본문까지 없는 공고라면 `ParseError`로 영구 미수집이
    된다.
    """


def test_all_attachments_are_captured_with_filenames(
    detail_image_html: str, refs: tuple[PostingRef, ...]
) -> None:
    """첨부는 **미리보기 컨테이너와 다른 곳**에 전체 목록이 있다.

    미리보기(`pnlAttachedImage`)에는 이미지형만 나오므로 그것만 보면 HWP·PDF를 통째로 잃는다.
    파일명이 필요한 이유: 다운로드 URL에 파일명이 없어(`/download/…/57439f…`) 이름이 없으면
    무슨 파일인지 알 수 없고, 구조화가 Gemini에 보낼지도 판단할 수 없다.
    """
    raw = ytus.parse_detail(detail_image_html, refs[0])
    assert len(raw.attachments) == 1
    attachment = raw.attachments[0]
    assert attachment.name == "삼성교회_담임목사_청빙.jpeg"
    assert attachment.is_image is True
    assert attachment.url.startswith("https://www.ytus.ac.kr/board/download/trXXR/25579/")


def test_preview_url_is_not_stored_as_an_inline_image(
    detail_image_html: str, refs: tuple[PostingRef, ...]
) -> None:
    """미리보기 URL을 `image_urls`에 넣으면 **같은 첨부가 두 번** 저장된다.

    실측: 미리보기 `/board/filelink/trXXR/25579/1/…` 와 다운로드
    `/board/download/trXXR/25579/file/1/…` 는 같은 파일(글 25579·첨부 1)인데 URL이 달라
    어떤 중복 제거도 못 잡는다 → 바이트 fetch와 Gemini 비용이 두 배가 된다.
    `image_urls`는 계약대로 **본문 인라인 전용**이다(SPEC §6 ①).
    """
    raw = ytus.parse_detail(detail_image_html, refs[0])
    assert raw.image_urls == ()
    assert len(raw.attachments) == 1  # 첨부로는 한 번만


def test_attachment_list_selector_drift_is_caught() -> None:
    """첨부 목록 셀렉터가 빗나가면 본문 있는 공고는 "정상인데 첨부 0개"로 통과한다.

    상세의 미리보기와 목록의 첨부 아이콘이 독립 신호라 대조하면 잡힌다.
    """
    html = (
        '<div class="boardViewContent">본문 있음</div>'
        '<div class="pnlAttachedImage"><img src="/board/filelink/x/1"></div>'
        '<div class="view-file-RENAMED"><a href="/dl/1">공고.hwp</a></div>'
    )
    ref = PostingRef(external_id="1", url="https://www.ytus.ac.kr/board/view/trXXR/1", title="가")
    with pytest.raises(ParseError, match="첨부가 있다고 표시됐는데"):
        ytus.parse_detail(html, ref)


def test_list_icon_alone_catches_selector_drift() -> None:
    """미리보기가 없는(비이미지 첨부) 공고는 **목록 아이콘**만이 신호다."""
    ref = PostingRef(
        external_id="1",
        url="https://www.ytus.ac.kr/board/view/trXXR/1",
        title="가",
        list_meta={"has_attachment": True},
    )
    html = '<div class="boardViewContent">본문 있음</div>'
    with pytest.raises(ParseError, match="첨부가 있다고 표시됐는데"):
        ytus.parse_detail(html, ref)


def test_list_attachment_icon_is_recorded(refs: tuple[PostingRef, ...]) -> None:
    """목록의 클립 아이콘이 상세 첨부 파싱을 교차 확인하는 신호가 된다.

    실측: 클립이 있는 행은 5개지만 그중 1개가 공지행이라 **공고는 4건**이다.
    """
    assert sum(1 for r in refs if r.list_meta.get("has_attachment")) == 4


def test_attachment_name_falls_back_to_the_url() -> None:
    """링크 텍스트가 비어도 버리지 않는다 — 조용히 버리면 개편 한 번에 전량 유실된다.

    실측: 다운로드 URL 끝 세그먼트가 파일명이다(`/download/…/삼성교회_담임목사_청빙.jpeg`).
    """
    html = (
        '<div class="boardViewContent">본문</div>'
        '<div class="view-file"><a href="/board/download/x/1/%EA%B3%B5%EA%B3%A0.hwp"></a></div>'
    )
    ref = PostingRef(external_id="1", url="https://www.ytus.ac.kr/board/view/trXXR/1", title="가")
    raw = ytus.parse_detail(html, ref)
    assert raw.attachments[0].name == "공고.hwp"


def test_table_cells_become_separate_lines() -> None:
    """`td`가 블록 경계가 아니면 `<td>교회명</td><td>도원교회</td>` → `"교회명도원교회"`.

    YTUS 본문엔 표가 없지만 `base.py`는 31곳 공용이고 표 양식 본문이 흔하다.
    """
    from minjob_ingest.sources.adapters.base import normalized_text

    element = parse_html("<div><table><tr><td>교회명</td><td>도원교회</td></tr></table></div>")
    wrapper = element.select_one("div")
    assert wrapper is not None
    text = normalized_text(wrapper)
    assert "교회명도원교회" not in text
    assert "교회명" in text.splitlines()


def test_non_image_attachment_is_kept_but_not_marked_for_gemini() -> None:
    """HWP는 Gemini가 못 읽지만 **URL은 남긴다** — 운영자가 열어볼 수 있어야 한다."""
    html = (
        '<div class="boardViewContent">본문</div>'
        '<div class="view-file"><p class="file-tit">첨부파일</p>'
        '<a href="/board/download/x/1">청빙공고.hwp</a>'
        '<a href="/board/download/x/2">지원서.pdf</a></div>'
    )
    ref = PostingRef(external_id="1", url="https://www.ytus.ac.kr/board/view/trXXR/1", title="가")
    raw = ytus.parse_detail(html, ref)
    assert [(a.name, a.is_image) for a in raw.attachments] == [
        ("청빙공고.hwp", False),
        ("지원서.pdf", False),
    ]


def test_attachment_only_posting_is_not_a_failure() -> None:
    """본문·인라인이미지가 없고 첨부만 있어도 정상이다 — 실패시키면 영구 미수집이 된다."""
    html = (
        '<div class="boardViewContent"></div>'
        '<div class="view-file"><a href="/dl/1">공고.hwp</a></div>'
    )
    ref = PostingRef(external_id="1", url="https://www.ytus.ac.kr/board/view/trXXR/1", title="가")
    raw = ytus.parse_detail(html, ref)
    assert raw.raw_text == ""
    assert len(raw.attachments) == 1


def test_inline_separator_does_not_split_tokens(
    detail_html: str, refs: tuple[PostingRef, ...]
) -> None:
    """`get_text(" ")`는 span 경계마다 공백을 넣어 `"1 명"`·`"이력서 , "`처럼 벌어진다."""
    raw = ytus.parse_detail(detail_html, refs[0])
    assert "모집인원 : 1명" in raw.raw_text
    assert "제출서류 : 이력서, 자기소개서, 가족관계증명서" in raw.raw_text


def test_image_only_body_is_not_a_failure(refs: tuple[PostingRef, ...]) -> None:
    """본문을 이미지로만 올리는 공고가 있다(`image_only`) — 빈 raw_text가 정상이다."""
    html = '<div class="boardViewContent"><img src="/upload/notice.jpg"></div>'
    raw = ytus.parse_detail(html, refs[0])
    assert raw.raw_text == ""
    assert raw.image_urls == ("https://www.ytus.ac.kr/upload/notice.jpg",)


def test_an_empty_posting_is_a_fact_not_a_failure(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 게시판에는 **내용 없이 올라온 글이 실제로 있다**(실측 25309 = `<p>&nbsp;</p>`).

    예전엔 이걸 실패로 던졌다. 그러면 원장에 안 들어가 **매 실행 다시 받고 매번 "실패 1건"으로
    보고된다** — 그 노이즈가 진짜 실패를 가린다. 셀렉터가 빗나간 경우는 컨테이너 부재
    (`test_missing_body_container_is_an_error`)와 소스 단위 전량 빈 내용 판정이 잡는다.
    """
    raw = ytus.parse_detail('<div class="boardViewContent"><p>&nbsp;</p></div>', refs[0])
    assert raw.raw_text == ""
    assert raw.image_urls == ()
    assert raw.attachments == ()


def test_missing_body_container_is_an_error(refs: tuple[PostingRef, ...]) -> None:
    with pytest.raises(ParseError, match="셀렉터"):
        ytus.parse_detail("<html><body>개편 중</body></html>", refs[0])


# ── fixture 위생 ──────────────────────────────────


#: fixture별 "담임목사 :" 라벨 **실측 개수**. `all(...)`은 0건 매치에서도 통과하므로
