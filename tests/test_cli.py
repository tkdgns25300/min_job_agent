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
from minjob_ingest.domain import SourceHealthStatus
from minjob_ingest.fetch.client import FetchError
from minjob_ingest.lib import gemini
from minjob_ingest.pipeline.collect import CollectReport
from minjob_ingest.pipeline.health import EMPTY_RUNS_ALARM
from minjob_ingest.settings import (
    ENV_VERTEX_CLIENT_EMAIL,
    ENV_VERTEX_PRIVATE_KEY,
    ENV_VERTEX_PROJECT,
)
from minjob_ingest.store.json_store import JsonStore


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
    """전송 층 대역. 이 테스트는 게시판을 만지지 않는다(가드레일 #7)."""

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
