"""CLI 테스트 — 0-1a의 유일한 사용자 접점. 네트워크를 타지 않는다."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from minjob_ingest import cli
from minjob_ingest.cli import _NOISY_LOGGERS, _dispatch, main
from minjob_ingest.clock import kst_now
from minjob_ingest.console import Console
from minjob_ingest.domain import (
    IsChurchRecruitment,
    JobKind,
    Position,
    SourceHealthStatus,
)
from minjob_ingest.fetch.client import FetchError
from minjob_ingest.lib import gemini
from minjob_ingest.models import ReviewData, SourceData, new_id
from minjob_ingest.pipeline.collect import CollectReport
from minjob_ingest.pipeline.extraction import Extraction
from minjob_ingest.pipeline.health import EMPTY_RUNS_ALARM
from minjob_ingest.pipeline.heresy import HeresyEntry, HeresyMatch, HeresyRef
from minjob_ingest.pipeline.structure import (
    DEFAULT_WORKERS,
    StructureOptions,
    StructureReport,
    StructureResult,
    Verdict,
    _Tally,
)
from minjob_ingest.pipeline.structure import (
    build_draft as _build_draft,
)
from minjob_ingest.pipeline.verify import VerifyReport
from minjob_ingest.settings import (
    ENV_DATA_DIR,
    ENV_VERTEX_CLIENT_EMAIL,
    ENV_VERTEX_PRIVATE_KEY,
    ENV_VERTEX_PROJECT,
    Settings,
)
from minjob_ingest.store.json_store import JsonStore

#: `structure` 명령이 시작하자마자 읽는 이단 목록의 대역. 실제 파일은 실명 122건이 담겨
#: **커밋되지 않으므로**(`.gitignore`) 테스트가 그것에 기대면 새로 받은 리포에서 전부 깨진다.
_FAKE_HERESY = HeresyRef.of((HeresyEntry("아무개", ("○○교회",), ("합신",)),))


def build_draft(
    record: SourceData,
    extraction: Extraction,
    *,
    heresy: HeresyMatch | None = None,
    media_sent: bool = False,
    media_missed: bool = False,
) -> ReviewData:
    """그림 신호를 채워 부르는 테스트용 얇은 껍데기.

    운영 시그니처에서는 두 값이 **필수**다 — 빠뜨린 쪽이 자동 승인이라 기본값을 두지 않았다.
    여기서만 기본값을 준다: 대부분의 검사는 그림과 무관하고, 매 호출에 두 줄을 붙이면
    정작 무엇을 검사하는지가 묻힌다.
    """
    return _build_draft(
        record, extraction, heresy=heresy, media_sent=media_sent, media_missed=media_missed
    )


def _write_config(tmp_path: Path, *, enabled: bool = True) -> Path:
    row: dict[str, object] = {
        "key": "YTUS",
        "board_name": "영남신대 취업/초빙",
        "denomination_hint": "TONGHAP",
        "enabled": enabled,
        "fetch_tier": "static",
        "encoding": "utf-8",
        "list_url": "https://www.ytus.ac.kr/board/list/trXXR",
        "detail_pattern": "/board/view/trXXR/{id}",
        "fetch_note": "공지행(.notice-row) skip · pagination /page/{n}",
    }
    if not enabled:
        row["disabled_reason"] = "실측 공고 0건"
    target = tmp_path / "sources.json"
    target.write_text(json.dumps({"sources": [row]}), encoding="utf-8")
    return target


def test_lists_real_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list-sources"]) == 0
    out = capsys.readouterr().out
    # HANSEI는 2026-08-04에 게시판이 소멸해 비활성이다(config `disabled_reason` 참조).
    assert "등록 소스 31곳 (활성 30)" in out
    assert "YTUS" in out


def test_lists_single_source_with_note(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list-sources", "ytus", "--config", str(_write_config(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "note: 공지행" in out
    assert "detail: /board/view/trXXR/{id}" in out


def test_unknown_key_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list-sources", "NOPE", "--config", str(_write_config(tmp_path))]) == 1
    assert "알 수 없는 source_key" in capsys.readouterr().err


def test_config_error_is_reported_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "sources.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert main(["list-sources", "--config", str(broken)]) == 1
    assert "config 오류" in capsys.readouterr().err


def test_missing_config_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list-sources", "--config", str(tmp_path / "nope.json")]) == 1
    assert "읽을 수 없음" in capsys.readouterr().err


def test_shows_disabled_marker_and_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path, enabled=False)
    assert main(["list-sources", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "활성 0" in out
    assert "○" in out
    assert "제외 사유: 실측 공고 0건" in out


def test_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_unknown_subcommand_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["crawl-everything"])
    assert exc.value.code == 2


def test_unwired_subcommand_crashes_instead_of_succeeding() -> None:
    """서브파서만 추가하고 `_dispatch` 연결을 잊었을 때 조용히 0을 반환하면 안 된다."""
    with pytest.raises(RuntimeError, match="연결되지 않았다"):
        _dispatch(argparse.Namespace(command="not-wired"))


def test_check_gemini_reports_missing_config_without_calling_the_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """설정이 비면 실호출 전에 멈춘다.

    ⚠️ 이 테스트가 실제 `.env`를 읽어버리면 운영자 자격증명으로 **유료 API를 호출**한다.
    빈 문자열을 넣어두면 `load_dotenv(override=False)`가 덮어쓰지 못하므로
    (`os.environ`에 키가 이미 존재), 리포에 `.env`가 있든 없든 결과가 같다.
    """
    for name in (ENV_VERTEX_PROJECT, ENV_VERTEX_CLIENT_EMAIL, ENV_VERTEX_PRIVATE_KEY):
        monkeypatch.setenv(name, "")

    def fail_if_called(_settings: object) -> object:  # pragma: no cover - 불려선 안 된다
        raise AssertionError("설정 검증 전에 Gemini 클라이언트를 만들었다")

    monkeypatch.setattr(gemini, "build_client", fail_if_called)
    assert main(["check-gemini"]) == 1
    assert "Vertex 설정 오류" in capsys.readouterr().err


# ── collect: source_health 배선 ──────────────────────────────────
#
# `collect_source`는 이미 fixture로 검증됐다. 여기서 보는 것은 **배선**이다 — 결과를
# `source_health`에 남기는가, `--dry-run`이 그걸 건드리지 않는가.


class _NoClient:
    """전송 층 대역. 이 테스트는 게시판을 만지지 않는다."""

    def __init__(self, source: object) -> None:
        pass

    def __enter__(self) -> _NoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _canned_report(
    *, rows: int, saved: int, newest: date | None = date(2026, 8, 4)
) -> CollectReport:
    return CollectReport(
        source_key="YTUS",
        pages_read=1,
        rows=rows,
        fresh=saved,
        seen=rows - saved,
        stale=0,
        saved=saved,
        shifted=0,
        oldest=newest,
        newest=newest,
        samples=(),
        cutoff=date(2026, 5, 4),
    )


def _run_collect_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    # ⚠️ `Exception`이 아니라 `BaseException`이다 — `KeyboardInterrupt`가 대상이다.
    outcome: CollectReport | BaseException,
    dry_run: bool,
) -> JsonStore:
    monkeypatch.setenv("MINJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "SourceClient", _NoClient)
    monkeypatch.setattr(cli, "find_adapter", lambda _key: object())

    def fake_collect(*_args: object, **_kwargs: object) -> CollectReport:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(cli, "collect_source", fake_collect)
    main(
        [
            "collect",
            "--config",
            str(_write_config(tmp_path)),
            *(["--dry-run"] if dry_run else []),
        ]
    )
    return JsonStore(tmp_path / "data")


def test_collect_records_health_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _run_collect_with(
        monkeypatch, tmp_path, outcome=_canned_report(rows=18, saved=2), dry_run=False
    )
    health = store.get_health("YTUS")
    assert health is not None
    assert health.last_status is SourceHealthStatus.OK
    assert health.last_rows == 18
    assert health.last_cutoff == date(2026, 5, 4)  # 기간이 없으면 행 수를 해석할 수 없다
    assert health.last_run_id is not None  # crawl_run 과 이어져야 되짚을 수 있다
    capsys.readouterr()


def test_collect_records_health_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """실패를 안 남기면 연속 실패를 셀 수 없어 §7 경보가 죽는다."""
    store = _run_collect_with(monkeypatch, tmp_path, outcome=FetchError("HTTP 500"), dry_run=False)
    health = store.get_health("YTUS")
    assert health is not None
    assert health.last_status is SourceHealthStatus.FAIL
    assert health.consecutive_failures == 1
    assert health.last_error is not None
    capsys.readouterr()


def test_dry_run_records_no_health(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dry-run`은 아무것도 저장하지 않는다고 약속한다 — 상태도 저장이다."""
    store = _run_collect_with(
        monkeypatch, tmp_path, outcome=_canned_report(rows=18, saved=2), dry_run=True
    )
    assert store.get_health("YTUS") is None
    assert not (tmp_path / "data" / "source_health.json").exists()
    capsys.readouterr()


