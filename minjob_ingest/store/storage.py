"""Supabase Storage 전송 — 포스터를 보관하는 **유일한 창구**.

**왜 보관하나** — 검수 대상의 **88%가 포스터 공고**다(1주치 실측 69건 중 61건 · SPEC §5.7).
검수 화면이 원본 게시판 URL을 그대로 `<img src>`에 걸면 최소 5~7곳이 안 뜬다:
`http_only` 2곳(https 페이지에서 mixed content 차단) · `insecure_tls` 3곳(인증서 오류) ·
`needs_session` 2곳(쿠키 필요) + 열거할 수 없는 referer 차단. 게다가 게시판이 글을 지우면
판정을 되짚을 증거가 사라진다.

구조화는 **이미 그 바이트를 손에 들고 있다**(Gemini에 보내려고 받는다). 버리지 않고 올린다.

⚠️ **전송 정책은 `store/transport.py`가 한다** — 재시도·백오프·`Retry-After`. 여기 다시 쓰면
원장 쪽과 두 벌이 되어 한쪽만 고쳐진다.

⚠️ **버킷은 코드가 만들지 않는다**(운영자가 대시보드에서 한 번 만든다 · RUNBOOK). 만들기를
코드에 넣으면 실패 경로가 하나 늘고, 실제로는 한 번 하는 일이다. 대신 **있는지 확인**한다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Final
from uuid import UUID

import httpx

from minjob_ingest.settings import SupabaseSettings
from minjob_ingest.store.base import Poster
from minjob_ingest.store.transport import REQUEST_TIMEOUT_SECONDS, Sleeper, send_with_retry

_LOG = logging.getLogger(__name__)

#: 포스터가 들어가는 버킷. **비공개**다 — 포스터에 담당자 이름·연락처가 있어서 인증 없이
#: 읽히면 그게 그대로 공개된다. 검수 화면은 signed URL로 본다.
BUCKET: Final = "postings"

#: 형식 → 파일 확장자. ⚠️ **`sniff_media_type`이 낼 수 있는 값 전부**여야 한다 — 빠진 형식이
#: 있으면 그 포스터만 조용히 안 올라간다. 버킷의 `allowed_mime_types`도 같은 여섯이다.
_EXTENSIONS: Final = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "application/pdf": "pdf",
}


class SupabaseStorage:
    """`PosterStore`의 Supabase 구현. 저장 의미(무엇을 왜 올리나)는 모른다 — 그건 구조화다."""

    def __init__(
        self,
        settings: SupabaseSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleeper = time.sleep,
    ) -> None:
        # `transport`는 테스트가 가짜 Storage를 끼우는 자리다(`PostgrestClient`와 같은 방식).
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=settings.storage_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
            headers={
                "apikey": settings.service_role_key,
                "Authorization": f"Bearer {settings.service_role_key}",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SupabaseStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def check_bucket(self) -> None:
        """버킷이 있나. 없으면 **한 건도 올리기 전에** 멈춘다."""
        send_with_retry(
            self._client,
            "GET",
            f"/bucket/{BUCKET}",
            params={},
            content=None,
            headers={"Accept": "application/json"},
            sleep=self._sleep,
        )

    def upload(
        self, *, source_key: str, source_data_id: UUID, posters: Sequence[Poster]
    ) -> tuple[str, ...]:
        """올리고 경로들을 돌려준다. 형식을 모르는 파일은 **건너뛰고 경고**한다."""
        paths: list[str] = []
        for number, poster in enumerate(posters):
            extension = _EXTENSIONS.get(poster.media_type)
            if extension is None:
                # 여기 오면 `_EXTENSIONS`가 `sniff_media_type`보다 뒤처진 것이다 — 조용히
                # 넘기지 않는다. 그 공고는 포스터 없이 검수로 가고 로그가 이유를 남긴다.
                _LOG.warning(
                    "올릴 수 없는 형식이라 건너뜀 (source_data=%s · %s)",
                    source_data_id,
                    poster.media_type,
                )
                continue
            path = f"{source_key}/{source_data_id}/{number}.{extension}"
            self._put(path, poster)
            paths.append(path)
        return tuple(paths)

    def _put(self, path: str, poster: Poster) -> None:
        """한 파일. ⚠️ `x-upsert`가 있어야 **재구조화가 같은 자리를 덮는다**(고아 파일 없음)."""
        send_with_retry(
            self._client,
            "POST",
            f"/object/{BUCKET}/{path}",
            params={},
            content=poster.data,
            headers={
                "Content-Type": poster.media_type,
                "x-upsert": "true",
            },
            sleep=self._sleep,
        )
