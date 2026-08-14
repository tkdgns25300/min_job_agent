"""구조화 직전에 모델로 보낼 **그림·문서 바이트**를 모은다(SPEC §3 이미지 공고).

공고 내용이 본문이 아니라 포스터에 있는 게시판이 있다 — 실측 237건(7.4%)이 이미지를 갖고
있고 그중 116건은 본문이 아예 없다. 텍스트만 보내면 그 공고들은 "내용 없음"으로 읽힌다.
**PDF도 같은 처지다** — 첨부 22건 중 2건은 본문이 0자·24자이고 공고문이 PDF에만 있다.
Gemini는 PDF를 직접 읽으므로 그림과 나눠 다룰 이유가 없다(HWP는 못 읽어 이름만 간다).

**전송은 하지 않는다** — `fetch/client.py`를 지난다(CLAUDE.md: 모든 HTTP는 그 층). 여기는
**무엇을 가져올지 고르고 실패를 사유로 바꾸는** 일만 한다.

⚠️ **실패는 예외가 아니라 사유다.** 그림을 못 받았다고 공고를 통째로 실패시키면 텍스트만으로
충분한 공고까지 재시도에 걸린다 — 텍스트로 진행하고 `confidence`를 낮춘다(SPEC §3).
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from typing import Final, Protocol
from urllib.parse import unquote_to_bytes

from PIL import Image

from minjob_ingest.fetch.client import FetchError, SourceClient
from minjob_ingest.models import SourceData

_LOG = logging.getLogger(__name__)

#: 한 공고에서 모델에 보낼 파일 수 상한. 포스터는 1~3장이면 내용이 다 담긴다 — 갤러리형
#: 게시판에서 수십 장을 보내면 토큰만 쓰고 판단이 흐려진다.
MAX_MEDIA_PER_POSTING: Final = 4

#: 파일 하나의 크기 상한(바이트). 넘으면 보내지 않는다 — 요청 크기·비용이 급격히 는다.
MAX_MEDIA_BYTES: Final = 8 * 1024 * 1024

#: 한 공고에서 모델에 실어 보낼 **합계** 상한. ⚠️ 장당 상한만 두면 4장이면 32MB가 되어
#: 인라인 데이터 총량 한도를 넘고, 그 호출은 실패한 뒤 재시도 예산까지 태운다.
MAX_TOTAL_MEDIA_BYTES: Final = 15 * 1024 * 1024

#: 이보다 작으면 아이콘·구분선이다(실측 게시판 장식 이미지가 1KB 미만).
MIN_MEDIA_BYTES: Final = 2 * 1024

#: `data:` URI 시작.
_DATA_SCHEME: Final = "data:"

_JPEG: Final = "image/jpeg"

#: 인쇄용 JPEG. Vertex가 400으로 거절한다(`screen_ready`).
_CMYK_CHANNELS: Final = 4

#: 다시 인코딩할 때의 화질. 포스터는 글씨가 내용이라 낮추면 못 읽는다.
_REENCODE_QUALITY: Final = 90

#: JPEG 조각을 걷는 데 필요한 최소 바이트(마커 2 + 길이 2 + 프레임 머리 5).
_SEGMENT_HEADER: Final = 9

#: 길이 칸이 없는 마커 — SOI·EOI.
_STANDALONE_MARKERS: Final = frozenset({0xD8, 0xD9})

#: 재시작 마커(RST0~RST7)도 길이가 없다.
_RESTART_MARKERS: Final = range(0xD0, 0xD8)

#: 프레임 머리(SOF) 안에서 채널 수가 있는 자리(마커 2 + 길이 2 + 정밀도 1 + 높이 2 + 너비 2).
_CHANNELS_OFFSET: Final = 9

#: 프레임 머리(SOF). 허프만·산술·손실없음 변형을 모두 담는다.
_FRAME_MARKERS: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)

#: 게시판에 요청할 수 있는 스킴. ⚠️ 본문에 `file:///C:\...`가 섞인 공고가 있다(실측 8건 —
#: HWP에서 붙여넣은 흔적). 요청하면 전송 층이 죽고 그 실행 내내 robots를 못 읽는다.
_FETCHABLE_SCHEMES: Final = ("http://", "https://")

#: 파일 앞머리 → 실제 형식. ⚠️ **헤더를 믿지 않는다** — 게시판 `download.php`는 첨부를
#: 대개 `application/octet-stream`으로 준다(확장자가 `.jpg`여도). 헤더로 고르면 보낼 수 있는
#: 파일을 "그림이 아님"으로 버리고, 반대로 HTML 오류 페이지를 그림으로 실어 보낸다.
#: config encoding이 서버 헤더를 이기는 것과 같은 이유다(CLAUDE.md fetch 층).
_MAGIC: Final = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


@dataclass(frozen=True, slots=True)
class Media:
    """모델에 넘길 파일 하나 — 그림 또는 PDF."""

    media_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class MediaSet:
    """한 공고에서 모은 결과. **실패도 함께 돌려준다** — 판정이 그걸 보고 낮춰야 한다."""

    items: tuple[Media, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """하나도 놓치지 않았나. 놓쳤으면 텍스트만으로 판정한 것이다."""
        return not self.failures


class MediaSource(Protocol):
    """파이프라인이 그림·문서에 대해 아는 전부.

    구상 클래스가 아니라 프로토콜인 이유: 테스트가 게시판에 요청하지 않아야 한다.
    """

    def media_for(self, record: SourceData) -> MediaSet: ...


def wanted_urls(record: SourceData) -> tuple[str, ...]:
    """이 공고에서 모델에 보낼 URL들. **본문 인라인 먼저, 첨부는 그다음.**

    인라인이 공고 내용이고 첨부는 서식·양식인 경우가 많다 — 상한에 걸릴 때 남겨야 할 쪽이
    인라인이다.

    ⚠️ **같은 URL은 한 번만**이다. 인라인과 첨부에 같은 포스터를 올린 게시판이 있다(SJS 9건) —
    두 번 보내면 토큰을 두 배로 쓰고 요청도 하나 더 나간다.
    """
    seen: dict[str, None] = {}
    for url in (*record.image_urls, *attachment_urls(record)):
        seen.setdefault(url, None)
    return tuple(seen)[:MAX_MEDIA_PER_POSTING]


def attachment_urls(record: SourceData) -> tuple[str, ...]:
    """첨부 중 모델이 읽을 수 있는 것. **그림 먼저, PDF는 그다음.**

    ⚠️ 순서가 규칙이다 — 상한(4개)에 걸리면 뒤가 잘린다. 포스터가 공고 내용이고 PDF는
    같은 내용을 옮긴 공고문이거나 지원 양식인 경우가 많다.

    HWP·DOCX는 목록에 없다 — Gemini가 못 읽어 이름만 프롬프트로 간다(실측 첨부 304건 중
    대부분이 이력서 양식이다).

    **상세 선요청이 필요한 대상**이기도 하다(아래 `_prime`).
    """
    images = tuple(item.url for item in record.attachments if item.is_image)
    documents = tuple(item.url for item in record.attachments if item.is_pdf)
    return images + documents


def sniff_media_type(data: bytes) -> str | None:
    """앞머리가 말하는 실제 형식. 우리가 보낼 수 없는 형식이면 `None`."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for prefix, media_type in _MAGIC:
        if data.startswith(prefix):
            return media_type
    return None


