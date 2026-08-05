"""STS(순복음대학원대 청빙및취업) 어댑터 — anyboard 스킨.

게시판 실측(2026-08-04 · fixture `tests/fixtures/STS/`):

```
목록  /main/sub.html?pageCode=38       2페이지 이상은 `&page={n}`
      table.jmboardskin1 · tr 17 = 헤더 1(th) + 여백행 1(tr.jTh2) + 공고 15
      칸: td.jNum(표시번호) td.jSubject(링크) td.jWriter td.jDate(YYYY.MM.DD) td.jView
상세  /main/sub.html?Mode=view&boardID=www38&num={id}    본문 = div.mdView_cont
      첨부 = div.mdView_cont div.mdView_file #AB_viewFileLayer a  ← **본문 안**이다
```

## 첨부 실측(2026-08-05 · 목록 2페이지 30건 + 상세 3건: 7238·7250 + detail.html)

이 게시판은 **같은 게시판인데 텍스트 공고와 이미지 공고가 섞여 있다**(운영자 관찰).
7250은 본문 텍스트가 **0자**이고 내용 전체가 이미지 1장 + 지원서 양식 `.hwp`다(7238도 제목 한
줄 16자뿐) — 첨부를 놓치면 공고를 통째로 잃는 쪽이다. detail.html(7319)은 반대로 순수 텍스트다.

⚠️ **첨부 영역이 본문 안에 있다**(`div.mdView_cont > ul > div.mdView_file`). 예전 코드는
본문에서 첨부를 긁어 **UI 버튼 2개를 첨부로 저장**했다(실측 7238):
`[ 첨부파일 일괄 다운로드 ]`(`fileListCheck`) · `[첨부파일 1개 ]`(`fileListViewPage`).
실제 파일 링크는 **`#AB_viewFileLayer` 안에만** 있어 컨테이너로 갈린다.

⚠️ 같은 이유로 **스킨 아이콘이 본문 이미지로 새어 들어갔다** —
`bul_arrow_down.png`·`bg_addfile_top.png`·`bul_addfile.gif`·`bg_addfile_bottom.png`(실측 4~5개).
그래서 첨부 영역을 **본문 텍스트·이미지 계산 전에 걷어낸다.**

⚠️ 다운로드 URL이 href에 없다 — `javascript:anyboard.fileDown('3155')`이고 실제 경로는
`/core/anyboard/download.php?boardID=<boardID>&fileNum=<n>`
(`/core/script/anyboard/anyboard.js`의 `fileDown` 실측 · GET으로 863KB JPEG 수신 확인).
`boardID`는 페이지의 `input[name=boardID]`에서 읽는다 — ref에 의존하지 않는다.
파일명은 링크 텍스트뿐이고 그것으로 `is_image`가 성립한다(`.hwp` vs `.jpg` 실측).

✅ **목록에 첨부 표시가 있다** — `td.jSubject img.mdBoardIcon`(`disk.png` 파일 · `photo.png`
이미지). 30건 중 8건. 상세 첨부 셀렉터를 검증하는 **독립 신호**라 대조에 쓴다.

⚠️ **목록과 상세가 같은 `sub.html`인데 파라미터가 다르다** — 목록은 `pageCode=38`,
상세는 `boardID=www38`이다(같은 게시판을 가리키는 두 이름). config가 둘을 각각 들고 있으므로
어댑터는 `list_url`에 페이지만 얹고, 상세 URL은 `detail_pattern`으로 만든다.

⚠️ **제목을 `td.jSubject` 텍스트로 읽으면 안 된다.** 그 칸에는 모바일용 `<p>`가 함께 있어
ANYSECURE 암호문(작성자)·등록일·조회수가 제목 뒤에 붙는다(실측). 앵커 텍스트만 쓴다.

작성자는 `list_meta`에 담지 않는다 — 이 스킨은 작성자명을 ANYSECURE 암호문
(`eyJjdCI6…`)으로만 내려주므로 저장해도 읽을 수 없고, 개인정보 최소 원칙에도 맞지 않는다.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

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
    require_attachment_evidence,
    require_date,
    require_numeric_id,
    require_one,
    require_some_kept,
    rows_with_data,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "STS"

_LIST_TABLE: Final = "table.jmboardskin1"
_DETAIL_LINK: Final = "td.jSubject a[href]"
_DATE_CELL: Final = "td.jDate"
#: 표시번호 칸. **숫자가 아니면 공고가 아니다** — 고정공지는 여기에 `공지`가 들어가고,
#: 헤더 아래 여백행(`tr.jTh2`)은 `colspan` 한 칸뿐이라 이 칸이 아예 없다(실측).
#: 두 경우를 한 규칙으로 걸러낸다.
_NUMBER_CELL: Final = "td.jNum"
_VIEWS_CELL: Final = "td.jView"
#: 목록의 첨부 표시(`disk.png`·`photo.png`). 상세 셀렉터를 대조하는 **독립 신호**다(실측).
_ATTACHMENT_ICON: Final = "td.jSubject img.mdBoardIcon"
_PAGE_PARAM: Final = "page"
#: 본문. 안쪽 `#lightgallery`에 에디터 내용이 들어간다.
_BODY: Final = "div.mdView_cont"
#: 첨부 영역 — **본문 안**에 있다(모듈 상단). 본문 텍스트·이미지 계산 전에 걷어낸다.
_FILE_AREA: Final = "div.mdView_file"
#: 그 안에서 **실제 파일 링크만** 담고 있는 목록. 일괄 다운로드·개수 토글 버튼은 이 밖이다.
_FILE_LIST: Final = "#AB_viewFileLayer"
_FILE_LINK: Final = "a[href]"
#: `javascript:anyboard.fileDown('3155')` → fileNum.
_FILE_NUM: Final = re.compile(r"fileDown\(\s*'([^']+)'")
#: 다운로드 경로(모듈 상단 실측). `boardID`는 페이지에서 읽는다.
_DOWNLOAD_PATH: Final = "/core/anyboard/download.php?boardID={board}&fileNum={num}"
_BOARD_ID_INPUT: Final = "input[name='boardID']"
_UNNAMED_FILE: Final = "attachment"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로.

    게시판 자신의 페이징 링크는 `?page=N&boardID=www38`(pageCode 없음) 형태지만, 우리는
    `pageCode=38&page=N`으로 요청한다 — 라이브 확인 결과 같은 결과가 나온다(실측: 2페이지
    글번호 7133~6801). config의 `list_url` 하나만 정본으로 두기 위해서다.
    """
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지·여백행은 제외한다."""
    table = require_one(parse_html(html), _LIST_TABLE, what=f"{SOURCE_KEY} 목록")
    data_rows = rows_with_data(table)
    refs = [_ref_from_row(row, source) for row in data_rows if _has_posting_number(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by=f"표시번호 판정(`{_NUMBER_CELL}`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    ⚠️ **첨부 영역을 먼저 떼어낸다**(모듈 상단 첨부 실측). 그것이 본문 안에 있어서, 남겨 두면
    UI 문구가 `raw_text`에 · 스킨 아이콘이 `image_urls`에 · 버튼이 `attachments`에 섞인다.

    본문 텍스트가 비는 것은 실패가 아니다 — 내용 전체가 이미지 1장인 공고가 있다(7250 실측).
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    file_area = body.select_one(_FILE_AREA)
    files = _attachments(file_area, soup, base_url=ref.url)
    if file_area is not None:
        file_area.decompose()
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    # 본문 이미지를 증거로 인정하지 않는다 — 첨부 이미지는 본문에도 인라인으로 렌더되므로
    # 그것까지 세면 파일 목록 셀렉터가 깨져도 통과해 비이미지 첨부(hwp)를 조용히 잃는다.
    require_attachment_evidence(
        ref, source_key=SOURCE_KEY, selector=f"{_FILE_AREA} {_FILE_LIST}", found=files
    )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _attachments(
    file_area: Tag | None, soup: BeautifulSoup, *, base_url: str
) -> tuple[Attachment, ...]:
    """첨부 영역 → (파일명, 절대 URL). 파일 링크는 `#AB_viewFileLayer` 안에만 있다.

    영역이 있는데 파일을 못 뽑으면 **실패시킨다** — 첨부 영역은 첨부가 있을 때만 생기므로
    (실측) 빈 목록을 조용히 돌려주면 이미지형 공고를 통째로 잃고도 아무도 모른다.
    """
    if file_area is None:
        return ()
    listing = file_area.select_one(_FILE_LIST)
    if listing is None:
        raise ParseError(
            f"{SOURCE_KEY}: `{_FILE_AREA}`는 있는데 `{_FILE_LIST}`가 없음 — 사이트 개편 의심"
        )
    board_id = _board_id(soup)
    found: list[Attachment] = []
    for link in listing.select(_FILE_LINK):
        href = str(link.get("href") or "")
        matched = _FILE_NUM.search(href)
        if matched is None:
            raise ParseError(f"{SOURCE_KEY}: 첨부 링크에서 fileNum을 못 뽑음 ({href[:60]!r})")
        name = link.get_text(" ", strip=True) or _UNNAMED_FILE
        path = _DOWNLOAD_PATH.format(board=board_id, num=matched.group(1))
        found.append(Attachment(name=name, url=urljoin(base_url, path)))
    if not found:
        raise ParseError(
            f"{SOURCE_KEY}: `{_FILE_LIST}`에 파일 링크가 없음 — 셀렉터 `{_FILE_LINK}` 확인"
        )
    return tuple(found)


def _board_id(soup: BeautifulSoup) -> str:
    """다운로드 URL에 필요한 게시판 이름. **페이지가 알려주는 값을 쓴다.**

    `ref.url`에서 뽑을 수도 있지만 그러면 상세 URL 형태 변경에 첨부가 함께 죽는다.
    """
    found = soup.select_one(_BOARD_ID_INPUT)
    value = "" if found is None else str(found.get("value") or "").strip()
    if not value:
        raise ParseError(
            f"{SOURCE_KEY}: 상세에서 boardID를 못 찾음 (`{_BOARD_ID_INPUT}`) —"
            " 다운로드 URL을 만들 수 없다"
        )
    return value


def _has_posting_number(row: Tag) -> bool:
    """표시번호가 숫자인 행만 공고다(모듈 상단 `_NUMBER_CELL` 주석 참조)."""
    return cell_text(row, _NUMBER_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    href = str(link.get("href") or "").strip()
    external_id = _external_id_from(urljoin(source.list_url, href), source)
    # 앵커 텍스트만 제목이다 — 칸 전체를 읽으면 암호문·날짜·조회수가 붙는다(모듈 docstring).
    title = link.get_text(" ", strip=True)
    return PostingRef(
        external_id=external_id,
        # 목록 href에는 빈 검색·페이지 파라미터(`&page=&keyfield=&key=&bCate=`)가 달려 있어
        # **정규형**으로 다시 만든다 — 같은 글의 `source_url`이 달라지면 안 된다.
        url=detail_url(source, external_id),
        title=title,
        posted_on=require_date(cell_text(row, _DATE_CELL), source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": cell_text(row, _DATE_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NUMBER_CELL) or None,
            # 상세 첨부 셀렉터를 대조하는 독립 신호(모듈 상단 첨부 실측).
            "has_attachment": bool(row.select(_ATTACHMENT_ICON)),
        },
    )


def _external_id_from(url: str, source: SourceConfig) -> str:
    """상세 URL의 `num`. 표시번호(`td.jNum`)가 아니다 — 실측 111 vs 7319."""
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    found = external_id_from_url(url, detail_pattern=source.detail_pattern, what=SOURCE_KEY)
    return require_numeric_id(found, source_key=SOURCE_KEY)
