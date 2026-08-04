"""어댑터 공통 계약 — 게시판별 파일이 이것만 채운다(SPEC §10).

**어댑터는 파싱만 한다.** 네트워크·UA·인코딩·재시도·간격은 fetch 층이 이미 흡수했고, 여기
들어오는 것은 **디코드까지 끝난 문자열**이다. 그래서 어댑터 테스트는 fixture만으로 돌고
네트워크를 타지 않는다(가드레일 #7·#10).

호출 흐름: `list_page_url` → (fetch) → `parse_list` → 원장 대조 → (fetch) → `parse_detail`.
URL 조립과 파싱은 순수 함수이므로 게시판을 두드리지 않고 검증할 수 있다.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Final
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from minjob_ingest.models import Attachment, JsonValue, as_json_value

#: `lxml`을 쓴다 — 깨진 마크업(닫히지 않은 `<td>` 등)에서 표준 파서보다 관대하다.
HTML_PARSER: Final = "lxml"

#: 줄바꿈으로 취급할 블록 요소. HWP에서 붙여넣은 본문은 `<span>`으로 조각나 있어
#: 이걸 하지 않으면 raw_text가 한 줄로 뭉치거나 토큰마다 줄이 바뀐다.
#: ⚠️ `td`·`th`가 없으면 `<td>교회명</td><td>도원교회</td>` → `"교회명도원교회"`로 붙는다.
#: YTUS 본문엔 표가 없지만 이 파일은 31곳 공용이고 표 양식 본문이 흔하다.
_BLOCK_TAGS: Final = (
    "p",
    "div",
    "br",
    "tr",
    "td",
    "th",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "table",
)

#: `detail_pattern`의 치환 자리(레지스트리와 같은 값).
_ID_PLACEHOLDER: Final = "{id}"

_SPACES: Final = re.compile("[ \\t\\u00a0]+")
_BLANK_LINES: Final = re.compile(r"\n{3,}")


class ParseError(Exception):
    """게시판 HTML이 예상과 다를 때. 어댑터의 **유일한** 실패 신호다.

    ⚠️ **빈 결과로 흘리지 않는다.** 목록 테이블이 사라진 것(사이트 개편)과 공고가 0건인 것은
    다르다 — 전자를 빈 리스트로 돌려주면 `source_health`에 "정상인데 0건"으로 남아
    셀렉터가 깨진 사실을 아무도 모른다(SPEC §7 소프트 실패).
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class PostingRef:
    """목록 한 행에서 얻은 것. **상세를 요청할지 판단하는 데 필요한 최소 정보.**

    `posted_on`은 백필 컷오프(`--months N`)에 쓴다 — 구조화 전이라 `posted_at`이 없으므로
    목록의 게시일이 유일한 기준이다(SPEC §4). 목록에 날짜가 없는 소스가 있어 `None`을 허용하고,
    그런 소스는 페이지 수(`--pages`)로 범위를 정한다.
    """

    #: 그 게시판 안에서 이 글을 특정하는 값. 유일성은 어댑터 책임(SPEC §10).
    external_id: str
    #: 상세 절대 URL.
    url: str
    title: str
    posted_on: date | None = None
    #: 게시판 원필드(작성자·조회수·표시번호 등). `source_data.raw_meta`로 들어간다.
    list_meta: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "external_id", _require_text(self.external_id, "external_id"))
        object.__setattr__(self, "url", _require_text(self.url, "url"))
        object.__setattr__(self, "title", _require_text(self.title, "title"))
        object.__setattr__(self, "list_meta", _frozen_meta(self.list_meta))


@dataclass(frozen=True, slots=True, kw_only=True)
class RawPosting:
    """상세에서 얻은 원문. 이 값이 그대로 `source_data`의 증거가 된다(write-once)."""

    ref: PostingRef
    #: 본문 텍스트. **빈 문자열이 정상인 소스가 있다**(`image_only` — 본문이 이미지뿐).
    raw_text: str
    #: **본문에 인라인으로 박힌** 이미지의 절대 URL. 페이지에 있는 대로 보고한다 —
    #: **중복 제거는 하지 않는다.** 그건 `SourceData`(저장·과금되는 레코드)가 한 곳에서 한다.
    #: 두 곳에서 하면 한쪽을 지워도 다른 쪽이 가려 테스트가 결함을 못 잡는다(실제로 그랬다).
    image_urls: tuple[str, ...] = ()
    #: **첨부파일 전부**(이름 + URL). 이미지만이 아니라 HWP·PDF도 담는다 — 원문을 최대한
    #: 남긴다. 구조화가 `Attachment.is_image`로 Gemini에 보낼 것을 고른다.
    attachments: tuple[Attachment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_text", self.raw_text.strip())
        object.__setattr__(self, "image_urls", tuple(self.image_urls))
        object.__setattr__(self, "attachments", tuple(self.attachments))


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, HTML_PARSER)


def require_one(soup: BeautifulSoup | Tag, selector: str, *, what: str) -> Tag:
    """셀렉터가 정확히 무엇을 못 찾았는지 알려주며 실패한다.

    `select_one`이 `None`을 돌려주는 것을 그대로 흘리면 `AttributeError`가 엉뚱한 줄에서 터져
    "사이트가 바뀌었다"는 진단이 늦어진다.
    """
    found = soup.select_one(selector)
    if found is None:
        raise ParseError(f"{what}: 셀렉터 `{selector}`가 아무것도 찾지 못함 — 사이트 개편 의심")
    return found


def cell_text(row: Tag, selector: str) -> str:
    """행 안의 한 칸을 텍스트로. 없으면 빈 문자열(칸이 비는 것은 정상일 수 있다)."""
    found = row.select_one(selector)
    return "" if found is None else found.get_text(" ", strip=True).strip()


