"""NAZARENE(나사렛성결회 목회자청빙) 어댑터 — 그누보드5 + clean URL 스킨.

게시판 실측(2026-08-04 · fixture `tests/fixtures/NAZARENE/`):

```
목록  /ccall                       2페이지 이상은 ?page={n}
      ul.na-table > li.d-md-table-row 15 (헤더 행 없음 · 공지도 없음)
      칸이 `td`가 아니라 **`div`**다: 번호 | 제목(a.na-subject) | 등록자 | 등록일 | 조회
      각 칸 머리에 `span.sr-only` 라벨("번호"·"등록일")이 숨어 있어 그대로 읽으면
      `"번호 112"`·`"등록일 2026.07.06"`이 된다 → 파싱 전에 걷어낸다.
상세  /ccall/{id}                  본문 = #bo_v_con
      첨부(이미지)   #bo_v_img a.view_image        ← **본문 안**에 있다
      첨부(그 외)    #bo_v_data a.view_file_download ← "관련자료" 절에 이전글/다음글과 섞여 있다
```

## 첨부 실측(2026-08-05 · 목록 4페이지 60건 + 상세 3건: 97·78 + detail.html)

⚠️ **`#bo_v_file`은 이 스킨에 존재하지 않는다.** 그누보드5 기본 id지만 이 테마
(`BS4-Block` · na-table)는 첨부 목록을 **`#bo_v_data`("관련자료")** 안에 이전글/다음글과 같은
표로 렌더한다 — `#bo_v_file`만 보던 예전 코드는 **비이미지 첨부를 전량 잃었다**(78 = 3.5M PDF).
첨부 행만 `a.view_file_download` 클래스를 갖고 있어 이전글/다음글과 구분된다.

⚠️ **이미지 첨부는 또 다른 상자다** — `#bo_v_img`(97 실측). KTS와 달리 본문의 형제가 아니라
**`#bo_v_con` 안**이라서 `image_urls`에는 잡히지만, 그 값은 **600px 썸네일**
(`/data/file/ccall/thumb-…_600x275.jpg`)이다. 원본은 `a.view_image`의 href
(`view_image.php?…&fn=<파일명>.jpg`)에 있고 그것을 첨부로 담아야 포스터형 공고를 읽을 수 있다.

⚠️ **파일명이 링크 텍스트 전체가 아니다** — 앵커에 파일크기·다운로드 횟수가 함께 들어 있어
`get_text`로 읽으면 `…청빙.pdf 파일크기 (3.5M) 124 회 다운로드`가 된다. 파일명은 앵커의
**첫 텍스트 노드**뿐이고, 다운로드 URL(`download.php?…&no=0`)에는 이름이 없다.

✅ **목록에 첨부 표시가 있다** — `span.na-ticon.na-file`(1·2페이지 30건 중 1건, 3페이지 4건).
상세 첨부 셀렉터를 검증하는 **독립 신호**라 `require_attachment_evidence`로 대조한다.

⚠️ **회원전용 글은 건너뛴다**(가드레일 #1 — 우회하지 않는다). 표시는 행 안의 `.fa-lock`이고,
실측 1·2페이지 30건에는 **한 건도 없었다**(페이지의 유일한 `fa-lock`은 로그인 폼 자물쇠 아이콘).
`fetch_note`의 "일부 글 잠금"은 지금 목록에서 재현되지 않는다 — 규칙만 심어 둔다.

⚠️ **2페이지 상세 링크에는 `?page=2`가 붙는다**(실측) — 목록 href를 그대로 쓰면 같은 글의
`source_url`이 페이지마다 달라진다. 그래서 `detail_url`로 정규형을 만든다.
"""

from __future__ import annotations

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
    attachments_in,
    cell_text,
    external_id_from_url,
    gnuboard_list_date,
    image_urls_in,
    normalized_text,
    page_query_request,
    parse_html,
    require_attachment_evidence,
    require_numeric_id,
    require_one,
    require_some_kept,
)
from minjob_ingest.sources.registry import SourceConfig, detail_url

SOURCE_KEY: Final = "NAZARENE"

