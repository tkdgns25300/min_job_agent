"""WGST(웨스트민스터신대원 교역자청빙) 어댑터.

게시판 실측(2026-08-04 · fixture `tests/fixtures/WGST/`):

```
목록  /wgst_renew/board/board.asp?key=6131      2페이지 이상은 &pageno={n}
      **표가 아니라 리스트**다: ul.newsfeed_lst > li.item 12개(페이지당 12건 · 140페이지)
      li  span.num(표시번호=rowNo) ·
          div.content > strong.subject > a(제목 · font=교회 라벨) ·
          dl.info > dd(작성일) · dd.file(첨부칸) · dd(조회수)
상세  /wgst_renew/board/boardview.asp?key=6131&seq={id}
```

⚠️ **`span.num`(1676)은 `external_id`가 아니다** — 목록 href의 `seq`(6910)가 글번호다.
`rowNo`는 게시판이 다시 매기는 표시번호라 글이 삭제되면 다른 글에 붙는다.

⚠️ **`external_id`를 URL 접두사로 뽑을 수 없다.** 목록 href의 파라미터 순서가
`key → pageno → searchkey → rowNo → seq`로, config의 `detail_pattern`(`key=6131&seq=`)과 다르다
(실측) — 접두사 매칭은 통째로 실패한다. 그래서 쿼리를 파싱해 `seq`를 이름으로 집는다.

⚠️ **고정공지가 없는 게시판**이다(실측: 1페이지 12행 전부 공고). 그래도 표시번호가 숫자가 아닌
행은 걸러낸다 — 공지가 생기면 그 칸에 `공지`가 들어오는 것이 이 계열의 관례이고, 걸러지지 않으면
`require_numeric_id`가 소스 전체를 실패시킨다.

## 첨부 실측(2026-08-05 · 목록 6페이지 72건 + 상세 2건)

목록의 첨부칸(`dd.file`)은 첨부가 있는 행에만 `<i class="xi-file-text-o">` 아이콘을 담는다.
최근 60건(1·2·3·10·30·70페이지)에는 하나도 없고 **120페이지에서 3건**이 나왔다(seq 664·668·670)
— 이 게시판은 첨부를 거의 쓰지 않는다. 목록 아이콘이 **상세와 독립된 신호**이므로
`require_attachment_evidence`로 대조한다.

⚠️ **첨부 영역이 본문 상자 안에 있다**: `div.newsfeed_cnts > dl.fileAttach_wrap > dd >
ul.file_attach > li > a`. 첨부가 없는 공고에는 이 `dl`이 아예 없다. 본문 텍스트를 뽑기 전에
**빼낸다** — 그러지 않으면 파일명·"다운로드"가 공고 본문으로 섞인다.

⚠️ **`dl`을 빼내면 첨부만 있는 공고의 본문이 빈다.** seq=670이 그렇다(`div.description`이
빈 `<span>`과 `<br/>`뿐이고 내용 전부가 hwp에 있다) — 첨부를 놓치면 이 공고는 증거가 0이 된다.
빼내지 않던 예전 코드는 `raw_text`가 파일명 한 줄이라 "정상"으로 통과했다.

⚠️ **다운로드 URL이 href에 없다.** `javascript:fileDown('원본명','저장명','게시일')`이고 그 함수는
숨은 폼(`form[name=frmdown]`)의 입력 3개를 채워 `/common/download.asp`로 submit한다
(`resources/js/lib/tot_script.js` 실측). 폼에 `method`가 없어 **GET**이므로 쿼리로 되살릴 수 있다 —
2026-08-05 실측으로 확인했다(23,648바이트 · OLE/CFB 헤더 = hwp 본체).

⚠️ 페이지 좌측 메뉴에 **사이트 공용 PDF**(`/wgst_renew/upfile/gong/2022_대학안전관리계획.pdf`)가
있다. 첨부를 본문 밖에서 찾으면 그것이 공고의 첨부로 저장된다 — 첨부는 `fileAttach_wrap`에서만
온다.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

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
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_attachment_evidence,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "WGST"

_LIST: Final = "ul.newsfeed_lst"
_ROW: Final = "li.item"
_DETAIL_LINK: Final = "strong.subject a[href]"
_DISPLAY_NO: Final = "span.num"
#: 제목 앞의 교회 라벨(`<font color="#2366b5">[제자들교회]</font>`). **정규식으로 대괄호를
#: 잘라내면 안 된다** — 제목 자체에 `[청빙중]`·`[용인]`처럼 대괄호가 들어 있다(실측).
_CHURCH_LABEL: Final = "font"
#: 작성일·조회수. 사이에 첨부칸(`dd.file`)이 끼어 있어 위치로만 세면 밀린다.
_INFO_VALUES: Final = "dl.info dd:not(.file)"
#: 첨부칸의 **내용물**. 빈 칸은 첨부가 없는 것이고, 있으면 `<i class="xi-file-text-o">`가 들어온다
#: (실측). 아이콘 class를 박지 않는 이유: 아이콘 세트가 바뀌어도 "무언가 들어 있음"은 유지된다.
_FILE_CELL_CONTENT: Final = "dd.file *"
#: 상세 URL의 글번호 파라미터.
_ID_PARAM: Final = "seq"
_PAGE_PARAM: Final = "pageno"
#: 상세 본문. 정보칸(`newsfeed_cnts_info`)과 클래스 토큰이 달라 겹치지 않는다.
_BODY: Final = "div.newsfeed_view div.newsfeed_cnts"
#: 첨부 상자. **본문 상자 안에** 있고 첨부가 없는 공고에는 아예 없다(모듈 상단 첨부 실측).
_ATTACHMENTS: Final = "dl.fileAttach_wrap"
_FILE_LINK: Final = "ul.file_attach a[href]"
#: 앵커 안에서 파일명만 담는 칸. `rfilename` 인자가 비었을 때만 쓰는 폴백이다 — 형제
#: `span.down`("다운로드")이 붙어 있어 앵커 **전체**를 이름으로 쓰면 확장자가 끝이 아니게 된다.
_FILE_NAME: Final = "span.filename"
#: `javascript:fileDown('원본명','저장명','게시일')`의 인자 3개.
_FILE_DOWN: Final = re.compile(r"fileDown\(\s*'(.*?)'\s*,\s*'(.*?)'\s*,\s*'(.*?)'\s*\)")
#: 숨은 폼이 GET으로 submit하는 경로(모듈 상단 실측). 입력 이름이 그대로 쿼리 키다.
_DOWNLOAD_PATH: Final = "/common/download.asp"
_DOWNLOAD_KEYS: Final = ("rfilename", "filename", "regdate")


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. `pageno` 쿼리다(`fetch_note` 실측).

    1페이지에도 `pageno=1`을 명시한다 — 기본 페이지가 어디인지 서버 구현에 맡기지 않는다.
    """
    return page_query_request(source, page, param=_PAGE_PARAM, always_include=True)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 표시번호가 숫자가 아닌 행(공지)은 제외한다."""
    listing = require_one(parse_html(html), _LIST, what=f"{SOURCE_KEY} 목록")
    rows = listing.select(_ROW)
    if not rows:
        raise ParseError(f"{SOURCE_KEY} 목록: `{_ROW}` 행이 없음 — 사이트 개편 의심")
    refs = [_ref_from_row(row, source) for row in rows if _is_posting(row)]
    require_some_kept(
        refs, rows, source_key=SOURCE_KEY, filtered_by=f"표시번호 판정(`{_DISPLAY_NO}`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 본문 안 이미지 + 첨부.

    첨부 상자를 **본문에서 빼낸 뒤** 본문 텍스트를 뽑는다 — 순서를 바꾸면 파일명이 공고 본문에
    섞이고, 첨부만 있는 공고(seq=670 실측)가 "본문 있음"으로 위장된다.

    본문이 비어도 실패로 보지 않는다 — 내용이 hwp 첨부에만 있는 공고가 있고 그때는 그것이 유일한
    증거다. 셋 다 없을 때만 파싱이 빗나간 것이다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    box = body.select_one(_ATTACHMENTS)
    if box is not None:
        box.extract()  # 떼어내도 태그는 그대로 쓸 수 있다 — 본문 텍스트에서만 사라진다
    files = _attachments(box, base_url=ref.url)
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    # 첨부가 이미지로 렌더되는 공고를 만나면 그것도 증거다 — 형식보다 내용 유실이 중요하다.
    require_attachment_evidence(
        ref, source_key=SOURCE_KEY, selector=_ATTACHMENTS, found=(*files, *images)
    )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _attachments(box: Tag | None, *, base_url: str) -> tuple[Attachment, ...]:
    """첨부 상자 → (파일명, 절대 URL). **URL은 JS 호출에서 되살린다**(모듈 상단 실측).

    상자가 있는데 링크를 하나도 못 뽑으면 실패시킨다 — 조용히 빈 목록을 돌려주면 내용이 첨부에만
    있는 공고를 통째로 잃고도 아무도 모른다.
    """
    if box is None:
        return ()
    found = [_attachment_from(link, base_url=base_url) for link in box.select(_FILE_LINK)]
    if not found:
        raise ParseError(
            f"{SOURCE_KEY}: `{_ATTACHMENTS}` 상자는 있는데 첨부 링크가 없음 —"
            f" 셀렉터 `{_FILE_LINK}` 확인(사이트 개편 의심)"
        )
    return tuple(found)


def _attachment_from(link: Tag, *, base_url: str) -> Attachment:
    """첨부 링크 하나. `fileDown` 인자 3개가 그대로 다운로드 쿼리가 된다(모듈 상단 실측)."""
    href = str(link.get("href") or "")
    matched = _FILE_DOWN.search(href)
    if matched is None:
        raise ParseError(
            f"{SOURCE_KEY}: 첨부 링크에서 `fileDown` 인자를 못 뽑음 ({href[:80]!r}) —"
            " 다운로드 방식이 바뀌었다"
        )
    query = urlencode(dict(zip(_DOWNLOAD_KEYS, matched.groups(), strict=True)))
    # 이름은 JS 첫 인자(`rfilename` = 서버가 내려줄 원본명)를 쓴다. 앵커 전체 텍스트를 쓰면
    # `…hwp 다운로드`가 되어 확장자가 끝이 아니게 되고 `is_image`가 이미지를 못 알아본다.
    name = matched.group(1).strip() or cell_text(link, _FILE_NAME)
    if not name:
        raise ParseError(f"{SOURCE_KEY}: 첨부 파일명이 비었음 ({href[:80]!r})")
    return Attachment(name=name, url=urljoin(base_url, f"{_DOWNLOAD_PATH}?{query}"))


def _is_posting(row: Tag) -> bool:
    """표시번호가 숫자인 행만 공고로 본다(공지는 그 칸에 `공지`가 들어온다)."""
    return cell_text(row, _DISPLAY_NO).replace(",", "").isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    external_id = require_numeric_id(_seq_in(link), source_key=SOURCE_KEY)
    church = _take_church_label(link)
    title = link.get_text(" ", strip=True)
    posted_text, views = _info_of(row)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 pageno·rowNo·검색어가 붙어 있다 — **정규형**으로 다시 만든다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(posted_text, source_key=SOURCE_KEY, cell=_INFO_VALUES),
        list_meta={
            "list_title": title,
            "list_date": posted_text or None,
            # 이 게시판엔 작성자 칸이 없다 — 교회 라벨이 그 자리를 대신한다.
            "author": church,
            "views": as_int(views),
            "display_no": cell_text(row, _DISPLAY_NO) or None,
            "has_attachment": bool(row.select(_FILE_CELL_CONTENT)),
        },
    )


def _seq_in(link: Tag) -> str:
    """목록 href의 `seq`. 파라미터 **이름으로** 집는다(순서가 config와 다르다 · 모듈 docstring)."""
    found = parse_qs(urlsplit(str(link.get("href") or "")).query).get(_ID_PARAM, [])
    if not found or not found[0].strip():
        raise ParseError(
            f"{SOURCE_KEY}: 목록 링크에 `{_ID_PARAM}`이 없음 ({link.get('href')!r}) —"
            " 링크 형태가 바뀌었다"
        )
    return found[0].strip()


def _take_church_label(link: Tag) -> str | None:
    """제목 앞의 교회 라벨을 **떼어내** 따로 돌려준다.

    떼지 않으면 제목이 `[제자들교회] 제자들교회(동탄)에서 …`처럼 교회명이 두 번 들어간다.
    이 게시판은 `a[title]`에 순수 제목을 담고 있어(실측) 테스트가 그 값과 대조할 수 있다.
    """
    label = link.select_one(_CHURCH_LABEL)
    if label is None:
        return None
    text = label.get_text(" ", strip=True)
    label.extract()
    return text.strip("[]") or None


def _info_of(row: Tag) -> tuple[str, str]:
    """(작성일, 조회수). 첨부칸을 뺀 `dd` 순서가 계약이다."""
    values = [value.get_text(" ", strip=True) for value in row.select(_INFO_VALUES)]
    if len(values) < 2:
        raise ParseError(
            f"{SOURCE_KEY} 목록 행의 정보칸이 {len(values)}개 — 셀렉터 `{_INFO_VALUES}` 확인"
        )
    return values[0], values[1]
