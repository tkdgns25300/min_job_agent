"""CLI 진입점 — 운영자가 크롤을 실행하는 창구(CLAUDE.md 가드레일 #10).

0-1a 시점 명령: `list-sources`.
`daily`·`backfill`·`check-gemini`는 이후 Phase에서 붙는다.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from minjob_agent.sources.registry import (
    ConfigError,
    SourceConfig,
    enabled_sources,
    find_source,
    load_sources,
)

_PROGRAM = "minjob-agent"
_LIST_SOURCES = "list-sources"
_ENABLED_MARKER = "●"
_DISABLED_MARKER = "○"
_INTERDENOMINATIONAL_LABEL = "초교파"


def main(argv: Sequence[str] | None = None) -> int:
    """종료 코드를 반환한다(0=성공). 예외는 사용자용 메시지로 바꿔 보여준다."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # argparse는 Any를 돌려주므로 경계에서 타입을 좁힌다(CLAUDE.md 경계에서 검증).
    command = str(args.command)
    config_value: object = args.config
    config_path = Path(str(config_value)) if config_value is not None else None
    key_value: object = args.key
    key = str(key_value) if key_value is not None else None

    if command != _LIST_SOURCES:  # 하위 명령이 늘어날 때 조용히 오동작하지 않도록.
        parser.error(f"알 수 없는 명령: {command}")

    try:
        return _run_list_sources(config_path=config_path, key=key)
    except ConfigError as err:
        print(f"[{_PROGRAM}] config 오류: {err}", file=sys.stderr)
        return 1


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
    return parser


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
