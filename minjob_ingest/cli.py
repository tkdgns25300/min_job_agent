"""CLI 진입점 — 운영자가 크롤을 실행하는 창구(CLAUDE.md 가드레일 #10).

현재 명령: `list-sources`(등록 소스 확인) · `check-gemini`(Vertex 인증 실호출 1회) ·
`collect`(게시판에서 공고 수집). `structure`·`daily`·`backfill`은 이후 단계에서 붙는다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Final
from uuid import UUID

from minjob_ingest.clock import kst_now
from minjob_ingest.console import Console, ProgressLine
from minjob_ingest.domain import CrawlMode
from minjob_ingest.fetch.client import FetchError, SourceClient
from minjob_ingest.lib.gemini import GeminiClient, GeminiError
from minjob_ingest.models import CrawlRun, SourceHealth
from minjob_ingest.paths import PROJECT_ROOT
from minjob_ingest.pipeline.collect import (
    DEFAULT_MONTHS,
    CollectOptions,
    CollectReport,
    LedgerConflict,
    Progress,
    ProgressSink,
    collect_source,
)
from minjob_ingest.pipeline.health import (
    Alert,
    AlertKind,
    alerts_for,
    days_since_last_posting,
    record_failure,
    record_success,
)
from minjob_ingest.pipeline.snapshot import (
    SnapshotResult,
    fixture_dir,
    snapshot_source,
    snapshot_url,
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
_SNAPSHOT = "snapshot"
#: 요청마다 한 줄씩 찍어 리포트를 덮는 로거들. `--verbose`에서만 켠다.
_NOISY_LOGGERS = ("httpx", "httpcore")
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
    if command == _SNAPSHOT:
        config_value = args.config
        return _run_snapshot(
            config_path=Path(str(config_value)) if config_value is not None else None,
            only=str(args.source) if args.source is not None else None,
            url=str(args.url) if args.url is not None else None,
            name=str(args.name),
            verbose=bool(args.verbose),
        )
    if command == _COLLECT:
        config_value = args.config
        return _run_collect(
            config_path=Path(str(config_value)) if config_value is not None else None,
            only=str(args.source) if args.source is not None else None,
            months=None if args.days is not None else (int(args.months) or None),
            days=int(args.days) if args.days is not None else None,
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
    days: int | None,
    dry_run: bool,
    verbose: bool,
) -> int:
    """게시판에서 공고를 수집한다. ⚠️ **게시판에 실제로 요청한다.**

    소스 단위로 격리한다 — 한 곳이 실패해도 나머지를 계속한다(SPEC §3). 종료코드는 실패한
    소스가 있으면 1이다(운영자가 `status` 없이도 알 수 있게).
    """
    console = Console()
    with _console_logging(console, verbose=verbose):
        return _collect_all(console, config_path, only, months=months, days=days, dry_run=dry_run)


def _collect_all(
    console: Console,
    config_path: Path | None,
    only: str | None,
    *,
    months: int | None,
    days: int | None,
    dry_run: bool,
) -> int:
    sources = _collect_targets(load_sources(config_path), only)
    store = JsonStore(Settings.load().data_dir)
    # dry-run은 아무것도 쓰지 않는다 — 실행 기록(crawl_run)도 남기지 않는다.
    run = None if dry_run else store.start_run(CrawlMode.BACKFILL)

    failures: dict[str, str] = {}
    saved_total = 0
    states: list[SourceHealth] = []
    # ⚠️ `finally`가 필요하다: 예상 못 한 예외·Ctrl-C가 실행 기록을 **열린 채**(`finished_at`
    # null) 남기면 그 run은 영구히 미완으로 보이고 `status`가 거짓말을 한다(2026-08-05 실측).
    try:
        for source in sources:
            report = _collect_one(
                console,
                source,
                store,
                run_id=None if run is None else run.id,
                options=CollectOptions(months=months, days=days, dry_run=dry_run),
                failures=failures,
                states=states,
            )
            if report is not None:
                saved_total += report.saved
                _print_report(console, report, dry_run=dry_run)
    except BaseException as err:
        if run is not None:
            failures[_ABORTED] = f"{type(err).__name__}: {err}"
            _finish(store, run, sources, failures, saved_total)
        raise
    else:
        if run is not None:
            _finish(store, run, sources, failures, saved_total)
    _print_summary(console, len(sources), failures, saved_total, states, dry_run=dry_run)
    return 1 if failures else 0


#: 중단 사유를 담는 `error_detail` 키. `source_key`와 겹치지 않게 소문자·밑줄로 둔다.
_ABORTED: Final = "_aborted"


def _finish(
    store: JsonStore,
    run: CrawlRun,
    sources: Sequence[SourceConfig],
    failures: Mapping[str, str],
    saved_total: int,
) -> None:
    """실행 기록을 닫는다. 소스 실패 수는 `error_detail`이 아니라 **소스 키 수**로 센다."""
    failed_keys = {key for key in failures if key != _ABORTED}
    store.finish_run(
        run.finish(
            sources_ok=len(sources) - len(failed_keys),
            sources_failed=len(failed_keys),
            new_count=saved_total,
            error_detail=failures,
        )
    )


def _run_snapshot(
    *,
    config_path: Path | None,
    only: str | None,
    url: str | None,
    name: str,
    verbose: bool,
) -> int:
    """fixture용 HTML을 받아 `tests/fixtures/<KEY>/`에 저장한다. ⚠️ **게시판에 실제로 요청한다.**

    어댑터를 *만들기 전에* 필요하므로 어댑터를 요구하지 않는다. 소스 단위로 격리한다.
    """
    console = Console()
    with _console_logging(console, verbose=verbose):
        return _snapshot_all(console, config_path, only, url=url, name=name)


def _snapshot_all(
    console: Console, config_path: Path | None, only: str | None, *, url: str | None, name: str
) -> int:
    sources = _snapshot_targets(load_sources(config_path), only)
    if url is not None and len(sources) != 1:
        raise ConfigError("--url 은 --source 와 함께 한 곳만 지정해야 한다")
    root = PROJECT_ROOT / "tests" / "fixtures"
    failures: dict[str, str] = {}
    for source in sources:
        console.heading(source.key, note=source.board_name)
        try:
            with SourceClient(source) as client:
                target = fixture_dir(root, source.key)
                result = (
                    snapshot_url(client, url, target, name)
                    if url is not None
                    else snapshot_source(source, client, target)
                )
        except (FetchError, OSError) as err:
            failures[source.key] = f"{type(err).__name__}: {err}"
            console.error(str(err))
            continue
        _print_snapshot(console, result)
    _print_snapshot_summary(console, len(sources), failures)
    return 1 if failures else 0


def _snapshot_targets(
    sources: Sequence[SourceConfig], only: str | None
) -> tuple[SourceConfig, ...]:
    """어댑터 유무와 무관하게 **활성 소스 전부**가 대상이다(fixture가 어댑터보다 먼저다)."""
    if only is None:
        return tuple(enabled_sources(sources))
    found = find_source(sources, only)
    if found is None:
        raise ConfigError(f"알 수 없는 source_key: {only}")
    return (found,)


def _print_snapshot(console: Console, result: SnapshotResult) -> None:
    for path in result.saved:
        size = path.stat().st_size
        console.field(path.name, f"{size:,}바이트")
    if result.detail_skipped is not None:
        console.warn(f"상세 없음 — {result.detail_skipped}")


def _print_snapshot_summary(console: Console, total: int, failures: Mapping[str, str]) -> None:
    console.line()
    console.line(console.paint("── 요약", "bold"))
    console.field("대상", f"{total}곳")
    if failures:
        console.field("실패", console.paint(f"{len(failures)}곳", "red", "bold"))
        for key, reason in failures.items():
            console.bullet(console.paint(f"{key}  {reason}", "red"))
    else:
        console.ok("전부 저장했습니다")
    console.line()
    console.field("저장 위치", "tests/fixtures/<KEY>/", note="커밋되지 않습니다(가드레일 #11)")


def _collect_one(
    console: Console,
    source: SourceConfig,
    store: JsonStore,
    *,
    run_id: UUID | None,
    options: CollectOptions,
    failures: dict[str, str],
    states: list[SourceHealth],
) -> CollectReport | None:
    """게시판 하나. 실패는 `failures`에 담고 `None`을 돌려준다 — 나머지 소스는 계속 돈다.

    잡는 예외는 **예상된 실패만**이다(어댑터 없음·전송·파싱·원장 충돌). 그 밖의 예외는 버그이므로
    그대로 터뜨려 눈에 보이게 한다.

    성공이든 실패든 `source_health`에 남긴다 — 실패를 안 남기면 연속 실패를 셀 수 없어 §7 경보가
    죽는다. `--dry-run`은 아무것도 쓰지 않으므로 상태도 남기지 않는다.
    """
    # 제목을 먼저 낸다 — 진행 줄이 그 아래에서 갱신되고, 그 자리에 최종 리포트가 온다.
    console.heading(source.key, note=source.board_name)
    line = console.progress()
    now = kst_now()
    try:
        with SourceClient(source) as client:
            report = collect_source(
                source,
                find_adapter(source.key),
                client,
                store,
                run_id=run_id,
                options=options,
                today=now.date(),
                on_progress=_progress_renderer(console, line, dry_run=options.dry_run),
            )
    except (AdapterMissing, FetchError, ParseError, LedgerConflict) as err:
        failures[source.key] = f"{type(err).__name__}: {err}"
        line.clear()
        console.error(str(err))
        if not options.dry_run:
            states.append(record_failure(store, source.key, run_id=run_id, at=now, error=str(err)))
        return None
    line.clear()
    if not options.dry_run:
        states.append(record_success(store, report, run_id=run_id, at=now))
    return report


class _ConsoleHandler(logging.Handler):
    """로그를 Console로 흘린다.

    ⚠️ 진행 줄이 있는 동안 로그를 **직접** 찍으면 그 줄에 겹쳐 쓰이고 다음 갱신이 덮어 **사라진다**
    (재시도·`Crawl-delay` 경고가 그렇게 조용히 없어진다). Console을 지나면 진행 줄이 먼저 지워진다.
    """

    def __init__(self, console: Console) -> None:
        super().__init__()
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            self._console.warn(message)
        else:
            self._console.line(f"  {self._console.paint(message, 'dim')}")


@contextmanager
def _console_logging(console: Console, *, verbose: bool) -> Iterator[None]:
    """이 명령이 도는 동안만 로그를 Console로 보낸다.

    기본은 우리 메시지만 보여준다 — `httpx`는 INFO에서 요청마다 한 줄씩 찍어 리포트를 덮으므로
    진단이 필요할 때만 켠다.

    ⚠️ **핸들러를 남겨두면 안 된다.** Console이 붙은 스트림은 명령이 끝나면 닫히는데, 핸들러가
    살아 있으면 그 뒤의 로그가 닫힌 스트림에 쓰여 터진다(테스트 20개가 실제로 깨졌다).
    기존 핸들러를 지우지도 않는다 — 우리 것만 붙이고 우리 것만 뗀다.
    """
    root = logging.getLogger()
    handler = _ConsoleHandler(console)
    noisy = [logging.getLogger(name) for name in _NOISY_LOGGERS]
    saved = [(logger, logger.level) for logger in (root, *noisy)]
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if not verbose:
        for logger in noisy:
            logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        root.removeHandler(handler)
        for logger, level in saved:
            logger.setLevel(level)


def _progress_renderer(console: Console, line: ProgressLine, *, dry_run: bool) -> ProgressSink:
    """진행 스냅샷 → 한 줄. 형식을 아는 건 CLI뿐이다(파이프라인은 콘솔을 모른다)."""

    def render(progress: Progress) -> None:
        parts = [f"{progress.page}p", f"{progress.rows}행", f"새 글 {progress.fresh}"]
        if not dry_run:
            parts.append(f"저장 {progress.details_done}/{progress.fresh}")
        latest = progress.latest
        tail = f"  {latest.external_id} {latest.title}" if latest is not None else ""
        line.update(f"  {console.paint('⋯ ' + ' · '.join(parts), 'dim')}{tail}")

    return render


def _collect_targets(sources: Sequence[SourceConfig], only: str | None) -> tuple[SourceConfig, ...]:
    if only is not None:
        found = find_source(sources, only)
        if found is None:
            raise ConfigError(f"알 수 없는 source_key: {only}")
        return (found,)
    # 어댑터가 있는 곳만. 없는 곳까지 돌면 매번 실패가 쌓인다(1-4에서 채운다).
    implemented = set(implemented_keys())
    return tuple(s for s in enabled_sources(sources) if s.key in implemented)


def _print_report(console: Console, report: CollectReport, *, dry_run: bool) -> None:
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
    _warn_if_details_failed(console, report)
    _note_empty_postings(console, report)
    _warn_if_short(console, report)


def _note_empty_postings(console: Console, report: CollectReport) -> None:
    """내용 없이 올라온 글의 개수를 알린다. **실패가 아니라 사실이다.**

    게시판에 그런 글이 실제로 있어 실패로 두면 매 실행 다시 받는다. 다만 개수가 크면 본문
    셀렉터가 일부 스킨에서 빗나간 신호이므로 눈에 보여야 한다(전량이면 소스 실패로 올라간다).
    """
    if not report.empty:
        return
    console.field("빈 공고", f"{report.empty}건", note="본문·이미지·첨부 없음 — 저장은 됨")


def _warn_if_details_failed(console: Console, report: CollectReport) -> None:
    """상세를 못 읽은 글이 있으면 알린다.

    한 건의 실패로 게시판 전체를 포기하지 않되(SPEC §4) **조용히 넘기지도 않는다** — 개수와
    사유를 보여줘야 운영자가 셀렉터가 조금씩 어긋나는 것을 알아챈다.
    """
    if not report.failed:
        return
    console.warn(
        f"상세를 못 읽은 글 {report.failed}건 — 나머지는 계속 수집했습니다.",
        *report.failure_samples,
    )


def _warn_if_short(console: Console, report: CollectReport) -> None:
    """안전 상한에 걸려 요청 범위를 못 채웠으면 알린다.

    이게 없으면 "범위 밖 0"만 보고 3개월을 다 받은 줄 안다 — 조용한 미달이다. 범위는 `--months`가
    정하고 상한은 폭주 방지용이므로, **여기 걸리는 것은 정상 상황이 아니다** — 게시일 파싱이
    깨졌다는 뜻이다(운영자가 옵션으로 풀 문제가 아니라 어댑터를 봐야 한다).
    """
    if not report.short_of_cutoff:
        return
    console.warn(
        f"안전 상한 {report.max_pages}페이지까지 읽었는데도 컷오프 {report.cutoff}에"
        " 도달하지 못했습니다.",
        "게시일 파싱이 깨졌을 가능성이 높습니다 — 어댑터의 날짜 셀렉터를 확인하세요.",
    )


def _print_summary(
    console: Console,
    total: int,
    failures: Mapping[str, str],
    saved: int,
    states: Sequence[SourceHealth],
    *,
    dry_run: bool,
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
        return
    console.field("신규", console.paint(f"{saved}건", "green", "bold"))
    _print_alerts(console, states)


def _print_alerts(console: Console, states: Sequence[SourceHealth]) -> None:
    """게시판별 경보. **이게 없으면 31곳 리포트를 눈으로 비교해야 조용한 실패를 잡는다.**"""
    today = kst_now().date()
    alerts = [alert for health in states for alert in alerts_for(health, today=today)]
    if not alerts:
        return
    console.line()
    for alert in alerts:
        text = f"{alert.source_key}  {_alert_sentence(alert, today=today)}"
        if alert.kind.is_warning:
            console.warn(text)
        else:
            console.bullet(console.paint(f"· {text}", "dim"))


def _alert_sentence(alert: Alert, *, today: date) -> str:
    """사유별 문장. 판정은 pipeline이 하고 **표현은 여기서만** 한다."""
    health = alert.health
    if alert.kind is AlertKind.FETCH_FAILING:
        return f"{health.consecutive_failures}회 연속 실패 — {health.last_error}"
    if alert.kind is AlertKind.LISTING_EMPTY:
        since = health.last_success_at
        return (
            f"목록 0행 {health.consecutive_empty_runs}회 연속 —"
            f" 셀렉터 또는 로그인벽 확인 (마지막 성공 {since:%Y-%m-%d})"
            if since is not None
            else f"목록 0행 {health.consecutive_empty_runs}회 연속 — 셀렉터 확인"
        )
    elapsed = days_since_last_posting(health, today=today)
    if elapsed is None:
        # 여기 오는 것은 컷오프가 있었던 경우뿐이다(날짜를 안 주는 게시판은 경보 대상이 아니다 —
        # `_has_no_recent_postings`). 그래서 `last_cutoff`는 항상 값이 있다.
        return f"{health.last_cutoff} 이후 올라온 글이 없습니다 — 게시판이 조용합니다"
    return f"최신 글이 {health.last_posted_on} ({elapsed}일 전) — 게시판이 조용합니다"


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

    snapshot = subcommands.add_parser(
        _SNAPSHOT, help="fixture용 HTML 확보 (게시판에 요청함 · 어댑터 없어도 동작)"
    )
    snapshot.add_argument("--source", default=None, help="한 곳만 (기본: 활성 전부)")
    snapshot.add_argument("--url", default=None, help="임의 URL 1장만 받는다 (2페이지·특수 경로)")
    snapshot.add_argument(
        "--name", default="page2.html", help="--url 로 받을 때 저장할 파일명 (기본 page2.html)"
    )
    snapshot.add_argument("--verbose", action="store_true", help="HTTP 요청 로그까지 표시")
    snapshot.add_argument("--config", default=None, help="sources.json 경로")

    collect = subcommands.add_parser(_COLLECT, help="게시판에서 공고 수집 (게시판에 요청함)")
    collect.add_argument("--source", default=None, help="한 곳만 (기본: 어댑터가 있는 전부)")
    # 범위는 **하나로** 정한다 — 둘 다 받으면 리포트만 보고 어느 범위로 돌았는지 모른다.
    window = collect.add_mutually_exclusive_group()
    window.add_argument(
        "--months",
        type=int,
        default=DEFAULT_MONTHS,
        help=f"게시일 기준 수집 범위 (기본 {DEFAULT_MONTHS}개월 · 0이면 날짜로 자르지 않음)",
    )
    window.add_argument(
        "--days",
        type=int,
        default=None,
        help="범위를 일 단위로 (예: --days 14 = 최근 2주 · --months 와 함께 쓸 수 없음)",
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
