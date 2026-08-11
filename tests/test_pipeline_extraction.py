"""추출 계약 테스트 — 프롬프트에 무엇을 넣고 응답을 어떻게 검증하는가.

모델을 부르지 않는다(가드레일 #10). 프롬프트 조립과 응답 파싱은 순수 함수라 그대로 검증된다.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Final

import pytest

from minjob_ingest.clock import KST
from minjob_ingest.domain import IsChurchRecruitment
from minjob_ingest.models import MAX_DESCRIPTION_CHARS, Attachment, JsonValue, SourceData, new_id
from minjob_ingest.pipeline.extraction import (
    MAX_SHORT_TEXT_CHARS,
    RESPONSE_SCHEMA,
    ExtractionError,
    build_prompt,
    parse_extraction,
)

_NOW: Final = datetime(2026, 8, 10, 9, 0, tzinfo=KST)


def _source_data(
    *,
    raw_text: str = "점촌제일교회에서 전임 사역자를 청빙합니다.",
    raw_meta: dict[str, JsonValue] | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> SourceData:
    return SourceData(
        source_key="CSU",
        external_id="1117808",
        source_url="https://example.kr/board/1117808",
        title="점촌제일교회 전임 사역자 청빙",
        run_id=new_id(),
        fetched_at=_NOW,
        raw_text=raw_text,
        raw_meta=raw_meta or {},
        attachments=attachments,
    )


def _answer(**overrides: object) -> str:
    payload: dict[str, object] = {
        "church_name": "점촌제일교회",
        "title": "전임 사역자 청빙",
        "is_church_recruitment": "YES",
        "description": "전임 사역자를 청빙합니다.",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ── 프롬프트 ─────────────────────────────────────────────────────


def test_the_prompt_carries_the_board_title_and_body() -> None:
    prompt = build_prompt(_source_data())

    assert "CSU" in prompt
    assert "점촌제일교회 전임 사역자 청빙" in prompt
    assert "전임 사역자를 청빙합니다." in prompt


def test_an_empty_body_is_marked_not_left_blank() -> None:
    """빈 칸을 그대로 두면 모델이 앞 문단을 본문으로 오해한다."""
    prompt = build_prompt(_source_data(raw_text="   "))

    assert "(본문 없음)" in prompt


def test_board_fields_are_included_because_some_boards_keep_the_facts_there() -> None:
    """⚠️ `CSU`는 교단·교회명·지역이 본문이 아니라 `raw_meta`에 있다(SPEC §5.3)."""
    prompt = build_prompt(_source_data(raw_text="", raw_meta={"order_name": "예장통합"}))

    assert "게시판 필드:" in prompt
    assert "order_name: 예장통합" in prompt


def test_ui_only_board_fields_are_left_out() -> None:
    """조회수·번호는 공고 내용이 아니다 — 토큰만 쓰고 판단을 흐린다."""
    prompt = build_prompt(_source_data(raw_meta={"views": 42, "order_name": "예장합동"}))

    assert "views" not in prompt
    assert "order_name" in prompt


def test_blank_board_fields_are_left_out() -> None:
    prompt = build_prompt(_source_data(raw_meta={"gratuity": "  ", "order_name": "기감"}))

    assert "gratuity" not in prompt


def test_no_board_field_block_when_there_is_nothing_to_show() -> None:
    assert "게시판 필드:" not in build_prompt(_source_data(raw_meta={"views": 1}))


def test_attachment_names_reach_the_model() -> None:
    """본문·이미지가 없고 첨부만 있는 공고가 있다 — 이름이 유일한 단서다."""
    prompt = build_prompt(
        _source_data(
            raw_text="",
            attachments=(Attachment(name="청빙공고문.hwp", url="https://e.kr/a.hwp"),),
        )
    )

    assert "첨부: 청빙공고문.hwp" in prompt


def test_the_summary_limit_is_stated_in_the_prompt() -> None:
    assert str(MAX_DESCRIPTION_CHARS) in build_prompt(_source_data())


# ── 응답 스키마 ──────────────────────────────────────────────────


def test_the_gate1_enum_matches_the_stored_values() -> None:
    """스키마와 `domain.py`가 갈라지면 모델이 저장할 수 없는 값을 돌려준다."""
    gate1 = (RESPONSE_SCHEMA.properties or {})["is_church_recruitment"]
    assert set(gate1.enum or []) == {value.value for value in IsChurchRecruitment}


# ── 응답 파싱 ────────────────────────────────────────────────────


def test_a_well_formed_answer_becomes_an_extraction() -> None:
    extraction = parse_extraction(_answer())

    assert extraction.is_church_recruitment is IsChurchRecruitment.YES
    assert extraction.church_name == "점촌제일교회"
    assert extraction.description == "전임 사역자를 청빙합니다."


def test_nulls_and_blanks_both_mean_no_value() -> None:
    extraction = parse_extraction(_answer(church_name=None, title="   "))

    assert extraction.church_name is None
    assert extraction.title is None


def test_text_that_is_not_json_is_a_failure() -> None:
    with pytest.raises(ExtractionError, match="JSON"):
        parse_extraction("죄송합니다, 답변드릴 수 없습니다.")


def test_a_json_array_is_a_failure() -> None:
    with pytest.raises(ExtractionError, match="객체"):
        parse_extraction("[1, 2]")


def test_a_missing_key_is_a_failure_not_a_none() -> None:
    """⚠️ 없는 키를 `None`으로 흘리면 모델이 스키마를 무시한 응답이 초안으로 들어온다."""
    payload = json.loads(_answer())
    del payload["church_name"]

    with pytest.raises(ExtractionError, match="church_name"):
        parse_extraction(json.dumps(payload))


def test_a_wrong_type_is_a_failure() -> None:
    with pytest.raises(ExtractionError, match="문자열이 아님"):
        parse_extraction(_answer(title=12))


def test_a_missing_gate1_is_a_failure() -> None:
    payload = json.loads(_answer())
    del payload["is_church_recruitment"]

    with pytest.raises(ExtractionError, match="is_church_recruitment"):
        parse_extraction(json.dumps(payload))


def test_an_unknown_gate1_value_falls_back_to_uncertain() -> None:
    """허용값 밖은 운영자에게 보낸다 — 드롭하는 것보다 안전하다(SPEC §5.1)."""
    extraction = parse_extraction(_answer(is_church_recruitment="MAYBE"))

    assert extraction.is_church_recruitment is IsChurchRecruitment.UNCERTAIN


def test_gate1_is_read_case_insensitively() -> None:
    extraction = parse_extraction(_answer(is_church_recruitment="yes"))

    assert extraction.is_church_recruitment is IsChurchRecruitment.YES


def test_a_summary_over_the_limit_is_a_failure_not_a_truncation() -> None:
    """⚠️ 상한을 넘겼다는 것은 원문 복사 신호다 — 잘라서 저장하면 잘린 복사본이 남는다."""
    with pytest.raises(ExtractionError, match="상한"):
        parse_extraction(_answer(description="가" * (MAX_DESCRIPTION_CHARS + 1)))


def test_a_summary_exactly_at_the_limit_is_accepted() -> None:
    extraction = parse_extraction(_answer(description="가" * MAX_DESCRIPTION_CHARS))

    assert extraction.description is not None
    assert len(extraction.description) == MAX_DESCRIPTION_CHARS


def test_board_fields_reach_the_model_including_names() -> None:
    """맥락은 다 준다(운영자 결정 2026-08-10).

    가드레일 #4는 "제3자 개인정보를 **추출**하지 않는다"이지 모델에 맥락으로 주지 말라는
    뜻이 아니다 — 맥락이 적을수록 오추출이 늘어난다. 저장·공개는 출력 필드가 막는다.
    """
    prompt = build_prompt(
        _source_data(
            raw_text="",
            raw_meta={"author": "이관석", "senior_pastor": "김목사", "church_name": "창원왕성교회"},
        )
    )

    assert "author: 이관석" in prompt
    assert "senior_pastor: 김목사" in prompt
    assert "창원왕성교회" in prompt


def test_the_output_contract_has_no_slot_for_a_person() -> None:
    """맥락으로 받은 이름이 **저장될 칸이 없다** — 이것이 가드레일 #4를 지키는 자리다."""
    assert set(RESPONSE_SCHEMA.properties or {}) == {
        "church_name",
        "title",
        "is_church_recruitment",
        "description",
    }


