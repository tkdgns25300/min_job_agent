"""교단 확정 테스트 — 명시된 것만 확정하고, 갈리면 확정하지 않는다.

실제 게시판 표기(공고 730건에서 78가지)를 쓴다. 표기를 **짜맞춘** 경우는 그렇다고 적는다 —
`실측`이라는 말은 이 리포에서 "데이터에서 봤다"는 뜻이다. 모델도 네트워크도 없다.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Final

import pytest

from minjob_ingest.clock import KST
from minjob_ingest.domain import PUBLISHABLE_DENOMINATIONS, Denomination, DenominationSource
from minjob_ingest.models import SourceData, new_id
from minjob_ingest.pipeline import denomination as module
from minjob_ingest.pipeline.denomination import _ALIASES, _LATIN_ALIASES, confirm

_NOW: Final = datetime(2026, 8, 15, 9, 0, tzinfo=KST)


def _record(raw_text: str = "", **raw_meta: str) -> SourceData:
    """원문 한 건. 교단 표기가 **어디에 적혔나**를 보는 것이 이 모듈의 절반이다."""
    return SourceData(
        source_key="CSU",
        external_id="1",
        source_url="https://example.kr/1",
        title="○○교회에서 사역자를 청빙합니다",
        posted_on=_NOW.date(),
        run_id=new_id(),
        fetched_at=_NOW,
        raw_text=raw_text,
        raw_meta=dict(raw_meta),
    )


def confirmed(
    raw: str | None, raw_text: str = "", **raw_meta: str
) -> tuple[Denomination, DenominationSource, str | None]:
    """표기가 원문에도 있는 정상 상황 — 대부분의 검사가 표 자체를 본다."""
    body = raw_text or (raw or "")
    return confirm(raw, _record(body, **raw_meta))


# ── 명시된 교단을 확정한다 ───────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("합동", Denomination.HAPDONG),
        ("예장합동", Denomination.HAPDONG),
        ("대한예수교장로회 합동", Denomination.HAPDONG),
        ("대한예수교장로회(합동)", Denomination.HAPDONG),
        ("대한 예수교 장로회 합동", Denomination.HAPDONG),
        ("예장 통합", Denomination.TONGHAP),
        ("기독교대한성결교회", Denomination.SEONGGYUL),
        ("예장고신", Denomination.GOSIN),
        ("백석", Denomination.BAEKSEOK),
        ("기독교대한감리회", Denomination.GAMLI),
        ("기감 서울남연회", Denomination.GAMLI),
        ("여의도순복음", Denomination.SUNBOK),
        ("기독교한국침례회", Denomination.BAPTIST),
        ("기하성", Denomination.SUNBOK),
        ("대한기독교나사렛성결회", Denomination.ETC),
        ("독립교단", Denomination.ETC),
        ("GAPCK", Denomination.HAPDONG),
        ("PROK", Denomination.ETC),
        ("AGK", Denomination.SUNBOK),
    ],
    ids=lambda value: str(value),
)
def test_a_stated_denomination_becomes_its_key(raw: str, expected: Denomination) -> None:
    """실측 730건이 78가지 표기로 갈린다 — 공백을 지우고 부분일치로 본다."""
    key, source, evidence = confirmed(raw)

    assert key is expected
    assert source is DenominationSource.STATED
    assert evidence is not None


def test_the_evidence_says_which_word_matched() -> None:
    """⚠️ 근거가 없으면 잘못 확정된 행을 봤을 때 표의 어느 줄이 문제인지 알 수 없다."""
    assert confirmed("대한예수교장로회 합동")[2] == "합동"


def test_the_evidence_is_a_word_not_a_pattern() -> None:
    """⚠️ 영문은 정규식으로 찾는다 — 근거로 **정규식 문자열**을 남기면 두 가지가 깨진다.

    운영자는 `(?<![a-z])pck(?![a-z])`를 3초에 확인할 수 없고(CONTRACT §2c), "원문 어디에
    적혔나" 검사가 그 문자열을 원문에서 찾다 늘 실패해 영문만 검사를 빠져나간다.
    """
    assert confirmed("PCK 소속")[2] == "pck"


# ── 낱말이 서로를 품는다 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("합동신학", Denomination.HAPSIN),
        ("예장합신", Denomination.HAPSIN),
        ("합동정통", Denomination.BAEKSEOK),
        ("백석대신", Denomination.BAEKSEOK),
        ("합동보수", Denomination.ETC),
    ],
    ids=lambda value: str(value),
)
def test_a_longer_name_wins_over_the_word_inside_it(raw: str, expected: Denomination) -> None:
    """⚠️ `합동신학`을 `합동`으로 읽으면 **합신 교회가 합동으로 저장된다**(CONTRACT §2c).

    순서가 아니라 **포함 관계**로 가른다 — 표의 줄을 옮겨 적어도 결과가 같아야 한다.
    """
    assert confirmed(raw)[0] is expected


def test_two_different_denominations_in_one_line_are_not_confirmed() -> None:
    """⚠️ `대한예수교장로회 독립교회 (고신에서 독립함)` — 고신에서 **나온** 독립교회다(실측 1건).

    `고신`만 보면 고신으로 저장된다. 무엇인지 모르면 `UNKNOWN`이 정답이고, 운영자가 정한다.
    """
    key, source, evidence = confirmed("대한예수교장로회 독립교회 (고신에서 독립함)")

    assert key is Denomination.UNKNOWN
    assert source is DenominationSource.UNKNOWN
    assert evidence is None


# ── 못 알아보면 지어내지 않는다 ──────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "본문 참조",
        "아래",
        "-",
        "없음",
        "대한예수교장로회",
        "PCA 호주장로교",
        "초교파",
    ],
    ids=[
        "없음",
        "빈값",
        "공백",
        "본문참조",
        "아래",
        "하이픈",
        "없음표기",
        "교파없는장로회",
        "해외",
        "초교파",
    ],
)
def test_what_is_not_a_denomination_stays_unknown(raw: str | None) -> None:
    """⚠️ `ETC`로 밀어 넣지 않는다 — `ETC`는 "그 외 교단"이라는 **주장**이다.

    `아래 참조`가 그리로 가면 거짓이 되고, 그 거짓은 공개 화면까지 간다.
    `초교파`는 소속이 **없다**고 적은 값이라 SPEC §5.3의 `NULL = 미상 또는 무소속`이 답이다.
    """
    assert confirmed(raw) == (Denomination.UNKNOWN, DenominationSource.UNKNOWN, None)


@pytest.mark.parametrize(
    "raw",
    [
        "미주한인예수교장로회(KAPC) 합동교단과 교류 교단.",
        "미주한인예수교장로회/ 합동교단과 교류 교단입니다.",
    ],
    ids=["마침표", "종결어미"],
)
def test_a_sentence_in_the_denomination_field_is_not_a_denomination(raw: str) -> None:
    """⚠️ 이 두 교회는 **KAPC**다(실측 2건) — `합동`은 문장 안에서 남의 교단을 가리킨다.

    `stated`는 운영자 검토를 건너뛰므로 여기서 틀리면 아무도 못 잡는다.

    ⚠️ **낱말이 아니라 모양을 본다.** `교류`·`협력`을 표로 두면 `연합`·`자매`가 뒤따르고
    끝나지 않는다. 게시판 교단 칸은 교회가 직접 쓰는 자유 입력 칸이라 문장이 들어온다.
    """
    assert confirmed(raw)[0] is Denomination.UNKNOWN


@pytest.mark.parametrize(
    "raw",
    ["한국독립교회선교단체연합회 (KAICAM)", "KAICAM", "한국독립교회선교단체협의회(카이캄, KAICAM)"],
    ids=["풀어쓴이름", "영문약칭", "한글약칭"],
)
def test_one_body_gets_one_answer_however_it_is_spelled(raw: str) -> None:
    """⚠️ 약칭만 적은 공고가 `UNKNOWN`으로 갈렸다 — **같은 단체가 표기에 따라 답이 달라졌다.**

    풀어 쓴 이름은 `독립`으로 걸리는데 `KAICAM` 한 낱말은 표에 없었다(실측 1건).
    """
    assert confirmed(raw)[0] is Denomination.ETC


def test_an_honorific_is_not_added_as_a_denomination() -> None:
    """⚠️ `예장 계신`은 실재하는 군소 교단이지만 **표에 넣지 않는다**(실측 3건 포기).

    `계신`은 `계시는`의 준말로 본문에 27번 나온다(`농촌 목회에 사명이 계신 분`).
    표기 3건을 얻자고 존댓말을 교단으로 읽으면 그 손해가 훨씬 크다 — 빈 칸이 답이다.
    """
    assert confirmed("대한예수교장로회 계신")[0] is Denomination.UNKNOWN
    assert confirmed("농촌 목회에 사명이 계신 분")[0] is Denomination.UNKNOWN


def test_a_plain_name_is_not_mistaken_for_a_sentence() -> None:
    """⚠️ 문장 판정이 넓으면 정상값 78가지가 통째로 막힌다 — 종결어미·마침표만 본다."""
    assert confirmed("대한예수교장로회(합동) 소속")[0] is Denomination.HAPDONG
    assert confirmed("기독교대한성결교회 서울지방회")[0] is Denomination.SEONGGYUL


def test_a_board_key_is_not_read_as_a_denomination() -> None:
    """⚠️ `PCKWORLD`가 `PCK`(통합)로 읽히면 게시판 이름이 교단이 된다 — 영문은 낱말 경계를 본다."""
    assert confirmed("PCKWORLD")[0] is Denomination.UNKNOWN
    assert confirmed("PCK")[0] is Denomination.TONGHAP


@pytest.mark.parametrize(
    "body",
    [
        "대한예수교장로회 GAPCK 소속입니다",
        "게시판: PCKWORLD 청빙 공고",
        "문의: hanyoungpck@example.kr",
    ],
    ids=["다른교단", "게시판키", "이메일"],
)
def test_a_longer_latin_word_cannot_ground_a_shorter_one(body: str) -> None:
    """⚠️ 낱말 경계를 **값에만** 쓰고 원문 대조는 부분일치로 두면 경계가 무의미해진다.

    모델이 답한 `PCK`(통합)를 원문의 `GAPCK`(**합동**)이 뒷받침해 다른 교단으로 확정됐다.
    원장에 `pckyesan`·`pckworld`·`hanyoungpck`가 실제로 있다 — 값과 근거는 같은 패턴으로 찾는다.
    """
    assert confirm("PCK", _record(body))[0] is Denomination.UNKNOWN


def test_a_reformed_theology_phrase_is_not_a_denomination() -> None:
    """⚠️ 본문에 `개혁주의 신학`이 54번 나온다 — `개혁`만 표에 두면 그게 교단이 된다(실측)."""
    assert confirmed("개혁주의 신학")[0] is Denomination.UNKNOWN
    assert confirmed("예장개혁")[0] is Denomination.ETC


# ── 표 자체의 적합성 ─────────────────────────────────────────────


def test_every_alias_maps_to_a_public_key() -> None:
    """⚠️ 표가 `UNKNOWN`을 가리키면 "확정했는데 값이 없는" 행이 되어 레코드가 거부한다."""
    for alias, key in (*_ALIASES, *_LATIN_ALIASES):
        assert key is not Denomination.UNKNOWN, alias


def test_every_public_key_is_reachable_from_the_table() -> None:
    """⚠️ 드리프트를 잡는 방향은 이쪽이다(CONTRACT §1).

    "표에 적힌 것이 유효한가"는 표를 지워도 통과한다 — `GAMLI`·`SUNBOK`·`BAPTIST` 열한 줄을
    통째로 지워도 아무도 몰랐다. 계약이 정한 key마다 **닿는 길이 있나**를 본다.
    """
    reachable = {key for _, key in (*_ALIASES, *_LATIN_ALIASES)}

    assert set(PUBLISHABLE_DENOMINATIONS) == reachable


def test_the_order_of_the_table_does_not_change_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 이 모듈의 중심 설계다 — 순서가 규칙이면 줄 하나 옮겨 적는 순간 합신이 합동이 된다.

    산문으로만 적어 두면 다음 사람이 표를 정렬하다 조용히 깨뜨린다. 실제로 섞어서 본다.
    """
    cases = [("합동신학", Denomination.HAPSIN), ("백석대신", Denomination.BAEKSEOK)]
    shuffled = list(_ALIASES)
    random.Random(20260815).shuffle(shuffled)
    monkeypatch.setattr(module, "_ALIASES", tuple(shuffled))

    for raw, expected in cases:
        assert confirmed(raw)[0] is expected, raw


