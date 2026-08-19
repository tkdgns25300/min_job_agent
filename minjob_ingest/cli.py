"""CLI 진입점 — 운영자가 크롤을 실행하는 창구(CLAUDE.md).

현재 명령: `list-sources`(등록 소스 확인) · `check-gemini`(Vertex 인증 실호출 1회) ·
`snapshot`(fixture용 HTML 확보) · `collect`(게시판에서 공고 수집) · `structure`(AI 구조화).
`daily`·`backfill`·`status`는 이후 단계에서 붙는다.

⚠️ **유료 호출은 `structure` 하나뿐이고 범위를 반드시 받는다**(`--limit N` 또는 `--all`).
`collect`는 무료라 기본 범위가 있지만, 유료 호출이 옵션 없이 도는 일은 없어야 한다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Final
from uuid import UUID

from minjob_ingest.clock import kst_now
from minjob_ingest.console import Console, ProgressLine
from minjob_ingest.domain import Confidence, CrawlMode, DedupState, ReviewStatus
from minjob_ingest.fetch.client import FetchError, SourceClient
from minjob_ingest.lib.gemini import GeminiClient, GeminiError
from minjob_ingest.models import (
    REVIEW_STATE_FIELDS,
    CrawlRun,
    JsonValue,
    ReviewData,
    SourceHealth,
)
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
from minjob_ingest.pipeline.dedup import DedupReport, dedup_all
from minjob_ingest.pipeline.extraction import GeminiExtractor
from minjob_ingest.pipeline.health import (
    Alert,
    AlertKind,
    alerts_for,
    days_since_last_posting,
    record_failure,
    record_success,
)
from minjob_ingest.pipeline.heresy import HeresyRefError, load_ref
from minjob_ingest.pipeline.media import board_media
from minjob_ingest.pipeline.snapshot import (
    SnapshotResult,
    fixture_dir,
    snapshot_source,
    snapshot_url,
)
from minjob_ingest.pipeline.structure import (
    DEFAULT_WORKERS,
    ResultSink,
    StructureOptions,
    StructureReport,
    StructureResult,
    Verdict,
    structure_pending,
)
from minjob_ingest.pipeline.verify import Dropped
from minjob_ingest.settings import (
    ENV_VERTEX_MODEL,
    ENV_VERTEX_MODEL_LITE,
    Settings,
    VertexConfigError,
)
from minjob_ingest.sources.adapters.base import ParseError
from minjob_ingest.sources.adapters.registry import AdapterMissing, find_adapter, implemented_keys
from minjob_ingest.sources.registry import (
    ConfigError,
    SourceConfig,
    enabled_sources,
    find_source,
    load_sources,
)
from minjob_ingest.store.base import Store, StoreError
from minjob_ingest.store.json_store import JsonStore
from minjob_ingest.store.serde import to_row

_PROGRAM = "minjob-ingest"
_LIST_SOURCES = "list-sources"
_CHECK_GEMINI = "check-gemini"
_COLLECT = "collect"
_SNAPSHOT = "snapshot"
_STRUCTURE = "structure"
_DEDUP = "dedup"
#: 요청마다 한 줄씩 찍어 리포트를 덮는 로거들. `--verbose`에서만 켠다.
#: ⚠️ 구조화는 공고마다 한 줄을 찍는다 — 빼두면 전량 실행에서 진행 줄이 수천 줄에 묻힌다.
_NOISY_LOGGERS = ("httpx", "httpcore", "minjob_ingest.lib.gemini")
_ENABLED_MARKER = "●"
_DISABLED_MARKER = "○"
_INTERDENOMINATIONAL_LABEL = "초교파"

_SMOKE_PROMPT = "연결 확인용. 한국어로 정확히 'OK'라고만 답하세요."

#: 뽑히지 않은 값의 표시. 빈 칸으로 두면 "안 뽑힌 것"과 "빈 문자열"이 같아 보인다.
_NO_VALUE: Final = "—"
#: 검산 요약에 보여줄 칸 이름 수. 전부 찍으면 한 줄이 화면을 넘는다.
_FIELD_NOTE_SAMPLE: Final = 4

#: 미리보기에 이름을 펼칠 빈 칸 개수. 40개를 다 찍으면 정작 뽑힌 값이 묻힌다.
_UNFILLED_SAMPLES: Final = 6


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
    except HeresyRefError as err:
        # ⚠️ 목록 없이 돌면 이단으로 규정된 교회의 공고가 검수 큐에 그대로 올라간다.
        #    유료 호출을 시작하기 전에 여기서 멈춘다(SPEC §5.4).
        print(f"[{_PROGRAM}] 이단 참고 목록 오류: {err}", file=sys.stderr)
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
        return _run_check_gemini(lite=bool(args.lite))
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
    if command == _STRUCTURE:
        return _run_structure(
            # `--all`은 "상한 없음"이고 `--limit N`은 N건이다. 둘 중 하나가 반드시 온다
            # (파서의 required 배타 그룹) — 기본값으로 전량이 도는 경로를 만들지 않는다.
            limit=None if bool(args.all) else int(args.limit),
            source_key=str(args.source) if args.source is not None else None,
            dry_run=bool(args.dry_run),
            verbose=bool(args.verbose),
            out=Path(str(args.out)) if args.out is not None else None,
            lite=bool(args.lite),
            workers=int(args.workers),
        )
    if command == _DEDUP:
        return _run_dedup(dry_run=bool(args.dry_run), verbose=bool(args.verbose))
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
    store: Store,
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
    return (_require_source(sources, only),)


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
    console.field("저장 위치", "tests/fixtures/<KEY>/", note="커밋되지 않습니다")


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
        return (_require_source(sources, only),)
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


def _run_structure(
    *,
    limit: int | None,
    source_key: str | None,
    dry_run: bool,
    verbose: bool,
    out: Path | None,
    lite: bool,
    workers: int,
) -> int:
    """수집한 원자료를 AI로 구조화해 검수 초안(`review_data`)을 만든다.

    ⚠️ **유료 호출이다**. 범위는 `--limit N` 또는 `--all`이
    반드시 정한다: 기본값으로 도는 경로를 두면 실수 한 번이 남은 전량을 호출한다.
    """
    options = StructureOptions(limit=limit, source_key=_registered_key(source_key), dry_run=dry_run)
    console = Console()
    settings = Settings.load()
    store = JsonStore(settings.data_dir)
    # ⚠️ **유료 호출을 시작하기 전에** 이단 목록을 읽는다. 뒤로 미루면 3,000건을 부른 뒤
    #    목록이 없다는 것을 알게 되고, 그때는 이미 이단 교회 공고가 검수 큐에 들어가 있다.
    heresy = load_ref(settings.heresy_path)
    client = GeminiClient(settings.require_vertex(lite=lite))
    extractor = GeminiExtractor(client)

    console.heading(
        "구조화 미리보기" if dry_run else "구조화", note=_structure_scope(options, workers)
    )
    # ⚠️ 모델 이름을 **실행마다 찍는다** — 두 모델을 견주는 실행에서 어느 쪽 결과인지
    #    화면으로 확인할 수 없으면 `--out` 파일이 뒤바뀐 것을 알아낼 방법이 없다.
    console.field("모델", client.model, note="--lite" if lite else ENV_VERTEX_MODEL)
    console.field("이단 목록", str(settings.heresy_path.name), note=f"{len(heresy.entries)}건")
    line = console.progress()
    preview = None if out is None else _PreviewFile(out, model=client.model)
    sinks: list[ResultSink] = [_structure_renderer(console, line, dry_run=dry_run)]
    if preview is not None:
        sinks.append(preview.add)
    with _console_logging(console, verbose=verbose), board_media(_open_source_client) as images:
        report = structure_pending(
            store,
            extractor,
            options,
            heresy=heresy,
            on_result=_fan_out(sinks),
            images=images,
            workers=workers,
        )
    line.clear()
    if preview is not None:
        preview.write()
        console.field("미리보기 파일", str(out), note=f"{preview.count}건")
    _print_structure_report(console, report, dry_run=dry_run)
    if not dry_run:
        # ⚠️ **잊어버릴 자리에 두지 않는다.** 자동 승인이 켜진 이상(SPEC §5.7) dedup을 빼먹으면
        #    같은 자리가 최대 26번 그대로 공개된다. 무료·무네트워크·멱등이라 매번 돌려도 된다.
        #    `--dry-run`에서는 돌리지 않는다 — 저장된 것이 없으니 판정할 것도 없다.
        console.heading("중복 판정", note="구조화 결과 전체를 다시 훑는다")
        _print_dedup_report(console, dedup_all(store, dry_run=False), dry_run=False)
    # 멈춘 실행은 실패도 함께 세어져 있다(`_Tally._watch_store` — 저장 실패는 FAILED다).
    return 1 if report.failed else 0


def _open_source_client(source_key: str) -> SourceClient:
    """그림을 받아올 클라이언트. 게시판 설정(UA·인코딩·TLS·세션)을 그대로 쓴다.

    ⚠️ 소스별로 하나씩만 만들어야 요청 간격과 세션이 유지된다 — 재사용은 `BoardMediaSource`가
    한다(SPEC §3 한 호스트에 요청 1개).
    """
    return SourceClient(_require_source(load_sources(None), source_key))


def _fan_out(sinks: Sequence[ResultSink]) -> ResultSink:
    def deliver(result: StructureResult, progress: StructureReport) -> None:
        for sink in sinks:
            sink(result, progress)

    return deliver


class _PreviewFile:
    """구조화 결과를 파일로 모은다 — **프롬프트를 비교하는 도구**다.

    ⚠️ 20건에 34필드면 터미널로 볼 수 없다. 프롬프트를 고치고 무엇이 달라졌는지 알려면 두
    실행의 결과를 **diff** 할 수 있어야 한다. 그래서 사람이 읽는 출력이 아니라 **줄 단위로
    비교되는 형식**(들여쓴 JSON · 키 정렬)으로 쓴다.

    ⚠️ **`id`·`created_at`은 뺀다.** 실행마다 새로 생기므로 그대로 두면 값이 하나도 안
    바뀌었는데 **전 레코드가 달라진 것처럼** 보여 diff가 무용지물이 된다.
    """

    #: 실행마다 달라져 비교를 방해하는 칸.
    _VOLATILE: Final = ("id", "created_at")

    def __init__(self, path: Path, *, model: str) -> None:
        self._path = path
        self._model = model
        self._rows: list[dict[str, JsonValue]] = []

    @property
    def count(self) -> int:
        return len(self._rows)

    def add(self, result: StructureResult, _progress: StructureReport) -> None:
        row: dict[str, JsonValue] = {
            "posting": result.record.label,
            "source_url": result.record.source_url,
            # ⚠️ **어느 모델이 답했나를 파일에 적는다.** 두 모델을 견주는 실행에서 파일 이름만
            #    믿으면 뒤바뀐 것을 알아낼 방법이 없다 — 결론이 조용히 반대가 된다.
            "model": self._model,
            "verdict": result.verdict.value,
        }
        if result.error is not None:
            row["error"] = result.error
        if result.media_note is not None:
            row["media_note"] = result.media_note
        # ⚠️ 공고 단위로 남겨야 "모델이 null을 줬다"와 "검산이 비웠다"를 구분할 수 있다.
        if result.verified.scrubbed:
            row["scrubbed"] = list(result.verified.scrubbed)
            # ⚠️ **버린 값까지 남긴다.** 칸 이름만으로는 "모델이 뭐라고 답했길래 지웠나"를
            #    알 수 없어 과검을 검수할 수 없다 — 실측에서 여기서 검수가 막혔다.
            row["dropped"] = [
                {"field": item.field, "value": item.value, "evidence": item.evidence}
                for item in result.verified.dropped
            ]
        if result.verified.unchecked:
            row["unchecked"] = dict(result.verified.unchecked_fields)
        if result.draft is not None:
            row["draft"] = {
                key: value
                for key, value in sorted(to_row(result.draft).items())
                if key not in self._VOLATILE
            }
        self._rows.append(row)

    def write(self) -> None:
        """마지막에 한 번 쓴다.

        ⚠️ 도중에 죽으면 파일이 남지 않는다 — 이건 검수 도구이고 표본은 작다. 전량 실행의
        기록이 필요해지면 그때 줄 단위로 흘려 쓴다(ROADMAP 1-2 3단계).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _registered_key(source_key: str | None) -> str | None:
    """`--source`를 등록된 키로 맞춘다 — `collect`와 같은 규칙(대소문자 무시 · 오타는 즉시 오류).

    맞춰주지 않으면 소문자 입력이 **저장소 경계에서 형식 오류로 터지고**, 오타는 "처리할
    공고가 없습니다"로 조용히 넘어가 운영자가 다 끝난 줄 안다.

    ⚠️ `--config`를 받지 않는다 — 이 명령은 게시판에 접속하지 않고 config를 **키 확인에만**
    쓴다. 수집한 데이터의 키는 이미 정본 config로 저장된 값이다.
    """
    if source_key is None:
        return None
    return _require_source(load_sources(None), source_key).key