def as_media(header_media_type: str, data: bytes) -> Media:
    """받은 바이트를 `Media`로. **앞머리가 헤더를 이긴다**(`_MAGIC` 참조)."""
    media_type = sniff_media_type(data) or header_media_type
    return Media(media_type=media_type, data=screen_ready(media_type, data))


def screen_ready(media_type: str, data: bytes) -> bytes:
    """인쇄용(CMYK) JPEG를 화면용(RGB)으로 바꾼다. 나머지 바이트는 **손대지 않는다**.

    ⚠️ Vertex는 4채널 JPEG를 400 INVALID_ARGUMENT로 거절한다(실측 2026-08-14 ·
    `PCKWORLD/1545`·`1539` — 인쇄소에서 받은 포스터를 그대로 올린 것이다). `PCKWORLD`는
    본문이 비어 있어 그림이 곧 공고라, 못 읽으면 **그 공고가 통째로 사라진다**.

    ⚠️ **필요할 때만 다시 만든다.** 전부 다시 인코딩하면 화질이 떨어지고 느려지는데,
    실측 299개 중 바꿔야 하는 것은 2개였다.
    """
    if media_type != _JPEG or jpeg_channels(data) != _CMYK_CHANNELS:
        return data
    try:
        with Image.open(BytesIO(data)) as image:
            buffer = BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=_REENCODE_QUALITY)
    except (OSError, ValueError) as err:
        # 바꾸지 못해도 원본을 보낸다 — 거절당하겠지만 사유가 리포트에 남는다.
        _LOG.warning("CMYK 그림을 바꾸지 못해 원본 그대로 보낸다 (%s)", err)
        return data
    return buffer.getvalue()


