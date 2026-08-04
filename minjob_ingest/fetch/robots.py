"""robots.txt 준수 — 가드레일 #7 "예의 있는 크롤".

게시판이 명시적으로 막은 경로는 요청하지 않는다. 또한 `Crawl-delay`를 선언한 사이트는
그 값이 우리 기본 간격보다 크면 **사이트 쪽 값을 따른다**(우리가 정한 1.5s로 밀어붙이지 않는다).

⚠️ **robots.txt를 못 가져온 경우(404·연결 실패)는 허용으로 본다** — robots 부재는 표준적으로
"제한 없음"이며, 반대로 처리하면 robots.txt가 없는 게시판을 전부 크롤 못 한다.
단 **가져왔는데 Disallow면 반드시 막는다**(조용히 무시하지 않는다).
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

#: robots.txt는 짧다 — 본문 길이 하한(스텁 응답 판정)을 적용하지 않는다.
ROBOTS_PATH: Final = "/robots.txt"


def robots_url_for(url: str) -> str:
    """해당 URL의 호스트에 대한 robots.txt 위치. 스킴·포트를 그대로 유지한다."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{ROBOTS_PATH}"


def parse_robots(text: str) -> RobotFileParser:
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return parser


def allows(parser: RobotFileParser | None, user_agent: str, url: str) -> bool:
    """robots를 못 가져왔으면(`None`) 허용. 가져왔으면 규칙대로 판정한다."""
    if parser is None:
        return True
    return parser.can_fetch(user_agent, url)


#: 표준 파서가 버리는 소수점 값을 줍기 위한 패턴.
_CRAWL_DELAY_LINE: Final = re.compile(r"^\s*crawl-delay\s*:\s*([0-9]*\.?[0-9]+)", re.I | re.M)


def crawl_delay_seconds(
    parser: RobotFileParser | None, user_agent: str, raw_text: str = ""
) -> float | None:
    """사이트가 선언한 요청 간격. 없으면 None.

    ⚠️ **표준 `RobotFileParser`는 소수점 값을 조용히 버린다**(`"2.5".isdigit()`이 False라
    `Crawl-delay: 2.5`가 없는 것으로 처리된다). 그러면 2.5초를 요청한 사이트를 우리 기본
    1.5초로 두드리게 되므로, 파서가 못 읽었을 때 **원문에서 직접 줍는다**.

    폴백은 UA 그룹을 구분하지 않고 **선언된 값 중 최댓값**을 쓴다 — 우리는 이 값으로 간격을
    늘리기만 하므로(줄이지 않는다) 보수적인 쪽으로 틀리는 편이 안전하다.
    """
    if parser is None:
        return None
    declared = parser.crawl_delay(user_agent)
    if declared is not None:
        try:
            return float(declared)
        except (TypeError, ValueError):
            pass
    found = [float(m) for m in _CRAWL_DELAY_LINE.findall(raw_text)]
    return max(found) if found else None
