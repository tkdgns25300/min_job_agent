# min_job_agent — 작업 로드맵

> 파이프라인 명세는 [`SPEC.md`](./SPEC.md), 소스 카탈로그는 [`SOURCES.md`](./SOURCES.md), 출력 계약·교단은 [`CONTRACT.md`](./CONTRACT.md), 시점 핸드오프는 [`SNAPSHOT.md`](./SNAPSHOT.md).
> 브랜치: `prod`(배포) / `dev`(작업), dev→prod **ff-only**. commit·push·merge는 사용자 명시 요청 시에만.
>
> **스택 = TypeScript/Node**(min_job enum·타입 공유해 드리프트 최소화). **저장은 JSON 먼저 → 스키마 굳으면 Supabase 스왑**(1-6). 크롤러는 `review_data`까지만 만들고, **공개 게재(`churches`/`jobs`)는 min_job 측**(min_job ROADMAP 1-10).
>
> **작업 원칙**: **걷는 뼈대(1소스 전 구간 관통) 먼저 → 어댑터 대량 확장은 뒤.** fetch→구조화→review_data 계약이 실제로 연결되는지 **31곳 만들기 전에** 검증한다.

## Phase 0: 뼈대 (골격)

> "빈 파이프" — 코드 골격 + 저장 seam + 외부 연결(Gemini) 검증. 로직은 Phase 1.
- [ ] 부트스트랩 — `package.json`·`tsconfig`(strict)·Prettier·`.env.example`·폴더 구조
- [ ] **Store seam(인터페이스) + JSON 구현** — 파이프라인이 저장소를 모르게(나중 Supabase 스왑용)
- [ ] `types/domain.ts` — min_job enum 미러(교단 10키·region·position·department·employment_type…)
- [ ] **Gemini(Vertex) 인증 실호출 1번 성공** — service-account(PROJECT_ID·CLIENT_EMAIL·PRIVATE_KEY) + API 활성 검증(**알려진 셋업 함정 먼저 제거**)
- [ ] 소스 레지스트리 스캐폴드 — `sources/registry.ts` 1칸(`YTUS`) + `SourceAdapter` 인터페이스(SPEC §10)

## Phase 1: MVP 크롤러 (수집 → review_data · JSON · 31곳 · 배포)

> 동작 명세 = SPEC. 여기는 작업 단위. Phase 1이 끝나면 **매일 자동으로 31곳을 긁어 구조화 → `review_data`(PENDING)까지** 쌓인다.

### 1-1. 수집 (fetch → source_data)  ← 플로우 앞단
- [ ] `YTUS` 어댑터 — 목록→상세→`raw_text`+이미지 URL 확보
- [ ] `source_data` 적재 — 불변·`UNIQUE(source_key, external_id)`(원장)·이미 본 글 skip
- [ ] 티어별 fetch 골격(정적) + 인코딩(EUC-KR) 처리

### 1-2. 구조화 (source_data → review_data)  ← ★ 1소스 전 구간 관통(뼈대 완성)
- [ ] Gemini 구조화 호출 + **출력 JSON 계약**(필드·타입) + 한글→enum 매핑(position·region 등)
- [ ] 게이트1(개교회 채용? `true`/`false`/`uncertain`) · 게이트2(`job_kind` MINISTRY/GENERAL·`role`)
- [ ] 교단·`contact`·`confidence` 산출 → `review_data`(PENDING) 적재
- [ ] 이미지 공고 = 이미지 바이트를 Gemini에 함께(멀티모달 · 별도 OCR 없음)

### 1-3. 판정 견고화
- [ ] 교단 확정 — alias(**긴 표현 우선**)·명부 대조(가능 시)·AI 추정(`ai_guess`)·`미상`
- [ ] `dedup_key`(교차게시 병합 후보) · 이단 플래그(`config/heresy-ref.json`)
- [ ] **재구조화 pass** — `review_data` 없는 `source_data` 재처리(구조화 실패 공고 유실 방지)

### 1-4. 소스 확장 (1 → 31곳)
- [ ] **유형 다른 2~3개로 어댑터 틀 먼저 검증** — `PUTS`(EUC-KR)·`CSU`/`HANIL`(JSON 엔드포인트)
- [ ] CMS 계열별 어댑터 — 그누보드·대학 `.do`·`/Board/Index`·webchon 등(정적 일괄)
- [ ] JSON 엔드포인트(`CSU` getBoardContent·`HANIL` article_list.ajax)
- [ ] 헤드리스(`MOKWON`·`ACTS`) — 최후수단
- [ ] 각 어댑터 **HTML fixture + 파서 테스트**(사이트 변동 대비)