def jpeg_channels(data: bytes) -> int | None:
    """JPEG의 색 채널 수(3=RGB · 4=CMYK). JPEG가 아니거나 못 읽으면 `None`.

    ⚠️ 앞머리만 훑는 것으로는 부족하다 — ICC 프로파일이 64KB짜리 조각 여럿으로 붙어 색
    정보가 130KB 뒤로 밀린 파일이 있었다(`PCKWORLD/1545`). 조각 길이를 따라 끝까지 걷는다.
    """
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index < len(data) - _SEGMENT_HEADER:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if (
            marker in _STANDALONE_MARKERS
            or _RESTART_MARKERS.start <= marker < _RESTART_MARKERS.stop
        ):
            index += 2
            continue
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        if marker in _FRAME_MARKERS:
            return data[index + _CHANNELS_OFFSET]
        index += 2 + length
    return None


def decode_data_uri(uri: str) -> Media:
    """`data:image/png;base64,...`를 바이트로.

    ⚠️ **fetch하지 않는다.** URL로 취급해 요청하면 그 게시판 전체가 실패한다(CALVIN 26건 —
    본문 텍스트가 0자이고 내용이 인라인 `data:` 한 장이다 · SPEC §6).
    """
    header, _, payload = uri.partition(",")
    if not payload:
        raise ValueError("data: URI에 내용이 없음")
    media_type = header[len(_DATA_SCHEME) :].split(";")[0] or "application/octet-stream"
    if ";base64" not in header:
        # ⚠️ `unquote`가 아니라 `unquote_to_bytes` — 문자열로 풀면 `%89` 같은 바이트가
        #    U+FFFD로 바뀌어 그림이 깨진다(비-base64 `data:`는 드물지만 조용히 망가진다).
        return as_media(media_type, unquote_to_bytes(payload))
    try:
        return as_media(media_type, base64.b64decode(payload, validate=True))
    except (binascii.Error, ValueError) as err:
        raise ValueError(f"base64가 아님: {err}") from err


def unusable_reason(item: Media, url: str) -> str | None:
    """보낼 수 없는 이유. 보낼 만하면 `None` — 이름이 불리언처럼 읽히지 않게 사유로 둔다."""
    size = len(item.data)
    if size < MIN_MEDIA_BYTES:
        return f"{_short(url)}: 너무 작아 장식으로 봄 ({size}B)"
    if size > MAX_MEDIA_BYTES:
        return f"{_short(url)}: 너무 큼 ({size // 1024}KB)"
    # ⚠️ 선언된 형식이 아니라 **앞머리**로 판정한다 — 게시판이 그림을 octet-stream으로 주고
    #    오류 페이지를 image/jpeg로 주기도 한다. 둘 다 헤더를 믿으면 조용히 잘못된다.
    if sniff_media_type(item.data) is None:
        return f"{_short(url)}: 보낼 수 없는 형식 ({item.media_type or '형식 없음'})"
    return None


