"""fixture가 실수로 커밋되지 않는지 지킨다(가드레일 #11).

게시판 HTML 원본에는 **실제 전화번호·이메일·실명**이 있고 이 리포는 공개다.

전에는 "마스킹해서 커밋한다"였는데, 그 방식은 마스킹 패턴이 완벽해야 성립한다 — 실제로 구멍
2개(구분자 없는 번호 `0542910394`·잘린 도메인 `x@hanmail....`)로 3건이 공개 이력에 올라갔고,
**검사 코드가 같은 구멍을 갖고 있어서** "다 지웠다"고 통과했다(2026-08-04).

그래서 규칙을 바꿨다: **아예 커밋하지 않는다.** 지울 게 없으면 지우다 놓칠 일도 없다.
`git add -f`로 우회하는 실수만 막으면 되고, 그게 이 파일이다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minjob_ingest.paths import PROJECT_ROOT

_FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
#: 이 디렉터리에서 유일하게 커밋하는 파일(받는 방법 설명).
_ALLOWED = {"README.md"}


def _tracked_fixture_paths() -> list[str]:
    """git이 추적하는 fixture 경로. git 저장소가 아니면 빈 목록."""
    if not (PROJECT_ROOT / ".git").exists():
        return []
    result = subprocess.run(
        ["git", "ls-files", "tests/fixtures"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_no_fixture_is_tracked_by_git() -> None:
    """⚠️ 하나라도 추적되면 그 안의 실제 연락처가 **공개 리포 이력에 영구히** 남는다.

    git 이력은 지울 수 없다 — 파일을 나중에 삭제해도 과거 커밋에서 그대로 꺼내볼 수 있고,
    교회가 삭제를 요청해도 이행할 수 없게 된다(가드레일 #4).
    """
    leaked = [path for path in _tracked_fixture_paths() if Path(path).name not in _ALLOWED]
    assert not leaked, (
        f"fixture가 커밋 대상이 됐다: {leaked}\n"
        "→ `git rm --cached <경로>`로 추적을 해제한다. `.gitignore`에 이미 규칙이 있다."
    )


def test_the_readme_is_tracked() -> None:
    """설명 파일은 반드시 커밋한다 — 없으면 새로 클론한 사람이 fixture를 어디서 받는지 모른다."""
    if not (PROJECT_ROOT / ".git").exists():
        pytest.skip("git 저장소가 아니다")
    assert "tests/fixtures/README.md" in _tracked_fixture_paths()


def test_gitignore_covers_the_fixture_directory() -> None:
    """규칙이 사라지면 다음 `git add .`에서 전부 올라간다."""
    rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "tests/fixtures/*" in rules
    assert "!tests/fixtures/README.md" in rules  # README는 예외로 남겨둔다