def _require_source(sources: Sequence[SourceConfig], key: str) -> SourceConfig:
    """등록된 소스를 찾는다(대소문자 무시). 없으면 `ConfigError` — 오타를 조용히 넘기지 않는다."""
    found = find_source(sources, key)
    if found is None:
        raise ConfigError(f"알 수 없는 source_key: {key}")
    return found


def _structure_scope(options: StructureOptions, workers: int) -> str:
    scope = "미판정 전부" if options.limit is None else f"최대 {options.limit}건"
    if options.source_key is not None:
        # 게시판을 하나로 좁히면 스레드도 하나다 — 숫자를 보여주면 안 도는 병렬을 돈다고 읽는다.
        return f"{scope} · {options.source_key}"
    return scope if workers == 1 else f"{scope} · 게시판 {workers}곳씩"


def _structure_renderer(console: Console, line: ProgressLine, *, dry_run: bool) -> ResultSink:
    """미리보기는 공고마다 뽑힌 값을 펼치고, 실제 실행은 한 줄에서 진행만 알린다.

    `--dry-run`의 목적이 **눈으로 확인하는 것**이라 값을 접으면 쓸모가 없다. 반대로 전량
    실행은 수천 건이라 한 줄에서 갱신한다 — 2~3시간짜리라 표시가 없으면 멈춘 줄 안다.
    """

    def render(result: StructureResult, progress: StructureReport) -> None:
        if dry_run:
            _print_preview(console, result)
        else:
            line.update(_structure_progress(progress))

    return render