def test_collect_leaves_logging_as_it_found_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ 핸들러를 남기면 **명령이 끝난 뒤의 로그가 닫힌 스트림에 쓰여** 터진다.

    Console은 이 명령의 출력 스트림에 묶여 있고 그 스트림은 곧 닫힌다. 실제로 이걸 안 치웠을 때
    이후 테스트 20개가 `ValueError: I/O operation on closed file`로 깨졌다.
    """
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    # ⚠️ 현재 레벨을 그냥 읽어 비교하면 안 된다 — 앞선 collect 테스트가 이미 WARNING을 남긴
    # 상태면 "복원 안 함" 변이에서도 before == after 가 되어 조용히 통과한다.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)

    _run_collect_with(monkeypatch, tmp_path, outcome=_canned_report(rows=18, saved=2), dry_run=True)

    assert root.handlers == before_handlers
    for name in _NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.DEBUG  # 우리가 낮춘 것을 되돌렸다
        logging.getLogger(name).setLevel(logging.NOTSET)  # 이 테스트가 남기지 않는다
    # 명령이 끝난 뒤 로그를 찍어도 터지지 않는다.
    capsys.readouterr()
    logging.getLogger("minjob_ingest.after").info("이 줄이 예외를 내면 핸들러가 남은 것이다")


def test_repeated_empty_listings_are_reported_in_the_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """판정만 맞고 **출력하지 않으면** 운영자는 여전히 리포트 31개를 눈으로 비교해야 한다."""
    empty = _canned_report(rows=0, saved=0, newest=None)
    for _ in range(EMPTY_RUNS_ALARM):
        _run_collect_with(monkeypatch, tmp_path, outcome=empty, dry_run=False)
    printed = capsys.readouterr().out
    assert "목록 0행" in printed
    assert "YTUS" in printed
    # 경보 표시(⚠)까지 확인한다 — 참고 정보로 격하되면 31곳 요약에서 눈에 안 들어온다.
    assert any("⚠" in line and "YTUS" in line for line in printed.splitlines())


def test_logs_emitted_during_collect_reach_the_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """robots·재시도 알림은 로거로 나간다 — 콘솔에 붙이지 않으면 **전부 사라진다**.

    차단 직전 신호(`Retry-After`·`Crawl-delay`)가 조용히 없어지는 경로다.
    """
    report = _canned_report(rows=18, saved=2)

    def logging_collect(*_args: object, **_kwargs: object) -> CollectReport:
        logging.getLogger("minjob_ingest.fetch.client").info("YTUS 일시 오류 — 재시도")
        return report

    monkeypatch.setenv("MINJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "SourceClient", _NoClient)
    monkeypatch.setattr(cli, "find_adapter", lambda _key: object())
    monkeypatch.setattr(cli, "collect_source", logging_collect)
    main(["collect", "--config", str(_write_config(tmp_path)), "--dry-run"])
    assert "YTUS 일시 오류 — 재시도" in capsys.readouterr().out


def test_failed_details_are_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ 글 하나의 실패로 게시판을 포기하지 않되 **조용히 넘기지도 않는다**.

    개수와 사유를 보여줘야 운영자가 셀렉터가 조금씩 어긋나는 것을 알아챈다.
    """
    partial = replace(
        _canned_report(rows=18, saved=17),
        failed=1,
        failure_samples=("25580: ParseError: 본문 컨테이너 없음",),
    )
    _run_collect_with(monkeypatch, tmp_path, outcome=partial, dry_run=False)
    printed = capsys.readouterr().out
    assert "상세를 못 읽은 글 1건" in printed
    assert "25580" in printed


