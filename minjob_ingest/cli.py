"""CLI 진입점 — 운영자가 크롤을 실행하는 창구(CLAUDE.md 가드레일 #10).

현재 명령: `list-sources`(등록 소스 확인) · `check-gemini`(Vertex 인증 실호출 1회) ·
`collect`(게시판에서 공고 수집). `structure`·`daily`·`backfill`은 이후 단계에서 붙는다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from minjob_ingest.clock import utc_now
from minjob_ingest.console import Console
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
            verbose=bool(args.verbose),
        )
    # argparse가 이미 미등록 명령을 걸러내므로, 여기 오는 건 "서브파서는 추가했는데 연결을
    # 잊은" 경우다 — 조용히 성공(0)하는 대신 크래시로 알린다.
    raise RuntimeError(f"명령 '{command}'이 _dispatch에 연결되지 않았다")


def _run_collect(
    *,
    config_path: Path | None,
    only: str | None,
    months: int | None,
    max_pages: int,
    dry_run: bool,
    verbose: bool,
) -> int:
    """게시판에서 공고를 수집한다. ⚠️ **게시판에 실제로 요청한다.**

    소스 단위로 격리한다 — 한 곳이 실패해도 나머지를 계속한다(SPEC §3). 종료코드는 실패한
    소스가 있으면 1이다(운영자가 `status` 없이도 알 수 있게).
    """
    _setup_logging(verbose)
    console = Console()
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
            console.heading(source.key, note=source.board_name)
            console.error(str(err))
            continue
        saved_total += report.saved
        _print_report(console, report, source, dry_run=dry_run)

    if run is not None:
        store.finish_run(
            run.finish(
                sources_ok=len(sources) - len(failures),
                sources_failed=len(failures),
                new_count=saved_total,
                error_detail=failures,
            )
        )
    _print_summary(console, len(sources), failures, saved_total, dry_run=dry_run)
    return 1 if failures else 0


def _setup_logging(verbose: bool) -> None:
    """기본은 우리 메시지만 보여준다.

    `httpx`는 INFO에서 요청마다 한 줄씩 찍어 리포트를 덮는다 — 진단이 필요할 때만 켠다.
    """
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _collect_targets(sources: Sequence[SourceConfig], only: str | None) -> tuple[SourceConfig, ...]:
    if only is not None:
        found = find_source(sources, only)
        if found is None:
            raise ConfigError(f"알 수 없는 source_key: {only}")
        return (found,)
    # 어댑터가 있는 곳만. 없는 곳까지 돌면 매번 실패가 쌓인다(1-4에서 채운다).
    implemented = set(implemented_keys())
    return tuple(s for s in enabled_sources(sources) if s.key in implemented)


def _print_report(
    console: Console, report: CollectReport, source: SourceConfig, *, dry_run: bool
) -> None:
    console.heading(report.source_key, note=source.board_name)
    console.field("목록", f"{report.pages_read}페이지 · {report.rows}행")
    console.field(
        "새 글",
        console.paint(str(report.fresh), "bold") if report.fresh else "0",
        note="상세를 요청할 것" if report.fresh else None,
    )
    console.field("이미 본 글", str(report.seen), note="건너뜀" if report.seen else None)
    if report.stale:
        console.field("범위 밖", str(report.stale), note="컷오프보다 오래됨")
    if report.shifted:
        console.field(
            "페이지 밀림", str(report.shifted), note="스캔 중 새 글이 올라옴 · 한 번만 수집"
        )
    if report.newest is not None:
        span = f"{report.oldest} ~ {report.newest}"
        console.field("게시일", span, note=f"컷오프 {report.cutoff}" if report.cutoff else None)
    if not dry_run:
        console.field("저장", console.paint(f"{report.saved}건", "green", "bold"))

    if report.samples:
        console.line()
        for ref in report.samples:
            console.bullet(
                f"{console.paint(ref.external_id, 'cyan')}  {ref.posted_on}  {ref.title[:44]}"
            )
    if report.detail_sample is not None:
        sample = report.detail_sample
        console.field(
            "상세 표본",
            f"{sample.ref.external_id} · 본문 {len(sample.raw_text)}자"
            f" · 이미지 {len(sample.image_urls)} · 첨부 {len(sample.attachments)}",
        )
        for attachment in sample.attachments:
            console.bullet(console.paint(f"첨부 {attachment.name}", "dim"))
    _warn_if_short(console, report)


def _warn_if_short(console: Console, report: CollectReport) -> None:
    """페이지 상한에 걸려 요청 범위를 못 채웠으면 알린다.

    이게 없으면 "범위 밖 0"만 보고 3개월을 다 받은 줄 안다 — 조용한 미달이다.
    """
    if not report.short_of_cutoff:
        return
    needed = report.pages_needed_estimate()
    hint = (
        f"--pages {needed} 정도가 필요합니다(관측 속도 기준 추정)"
        if needed
        else "--pages 를 늘리세요"
    )
    console.warn(
        f"페이지 상한({report.max_pages}p)에서 멈췄습니다 —"
        f" 컷오프 {report.cutoff}에 도달하지 않았습니다.",
        hint,
    )


def _print_summary(
    console: Console, total: int, failures: Mapping[str, str], saved: int, *, dry_run: bool
) -> None:
    console.line()
    console.line(console.paint("── 요약", "bold"))
    console.field("소스", f"{total}곳")
    if failures:
        console.field("실패", console.paint(f"{len(failures)}곳", "red", "bold"))
        for key in failures:
            console.bullet(console.paint(key, "red"))
    if dry_run:
        console.line()
        console.warn("--dry-run — 아무것도 저장하지 않았습니다")
    else:
        console.field("신규", console.paint(f"{saved}건", "green", "bold"))


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
    collect.add_argument("--verbose", action="store_true", help="HTTP 요청 로그까지 표시 (진단용)")
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
