# min_job_agent — 작업 로드맵

> 실행 명령은 [`RUNBOOK.md`](./RUNBOOK.md), 파이프라인 명세는 [`SPEC.md`](./SPEC.md), 소스 카탈로그는 [`SOURCES.md`](./SOURCES.md), 출력 계약·교단은 [`CONTRACT.md`](./CONTRACT.md), 시점 핸드오프는 [`SNAPSHOT.md`](./SNAPSHOT.md).
> 브랜치: `prod`(배포) / `dev`(작업), dev→prod **ff-only**. commit·push·merge는 사용자 명시 요청 시에만.
>
> **스택 = Python 3.12+**(2026-07-29 변경 · 이전 TS/Node 결정 철회 — 근거는 CLAUDE.md Stack). **저장은 JSON 먼저 → 스키마 굳으면 Supabase 스왑**(1-6). 크롤러는 `review_data`까지만 만들고, **공개 게재(`churches`/`jobs`)는 min_job 측**(min_job ROADMAP 1-10).
>
> 아키텍처·레이어 책임·가드레일·컨벤션은 [`../CLAUDE.md`](../CLAUDE.md).
>
> **작업 원칙**: **걷는 뼈대(1소스 전 구간 관통) 먼저 → 어댑터 대량 확장은 뒤.** fetch→구조화→review_data 계약이 실제로 연결되는지 **31곳 만들기 전에** 검증한다.

## Phase 0: 뼈대 (골격)  ✅ 착수 완료(2026-07-28)

> "빈 파이프" — 코드 골격 + 저장 seam + 외부 연결(Gemini) 검증. 로직은 Phase 1.
>
> ⚠️ 이 절은 **TypeScript로 수행한 이력**이다(파일 경로는 이미 삭제됨 — 0-1c). 같은 내용을 Python으로 다시 만든 기록이 아래 0-1이다.
- [x] 부트스트랩 — `package.json`·`tsconfig`(strict·bundler)·`.env.example`·`src/` 구조 (Prettier 미도입)
- [x] **Store seam + JSON 구현** — `src/store/{types,json-store}.ts`(파이프라인이 저장소 모름 → Supabase 스왑용)
- [x] `types/domain.ts` — min_job enum 미러(교단 10키·region·position…) + 크롤러 enum(job_kind·denomination_source 등)
- [x] **Gemini(Vertex) 인증 실호출 성공**(운영자 확인 2026-07-29) — 래퍼 `src/lib/gemini.ts`·스모크 `check-gemini`
- [x] 소스 레지스트리 — `src/sources/{types,registry}.ts`에 **31곳 전부**(라이브 2차 검증: tier·encoding·flags·detailPattern·fetchNote) + `SourceAdapter` 인터페이스(SPEC §10)
- [x] **CLAUDE.md 작성 + 3렌즈 검수 반영**(설계 결함 `structured_at` 발견·SPEC 정정 포함)

### 0-1. Python 이식 (스택 변경 후속 · Phase 1 선행)
- [x] **0-1a** 프로젝트 골격 + `config/sources.json`(31곳 기계 변환) + `domain.py` + `cli list-sources` — 검수 2회 반영
- [x] **0-1b-1** `paths.py`·`clock.py`(UTC·date 단일 창구)·`models.py`(SPEC §6 4레코드)·`settings.py` — 검수 2회 + 검증 패스 반영
- [x] **flat 레이아웃 확정** — 패키지를 `minjob_ingest/`(리포 루트)로. `src/` 껍데기는 배포 라이브러리용이라 앱에는 불필요(CLAUDE.md Directory)
- [x] **0-1b-2** `store/{base,serde,json_store}.py` — Store 프로토콜 + JSON 구현(원자적 쓰기·행별 격리 읽기·write-once 강제·검수 상태 보존) — 검수 + mutation 테스트 20/20 반영
- [x] **0-1c** `lib/gemini.py`(SDK 내장 재시도·타임아웃 설정 사용) + `cli check-gemini` + **TS 잔존물 제거 완료**
- [x] 툴체인 — venv+pip(uv 미설치) · ruff(+DTZ·TID) · mypy strict · pytest

## Phase 1: MVP 크롤러 (수집 → review_data · JSON · 31곳 · 배포)

> 동작 명세 = SPEC. 여기는 작업 단위. Phase 1이 끝나면 **매일 자동으로 31곳을 긁어 구조화 → `review_data`(PENDING)까지** 쌓인다.