def test_aliases_are_written_the_way_they_are_compared() -> None:
    """⚠️ 표기는 **공백 없는 소문자**로 견준다(`confirm`).

    표에 공백·대문자가 남아 있으면 그 줄은 영영 안 걸린다.
    """
    for alias, _ in (*_ALIASES, *_LATIN_ALIASES):
        assert alias == "".join(alias.split()).lower(), alias


# ── 자격 요건에 적힌 교단은 그 교회의 교단이 아니다 ─────────────


def test_a_denomination_named_only_as_an_entry_requirement_is_refused() -> None:
    """⚠️ `기타 교단 정규 신대원 졸업자(장신, 침신, 백석, 고신 등)` — **받아주는 신대원 목록**이다.

    모델이 그 줄을 집으면 합동 교회가 고신으로 저장된다. 실측 298건이 이 모양이다
    (`대신` 50 · `고신` 46 · `감리` 43).
    """
    body = (
        "풍기제일교회는 예장합동 소속의 건강한 교회입니다.\n"
        "① 총신대 신대원 및 교단 인준 신대원 졸업자.\n"
        "② 기타 교단 정규 신대원 졸업자(장신, 침신, 백석, 고신 등)."
    )

    assert confirm("고신", _record(body))[0] is Denomination.UNKNOWN
    assert confirm("예장합동", _record(body))[0] is Denomination.HAPDONG, "소속은 확정된다"