def _structure_progress(progress: StructureReport) -> str:
    counted = (
        (Verdict.DRAFTED, progress.drafted),
        (Verdict.EXCLUDED, progress.excluded),
        (Verdict.EMPTY, progress.empty),
        (Verdict.DEFERRED, progress.deferred),
        (Verdict.FAILED, progress.failed),
    )
    parts = [f"{progress.scanned}건 처리"]
    parts += [f"{_verdict_label(verdict)} {count}" for verdict, count in counted if count]
    return "⋯ " + " · ".join(parts)


def _verdict_label(verdict: Verdict) -> str:
    """판정의 화면 라벨. ⚠️ `match`로 둔다 — 판정이 늘면 mypy가 여기서 잡아준다."""
    match verdict:
        case Verdict.DRAFTED:
            return "초안"
        case Verdict.EXCLUDED:
            return "제외"
        case Verdict.EMPTY:
            return "빈 공고"
        case Verdict.DEFERRED:
            return "그림 대기"
        case Verdict.FAILED:
            return "실패"


def _dropped_note(item: Dropped) -> str:
    """버린 값 한 줄. 근거가 있으면 함께 보여준다 — 값만으로는 왜 떨어졌는지 안 보인다."""
    return item.value if item.evidence is None else f"{item.value}  ← 근거 {item.evidence!r}"