def _runs(tmp_path: Path) -> list[dict[str, object]]:
    """`crawl_run`을 파일에서 직접 읽는다 — Store에 실행 조회가 없다(있어야 할 이유도 아직 없다)."""
    path = tmp_path / "data" / "crawl_run.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [row for row in payload["records"] if isinstance(row, dict)]


def test_an_unexpected_crash_still_closes_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ 실제로 이렇게 남았다(2026-08-05): 크래시가 `crawl_run`을 `finished_at: null`로 두었다.

    열린 채 남은 run은 영구히 "진행 중"으로 보여 `status`가 거짓말을 하고, 다음 실행과 구분되지
    않는다. 예외는 **삼키지 않고**(운영자가 스택을 봐야 한다) 기록만 남기고 올려보낸다.
    """
    with pytest.raises(RuntimeError, match="boom"):
        _run_collect_with(monkeypatch, tmp_path, outcome=RuntimeError("boom"), dry_run=False)
    runs = _runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["finished_at"] is not None
    detail = runs[0]["error_detail"]
    assert isinstance(detail, dict)
    assert "RuntimeError: boom" in str(detail["_aborted"])
    capsys.readouterr()


def test_a_keyboard_interrupt_still_closes_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """운영자가 Ctrl-C로 멈추는 일은 백필에서 흔하다 — `Exception`만 잡으면 여기서 새어 나간다."""
    with pytest.raises(KeyboardInterrupt):
        _run_collect_with(monkeypatch, tmp_path, outcome=KeyboardInterrupt(), dry_run=False)
    runs = _runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["finished_at"] is not None
    capsys.readouterr()


def test_the_abort_marker_is_not_counted_as_a_failed_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """중단 사유는 `error_detail`에 들어가지만 **소스가 아니다** — 실패 소스 수에 세면 안 된다."""
    with pytest.raises(RuntimeError):
        _run_collect_with(monkeypatch, tmp_path, outcome=RuntimeError("boom"), dry_run=False)
    run = _runs(tmp_path)[0]
    assert run["sources_failed"] == 0
    assert run["sources_ok"] == 1
    capsys.readouterr()


def test_dry_run_records_no_run_even_on_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(RuntimeError):
        _run_collect_with(monkeypatch, tmp_path, outcome=RuntimeError("boom"), dry_run=True)
    assert _runs(tmp_path) == []
    capsys.readouterr()


# ── structure — 비용 안전장치 ────────────────────────────────────


def test_structure_refuses_to_run_without_a_scope(capsys: pytest.CaptureFixture[str]) -> None:
    """⚠️ 기본값으로 도는 경로가 있으면 실수 한 번이 남은 전량을 유료 호출한다."""
    with pytest.raises(SystemExit) as exit_info:
        main(["structure"])

    assert exit_info.value.code == 2
    assert "--limit" in capsys.readouterr().err


def test_structure_rejects_a_scope_given_twice(capsys: pytest.CaptureFixture[str]) -> None:
    """`--limit 20 --all`은 어느 쪽이 이겼는지 리포트만 보고 알 수 없다."""
    with pytest.raises(SystemExit) as exit_info:
        main(["structure", "--limit", "20", "--all"])

    assert exit_info.value.code == 2
    capsys.readouterr()


@pytest.mark.parametrize("value", ["0", "-1"], ids=["0건", "음수"])
def test_structure_rejects_a_useless_limit(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    """0건짜리 실행이 조용히 성공하면 운영자는 "처리할 게 없다"로 오해한다."""
    with pytest.raises(SystemExit) as exit_info:
        main(["structure", "--limit", value])

    assert exit_info.value.code == 2
    capsys.readouterr()


def test_structure_rejects_an_unknown_source_before_spending_anything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """오타를 그대로 두면 "처리할 공고가 없습니다"로 조용히 넘어가 다 끝난 줄 안다."""
    assert main(["structure", "--limit", "1", "--source", "YTUSS"]) == 1

    assert "알 수 없는 source_key" in capsys.readouterr().err


class _FakeGemini:
    """`GeminiClient` 대역. 화면·`--out`에 찍히는 모델 이름만 필요하다."""

    model = "gemini-테스트-모델"


def _stub_gemini(monkeypatch: pytest.MonkeyPatch, *, asked: list[bool] | None = None) -> None:
    """Vertex와 이단 목록을 대역으로 바꾼다 — 이 테스트들은 유료 호출도 실명 파일 읽기도 안 한다.

    `asked`를 주면 `--lite`가 설정 층까지 실제로 닿았는지 기록한다.

    ⚠️ **이단 목록을 함께 대역으로 둔다.** `structure`는 시작하자마자 `config/heresy-ref.json`을
    읽는데, 그 파일은 실명 122건이 담겨 **커밋되지 않는다**(`.gitignore`). 대역이 없으면 이
    테스트들이 운영자 컴퓨터에서만 통과하고 새로 받은 리포에서는 통째로 실패한다
    (CLAUDE.md "pytest — fixture만 사용 · 네트워크 금지"와 같은 취지).
    """

    def require_vertex(_self: Settings, *, lite: bool = False) -> object:
        if asked is not None:
            asked.append(lite)
        return object()

    monkeypatch.setattr(cli, "GeminiClient", lambda _settings: _FakeGemini())
    monkeypatch.setattr(cli, "GeminiExtractor", lambda _client: object())
    monkeypatch.setattr(cli, "load_ref", lambda _path: _FAKE_HERESY)
    monkeypatch.setattr(Settings, "require_vertex", require_vertex)


def test_structure_options_reach_the_pipeline_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ 옵션 해석이 뒤집혀도 파서 테스트는 전부 통과한다.

    `--all`↔`--limit`이 바뀌면 20건짜리 확인이 **전량 유료 호출**이 된다 — 여기서 고정한다.
    """
    captured: list[StructureOptions] = []

    def fake_pipeline(
        _store: object, _extractor: object, options: StructureOptions, **_kwargs: object
    ) -> StructureReport:
        captured.append(options)
        return StructureReport()

    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path))
    monkeypatch.setattr(cli, "structure_pending", fake_pipeline)
    _stub_gemini(monkeypatch)

    assert main(["structure", "--limit", "20", "--source", "ytus"]) == 0
    assert main(["structure", "--all", "--dry-run"]) == 0

    bounded, everything = captured
    assert (bounded.limit, bounded.source_key, bounded.dry_run) == (20, "YTUS", False)
    assert (everything.limit, everything.source_key, everything.dry_run) == (None, None, True)
    capsys.readouterr()