_LIST: Final = "ul.na-table"
_ROW: Final = "li.d-md-table-row"
#: 화면에만 보이지 않는 칸 라벨. 남겨 두면 모든 칸 값에 라벨이 앞에 붙는다(실측).
_SCREEN_READER_LABEL: Final = "span.sr-only"
#: 회원전용(잠금) 표시. **우회하지 않고 건너뛴다**(가드레일 #1).
_LOCK: Final = ".fa-lock"
_DETAIL_LINK: Final = "a.na-subject"
#: 칸이 `div`라 클래스로는 못 가른다(부트스트랩 유틸리티 클래스뿐) — 위치로 구분한다.
#: `:scope >`로 **직계 자식**에 묶는다. 없으면 제목 칸 안의 중첩 div까지 세어 칸이 밀린다.
_NO_CELL: Final = ":scope > div:nth-of-type(1)"
_AUTHOR_CELL: Final = ":scope > div:nth-of-type(3)"
_DATE_CELL: Final = ":scope > div:nth-of-type(4)"
_VIEWS_CELL: Final = ":scope > div:nth-of-type(5)"
#: 목록의 첨부 표시. 상세 첨부 셀렉터가 빗나갔는지 보는 **독립 신호**다(실측 · 모듈 상단).
_ATTACHMENT_ICON: Final = "span.na-file"
_PAGE_PARAM: Final = "page"

_BODY: Final = "#bo_v_con"
#: 이미지 첨부 상자. **본문 안**에 있다(모듈 상단) — 원본 URL은 여기 href에만 있다.
_IMAGE_BOX: Final = "#bo_v_img"
#: 비이미지 첨부. `#bo_v_file`이 아니라 "관련자료" 절이고, 첨부 행만 이 클래스를 갖는다.
_FILE_BOX: Final = "#bo_v_data"
_FILE_LINK: Final = "a.view_file_download"
_UNNAMED_FILE: Final = "attachment"


def list_request(source: SourceConfig, page: int) -> ListRequest:
    """N페이지 목록. 1페이지는 `list_url` 그대로(clean URL이라 쿼리가 없다 · 실측)."""
    return page_query_request(source, page, param=_PAGE_PARAM)


