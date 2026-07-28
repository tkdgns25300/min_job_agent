# SPEC.md — 크롤 파이프라인 명세

> **크롤러가 어떻게 동작하는가** — 스코프·파이프라인·크롤 정책·판정 로직·데이터 모델·배포. 크롤 대상 소스는 [`SOURCES.md`](./SOURCES.md), 출력 스키마·교단 정규화는 [`CONTRACT.md`](./CONTRACT.md), 시점 핸드오프는 [`SNAPSHOT.md`](./SNAPSHOT.md).
>
> **출력(공개) 스키마 정본 = `../min_job/docs/DATA.md`**(`churches`/`jobs`). 크롤러 전용 staging 테이블은 이 문서가 소유·명세.
>
> ⚠️ 이 문서가 파이프라인 동작의 **최신 정본**이다. CONTRACT의 일부(연락처 미추출·노회 매핑표·교단 매핑 원칙)는 여기서 갱신됨 — §8 "이 리포 문서 갱신" 참조.

---

## 0. 개요

크롤러는 **개교회(지역 교회)의 채용 공고**를 공식 게시판(SOURCES 31곳)에서 수집해, 구조화 초안으로 만들어 **리뷰 큐(`review_data`)**에 쌓는다. 운영자가 min_job admin에서 검수·승인하면 공개(`churches`/`jobs`)로 승격한다. 크롤러는 **절대 바로 공개하지 않는다**(사람 게이트).

- 자동: 수집 → 구조화 → 리뷰 큐 적재 → 운영 상태 기록.
- 사람: 검수 → 승격.

---

## 1. 스코프 — 무엇을 수집하나

**min_job = 개교회 채용 허브.** 판정은 **직함이 아니라 "누가·무엇을 뽑나"**로 한다.

### 게이트 1 — 개교회 채용인가?
그 공고가 **개교회가 자기 인력을 뽑는** 것이면 IN, 아니면 제외.

| | 예 | 처리 |
|---|---|---|
| **IN** | 개교회의 채용(사역직·일반직 불문) | 게이트 2로 |
| **OUT — 개교회 아님** | 방송사(CTS·CBS·극동) · 선교단체(CCC·YWAM) · 총회/노회 사무국 · 신학교·기독학교 교직원 · 기독 기업·출판·NGO·재단 | 제외(review_data 안 만듦) |
| **OUT — 비채용 글** | 공지 · 행사/세미나 · 모집설명회 · 부고 · 광고 · 기도제목 | 제외(review_data 안 만듦) |
| **경계(불확실)** | 병원/군/학교 원목(chaplain) 등 사역이나 개교회 여부 애매 | review_data(**낮은 confidence**) → 운영자 |

> 예: "[CTS기독교TV] 영상취재기자"는 채용 글이지만 **방송사**라서 게이트 1에서 제외. 경계(원목 등)는 버리지 않고 낮은 confidence로 운영자에게(§5.1).

### 게이트 2 — job_kind (개교회 채용만)
| job_kind | 대상 | 판정 힌트 |
|---|---|---|
| **MINISTRY (사역직)** | 담임/부목사·전도사·강도사·교육전도사 + **사역 간사**(교육·청년·유초등·찬양·선교·심방) | 말씀·목회·교육·전도·예배/찬양 사역 |
| **GENERAL (일반직)** | 방송·미디어·음향 · 행정·사무·회계 · 시설·관리·운전·주방 등 그 교회 운영 인력 | 운영·일반 업무 |

- **판정 원칙**: 직함(간사/집사/직원)이 아니라 **하는 일**. 같은 "간사"라도 교육간사=MINISTRY, 사무간사=GENERAL.
- 애매(사역+행정 겸직·반주자 등) → 버리지 말고 **낮은 confidence로 review_data → 운영자**.
- MVP 노출: min_job 기본뷰 = 사역직, 일반직은 필터(→ min_job 스키마 변경 pending §8).