def test_the_lite_flag_picks_the_other_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ `--lite`가 조용히 무시되면 4배 비싼 모델이 돈다 — 화면으로는 구분되지 않는다.

    두 모델을 견주는 실행에서 이게 어긋나면 **결론이 반대**가 된다.
    """
    asked: list[bool] = []

    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path))
    monkeypatch.setattr(cli, "structure_pending", lambda *_a, **_k: StructureReport())
    _stub_gemini(monkeypatch, asked=asked)

    assert main(["structure", "--limit", "1", "--dry-run"]) == 0
    assert main(["structure", "--limit", "1", "--dry-run", "--lite"]) == 0

    assert asked == [False, True], "기본은 VERTEX_MODEL, --lite 일 때만 VERTEX_MODEL_LITE"
    capsys.readouterr()


def test_the_model_name_is_printed_every_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ 어느 모델로 돌았는지 화면에 없으면 `--out` 파일이 뒤바뀐 것을 알 수 없다."""
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path))
    monkeypatch.setattr(cli, "structure_pending", lambda *_a, **_k: StructureReport())
    _stub_gemini(monkeypatch)

    assert main(["structure", "--limit", "1", "--dry-run"]) == 0

    assert _FakeGemini.model in capsys.readouterr().out


def test_the_preview_shows_the_record_that_would_be_stored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """⚠️ 모델 답만 보여주면 실제로 무엇이 저장되는지 알 수 없다.

    `--dry-run`은 **저장만 안 하는 것**이지 덜 보여주는 것이 아니다 — `confidence`·교단 근거·
    검수 상태처럼 코드가 붙이는 값과, 아직 비어 있는 칸이 몇 개인지가 화면에 있어야 한다.
    """
    record = SourceData(
        source_key="DAESHIN",
        external_id="37",
        source_url="https://daeshin.ac.kr/board/37",
        title="성원교회에서 함께할 동역자를 모십니다.",
        posted_on=kst_now().date(),
        run_id=new_id(),
        fetched_at=kst_now(),
        raw_text="성원교회 주일학교 사역자 모집",
    )
    draft = build_draft(
        record,
        Extraction(
            is_church_recruitment=IsChurchRecruitment.YES,
            church_name="성원교회",
            description="대구 수성구 성원교회가 주일학교 사역자를 모집합니다.",
        ),
    )

    cli._print_preview(
        Console(color=False), StructureResult(record=record, verdict=Verdict.DRAFTED, draft=draft)
    )

    shown = capsys.readouterr().out
    assert "review_data (PENDING)" in shown
    assert "성원교회" in shown
    assert "low" in shown, "운영자 우선검토라는 사실이 보여야 한다"
    assert "unknown" in shown, "교단 근거가 없다는 사실이 보여야 한다"
    assert record.source_url in shown
    assert "아직 비어 있는 칸" in shown, "얇아 보이는 이유가 화면에 있어야 한다"


