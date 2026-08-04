"""게시판 → 어댑터 연결. 파이프라인이 `source_key`로 파서를 찾는 유일한 창구.

어댑터는 **클래스가 아니라 모듈**이다(함수 3개). mypy가 모듈을 Protocol로 구조적으로 검사해
주므로 껍데기 클래스를 만들 이유가 없다 — `ytus.parse_list(...)`가 그대로 계약을 만족한다.

⚠️ **레지스트리에 등록하지 않으면 그 게시판은 수집되지 않는다.** `config/sources.json`에
있는데 어댑터가 없으면 `AdapterMissing`으로 **명시적으로 실패**한다 — 조용히 0건이 되면
"게시판이 조용하네"로 오해한다(1-4에서 31곳을 채워간다).
"""

from __future__ import annotations

from typing import Final, Protocol

from minjob_ingest.sources.adapters import ytus
from minjob_ingest.sources.adapters.base import ListRequest, PostingRef, RawPosting
from minjob_ingest.sources.registry import SourceConfig


class AdapterMissing(Exception):
    """등록된 어댑터가 없는 게시판. 아직 만들지 않은 소스를 조용히 건너뛰지 않는다."""


class Adapter(Protocol):
    """게시판별 파서가 제공해야 하는 것 전부(SPEC §10). **네트워크를 만지지 않는다.**"""

    def list_request(self, source: SourceConfig, page: int) -> ListRequest:
        """N페이지 목록을 가져오는 방법(URL + 필요하면 POST 본문)."""
        ...

    def parse_list(self, html: str, source: SourceConfig) -> tuple[PostingRef, ...]:
        """목록 HTML → 공고 참조들(고정공지 제외)."""
        ...

    def parse_detail(self, html: str, ref: PostingRef) -> RawPosting:
        """상세 HTML → 원문·이미지·첨부."""
        ...


#: 구현된 어댑터. 1-4에서 31곳까지 채운다.
ADAPTERS: Final[dict[str, Adapter]] = {ytus.SOURCE_KEY: ytus}


def find_adapter(source_key: str) -> Adapter:
    adapter = ADAPTERS.get(source_key)
    if adapter is None:
        raise AdapterMissing(
            f"{source_key}: 어댑터가 없다 — `sources/adapters/`에 만들고 이 레지스트리에 등록한다"
        )
    return adapter


def implemented_keys() -> tuple[str, ...]:
    """어댑터가 있는 게시판. `collect`가 대상을 정할 때 쓴다."""
    return tuple(sorted(ADAPTERS))
