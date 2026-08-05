"""CSU 어댑터 — 목록 API가 본문·첨부·구조화 필드까지 주는 SPA.

구조적 검사는 `test_adapter_conformance.py`가 한다. 여기엔 실측값과 이 게시판만의 함정만 둔다.
fixture(`list.html`)는 HTML이 아니라 **API JSON 응답**이다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import csu
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.adapters.registry import needs_detail_request
from minjob_ingest.sources.registry import SourceConfig, find_source, load_sources

_FIXTURES: Final = Path(__file__).parent / "fixtures" / "CSU"
#: 실측: API가 페이지당 10건(전체 75,000건 이상).
_PER_PAGE: Final = 10

pytestmark = pytest.mark.skipif(not (_FIXTURES / "list.html").exists(), reason="CSU fixture 없음")


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    found = find_source(load_sources(None), "CSU")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def refs(source: SourceConfig) -> tuple[PostingRef, ...]:
    return csu.parse_list((_FIXTURES / "list.html").read_text(encoding="utf-8"), source)


# ── 목록 요청 ────────────────────────────────────────────────────


def test_the_form_uses_camel_case_names(source: SourceConfig) -> None:
    """⚠️ 이름이 `board_id`가 아니라 **`boardIdList`**다.

    snake_case로 보내면 서버가 본문을 못 읽고 `code 22000 유효하지 않은 세션입니다`를 준다.
    메시지가 세션을 가리켜 쿠키를 찾게 만들지만 **쿠키는 필요 없다** — 이 이름 하나 때문이었다.
    """
    request = csu.list_request(source, 3)
    assert request.is_post
    assert request.form is not None
    assert request.form["boardIdList"] == "178"
    assert request.form["page"] == "3"
    # 본문·첨부·공고 필드를 함께 받아야 상세 요청이 필요 없어진다.
    assert request.form["includeBody"] == "1"
    assert request.form["includeAttachmentList"] == "1"
    assert request.form["includeProperties"] == "1"


def test_no_detail_request_is_needed() -> None:
    assert needs_detail_request(csu) is False


# ── 목록 파싱 ────────────────────────────────────────────────────


def test_postings_are_read_as_measured(refs: tuple[PostingRef, ...]) -> None:
    assert len(refs) == _PER_PAGE
    first = refs[0]
    assert first.external_id.isdigit()
    assert first.posted_on is not None
    assert first.url == (
        "https://csu.ac.kr/?m1=page_ministry_detail&menu_id=1110"
        f"&board_content_id={first.external_id}"
    )


def test_an_api_failure_is_not_a_quiet_zero(source: SourceConfig) -> None:
    """`code != 10000`을 빈 목록으로 흘리면 파라미터가 바뀐 것을 아무도 모른다."""
    failed = json.dumps({"code": 22000, "message": "유효하지 않은 세션입니다."})
    with pytest.raises(ParseError, match="code=22000"):
        csu.parse_list(failed, source)


def test_pinned_notices_are_excluded(source: SourceConfig) -> None:
    payload = json.loads((_FIXTURES / "list.html").read_text(encoding="utf-8"))
    payload["body"]["list"][0]["is_always_on_top"] = 1
    assert len(csu.parse_list(json.dumps(payload), source)) == _PER_PAGE - 1


# ── 개인정보 ─────────────────────────────────────────────────────


def test_poster_identity_is_never_carried(refs: tuple[PostingRef, ...]) -> None:
    """⚠️⚠️ 응답의 `properties.cert_data`에는 작성자의 **CI(주민번호 파생 연계정보)**·생년월일·
    성별·휴대폰·실명이 있다. 공고 내용이 아니라 신원 정보이므로 저장하면 가드레일 #4 위반이다.

    `properties`를 통째로 옮기지 않고 **화이트리스트**로 옮기는 이유가 이것이다 — 서버가 필드를
    추가해도 새 개인정보가 자동으로 흘러들지 않는다.
    """
    forbidden = {
        "cert_data",
        "registered_from_ip_address",
        "last_modified_from_ip_address",
        "registered_by_user_id",
        "registered_by_user_idx",
        "registered_by_user_name",
    }
    for ref in refs:
        leaked = forbidden & set(ref.list_meta)
        assert not leaked, f"{ref.external_id}: 개인정보 필드가 남았다 {sorted(leaked)}"
        assert "CI" not in json.dumps(dict(ref.list_meta), ensure_ascii=False)


def test_posting_fields_survive_the_whitelist(refs: tuple[PostingRef, ...]) -> None:
    """교회가 공고에 스스로 적은 필드는 남아야 한다 — 특히 교단은 구조화가 그대로 쓴다."""
    meta = refs[0].list_meta
    assert isinstance(meta.get("church_name"), str)
    assert isinstance(meta.get("order_name"), str)  # 예: 대한예수교장로회(합동)
    assert "presbytery_name" in meta


# ── 본문·첨부 ────────────────────────────────────────────────────


def test_body_comes_from_the_listing(refs: tuple[PostingRef, ...]) -> None:
    raw = csu.parse_detail("", refs[0])
    assert len(raw.raw_text) > 50
    assert raw.ref is refs[0]


def test_attachments_keep_their_filenames(refs: tuple[PostingRef, ...]) -> None:
    """다운로드 경로가 UUID라(`e72ef422-….jpg`) 파일명이 없으면 종류를 알 수 없다.

    구조화가 이미지 첨부만 Gemini에 보내므로 `is_image` 판정이 파일명에 달려 있다.
    """
    withfile = [ref for ref in refs if ref.list_meta.get("has_attachment")]
    if not withfile:
        pytest.skip("이 fixture에 첨부 있는 공고가 없다")
    attachments = csu.parse_detail("", withfile[0]).attachments
    assert attachments
    for attachment in attachments:
        assert attachment.name  # UUID 경로만 남으면 종류를 알 수 없다
        assert attachment.url.startswith("https://csu.ac.kr/upload/")


def test_passing_detail_html_is_rejected(refs: tuple[PostingRef, ...]) -> None:
    with pytest.raises(ParseError, match="상세 HTML이 넘어왔다"):
        csu.parse_detail("<html>...</html>", refs[0])
