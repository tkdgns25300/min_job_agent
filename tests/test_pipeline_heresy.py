"""이단 대조 테스트 — 목록에 있는 이름만 막고, 이름만 겹친 남의 교회는 통과시킨다.

⚠️ **실제 목록(`config/heresy-ref.json`)을 쓰지 않는다.** 실명 122건이 담긴 자료라 커밋할 수
없고(`.gitignore`), 테스트가 그 파일에 기대면 다른 사람의 리포에서 통째로 실패한다.
모양만 같은 **합성 목록**으로 규칙을 검사한다. 모델도 네트워크도 없다.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from minjob_ingest.domain import Region
from minjob_ingest.pipeline.heresy import (
    NO_REGION_NOTE,
    HeresyEntry,
    HeresyMatch,
    HeresyRef,
    HeresyRefError,
    load_ref,
    screen,
)

# ── 합성 목록: 실제 목록의 세 가지 모양을 그대로 흉내낸다 ────────


def _ref() -> HeresyRef:
    return HeresyRef.of(
        (
            # 지역이 있는 항목 — 원본이 `아무개(어딘가 △△교회)`로 적어 둔 모양
            HeresyEntry("아무개", ("어딘가 △△교회", "△△교회"), ("합신",), Region.GANGWON),
            # 지역이 없는 항목 — 목록의 대부분이 이 모양이다
            HeresyEntry("나아무개", ("○○교회",), ("통합", "고신")),
            # 사람 이름뿐인 항목 — 별칭이 없다
            HeresyEntry("다아무개", (), ("개혁",)),
            # 세 글자 단체명 — `안식교`·`구원파`·`통일교`·`몰몬교`가 이 모양이다
            HeresyEntry("◎◎교", (), ("합신",)),
            # 교단·단체 이름 — 교단 칸으로 들어온다
            HeresyEntry("◇◇교단", ("◇◇선교회",), ("합동",)),
        )
    )


def _screen(
    church_name: str | None = None,
    raw: str | None = None,
    region: Region | None = None,
    senior_pastor: str | None = None,
) -> HeresyMatch | None:
    return screen(church_name, raw, region, _ref(), senior_pastor=senior_pastor)


# ── 목록에 있으면 거절한다 ───────────────────────────────────────


def test_a_listed_church_name_is_matched() -> None:
    match = _screen("○○교회")

    assert match is not None
    assert match.entry.name == "나아무개"
    assert match.field == "church_name"


def test_a_listed_body_in_the_denomination_field_is_matched() -> None:
    """⚠️ 이단 단체명이 교회명이 아니라 **교단 칸**으로 오는 공고가 있다."""
    match = _screen("평범한교회", raw="◇◇교단")

    assert match is not None
    assert match.field == "raw_denomination"


def test_an_alias_is_matched_like_the_name() -> None:
    assert _screen(raw="◇◇선교회") is not None


def test_spacing_does_not_decide_the_answer() -> None:
    """게시판이 넣는 공백은 제각각이라 그대로 견주면 늘 어긋난다."""
    assert _screen(" ○ ○ 교회 ") is not None


# ── 이름만 겹친 남의 교회는 통과시킨다 ───────────────────────────


@pytest.mark.parametrize(
    "church_name",
    ["송도○○교회", "○○제일교회", "○○교회당", "○", "그냥교회"],
    ids=["앞에붙음", "가운데다름", "뒤에붙음", "일부", "무관"],
)
def test_a_name_that_merely_contains_the_listed_one_is_not_matched(church_name: str) -> None:
    """⚠️ 부분일치로 하면 **대부분 이름만 겹친 남의 교회**가 걸린다(SPEC §5.4 실측).

    목록에 `○○교회`가 있을 때 `△△○○교회`·`○○제일교회`는 서로 다른 교회다.
    ⚠️ 실존 교회 이름은 적지 않는다 — 목록을 커밋하지 않는 이유와 같다.
    """
    assert _screen(church_name) is None


def test_nothing_matches_when_there_is_no_name() -> None:
    assert _screen(None, None) is None


def test_a_three_letter_body_name_still_participates() -> None:
    """⚠️ "짧은 이름은 빼자"가 안 되는 이유 — `안식교`·`구원파`·`통일교`·`몰몬교`가 세 글자다.

    목록 절반이 세 글자 사람 이름이지만 길이로는 단체명과 구분되지 않는다. 그래서 이름을
    골라내지 않고, **대조하는 칸을 교회명·교단 표기 둘로 제한**하는 것으로 막는다 —
    본문을 뒤지지 않으므로 동명이인이 걸릴 자리가 없다.
    """
    assert _screen("◎◎교") is not None


def test_the_body_text_is_never_screened() -> None:
    """⚠️ 사람 이름을 본문에서 찾으면 세 글자 동명이인이 무더기로 걸린다.

    `screen`은 본문을 **받지 않는다** — 받을 수 없으므로 그런 실수가 불가능하다.
    ⚠️ `senior_pastor`는 본문이 아니라 **이미 뽑힌 이름 하나**다(`normalize.senior_pastor_of`).
    그것으로 걸지 않고 걸린 뒤 가르는 데만 쓴다
    (`test_the_senior_pastor_never_triggers_a_match_by_itself`).
    """
    import inspect

    assert set(inspect.signature(screen).parameters) == {
        "church_name",
        "raw_denomination",
        "region",
        "ref",
        "senior_pastor",
    }


# ── 지역이 있으면 지역까지 봐야 거절한다 ─────────────────────────


def test_the_same_name_in_another_region_is_not_rejected() -> None:
    """지역이 다르면 **이단을 봐주는 것이 아니라 애초에 그 교회가 아니다.**

    ⚠️ 목록에 지역이 있는 항목은 5개뿐이라, 실데이터의 오거부는 이 규칙이 아니라
    **`is_conclusive`**(지역을 못 본 교회명은 검수로)가 막는다.
    """
    assert _screen("△△교회", region=Region.GYEONGNAM) is None
    assert _screen("△△교회", region=Region.GANGWON) is not None


def test_an_unknown_posting_region_is_a_match_but_not_a_rejection() -> None:
    """⚠️ 공고에 지역이 없으면 **확인한 것이 아니다**(2026-08-19). 목록에 지역이 있어도
    맞춰본 것이 아니므로 거절까지 가지 않고 사람이 본다."""
    match = _screen("△△교회", region=None)

    assert match is not None, "일치는 일치다 — 표시와 근거는 남는다"
    assert match.is_conclusive is False, "확인하지 못한 것으로 거절하면 안 된다"


# ── 거절까지 할 수 있나 (`is_conclusive`) ────────────────────────


def test_a_group_name_is_rejected_without_a_region() -> None:
    """단체·사람 이름은 **동명이 생기지 않는다** — 지역을 못 봐도 거절한다.

    지역 없는 항목의 이름 228개 중 176개가 이 꼴이다(실측 2026-08-19).
    """
    for name in ("◎◎교", "◇◇교단"):
        match = _screen(name)

        assert match is not None
        assert match.is_conclusive is True, name


def test_a_mission_group_is_not_mistaken_for_a_church() -> None:
    """⚠️ **`선교회`는 글자로는 `교회`로 끝난다.** 그걸 교회명으로 보면 단체명이 검수로 새고,
    그건 이단을 봐주는 쪽으로 틀리는 것이다(실측: 목록에 이런 이름이 6개 있다)."""
    match = _screen(raw="◇◇선교회")

    assert match is not None
    assert match.is_conclusive is True


def test_a_church_name_without_a_region_waits_for_a_person() -> None:
    """⚠️ **동명이교회를 가릴 수 없으면 거절하지 않는다**(2026-08-19 실측으로 고쳤다).

    목록 122항목 중 117개(96%)에 지역이 없어 사실상 이름만으로 거절하고 있었고, 실제로 예장합동
    소속 교회가 이름만 같아서 자동 거절됐다(옛 원장에 같은 이름 21건). 자동 거절은 검수 큐에도
    뜨지 않아 **무고한 교회가 아무도 모르게 사라진다.**
    """
    match = _screen("○○교회")

    assert match is not None, "표시와 근거는 남는다"
    assert match.is_conclusive is False


def test_a_church_name_with_the_region_confirmed_is_rejected() -> None:
    """양쪽에 지역이 있고 같으면 **그 교회다.**"""
    match = _screen("△△교회", region=Region.GANGWON)

    assert match is not None
    assert match.is_conclusive is True


# ── 같은 이름이 여럿이면 전부 본다 ───────────────────────────────


def test_every_entry_stays_reachable_when_names_collide() -> None:
    """⚠️ 이름 하나에 항목 하나만 두면 뒤 항목이 **영영 대조되지 않는다.**

    실측: 122항목·252이름 중 이름 13개가 겹치고, 그렇게 두면 2항목이 통째로 사라졌다.
    """
    ref = HeresyRef.of(
        (
            HeresyEntry("아무개", ("○○교회",), ("합신",), Region.GANGWON),
            HeresyEntry("나아무개", ("○○교회",), ("통합",), Region.SEOUL),
        )
    )

    reachable = {entry.name for group in ref.by_name.values() for entry in group}

    assert reachable == {"아무개", "나아무개"}


def test_the_entry_whose_region_matches_is_the_one_that_decides() -> None:
    """⚠️ 겹친 이름의 지역이 서로 다르면, 앞선 것 하나만 보는 순간 **삽입 순서가 판정을 바꾼다.**

    서울 등재 교회가 강원 항목에 가려 통과해 버린다.
    """
    ref = HeresyRef.of(
        (
            HeresyEntry("아무개", ("○○교회",), ("합신",), Region.GANGWON),
            HeresyEntry("나아무개", ("○○교회",), ("통합",), Region.SEOUL),
        )
    )

    seoul = screen("○○교회", None, Region.SEOUL, ref)
    gangwon = screen("○○교회", None, Region.GANGWON, ref)
    busan = screen("○○교회", None, Region.BUSAN, ref)

    assert seoul is not None and seoul.entry.name == "나아무개"
    assert gangwon is not None and gangwon.entry.name == "아무개"
    assert busan is None, "어느 쪽 지역도 아니면 그 교회가 아니다"


def test_an_entry_without_a_region_still_applies_beside_one_with_a_region() -> None:
    """지역 없는 항목은 어느 지역이든 해당한다 — 가려지면 안 된다."""
    ref = HeresyRef.of(
        (
            HeresyEntry("아무개", ("○○교회",), ("합신",), Region.GANGWON),
            HeresyEntry("나아무개", ("○○교회",), ("통합",)),
        )
    )

    assert screen("○○교회", None, Region.BUSAN, ref) is not None


# ── 왜 거절했는지 남긴다 ─────────────────────────────────────────


def test_the_evidence_names_the_entry_and_who_ruled_it() -> None:
    match = _screen("○○교회")

    assert match is not None
    assert "○○교회" in match.evidence
    assert "나아무개" in match.evidence
    assert "통합,고신" in match.evidence


def test_the_evidence_says_why_the_region_could_not_be_checked() -> None:
    """⚠️ 못 본 이유가 둘이라 **갈라 적는다** — 손쓸 방법이 다르다.

    목록에 지역이 없으면 목록을 채우면 되고, 공고에 지역이 없으면 그 공고를 봐야 한다.
    한 문구로 뭉치면 "확인해서 맞았다"와 "확인을 못 했다"가 **글자까지 똑같아진다.**
    """
    no_region_in_list = _screen("○○교회", region=Region.SEOUL)
    no_region_in_posting = _screen("△△교회", region=None)
    checked = _screen("△△교회", region=Region.GANGWON)

    assert no_region_in_list is not None
    assert "이단 목록에 지역이 없어" in no_region_in_list.evidence
    assert no_region_in_posting is not None
    assert "이 공고에 지역이 없어" in no_region_in_posting.evidence
    assert checked is not None and NO_REGION_NOTE not in checked.evidence
    assert "지역 일치: GANGWON" in checked.evidence


def test_the_evidence_names_the_list_and_the_field_in_plain_words() -> None:
    """⚠️ 근거는 min_job 검수 화면에 그대로 나간다. `church_name=…` 같은 컬럼명이나 주어 없는
    `목록`은 운영자가 읽는 말이 아니다 — 실제로 `지역 확인 불가`가 "지역을 못 긁었다"로 읽혔다."""
    match = _screen("○○교회", region=Region.SEOUL)

    assert match is not None
    assert "교회명 '○○교회'가 이단 목록의 「나아무개」와 일치" in match.evidence
    assert "church_name" not in match.evidence


# ── 담임목사 — 대조 대상이 아니라 판별 근거 ────────────────────────


def test_a_senior_pastor_who_is_the_listed_person_settles_it() -> None:
    """③ 교회명이 걸렸고 지역은 못 봤지만 **담임이 목록의 그 사람**이면 그 교회다 — 확정 거절."""
    match = _screen("○○교회", senior_pastor="나아무개")

    assert match is not None
    assert match.names_the_senior_pastor is True
    assert match.is_conclusive is True
    assert "이 공고 담임목사: 나아무개 (이단 목록 항목과 같은 이름)" in match.evidence


def test_the_senior_pastor_is_compared_after_normalizing_both_sides() -> None:
    """`나아무개 목사`처럼 직함이 붙어도, 띄어쓰기가 달라도 같은 사람이다."""
    match = _screen("○○교회", senior_pastor="나 아무개")

    assert match is not None and match.names_the_senior_pastor is True


def test_a_senior_pastor_named_in_an_alias_also_settles_it() -> None:
    """단체 항목은 별칭에 **대표자 이름**을 갖는다 — 담임이 그 사람이면 그 단체의 교회다."""
    ref = HeresyRef.of((HeresyEntry("◇◇선교회", ("라아무개", "□□교회"), ("합동",)),))

    match = screen("□□교회", None, None, ref, senior_pastor="라아무개")

    assert match is not None and match.is_conclusive is True


def test_a_different_senior_pastor_does_not_clear_the_church() -> None:
    """⚠️ **③의 반대는 성립하지 않는다.** 담임이 바뀌어도 그 교회는 그 교회다 — 여전히 사람이
    보되, 근거에 다른 이름이라고 적어 3초에 판단하게 한다."""
    match = _screen("○○교회", senior_pastor="마아무개")

    assert match is not None
    assert match.names_the_senior_pastor is False
    assert match.is_conclusive is False, "다르다고 통과시키지 않는다"
    assert "이 공고 담임목사: 마아무개 (이단 목록 항목과 다른 이름)" in match.evidence


def test_an_unknown_senior_pastor_is_said_so() -> None:
    match = _screen("○○교회")

    assert match is not None
    assert match.is_conclusive is False
    assert "이 공고 담임목사: 미상" in match.evidence


def test_the_senior_pastor_never_triggers_a_match_by_itself() -> None:
    """⚠️ 담임 이름으로 **걸지는 않는다** — 본문의 사람 이름으로 걸면 동명이인이 무더기로 걸린다.
    교회명·교단이 목록에 없으면 담임이 목록 사람이어도 `None`이다."""
    assert _screen("평범한교회", senior_pastor="나아무개") is None


# ── 같은 이름을 다르게 적은 것 ────────────────────────────────────


@pytest.mark.parametrize(
    "church_name",
    ["○○교회(어딘가)", "○○·교회", "○○-교회", "[어딘가] ○○교회", "○○교회."],
    ids=["괄호 꼬리", "중점", "붙임표", "괄호 머리", "마침표"],
)
def test_the_same_name_with_brackets_or_marks_is_still_matched(church_name: str) -> None:
    """괄호 안 지역·기호는 표기 차이다 — `dedup`의 자물쇠 키와 같은 정규식으로 뗀다(2026-08-28)."""
    match = _screen(church_name)

    assert match is not None and match.entry.name == "나아무개"


# ── 목록 파일은 경계에서 검증한다 ────────────────────────────────


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "ref.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_valid_document_loads(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "version": 2,
            "policy": "STRICT_ANY",
            "entries": [
                {"name": "아무개", "aliases": ["○○교회"], "ruled_by": ["통합"], "region": "SEOUL"}
            ],
        },
    )

    ref = load_ref(path)

    assert ref.entries[0].region is Region.SEOUL
    assert screen("○○교회", None, Region.SEOUL, ref) is not None


#: 깨진 문서 검사에 붙이는 최소 머리. ⚠️ **버전을 빼면 안 된다** — 버전 검사가 먼저 걸려
#: 정작 보려던 검증이 실행되지 않고, 그 검증을 통째로 지워도 테스트가 통과한다(실측).
def _document(entries: object) -> dict[str, object]:
    return {"version": 2, "entries": entries}


@pytest.mark.parametrize(
    "document",
    [
        _document([]),
        _document({}),
        _document([{"name": ""}]),
        _document([{"name": "   "}]),
        _document([{"name": "아무개", "region": "강원"}]),
        _document([{"name": "아무개", "region": 17}]),
        _document([{"name": "아무개", "aliases": "○○교회"}]),
        _document([{"name": "아무개", "aliases": [""]}]),
        _document([{"name": "아무개", "ruled_by": [""]}]),
        _document([{"name": "아무개", "지역": "SEOUL"}]),
        {"version": 2, "entries": [{"name": "아무개"}], "목록": []},
        {"entries": [{"name": "아무개"}]},
        {"version": 1, "entries": [{"name": "아무개"}]},
        {"version": "2", "entries": [{"name": "아무개"}]},
        {"version": True, "entries": [{"name": "아무개"}]},
    ],
    ids=[
        "비어있음",
        "배열아님",
        "빈이름",
        "공백이름",
        "지역이enum밖",
        "지역이문자열아님",
        "별칭이배열아님",
        "빈문자열별칭",
        "빈문자열규정",
        "알수없는필드",
        "알수없는최상위필드",
        "버전없음",
        "옛버전",
        "버전이문자열",
        "버전이불리언",
    ],
)
def test_a_broken_document_is_refused(tmp_path: Path, document: object) -> None:
    """⚠️ 목록이 깨진 채로 통과하면 **아무도 안 걸리는데 아무도 모른다**(CLAUDE.md 경계 검증).

    ⚠️ 옛 판(version 1)에는 **지역 칸이 없다** — 조용히 읽으면 전국의 같은 이름이 걸리는데
    아무도 그 사실을 모른다. 그래서 버전도 여기서 막는다.
    """
    with pytest.raises(HeresyRefError):
        load_ref(_write(tmp_path, document))


def test_the_same_name_written_differently_is_the_same_name(tmp_path: Path) -> None:
    """⚠️ 눈에 같아 보이는 글자가 코드에는 다르다 — 분해형(NFD)·전각은 그냥 통과해 버린다.

    `verify`와 같은 기준(NFKC)으로 맞춘다. 스크리닝이 읽는 칸에서는 아직 관측되지 않았지만,
    첨부 파일명에 NFD를 쓰는 게시판이 있어 본문에도 언제든 올 수 있다.
    """
    ref = load_ref(
        _write(
            tmp_path,
            {
                "version": 2,
                "entries": [
                    {"name": "아무개", "aliases": ["○○교회", "kaicam"], "ruled_by": ["합신"]}
                ],
            },
        )
    )

    # 전각 영문은 소스에 그대로 적으면 읽는 사람이 반각과 구분하지 못한다 — 코드포인트로 만든다.
    fullwidth = "".join(chr(ord(letter) + 0xFEE0) for letter in "KAICAM")

    assert screen(unicodedata.normalize("NFD", "○○교회"), None, None, ref) is not None
    assert screen(fullwidth, None, None, ref) is not None, "전각 영문도 같은 이름이다"


def test_a_missing_file_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    """⚠️ 없으면 조용히 넘어가지 않는다 — 이단 교회 공고가 검수 큐에 그대로 올라간다."""
    with pytest.raises(HeresyRefError):
        load_ref(tmp_path / "없는파일.json")


def test_broken_json_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "ref.json"
    path.write_text("{이건 JSON이 아니다", encoding="utf-8")

    with pytest.raises(HeresyRefError):
        load_ref(path)
