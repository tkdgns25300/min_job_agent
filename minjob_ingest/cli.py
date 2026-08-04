"""CLI 진입점 — 운영자가 크롤을 실행하는 창구(CLAUDE.md 가드레일 #10).

현재 명령: `list-sources`(등록 소스 확인) · `check-gemini`(Vertex 인증 실호출 1회) ·
`collect`(게시판에서 공고 수집). `structure`·`daily`·`backfill`은 이후 단계에서 붙는다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from minjob_ingest.clock import utc_now
from minjob_ingest.domain import CrawlMode
from minjob_ingest.fetch.client import FetchError, SourceClient
from minjob_ingest.lib.gemini import GeminiClient, GeminiError
from minjob_ingest.pipeline.collect import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MONTHS,
    CollectOptions,
    CollectReport,
    LedgerConflict,
    collect_source,
)
from minjob_ingest.settings import Settings, VertexConfigError
from minjob_ingest.sources.adapters.base import ParseError
from minjob_ingest.sources.adapters.registry import AdapterMissing, find_adapter, implemented_keys
from minjob_ingest.sources.registry import (
    ConfigError,
    SourceConfig,
    enabled_sources,
    find_source,
    load_sources,
)
from minjob_ingest.store.base import StoreError
from minjob_ingest.store.json_store import JsonStore

_PROGRAM = "minjob-ingest"
_LIST_SOURCES = "list-sources"
_CHECK_GEMINI = "check-gemini"
_COLLECT = "collect"
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
    except StoreError as err:
        print(f"[{_PROGRAM}] 저장소 오류: {err}", file=sys.stderr)
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
    if command == _COLLECT:
        config_value = args.config
        return _run_collect(
            config_path=Path(str(config_value)) if config_value is not None else None,
            only=str(args.source) if args.source is not None else None,
            months=int(args.months) or None,
            max_pages=int(args.pages),
            dry_run=bool(args.dry_run),
        )
    # argparse가 이미 미등록 명령을 걸러내므로, 여기 오는 건 "서브파서는 추가했는데 연결을
    # 잊은" 경우다 — 조용히 성공(0)하는 대신 크래시로 알린다.
    raise RuntimeError(f"명령 '{command}'이 _dispatch에 연결되지 않았다")


def _run_collect(
    *, config_path: Path | None, only: str | None, months: int | None, max_pages: int, dry_run: bool
) -> int:
    """게시판에서 공고를 수집한다. ⚠️ **게시판에 실제로 요청한다.**

    소스 단위로 격리한다 — 한 곳이 실패해도 나머지를 계속한다(SPEC §3). 종료코드는 실패한
    소스가 있으면 1이다(운영자가 `status` 없이도 알 수 있게).
    """
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    settings = Settings.load()
    sources = _collect_targets(load_sources(config_path), only)
    options = CollectOptions(months=months, max_pages=max_pages, dry_run=dry_run)
    store = JsonStore(settings.data_dir)
    # dry-run은 아무것도 쓰지 않는다 — 실행 기록(crawl_run)도 남기지 않는다.
    run = None if dry_run else store.start_run(CrawlMode.BACKFILL)
    today = utc_now().date()

    failures: dict[str, str] = {}
    saved_total = 0
    for source in sources:
        try:
            adapter = find_adapter(source.key)
            with SourceClient(source) as client:
                report = collect_source(
                    source,
                    adapter,
                    client,
                    store,
                    run_id=None if run is None else run.id,
                    options=options,
                    today=today,
                )
        except (AdapterMissing, FetchError, ParseError, LedgerConflict) as err:
            failures[source.key] = f"{type(err).__name__}: {err}"
            print(f"  ❌ {source.key}: {err}", file=sys.stderr)
            continue
        saved_total += report.saved
        _print_report(report, dry_run=dry_run)

    if run is not None:
        store.finish_run(
            run.finish(
                sources_ok=len(sources) - len(failures),
                sources_failed=len(failures),
                new_count=saved_total,
                error_detail=failures,
            )
        )
    print(f"\n소스 {len(sources)}곳 · 실패 {len(failures)} · 신규 {saved_total}건")
    if dry_run:
        print("(--dry-run — 아무것도 저장하지 않았습니다)")
    return 1 if failures else 0


def _collect_targets(sources: Sequence[SourceConfig], only: str | None) -> tuple[SourceConfig, ...]:
    if only is not None:
        found = find_source(sources, only)
        if found is None:
            raise ConfigError(f"알 수 없는 source_key: {only}")
        return (found,)
    # 어댑터가 있는 곳만. 없는 곳까지 돌면 매번 실패가 쌓인다(1-4에서 채운다).
    implemented = set(implemented_keys())
    return tuple(s for s in enabled_sources(sources) if s.key in implemented)


def _print_report(report: CollectReport, *, dry_run: bool) -> None:
    print(f"\n{report.source_key}  목록 {report.pages_read}p → {report.rows}행")
    print(
        f"  새 글 {report.fresh} · 이미 본 글 {report.seen} · 범위 밖 {report.stale}"
        + (f" · 페이지 밀림 {report.shifted}" if report.shifted else "")
    )
    if report.newest is not None:
        print(f"  게시일 {report.oldest} ~ {report.newest}")
    for ref in report.samples:
        print(f"    {ref.external_id:<8} {ref.posted_on}  {ref.title[:42]}")
    if report.detail_sample is not None:
        sample = report.detail_sample
        print(
            f"  상세 표본 {sample.ref.external_id}: 본문 {len(sample.raw_text)}자"
            f" · 이미지 {len(sample.image_urls)} · 첨부 {len(sample.attachments)}"
        )
        for attachment in sample.attachments:
            print(f"    첨부 {attachment.name}")
    if not dry_run:
        print(f"  저장 {report.saved}건")


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

    collect = subcommands.add_parser(_COLLECT, help="게시판에서 공고 수집 (게시판에 요청함)")
    collect.add_argument("--source", default=None, help="한 곳만 (기본: 어댑터가 있는 전부)")
    collect.add_argument(
        "--months",
        type=int,
        default=DEFAULT_MONTHS,
        help=f"게시일 기준 수집 범위 (기본 {DEFAULT_MONTHS}개월 · 0이면 날짜로 자르지 않음)",
    )
    collect.add_argument(
        "--pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"목록 페이지 상한 (기본 {DEFAULT_MAX_PAGES})",
    )
    collect.add_argument(
        "--dry-run",
        action="store_true",
        help="저장하지 않고 무엇을 가져올지만 출력 (목록 + 상세 표본 1건)",
    )
    collect.add_argument("--config", default=None, help="sources.json 경로")
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