def test_the_preview_says_whether_the_draft_goes_out_without_a_person(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """⚠️ 등급만 찍으면 `high`가 **자동 공개**라는 사실이 화면에 없다(SPEC §5.7).

    운영자가 프롬프트를 다듬으며 보는 화면이라, 지금 이 공고가 사람을 거치는지 아닌지가
    등급 옆에 있어야 한다.
    """
    record = SourceData(
        source_key="DAESHIN",
        external_id="41",
        source_url="https://e.kr/41",
        title="성원교회 부목사 청빙",
        posted_on=kst_now().date(),
        run_id=new_id(),
        fetched_at=kst_now(),
        raw_text="성원교회 부목사 청빙. church@example.kr",
    )
    complete = Extraction(
        is_church_recruitment=IsChurchRecruitment.YES,
        church_name="성원교회",
        description="성원교회가 부목사를 청빙합니다.",
        job_kind=(JobKind.MINISTRY,),
        position=(Position.ASSOCIATE_PASTOR,),
        contact_email="church@example.kr",
    )

    cli._print_preview(
        Console(color=False),
        StructureResult(
            record=record, verdict=Verdict.DRAFTED, draft=build_draft(record, complete)
        ),
    )
    approved = capsys.readouterr().out

    cli._print_preview(
        Console(color=False),
        StructureResult(
            record=record,
            verdict=Verdict.DRAFTED,
            draft=build_draft(record, complete, media_sent=True),
        ),
    )
    reviewed = capsys.readouterr().out

    assert "high" in approved and "자동 승인" in approved
    assert "medium" in reviewed and "운영자 검수" in reviewed


def test_the_preview_says_why_no_draft_was_made(capsys: pytest.CaptureFixture[str]) -> None:
    record = SourceData(
        source_key="CALVIN",
        external_id="9",
        source_url="https://e.kr/9",
        title="포스터 공고",
        posted_on=kst_now().date(),
        run_id=new_id(),
        fetched_at=kst_now(),
        raw_text="",
        image_urls=("https://e.kr/p.png",),
    )

    cli._print_preview(
        Console(color=False), StructureResult(record=record, verdict=Verdict.DEFERRED)
    )

    shown = capsys.readouterr().out
    assert "그림 대기" in shown and "만들지 않음" in shown


def test_the_preview_file_is_diffable_between_runs(tmp_path: Path) -> None:
    """⚠️ 프롬프트를 고치고 무엇이 달라졌는지 보려면 두 실행을 **diff** 할 수 있어야 한다.

    `id`·`created_at`은 실행마다 새로 생기므로 그대로 두면 값이 하나도 안 바뀌었는데
    **전 레코드가 달라진 것처럼** 보여 도구가 무용지물이 된다.
    """
    record = SourceData(
        source_key="DAESHIN",
        external_id="37",
        source_url="https://daeshin.ac.kr/board/37",
        title="성원교회 청빙",
        posted_on=kst_now().date(),
        run_id=new_id(),
        fetched_at=kst_now(),
        raw_text="본문",
    )
    extraction = Extraction(
        is_church_recruitment=IsChurchRecruitment.YES,
        church_name="성원교회",
        description="요약",
    )

    def run_once(path: Path) -> str:
        preview = cli._PreviewFile(path, model=_FakeGemini.model)
        # 같은 입력이라도 초안은 매번 새 id·created_at 을 갖는다
        result = StructureResult(
            record=record, verdict=Verdict.DRAFTED, draft=build_draft(record, extraction)
        )
        preview.add(result, StructureReport())
        preview.write()
        return path.read_text(encoding="utf-8")

    first = run_once(tmp_path / "a.json")
    second = run_once(tmp_path / "b.json")

    assert first == second, "값이 같은데 파일이 달라지면 diff 로 비교할 수 없다"
    assert "성원교회" in first
    assert '"verdict": "DRAFTED"' in first
    assert '"id"' not in first and '"created_at"' not in first


def test_the_preview_file_records_why_a_posting_was_skipped(tmp_path: Path) -> None:
    """초안이 없는 판정도 파일에 남아야 한다 — 왜 빠졌는지가 검수의 절반이다."""
    record = SourceData(
        source_key="CALVIN",
        external_id="9",
        source_url="https://e.kr/9",
        title="포스터",
        posted_on=kst_now().date(),
        run_id=new_id(),
        fetched_at=kst_now(),
        raw_text="",
        image_urls=("https://e.kr/p.png",),
    )
    path = tmp_path / "p.json"
    preview = cli._PreviewFile(path, model=_FakeGemini.model)

    preview.add(StructureResult(record=record, verdict=Verdict.DEFERRED), StructureReport())
    preview.write()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written[0]["verdict"] == "DEFERRED"
    assert written[0]["posting"] == "CALVIN/9"
    assert "draft" not in written[0]
    # ⚠️ 두 모델을 견줄 때 파일 이름만 믿으면 뒤바뀐 것을 알 수 없다 — 파일이 스스로 밝힌다.
    assert written[0]["model"] == _FakeGemini.model


# ── 검산 결과가 운영자에게 닿나 ─────────────────────────────────
#
# ⚠️ 검산은 "조용히 지우지 않는다"가 규칙이다. 집계와 화면 출력에 테스트가 없으면 그 규칙이
# 코드에만 있고 사람에게는 안 닿는다 — 실제로 초안 경로에서 리포트가 통째로 죽어 있었다.


def _result_with(report: VerifyReport) -> StructureResult:
    record = SourceData(
        source_key="DAESHIN",
        external_id="37",
        source_url="https://e.kr/37",
        title="성원교회 부교역자 청빙",
        posted_on=kst_now().date(),
        run_id=new_id(),
        fetched_at=kst_now(),
        raw_text="성원교회에서 부교역자를 청빙합니다.",
    )
    extraction = Extraction(
        is_church_recruitment=IsChurchRecruitment.YES,
        church_name="성원교회",
        description="성원교회가 부교역자를 청빙합니다.",
    )
    return StructureResult(
        record=record,
        verdict=Verdict.DRAFTED,
        extraction=extraction,
        draft=build_draft(record, extraction),
        verified=report,
    )


def test_the_run_report_adds_up_what_verification_did() -> None:
    """⚠️ 초안 경로에서 이 집계가 통째로 0이던 적이 있다 — 리포트를 붙인 의미가 없었다."""
    tally = _Tally()

    tally.add(
        _result_with(
            VerifyReport(
                scrubbed=("raw_denomination",),
                unchecked=2,
                unchecked_fields={"required_docs": 2},
            )
        )
    )
    tally.add(_result_with(VerifyReport(scrubbed=("contact_email",), unverifiable=3)))

    report = tally.report()
    assert report.scrubbed == 2
    assert report.scrubbed_fields == {"raw_denomination": 1, "contact_email": 1}
    assert report.unverifiable == 3
    # ⚠️ 조립 칸 집계도 같이 올라와야 한다 — 이게 빠지면 프롬프트를 고쳐도 움직임이 안 보인다.
    assert report.unchecked == 2
    assert report.unchecked_fields == {"required_docs": 2}


def test_the_run_report_shows_the_verification_lines(capsys: pytest.CaptureFixture[str]) -> None:
    report = StructureReport(
        scanned=2,
        drafted=2,
        scrubbed=1,
        scrubbed_fields={"raw_denomination": 1},
        unverifiable=3,
        unchecked=5,
        unchecked_fields={"required_docs": 5},
    )

    cli._print_structure_report(Console(color=False), report, dry_run=True)

    shown = capsys.readouterr().out
    assert "검산에서 비움" in shown and "raw_denomination" in shown
    assert "본문 확인 못 함" in shown
    assert "원문에서 확인 못 함" in shown and "required_docs" in shown


def test_the_run_report_shows_how_many_went_out_without_a_person(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """⚠️ 자동 승인은 사람을 거치지 않고 공개된다 — 그 수가 화면에 없으면 규칙이 느슨해진 것을
    알아챌 방법이 없다(SPEC §5.7).

    ⚠️ **초안 수 옆의 "검수 대기"는 초안 전체가 아니다** — 자동 승인·자동 거절이 빠진 수다.
    """
    report = StructureReport(
        scanned=12,
        drafted=10,
        rejected=1,
        rejected_reasons={"HERESY": 1},
        statuses={"APPROVED": 7, "PENDING": 2, "REJECTED": 1},
    )

    cli._print_structure_report(Console(color=False), report, dry_run=False)

    shown = capsys.readouterr().out
    assert "자동 승인" in shown and "7건" in shown
    assert "검수 대기 2건" in shown, "초안 10건이 전부 큐에 뜨는 것처럼 보이면 안 된다"
    assert "자동 거절" in shown and "HERESY" in shown


def test_the_run_report_stays_quiet_when_nothing_was_auto_approved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0건까지 적으면 눈이 그 줄을 흘려보내게 된다 — 있을 때만 눈에 띄어야 한다."""
    report = StructureReport(scanned=3, drafted=2, statuses={"PENDING": 2})

    cli._print_structure_report(Console(color=False), report, dry_run=False)

    assert "자동 승인" not in capsys.readouterr().out


def test_the_preview_says_which_field_verification_emptied(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """⚠️ 이게 없으면 빈 칸을 보고 "모델이 null 을 줬다"와 "검산이 비웠다"를 구분할 수 없다."""
    cli._print_preview(
        Console(color=False), _result_with(VerifyReport(scrubbed=("contact_email",)))
    )

    assert "원문에 없어 비운 칸" in capsys.readouterr().out


def test_the_preview_file_records_what_verification_did(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    preview = cli._PreviewFile(path, model=_FakeGemini.model)

    preview.add(
        _result_with(
            VerifyReport(
                scrubbed=("contact_link",), unchecked=2, unchecked_fields={"required_docs": 2}
            )
        ),
        StructureReport(),
    )
    preview.write()

    written = json.loads(path.read_text(encoding="utf-8"))[0]
    assert written["scrubbed"] == ["contact_link"]
    assert written["unchecked"] == {"required_docs": 2}


def test_the_worker_count_reaches_the_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ 전달이 끊기면 기본값으로 조용히 돈다 — 화면에는 `--workers 12`가 찍히는데 4곳만 돈다."""
    captured: list[object] = []

    def fake_pipeline(
        _store: object, _extractor: object, _options: object, **kwargs: object
    ) -> StructureReport:
        captured.append(kwargs.get("workers"))
        return StructureReport()

    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path))
    monkeypatch.setattr(cli, "structure_pending", fake_pipeline)
    _stub_gemini(monkeypatch)

    assert main(["structure", "--limit", "1", "--dry-run"]) == 0
    assert main(["structure", "--limit", "1", "--dry-run", "--workers", "12"]) == 0

    assert captured == [DEFAULT_WORKERS, 12]
    capsys.readouterr()


