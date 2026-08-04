"""터미널 출력 테스트 — 색이 새는 것과 정렬이 어긋나는 것."""

from __future__ import annotations

import io

import pytest

from minjob_ingest.console import Console, color_enabled, display_width, pad


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
    console.warn("페이지 상한에서 멈췄습니다", "--pages 를 늘리세요")
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
