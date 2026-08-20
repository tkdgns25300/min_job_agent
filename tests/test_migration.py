"""마이그레이션 SQL ↔ 레코드·enum 드리프트 테스트.

**스키마 정본은 SPEC §6이고 SQL은 그 구현**이다(CLAUDE.md). 사람의 규율로는 어긋남을 막을 수
없어서 여기서 대조한다 — `ReviewData`에 칸을 하나 붙이고 SQL을 안 고치면 그 값은 **Supabase에
저장되지 않는다**(PostgREST가 없는 컬럼을 거부한다). 반대로 SQL에만 있는 칸은 `serde`가
"잉여 컬럼"으로 거부해 **그 행을 통째로 읽지 못한다**.

⚠️ 네트워크·DB 없이 돈다 — SQL을 텍스트로 읽어 대조한다. 실제 적용은 운영자가 하고(RUNBOOK),
문법 검증은 그때 Postgres가 한다.
"""

from __future__ import annotations

import re
from dataclasses import fields
from typing import Final

import pytest

from minjob_ingest.domain import (
    Confidence,
    CrawlMode,
    DedupState,
    Denomination,
    DenominationSource,
    Department,
    EmploymentType,
    IsChurchRecruitment,
    JobKind,
    Position,
    Qualification,
    Region,
    RejectReason,
    ReviewStatus,
    SourceHealthStatus,
    StipendPeriod,
)
from minjob_ingest.models import CrawlRun, ReviewData, SourceData, SourceHealth
from minjob_ingest.paths import PROJECT_ROOT
from minjob_ingest.store.serde import StagingRecord

_MIGRATIONS: Final = PROJECT_ROOT / "supabase" / "migrations"

#: 테이블 이름 → 그 테이블을 담는 레코드. SPEC §6의 4테이블 전부.
_TABLES: Final[dict[str, type[StagingRecord]]] = {
    "crawl_run": CrawlRun,
    "source_data": SourceData,
    "review_data": ReviewData,
    "source_health": SourceHealth,
}

#: 컬럼 → 그 컬럼의 허용값을 정하는 enum.
_ENUM_COLUMNS: Final = {
    "mode": CrawlMode,
    "is_church_recruitment": IsChurchRecruitment,
    "job_kind": JobKind,
    "position": Position,
    "department": Department,
    "employment_type": EmploymentType,
    "qualification": Qualification,
    "pay_period": StipendPeriod,
    "region": Region,
    "denomination": Denomination,
    "denomination_source": DenominationSource,
    "confidence": Confidence,
    "dedup_state": DedupState,
    "review_status": ReviewStatus,
    "reject_reason": RejectReason,
    "last_status": SourceHealthStatus,
}

#: **의도적으로 좁힌 허용값.** 게이트1 `NO`는 review_data를 만들지 않으므로(SPEC §5.1) DB도
#: 받지 않는다 — 이 테스트가 그걸 "누락"으로 잡지 않게 여기에 적어 둔다.
_NARROWED: Final = {"is_church_recruitment": frozenset({IsChurchRecruitment.NO.value})}

#: SQL 컬럼 선언에 쓰는 타입. 제약절(`constraint …`)·주석과 구분하는 데 쓴다.
_COLUMN_LINE: Final = re.compile(r"^([a-z_]+)\s+(?:uuid|text|int|boolean|date|timestamptz|jsonb)\b")


def _sql() -> str:
    """마이그레이션 SQL 전문. 파일이 늘면 이어 붙인다(적용 순서 = 파일명 순서)."""
    paths = sorted(_MIGRATIONS.glob("*.sql"))
    assert paths, f"{_MIGRATIONS}에 마이그레이션이 없다"
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _table_bodies() -> dict[str, str]:
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r"^create table (\w+) \((.*?)^\);", _sql(), re.S | re.M)
    }


def _columns_of(body: str) -> set[str]:
    found: set[str] = set()
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("--") or line.startswith("constraint"):
            continue
        match = _COLUMN_LINE.match(line)
        if match is not None:
            found.add(match.group(1))
    return found


def _allowed_values() -> dict[str, set[str]]:
    """`check (col in (...))`과 `check (col <@ array[...])`에서 허용값을 줍는다."""
    # 주석에 예시 값이 들어 있을 수 있어 먼저 떼고, CHECK가 여러 줄에 걸쳐 있어 한 줄로 만든다.
    flat = " ".join(line.split("--")[0] for line in _sql().splitlines())
    found: dict[str, set[str]] = {}
    for pattern in (
        r"check \((\w+) in \(([^)]*)\)\)",
        r"check \((\w+) <@ array\[([^\]]*)\]\)",
    ):
        for match in re.finditer(pattern, flat):
            found[match.group(1)] = set(re.findall(r"'([^']+)'", match.group(2)))
    return found


def test_every_staging_table_is_defined() -> None:
    assert set(_table_bodies()) == set(_TABLES)


@pytest.mark.parametrize("table", sorted(_TABLES))
def test_columns_match_the_record(table: str) -> None:
    """⚠️ 칸을 추가하고 SQL을 안 고치면 그 값은 저장되지 않는다 — 이 테스트가 그걸 잡는다."""
    expected = {field.name for field in fields(_TABLES[table])}
    assert _columns_of(_table_bodies()[table]) == expected


@pytest.mark.parametrize("column", sorted(_ENUM_COLUMNS))
def test_check_constraint_matches_the_enum(column: str) -> None:
    """허용값 정본은 CONTRACT §1 + `domain.py`다. DB CHECK는 그 미러여야 한다.

    ⚠️ enum 값을 지우면 저장된 행을 읽을 수 없다(`scripts/migrate_dedup_state.py`가 그 건이다).
    CHECK가 뒤처지면 DB가 없어진 값을 계속 받아 그 상황을 다시 만든다.
    """
    expected = {member.value for member in _ENUM_COLUMNS[column]} - _NARROWED.get(
        column, frozenset()
    )
    assert _allowed_values()[column] == expected


def test_no_native_enum_types() -> None:
    """native enum을 쓰지 않는다 — `ALTER TYPE ... DROP VALUE`가 없어 값을 지울 수 없다."""
    assert "create type" not in _sql().lower()


def test_rls_and_grant_are_deferred() -> None:
    """⚠️ 정책 없이 RLS를 켜면 min_job admin 검수 화면이 통째로 빈 화면이 된다.

    켜는 것과 정책은 **같은 파일**에 둔다(다음 마이그레이션) — 이 테스트는 그 둘이 실수로
    여기 들어오는 것을 막는다.
    """
    lowered = _sql().lower()
    assert "row level security" not in lowered
    assert "grant " not in lowered