def _print_preview(console: Console, result: StructureResult) -> None:
    """미리보기는 **저장될 레코드**를 보여준다 — 모델이 답한 것만 보여주면 안 된다.

    ⚠️ 운영자가 확인하려는 것은 "이대로 `review_data`에 들어가도 되는가"다. 모델 응답만
    찍으면 실제로 붙는 값(`confidence`·교단 근거·검수 상태)이 안 보이고, 아직 비어 있는
    칸이 몇 개인지도 알 수 없어 "왜 이렇게 적지?"가 된다.
    """
    console.heading(result.record.label, note=_verdict_label(result.verdict))
    draft = result.draft
    if draft is None:
        _print_extraction_only(console, result)
    else:
        console.field("저장 위치", f"review_data ({draft.review_status.value})")
        console.field("개교회 채용", draft.is_church_recruitment.value)
        console.field("교회명", draft.church_name or _NO_VALUE)
        console.field("제목", draft.title or _NO_VALUE)
        console.field("요약", draft.description or _NO_VALUE)
        console.field(
            "신뢰도",
            draft.confidence.value,
            note="자동 승인" if draft.confidence is Confidence.HIGH else "운영자 검수",
        )
        console.field(
            "교단", str(draft.denomination or "—"), note=f"근거 {draft.denomination_source.value}"
        )
        console.field("원문 링크", draft.source_url)
        _note_unfilled_columns(console, draft)
    if result.verified.scrubbed:
        # ⚠️ 이게 없으면 빈 칸을 보고 "모델이 null 을 줬다"와 "검산이 비웠다"를 구분할 수 없다.
        console.warn(
            "원문에 없어 비운 칸: " + _field_note(Counter(result.verified.scrubbed)),
            "원문에서 찾지 못한 값입니다 — 검수에서 원문과 대조해 채우거나 되돌립니다.",
        )
        for item in result.verified.dropped:
            # ⚠️ 버린 **값**까지 보여준다. 칸 이름만으로는 지어낸 것인지 검산이 과한 것인지
            #    가릴 수 없다 — 실측에서 전화번호 4건이 과검이었다(2026-08-14).
            console.field(f"  버림 {item.field}", _dropped_note(item))
    if result.verified.unchecked:
        console.field(
            "원문에서 확인 못 함",
            f"{result.verified.unchecked}개",
            note=_field_note(result.verified.unchecked_fields),
        )
    if result.media_note is not None:
        console.warn(result.media_note)
    if result.error is not None:
        console.warn(result.error)


