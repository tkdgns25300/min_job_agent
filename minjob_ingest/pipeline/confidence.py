"""초안을 **사람이 봐야 하나**로 나눈다(SPEC §5.7). 등급이 곧 자동 승인 여부다.

`denomination.py`·`heresy.py`와 같은 자리다 — **모델을 부르지 않는다.** 조립이 끝난
`ReviewData`만 보고 판정하므로 유료 호출 없이 검증된다.

⚠️ **`high`는 사람을 거치지 않고 공개된다**(`review_status=APPROVED`). 그래서 등급은
"얼마나 완성됐나"가 아니라 **"원문을 열어볼 일이 있나"** 로 가른다:

    low      손봐야 한다 — 그대로는 공개가 안 되거나 판단이 필요하다
    medium   보기만 하면 된다 — 무엇을 볼지 이미 안다
    high     확인할 것이 없다 → 자동 승인

⚠️ **`high`가 "값이 맞다"는 뜻은 아니다.** `verify`는 *그 글자가 원문에 있나*를 보지
*올바른 곳에서 가져왔나*를 보지 않는다 — 원문에 전화번호가 둘이면 엉뚱한 쪽을 골라도
통과한다. 코드로 막을 수 없고, 운영자가 알고 내린 결정이다(SPEC §5.7).

⚠️ **조립이 끝난 레코드를 본다**(`Extraction`이 아니라). `build_draft`가 모델 답을 손으로
옮기므로 둘이 갈릴 수 있는데, 실제로 저장되고 공개되는 것은 레코드 쪽이다.

⚠️ **게시판 제목과 교회명을 대조하는 규칙은 넣었다가 뺐다**(2026-08-17). 제목은 모델이 만들 수
없는 유일한 두 번째 출처라 기대했지만, 실측 138건에서 **적발 3건이 전부 오탐**이었다:
`[대구] 영광교회` vs `대구영광교회`(대괄호가 부분일치를 깬다) · `[대전]대전한밭제일장로교회` vs
`한밭제일교회`(같은 교회의 다른 표기 · 본문 `교회명:`이 모델 편) · `교회 후임자 구합니다`
(제목의 `교회`가 일반명사). **참 적발 0건 · 헛검수 3건** — 근거 없는 규칙은 두지 않는다.
"""

from __future__ import annotations

from typing import Final

from minjob_ingest.domain import Confidence, IsChurchRecruitment, RejectReason, ReviewStatus
from minjob_ingest.models import ReviewData

#: 승격에 필요한 칸(SPEC §6 · min_job DATA.md §3 = 필수 5 + CHECK 2). 하나라도 비면 min_job이
#: 공개를 거부하므로 사람이 채워야 한다. ⚠️ 다섯째 필수인 `posted_at`과 `source_url`은 세지
#: 않는다 — `ReviewData`가 둘 다 값 없이는 만들어지지 않아(`models.py`) 검사가 늘 참이 된다.
PROMOTION_FIELDS: Final = (
    "church_name",
    "title",
    "job_kind",
    "position_or_role",
    "description",
    "contact",
)

#: 지원 연락처 — **넷 중 하나만** 있으면 된다(min_job `APPLY_METHODS` 4키).
_CONTACT_FIELDS: Final = ("contact_email", "contact_tel", "contact_link", "contact_post")

#: ⚠️ **원문과 대조되지 않는 유일한 연락처.** 나머지 셋은 원문에 없으면 `verify`가 비우지만,
#: 우편 주소는 프롬프트가 **조립을 시킨** 칸이라 세기만 한다(SPEC §5.5b). 이것뿐이면
#: **지원 경로 전체가 확인된 적 없는 값**이 된다 — 잘못된 주소로 서류를 보내게 된다.
_UNVERIFIED_CONTACT: Final = "contact_post"


