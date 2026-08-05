"""robots.txt 판정 — `Crawl-delay`는 **우리 UA 그룹만** 본다.

전송 층 테스트(`test_fetch_client.py`)는 "적용되는가"를 보고, 여기는 "무엇이 적용되는 값인가"를
본다. 그룹을 잘못 읽으면 남의 규칙으로 우리 속도가 정해진다(실측 SJS · 아래 첫 테스트).
"""

from __future__ import annotations

from minjob_ingest.fetch.client import USER_AGENT
from minjob_ingest.fetch.robots import crawl_delay_seconds, parse_robots

#: 31곳 전부 같은 브라우저 UA를 쓴다(CLAUDE.md Fetch · 운영자 결정 2026-08-04).
BROWSER_UA = USER_AGENT

# ── Crawl-delay 는 우리 그룹만 본다 ──────────────────────────────

#: SJS 실제 robots.txt(2026-08-05 실측 발췌). `*` 에는 지연이 없고 검색봇에만 있다.
_SJS_ROBOTS = """# SJS Homepage robots.txt
User-agent: *
Allow: /$
Disallow: /private*/

# Bing - 크롤링 속도 제한
User-agent: bingbot
Crawl-delay: 10
Disallow: /lms_bbs/dn.php

User-agent: msnbot
Crawl-delay: 10

User-agent: SemrushBot
Disallow: /
"""


def test_another_bots_delay_is_not_ours() -> None:
    """⚠️ **실측 버그**(2026-08-05): SJS가 6.7배 느렸다(1.5s → 10s).

    `Crawl-delay: 10`은 bingbot·msnbot에만 걸려 있고 `User-agent: *`엔 없다. 폴백이 파일
    전체에서 최댓값을 줍고 "늘리는 쪽이 안전하다"고 정당화했는데, SEO 봇에 30~60초를 거는
    사이트가 흔하므로 그 논리는 수집을 사실상 멈춘다.
    """
    assert crawl_delay_seconds(parse_robots(_SJS_ROBOTS), BROWSER_UA, _SJS_ROBOTS) is None


def test_the_named_bot_still_gets_its_delay() -> None:
    """그룹 판정이 맞는지 반대편으로 확인한다 — bingbot에게는 10초가 맞다."""
    assert crawl_delay_seconds(parse_robots(_SJS_ROBOTS), "bingbot/2.0", _SJS_ROBOTS) == 10.0


def test_a_decimal_delay_on_the_wildcard_is_honored() -> None:
    """표준 파서가 버리는 소수점을 폴백이 줍는다 — 그 기능은 유지된다."""
    text = "User-agent: *\nCrawl-delay: 2.5\n\nUser-agent: bingbot\nCrawl-delay: 30\n"
    assert crawl_delay_seconds(parse_robots(text), BROWSER_UA, text) == 2.5


def test_consecutive_user_agent_lines_are_one_group() -> None:
    """표준: 연속된 `User-agent:` 줄은 한 그룹이고 뒤따르는 지시자를 공유한다."""
    text = "User-agent: *\nUser-agent: bingbot\nCrawl-delay: 3\n"
    assert crawl_delay_seconds(parse_robots(text), BROWSER_UA, text) == 3.0


def test_a_comment_after_the_value_is_stripped() -> None:
    text = "User-agent: *\nCrawl-delay: 4.5  # 서버가 약함\n"
    assert crawl_delay_seconds(parse_robots(text), BROWSER_UA, text) == 4.5


def test_a_non_numeric_delay_is_ignored() -> None:
    text = "User-agent: *\nCrawl-delay: 잠깐\n"
    assert crawl_delay_seconds(parse_robots(text), BROWSER_UA, text) is None


#: 두 그룹 모두 **소수점**이라 표준 파서가 둘 다 버린다 → 폴백이 판정하는 경로다.
_DECIMALS = "User-agent: *\nCrawl-delay: 20.5\n\nUser-agent: bingbot\nCrawl-delay: 1.5\n"


def test_we_take_the_wildcard_value_not_another_bots() -> None:
    """⚠️ 남의 그룹 값은 **더 작아도** 쓰지 않는다.

    예전 폴백은 파일 전체에서 최댓값을 골랐다. 최솟값으로 바꾸는 것도 답이 아니다 — 기준은
    크기가 아니라 **그 규칙이 우리에게 걸려 있는가**다.
    """
    assert crawl_delay_seconds(parse_robots(_DECIMALS), BROWSER_UA, _DECIMALS) == 20.5


def test_a_group_that_names_us_wins_over_the_wildcard() -> None:
    """우리를 지목한 그룹이 있으면 그것이 `*`를 이긴다(표준 precedence)."""
    assert crawl_delay_seconds(parse_robots(_DECIMALS), "bingbot/2.0", _DECIMALS) == 1.5


def test_groups_are_separated_without_a_blank_line() -> None:
    """⚠️ 빈 줄 없이 붙여 쓴 robots.txt에서 **SJS 버그가 되살아난다.**

    `Crawl-delay` 다음 줄이 바로 `User-agent:`면 그건 새 그룹이다. 그걸 같은 그룹으로 읽으면
    검색봇 값이 `*` 그룹에 섞여 들어가 다시 남의 규칙을 따르게 된다. 실제 robots.txt에 빈 줄이
    없는 경우가 흔하다.
    """
    text = "User-agent: *\nCrawl-delay: 1.5\nUser-agent: bingbot\nCrawl-delay: 20.5\n"
    assert crawl_delay_seconds(parse_robots(text), BROWSER_UA, text) == 1.5


def test_an_orphan_delay_with_no_group_is_ignored() -> None:
    """그룹 밖에 떠 있는 지시자는 누구에게도 적용되지 않는다 — `*`에 붙이면 안 된다."""
    text = "User-agent: *\nCrawl-delay: 1.5\n\nCrawl-delay: 99\n"
    assert crawl_delay_seconds(parse_robots(text), BROWSER_UA, text) == 1.5
