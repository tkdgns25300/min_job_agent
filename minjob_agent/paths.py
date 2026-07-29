"""리포 기준 경로 — 한 곳에서만 계산한다.

CWD 기준 상대경로(`Path("data")`)를 쓰면 실행 디렉터리에 따라 저장소가 갈라져
원장을 잃고(전량 재크롤 · 가드레일 #7) `.gitignore`의 `/data/`도 비껴간다(가드레일 #11).
그래서 리포 루트에 고정한다. 배포 레이아웃에서는 CLI 플래그·env로 덮어쓴다(settings).
"""

from __future__ import annotations

from pathlib import Path

#: <repo>/  — 이 파일이 minjob_agent/paths.py이므로 한 단계 위.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

#: 소스 레지스트리 config(커밋 대상).
DEFAULT_SOURCES_PATH: Path = PROJECT_ROOT / "config" / "sources.json"

#: 로컬 JSON 저장소(gitignored). Supabase 전환 후에는 쓰이지 않는다.
DEFAULT_DATA_DIR: Path = PROJECT_ROOT / "data"

#: 로컬 비밀 파일(커밋 금지). `.env.example`만 커밋한다.
DEFAULT_DOTENV_PATH: Path = PROJECT_ROOT / ".env"
