"""터미널 출력 테스트 — 색이 새는 것과 정렬이 어긋나는 것."""

from __future__ import annotations

import io
import os

import pytest

from minjob_ingest.console import Console, color_enabled, display_width, pad, truncate


class _Pipe(io.StringIO):
    """파이프·파일처럼 TTY가 아닌 스트림."""

    def isatty(self) -> bool:
        return False


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


# ── 색을 언제 쓰나 ───────────────────────────────────────────────


def test_color_is_off_when_output_is_not_a_terminal() -> None:
    """파이프로 넘길 때 색을 쓰면 로그 파일·grep 결과에 제어문자가 섞인다."""
    assert not color_enabled(_Pipe())


def test_color_is_on_for_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert color_enabled(_Terminal())


def test_no_color_env_wins_over_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """`NO_COLOR`는 사실상의 표준 — 값이 무엇이든(빈 문자열 포함) 설정되면 끈다."""
    monkeypatch.setenv("NO_COLOR", "")
    assert not color_enabled(_Terminal())


def test_piped_output_contains_no_escape_codes() -> None:
    stream = _Pipe()
    console = Console(stream)
    console.heading("YTUS", note="영남신대")
    console.field("새 글", "58", note="상세를 요청할 것")
    console.warn("안전 상한에서 멈췄습니다", "게시일 파싱을 확인하세요")
    console.error("실패")
    console.ok("좋음")
    assert "\033[" not in stream.getvalue()


def test_terminal_output_is_colored() -> None:
    stream = _Terminal()
    Console(stream).error("실패")
    assert "\033[31m" in stream.getvalue()


def test_paint_without_styles_returns_the_text() -> None:
    assert Console(_Terminal()).paint("가", *()) == "가"


# ── 정렬 ─────────────────────────────────────────────────────────


def test_korean_counts_as_two_columns() -> None:
    """`len()`으로 세면 한글 라벨 줄이 어긋난다."""
    assert display_width("새 글") == 5  # 한글 2자(4) + 공백 1
    assert display_width("abc") == 3


def test_pad_aligns_by_display_width() -> None:
    labels = ("목록", "이미 본 글", "게시일")
    padded = [pad(label, 16) for label in labels]
    assert len({display_width(text) for text in padded}) == 1


def test_pad_does_not_truncate_when_too_long() -> None:
    """폭을 넘겨도 잘라내지 않는다 — 라벨이 사라지는 것보다 줄이 밀리는 게 낫다."""
    assert pad("아주아주긴라벨입니다", 4) == "아주아주긴라벨입니다"


# ── 내용 ─────────────────────────────────────────────────────────


def test_field_shows_label_value_and_note() -> None:
    stream = _Pipe()
    Console(stream).field("게시일", "2026-07-10 ~ 2026-08-04", note="컷오프 2026-05-04")
    written = stream.getvalue()
    assert "게시일" in written
    assert "2026-07-10 ~ 2026-08-04" in written
    assert "컷오프 2026-05-04" in written


def test_warn_shows_hints_on_their_own_lines() -> None:
    stream = _Pipe()
    Console(stream).warn("본문", "힌트1", "힌트2")
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 3


# ── 진행 줄 ──────────────────────────────────────────────────────


def test_progress_writes_nothing_when_not_a_terminal() -> None:
    r"""`\r`은 파이프·로그 파일에서 지워지지 않아 수백 줄이 한 줄로 뭉친다."""
    stream = _Pipe()
    console = Console(stream)
    line = console.progress()
    for page in range(1, 6):
        line.update(f"{page}p")
    line.clear()
    assert stream.getvalue() == ""


def test_progress_erases_before_each_update() -> None:
    """지우지 않으면 짧은 갱신이 앞 갱신의 꼬리를 남긴다 — "12p"가 "12p0행"으로 보인다."""
    stream = _Terminal()
    line = Console(stream).progress()
    line.update("100행")
    line.update("9행")
    assert stream.getvalue().count("\033[2K") == 2
    assert stream.getvalue().endswith("9행")


def test_progress_truncates_to_the_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """폭을 넘기면 줄바꿈돼 제자리 갱신이 깨지고 화면이 진행 줄로 뒤덮인다."""
    monkeypatch.setattr(
        "minjob_ingest.console.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((30, 24)),  # noqa: ARG005 — shutil 시그니처
    )
    stream = _Terminal()
    Console(stream).progress().update("가" * 40)
    written = stream.getvalue().split("\033[2K")[-1]
    assert display_width(written) < 30


def test_other_output_clears_a_pending_progress_line() -> None:
    """⚠️ 이게 없으면 재시도·`Crawl-delay` 경고가 진행 줄에 겹쳐 다음 갱신에 덮여 **사라진다**."""
    stream = _Terminal()
    console = Console(stream)
    console.progress().update("3p · 60행")
    console.warn("일시 오류 — 재시도")
    written = stream.getvalue()
    # 갱신의 지우기 1회 + **경고 직전** 지우기 1회. 후자가 없으면 경고가 진행 줄에 겹친다.
    # ⚠️ "경고보다 앞에 지우기가 있다"만 보면 갱신의 지우기로 통과해 버린다(헐거운 검증).
    assert written.count("\033[2K") == 2
    assert written.endswith("일시 오류 — 재시도\033[0m\n")


def test_clear_is_a_no_op_without_a_pending_line() -> None:
    """리포트 출력마다 이스케이프를 흘리면 안 된다 — 갱신한 적 없으면 아무것도 쓰지 않는다."""
    stream = _Terminal()
    console = Console(stream)
    console.progress()
    console.field("목록", "13페이지")
    assert "\033[2K" not in stream.getvalue()


def test_truncate_keeps_short_text_intact() -> None:
    assert truncate("13페이지", 40) == "13페이지"
