"""포스터 보관 — 경로 규약과 실패 처리(docs/REVIEW_PAGE.md §7.1).

여기서 막는 사고는 둘이다: **검수 화면이 포스터를 못 찾는 것**(경로가 계약과 다르다)과
**재구조화가 고아 파일을 남기는 것**(경로가 결정적이지 않다).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from minjob_ingest.pipeline.media import sniff_media_type
from minjob_ingest.settings import SupabaseSettings
from minjob_ingest.store.base import Poster, StoreError
from minjob_ingest.store.storage import _EXTENSIONS, BUCKET, SupabaseStorage
from tests.fake_storage import ALLOWED_MEDIA_TYPES, FakeStorage
from tests.fake_storage import BUCKET as FAKE_BUCKET

_SOURCE_DATA_ID = UUID("d38ae0db-1d8a-42ee-bb84-0c492694f8a4")

#: 실제 앞머리로 만든 최소 파일 — `sniff_media_type`이 이 형식으로 읽는다.
_JPEG = b"\xff\xd8\xff" + b"\x00" * 32
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_PDF = b"%PDF-1.4\n" + b"\x00" * 32


def _storage(fake: FakeStorage) -> SupabaseStorage:
    return SupabaseStorage(
        SupabaseSettings(url="https://x.supabase.co", service_role_key="secret"),
        transport=fake.transport(),
        sleep=lambda _: None,
    )


def _poster(data: bytes) -> Poster:
    media_type = sniff_media_type(data)
    assert media_type is not None, "테스트 표본이 우리가 읽는 형식이어야 한다"
    return Poster(media_type=media_type, data=data)


# ── 계약: 버킷 ──────────────────────────────────────────────


def test_the_bucket_check_passes_when_it_exists() -> None:
    fake = FakeStorage()
    with _storage(fake) as storage:
        storage.check_bucket()
    assert fake.requests == [("GET", f"/storage/v1/bucket/{BUCKET}")]


def test_a_missing_bucket_stops_us_before_any_upload() -> None:
    """⚠️ 이게 없으면 전량 실행이 포스터 공고마다 실패한다(추정 480건)."""
    fake = FakeStorage(bucket_exists=False)
    with _storage(fake) as storage, pytest.raises(StoreError, match="404"):
        storage.check_bucket()


def test_our_bucket_name_matches_the_one_that_exists() -> None:
    """⚠️ 이름이 갈리면 업로드가 전부 404다 — 실 버킷과 같은 값을 못박는다."""
    assert BUCKET == FAKE_BUCKET


# ── 계약: 경로 ──────────────────────────────────────────────


def test_paths_follow_the_agreed_shape() -> None:
    """min_job이 이 경로로 signed URL을 만든다 — 모양이 계약이다."""
    fake = FakeStorage()
    with _storage(fake) as storage:
        paths = storage.upload(
            source_key="YTUS",
            source_data_id=_SOURCE_DATA_ID,
            posters=[_poster(_JPEG), _poster(_PDF)],
        )

    assert paths == (
        f"YTUS/{_SOURCE_DATA_ID}/0.jpg",
        f"YTUS/{_SOURCE_DATA_ID}/1.pdf",
    )
    assert set(fake.objects) == set(paths)


def test_the_order_of_the_returned_paths_is_the_order_given() -> None:
    """⚠️ 포스터가 여러 장인 공고는 순서가 뒤바뀌면 읽을 수 없다."""
    fake = FakeStorage()
    with _storage(fake) as storage:
        paths = storage.upload(
            source_key="PCK",
            source_data_id=_SOURCE_DATA_ID,
            posters=[_poster(_PNG), _poster(_JPEG), _poster(_PDF)],
        )

    assert [path.rsplit("/", 1)[1] for path in paths] == ["0.png", "1.jpg", "2.pdf"]


def test_uploading_the_same_posting_again_overwrites_in_place() -> None:
    """⚠️ 경로가 결정적이라 재구조화가 고아 파일을 남기지 않는다.

    ⚠️ 서버는 `x-upsert` 없이는 409로 거절한다 — 그 헤더를 빼면 **재구조화가 전부 실패**한다.
    """
    fake = FakeStorage()
    with _storage(fake) as storage:
        first = storage.upload(
            source_key="YTUS", source_data_id=_SOURCE_DATA_ID, posters=[_poster(_JPEG)]
        )
        second = storage.upload(
            source_key="YTUS", source_data_id=_SOURCE_DATA_ID, posters=[_poster(_JPEG)]
        )

    assert first == second == (f"YTUS/{_SOURCE_DATA_ID}/0.jpg",)
    assert len(fake.objects) == 1


def test_a_changed_format_leaves_the_old_object_behind() -> None:
    """알고 미루는 것 — 확장자가 경로에 있어서 형식이 바뀌면 옛 파일이 남는다.

    ⚠️ **화면에는 안 뜬다**(`poster_paths`가 새 경로만 가리킨다) — 용량만 조금 쓴다.
    지우는 일을 넣지 않은 이유는 게시판이 그림 형식을 바꾸는 일이 드물고, 지우기를 넣으면
    "무엇을 지워도 되는가"를 판정하는 코드가 생기기 때문이다(REVIEW_PAGE §7).
    """
    fake = FakeStorage()
    with _storage(fake) as storage:
        storage.upload(source_key="YTUS", source_data_id=_SOURCE_DATA_ID, posters=[_poster(_JPEG)])
        paths = storage.upload(
            source_key="YTUS", source_data_id=_SOURCE_DATA_ID, posters=[_poster(_PNG)]
        )

    assert paths == (f"YTUS/{_SOURCE_DATA_ID}/0.png",)
    assert set(fake.objects) == {f"YTUS/{_SOURCE_DATA_ID}/0.jpg", f"YTUS/{_SOURCE_DATA_ID}/0.png"}


def test_nothing_is_uploaded_when_there_is_nothing_to_upload() -> None:
    fake = FakeStorage()
    with _storage(fake) as storage:
        assert storage.upload(source_key="YTUS", source_data_id=_SOURCE_DATA_ID, posters=[]) == ()
    assert fake.requests == []


# ── 형식 ────────────────────────────────────────────────────


def test_every_format_we_can_read_can_also_be_stored() -> None:
    """⚠️ **드리프트 테스트.** `sniff_media_type`이 내는 형식에 확장자가 없으면 그 포스터만
    조용히 안 올라간다 — 검수의 96%가 포스터라 그 구멍이 크다."""
    readable = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/bmp",
        "application/pdf",
    }
    assert set(_EXTENSIONS) == readable


def test_the_bucket_allows_exactly_what_we_can_store() -> None:
    """실 버킷의 `allowed_mime_types`와 우리 확장자표가 같아야 한다."""
    assert set(_EXTENSIONS) == set(ALLOWED_MEDIA_TYPES)


def test_a_format_without_an_extension_is_skipped_not_fatal() -> None:
    """⚠️ 한 파일 때문에 그 공고를 잃지 않는다 — 나머지는 올라가고 로그가 이유를 남긴다."""
    fake = FakeStorage()
    with _storage(fake) as storage:
        paths = storage.upload(
            source_key="YTUS",
            source_data_id=_SOURCE_DATA_ID,
            posters=[
                Poster(media_type="image/tiff", data=b"II*\x00"),
                _poster(_JPEG),
            ],
        )

    # ⚠️ 번호는 **자리 순서**다 — 건너뛴 파일이 번호를 쓰고 가지 않는다.
    assert paths == (f"YTUS/{_SOURCE_DATA_ID}/1.jpg",)


def test_the_media_type_we_send_is_the_one_we_sniffed() -> None:
    """⚠️ 헤더를 잘못 보내면 버킷의 MIME 제한이 415로 거절한다."""
    fake = FakeStorage()
    with _storage(fake) as storage:
        storage.upload(source_key="YTUS", source_data_id=_SOURCE_DATA_ID, posters=[_poster(_PDF)])

    (media_type, data) = fake.objects[f"YTUS/{_SOURCE_DATA_ID}/0.pdf"]
    assert media_type == "application/pdf"
    assert data == _PDF


# ── 실패 ────────────────────────────────────────────────────


def test_a_transient_failure_is_retried() -> None:
    """전송 층이 재시도한다 — 여기서 정책을 다시 쓰지 않았음을 확인한다."""
    fake = FakeStorage()
    fake.failures = [503]
    with _storage(fake) as storage:
        paths = storage.upload(
            source_key="YTUS", source_data_id=_SOURCE_DATA_ID, posters=[_poster(_JPEG)]
        )

    assert paths == (f"YTUS/{_SOURCE_DATA_ID}/0.jpg",)
    assert len(fake.requests) == 2


def test_a_permanent_failure_becomes_a_store_error() -> None:
    """4xx는 다시 보내도 같은 답이다 — 재시도하지 않고 올려 보낸다."""
    fake = FakeStorage()
    fake.failures = [400]
    with _storage(fake) as storage, pytest.raises(StoreError, match="400"):
        storage.upload(source_key="YTUS", source_data_id=_SOURCE_DATA_ID, posters=[_poster(_JPEG)])
    assert len(fake.requests) == 1


def test_the_service_key_never_reaches_the_error_message() -> None:
    """⚠️ 예외 메시지는 로그·리포트로 나간다 — 키가 섞이면 안 된다."""
    fake = FakeStorage()
    fake.failures = [400]
    with _storage(fake) as storage, pytest.raises(StoreError) as caught:
        storage.upload(source_key="YTUS", source_data_id=_SOURCE_DATA_ID, posters=[_poster(_JPEG)])
    assert "secret" not in str(caught.value)
