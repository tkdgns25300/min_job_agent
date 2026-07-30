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


@pytest.fixture(autouse=True)
def _block_the_operator_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """리포 루트 `.env`만 읽지 않게 한다. 테스트가 만든 `.env`는 그대로 동작한다."""

    def guarded(*, dotenv_path: Path | str | None = None, override: bool = True) -> bool:
        if dotenv_path is not None and Path(dotenv_path) == DEFAULT_DOTENV_PATH:
            return False
        return load_dotenv(dotenv_path=dotenv_path, override=override)

    # 문자열 타깃 — `settings`는 `load_dotenv`를 재export하지 않는다(strict no_implicit_reexport).
    monkeypatch.setattr("minjob_ingest.settings.load_dotenv", guarded)
