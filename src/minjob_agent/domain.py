"""도메인 enum — 저장값 정의.

허용값 정본 = docs/CONTRACT.md §1(교단)과 min_job docs/DATA.md(그 외).
min_job과 달라지면 CONTRACT를 따르고 불일치를 보고한다(CLAUDE.md Naming).
한글 라벨은 min_job 소관 — 여기서 라벨 맵을 만들지 않는다.

값 표기: 저장값은 영어이며 대문자가 기본이다. 단 `DenominationSource`·`Confidence`·
`FetchTier`·`Encoding`은 **SPEC이 소문자로 규정**한 값이라 그대로 따른다(SPEC §5.3·§6·§3).
"""

from __future__ import annotations

from enum import StrEnum


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
    """교단 확정 근거(SPEC §5.3). ai_guess는 확정이 아니라 운영자 검수 대상."""

    STATED = "stated"
    REGISTRY = "registry"
    AI_GUESS = "ai_guess"
    UNKNOWN = "unknown"


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CrawlMode(StrEnum):
    BACKFILL = "BACKFILL"
    DAILY = "DAILY"


class SourceHealthStatus(StrEnum):
    """ZERO = 응답은 정상인데 신규 0건(소프트 실패 후보 · SPEC §7)."""

    OK = "OK"
    FAIL = "FAIL"
    ZERO = "ZERO"


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
