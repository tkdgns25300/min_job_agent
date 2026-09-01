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
from collections.abc import Callable
from dataclasses import dataclass
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
    """이 패키지 디렉터리. `__path__`를 직접 쓰면 mypy가 모듈 속성으로 좁히지 못한다."""
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


@dataclass(frozen=True, slots=True)
class GoneProber:
    """게시판이 삭제를 **직접** 말해줄 때 그 신호를 쓰는 방법(SPEC §4 gone 단계).

    상세를 파싱해 보는 일반 확인보다 정확하다 — CSU는 상세 HTML이 살아있는 글과 삭제된 글에
    **똑같은 SPA 껍데기**라 구분이 안 되는데, API는 `삭제된 게시물입니다`라고 답한다(실측
    2026-08-30). 네트워크는 여전히 fetch 층이 하고 어댑터는 **무엇을 보낼지·응답이 무슨
    뜻인지만** 정한다(`ListRequest`와 같은 분업).
    """

    #: 이 글이 살아있는지 물어보는 요청.
    request: Callable[[SourceConfig, str], ListRequest]
    #: 응답 본문 → 삭제됐나. `None`은 모르겠다는 뜻이다 — 모르면 내리지 않는다.
    parse: Callable[[str], bool | None]

    @classmethod
    def of(cls, adapter: Adapter) -> GoneProber | None:
        """어댑터가 전용 신호를 선언했으면 그것을 쓴다(`needs_detail_request`와 같은 관례).

        둘 중 하나만 선언한 어댑터는 버그이므로 시끄럽게 죽는다 — 조용히 일반 확인으로
        넘어가면 전용 신호가 있는 게시판이 덜 정확한 판정을 받는다.
        """
        request = getattr(adapter, "gone_request", None)
        parse = getattr(adapter, "parse_gone", None)
        if request is None and parse is None:
            return None
        if request is None or parse is None:
            raise TypeError(f"{adapter!r}: gone_request·parse_gone은 짝으로 선언해야 한다")
        return cls(request=request, parse=parse)


def needs_detail_request(adapter: Adapter) -> bool:
    """상세 페이지를 따로 받아야 하나.

    거의 모든 게시판은 그렇다. 예외는 **목록 응답에 본문이 이미 들어 있는** 경우다 —
    HANIL은 목록 AJAX가 `contents`(본문 HTML)까지 주고, 상세 페이지는 JS가 채우는 빈 껍데기라
    받아도 제목조차 없다(실측 2026-08-04). 그런 게시판에 상세를 요청하면 글마다 한 번씩
    **쓸모없는 요청**을 보내는 셈이다.

    선언하지 않은 어댑터는 `True`로 본다 — 29곳에 같은 한 줄을 쓰게 만들지 않는다.
    """
    return bool(getattr(adapter, "NEEDS_DETAIL_REQUEST", True))


def implemented_keys() -> tuple[str, ...]:
    """어댑터가 있는 게시판. `collect`가 대상을 정할 때 쓴다."""
    return tuple(sorted(ADAPTERS))
