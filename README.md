# min_job_agent

`../min_job`(교회 사역자 청빙 채용 플랫폼)을 위한 **공고 수집 크롤러**.

공식 게시판(신학교·교단 총회, 공개 31곳)에서 청빙 공고를 수집 → AI로 구조화 → **리뷰 큐**에 적재하면, 운영자가 min_job admin에서 검토·승인 후 게재한다. (min_job 본체는 in-repo 크롤러를 금지하므로 수집기를 별도 리포로 분리.)

> 📄 **문서 정본**: 파이프라인 = [`docs/SPEC.md`](docs/SPEC.md) · 소스 카탈로그 = [`docs/SOURCES.md`](docs/SOURCES.md) · 출력 계약·교단 = [`docs/CONTRACT.md`](docs/CONTRACT.md) · 시점 핸드오프 = [`docs/SNAPSHOT.md`](docs/SNAPSHOT.md). (`CLAUDE.md`·`docs/ROADMAP.md`·정식 `src/`는 미작성.)

## 브랜치 / Git

- `prod` — 배포·안정
- `dev` — 개발·작업 (기본 작업 브랜치)
- 릴리스: `dev → prod` **fast-forward only** (merge 커밋 만들지 않음)
- **commit / push / merge는 사용자가 명시적으로 요청할 때만.**
- 커밋 메시지: 영어, 동사 원형(Add/Fix/Update/Remove). 1 커밋 = 1 논리적 변경.

## 스키마 정본

**출력(공개)** 스키마·enum 정본 = `../min_job/docs/DATA.md`(`churches`/`jobs`). **크롤러 staging 스키마**(`source_data`·`review_data`·`source_health`·`crawl_run`)는 **이 리포가 소유·마이그레이션**한다(SPEC §6·§8). enum 드리프트 주의.
