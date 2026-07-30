"""CLI 진입점 — 운영자가 크롤을 실행하는 창구(CLAUDE.md 가드레일 #10).

현재 명령: `list-sources`(등록 소스 확인) · `check-gemini`(Vertex 인증·연결 실호출 1회).
`daily`·`backfill`은 Phase 1에서 붙는다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from minjob_ingest.lib.gemini import GeminiClient, GeminiError
from minjob_ingest.settings import Settings, VertexConfigError
from minjob_ingest.sources.registry import (
    ConfigError,
    SourceConfig,
    enabled_sources,
    find_source,
    load_sources,
)

_PROGRAM = "minjob-ingest"
_LIST_SOURCES = "list-sources"
_CHECK_GEMINI = "check-gemini"
_ENABLED_MARKER = "●"
_DISABLED_MARKER = "○"
_INTERDENOMINATIONAL_LABEL = "초교파"

_SMOKE_PROMPT = "연결 확인용. 한국어로 정확히 'OK'라고만 답하세요."


def main(argv: Sequence[str] | None = None) -> int:
    """종료 코드를 반환한다(0=성공). 예외는 사용자용 메시지로 바꿔 보여준다."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except ConfigError as err:
        print(f"[{_PROGRAM}] config 오류: {err}", file=sys.stderr)
        return 1
    except VertexConfigError as err:
        print(f"[{_PROGRAM}] Vertex 설정 오류: {err}", file=sys.stderr)
        return 1
    except GeminiError as err:
        print(f"[{_PROGRAM}] Gemini 호출 실패: {err}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    """파싱된 인자를 명령 함수로 넘긴다. 예상 오류를 메시지로 바꾸는 일은 `main`이 한다."""
    # argparse는 Any를 돌려주므로 경계에서 타입을 좁힌다(CLAUDE.md 경계에서 검증).
    command = str(args.command)
    if command == _LIST_SOURCES:
        config_value: object = args.config
        key_value: object = args.key
        return _run_list_sources(
            config_path=Path(str(config_value)) if config_value is not None else None,
            key=str(key_value) if key_value is not None else None,
        )
    if command == _CHECK_GEMINI:
        return _run_check_gemini()
    # argparse가 이미 미등록 명령을 걸러내므로, 여기 오는 건 "서브파서는 추가했는데 연결을
    # 잊은" 경우다 — 조용히 성공(0)하는 대신 크래시로 알린다.
    raise RuntimeError(f"명령 '{command}'이 _dispatch에 연결되지 않았다")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=_PROGRAM, description="청빙 공고 수집 크롤러")
    subcommands = parser.add_subparsers(dest="command", required=True)

    listing = subcommands.add_parser(_LIST_SOURCES, help="등록된 게시판 설정 출력")
    listing.add_argument("key", nargs="?", default=None, help="특정 source_key만 (예: YTUS)")
    listing.add_argument(
        "--config",
        default=None,
        help="sources.json 경로 (기본: 리포의 config/sources.json)",
    )

    subcommands.add_parser(_CHECK_GEMINI, help="Vertex 인증·연결 확인 (실호출 1회)")
    return parser


def _run_check_gemini() -> int:
    """서비스계정 인증이 실제로 통하는지 실호출 1번으로 검증한다(셋업 함정 조기 제거).

    ⚠️ 유일하게 **외부 유료 API를 직접 호출**하는 명령이다(토큰 소량). 게시판은 건드리지 않는다.
    """
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    settings = Settings.load()
    client = GeminiClient(settings.require_vertex())
    print(f"[{_CHECK_GEMINI}] 모델={client.model} 연결 시도…")
    answer = client.generate_smoke_text(_SMOKE_PROMPT)
    print(f"[{_CHECK_GEMINI}] ✅ 응답: {answer.strip()!r}")
    print(f"[{_CHECK_GEMINI}] Vertex 인증·호출 성공.")
    return 0


def _run_list_sources(*, config_path: Path | None, key: str | None) -> int:
    sources = load_sources(config_path)

    if key is not None:
        found = find_source(sources, key)
        if found is None:
            print(f"[{_PROGRAM}] 알 수 없는 source_key: {key}", file=sys.stderr)
            return 1
        _print_sources([found], show_note=True)
        return 0

    active = enabled_sources(sources)
    print(f"등록 소스 {len(sources)}곳 (활성 {len(active)}):")
    _print_sources(sources, show_note=False)
    return 0


def _print_sources(sources: Sequence[SourceConfig], *, show_note: bool) -> None:
    key_width = max((len(s.key) for s in sources), default=0)
    for source in sources:
        marker = _ENABLED_MARKER if source.enabled else _DISABLED_MARKER
        attributes = [_hint_label(source), source.fetch_tier.value, source.encoding.value]
        attributes.extend(_flag_names(source))
        print(
            f"  {marker} {source.key:<{key_width}}  {source.board_name}  [{' · '.join(attributes)}]"
        )
        if source.disabled_reason is not None:
            print(f"      제외 사유: {source.disabled_reason}")
        if show_note:
            print(f"      list: {source.list_url}")
            print(f"      detail: {source.detail_pattern or '(URL 템플릿 없음 — 목록 링크 사용)'}")
            print(f"      note: {source.fetch_note}")


def _hint_label(source: SourceConfig) -> str:
    if source.is_interdenominational:
        return _INTERDENOMINATIONAL_LABEL
    assert source.denomination_hint is not None  # is_interdenominational이 보장
    return source.denomination_hint.value


def _flag_names(source: SourceConfig) -> list[str]:
    flags = source.flags
    return [name for name in flags.__dataclass_fields__ if getattr(flags, name)]


if __name__ == "__main__":
    sys.exit(main())