### 제외 대상의 처리
게이트 1에서 **명백히 탈락**(개교회 아님·비채용)한 공고도 **`source_data`엔 raw로 저장**(원장 유지 → 재수집 방지). **`review_data`만 안 만든다.** (경계/불확실은 review_data로 감 — 위 표.)

---

## 2. 파이프라인 (①~⑥)

```
[입력] GitHub: 소스 config(31) + config/heresy-ref.json   ·   GH Secrets: Vertex·Supabase 키
   │
   ▼ ① 크롤 실행 시작 (GitHub Actions 매일 07:00 KST / 로컬 백필)
      crawl_run INSERT(started_at·mode) → run_id 확보
      소스 간 병렬 · 소스 내 순차(rate limit)
   │
   ▼ ② 목록 fetch → 글 식별자(external_id) 추출 → source_data에 없는 것만 상세 fetch
      상세에서 텍스트 + 이미지(본문·첨부) 확보
   │
   ▼ ③ source_data INSERT (raw · 불변 · run_id · UNIQUE(source_key, external_id))
   │
   ▼ ④ 구조화 (Gemini 2.5 Flash · 멀티모달: 텍스트 + 이미지 바이트 함께)
      게이트1 개교회 채용? (명백 기관·비채용 → review_data 생성 안 함)
      게이트2 job_kind · 교단 판정 · contact 추출 · 이단 스크리닝
   │
   ▼ ⑤ review_data INSERT (PENDING) + source_health UPSERT + crawl_run UPDATE(finished_at·집계)
──────────────────  여기까지 크롤러(자동)  ──────────────────
   ▼ ⑥ 운영자 검수 (min_job admin) → 승인/수정 → churches/jobs 승격(공개)
```

- ①~⑤ 자동, ⑥ 사람. 크롤러 write = `source_data`·`review_data`·`source_health`·`crawl_run`. 크롤러는 `churches`/`jobs`를 직접 안 건드린다.
- **crawl_run은 실행 시작(①)에 INSERT**(started_at·mode, run_id 확보) → source_data/review_data가 이 run_id를 FK로 참조 → **종료(⑤)에 UPDATE**(finished_at·sources_ok/failed·new_count·error_detail).

---

## 3. 크롤 정책 — "안전하되 빠르게"

- **동시성**: **소스 간 병렬 + 소스 내 순차.** 한 사이트를 몰아치지 않되(순차+요청 간 지연) 31곳은 동시에 → 예의 + 속도.
- **요청**: per-request **timeout + 재시도**(지수 백오프). 소스별 rate limit(요청 간 지연).
- **robots.txt**: 명시적 `Disallow`면 해당 경로 skip.
- **User-Agent**: 봇차단 사이트만 브라우저 UA 위장(SOURCES §6), 그 외는 정직 UA.
- **인코딩**: EUC-KR 등 자동 감지(구형 ASP/PHP 다수).
- **이미지 공고**: **이미지/텍스트 구분 로직 없음.** 상세에서 텍스트 + 이미지 URL을 확보하고, **구조화 직전 이미지 URL에서 바이트를 fetch**해 Gemini 멀티모달에 텍스트와 함께 전달 → Gemini가 이미지까지 읽어 필드를 뽑는다. **별도 OCR 파이프라인 없음.** 이미지 바이트 fetch 실패 시 raw_text만으로 진행하고 `confidence=low`로 운영자에게(공고가 이미지에만 있으면 본문이 얇을 수 있음).
- **에러 격리**: 한 소스 실패해도 나머지 계속(어댑터별 try/catch → crawl_run.error_detail 기록).

---

## 4. 증분 · 중복 · 백필

- **증분 키 = `external_id` + `source_key`.** `external_id` = **어댑터가 정하는, 그 소스 내 유일한 글 식별자** — 보통 URL의 글번호, 단 REST/AJAX 소스(CSU·HANIL 등)는 JSON/REST의 content id. **유일성 보장은 어댑터 책임**(§10). 목록 앞 페이지를 훑어 **`source_data`에 없는 식별자만** 상세 수집.
  - ⚠️ "처음 본(이미 아는) 글에서 멈추기" **금지** — 고정공지·끌어올림 때문에 아래 새 글을 놓친다. 페이지를 훑고 **unseen만** 가져온다.
  - 최종 안전망: DB `UNIQUE(source_key, external_id)` + `INSERT ... ON CONFLICT DO NOTHING`.
