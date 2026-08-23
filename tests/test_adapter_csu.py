"""CSU 어댑터 — 목록 API가 본문·첨부·구조화 필드까지 주는 SPA.

구조적 검사는 `test_adapter_conformance.py`가 한다. 여기엔 실측값과 이 게시판만의 함정만 둔다.
fixture(`list.html`)는 HTML이 아니라 **API JSON 응답**이다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from minjob_ingest.sources.adapters import csu
from minjob_ingest.sources.adapters.base import ParseError, PostingRef
from minjob_ingest.sources.adapters.csu import _BODY_FIELD
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


def _ref_with(*, body: str, attachments: Sequence[Sequence[str]]) -> PostingRef:
    """본문·첨부만 정해 놓은 목록 참조. 상세 URL은 실제 공유용 형태를 쓴다."""
    return PostingRef(
        external_id="1118481",
        url="https://csu.ac.kr/?m1=page_ministry_detail&menu_id=1110&board_content_id=1118481",
        title="퍼스우리교회 전임 사역자 청빙공고",
        list_meta={"_body_html": body, "_attachments": [list(pair) for pair in attachments]},
    )


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
    성별·휴대폰·실명이 있다. 공고 내용이 아니라 신원 정보이므로 저장하지 않는다.

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


def test_attachments_use_the_same_file_api_as_inline_images() -> None:
    """⚠️ **첨부 URL을 추측하지 않는다**(2026-08-23 실측으로 고쳤다).

    JSON은 상대 경로(`board/202608//x.pdf`)만 주고 다운로드 루트를 알려주지 않는다. 처음엔
    `/upload/`로 추측했고 **모든 첨부가 404**였는데, 포스터가 저장돼 있어서 오래 안 드러났다 —
    그 포스터는 전부 **본문 인라인 그림**이었다.

    게시판 에디터가 본문 HTML에 써 두는 인라인 URL이 **정답의 증거**다(추측이 아니라 게시판이
    직접 쓴 값). 그것과 같은 형태여야 한다 — 그래서 둘을 한 테스트로 묶는다.
    """
    inline = "https://csu.ac.kr/api/file/get?path=html_editor/202608//abc.jpg"
    body = f'<p><img src="{inline}"></p>'
    ref = _ref_with(body=body, attachments=[["청빙공고.pdf", "board/202608//def.pdf"]])
    raw = csu.parse_detail("", ref)

    assert raw.image_urls == (inline,)
    (attachment,) = raw.attachments
    assert attachment.url == "https://csu.ac.kr/api/file/get?path=board/202608//def.pdf"
    # ⚠️ 두 URL이 **같은 엔드포인트**를 지나야 한다 — 인라인만 되고 첨부는 404이던 상태를 막는다.
    api = "https://csu.ac.kr/api/file/get?path="
    assert raw.image_urls[0].startswith(api)
    assert attachment.url.startswith(api)


def test_passing_detail_html_is_rejected(refs: tuple[PostingRef, ...]) -> None:
    with pytest.raises(ParseError, match="상세 HTML이 넘어왔다"):
        csu.parse_detail("<html>...</html>", refs[0])


# ── 포스터만 있는 공고 · includeBody 감지 ────────────────────────


def _payload(body: str | None, *, with_body_key: bool = True, attachment: str | None = None) -> str:
    """최소 목록 응답. **실제 응답을 쓰지 않는다** — 작성자 본인인증 정보가 들어 있다."""
    row: dict[str, object] = {
        "id": 1117808,
        "title": "성실교회 중등부에서 동역자를 모십니다.",
        "registered_date": "2026-08-03 20:16:51",
        "view_count": 12,
        "attachment_count": 1 if attachment else 0,
        "properties": {"church_name": "성실교회", "order_name": "합동"},
    }
    if attachment:
        row["attachment_list"] = [
            {"original_filename": attachment, "url": f"board/202608//{attachment}"}
        ]
    if with_body_key:
        row[_BODY_FIELD] = body
    return json.dumps({"code": 10000, "body": {"total_count": 1, "list": [row]}})


def test_a_poster_only_posting_is_collected(source: SourceConfig) -> None:
    """⚠️ **이것 때문에 공고를 버렸다**(2026-08-05 · 실측 1117808 성실교회).

    본문이 포스터 이미지 한 장뿐인 공고가 흔하다. 증거 판정에서 이미지를 빼먹어 `본문과 첨부가
    모두 없음`으로 탈락시켰다 — 나머지 29곳은 처음부터 **본문·이미지·첨부 셋을** 본다.
    내용은 포스터에 있고 구조화가 Gemini 멀티모달로 읽는다(SPEC §5).
    """
    poster = '<p><img src="/api/file/get?path=html_editor/202608//8748ae55.png"></p>'
    refs = csu.parse_list(_payload(poster), source)
    raw = csu.parse_detail("", refs[0])
    assert raw.raw_text == ""  # 포스터 공고는 빈 본문이 정상이다
    assert raw.image_urls == (
        "https://csu.ac.kr/api/file/get?path=html_editor/202608//8748ae55.png",
    )


def test_a_page_with_no_body_at_all_is_a_source_failure(source: SourceConfig) -> None:
    """⚠️ `includeBody`가 먹지 않으면 서버는 빈 문자열이 아니라 **키를 뺀다**(실측 `includeBody=0`).

    한 행도 본문을 안 주면 그 게시판 본문이 전량 유실된다 — 조용히 성공으로 흘리면 안 된다.
    """
    with pytest.raises(ParseError, match="includeBody"):
        csu.parse_list(_payload(None, with_body_key=False), source)


def test_one_row_without_a_body_key_is_normal(source: SourceConfig) -> None:
    """⚠️⚠️ **행 단위로 판정하면 정상 공고 하나가 게시판 전체를 죽인다.**

    이 API는 값이 null인 키를 응답에서 뺀다(실측 40건 중 1건 — 이스탄불한인교회). 그 공고는
    내용을 첨부와 `properties`에 담고 있어 정상이다. 처음에 행 단위로 넣었다가 실측에서
    바로 드러났다(2026-08-05).
    """
    rows = json.loads(_payload("<p>본문</p>"))
    rows["body"]["list"].append(
        {
            "id": 1117810,
            "title": "튀르키예 이스탄불한인교회에서 담임목사님을 청빙합니다.",
            "registered_date": "2026-08-03 21:50:00",
            "attachment_count": 1,
            "attachment_list": [
                {"original_filename": "poster.jpeg", "url": "board/202608//f1e464d7.jpeg"}
            ],
            "properties": {"church_name": "이스탄불 한인교회", "order_name": "초교파"},
        }
    )
    refs = csu.parse_list(json.dumps(rows), source)
    assert len(refs) == 2
    raw = csu.parse_detail("", refs[1])
    assert [a.name for a in raw.attachments] == ["poster.jpeg"]


def test_an_empty_posting_is_a_fact_not_a_failure(source: SourceConfig) -> None:
    """내용이 전무한 글도 저장한다 — 실패로 두면 매 실행 다시 받고 매번 실패로 보고된다.

    셀렉터·파라미터가 깨진 경우는 `_require_body_field`(페이지 전량)와 `collect`의 소스 단위
    전량 빈 내용 판정이 잡는다.
    """
    refs = csu.parse_list(_payload("<p>&nbsp;</p>"), source)
    raw = csu.parse_detail("", refs[0])
    assert raw.raw_text == ""
    assert raw.attachments == ()


def test_an_empty_body_is_not_mistaken_for_a_broken_parameter(source: SourceConfig) -> None:
    """⚠️ 빈 본문과 "`includeBody`가 깨졌다"는 **다른 사건**이다.

    빈 문자열로 판정하면 첨부만 있는 정상 공고 하나가 **게시판 전체를 실패시킨다**(목록 단계
    실패는 소스 격리로 올라간다). 서버는 파라미터가 안 먹을 때 키를 빼므로 키 유무로 가른다.
    """
    refs = csu.parse_list(_payload("", attachment="공고문.pdf"), source)
    raw = csu.parse_detail("", refs[0])
    assert [a.name for a in raw.attachments] == ["공고문.pdf"]
    assert raw.raw_text == ""