> **구현 주의(2026-07-29 코드 검수 반영 — 이식 시 반드시 반영)**
> - **`structured_at` 기록 필수**(게이트1 탈락 포함) — 없으면 제외 공고가 매 실행 Gemini로 재전송되는 비용 루프(SPEC §4).
> - **EUC-KR 소스는 `cp949`로 디코드** — 순정 EUC-KR 코덱은 확장 한글에서 예외 → 페이지 전체 유실.
> - **JSON 저장은 원자적**(임시파일→rename) + 병렬 시 직렬화/JSONL — 전체 배열 read-modify-write는 레코드 유실·파일 손상.
> - **Store에 읽기 포함** — 원장 bulk 조회 + `source_health` 조회(연속 실패 누적·마지막 성공 보존).
> - **Gemini 재시도를 손으로 만들지 않기** — SDK의 `HttpRetryOptions`(408·429·5xx + httpx 타임아웃/커넥션, 지수 백오프+지터)를 설정으로 쓴다. 직접 판정하면 예외 타입을 추측하게 되고 실제 SDK와 어긋난다. **빈 AI 응답은 실패 처리**, 요청 타임아웃(ms) 필수. (0-1c에서 적용 완료 — `lib/gemini.py`)
> - **실패를 조용히 넘기지 않기** — 알 수 없는 `run_id`·손상 파일은 예외로.

### 1-1. 수집 (fetch → source_data)  ← 플로우 앞단
> **작업 순서(2026-07-30 확정)**: 게시판 하나씩 **`--dry-run`으로 파싱·id 유일성 확인 → 통과분만 실제 수집(3개월) → 그 다음 `structure`(유료)**. 수집과 구조화를 다른 명령으로 나눈 이유 = 파싱이 틀린 채로 수백 건을 AI에 보내면 되돌릴 수 없다.
- [x] `fetch/client.py`·`fetch/robots.py` — **UA 31곳 동일(브라우저)+브라우저 헤더**·인코딩(cp949)·타임아웃 20s·재시도 3회·`Retry-After` 준수·소스별 간격 1.5s·robots `Crawl-delay` 준수(`Disallow`는 미준수)·세션 쿠키·본문 길이 하한. **모든 HTTP의 단일 창구** (36테스트 · mutation 21/21)
- [x] **YTUS fixture 확보**(`tests/fixtures/YTUS/` · 개인정보 마스킹 완료) — 실측 구조는 SNAPSHOT §10
- [ ] **fixture 저장 경로**(`collect --save-fixture`) — 가드레일 #7이 "테스트는 fixture로"를 요구하는데 fixture를 **만드는 수단이 없다**. 어댑터를 고칠 때마다 게시판을 다시 두드리게 되므로, 받아온 HTML을 `tests/fixtures/<KEY>/`에 저장해 이후 파싱 반복은 오프라인으로 한다
- [x] **어댑터 계층** `sources/adapters/{base,ytus}.py` — 순수 파싱(네트워크 없음) · `list_page_url`/`parse_list`/`parse_detail` · 공지 이중신호 · 실측 fixture 3종 · 25테스트 · mutation 15/15
- [x] **페이지 경계 중복 처리** — `collect`가 실행 내 스캔한 번호를 모아, 밀려 내려온 글을 **한 번만** 수집한다. 페이지 *안* 중복은 여전히 어댑터 에러(`as_listing`)이고, 페이지 *간* 중복은 정상 현상이라 에러가 아니다(SPEC §4 정정)
- [x] `source_data` 적재 — 불변·`UNIQUE(source_key, external_id)`(원장)·이미 본 글 skip(상세 요청 안 함)
- [x] **`collect` 명령 + `--dry-run`** — 어댑터 레지스트리 + 결정(순수 함수: 컷오프·페이지 종료·번호 충돌) + 실행 루프 + 리포트. 소스 단위 격리 · `--dry-run`은 목록 전체 + **상세 표본 1건**(목록만 보면 상세 파싱 미검증) · mutation 17/17
- [ ] (1-4) `PUTS` bd_name 필터 · `CSU`는 1110만 · **`HANSEI`는 `catId:artclNo` 복합키** — 각 어댑터를 만들 때 적용
- [x] **원장 조회 확장** — `SourceData.title`·`posted_on` 컬럼 + `seen_postings`가 `LedgerEntry`(제목·게시일)를 함께 반환 + `points_to_another_posting`(둘 다 다르면 소스 실패). 추가 요청 0건 · mutation 12/12
- [x] `--months N` 컷오프 = **목록의 게시일**(구조화 전이라 posted_at 없음 · 달 단위 말일 보정) · `--months 0`이면 날짜로 안 자름 · 페이지 상한은 `--pages`

