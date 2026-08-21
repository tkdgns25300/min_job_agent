"""메모리 위의 **가짜 Supabase Storage** — 포스터 보관을 네트워크 없이 계약 검증한다.

우리가 실제로 쓰는 것만 해석한다(`GET /bucket/{name}` · `POST /object/{bucket}/{path}`).
그 밖의 요청이 오면 **조용히 무시하지 않고 예외를 던진다** — 가짜가 관대하면 테스트는
초록불인데 진짜 서버에서만 깨진다(`fake_postgrest.py`와 같은 규율. 실제로 그 관대함 때문에
버그 셋을 놓친 적이 있다).

⚠️ **버킷 규칙도 흉내낸다** — 실 버킷은 `postings`(비공개 · 파일당 8MB · MIME 6종)로 만들어져
있다. 그 제약을 여기서도 걸어야 "우리 코드가 버킷 설정과 어긋나는" 경우가 테스트에서 걸린다.
"""

from __future__ import annotations

from typing import Final

import httpx

#: 실제 버킷과 같은 값 — 어긋나면 그 자체가 버그다(`storage.BUCKET`과 대조하는 테스트가 있다).
BUCKET: Final = "postings"

#: 실 버킷의 `file_size_limit`. 우리 `MAX_MEDIA_BYTES`와 같은 값으로 만들어 두었다.
MAX_FILE_BYTES: Final = 8 * 1024 * 1024

#: 실 버킷의 `allowed_mime_types`.
ALLOWED_MEDIA_TYPES: Final = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "application/pdf"}
)


class FakeStorage:
    """올라온 파일을 경로별로 들고 있는다. `objects`를 그대로 들여다보며 검증한다."""

    def __init__(self, *, bucket_exists: bool = True) -> None:
        self.bucket_exists = bucket_exists
        #: 경로 → (형식, 바이트). ⚠️ 덮어쓰기를 흉내내려고 dict다.
        self.objects: dict[str, tuple[str, bytes]] = {}
        #: 받은 요청 순서 — 순서·횟수도 계약이다.
        self.requests: list[tuple[str, str]] = []
        #: 다음 요청에 돌려줄 상태코드(재시도 검증용). 하나 꺼내 쓰고 없으면 정상 처리한다.
        self.failures: list[int] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if self.failures:
            return httpx.Response(self.failures.pop(0), json={"message": "일시적 오류"})
        if request.method == "GET" and path.startswith("/storage/v1/bucket/"):
            return self._bucket(path.removeprefix("/storage/v1/bucket/"))
        if request.method == "POST" and path.startswith("/storage/v1/object/"):
            return self._put(request, path.removeprefix("/storage/v1/object/"))
        raise AssertionError(f"가짜 Storage가 모르는 요청: {request.method} {path}")

    def _bucket(self, name: str) -> httpx.Response:
        if name != BUCKET:
            raise AssertionError(f"우리 버킷이 아니다: {name!r}")
        if not self.bucket_exists:
            return httpx.Response(404, json={"error": "Bucket not found", "statusCode": "404"})
        return httpx.Response(200, json={"id": BUCKET, "name": BUCKET, "public": False})

    def _put(self, request: httpx.Request, target: str) -> httpx.Response:
        bucket, _, path = target.partition("/")
        if bucket != BUCKET:
            raise AssertionError(f"우리 버킷이 아니다: {bucket!r}")
        if not path:
            raise AssertionError("경로 없이 올리려 했다")
        if not self.bucket_exists:
            return httpx.Response(404, json={"error": "Bucket not found", "statusCode": "404"})
        # ⚠️ 덮어쓰기는 `x-upsert`가 있을 때만 — 없으면 진짜 서버가 409를 준다.
        if path in self.objects and request.headers.get("x-upsert") != "true":
            return httpx.Response(409, json={"error": "Duplicate", "statusCode": "409"})
        media_type = request.headers.get("Content-Type", "")
        if media_type not in ALLOWED_MEDIA_TYPES:
            return httpx.Response(415, json={"message": f"mime type {media_type} is not supported"})
        if len(request.content) > MAX_FILE_BYTES:
            return httpx.Response(413, json={"message": "Payload too large"})
        self.objects[path] = (media_type, request.content)
        return httpx.Response(200, json={"Key": f"{BUCKET}/{path}"})