def test_a_denomination_the_board_states_is_kept_even_if_it_also_appears_in_requirements() -> None:
    """⚠️ 게시판 교단 칸은 자격 줄이 아니다 — `CSU` 730건이 여기 걸리면 안 된다."""
    record = _record(
        "지원자격: 총신 신대원 졸업자 또는 합동 인준 신대원 졸업자", order_name="예장합동"
    )

    assert confirm("예장합동", record)[0] is Denomination.HAPDONG


def test_a_value_that_is_nowhere_in_the_source_is_refused() -> None:
    """⚠️ 근거 없는 값은 확정하지 않는다 — **지어낸 값과 그림에만 있는 값을 코드는 구분 못 한다.**

    그림을 보낸 공고는 `verify`가 값을 비우지 않고 세기만 하므로(포스터가 원문이라 "본문에
    없다"가 정상이다), 여기서 막지 않으면 아무도 확인하지 않은 교단이 `stated`가 되어
    운영자 검토까지 건너뛴다. 실측 730건 중 0건이 여기 걸린다 — 잃는 것 없이 막힌다.
    """
    assert (
        confirm("예장합동", _record("교회 소개도 사역 안내도 없는 본문"))[0] is Denomination.UNKNOWN
    )


def test_a_poster_only_posting_confirms_from_the_board_field() -> None:
    """⚠️ 포스터 공고라도 게시판 교단 칸이 있으면 그 칸이 원문이다(실측 177건 중 68건)."""
    record = _record("", order_name="예장합동")

    assert confirm("예장합동", record)[0] is Denomination.HAPDONG