### 1-5. 오케스트레이션·운영
- [ ] `run.ts` — 소스 간 병렬·소스 내 순차·**에러 격리**(한 소스 실패해도 계속)
- [ ] `crawl_run`(시작 INSERT → 종료 UPDATE) · `source_health`(UPSERT)
- [ ] rate-limit·timeout·지수 백오프·robots.txt·UA 정책
- [ ] 백필 CLI(`mode=BACKFILL`·최근 3개월·로컬) + 데일리(증분)
- [ ] "0건·급감" 경보(`source_health` baseline)

### 1-6. DB 전환 (JSON → Supabase)
> 스키마가 여기서 굳음(그 전까진 JSON).
- [ ] 마이그레이션 — `source_data`·`review_data`·`source_health`·`crawl_run`(+ RLS 운영자 전용)
- [ ] Store를 Supabase 구현으로 스왑(파이프라인 코드 불변) + 스모크 테스트

### 1-7. 배포 (GitHub Actions)
- [ ] `.github/workflows/crawl.yml` — 매일 **07:00 KST** cron(DAILY) + `workflow_dispatch`(수동 재실행)
- [ ] GH Secrets — Vertex·Supabase service key
- [ ] 첫 자동 실행 검증 + crawl_run/source_health 확인

> **Phase 1 완료 기준**: GitHub Actions가 매일 31곳을 긁어 새 공고를 구조화 → `review_data`(PENDING)까지 쌓고, `crawl_run`·`source_health`로 관측된다.

## Phase 2: min_job 게재 연동 + 소스 확장 (게이트)

> 게재 브릿지·검수 UI는 **min_job 측 작업**(min_job ROADMAP 1-10). 크롤러는 `review_data` 제공까지가 임계경로.
- [ ] (min_job) `review_data` → `churches`/`jobs` **승격 UI** + 크롤 대시보드
- [ ] (min_job) 스키마 변경 — `job_kind`·`role`·`contact`·`position` nullable·`KIJANG` 제거
- [ ] **로그인 티어**(KMC·AGK·기독신문·CTS) — 인증 크롤(계정=운영자 제공)
- [ ] **커버리지 확장**(상업 CROSS: 청빙넷·cjob·갓피플) — 정책 재검토 후

## Phase 3: 고도화 (SPEC §9)
- [ ] 수정/삭제 감지 — `content_hash` + 리비전(`source_data` 불변 유지)
- [ ] 교회 enrichment 자동화 — 교회명 → 기존 매칭 / 교단 교회검색 / 홈페이지 조회
- [ ] 노회 → 교단 매핑표 — 필요 시 총회 명부로 구축
- [ ] 커뮤니티(카페·밴드·페북) 수집

## 전제 · 리스크 (계속 인지)

| # | 항목 | 상태 / 비고 |
|---|---|---|
| **법률** | 데이터 수집 법적 경계 | ✅ **IT·지식재산 변호사 검토 완료(2026-07-28)** — 공개 공식 게시판 한정·영리 사이트 출처 배제(가드레일 #1·#3 재정의). ⚠️ **약관·개인정보처리방침 정식 검토는 별도**(min_job 진행) |
| **min_job 선행** | 공개 게재는 min_job 스키마 변경 전제 | `job_kind`·`role`·`contact`·`position` nullable·`KIJANG` 제거(min_job ROADMAP 1-10). **사역직만이면 현 스키마로도 게재 가능** |
| **Gemini 인증** | Vertex service-account 셋업 | Phase 0에서 **실호출로 먼저 검증**(과거 셋업 함정) |
| **사이트 변동** | 셀렉터 깨짐·URL 변경·차단 | fixture 테스트 + `source_health` 경보(하드=Actions 빨간불 / 소프트=0건·급감) |
| **저장 소유** | Supabase는 min_job 프로젝트 공용 | staging 4테이블 **정의·마이그레이션은 이 리포 소유**(SPEC §8) |
| **구조화 품질** | Gemini 판정 정확도 | 저물량이라 비용 미미. 품질은 `confidence`·운영자 검수로 방어 |