- **끌어올림(bump)**: 같은 글이 위로 와도 external_id 동일 → 이미 있음 → skip(중복 없음). 삭제 후 새 식별자로 재등록은 **새 글 = 재공고**(보존).
- **백필(로컬 수동 1회)**: `mode=BACKFILL`, **게시일 최근 3개월**까지. 컷오프는 **목록 단계의 게시일**(`listPostings`가 반환하는 날짜)로 판정(구조화 전이라 posted_at 산출 이전 — 목록 날짜 사용). 목록에 날짜가 없는 소스는 **최근 N페이지**로 폴백. 이후 데일리가 이어감.
- **데일리(GitHub Actions)**: `mode=DAILY`, 증분만.
- **수정 감지 없음(MVP)**: 한 번 수집한 스냅샷 사용. 재게시/삭제/수정 추적은 Phase 후반(§9).
- **재공고 보존**: 같은 교회·자리의 다른 시점 공고는 **절대 합치지 않는다**(min_job 재공고 추적 차별점). dedup은 "같은 글의 중복 수집"까지만.
- **교차게시 dedup**: 같은 공고가 여러 게시판에 올라올 수 있음 → `dedup_key = 정규화(교회명 + 직분 + 사례비 + 연락처|마감일)`로 **병합 후보 표시**(운영자 검수 단계, 자동 병합 아님).

---

## 5. 판정 로직 (④ 구조화 = Gemini 2.5 Flash)

한 공고의 raw(텍스트 + 이미지 바이트)를 Gemini에 넣어 아래를 한 번에 산출한다. **AI는 "신호 추출 + 구조화"**를 하고, 최종 확정은 규칙·운영자가 한다. 각 레코드에 **`confidence` ∈ {high, medium, low}**(구조화 전반 신뢰도)를 붙이고, **low는 운영자 우선검토**로 라우팅한다.

### 5.1 게이트 1 — `is_church_recruitment` (3값)
| 값 | 의미 | 처리 |
|---|---|---|
| `true` | 명백한 개교회 채용 | 게이트 2로 |
| `false` | 명백한 기관 채용(방송사·선교단체·학교 등) 또는 비채용 글 | **review_data 생성 안 함**(source_data엔 raw 남김) |
| `uncertain` | 개교회 여부 애매(원목 등 경계) | **review_data 생성(`confidence=low`)** → 운영자 |

→ 경계 케이스가 드롭되지 않고 운영자에게 가도록 3값으로 둔다.

### 5.2 게이트 2 — `job_kind` + `role`
- `job_kind` = `MINISTRY` | `GENERAL`.
- MINISTRY → min_job `position`(담임/부목사·전도사·강도사·ETC=간사류) + `department`.
- GENERAL → `role` = 방송·미디어·음향 · 행정·사무·회계 · 시설·관리·운전·주방 · 기타 (§1 GENERAL 예시와 동일 범주, 못 맞추면 `기타`). role은 통제 목록이 아니라 대략 분류로, 확정은 운영자.

### 5.3 교단 (공고에서 확정 — 게시판 default는 힌트만)
우선순위:
1. **교단 직접 명시**("예장합동", "기독교대한성결교회" 등) → alias 맵(CONTRACT §2c)으로 확정 · `denomination_source=stated`.
2. **교회 명부/홈페이지 대조**(가능 시) → `denomination_source=registry`.
3. **AI 추정**(공고의 노회명·교회명·홈페이지를 근거로 Gemini가 추정) → `denomination_source=ai_guess` (**교단 신뢰도 낮음 표시** — 레코드 confidence와 별개로 이 필드가 "확정 아님"을 나타냄).
4. 근거 없음 → **`denomination=미상`** · `denomination_source=unknown`.

