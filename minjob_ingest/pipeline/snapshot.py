"""fixture 확보 — 게시판 HTML을 파일로 떠 온다.

⚠️ **어댑터 없이 동작해야 한다.** fixture는 어댑터를 *만들기 전에* 필요하므로(파싱 코드를 고칠
때마다 게시판을 다시 두드리지 않기 위해), 이 모듈은 `sources/adapters/`를 모른다.
config의 `list_url`·`detail_pattern`만 쓴다.

받은 HTML은 `tests/fixtures/<KEY>/`에 **그대로**(마스킹 없이) 둔다 — 그 디렉터리는
커밋되지 않는다(`tests/fixtures/README.md`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from minjob_ingest.fetch.client import FetchError, SourceClient
from minjob_ingest.sources.registry import SourceConfig

_ID_PLACEHOLDER: Final = "{id}"
#: 상세 URL의 `{id}` 자리에 들어갈 수 있는 문자(글번호·hex·복합키).
_ID_CHARS: Final = r"[A-Za-z0-9_:-]+"

_LIST_FILE: Final = "list.html"
_DETAIL_FILE: Final = "detail.html"


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """무엇을 받았나. 운영자에게 그대로 보고한다."""

    source_key: str
    saved: tuple[Path, ...]
    #: 상세 링크를 못 찾은 이유(찾았으면 `None`). 실패가 아니라 **설명**이다 — 목록만으로도
    #: 어댑터 작업을 시작할 수 있고, 상세는 `--url`로 따로 받을 수 있다.
    detail_skipped: str | None = None

    @property
    def has_detail(self) -> bool:
        return any(path.name == _DETAIL_FILE for path in self.saved)


def fixture_dir(root: Path, source_key: str) -> Path:
    return root / source_key


def detail_url_pattern(detail_pattern: str) -> re.Pattern[str]:
    """`detail_pattern`에서 목록 HTML을 뒤질 정규식을 만든다.

    `/board/view/trXXR/{id}` → `/board/view/trXXR/<id>`를 찾는 패턴. 쿼리 파라미터 순서가
    목록 href와 다를 수 있어 **`{id}` 앞부분만** 고정하고 뒤는 느슨하게 둔다.
    """
    head, _, _tail = detail_pattern.partition(_ID_PLACEHOLDER)
    # `?`·`&` 등 정규식 특수문자가 그대로 있으므로 escape 필수.
    return re.compile(re.escape(head) + f"({_ID_CHARS})")


def find_detail_url(list_html: str, source: SourceConfig) -> str | None:
    """목록 HTML에서 상세 URL 하나를 고른다(어댑터 없이 · 휴리스틱).

    ⚠️ **마지막 후보를 고른다.** 첫 후보를 고르면 거의 항상 고정공지를 받는다 — 공지는 목록 맨
    위에 붙어 있고, 실제로 YTUS에서 2014년 게시판 운영규정을 받아 왔다(2026-08-04). 공지 본문은
    구조도 내용도 실제 공고와 달라서 상세 파싱을 검증할 수 없다. 목록은 날짜 역순이므로
    마지막 후보는 그 페이지에서 가장 오래된 **실제 공고**다.

    정확한 추출은 어댑터의 일이다. 여기서는 상세 한 장을 받아 두는 것이 목적이고, 골라온 것이
    마음에 안 들면 `--url`로 원하는 글을 직접 받는다.
    """
    if source.detail_pattern is None:
        return None
    found = detail_url_pattern(source.detail_pattern).findall(list_html)
    if not found:
        return None
    return source.detail_pattern.replace(_ID_PLACEHOLDER, found[-1])


def snapshot_source(
    source: SourceConfig, client: SourceClient, target: Path, *, want_detail: bool = True
) -> SnapshotResult:
    """목록 1페이지(+ 상세 표본 1건)를 받아 저장한다. 요청은 **최대 2회**다."""
    target.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    list_html = client.get(source.list_url).text
    saved.append(_write(target / _LIST_FILE, list_html))

    if not want_detail:
        return SnapshotResult(
            source_key=source.key, saved=tuple(saved), detail_skipped="요청 안 함"
        )
    if source.detail_pattern is None:
        return SnapshotResult(
            source_key=source.key,
            saved=tuple(saved),
            detail_skipped="detail_pattern 없음 — 목록 href를 봐야 한다(--url 로 따로 받는다)",
        )
    detail_url = find_detail_url(list_html, source)
    if detail_url is None:
        return SnapshotResult(
            source_key=source.key,
            saved=tuple(saved),
            detail_skipped="목록에서 상세 링크 형태를 못 찾음 — 셀렉터·JS 링크 확인 필요",
        )
    try:
        detail_html = client.get(detail_url).text
    except FetchError as err:
        # 목록은 받았으니 절반은 성공이다 — 전체를 실패로 만들지 않고 사유를 남긴다.
        return SnapshotResult(
            source_key=source.key, saved=tuple(saved), detail_skipped=f"상세 요청 실패: {err}"
        )
    saved.append(_write(target / _DETAIL_FILE, detail_html))
    return SnapshotResult(source_key=source.key, saved=tuple(saved))


def snapshot_url(client: SourceClient, url: str, target: Path, name: str) -> SnapshotResult:
    """임의 URL 1장(2페이지·특수 경로 등). 휴리스틱이 안 통하는 곳의 탈출구다."""
    target.mkdir(parents=True, exist_ok=True)
    html = client.get(url).text
    return SnapshotResult(source_key=target.name, saved=(_write(target / name, html),))


def _write(path: Path, html: str) -> Path:
    """받은 그대로 쓴다.

    ⚠️ **파싱해서 다시 저장하지 않는다** — BeautifulSoup으로 재직렬화하면 DOM이 재구성돼
    본문이 274자에서 16,303자로 변한 적이 있다(YTUS 실측). fixture는 실물이어야 의미가 있다.
    """
    path.write_text(html, encoding="utf-8")
    return path
