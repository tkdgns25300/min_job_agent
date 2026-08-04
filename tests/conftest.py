"""테스트 전역 안전장치.

가장 큰 사고는 **테스트가 운영자의 실제 `.env`를 읽는 것**이다. 그 파일에는 Vertex 서비스계정
비밀키가 있고, 그 값이 `os.environ`에 들어오면 `check-gemini` 경로를 지나는 테스트가
**유료 API를 실제로 호출**한다(가드레일 #10 위반). 규율로 막지 않고 여기서 차단한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import load_dotenv

from minjob_ingest.paths import DEFAULT_DOTENV_PATH
from minjob_ingest.sources.adapters.registry import implemented_keys


@pytest.fixture(autouse=True)
def _block_the_operator_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """리포 루트 `.env`만 읽지 않게 한다. 테스트가 만든 `.env`는 그대로 동작한다."""

    def guarded(*, dotenv_path: Path | str | None = None, override: bool = True) -> bool:
        if dotenv_path is not None and Path(dotenv_path) == DEFAULT_DOTENV_PATH:
            return False
        return load_dotenv(dotenv_path=dotenv_path, override=override)

    # 문자열 타깃 — `settings`는 `load_dotenv`를 재export하지 않는다(strict no_implicit_reexport).
    monkeypatch.setattr("minjob_ingest.settings.load_dotenv", guarded)


#: 어댑터 fixture가 놓이는 곳. 커밋되지 않는다(가드레일 #11 · `tests/fixtures/README.md`).
_FIXTURE_ROOT = Path(__file__).parent / "fixtures"


def _adapter_fixture_coverage() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(목록 fixture가 있는 키, 없는 키).

    ⚠️ **한 곳에서만 계산한다.** 요약 출력과 "하나도 없으면 실패" 검사가 서로 다른 계산을 쓰면
    한쪽이 조용히 어긋난다 — 마스킹과 검증 패턴이 갈라져 개인정보 3건을 놓친 것과 같은 실수다.
    """
    keys = implemented_keys()
    have = tuple(key for key in keys if (_FIXTURE_ROOT / key / "list.html").exists())
    return have, tuple(key for key in keys if key not in have)


@pytest.fixture
def adapter_fixture_coverage() -> tuple[tuple[str, ...], tuple[str, ...]]:
    return _adapter_fixture_coverage()


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """어댑터 fixture 커버리지를 요약에 찍는다.

    ⚠️ fixture는 커밋되지 않으므로(가드레일 #11) 없으면 적합성 검사가 **조용히 skip**된다.
    숫자를 항상 눈에 보이게 두면 "초록불인데 검증 0건"을 알아챌 수 있다.
    """
    have, missing = _adapter_fixture_coverage()
    total = len(have) + len(missing)
    line = f"어댑터 fixture 커버리지: {len(have)}/{total} 검증"
    if missing:
        line += f"  · 없음: {', '.join(missing)}  (minjob-ingest snapshot --source KEY)"
    terminalreporter.write_line("")
    terminalreporter.write_line(line, yellow=bool(missing), green=not missing)
