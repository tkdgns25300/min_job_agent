"""CLI 테스트 — 0-1a의 유일한 사용자 접점. 네트워크를 타지 않는다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from minjob_ingest.cli import _dispatch, main
from minjob_ingest.lib import gemini
from minjob_ingest.settings import (
    ENV_VERTEX_CLIENT_EMAIL,
    ENV_VERTEX_PRIVATE_KEY,
    ENV_VERTEX_PROJECT,
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
    assert "등록 소스 31곳 (활성 31)" in out
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
