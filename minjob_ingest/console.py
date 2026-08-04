"""터미널 출력 단일 창구 — 색·정렬을 한 곳에서 정한다.

의존성을 늘리지 않고 ANSI 이스케이프만 쓴다. **파이프로 넘기거나 `NO_COLOR`가 설정되면 색을
끈다** — 안 그러면 로그 파일·grep 결과에 제어문자가 섞여 읽을 수 없게 된다.
"""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from typing import Final, TextIO

_RESET: Final = "\033[0m"
#: 커서 위치부터 줄 전체 지우기. 이전 갱신이 더 길었을 때 잔상이 남지 않게 한다.
_ERASE_LINE: Final = "\033[2K"
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
#: 터미널 폭을 못 알아낼 때(파이프·CI).
_FALLBACK_COLUMNS: Final = 80


def _terminal_columns() -> int:
    return shutil.get_terminal_size(fallback=(_FALLBACK_COLUMNS, 24)).columns


def display_width(text: str) -> int:
    """터미널이 차지하는 칸 수. 한글·전각 문자는 2칸이다."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad(text: str, width: int) -> str:
    """표시 폭 기준 왼쪽 정렬. `str.ljust`는 글자 수로 세서 한글 라벨을 못 맞춘다."""
    return text + " " * max(0, width - display_width(text))


def truncate(text: str, width: int) -> str:
    """표시 폭 기준으로 자른다. 진행 줄이 터미널 폭을 넘으면 줄바꿈돼 제자리 갱신이 깨진다."""
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        step = display_width(char)
        if used + step > width - 1:
            break
        kept.append(char)
        used += step
    return "".join(kept) + "…"


def is_interactive(stream: TextIO | None = None) -> bool:
    """사람이 보고 있는 터미널인가.

    ⚠️ `color_enabled`와 **다른 판단**이다 — `NO_COLOR`는 색만 끄는 것이고 "진행 표시를 끄라"는
    뜻이 아니다. 제자리 갱신(`\\r`)은 색이 아니라 **터미널 여부**에 달려 있다.
    """
    target = stream if stream is not None else sys.stdout
    return bool(getattr(target, "isatty", lambda: False)())


def color_enabled(stream: TextIO | None = None) -> bool:
    """색을 쓸 수 있는 출력인가.

    `NO_COLOR`는 사실상의 표준이다(값이 무엇이든 설정되면 끈다).
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    return is_interactive(stream)


class ProgressLine:
    """같은 자리에서 갱신되는 한 줄 — 오래 걸리는 작업이 살아 있음을 보여준다.

    ⚠️ **터미널이 아니면 아무것도 쓰지 않는다.** `\\r`은 파이프·로그 파일에서 지워지지 않아 수백
    줄이 한 줄로 뭉친다. 최종 리포트가 같은 수치를 담으므로 잃는 정보는 없다.
    """

    def __init__(self, stream: TextIO, *, live: bool) -> None:
        self._stream = stream
        self._live = live
        self._pending = False

    def update(self, text: str) -> None:
        if not self._live:
            return
        self._stream.write(f"\r{_ERASE_LINE}{truncate(text, _terminal_columns() - 1)}")
        self._stream.flush()
        self._pending = True

    def clear(self) -> None:
        """진행 줄을 지운다. 그 자리에 최종 결과가 온다."""
        if not self._pending:
            return
        self._stream.write(f"\r{_ERASE_LINE}")
        self._stream.flush()
        self._pending = False


class Console:
    """한 실행분 출력. 색 여부를 생성 시점에 고정해 매 호출 판단하지 않는다."""

    def __init__(self, stream: TextIO | None = None, *, color: bool | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._color = color_enabled(self._stream) if color is None else color
        self._progress: ProgressLine | None = None

    def progress(self) -> ProgressLine:
        """오래 걸리는 구간용 진행 줄. 이후 모든 출력이 이 줄을 먼저 지운다."""
        self._progress = ProgressLine(self._stream, live=is_interactive(self._stream))
        return self._progress

    def paint(self, text: str, *styles: str) -> str:
        if not self._color or not styles:
            return text
        codes = ";".join(_CODES[style] for style in styles)
        return f"\033[{codes}m{text}{_RESET}"

    def line(self, text: str = "") -> None:
        # ⚠️ 모든 출력이 여기를 지난다 — 그래서 진행 줄 지우기를 한 곳에만 두면 된다.
        # (로그 경고가 진행 줄에 겹쳐 덮여 사라지는 것을 막는다.)
        if self._progress is not None:
            self._progress.clear()
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
