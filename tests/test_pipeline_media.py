"""그림·PDF 확보 테스트 — 무엇을 가져오고 무엇을 버리는가.

게시판에 요청하지 않는다. 전송은 가짜 클라이언트로 바꾼다.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import Final

import pytest
from PIL import Image

from minjob_ingest.clock import KST
from minjob_ingest.fetch.client import Binary, FetchError
from minjob_ingest.models import Attachment, SourceData, new_id
from minjob_ingest.pipeline.media import (
    MAX_MEDIA_BYTES,
    MAX_MEDIA_PER_POSTING,
    MAX_TOTAL_MEDIA_BYTES,
    MIN_MEDIA_BYTES,
    BoardMediaSource,
    Media,
    MediaSet,
    as_media,
    attachment_urls,
    board_media,
    decode_data_uri,
    failure_note,
    jpeg_channels,
    sniff_media_type,
    unusable_reason,
    wanted_urls,
)

_NOW: Final = datetime(2026, 8, 11, 9, 0, tzinfo=KST)

#: ⚠️ **앞머리가 진짜여야 한다** — 판정이 헤더가 아니라 바이트를 본다(`sniff_media_type`).
_PNG: Final = b"\x89PNG\r\n\x1a\n" + b"x" * MIN_MEDIA_BYTES
_PDF: Final = b"%PDF-1.7\n" + b"x" * MIN_MEDIA_BYTES


def _source_data(
    *,
    image_urls: tuple[str, ...] = (),
    attachments: tuple[Attachment, ...] = (),
    source_key: str = "CALVIN",
) -> SourceData:
    return SourceData(
        source_key=source_key,
        external_id="9",
        source_url=f"https://example.kr/{source_key.lower()}/9",
        title="포스터 공고",
        run_id=new_id(),
        fetched_at=_NOW,
        raw_text="",
        image_urls=image_urls,
        attachments=attachments,
    )


@dataclass
class _FakeClient:
    """`SourceClient` 대역. 요청한 URL을 기록하고 정해둔 결과를 돌려준다."""

    result: Binary | Exception = field(
        default_factory=lambda: Binary(url="u", media_type="image/png", data=_PNG)
    )
    got: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    closed: bool = False

    def get_bytes(self, url: str) -> Binary:
        self.got.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def get(self, url: str) -> object:
        self.pages.append(url)
        return object()

    def close(self) -> None:
        self.closed = True


def _source_with(client: _FakeClient) -> BoardMediaSource:
    def open_client(_key: str) -> _FakeClient:
        return client

    return BoardMediaSource(open_client=open_client)  # type: ignore[arg-type]


# ── 무엇을 가져올까 ──────────────────────────────────────────────


def test_inline_images_come_before_attachments() -> None:
    """인라인이 공고 내용이고 첨부는 서식인 경우가 많다 — 상한에 걸리면 인라인을 남긴다."""
    record = _source_data(
        image_urls=("https://e.kr/body.png",),
        attachments=(Attachment(name="서식.png", url="https://e.kr/form.png"),),
    )

    assert wanted_urls(record) == ("https://e.kr/body.png", "https://e.kr/form.png")


def test_non_image_attachments_are_left_out() -> None:
    record = _source_data(
        attachments=(
            Attachment(name="공고문.hwp", url="https://e.kr/a.hwp"),
            Attachment(name="포스터.jpg", url="https://e.kr/a.jpg"),
        )
    )

    assert wanted_urls(record) == ("https://e.kr/a.jpg",)


def test_a_gallery_posting_is_capped() -> None:
    """포스터는 1~3장이면 내용이 다 담긴다 — 수십 장은 토큰만 쓰고 판단을 흐린다."""
    record = _source_data(image_urls=tuple(f"https://e.kr/{n}.png" for n in range(10)))

    assert len(wanted_urls(record)) == MAX_MEDIA_PER_POSTING


# ── data: URI ────────────────────────────────────────────────────


def test_a_data_uri_is_decoded_not_fetched() -> None:
    """⚠️ URL로 취급해 요청하면 그 게시판 전체가 실패한다(CALVIN 26건)."""
    uri = "data:image/png;base64," + base64.b64encode(b"bytes").decode()
    client = _FakeClient()

    image = _source_with(client).media_for(_source_data(image_urls=(uri,)))

    assert client.got == [], "data: 는 요청하지 않는다"
    assert image.failures, "5바이트라 너무 작다고 걸러진다"


def test_a_data_uri_carries_its_media_type() -> None:
    """⚠️ 선언이 아니라 **앞머리**가 형식을 정한다 — `data:image/jpeg`라고 써두고 PNG를 실은
    본문이 있다. 선언을 그대로 믿고 보내면 모델이 형식을 잘못 안다."""
    uri = "data:image/jpeg;base64," + base64.b64encode(_PNG).decode()

    assert decode_data_uri(uri) == Media(media_type="image/png", data=_PNG)


def test_a_broken_data_uri_is_a_reason_not_a_crash() -> None:
    result = _source_with(_FakeClient()).media_for(
        _source_data(image_urls=("data:image/png;base64,!!not-base64!!",))
    )

    assert result.items == ()
    assert "data: 이미지" in result.failures[0]


# ── 쓸 만한 그림인가 ─────────────────────────────────────────────


def test_a_tiny_image_is_treated_as_decoration() -> None:
    """게시판 장식 이미지가 1KB 미만이다(실측)."""
    assert unusable_reason(Media(media_type="image/png", data=b"x" * 10), "icon.png") is not None


def test_an_oversized_image_is_skipped() -> None:
    huge = Media(media_type="image/png", data=b"x" * (MAX_MEDIA_BYTES + 1))

    assert "너무 큼" in (unusable_reason(huge, "big.png") or "")


def test_a_file_that_is_not_an_image_is_skipped() -> None:
    """⚠️ 헤더가 `image/jpeg`여도 내용이 HTML이면 버린다 — 게시판이 접근 거부 페이지를
    그림 헤더로 주는 일이 있다. 그대로 보내면 모델에 쓰레기가 조용히 섞인다."""
    html = b"<!DOCTYPE html><p>" + b"x" * MIN_MEDIA_BYTES

    assert "보낼 수 없는 형식" in (
        unusable_reason(Media(media_type="image/jpeg", data=html), "a") or ""
    )


def test_a_good_image_passes() -> None:
    assert unusable_reason(Media(media_type="image/png", data=_PNG), "poster.png") is None


def test_a_pdf_passes() -> None:
    """⚠️ 공고문이 PDF에만 있는 공고가 있다(실측 2건 — 본문 0자·24자). Gemini는 PDF를 읽는다."""
    assert unusable_reason(Media(media_type="application/pdf", data=_PDF), "공고문.pdf") is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_PNG, "image/png"),
        (_PDF, "application/pdf"),
        (b"\xff\xd8\xff\xe0" + b"x" * 100, "image/jpeg"),
        (b"GIF89a" + b"x" * 100, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 100, "image/webp"),
        (b"BM" + b"x" * 100, "image/bmp"),
        (b"<!DOCTYPE html>", None),
        (b"", None),
    ],
    ids=["png", "pdf", "jpeg", "gif", "webp", "bmp", "html", "빈 값"],
)
def test_the_real_format_is_read_from_the_bytes(data: bytes, expected: str | None) -> None:
    """⚠️ 게시판 `download.php`는 첨부를 대개 `application/octet-stream`으로 준다.

    헤더로 고르면 보낼 수 있는 파일을 "그림이 아님"으로 버린다 — 앞머리가 정본이다.
    """
    assert sniff_media_type(data) == expected


def test_the_declared_type_loses_to_the_bytes() -> None:
    """`octet-stream`으로 온 PNG를 그대로 보내면 모델이 형식을 모른다."""
    client = _FakeClient(result=Binary(url="u", media_type="application/octet-stream", data=_PNG))

    result = _source_with(client).media_for(_source_data(image_urls=("https://e.kr/1.png",)))

    assert [item.media_type for item in result.items] == ["image/png"]


def test_pdf_attachments_come_after_images() -> None:
    """⚠️ 순서가 규칙이다 — 상한(4개)에 걸리면 뒤가 잘린다. 포스터가 공고 내용이고
    PDF는 같은 내용을 옮긴 공고문이거나 지원 양식인 경우가 많다."""
    record = _source_data(
        attachments=(
            Attachment(name="공고문.pdf", url="https://e.kr/a.pdf"),
            Attachment(name="포스터.png", url="https://e.kr/b.png"),
            Attachment(name="이력서양식.hwp", url="https://e.kr/c.hwp"),
        )
    )

    assert attachment_urls(record) == ("https://e.kr/b.png", "https://e.kr/a.pdf")


def test_documents_the_model_cannot_read_are_left_out() -> None:
    """HWP·DOCX는 이름만 프롬프트로 간다 — 바이트를 보내면 요청만 낭비한다."""
    record = _source_data(
        attachments=(
            Attachment(name="이력서.hwp", url="https://e.kr/a.hwp"),
            Attachment(name="양식.docx", url="https://e.kr/b.docx"),
        )
    )

    assert attachment_urls(record) == ()


# ── 게시판에서 받아오기 ──────────────────────────────────────────


def test_the_detail_page_is_requested_before_attachments() -> None:
    """⚠️ 그누보드 4곳은 `wr_id`별 세션 표시가 없으면 파일 대신 HTML을 준다(실측)."""
    record = _source_data(
        attachments=(Attachment(name="포스터.png", url="https://e.kr/download/1"),),
        source_key="HAPSHIN",
    )
    client = _FakeClient()

    _source_with(client).media_for(record)

    assert client.pages == [record.source_url], "상세를 먼저 GET 해야 한다"
    assert client.got == ["https://e.kr/download/1"]


def test_inline_images_do_not_need_the_detail_page() -> None:
    """⚠️ 전부에 걸면 그림 있는 237건마다 요청이 하나씩 는다 — 세션은 첨부에만 필요하다."""
    record = _source_data(image_urls=("https://e.kr/1.png", "https://e.kr/2.png"))
    client = _FakeClient()

    _source_with(client).media_for(record)

    assert client.pages == []
    assert len(client.got) == 2


def test_the_detail_page_is_requested_once_per_posting() -> None:
    record = _source_data(
        attachments=(
            Attachment(name="1.png", url="https://e.kr/d/1"),
            Attachment(name="2.png", url="https://e.kr/d/2"),
        ),
        source_key="HAPSHIN",
    )
    client = _FakeClient()
    source = _source_with(client)

    source.media_for(record)
    source.media_for(record)

    assert len(client.pages) == 1


def test_the_total_image_budget_is_bounded() -> None:
    """⚠️ 장당 상한만 두면 4장이 32MB가 되어 한 요청에 못 싣고, 그 호출은 재시도까지 태운다."""
    big = MAX_MEDIA_BYTES - 1
    body = _PNG + b"x" * (big - len(_PNG))
    client = _FakeClient(result=Binary(url="u", media_type="image/png", data=body))
    record = _source_data(image_urls=tuple(f"https://e.kr/{n}.png" for n in range(4)))

    result = _source_with(client).media_for(record)

    assert sum(len(item.data) for item in result.items) <= MAX_TOTAL_MEDIA_BYTES
    assert any("크기를 넘어" in reason for reason in result.failures)


def test_closing_continues_after_one_client_fails() -> None:
    """닫기 하나가 실패해서 나머지가 남으면 실행이 끝나지 않는다."""

    @dataclass
    class _Stubborn(_FakeClient):
        def close(self) -> None:
            raise RuntimeError("닫기 실패")

    stubborn, healthy = _Stubborn(), _FakeClient()
    clients: dict[str, _FakeClient] = {"CALVIN": stubborn, "YTUS": healthy}

    def open_client(key: str) -> _FakeClient:
        return clients[key]

    source = BoardMediaSource(open_client=open_client)  # type: ignore[arg-type]
    source.media_for(_source_data(image_urls=("https://e.kr/1.png",)))
    source.media_for(_source_data(image_urls=("https://e.kr/2.png",), source_key="YTUS"))

    source.close()

    assert healthy.closed


def test_the_context_manager_closes_even_on_failure() -> None:
    client = _FakeClient()

    def open_client(_key: str) -> _FakeClient:
        return client

    with pytest.raises(RuntimeError, match="중단"), board_media(open_client) as source:  # type: ignore[arg-type]
        source.media_for(_source_data(image_urls=("https://e.kr/1.png",)))
        raise RuntimeError("중단")

    assert client.closed


def test_a_fetch_failure_becomes_a_reason_not_an_exception() -> None:
    """⚠️ 그림 실패로 공고를 통째 실패시키면 텍스트만으로 충분한 것까지 재시도에 걸린다."""
    client = _FakeClient(result=FetchError("HTTP 404"))

    result = _source_with(client).media_for(_source_data(image_urls=("https://e.kr/p.png",)))

    assert result.items == ()
    assert "p.png" in result.failures[0]
    assert not result.is_complete


def test_clients_are_reused_across_postings() -> None:
    """⚠️ 새로 만들면 요청 간격이 초기화되고 세션을 매번 다시 받는다(SPEC §3)."""
    opened: list[str] = []
    client = _FakeClient()

    def open_client(key: str) -> _FakeClient:
        opened.append(key)
        return client

    source = BoardMediaSource(open_client=open_client)  # type: ignore[arg-type]

    source.media_for(_source_data(image_urls=("https://e.kr/1.png",)))
    source.media_for(_source_data(image_urls=("https://e.kr/2.png",)))

    assert opened == ["CALVIN"]


def test_closing_releases_every_client() -> None:
    client = _FakeClient()
    source = _source_with(client)
    source.media_for(_source_data(image_urls=("https://e.kr/1.png",)))

    source.close()

    assert client.closed


# ── 리포트 ───────────────────────────────────────────────────────


def test_no_note_when_nothing_was_missed() -> None:
    complete = MediaSet(items=(Media(media_type="image/png", data=_PNG),))

    assert failure_note(complete, ("https://e.kr/1.png",)) is None


def test_the_note_counts_what_was_missed() -> None:
    partial = MediaSet(items=(Media(media_type="image/png", data=_PNG),), failures=("2.png: 404",))

    note = failure_note(partial, ("https://e.kr/1.png", "https://e.kr/2.png"))

    assert note is not None and "1/2개를 못 읽음" in note


# ── 인쇄용(CMYK) 그림 ────────────────────────────────────────────


def _jpeg(mode: str, *, size: tuple[int, int] = (40, 30)) -> bytes:
    buffer = BytesIO()
    Image.new(mode, size, color=None).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_a_print_ready_poster_is_converted_for_the_screen() -> None:
    """⚠️ Vertex는 4채널 JPEG를 400으로 거절한다 — 실측 PCKWORLD/1545·1539.

    `PCKWORLD`는 본문이 비어 포스터가 곧 공고라, 못 읽으면 그 공고가 통째로 사라진다.
    """
    printed = _jpeg("CMYK")
    assert jpeg_channels(printed) == 4

    item = as_media("image/jpeg", printed)

    assert jpeg_channels(item.data) == 3
    assert item.media_type == "image/jpeg"


def test_an_ordinary_photo_is_passed_through_untouched() -> None:
    """⚠️ 전부 다시 인코딩하면 화질이 떨어진다 — 실측 299개 중 바꿀 것은 2개였다."""
    photo = _jpeg("RGB")

    item = as_media("image/jpeg", photo)

    assert item.data == photo


def test_a_png_is_never_re_encoded() -> None:
    """JPEG가 아닌 것은 채널을 세지도 않는다 — CMYK PNG는 존재하지 않는다."""
    buffer = BytesIO()
    Image.new("RGB", (40, 30)).save(buffer, format="PNG")
    png = buffer.getvalue()

    assert as_media("image/png", png).data == png


def test_bytes_inside_a_segment_are_not_mistaken_for_a_frame_header() -> None:
    """⚠️ 조각 **길이를 따라** 걸어야 한다 — 안을 훑으면 프로파일·주석에 우연히 든 바이트를
    프레임 머리로 읽는다.

    실측 `PCKWORLD/1545`는 ICC 프로파일이 64KB 조각 여럿으로 붙어 색 정보가 130KB 뒤에
    있었다. 여기서는 주석 조각 안에 4채널짜리 가짜 머리를 심어 같은 상황을 만든다.
    """
    photo = _jpeg("RGB")
    fake_frame = b"\xff\xc0\x00\x11\x08\x00\x10\x00\x10\x04"
    comment = b"\xff\xfe" + (len(fake_frame) + 2).to_bytes(2, "big") + fake_frame
    booby_trapped = photo[:2] + comment + photo[2:]

    assert jpeg_channels(booby_trapped) == 3


def test_bytes_that_are_not_a_jpeg_have_no_channel_count() -> None:
    assert jpeg_channels(b"%PDF-1.4 ...") is None
    assert jpeg_channels(b"") is None