def test_a_conference_names_the_methodist_church() -> None:
    """`연회`는 감리교 고유 조직이다(장로교=노회 · 성결교·침례회=지방회). 교회가 교단 대신
    소속 연회만 적는 일이 흔하다 — 실측 2026-09-03 미상 36건이 전부 이 모양이었고, 원장
    전수에서 `연회`가 든 표기가 다른 교단으로 확정된 적은 0건이다."""
    assert confirmed("중앙연회 성남지방")[:2] == (Denomination.GAMLI, DenominationSource.STATED)


def test_our_own_denomination_is_not_a_school_list() -> None:
    """⚠️ 자격 줄이어도 `본교단`이면 그 교회의 소속이다 — 받아주는 신대원 목록이 아니라
    글쓴이를 가리키는 말이다. 실측 2026-09-03: 자격 줄에 막힌 13건 중 6건이 이 모양."""
    record = _record("*지원자격 : 본교단(통합) 신대원 졸업 후 목사안수 받으신 분")

    assert confirm("통합", record)[:2] == (Denomination.TONGHAP, DenominationSource.STATED)


def test_a_school_list_without_that_word_is_still_refused() -> None:
    """`본교단`이 없으면 예전 그대로 — `고신 등 졸업자 지원 가능`은 그 교회 교단이 아니다."""
    record = _record("지원자격 : 기타 교단 정규 신대원 졸업자(장신, 침신, 고신 등)")

    assert confirm("고신", record)[0] is Denomination.UNKNOWN


def test_a_poster_confirms_what_the_model_read_from_it() -> None:
    """⚠️ **대조할 원문이 없는 것과 원문에 없는 것은 다르다**(2026-09-03에 뒤집은 결정).

    앞은 확인할 방법이 없는 것이고 뒤는 근거가 없는 것이다. 본문이 그림뿐인 공고에서
    `verify`는 이미 **어느 칸도 비우지 않고**(SPEC §5.5b) 그 공고는 `media_sent`로
    `medium`이 되어 반드시 사람이 본다 — 교단만 다르게 취급할 이유가 없다.
    실측 2026-09-03: 이 가지에 38건(`CALVIN` 21 · `PCKWORLD` 14 …).
    """
    record = _record("", list_title="담임목사청빙(평강교회)")

    assert confirm("예장합동", record) == (
        Denomination.HAPDONG,
        DenominationSource.STATED,
        "예장합동",
    )


def test_text_that_does_not_mention_it_still_refuses() -> None:
    """⚠️ 위 예외는 **원문이 없을 때만**이다 — 본문이 있는데 그 표기가 없으면 근거가 없다."""
    record = _record("사역자를 모십니다. 문의는 아래로.", list_title="청빙")

    assert confirm("예장합동", record)[0] is Denomination.UNKNOWN
