"""도메인 enum — 저장값 정의.

허용값 정본 = docs/CONTRACT.md §1(교단)과 min_job docs/DATA.md(그 외).
min_job과 달라지면 CONTRACT를 따르고 불일치를 보고한다(CLAUDE.md Naming).
한글 라벨은 min_job 소관 — 여기서 라벨 맵을 만들지 않는다.

값 표기: 저장값은 영어이며 대문자가 기본이다. 단 `DenominationSource`·`Confidence`·
`FetchTier`·`Encoding`은 **SPEC이 소문자로 규정**한 값이라 그대로 따른다(SPEC §5.3·§6·§3).
"""

from __future__ import annotations

import re
from enum import StrEnum

#: `source_key`는 저장값·config 키·로그 키로 모두 같은 문자열이 쓰인다.
#: 영문 대문자만 허용 — `str.isalnum()`은 유니코드 인식이라 한글이 통과하고,
#: `"영남".upper() == "영남"`이라 대문자 검사만으로도 막히지 않는다.
SOURCE_KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")


def normalize_source_key(value: str) -> str:
    """공백을 제거하고 형식을 검증한 `source_key`를 반환한다.

    저장 전에 정규화해야 `UNIQUE(source_key, external_id)`가 공백 변형으로 쪼개지지 않는다
    (같은 공고가 두 원장 행이 되면 재수집·재구조화 비용이 발생).
    """
    key = value.strip()
    if SOURCE_KEY_PATTERN.fullmatch(key) is None:
        raise ValueError(f"source_key는 영문 대문자·숫자·밑줄만 허용 (받은 값 {value!r})")
    return key


class Denomination(StrEnum):
    """교단. 공개 값은 9대형 + ETC = 10키(CONTRACT §1)."""

    HAPDONG = "HAPDONG"
    TONGHAP = "TONGHAP"
    BAEKSEOK = "BAEKSEOK"
    GAMLI = "GAMLI"
    SUNBOK = "SUNBOK"
    BAPTIST = "BAPTIST"
    SEONGGYUL = "SEONGGYUL"
    GOSIN = "GOSIN"
    HAPSIN = "HAPSIN"
    ETC = "ETC"

    # review_data 전용 임시값 — 근거가 없을 때만(SPEC §5.3). 승격 전 운영자가 위 10키로 해소하며
    # 공개(churches.denomination)로는 나가지 않는다. 표시 라벨 "미상"은 min_job 소관.
    UNKNOWN = "UNKNOWN"


#: 공개로 승격 가능한 교단 값(= CONTRACT §1의 10키). UNKNOWN은 제외된다.
#: 프롬프트·메시지에 나열될 수 있어 **선언 순서를 유지**한다(집합은 순회 순서가 실행마다 달라짐).
PUBLISHABLE_DENOMINATIONS: tuple[Denomination, ...] = tuple(
    d for d in Denomination if d is not Denomination.UNKNOWN
)


class Region(StrEnum):
    """광역 지역(min_job DATA.md §2)."""

    SEOUL = "SEOUL"
    GYEONGGI = "GYEONGGI"
    INCHEON = "INCHEON"
    GANGWON = "GANGWON"
    CHUNGBUK = "CHUNGBUK"
    CHUNGNAM = "CHUNGNAM"
    DAEJEON = "DAEJEON"
    SEJONG = "SEJONG"
    GYEONGBUK = "GYEONGBUK"
    GYEONGNAM = "GYEONGNAM"
    DAEGU = "DAEGU"
    ULSAN = "ULSAN"
    BUSAN = "BUSAN"
    JEONBUK = "JEONBUK"
    JEONNAM = "JEONNAM"
    GWANGJU = "GWANGJU"
    JEJU = "JEJU"
    OVERSEAS = "OVERSEAS"


class Position(StrEnum):
    """사역 직분(MINISTRY 전용)."""

    SENIOR_PASTOR = "SENIOR_PASTOR"
    ASSOCIATE_PASTOR = "ASSOCIATE_PASTOR"
    EVANGELIST = "EVANGELIST"
    LICENSED_MINISTER = "LICENSED_MINISTER"
    ETC = "ETC"


class Department(StrEnum):
    """담당 부서."""

    INFANT = "INFANT"
    CHILDREN = "CHILDREN"
    YOUTH = "YOUTH"
    YOUNG_ADULT = "YOUNG_ADULT"
    DISTRICT = "DISTRICT"
    WORSHIP = "WORSHIP"
    ADMIN = "ADMIN"
    ETC = "ETC"


class EmploymentType(StrEnum):
    FULL_TIME = "FULL_TIME"
    SEMI_FULL_TIME = "SEMI_FULL_TIME"
    PART_TIME = "PART_TIME"


class Qualification(StrEnum):
    ANY = "ANY"
    ENTRY = "ENTRY"
    EXPERIENCED = "EXPERIENCED"
    ORDAINED = "ORDAINED"
    SEMINARIAN = "SEMINARIAN"


