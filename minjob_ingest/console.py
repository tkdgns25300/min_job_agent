"""터미널 출력 단일 창구 — 색·정렬을 한 곳에서 정한다.

의존성을 늘리지 않고 ANSI 이스케이프만 쓴다. **파이프로 넘기거나 `NO_COLOR`가 설정되면 색을
끈다** — 안 그러면 로그 파일·grep 결과에 제어문자가 섞여 읽을 수 없게 된다.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Final, TextIO

_RESET: Final = "\033[0m"
_CODES: Final = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "cyan": "36",
}

#: 표 라벨 폭(**표시 칸 수**). 한글이 2칸이라 글자 수로 맞추면 줄이 어긋난다.
_LABEL_WIDTH: Final = 16


def display_width(text: str) -> int:
    """터미널이 차지하는 칸 수. 한글·전각 문자는 2칸이다."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad(text: str, width: int) -> str:
    """표시 폭 기준 왼쪽 정렬. `str.ljust`는 글자 수로 세서 한글 라벨을 못 맞춘다."""
    return text + " " * max(0, width - display_width(text))


def color_enabled(stream: TextIO | None = None) -> bool:
    """색을 쓸 수 있는 출력인가.

    `NO_COLOR`는 사실상의 표준이다(값이 무엇이든 설정되면 끈다).
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    target = stream if stream is not None else sys.stdout
    return bool(getattr(target, "isatty", lambda: False)())


class Console:
    """한 실행분 출력. 색 여부를 생성 시점에 고정해 매 호출 판단하지 않는다."""

    def __init__(self, stream: TextIO | None = None, *, color: bool | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._color = color_enabled(self._stream) if color is None else color

    def paint(self, text: str, *styles: str) -> str:
        if not self._color or not styles:
            return text
        codes = ";".join(_CODES[style] for style in styles)
        return f"\033[{codes}m{text}{_RESET}"

    def line(self, text: str = "") -> None:
        print(text, file=self._stream)

    def heading(self, text: str, *, note: str | None = None) -> None:
        """구획 제목. 소스마다 눈에 띄게 끊어 준다."""
        label = self.paint(text, "bold", "cyan")
        tail = f"  {self.paint(note, 'dim')}" if note else ""
        self.line()
        self.line(f"── {label}{tail}")

    def field(self, label: str, value: str, *, note: str | None = None) -> None:
        tail = f"  {self.paint(note, 'dim')}" if note else ""
        self.line(f"  {self.paint(pad(label, _LABEL_WIDTH), 'dim')}{value}{tail}")

    def warn(self, text: str, *hints: str) -> None:
        self.line(f"  {self.paint('⚠ ' + text, 'yellow')}")
        for hint in hints:
            self.line(f"    {self.paint(hint, 'dim')}")

    def error(self, text: str) -> None:
        self.line(f"  {self.paint('✗ ' + text, 'red')}")

    def ok(self, text: str) -> None:
        self.line(f"  {self.paint('✓ ' + text, 'green')}")

    def bullet(self, text: str) -> None:
        self.line(f"    {text}")