def missing_for_promotion(draft: ReviewData) -> tuple[str, ...]:
    """승격 6칸 중 **빈 칸의 이름**. 비어 있지 않으면 그 초안은 공개될 수 있다.

    개수만 돌려주면 프롬프트를 어디부터 고칠지 알 수 없어 이름으로 돌려준다.
    """
    filled = {
        "church_name": draft.church_name,
        "title": draft.title,
        "job_kind": draft.job_kind,
        "position_or_role": draft.position or draft.role,
        "description": draft.description,
        "contact": any(getattr(draft, name) for name in _CONTACT_FIELDS),
    }
    return tuple(name for name in PROMOTION_FIELDS if not filled[name])


def grade(draft: ReviewData, *, media_sent: bool, media_missed: bool) -> Confidence:
    """검수 큐를 가르는 등급. `high`면 `build_draft`가 `APPROVED`로 만든다.

    실측(1주치 전량 473건 · 2026-08-23): high 368 · medium 80 · low 17 — 사람이 볼 것은
    69건(15% · 2개월 환산 약 540건)이고 그중 61건(88%)이 그림 때문이다.
    """
    if (
        missing_for_promotion(draft)
        or draft.is_church_recruitment is not IsChurchRecruitment.YES
        or media_missed
    ):
        # ⚠️ 게이트1 `UNCERTAIN`은 레코드 불변식도 `low`를 요구한다(SPEC §5.1) — 여기서
        #    다른 값을 내면 레코드가 아예 만들어지지 않아 규칙 오류가 즉시 드러난다.
        return Confidence.LOW
    if media_sent or _only_contact_is_unverified(draft) or draft.heresy_flag:
        # ⚠️ **이단 목록에 걸린 공고도 여기 온다**(SPEC §5.4 · 2026-08-19). 지역을 확인해
        #    거절까지 한 건은 어차피 `REJECTED`가 되지만, 지역을 못 본 교회명은 **동명이교회일
        #    수 있어** 사람이 정한다 — 그 행이 `high`로 자동 공개되면 안 된다.
        # ⚠️ 나머지 둘은 **원문 대조를 거치지 않은 값**이라는 한 가지 이유다: 그림을 보낸 공고는
        #    `verify`가 어느 칸도 비우지 않고 세기만 하고(SPEC §5.5b), 우편 주소는 조립 칸이라
        #    그것뿐이면 지원 경로가 통째로 미확인이다. 자동 승인하면 "확인했다"가 거짓이 된다.
        return Confidence.MEDIUM
    return Confidence.HIGH


def _only_contact_is_unverified(draft: ReviewData) -> bool:
    """지원 경로가 **우편 주소 하나뿐**인가 — 그 값만 원문 대조를 거치지 않는다.

    실측 132건 중 0건이라 검수량은 늘지 않는다(`contact_post`가 있는 14건은 전부 다른
    연락처를 함께 갖고 있었다). 값이 싸고 막는 것이 크다: 지원자가 엉뚱한 곳으로 서류를 보낸다.
    """
    others = (name for name in _CONTACT_FIELDS if name != _UNVERIFIED_CONTACT)
    return bool(getattr(draft, _UNVERIFIED_CONTACT)) and not any(
        getattr(draft, name) for name in others
    )


def review_status_for(confidence: Confidence, reject_reason: RejectReason | None) -> ReviewStatus:
    """등급 → 검수 상태. **거절이 먼저다** — 이단·마감은 등급과 무관하게 공개되지 않는다
    (SPEC §5.4·§5.4b).

    ⚠️ 두 곳이 쓴다: 초안을 만들 때(`structure.build_draft`)와 **중복 판정을 되돌릴 때**
    (`dedup`이 잘못 거절한 행을 되살릴 때 · SPEC §4.1). 규칙이 갈리면 되살아난 행의 상태가
    처음 저장될 때와 달라진다.
    """
    if reject_reason is not None:
        return ReviewStatus.REJECTED
    if confidence is Confidence.HIGH:
        return ReviewStatus.APPROVED
    return ReviewStatus.PENDING