def _print_extraction_only(console: Console, result: StructureResult) -> None:
    """초안을 만들지 않는 판정(게이트1 NO·빈 공고·이미지 대기)의 근거를 보여준다."""
    extraction = result.extraction
    if extraction is None:
        console.field("초안", "만들지 않음", note=_verdict_reason(result.verdict))
        return
    console.field("개교회 채용", extraction.is_church_recruitment.value)
    console.field("교회명", extraction.church_name or _NO_VALUE)
    console.field("초안", "만들지 않음", note=_verdict_reason(result.verdict))


def _verdict_reason(verdict: Verdict) -> str:
    match verdict:
        case Verdict.EXCLUDED:
            return "개교회 채용이 아님"
        case Verdict.EMPTY:
            return "본문·이미지·첨부가 없어 호출하지 않음"
        case Verdict.DEFERRED:
            return "그림을 가져올 수단 없이 실행됨"
        case Verdict.FAILED:
            return "실패 — 다음 실행이 다시 시도"
        case Verdict.DRAFTED:
            return "초안 있음"


def _note_unfilled_columns(console: Console, draft: ReviewData) -> None:
    """아직 비어 있는 칸을 세어 보여준다.

    ⚠️ 이게 없으면 1단계 결과가 "데이터가 빠진 것"처럼 보인다 — 실제로는 **아직 안 뽑는
    칸**이고 2단계가 채운다. 화면에서 바로 알 수 있어야 같은 질문이 반복되지 않는다.
    """
    empty = [
        info.name
        for info in fields(draft)
        if info.name not in REVIEW_STATE_FIELDS and not getattr(draft, info.name)
    ]
    if not empty:
        return
    console.bullet(
        console.paint(f"아직 비어 있는 칸 {len(empty)}개 — 2단계에서 채운다: ", "dim")
        + console.paint(" · ".join(empty[:_UNFILLED_SAMPLES]) + " …", "dim")
    )


