"""게시판 → 어댑터 연결. 파이프라인이 `source_key`로 파서를 찾는 유일한 창구.

어댑터는 **클래스가 아니라 모듈**이다(함수 3개). mypy가 모듈을 Protocol로 구조적으로 검사해
주므로 껍데기 클래스를 만들 이유가 없다 — `ytus.parse_list(...)`가 그대로 계약을 만족한다.

⚠️ **레지스트리에 등록하지 않으면 그 게시판은 수집되지 않는다.** `config/sources.json`에
있는데 어댑터가 없으면 `AdapterMissing`으로 **명시적으로 실패**한다 — 조용히 0건이 되면
"게시판이 조용하네"로 오해한다(1-4에서 31곳을 채워간다).
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Final, Protocol

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


#: 어댑터가 아닌 이 패키지의 모듈(공용 도구·레지스트리 자신).
_NOT_ADAPTERS: Final = frozenset({"base", "registry"})


def _discover() -> dict[str, Adapter]:
    """이 패키지의 모듈에서 `SOURCE_KEY`를 선언한 것을 모은다.

    손으로 관리하는 dict를 두지 않는 이유가 둘이다. 게시판이 31곳이면 **등록을 잊는 실수**가
    생기고(그 게시판은 조용히 수집되지 않는다), 어댑터를 여러 갈래로 동시에 추가할 때
    같은 dict를 고치다 서로의 등록을 덮어쓴다.

    파일명이 곧 키라는 규칙(`ytus.py` → `YTUS`)은 적합성 테스트가 강제하므로, 파일을 놓아두는
    것만으로 등록이 끝나도 안전하다.
    """
    found: dict[str, Adapter] = {}
    for module_info in pkgutil.iter_modules(_package_path()):
        if module_info.name in _NOT_ADAPTERS or module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__package__}.{module_info.name}")
        key = getattr(module, "SOURCE_KEY", None)
        if isinstance(key, str):
            found[key] = module
    return found


def _package_path() -> list[str]:
    """이 패키지 디렉터리. 를 직접 쓰면 mypy가 모듈 속성으로 좁히지 못한다."""
    return [str(Path(__file__).parent)]


#: 구현된 어댑터. `sources/adapters/<key 소문자>.py`를 두면 자동으로 들어온다.
ADAPTERS: Final[dict[str, Adapter]] = _discover()


def find_adapter(source_key: str) -> Adapter:
    adapter = ADAPTERS.get(source_key)
    if adapter is None:
        raise AdapterMissing(
            f"{source_key}: 어댑터가 없다 —"
            f" `sources/adapters/{source_key.lower()}.py`를 만들면 자동으로 등록된다"
        )
    return adapter


def implemented_keys() -> tuple[str, ...]:
    """어댑터가 있는 게시판. `collect`가 대상을 정할 때 쓴다."""
    return tuple(sorted(ADAPTERS))
