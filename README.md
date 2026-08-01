# min_job_agent 

`../min_job`(교회 사역자 청빙 채용 플랫폼)을 위한 **공고 수집 크롤러**.

공식 게시판(신학교·교단 총회, 공개 31곳)에서 청빙 공고를 수집 → AI로 구조화 → **리뷰 큐**에 적재하면, 운영자가 min_job admin에서 검토·승인 후 게재한다. (min_job 본체는 in-repo 크롤러를 금지하므로 수집기를 별도 리포로 분리.)

> 📄 **문서 정본**: **실행 명령 = [`docs/RUNBOOK.md`](docs/RUNBOOK.md)** · 아키텍처·컨벤션·가드레일 = [`CLAUDE.md`](CLAUDE.md) · 파이프라인 = [`docs/SPEC.md`](docs/SPEC.md) · 소스 카탈로그 = [`docs/SOURCES.md`](docs/SOURCES.md) · 출력 계약·교단 = [`docs/CONTRACT.md`](docs/CONTRACT.md) · 작업 로드맵 = [`docs/ROADMAP.md`](docs/ROADMAP.md) · 시점 핸드오프 = [`docs/SNAPSHOT.md`](docs/SNAPSHOT.md).
>
> **게시판 전송 정본** = [`config/sources.json`](config/sources.json) — 31곳의 tier·encoding·flags·상세URL **라이브 검증값**. 문서와 다르면 이 파일이 이긴다.

## 환경 · 실행

**스택 = Python 3.12+**(2026-07-29 확정 · TS 뼈대 이식 완료).

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env                        # Vertex(Gemini) 서비스계정 값 입력

.venv/bin/minjob-ingest list-sources [KEY]   # 등록 소스 확인 (예: … YTUS)
.venv/bin/minjob-ingest check-gemini         # Vertex 인증 스모크 (실호출 1회)
```

**커밋 전 게이트 — 4개 전부 통과**
```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy && .venv/bin/pytest -q
```

> ⚠️ **프로덕션 수집은 운영자가 실행한다**(CLAUDE.md 가드레일 #10). 수집 명령(`daily`·`backfill`)은 Phase 1에서 붙는다.

## 브랜치 / Git

- `prod` — 배포·안정
- `dev` — 개발·작업 (기본 작업 브랜치)
- 릴리스: `dev → prod` **fast-forward only** (merge 커밋 만들지 않음)
- **commit / push / merge는 사용자가 명시적으로 요청할 때만.**
- 커밋 메시지: 영어, 동사 원형(Add/Fix/Update/Remove). 1 커밋 = 1 논리적 변경.

## 스키마 정본

- **출력(공개) 스키마** = `../min_job/docs/DATA.md`(`churches`/`jobs`).
- **크롤러가 쓰는 enum 허용값** = `docs/CONTRACT.md` §1 (min_job과 달라지면 CONTRACT를 따르고 불일치를 보고 — 드리프트 테스트 대상).
- **크롤러 staging 스키마**(`source_data`·`review_data`·`source_health`·`crawl_run`) = **이 리포가 소유·마이그레이션**(SPEC §6·§8).