- 출력: `denomination` · `denomination_source`(stated/registry/ai_guess/unknown) · `denomination_evidence`(원문 근거).
- **`미상`은 review_data 임시값**이다 — 승격 전 운영자가 반드시 **9대형+ETC 10키 중 하나로 해소**한다(9대형 화이트리스트 밖=`ETC`, 근거 전무해서 못 정하면 `ETC`+플래그). 공개 `jobs.denomination`엔 미상이 나가지 않는다.
- `미상`(근거 전무) vs `ETC`(식별됐으나 화이트리스트 밖)는 다르다 — review 단계 구분용.
- **노회는 저장하지 않고 매핑표도 만들지 않는다**(min_job 스키마에 노회 없음). 노회명은 위 3순위(AI 추정)의 근거로만 쓴다. → 표 유지비 0, 대신 검수 의존↑(운영자가 확정).

### 5.4 이단 스크리닝 — 플래그만
- 참고 목록 **`config/heresy-ref.json`**(교회명·교단명·키워드 + 근거 URL, 사람이 관리)과 공고의 교회명/교단을 대조.
- 매칭 시 **`review_data.heresy_flag=true` + `heresy_evidence`** → 운영자 우선검토.
- ⚠️ **자동 삭제·공개 이단 낙인 금지**(가드레일: 명예훼손·오판 회피). 최종 판단은 사람. 화이트리스트가 1차 방어, heresy-ref는 교회명 기반 추가망.

### 5.5 지원 연락처 (contact) — 공개
- 공고에 **지원용으로 명시된 연락처**(전화·이메일·지원 링크)를 `contact`로 추출 → 승격 시 공개(지원 경로 제공).
- 본문에 우연히 있는 **무관한 제3자 개인정보는 추출하지 않는다**(프라이버시 취지 유지).
- ⚠️ 이는 min_job 가드레일 #3(개인 담당자 연락처 노출 금지)의 **정제**다 — "교회가 지원받으려 공개한 연락처는 공개"로 갱신(min_job 스키마·정책 변경 pending §8).

### 5.6 나머지 필드
min_job `jobs` 미러(title·position·department·employment_type·qualification·housing_provided·stipend_*·work_days·requirements[]·preferred[]·required_docs[]·description·posted_at·deadline) + 교회 초안(church_name·region·city)을 raw에서 추출. `raw_text`(원문 전체)는 항상 보존.

---

## 6. 데이터 모델

크롤러 **staging 4테이블**(Supabase) + **참고 config 1**(GitHub JSON) + **승격 목적지 2테이블**(min_job DATA.md).

### ① `source_data` — 원자료 + 원장 (불변 · write-once · 누적)
| 컬럼 | 타입 | 비고 |
|---|---|---|
| `id` | uuid PK | |
| `source_key` | text | 게시판 id (대문자 enum 값: `YTUS`·`PUTS`… §10) |
| `external_id` | text | 소스 내 유일 글 식별자(§4) |
| `source_url` | text | 원문 링크 |
| `run_id` | uuid FK→crawl_run | ①에서 INSERT된 crawl_run 참조 |
| `fetched_at` | timestamptz | |
| `raw_text` | text | 확보 텍스트(이미지형은 얇을 수 있음 — 이미지는 raw_meta의 URL) |
| `raw_meta` | jsonb | 작성일·조회수·첨부/이미지 URL·게시판 원필드 |
| `content_hash` | text NULL | *Phase 후반(수정감지)용 — MVP 미채움(§9 리비전 방식)* |
| — | **UNIQUE(`source_key`,`external_id`)** | 원장(증분·중복 방지) |

