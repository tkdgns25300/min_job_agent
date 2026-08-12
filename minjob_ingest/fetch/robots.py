"""robots.txt 준수 — 예의 있는 크롤.

게시판이 명시적으로 막은 경로는 요청하지 않는다. 또한 `Crawl-delay`를 선언한 사이트는
그 값이 우리 기본 간격보다 크면 **사이트 쪽 값을 따른다**(우리가 정한 1.5s로 밀어붙이지 않는다).

⚠️ **robots.txt를 못 가져온 경우(404·연결 실패)는 허용으로 본다** — robots 부재는 표준적으로
"제한 없음"이며, 반대로 처리하면 robots.txt가 없는 게시판을 전부 크롤 못 한다.
단 **가져왔는데 Disallow면 반드시 막는다**(조용히 무시하지 않는다).
"""

from __future__ import annotations

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


#: 값에 붙은 주석(`Crawl-delay: 10  # 이유`)을 떼기 위한 구분자.
_COMMENT: Final = "#"
_WILDCARD: Final = "*"
_UA_KEY: Final = "user-agent"
_DELAY_KEY: Final = "crawl-delay"


def crawl_delay_seconds(
    parser: RobotFileParser | None, user_agent: str, raw_text: str = ""
) -> float | None:
    """사이트가 **우리에게** 선언한 요청 간격. 없으면 None.

    ⚠️ **표준 `RobotFileParser`는 소수점 값을 조용히 버린다**(`"2.5".isdigit()`이 False라
    `Crawl-delay: 2.5`가 없는 것으로 처리된다). 그러면 2.5초를 요청한 사이트를 우리 기본
    1.5초로 두드리게 되므로, 파서가 못 읽었을 때 **원문에서 직접 줍는다**.

    ⚠️⚠️ **UA 그룹을 지킨다.** 예전엔 파일 전체에서 최댓값을 줍고 "간격은 늘리는 쪽이
    안전하다"고 정당화했는데, 실측에서 그게 틀렸다 — `SJS`는 `Crawl-delay: 10`을 **bingbot·
    msnbot에만** 걸어 뒀고 `User-agent: *`에는 값이 없다. 남의 규칙을 가져다 써서 그 게시판만
    6.7배 느려졌다(1.5s → 10s). SEO 봇에 30~60초를 거는 사이트도 흔하므로, 그룹을 무시하면
    한 줄 때문에 수집이 사실상 멈춘다.

    ⚠️ **지연 판정은 원문을 직접 읽는 우리 파서가 정본이고**, 표준 파서는 원문이 없을 때만
    쓴다. 표준 파서는 소수점을 버리는 것 말고도 **그룹 밖에 떠 있는 지시자를 `*`에 붙인다**
    (실측: `Crawl-delay: 99`가 어느 그룹에도 없는데 99를 돌려준다). 그러면 망가진 robots.txt
    한 줄이 그 게시판 수집을 멈춘다. 표준 파서는 `Disallow` 판정(`allows`)에 계속 쓴다.
    """
    if parser is None:
        return None
    if raw_text.strip():
        return _declared_for_us(raw_text, user_agent)
    declared = parser.crawl_delay(user_agent)
    try:
        return None if declared is None else float(declared)
    except (TypeError, ValueError):
        return None


def _declared_for_us(raw_text: str, user_agent: str) -> float | None:
    """우리에게 적용되는 그룹의 `Crawl-delay`. 구체적 그룹이 있으면 그것이 `*`를 이긴다."""
    ours = user_agent.lower()
    specific: list[float] = []
    wildcard: list[float] = []
    for agents, delay in _delay_groups(raw_text):
        if any(agent != _WILDCARD and agent in ours for agent in agents):
            specific.append(delay)
        elif _WILDCARD in agents:
            wildcard.append(delay)
    if specific:
        return max(specific)
    return max(wildcard) if wildcard else None


def _delay_groups(raw_text: str) -> list[tuple[tuple[str, ...], float]]:
    """robots.txt를 `(UA 토큰들, Crawl-delay)` 그룹으로 훑는다.

    연속된 `User-agent:` 줄은 **한 그룹**이고(표준), 다른 지시자나 빈 줄이 그룹을 닫는다.
    """
    groups: list[tuple[tuple[str, ...], float]] = []
    agents: list[str] = []
    collecting = False
    for line in raw_text.splitlines():
        text = line.split(_COMMENT, 1)[0].strip()
        if not text:
            agents, collecting = [], False
            continue
        key, separator, value = text.partition(":")
        if not separator:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == _UA_KEY:
            if not collecting:
                agents = []
            agents.append(value.lower())
            collecting = True
            continue
        collecting = False
        if key == _DELAY_KEY and agents:
            try:
                groups.append((tuple(agents), float(value)))
            except ValueError:
                continue  # 숫자가 아니면 선언이 없는 것으로 본다
    return groups