@dataclass
class BoardMediaSource:
    """게시판에서 그림·PDF를 받아온다.

    ⚠️ **소스별 클라이언트를 재사용한다.** 새로 만들면 요청 간격(1.5s)이 초기화되고 세션
    쿠키를 매번 다시 받는다 — 한 호스트에 요청 1개라는 원칙이 깨진다(SPEC §3).

    ⚠️ **잠금장치가 없는 이유**: 구조화는 게시판 하나를 스레드 하나가 통째로 맡으므로
    (`pipeline/structure.py`) 한 `source_key`의 클라이언트·세션 표시를 두 스레드가 함께
    만지는 일이 없다. 공고 단위로 병렬화하면 그 전제가 깨진다 — 그때는 여기와
    `SourceClient`의 요청 간격에 잠금이 필요하다.

    ⚠️ **첨부를 받기 전에만** 그 공고 상세를 같은 세션에서 먼저 GET한다. 그누보드 계열
    4곳(`HAPSHIN`·`HTUS`·`PCK`·`SUNGKYUL`)은 `wr_id`별 세션 표시를 남기고 `download.php`가
    그걸 검사한다 — 안 하면 파일 대신 `잘못된 접근입니다` HTML이 온다(2026-08-05 실측).
    본문 인라인 그림에는 필요 없다 — 전부에 걸면 그림 있는 237건마다 요청이 하나씩 는다.
    """

    open_client: Callable[[str], SourceClient]
    _clients: dict[str, SourceClient] = field(default_factory=dict)
    _primed: set[str] = field(default_factory=set)

    def media_for(self, record: SourceData) -> MediaSet:
        items: list[Media] = []
        failures: list[str] = []
        attachments = frozenset(attachment_urls(record))
        budget = MAX_TOTAL_MEDIA_BYTES
        for url in wanted_urls(record):
            try:
                item = self._one(record, url, needs_session=url in attachments)
            except (FetchError, ValueError) as err:
                failures.append(f"{_short(url)}: {err}")
                continue
            reason = unusable_reason(item, url)
            if reason is not None:
                failures.append(reason)
                continue
            # ⚠️ 받고 나서 다시 잰다 — 앞의 예산 검사는 "받을 가치가 있나"이고, 이건
            #    "실어 보낼 수 있나"다. 앞만 보면 마지막 한 장이 상한을 넘겨 실린다.
            if len(item.data) > budget:
                failures.append(f"{_short(url)}: 한 요청에 담을 크기를 넘어 건너뜀")
                continue
            items.append(item)
            budget -= len(item.data)
        return MediaSet(items=tuple(items), failures=tuple(failures))

    def close(self) -> None:
        """⚠️ 하나가 실패해도 나머지를 닫는다 — 중간에 멈추면 연결이 남아 실행이 안 끝난다."""
        for client in self._clients.values():
            try:
                client.close()
            except Exception as err:
                _LOG.warning("클라이언트 닫기 실패 — 나머지는 계속 닫는다 (%s)", err)
        self._clients.clear()
        self._primed.clear()

    def _one(self, record: SourceData, url: str, *, needs_session: bool) -> Media:
        if url.startswith(_DATA_SCHEME):
            return decode_data_uri(url)
        if not url.startswith(_FETCHABLE_SCHEMES):
            raise ValueError("가져올 수 없는 주소다 (게시판이 아닌 로컬 경로)")
        client = self._client_for(record.source_key)
        if needs_session:
            self._prime(client, record)
        received = client.get_bytes(url)
        return as_media(received.media_type, received.data)

    def _client_for(self, source_key: str) -> SourceClient:
        client = self._clients.get(source_key)
        if client is None:
            client = self.open_client(source_key)
            self._clients[source_key] = client
        return client

    def _prime(self, client: SourceClient, record: SourceData) -> None:
        """공고 상세를 한 번 GET해 세션 표시를 남긴다(공고당 1회)."""
        if record.source_url in self._primed:
            return
        self._primed.add(record.source_url)
        try:
            client.get(record.source_url)
        except FetchError as err:
            # 상세를 못 읽어도 첨부 요청은 해본다 — 세션이 필요 없는 게시판이 대부분이다.
            _LOG.info("%s 상세 선요청 실패 — 그대로 진행 (%s)", record.label, err)


@contextmanager
def board_media(open_client: Callable[[str], SourceClient]) -> Iterator[BoardMediaSource]:
    """클라이언트를 반드시 닫는다 — 열어둔 연결이 남으면 실행이 끝나지 않는다."""
    source = BoardMediaSource(open_client=open_client)
    try:
        yield source
    finally:
        source.close()


def _short(url: str) -> str:
    """사유 메시지에 넣을 짧은 이름. `data:` URI는 통째로 넣으면 리포트를 덮는다."""
    if url.startswith(_DATA_SCHEME):
        return "data: 이미지"
    tail = url.rsplit("/", 1)[-1]
    return tail[:60] or url[:60]


def failure_note(media: MediaSet, urls: Sequence[str]) -> str | None:
    """리포트에 남길 한 줄. 놓친 것이 없으면 `None`."""
    if media.is_complete:
        return None
    return f"첨부 {len(urls) - len(media.items)}/{len(urls)}개를 못 읽음: " + " · ".join(
        media.failures[:2]
    )