def normalized_text(element: Tag) -> str:
    """블록 경계를 줄바꿈으로 살린 텍스트.

    게시판 본문은 HWP·워드에서 붙여넣은 `<span>` 더미가 흔하다. 줄 구분은 **블록 요소에서만**
    만들고, inline 사이에는 구분자를 넣지 않는다 — `separator=" "`를 쓰면 span 경계마다 공백이
    끼어 `"모집인원 : 1 명"` · `"이력서 , 자기소개서"` · `"(120)"`→`"(120)"`처럼 벌어진다
    (실측). 원문의 공백을 그대로 쓰는 편이 정확하다.
    """
    working = BeautifulSoup(str(element), HTML_PARSER)
    for tag in working(["script", "style"]):
        tag.decompose()
    for tag in working.find_all(_BLOCK_TAGS):
        tag.insert_after(NavigableString("\n"))
    text = working.get_text("")
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def image_urls_in(*elements: Tag | None, base_url: str) -> tuple[str, ...]:
    """이미지의 절대 URL. 상대 경로는 상세 URL 기준으로 합친다.

    ⚠️ **본문만 보면 안 되는 게시판이 있다.** YTUS는 첨부 이미지를 본문의 **형제** 컨테이너에
    렌더하므로 본문만 훑으면 포스터형 공고의 내용을 통째로 잃는다(실측). 그래서 여러 요소를
    받는다. `None`은 그냥 건너뛴다 — 첨부가 없는 게시판·공고가 정상이다.

    중복 제거는 하지 않는다 — `RawPosting`이 레코드 경계에서 한다. 두 곳에서 하면 한쪽을
    지워도 다른 쪽이 가려서 테스트가 결함을 못 잡는다(실제로 그랬다).
    """
    return tuple(
        urljoin(base_url, str(src).strip())
        for element in elements
        if element is not None
        for img in element.select("img")
        if (src := img.get("src")) and str(src).strip()
    )


def attachments_in(container: Tag | None, *, base_url: str) -> tuple[Attachment, ...]:
    """첨부 목록에서 (파일명, 절대 URL)을 뽑는다.

    ⚠️ **이미지 미리보기 컨테이너와 다른 곳이다.** YTUS는 이미지형 첨부를 `pnlAttachedImage`에
    미리보기로도 렌더하지만, **모든 형식**(HWP·PDF 포함)이 나오는 곳은 다운로드 링크 목록이다
    (실측). 미리보기만 보면 비이미지 첨부를 통째로 잃는다.

    링크 텍스트를 파일명으로 쓴다 — 다운로드 URL에는 파일명이 없다(`/download/…/57439f…`).
    """
    if container is None:
        return ()
    found = []
    for link in container.select("a[href]"):
        href = str(link.get("href") or "").strip()
        if not href:
            continue
        # 링크 텍스트가 비면 **버리지 않고** URL 끝에서 파일명을 복원한다 — 조용히 버리면
        # 파일명이 앵커 밖으로 나가는 개편 한 번에 첨부가 전량 유실된다.
        name = link.get_text(" ", strip=True) or _filename_from(href)
        found.append(Attachment(name=name, url=urljoin(base_url, href)))
    return tuple(found)


def _filename_from(url: str) -> str:
    """URL 끝 세그먼트를 파일명으로. YTUS 다운로드 URL은 끝에 파일명을 담는다(실측)."""
    last = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    return last or "unknown"


def external_id_from_url(url: str, *, detail_pattern: str, what: str) -> str:
    """`detail_pattern`의 `{id}` 자리에 해당하는 값을 URL에서 뽑는다.

    ⚠️ **URL 마지막 조각을 쓰면 안 된다.** 실측(YTUS 2페이지): 상세 링크가
    `/board/view/trXXR/25556/page/2`처럼 **id 뒤에 페이지가 붙는다** → 마지막 조각은 `2`가 되고
    한 페이지의 20행이 전부 같은 id를 받는다(중복 가드가 잡았다). 위치는 config가 알고 있으니
    거기서 구한다.

    캡처는 `/?&#`을 만나면 멈추므로 뒤에 무엇이 붙어도 영향받지 않는다.
    """
    prefix, _, _suffix = detail_pattern.partition(_ID_PLACEHOLDER)
    found = re.search(re.escape(prefix) + r"([^/?&#]+)", url)
    if found is None:
        raise ParseError(f"{what}: 상세 URL에서 id를 찾지 못함 ({url}) — 링크 형태가 바뀌었다")
    return found.group(1)


def as_listing(refs: Iterable[PostingRef], *, source_key: str) -> tuple[PostingRef, ...]:
    """목록 파싱 결과를 확정한다. **중복 `external_id`는 에러**(SPEC §10).

    어댑터가 잊을 수 없도록 여기서 검사한다 — 중복을 통과시키면 한 글이 다른 글을 덮거나,
    실행 간에는 "이미 본 글"로 조용히 걸러져 **유실이 사후 탐지 불가**가 된다.
    """
    listing = tuple(refs)
    duplicates = [
        key for key, count in Counter(r.external_id for r in listing).items() if count > 1
    ]
    if duplicates:
        raise ParseError(
            f"{source_key}: external_id 중복 {sorted(duplicates)} —"
            " 하위 게시판이 섞였거나 id 추출이 잘못됐다(SPEC §10)"
        )
    return listing


def _require_text(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ParseError(f"{field_name}: 비어 있음 — 목록 행에서 값을 못 뽑았다")
    return stripped


def _frozen_meta(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    checked = as_json_value(dict(value), "list_meta")
    if not isinstance(checked, dict):
        raise ParseError("list_meta: 객체여야 함")
    return MappingProxyType(checked)