def _draft_note(report: StructureReport, *, dry_run: bool) -> str:
    """⚠️ 초안이 전부 검수 대기는 아니다 — 자동 승인·자동 거절이 섞여 있다(SPEC §5.7)."""
    if dry_run:
        return "저장하지 않음(미리보기)"
    pending = report.statuses.get(ReviewStatus.PENDING.value, 0)
    return f"검수 대기 {pending}건(PENDING)"


def _print_structure_report(console: Console, report: StructureReport, *, dry_run: bool) -> None:
    console.line()
    if not report.scanned:
        console.warn(
            "처리할 공고가 없습니다.",
            "이미 전부 판정됐거나(structured_at), 시도 상한을 넘겼거나,",
            "--source 가 가리키는 게시판에 남은 것이 없습니다.",
        )
        return
    if report.halted is not None:
        # ⚠️ 제일 위에 놓는다 — 아래 숫자들이 "끝까지 돈 결과"가 아니라는 사실이 먼저다.
        console.warn(f"⛔ {report.halted}", "남은 공고는 다음 실행이 다시 잡습니다.")
    console.field("훑음", f"{report.scanned}건")
    console.field(
        "초안",
        console.paint(f"{report.drafted}건", "green", "bold"),
        note=_draft_note(report, dry_run=dry_run),
    )
    approved = report.statuses.get(ReviewStatus.APPROVED.value, 0)
    if approved:
        # ⚠️ **자동 승인 수를 화면에 내놓는다.** 사람을 거치지 않고 공개되므로, 규칙이
        #    느슨해져 그 수가 튀는 것을 여기서 알아채야 한다(SPEC §5.7).
        console.field(
            "  ↳ 자동 승인",
            console.paint(f"{approved}건", "green", "bold"),
            note="사람을 거치지 않고 공개된다",
        )
    if report.rejected:
        # ⚠️ **거절을 화면에 내놓는다.** 초안은 만들어졌지만 검수 큐에 뜨지 않는다 —
        #    여기서 안 보이면 잘못 걸러도 아무도 모른다(SPEC §5.4).
        breakdown = " · ".join(f"{name} {count}" for name, count in report.rejected_reasons.items())
        console.field(
            "  ↳ 자동 거절",
            console.paint(f"{report.rejected}건", "yellow"),
            note=f"{breakdown} — 검수 큐에 뜨지 않는다",
        )
    if report.excluded:
        console.field("제외", f"{report.excluded}건", note="개교회 채용이 아님 — 초안 없음")
    if report.empty:
        console.field("빈 공고", f"{report.empty}건", note="내용이 없어 호출하지 않음")
    if report.deferred:
        console.field("그림 대기", f"{report.deferred}건", note="그림을 가져올 수단 없이 실행됨")
    if report.scrubbed:
        # ⚠️ 조용히 지우지 않는다. 이 줄이 없으면 프롬프트를 고쳤을 때 나아졌는지 알려고
        #    매번 원문과 손으로 대조해야 한다.
        console.field(
            "검산에서 비움",
            f"{report.scrubbed}개",
            note=_field_note(report.scrubbed_fields),
        )
    if report.unverifiable:
        console.field(
            "본문 확인 못 함",
            f"{report.unverifiable}개",
            note="그림·PDF 공고 — 포스터가 원문이라 비우지 않았습니다",
        )
    if report.unchecked:
        console.field(
            "원문에서 확인 못 함",
            f"{report.unchecked}개",
            note=_field_note(report.unchecked_fields),
        )
    if report.text_only:
        console.warn(
            f"그림을 못 읽고 텍스트만으로 판정한 공고 {report.text_only}건"
            " — 포스터 공고면 내용을 못 본 채 판정된 것입니다.",
            *(f"{item.posting}: {item.reason}" for item in report.media_failures),
        )
    if report.failed:
        console.warn(
            f"구조화 실패 {report.failed}건 — 판정을 남기지 않아 다음 실행이 다시 시도합니다.",
            *(f"{failure.posting}: {failure.reason}" for failure in report.failures),
        )
    if dry_run:
        console.line()
        console.ok("미리보기입니다 — 저장하지 않았으므로 같은 공고를 다시 볼 수 있습니다.")