### ② `review_data` — 구조화 초안 + 검수 (가변 · 누적)
| 그룹 | 컬럼 |
|---|---|
| 링크 | `id` PK · `source_data_id` FK · `run_id` FK |
| 분류(게이트) | `is_church_recruitment`(true/false/uncertain — false는 여기 안 옴) · `job_kind`(MINISTRY/GENERAL) · `role`(GENERAL용) |
| 공고(jobs 미러) | `title`·`position`·`department`·`employment_type`·`qualification`·`housing_provided`·`stipend_min`·`stipend_max`·`stipend_note`·`stipend_period`·`work_days`·`requirements[]`·`preferred[]`·`required_docs[]`·`description`·`posted_at`·`deadline` |
| 교회 초안 | `church_name`·`region`·`city` |
| 교단 | `denomination`(미상 가능·임시) · `denomination_source`(stated/registry/ai_guess/unknown) · `denomination_evidence` · `raw_denomination`(원표기) |
| 지원 | `contact` (지원 연락처) |
| 이단 | `heresy_flag`·`heresy_evidence` |
| 검수 메타 | `confidence`(high/medium/low) · `dedup_key` · `review_status`(PENDING/APPROVED/REJECTED) · `matched_church_id` FK→churches · `published_job_id` FK→jobs · `reviewed_by` · `reviewed_at` |

> 게이트1 `false`(개교회 아님·비채용)는 review_data를 만들지 않는다(§1·§5.1). `uncertain`은 confidence=low로 여기 온다. 게시판 default 교단은 `source_key`로 유도 가능(레지스트리)하므로 별도 hint 컬럼을 두지 않는다.

### ③ `source_health` — 게시판별 상태 (약 31행 · 매 실행 UPSERT)
`source_key` PK · `last_run_at` · `last_success_at` · `last_new_count` · `consecutive_failures` · `last_status`(OK/FAIL/ZERO) · `last_error`

### ④ `crawl_run` — 실행별 요약 (실행마다 1행 · 누적)
`id` PK · `started_at` · `finished_at` · `mode`(BACKFILL/DAILY) · `sources_ok` · `sources_failed` · `new_count` · `error_detail` jsonb(source_key→에러)
> **실행 시작(①)에 INSERT**(started_at·mode) → 종료(⑤)에 UPDATE(finished_at·집계). run_id는 시작 시 확보돼 source_data/review_data가 참조.

### 참고 config — `config/heresy-ref.json` (GitHub, 사람 관리)
`[{ match, type(church_name|denomination|keyword), source_url, note, added_at }]` — 민감 자료라 git 이력=감사 추적.

### 승격 목적지 — `churches` / `jobs` (min_job DATA.md)
운영자 승인 시 review_data → 승격. **요약 + `source_url` + `contact` · `source=OPERATOR` · `owner_id=NULL`.** 검수 메타·미상 교단은 넘기지 않음(교단은 승격 전 해소). ※ `job_kind`·`role`·`contact` 수용은 min_job 스키마 변경 필요(§8).

---

## 7. 배포 · 운영

- **배포 = GitHub Actions**(상시 서버 없음 · 무료 한도). 크롤러 코드 + `.github/workflows/crawl.yml`.
- **트리거**: **매일 07:00 KST**(`cron: "0 22 * * *"` UTC) · `mode=DAILY`. **백필은 로컬 수동**(`mode=BACKFILL`, 최근 3개월).
- **시크릿(GH Secrets)**: Vertex(Gemini) 키 · Supabase service key. 코드/DB에 노출 X.
- **실패 감지**:
  - **하드**(비2xx·타임아웃·000): GitHub Actions 실패(빨간불) + 이메일 알림.
  - **소프트**(200인데 신규 0건·급감): `source_health` baseline 비교 경보 + 내용 sanity(리스트 자리에 로그인폼/에러 감지).
- **대시보드**: min_job admin이 `crawl_run`(최근 실행) + `source_health`(게시판별) + `review_data` PENDING 카운트를 읽어 "오늘 몇 건 추가/실패·언제 긁었나" 표시.

---

## 8. min_job 연동 · 스키마 거버넌스

- **staging 4테이블(`source_data`·`review_data`·`source_health`·`crawl_run`)은 이 리포가 소유·마이그레이션.** 물리적으로 min_job Supabase 프로젝트에 함께 두되(검수·승격 단순), 정의·변경은 이 리포.
- **`churches`/`jobs`는 min_job `DATA.md`가 정본.** 크롤러는 그 모양에 맞춰 승격만.

