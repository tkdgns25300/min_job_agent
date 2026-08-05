"""HANIL 어댑터 — 목록이 JSON이고 본문까지 담고 있는 유일한 게시판.

구조적 검사는 `test_adapter_conformance.py`가 한다. 여기엔 실측값과 이 게시판만의 함정만 둔다.
fixture(`list.html`)는 HTML이 아니라 **API JSON 응답**이다 —
`minjob-ingest snapshot`으로는 받을 수 없어 어댑터의 `list_request`로 직접 받았다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import hanil
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.adapters.registry import needs_detail_request
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "HANIL"
#: 실측: API가 페이지당 10건을 준다(전체 12,497건).
_PER_PAGE: Final = 10

pytestmark = pytest.mark.skipif(not (_FIXTURES / "list.html").exists(), reason="HANIL fixture 없음")


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "HANIL")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return hanil.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


# ── 목록이 POST다 ────────────────────────────────────────────────


def test_the_list_is_a_post_with_a_form(source: SourceConfig) -> None:
    """URL만으로는 표현할 수 없는 게시판 — `ListRequest`가 form을 함께 드는 이유다."""
    request = hanil.list_request(source, 3)
    assert request.is_post
    assert request.form == {
        "boardId": "BBS00000000000000262",
        "menuId": "M0004000500000000",
        "pageIndex": "3",
    }


def test_postings_are_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    assert len(refs) == _PER_PAGE
    first = refs[0]
    assert first.external_id == "104524"
    assert first.posted_on == date(2026, 8, 4)
    assert "안양일심교회" in first.title


def test_integer_fields_are_not_dropped(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ `boardSeq`·`count`는 JSON에서 **정수**로 온다.

    문자열만 받으면 id가 빈 값이 되어 목록 전체가 `ParseError`로 죽는다(실제로 그랬다).
    """
    assert refs[0].external_id.isdigit()
    assert isinstance(refs[0].list_meta["views"], int)


def test_the_string_none_is_not_taken_as_a_value(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 이 API는 빈 칸을 `null`이 아니라 **문자열 `"None"`**으로 준다.

    그대로 쓰면 작성자가 "None"인 공고가 저장된다.
    """
    assert all(ref.list_meta.get("author") != "None" for ref in refs)


def test_notices_are_excluded(source: SourceConfig) -> None:
    payload = json.loads((_FIXTURES / "list.html").read_text(encoding="utf-8"))
    rows = payload["list"]
    rows[0]["noticeYn"] = "Y"
    refs = hanil.parse_list(json.dumps(payload), source)
    assert len(refs) == _PER_PAGE - 1


def test_a_non_json_response_is_an_error(source: SourceConfig) -> None:
    """API가 HTML 오류 페이지를 주면 조용한 0건이 아니라 실패여야 한다."""
    with pytest.raises(ParseError, match="JSON이 아님"):
        hanil.parse_list("<html><body>error</body></html>", source)


# ── 본문이 목록에서 온다 ─────────────────────────────────────────


def test_no_detail_request_is_needed() -> None:
    """상세 페이지는 JS가 채우는 빈 껍데기라 받아도 제목조차 없다(실측 115KB)."""
    assert needs_detail_request(hanil) is False


def test_body_comes_from_the_listing(refs: tuple[PostingRef, ...]) -> None:
    raw = hanil.parse_detail("", refs[0])
    assert "안양일심교회" in raw.raw_text


def test_contact_emails_are_not_attachments(refs: tuple[PostingRef, ...]) -> None:
    """⚠️ 이 게시판 본문에는 지원 문의 `mailto:`가 들어 있다.

    그것을 첨부로 저장하면 구조화가 "첨부 파일"을 열려 하고 아무것도 받지 못한다.
    실제로 4건이 그렇게 저장되고 있었다(2026-08-05) — base가 이제 걸러낸다.
    """
    for ref in refs:
        for attachment in hanil.parse_detail("", ref).attachments:
            assert not attachment.url.startswith("mailto:"), attachment.url


def test_a_posting_with_a_file_is_still_collected(source: SourceConfig) -> None:
    """첨부 다운로드 경로를 아직 모르지만(모듈 docstring) **공고를 버리지 않는다**.

    본문이 내용을 담고 있어 유실이 아니고, `has_attachment`가 "파일이 있다"는 사실을 남겨
    운영자가 검수에서 원문을 열 수 있다.
    """
    page2 = _FIXTURES / "list_page2.html"
    if not page2.exists():
        pytest.skip("list_page2.html 없음")
    marked = [
        ref
        for ref in hanil.parse_list(page2.read_text(encoding="utf-8"), source)
        if ref.list_meta.get("has_attachment")
    ]
    assert marked, "isFile=Y 인 공고가 없다 — 신호가 사라졌다"
    raw = hanil.parse_detail("", marked[0])
    assert raw.raw_text.strip()  # 본문은 살아 있다


def test_passing_detail_html_is_rejected(refs: tuple[PostingRef, ...]) -> None:
    """호출자가 규칙을 어기면 조용히 무시하지 않는다 — 빈 껍데기를 파싱해 0자가 된다."""
    with pytest.raises(ParseError, match="상세 HTML이 넘어왔다"):
        hanil.parse_detail("<html>...</html>", refs[0])


def test_the_body_is_not_duplicated_into_raw_meta(refs: tuple[PostingRef, ...]) -> None:
    """`_` 접두 키는 `collect`가 `raw_meta`에서 뺀다 — 안 그러면 `raw_text`와 그대로 중복된다."""
    internal = [key for key in refs[0].list_meta if key.startswith("_")]
    assert internal == ["_body_html"]


def test_body_links_are_not_attachments(source: SourceConfig) -> None:
    """⚠️ **실측 4건 전부가 잘못 들어갔다**(2026-08-05): 교회 홈페이지·타 게시판 공고 URL.

    이 게시판의 첨부는 목록 JSON의 `fileSeq`이고 다운로드 경로를 아직 모른다(모듈 docstring).
    본문을 긁으면 파일이 아닌 것이 첨부로 저장돼, 구조화가 그것을 파일로 열려 하고 지원자에게
    첨부라고 보여진다. 첨부 **유무**는 `has_attachment`에 사실로 남는다.
    """
    payload = json.dumps(
        {
            "cnt": 1,
            "isSuccess": True,
            "list": [
                {
                    "boardSeq": 104537,
                    "title": "이리제일교회에서 사역자를 모십니다",
                    "createDt": "20260731",
                    "isFile": "Y",
                    "fileSeq": 27695,
                    "contents": (
                        '<p>홈페이지 <a href="http://www.irijeil.or.kr">www.irijeil.or.kr</a></p>'
                        '<p>참고 <a href="https://www.puts.ac.kr/www/board/view.general.asp'
                        '?seq=149891">타 게시판 공고</a></p>'
                    ),
                }
            ],
        }
    )
    refs = hanil.parse_list(payload, source)
    raw = hanil.parse_detail("", refs[0])
    assert raw.attachments == ()
    assert refs[0].list_meta["has_attachment"] is True  # 사실은 기록된다
    assert "irijeil" in raw.raw_text  # 주소는 본문에 그대로 남는다
