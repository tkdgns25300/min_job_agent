"""KAICAM(독립교회연합회 청빙·청원) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/KAICAM/`):

```
목록  /webchon.layout/board/white2022/list.asp?boardid=D9537   2페이지 이상은 &page={n}
      table#BOARD_white2022_list tr 32 = 헤더 1(tr.fix) + tr.list 31 = 공지 1 + 공고 30
      페이지 크기는 숨은 입력 `LISTLINE=30`이 알려준다(실측 31행 = 공지 1 + 30).
      칸: td.lt(표시번호 · 공지는 icon_notice.gif) · td(제목: div.innerBoardTitles>a) ·
          div.username · div.date · td:last-child(조회)
상세  /webchon.layout/board/white2022/view.asp?boardid=D9537&boardmasterseq=2726&boarddetailseq={id}
```

⚠️ **`soft_200`**: `view.asp`는 없는 `boarddetailseq`에도 200을 준다(config `fetch_note`).
2026-08-04 실측(`boarddetailseq=999999999`): **HTTP 200 · 2,071바이트의 빈 껍데기** —
`<title>청빙청원</title>`만 있고 `#WC_BOARD_TITLES`도 `div#contents`도 없다. 그래서 상세 파싱은
**제목이 목록에서 가져온 것과 같은지 먼저 확인**한다. 이 검사가 없으면 빈 껍데기나 남의 글이
이 공고의 증거로 저장되고, 상태코드만 보는 코드는 그것을 성공으로 기록한다(SPEC §3).

⚠️ **페이지 링크가 href가 아니라 `onclick="goPage('2')"`** 다. 실제 요청 파라미터는 숨은
`pagingPrefix`(`./list.asp?boardmasterseq=2726&…&boardtype=list&rwd=1`)와 같은 CMS를 쓰는
PGAK의 페이저(`list.asp?boardid=…&page=2`)에서 `page`로 확인했다(2페이지 fixture로 검증).

표시번호(730)와 원장 키(`boarddetailseq`=436518)는 다르다 — 표시번호는 게시판이 다시 매긴다.

**첨부 실측(2026-08-05 · 431478 · fixture `detail_file.html`)**: 본문 상자(`div#contents`)의
**형제**로 두 상자가 붙는다. 첨부가 없는 공고에는 **둘 다 아예 없다**(표본 5건 실측).

```html
<div id="divAttachCount"><img …icon_attach.gif><strong>3</strong>개의 첨부파일이 있습니다.</div>
<div id="fileAttachList" class="fileAttachList"><table><tr>
  <td class="icon" onclick="alert('권한이 없습니다.');"><img …/white2022/file/hwp.gif"></td>
  <td onclick="alert('권한이 없습니다.');">
    <span class="file" data-sub="https://pds.rh2.kr/kaicam" id="file_downloadCount_0">
      서식1. 이력서 및 개인정보 수집동의서_ts1779548611765.hwp</span>
    <span class="size">0.1 MB</span></td>
  <td><span class="download">(다운로드: <span id="downloadCount_0">0</span>)</span></td>
</tr>…</table></div>
```

⚠️ **`<a href>`가 하나도 없다** — 다운로드가 JS click이라 base의 `attachments_in`은 여기서
아무것도 못 찾는다. URL은 `data-sub`(저장 경로 접두사) + 저장 파일명으로 **조립**한다. 같은
CMS 벤더인 PGAK의 상세는 같은 파일을 `https://pds.rh2.kr/pgak/<이름>_ts<타임스탬프>.<확장자>`
href로 내려주므로 이 조립이 벤더 규칙과 일치한다(2026-08-05 PGAK 실측 대조).
표시 이름에서는 `_ts…`를 뗀다 — PGAK가 같은 파일을 그 형태로 보여준다.

⚠️ **첨부 상자의 `<img>`는 이미지가 아니라 확장자 아이콘**(`…/white2022/file/hwp.gif`)이다 —
`image_urls`를 본문에서만 모으는 이유다. 첨부가 이미지면 이름이 `.jpg`로 끝나 구조화가 알아본다.

💡 `div#divAttachCount`의 "N개의 첨부파일" 은 **독립 신호**다 — 목록에 첨부 표시 칸이 없는
게시판이라 이것이 유일한 교차 확인이고, 개수까지 맞춰볼 수 있다.

⚠️ 이 게시판은 **포스터를 첨부가 아니라 본문에 붙여 넣는 쪽이 흔하다**(실측 433091·431027 =
`storage.rh2.kr/kaicam/…jpeg|png`가 `div#contents` 안에 있다) — 본문 이미지 수집이 첨부보다
먼저 내용을 지킨다.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final
from urllib.parse import urljoin

from bs4 import Tag

from minjob_ingest.models import Attachment
from minjob_ingest.sources.adapters.base import (
    ListRequest,
    ParseError,
    PostingRef,
    RawPosting,
    as_int,
    as_listing,
    cell_text,
    external_id_from_url,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
    structural_html,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "KAICAM"

#: 표에 class가 없어 id로 잡는다. id에 스킨명(`white2022`)이 박혀 있고 그 값은 `list_url`
#: 경로에도 들어 있다 — 스킨이 바뀌면 둘이 함께 바뀌므로 `ParseError`로 즉시 드러난다.
_LIST_TABLE: Final = "table#BOARD_white2022_list"
_ROW: Final = "tr.list"
#: 고정공지 — 표시번호 칸이 숫자 대신 아이콘이다. 두 신호를 독립적으로 본다.
_NOTICE_ICON: Final = 'td.lt img[src*="icon_notice"]'
_DISPLAY_NO: Final = "td.lt"
_DETAIL_LINK: Final = "div.innerBoardTitles a[href]"
_AUTHOR: Final = "div.username"
_DATE: Final = "div.date"
_VIEWS: Final = "td:last-child"
_PAGE_PARAM: Final = "page"
#: 상세 본문. ⚠️ **상세 페이지는 아래에 목록을 다시 그린다** — 범위를 넓히면 다른 공고 30건의
#: 제목이 이 공고의 증거로 저장된다(실측).
_BODY: Final = "div#contents"
#: 상세 페이지가 제목을 담는 곳. `soft_200` 검증의 기준값이다.
_DETAIL_TITLE: Final = "#WC_BOARD_TITLES"
#: 첨부 목록 상자(본문의 형제 · 위 docstring). 첨부가 없는 공고에는 이 상자가 없다.
_FILE_LIST: Final = "div#fileAttachList"
#: 그 안의 파일 한 칸. **앵커가 아니라 span**이다 — 이름은 텍스트, 경로 접두사는 속성에 있다.
_FILE_NAME: Final = "span.file"
_FILE_BASE_ATTR: Final = "data-sub"
#: 첨부 개수를 알려주는 독립 신호(`3개의 첨부파일이 있습니다`).
_ATTACH_COUNT: Final = "div#divAttachCount"
_DIGITS: Final = re.compile(r"\d+")
#: 저장 파일명에 붙는 업로드 타임스탬프(`_ts1779548611765`). **확장자 바로 앞만** 지운다 —
#: 파일명 중간에 우연히 같은 꼴이 있어도 건드리지 않는다.
_UPLOAD_STAMP: Final = re.compile(r"_ts\d+(?=\.[A-Za-z0-9]{2,5}$)")
#: ⚠️ **이 게시판의 파일명은 NFD(분해형) 한글이다** — `서식` 이 `ᄉ+ᅥ+ᄉ+ᅵ+ᆨ`로 온다
#: (2026-08-05 실측 · macOS에서 올린 파일이 그대로 저장된다). 눈으로는 구분되지 않는데
#: 문자열 비교·검색이 전부 어긋나므로 **표시 이름은 NFC로 정규화**한다.
#: URL은 정규화하지 않는다 — 저장소 경로가 NFD 그대로라 바꾸면 404가 된다.
_DISPLAY_FORM: Final = "NFC"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `page` 쿼리다(모듈 docstring의 근거 참조)."""
    return page_query_request(source, page, param=_PAGE_PARAM, always_include=True)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지는 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    rows = table.select(_ROW)
    if not rows:
        raise ParseError(f"{SOURCE_KEY} 목록: `{_ROW}` 행이 없음 — 사이트 개편 의심")
    refs = [_ref_from_row(row, source) for row in rows if not _is_notice(row)]
    require_some_kept(
        refs, rows, source_key=SOURCE_KEY, filtered_by=f"공지 판정(`{_NOTICE_ICON}`·표시번호)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 본문 안 이미지 + 첨부. **받아온 글이 요청한 글인지 먼저 확인한다.**

    본문이 비어도 실패로 보지 않는다 — 포스터 이미지만 올리는 공고가 있다. 셋 다 없으면
    파싱이 빗나간 것이므로 실패다.

    첨부는 **본문 밖의 전용 상자에서만** 온다(위 docstring). 본문까지 범위를 넓히면 공고에 적힌
    교회 홈페이지 링크가 첨부로 저장된다 — 잘못된 첨부는 없는 첨부보다 나쁘다(DAESHIN 실측).
    """
    soup = parse_html(html)
    _require_same_posting(soup, ref)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    raw_html = structural_html(body)
    images = image_urls_in(body, base_url=ref.url)
    files = _attachments(soup)
    _require_declared_count(soup, ref, files=files)
    return RawPosting(
        ref=ref, raw_text=raw_text, raw_html=raw_html, image_urls=images, attachments=files
    )


def _attachments(soup: Tag) -> tuple[Attachment, ...]:
    """첨부 목록 상자에서 (표시 이름, 다운로드 URL)을 조립한다(위 docstring의 실측).

    ⚠️ 앵커가 없어 `attachments_in`을 쓸 수 없다. 이름·경로 중 하나라도 비면 조용히 버리지 않고
    실패한다 — 첨부 유실은 사후에 알 수 없다.
    """
    box = soup.select_one(_FILE_LIST)
    found: list[Attachment] = []
    for cell in () if box is None else box.select(_FILE_NAME):
        stored = cell.get_text(" ", strip=True)
        prefix = str(cell.get(_FILE_BASE_ATTR) or "").strip().rstrip("/")
        if not stored or not prefix:
            raise ParseError(
                f"{SOURCE_KEY}: 첨부 칸에서 이름·경로를 못 뽑음"
                f" (이름 {stored[:40]!r} · `{_FILE_BASE_ATTR}` {prefix[:40]!r})"
            )
        display = unicodedata.normalize(_DISPLAY_FORM, _UPLOAD_STAMP.sub("", stored))
        found.append(Attachment(name=display, url=f"{prefix}/{stored}"))
    return tuple(found)


def _require_declared_count(soup: Tag, ref: PostingRef, *, files: tuple[Attachment, ...]) -> None:
    """게시판이 스스로 말한 첨부 개수와 대조한다(`3개의 첨부파일이 있습니다`).

    목록에 첨부 표시 칸이 없어 이것이 **유일한 독립 신호**다. 이 대조가 없으면 파일 상자 셀렉터가
    빗나가도 "본문 있으니 정상"으로 통과해 아무도 모른다(2026-08-05 실측 전까지 그랬다).
    개수 상자가 없는 것은 첨부 없는 공고의 정상 모습이다(표본 5건).
    """
    declared = soup.select_one(_ATTACH_COUNT)
    if declared is None:
        return
    digits = _DIGITS.search(declared.get_text(" ", strip=True))
    expected = int(digits.group()) if digits else 0
    if len(files) != expected:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 첨부가 {expected}개라고 표시됐는데"
            f" {len(files)}개만 찾음 — 셀렉터 `{_FILE_LIST} {_FILE_NAME}` 확인"
        )


def _require_same_posting(soup: Tag, ref: PostingRef) -> None:
    """받아온 페이지가 **요청한 글**인지 확인한다(`soft_200` 방어 · 모듈 docstring).

    상태코드로는 판정할 수 없는 게시판이라 **본문 내용으로** 성공을 판정한다(SPEC §3).
    두 단계다:

    1. 제목 요소가 있어야 한다 — 없는 글의 껍데기에는 이게 없다(실측).
    2. 목록에서 온 `ref`면 제목이 같아야 한다 — 목록 원필드(`list_meta["list_title"]`)를
       기준으로 삼는다. 그것이 없는 `ref`는 목록을 거치지 않은 것이라(적합성 테스트의 탐침·
       상세 재파싱) 대조 기준이 없다. `ref.title`을 쓰면 그런 호출을 전부 실패시키게 된다.
    """
    found = require_one(soup, _DETAIL_TITLE, what=f"{SOURCE_KEY} 상세 제목")
    expected = ref.list_meta.get("list_title")
    if not isinstance(expected, str):
        return
    title = found.get_text(" ", strip=True)
    if _comparable(title) != _comparable(expected):
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 상세 제목이 목록과 다름"
            f" (목록 {expected!r} ≠ 상세 {title!r}) — soft_200(없는 글에도 200)일 수 있다"
        )


def _comparable(title: str) -> str:
    """공백만 접어 비교한다. 목록은 `div`, 상세는 `span`이라 공백이 갈릴 수 있다(실측)."""
    return "".join(title.split())


def _is_notice(row: Tag) -> bool:
    """고정공지 행. 아이콘과 표시번호를 독립적으로 본다 — 하나가 바뀌어도 걸린다."""
    return bool(row.select(_NOTICE_ICON)) or not cell_text(row, _DISPLAY_NO).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    external_id = require_numeric_id(
        external_id_from_url(
            urljoin(source.list_url, str(link.get("href") or "")),
            detail_pattern=source.detail_pattern,
            what=SOURCE_KEY,
        ),
        source_key=SOURCE_KEY,
    )
    # `New`·`Hit` 배지는 앵커 **밖**의 `div.innerBoardIcons`에 있어 제목에 섞이지 않는다(실측).
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href는 `view.asp?…` 상대 경로다 — **정규형**으로 다시 만든다
        # (`boardmasterseq=2726`은 config가 들고 있다).
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE), source_key=SOURCE_KEY, cell=_DATE),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE) or None,
            "author": cell_text(row, _AUTHOR) or None,
            "views": as_int(cell_text(row, _VIEWS)),
            "display_no": cell_text(row, _DISPLAY_NO) or None,
        },
    )