def parse_list(html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
    """목록 HTML → 공고 참조들. 고정공지·회원전용 글은 제외한다."""
    soup = parse_html(html)
    for label in soup.select(_SCREEN_READER_LABEL):
        label.decompose()
    listing = require_one(soup, _LIST, what=f"{SOURCE_KEY} 목록")
    data_rows = listing.select(_ROW)
    refs = [_ref_from_row(row, source) for row in data_rows if not _is_skippable_row(row)]
    require_some_kept(
        refs, data_rows, source_key=SOURCE_KEY, filtered_by="공지·잠금 판정(번호 칸·`.fa-lock`)"
    )
    return as_listing(refs, source_key=SOURCE_KEY)


def parse_detail(html: str, ref: PostingRef) -> RawPosting:
    """상세 HTML → 본문 + 이미지 + 첨부.

    첨부는 **두 상자**에서 온다(모듈 상단 첨부 실측) — 이미지는 `#bo_v_img`, 그 외는
    "관련자료" 절의 `a.view_file_download`. 한쪽만 보면 그 형식의 첨부를 통째로 잃는다.
    """
    soup = parse_html(html)
    body = require_one(soup, _BODY, what=f"{SOURCE_KEY} 상세 본문")
    raw_text = normalized_text(body)
    images = image_urls_in(body, base_url=ref.url)
    # ⚠️ 이미지 첨부는 base의 `attachments_in`이 그대로 처리한다 — 링크 텍스트가 비어 있어
    # 파일명을 `fn=` 쿼리에서 되살리는 경로를 탄다(KTS와 같은 `view_image.php` 형태).
    files = attachments_in(soup.select_one(_IMAGE_BOX), base_url=ref.url) + _attachments(
        soup.select_one(_FILE_BOX), base_url=ref.url
    )
    require_attachment_evidence(
        ref, source_key=SOURCE_KEY, selector=f"{_IMAGE_BOX}·{_FILE_BOX} {_FILE_LINK}", found=files
    )
    if not raw_text and not images and not files:
        raise ParseError(
            f"{SOURCE_KEY} {ref.external_id}: 본문·이미지·첨부가 모두 없음 — 셀렉터 `{_BODY}` 확인"
        )
    return RawPosting(ref=ref, raw_text=raw_text, image_urls=images, attachments=files)


def _attachments(box: Tag | None, *, base_url: str) -> tuple[Attachment, ...]:
    """ "관련자료" 절의 첨부 행 → (파일명, 절대 URL). 이전글/다음글은 클래스로 갈라진다.

    ⚠️ base의 `attachments_in`을 쓸 수 없다 — 그것은 링크 텍스트 **전체**를 파일명으로 쓰는데
    이 앵커에는 파일크기·다운로드 횟수가 함께 들어 있다(모듈 상단). 파일명은 첫 텍스트 노드뿐.
    """
    if box is None:
        return ()
    return tuple(
        Attachment(name=_file_name(link), url=urljoin(base_url, href))
        for link in box.select(_FILE_LINK)
        if (href := str(link.get("href") or "").strip())
    )


def _file_name(link: Tag) -> str:
    """앵커의 **첫 텍스트 노드**가 파일명이다. 뒤에 오는 것은 파일크기·다운로드 횟수다.

    URL(`download.php?…&no=0`)에 이름이 없어 여기가 유일한 출처다 — 비면 확장자를 잃고
    `is_image` 판정이 깨지므로 조용히 넘기지 않고 표시가 남는 이름을 준다.
    """
    return next((text for raw in link.strings if (text := raw.strip())), _UNNAMED_FILE)


def _is_skippable_row(row: Tag) -> bool:
    """건너뛸 행인가 — 회원전용(잠금)이거나 번호가 숫자가 아닌 행(고정공지).

    ⚠️ 잠금 판정을 **링크 파싱보다 먼저** 한다. 잠긴 글은 링크 형태가 다를 수 있고, 그때
    `_ref_from_row`가 먼저 터지면 그 페이지 전체를 잃는다.
    """
    return bool(row.select(_LOCK)) or not cell_text(row, _NO_CELL).isdigit()


def _ref_from_row(row: Tag, source: SourceConfig) -> PostingRef:
    link = row.select_one(_DETAIL_LINK)
    if link is None:
        raise ParseError(f"{SOURCE_KEY} 목록 행에 상세 링크가 없음 — 셀렉터 `{_DETAIL_LINK}` 확인")
    if source.detail_pattern is None:  # 레지스트리가 보장하지만 타입을 좁힌다
        raise ParseError(f"{SOURCE_KEY}: detail_pattern이 없다")
    external_id = require_numeric_id(
        external_id_from_url(
            str(link.get("href") or "").strip(),
            detail_pattern=source.detail_pattern,
            what=SOURCE_KEY,
        ),
        source_key=SOURCE_KEY,
    )
    title = link.get_text(" ", strip=True)
    shown_date = cell_text(row, _DATE_CELL)
    return PostingRef(
        external_id=external_id,
        url=detail_url(source, external_id),
        title=title,
        # 실측 범위(최신 2026.07.06)에서는 연도까지 나왔지만, 그누보드는 **오늘 글을 `HH:MM`**로
        # 준다. 저물량 게시판이라 오늘 글을 실측할 기회가 드물어 처음부터 흡수한다.
        posted_on=gnuboard_list_date(shown_date, source_key=SOURCE_KEY, cell=_DATE_CELL),
        list_meta={
            "list_title": title,
            "list_date": shown_date or None,
            "author": cell_text(row, _AUTHOR_CELL) or None,
            "views": as_int(cell_text(row, _VIEWS_CELL)),
            "display_no": cell_text(row, _NO_CELL) or None,
            # 상세 첨부 셀렉터를 대조하는 독립 신호(모듈 상단 첨부 실측).
            "has_attachment": bool(row.select(_ATTACHMENT_ICON)),
        },
    )