### ★ min_job 스키마·정책 변경 (pending — 별도 작업)
1. `jobs`에 **`job_kind`(MINISTRY/GENERAL)** + 일반직 **`role`** 추가 + 목록 UI 필터(기본뷰=사역직).
2. `jobs`에 **`contact`(지원 연락처)** 추가 + **가드레일 #3 갱신**("지원용 공개 연락처는 공개").
3. `constants/domain.ts`에서 **`KIJANG` 제거**(11→10키 = 9대형+ETC).

### ★ 이 리포 문서 갱신 (pending — SPEC이 최신 정본, 아래는 뒤따라 맞춰야)
4. **`source_key` 대문자 통일**: SPEC은 대문자(YTUS)인데 CONTRACT §4·SOURCES §7·SNAPSHOT §9.4·데모는 소문자(ytus) → 정본을 대문자로 통일하거나 어댑터에서 대문자 정규화 저장 규칙 명문화.
5. **CONTRACT.md 갱신**: §3 "연락처 미추출"→"지원 연락처 추출·공개" · §2b "노회 매핑표"→"폐기(교단만)" · §2 `denomination_source` enum(`nohoe` 제거·`ai_guess` 추가) 및 "교단 매핑은 LLM 금지" 원칙 완화(근거 없을 때 AI 추정 허용, ai_guess 표시).
6. **SOURCES.md 갱신**: `pckworld`·`koreabaptist`의 "OCR" 기술요건 표기 폐기 → "Gemini 멀티모달로 대체".

---

## 9. 범위 밖 / Phase 후반

- **마감/삭제 능동추적 없음(MVP)**: 크롤러는 `deadline`만 추출. 마감은 min_job이 처리(deadline 경과 시 CLOSED + deadline 없는 오래된 공고는 "게시일+N개월 자동 만료" 운영규칙).
- **수정 감지**: `content_hash` + 재방문 → Phase 후반. ⚠️ source_data가 write-once 불변이므로, 수정감지 도입 시 **기존 행 갱신이 아니라 리비전 행을 새로 INSERT**(불변 유지)하거나 별도 revision 테이블로 분리한다(전제 명시).
- **교회 보강(enrichment) 자동화**: 교회명 → 기존 churches 매칭 / 교단 교회검색 / 홈페이지 조회. MVP는 공고에서 뽑히는 것만, 나머지는 운영자 수기.
- **노회→교단 매핑표**: 필요 시 각 총회 명부로 구축(현재는 AI 추정+검수).
- **로그인 티어**(KMC·AGK·기독신문·CTS): 변호사 게이트 통과 후 인증 크롤(계정=운영자 제공).
- **상업 CROSS**(청빙넷·cjob·갓피플): "공식만" 정책 · 법적 검토 후 재결정.
- **커뮤니티**(카페·밴드·페북): Phase 후반.

---

## 10. 어댑터 인터페이스 (구현 계약)

- **`SourceKey`** = 대문자 union(어댑터 레지스트리 파생). DB `source_key`도 이 대문자 값을 저장. (⚠️ 타 문서 소문자 표기와 통일 필요 §8-4)
- **`SourceAdapter`** (1 소스 = 1 어댑터):
  - `source_key: SourceKey` · `board_name` · `denomination_hint`(참고 — 확정 아님, §5.3에서 힌트로만)
  - `listPostings(opts) → PostingRef[]` — 목록에서 **external_id**(소스 내 유일 식별자·유일성 어댑터 책임) · url · 제목 · **게시일**(백필 컷오프용).
  - `fetchPosting(ref) → RawPosting` — 상세에서 raw_text + 이미지 URL + 메타.
- 공통 로직(fetch·인코딩·샘플 폴백·본문 추출·이미지 바이트 fetch)은 base/common으로. 신규 소스 = 어댑터 1파일 + 레지스트리 등록.
- fetch 계층이 UA·SSL·EUC-KR·rate limit·robots를 흡수(어댑터는 파싱만). `crawl_mode(A/B)` 개념은 폐기 — 교단은 항상 공고에서 판정(§5.3)하므로 어댑터는 `denomination_hint`(참고)만 제공한다.