@pytest.mark.parametrize("value", ["0", "-3"])
def test_structure_rejects_a_useless_worker_count(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["structure", "--limit", "1", "--workers", value])
    capsys.readouterr()


def test_one_board_is_not_advertised_as_parallel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ `--source`를 주면 게시판이 하나라 스레드도 하나다.

    숫자를 그대로 찍으면 돌지 않는 병렬을 돈다고 읽는다.
    """
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path))
    monkeypatch.setattr(cli, "structure_pending", lambda *_a, **_k: StructureReport())
    _stub_gemini(monkeypatch)

    assert main(["structure", "--limit", "1", "--dry-run", "--workers", "8"]) == 0
    spread = capsys.readouterr().out
    assert (
        main(["structure", "--limit", "1", "--dry-run", "--workers", "8", "--source", "YTUS"]) == 0
    )
    narrowed = capsys.readouterr().out

    assert "게시판 8곳씩" in spread
    assert "게시판 8곳씩" not in narrowed


def test_a_halted_run_reports_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ 멈춘 사유가 화면에 없으면 운영자는 "5건만 처리됐네"로 읽는다.

    종료 코드는 `failed`가 책임진다 — 멈춘 실행은 저장 실패를 함께 센다
    (`test_a_broken_ledger_stops_the_run_instead_of_burning_the_budget`).
    """
    halted = StructureReport(scanned=5, failed=5, halted="저장이 연속 5번 실패해 멈췄다")
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path))
    monkeypatch.setattr(cli, "structure_pending", lambda *_a, **_k: halted)
    _stub_gemini(monkeypatch)

    assert main(["structure", "--all"]) == 1

    assert "저장이 연속 5번 실패해 멈췄다" in capsys.readouterr().out