def _field_note(counts: Mapping[str, int]) -> str:
    """`required_docs 3 · headcount 2` — 많이 걸린 칸부터.

    어느 칸이 문제인지가 다음 프롬프트 수정을 정한다.
    """
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return " · ".join(f"{name} {count}" for name, count in ranked[:_FIELD_NOTE_SAMPLE])


def _positive_int(value: str) -> int:
    """`--limit` 전용. 0·음수를 허용하면 **아무것도 안 하는 실행이 조용히 성공**한다."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"1 이상이어야 합니다 ({value})")
    return parsed


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

    check_gemini = subcommands.add_parser(_CHECK_GEMINI, help="Vertex 인증·연결 확인 (실호출 1회)")
    check_gemini.add_argument(
        "--lite",
        action="store_true",
        help=f"{ENV_VERTEX_MODEL_LITE} 모델로 (기본은 {ENV_VERTEX_MODEL})",
    )

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

    structure = subcommands.add_parser(
        _STRUCTURE, help="수집한 공고를 AI로 구조화 (⚠️ 유료 호출 · 게시판에는 요청하지 않음)"
    )
    # ⚠️ **비용 사고 방지**: 범위를 반드시 받는다. 기본값을 두면 옵션 없는 실행이 남은
    # 전량(수천 건)을 호출한다 — `collect`는 무료라 기본 범위가 있지만 여기는 다르다.
    scope = structure.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="판정할 최대 건수 = 유료 호출 상한 (예: --limit 20)",
    )
    scope.add_argument(
        "--all", action="store_true", help="미판정 전부 (⚠️ 남은 건수만큼 유료 호출이 나간다)"
    )
    structure.add_argument("--source", default=None, help="한 게시판만 (예: YTUS)")
    structure.add_argument(
        "--dry-run", action="store_true", help="호출은 하되 저장 안 함 (프롬프트 확인용)"
    )
    structure.add_argument("--verbose", action="store_true", help="호출 로그까지 표시")
    structure.add_argument(
        "--out",
        default=None,
        help="결과를 JSON 파일로 (프롬프트 비교용 · ⚠️ 연락처가 담긴다 — data/preview/ 아래로)",
    )
    structure.add_argument(
        "--workers",
        type=_positive_int,
        default=DEFAULT_WORKERS,
        help=f"동시에 돌릴 게시판 수 (기본 {DEFAULT_WORKERS} · 게시판 안은 언제나 순차)",
    )
    structure.add_argument(
        "--lite",
        action="store_true",
        help=f"{ENV_VERTEX_MODEL_LITE} 모델로 (기본은 {ENV_VERTEX_MODEL})",
    )

    # ⚠️ `structure`가 끝나면 자동으로 돈다 — 이 명령은 **다시 돌릴 때**를 위한 것이다
    #    (규칙을 고친 뒤 · 무엇이 묶이는지만 볼 때 · 이미 저장된 데이터에 처음 적용할 때).
    dedup = subcommands.add_parser(
        _DEDUP, help="같은 자리가 여러 번 올라온 것을 하나로 (무료 · 게시판에 요청하지 않음)"
    )
    dedup.add_argument(
        "--dry-run", action="store_true", help="판정만 하고 저장 안 함 (무엇이 묶이는지 확인용)"
    )
    dedup.add_argument("--verbose", action="store_true", help="로그까지 표시")
    return parser


def _run_dedup(*, dry_run: bool, verbose: bool) -> int:
    """같은 자리가 여러 번·여러 게시판에 올라온 것을 하나로 줄인다(SPEC §4.1).

    ⚠️ **유료 호출도 게시판 요청도 없다.** 저장된 초안만 보고 판정하므로 몇 번을 돌려도 안전하고,
    같은 데이터면 같은 결과가 나온다.
    """
    console = Console()
    store = JsonStore(Settings.load().data_dir)

    console.heading("중복 판정 미리보기" if dry_run else "중복 판정", note="게시판에 요청하지 않음")
    with _console_logging(console, verbose=verbose):
        report = dedup_all(store, dry_run=dry_run)
    _print_dedup_report(console, report, dry_run=dry_run)
    return 0


def _print_dedup_report(console: Console, report: DedupReport, *, dry_run: bool) -> None:
    console.field("훑음", f"{report.scanned}건")
    duplicates = report.count(DedupState.DUPLICATE)
    if duplicates:
        # ⚠️ **거절한 수를 화면에 내놓는다.** 중복은 검수 큐에 뜨지 않으므로, 잘못 묶어도
        #    여기서 안 보이면 아무도 모른다(SPEC §4.1).
        console.field(
            "중복",
            console.paint(f"{duplicates}건", "yellow"),
            note=f"{report.groups}개 자리로 줄었다 — 검수 큐에 뜨지 않는다",
        )
    uncertain = report.count(DedupState.UNCERTAIN)
    if uncertain:
        console.field(
            "  ↳ 판단 못 함",
            console.paint(f"{uncertain}건", "yellow", "bold"),
            note="부서가 여럿이거나 접수 이메일이 갈렸다 — 사람이 본다",
        )
    alone = report.count(DedupState.ALONE)
    if alone:
        console.field("혼자", f"{alone}건", note="같은 자리가 없다")
    if report.unjudged:
        # ⚠️ 조용히 빠지면 "왜 이 중복이 안 잡히나"에 답할 수 없다.
        console.field(
            "견줄 수 없음",
            f"{report.unjudged}건",
            note="교회명·지역·직분 중 하나가 비었다",
        )
    if report.settled:
        console.field(
            "이미 결론", f"{report.settled}건", note="이단·마감·운영자 거절 — 건드리지 않는다"
        )
    console.field(
        "저장",
        "하지 않음(미리보기)" if dry_run else f"{report.changed}건 갱신",
    )


def _run_check_gemini(*, lite: bool) -> int:
    """서비스계정 인증이 실제로 통하는지 실호출 1번으로 검증한다(셋업 함정 조기 제거).

    ⚠️ 유일하게 **외부 유료 API를 직접 호출**하는 명령이다(토큰 소량). 게시판은 건드리지 않는다.
    """
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    settings = Settings.load()
    client = GeminiClient(settings.require_vertex(lite=lite))
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
