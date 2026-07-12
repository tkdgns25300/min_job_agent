# min_job_agent

`../min_job`(교회 사역자 청빙 채용 플랫폼)을 위한 **공고 수집 크롤러**.

공식 게시판(신학교·교단·노회)에서 청빙 공고를 수집 → AI로 구조화 → **리뷰 큐**에 적재하면, 운영자가 min_job admin에서 검토·승인 후 게재한다. (min_job 본체는 in-repo 크롤러를 금지하므로 수집기를 별도 리포로 분리.)

> ⚠️ **초기 세팅 중.** 아키텍처·컨벤션·데이터 계약 등 문서는 **소스 정찰(`docs/SOURCES.md`) 이후** 작성 예정.

## 브랜치 / Git

- `prod` — 배포·안정
- `dev` — 개발·작업 (기본 작업 브랜치)
- 릴리스: `dev → prod` **fast-forward only** (merge 커밋 만들지 않음)
- **commit / push / merge는 사용자가 명시적으로 요청할 때만.**
- 커밋 메시지: 영어, 동사 원형(Add/Fix/Update/Remove). 1 커밋 = 1 논리적 변경.

## 스키마 정본

출력 스키마·enum의 정본(canonical)은 `../min_job/docs/DATA.md`. 이 리포는 참조·미러링만 한다(enum 드리프트 주의).