### 1-2. 구조화 (source_data → review_data)  ← ★ 1소스 전 구간 관통(뼈대 완성)
- [ ] Gemini 구조화 호출 + **출력 JSON 계약**(필드·타입) + 한글→enum 매핑(position·region 등)
- [ ] 게이트1(개교회 채용? `YES`/`NO`/`UNCERTAIN`) · 게이트2(`job_kind` MINISTRY/GENERAL·`role`)
- [ ] 교단·`contact`·`confidence` 산출 → `review_data`(PENDING) 적재
- [ ] 이미지 공고 = 이미지 바이트를 Gemini에 함께(멀티모달 · 별도 OCR 없음)

### 1-3. 판정 견고화
- [ ] 교단 확정 — alias(**긴 표현 우선**)·명부 대조(가능 시)·AI 추정(`ai_guess`)·`UNKNOWN`
- [ ] `dedup_key`(교차게시 병합 후보) · 이단 플래그(`config/heresy-ref.json`)
- [ ] **재구조화 pass** — **`structured_at IS NULL`**인 `source_data` 재처리(+`structure_attempts` 상한). ⚠️ "review_data 없는 행" 기준 금지 — 게이트1 탈락과 실패가 구분되지 않아 비용 루프(SPEC §4)

### 1-4. 소스 확장 (1 → 31곳)
- [ ] **유형 다른 2~3개로 어댑터 틀 먼저 검증** — `PUTS`(EUC-KR)·`CSU`/`HANIL`(JSON 엔드포인트)
- [ ] CMS 계열별 어댑터 — 그누보드·대학 `.do`·`/Board/Index`·webchon 등(정적 일괄)
- [ ] JSON 엔드포인트(`CSU` getBoardContent·`HANIL` article_list.ajax)
- ~~헤드리스~~ — **불필요**(31곳 중 headless 0 · MOKWON·ACTS 모두 정적으로 확인). 새 소스가 JS 렌더면 그때 도입
- [ ] 각 어댑터 **HTML fixture + 파서 테스트**(사이트 변동 대비)

### 1-5. 오케스트레이션·운영
- [ ] `pipeline/run.py` — 소스 간 병렬·소스 내 순차·**에러 격리**(한 소스 실패해도 계속)
- [ ] `crawl_run`(시작 INSERT → 종료 UPDATE) · `source_health`(UPSERT)
- [ ] rate-limit·timeout·지수 백오프·robots.txt·UA 정책
- [ ] 백필 CLI(`mode=BACKFILL`·최근 3개월·로컬) + 데일리(증분)
- [ ] "0건·급감" 경보(`source_health` baseline)

### 1-6. DB 전환 (JSON → Supabase)
> 스키마가 여기서 굳음(그 전까진 JSON).
- [ ] 마이그레이션 — `source_data`·`review_data`·`source_health`·`crawl_run`(+ RLS 운영자 전용)
- [ ] Store를 Supabase 구현으로 스왑(파이프라인 코드 불변) + 스모크 테스트
- [ ] **운영자 전용 쓰기 경로 2개를 Store에 추가**(JSON 단계에선 파일 직접 편집으로 대체 중):
  ① opt-out·법적 삭제(write-once 예외 — SPEC §6 ①·가드레일 #4), ② 구조화 시도 횟수 리셋
  (`SourceData.with_attempts_reset` — `update_structure_state`는 횟수 감소를 거부한다).
  Supabase로 옮기면 운영자가 service-role SQL을 직접 쓰는 수밖에 없어지므로 여기서 같이 낸다.

### 1-7. 배포 (GitHub Actions) — ⚠️ **1-6 이후에만**
> ephemeral 러너 + JsonStore 조합은 매 실행 원장을 잃어 **전량 재크롤·재구조화·산출물 유실**을 만든다. `crawl.yml`은 **Supabase 전환(1-6) 완료 후** 작성한다(CLAUDE.md 순서 제약).
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