def _no_vertex(_self: Settings, **_kwargs: object) -> object:
    """Vertex 설정 검증을 건너뛴다 — 이 테스트들은 GCP 계정 없이 돌아야 한다."""
    return object()


def test_structure_stops_when_the_heresy_list_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠️ **목록 없이 돌면 이단으로 규정된 교회의 공고가 검수 큐에 그대로 올라간다**(SPEC §5.4).

    조용히 넘어가면 아무도 그 사실을 모르므로, 유료 호출을 시작하기 전에 멈춘다.
    """
    called: list[bool] = []

    def refuse(_settings: object) -> object:
        called.append(True)
        raise AssertionError("여기 오면 안 된다")

    monkeypatch.setattr(cli, "GeminiClient", refuse)
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "data"))
    monkeypatch.setenv("MINJOB_HERESY_REF", str(tmp_path / "없는목록.json"))

    assert main(["structure", "--limit", "1"]) == 1

    assert called == [], "Gemini 클라이언트를 만들기도 전에 멈춰야 한다"
    assert "이단 참고 목록 오류" in capsys.readouterr().err


def test_the_heresy_list_is_read_before_the_first_paid_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """⚠️ 순서가 계약이다 — 뒤로 미루면 3,000건을 부른 뒤에야 목록이 없다는 것을 안다."""
    order: list[str] = []

    def note_heresy(_path: Path) -> HeresyRef:
        order.append("heresy")
        return _FAKE_HERESY

    def note_gemini(_settings: object) -> _FakeGemini:
        order.append("gemini")
        return _FakeGemini()

    monkeypatch.setattr(cli, "load_ref", note_heresy)
    monkeypatch.setattr(cli, "GeminiClient", note_gemini)

    def no_pipeline(*_args: object, **_kwargs: object) -> StructureReport:
        return StructureReport()

    monkeypatch.setattr(cli, "GeminiExtractor", lambda _client: object())
    monkeypatch.setattr(Settings, "require_vertex", _no_vertex)
    monkeypatch.setattr(cli, "structure_pending", no_pipeline)
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "data"))

    assert main(["structure", "--limit", "1", "--dry-run"]) == 0

    assert order == ["heresy", "gemini"], "--dry-run 도 유료 호출을 한다 — 같은 순서여야 한다"