def test_the_board_title_is_not_sent_twice() -> None:
    """`list_title`은 프롬프트가 이미 `제목:`으로 보낸다 — 토큰만 쓴다."""
    prompt = build_prompt(_source_data(raw_meta={"list_title": "점촌제일교회 전임 사역자 청빙"}))

    assert prompt.count("점촌제일교회 전임 사역자 청빙") == 1


@pytest.mark.parametrize("key", ["title", "church_name"], ids=["제목", "교회명"])
def test_a_one_line_field_that_swallowed_the_body_is_a_failure(key: str) -> None:
    """⚠️ `description`만 막으면 원문이 `title`로 흘러 공개된다(가드레일 #3)."""
    with pytest.raises(ExtractionError, match="한 줄 상한"):
        parse_extraction(_answer(**{key: "가" * (MAX_SHORT_TEXT_CHARS + 1)}))


def test_the_prompt_pins_the_summary_voice() -> None:
    """어투를 안 정하면 공고마다 개조식·교회 1인칭·불릿·기도문이 섞인다.

    채용 사이트에서는 그게 그대로 보인다 — 규칙이 프롬프트에 있는지 고정한다.
    """
    prompt = build_prompt(_source_data())

    for rule in ("평서문", "3인칭", "머리기호", "보태지 않는다"):
        assert rule in prompt, f"어투 규칙이 빠졌다: {rule}"


def test_the_prompt_keeps_what_the_church_is_like() -> None:
    """⚠️ 교회의 사역 방향은 광고 문구가 아니라 **지원 판단의 핵심 정보**다.

    실측(DAESHIN 0001541099999999): "인사말·광고 문구를 빼라"고만 했더니 모델이 교회 소개
    2문단(`말씀중심·기도중심·선교중심`·`전원 속 도심교회`)을 통째로 버렸다. 사역자 채용에서
    이건 사례비만큼 중요하고, 담을 칸도 `description`밖에 없다.
    """
    prompt = build_prompt(_source_data())

    assert "교회의 성격·사역 방향·입지는 남긴다" in prompt
    assert "정보가 없는 인사·축복·기도 문구만 뺀다" in prompt