class StipendPeriod(StrEnum):
    MONTH = "MONTH"
    YEAR = "YEAR"


class JobKind(StrEnum):
    """게이트2 — 개교회 채용의 최상위 구분(SPEC §1)."""

    MINISTRY = "MINISTRY"
    GENERAL = "GENERAL"


class IsChurchRecruitment(StrEnum):
    """게이트1 — 개교회 채용인가(SPEC §5.1).

    NO는 review_data를 만들지 않고 source_data에 structured_at만 기록한다.
    UNCERTAIN은 confidence=LOW로 운영자에게 보낸다.
    """

    YES = "YES"
    NO = "NO"
    UNCERTAIN = "UNCERTAIN"


class DenominationSource(StrEnum):
    """교단 근거(SPEC §5.3). `ai_guess`는 값이 있어도 **확정이 아니라** 운영자 검수 대상."""

    STATED = "stated"
    REGISTRY = "registry"
    AI_GUESS = "ai_guess"
    UNKNOWN = "unknown"
    #: 운영자가 검수에서 직접 확정한 값. SPEC §5.3의 "승격 전 10키로 해소"가 이 근거로 남는다
    #: (이 값이 없으면 해소된 행을 되읽을 때 근거=unknown과 모순이라 크래시한다).
    OPERATOR = "operator"


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RejectReason(StrEnum):
    """`REJECTED`가 **왜** 거절됐나. 이유를 구분하지 않으면 자동 거부를 되짚을 수 없다.

    ⚠️ 특히 `HERESY`는 **검수 큐에 뜨지 않는 자동 거부**라(SPEC §5.4), 이유를 구분해 두지 않으면
    잘못 걸러도 영원히 드러나지 않는다. 목록 대조는 **동명이교회**를 만들 수 있어
    (부분일치로 보면 대부분이 이름만 겹친 다른 교회였다) 되짚을 수 있어야 한다.

    게이트1 `NO`(개교회 아님·비채용)는 여기 없다 — 그건 `review_data`를 아예 만들지 않는다.
    """

    #: `dedup_key`가 같은 대표 행이 따로 있다(SPEC §4.1). 자동.
    DUPLICATE = "DUPLICATE"
    #: 공고가 스스로 끝났다고 말한다(`청빙완료`·`마감`). 이미 채워진 자리를 공개하지 않는다.
    #: ⚠️ **게시판 상태 필드와 제목에 명시된 것만**이다 — 본문의 `채용 완료 후 서류 폐기`는
    #: 안내 문구이지 마감이 아니다(실측 370건 중 대부분이 그것 · 2026-08-11).
    CLOSED = "CLOSED"
    #: `config/heresy-ref.json` 정확 일치(SPEC §5.4). 자동 · 근거는 `heresy_evidence`.
    HERESY = "HERESY"
    #: 운영자가 검수에서 거절.
    OPERATOR = "OPERATOR"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CrawlMode(StrEnum):
    BACKFILL = "BACKFILL"
    DAILY = "DAILY"


class SourceHealthStatus(StrEnum):
    """게시판 한 곳의 마지막 실행 결과(SPEC §7).

    ⚠️ **`EMPTY`는 "목록 행이 0"이다 — "신규가 0"이 아니다.**
    데일리 실행에서 신규 0건은 **정상**이다(원장이 이미 본 글을 걸러내므로, 조용한 게시판은 며칠씩
    신규가 없다). 그걸 소프트 실패로 세면 31곳 중 조용한 곳들이 매일 경보를 울려 **경보가 잡음이
    되고, 정작 깨진 게시판이 그 속에 묻힌다**. 목록 자체를 못 읽는 것(셀렉터 깨짐·로그인벽 전환)이
    진짜 신호다.
    """

    OK = "OK"
    FAIL = "FAIL"
    EMPTY = "EMPTY"


class FetchTier(StrEnum):
    """전송 방식(SPEC §3). 2026-07-29 실측 기준 31곳에 HEADLESS는 없다."""

    STATIC = "static"
    JSON = "json"
    HEADLESS = "headless"


class Encoding(StrEnum):
    """게시판 선언 인코딩. 서버 헤더가 틀린 보드가 있어 config 값이 우선한다(SPEC §3)."""

    UTF8 = "utf-8"
    EUC_KR = "euc-kr"

    @property
    def python_codec(self) -> str:
        """실제 디코드에 쓸 코덱.

        EUC-KR 선언 소스는 cp949로 디코드한다 — 순정 euc_kr 코덱은 확장 한글에서
        예외를 던져 한 글자 때문에 페이지 전체를 잃는다(CLAUDE.md fetch 규칙).
        멤버가 늘면 조용히 utf-8로 떨어지지 않도록 명시 매핑으로 둔다.
        """
        match self:
            case Encoding.UTF8:
                return "utf-8"
            case Encoding.EUC_KR:
                return "cp949"
