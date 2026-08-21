# CLAUDE.md — min_job_agent

> **이 파일은 HOW** — 아키텍처·레이어 책임·코드 컨벤션. **운영자가 타이핑하는 명령은 [`docs/RUNBOOK.md`](./docs/RUNBOOK.md)**. 파이프라인 동작·판정 규칙·스키마는 [`docs/SPEC.md`](./docs/SPEC.md), 크롤 대상 소스는 [`docs/SOURCES.md`](./docs/SOURCES.md), 출력 계약·교단 정규화는 [`docs/CONTRACT.md`](./docs/CONTRACT.md), 작업 단위는 [`docs/ROADMAP.md`](./docs/ROADMAP.md), 시점 핸드오프는 [`docs/SNAPSHOT.md`](./docs/SNAPSHOT.md).
>
> **문서 책임 분리** — 같은 사실을 두 곳에 쓰지 않는다. **여기는 "코드를 어떻게 쓰는가"만** 담고, 정책·판정 규칙·소스 목록·스키마 필드는 위 문서를 **가리킨다**(복사하지 않는다).
>
> ⚠️ **정본 순서**: 게시판 전송 사실(tier·encoding·flags·상세URL) = **`config/sources.json`**(라이브 검증값) > SOURCES §6 요약. 그 외 파이프라인·판정·스키마 = SPEC. **코드와 문서가 다르면 전송은 코드가, 정책은 SPEC이 이긴다.**

## Project

형제 리포 `../min_job`(개교회 채용 플랫폼)을 위한 **공고 수집 크롤러**. 공개 청빙 게시판(확정 목록 = SOURCES §7)에서 공고를 수집 → AI로 구조화 → **리뷰 큐(`review_data`)** 에 적재한다. 운영자가 min_job admin에서 검수·승격하면 공개된다. min_job 파이프라인에서 **fetch 한 단계만 자동화하는 델타**다.

**별도 리포인 이유**: min_job은 **in-repo 크롤러 코드를 두지 않는다**(min_job `CLAUDE.md` Ingest 레이어 규칙). 자동 수집 자체는 min_job 쪽에서 허용된 것이며(공개 공식 게시판 한정·운영자 검수 전제·법률 검토 완료 2026-07-28), 그 구현체가 이 리포다.

**Stack**: Python 3.12+ · `httpx` + `beautifulsoup4`/`lxml` · `google-genai`(Vertex AI Gemini) · `Pillow`(인쇄용 CMYK 포스터를 화면용으로 — Vertex가 4채널 JPEG를 거절한다) · JSON 파일 저장(Phase 1) → Supabase · GitHub Actions(스케줄)

> ⚠️ **스택 변경(2026-07-29)**: 원래 TypeScript/Node였다(SNAPSHOT §2 "되돌리지 말 것" 항목). **Python으로 교체됨** — 크롤 생태계 성숙도 + 운영자가 직접 실행. TS를 택한 명목("min_job 타입 공유")은 별도 리포·별도 프로세스라 실제로 성립하지 않았고, enum 정합은 코드 공유가 아니라 **CONTRACT §1 계약 + 드리프트 테스트**로 지킨다.
>
> ✅ **TS 뼈대 이식 완료(2026-07-29, 0-1c).** `src/*.ts`·`package.json`·`tsconfig.json`은 삭제했다 — 되짚어야 하면 git 이력을 본다. 재취득 불가 자산이던 31곳 검증값(특히 `fetch_note`)은 `config/sources.json`으로 **문자 그대로** 이관됐다.
>
> ⚠️ **`google-genai` SDK·Gemini 모델 ID는 학습 데이터와 다를 수 있다.** 구조화 코드 작성 전 공식 문서를 확인할 것. 모델 ID는 **env에서 읽고 하드코딩하지 않는다**(운영자가 최신 Flash로 교체함) — 기본은 **`VERTEX_MODEL`**, `--lite`일 때만 **`VERTEX_MODEL_LITE`**. ⚠️ `--lite`인데 후자가 비면 **비싼 모델로 대체하지 않고 멈춘다**(비용 사고 방지). 인증은 서비스계정 4개 env(`VERTEX_AI_PROJECT_ID`·`_LOCATION`·`_CLIENT_EMAIL`·`_PRIVATE_KEY`) — `.env.example` 참조.

## Architecture Overview

### 핵심 결정: 상시 서버 없는 배치 크롤러

하루 1회 짧게 도는 배치다. 상시 서버를 두지 않고 **러너에서 실행 후 종료**한다(고정비 0). 러너는 끝나면 사라지므로(ephemeral) **다음 실행이 이어받을 상태는 전부 원격 저장소에 있어야 한다**.

```
[운영자 로컬 CLI]  ─ 백필·수동 실행 (Phase 1 기본)
[GitHub Actions cron] ─ 매일 자동 (⚠️ Supabase 전환 후에만)
        │  config(소스 레지스트리) + env(Vertex·Supabase 키)
        ▼
[크롤러 프로세스]  fetch → 구조화 → 중복 판정 → 공개      ← 끝나면 소멸
        ▼
[저장소]  source_data → review_data ─┬─ APPROVED ──▶ jobs (크롤러가 INSERT)
                                     └─ PENDING  ──▶ min_job admin 검수 ──▶ 승인 → 다음 실행이 공개
```

> ⚠️ **순서 제약(필수)**: **JsonStore(로컬 파일) 단계에서 GitHub Actions를 붙이지 않는다.** ephemeral 러너에선 JSON 원장이 매 실행 사라져 → 31곳 전량 재크롤 + 전량 재구조화(비용) + 산출물 유실. `crawl.yml`은 **SupabaseStore 전환(ROADMAP 1-6) 이후**에 만든다. 그전까지 실행은 운영자 로컬.

### 파이프라인

단계 정의·판정 규칙은 **SPEC §2·§5가 정본**이다(여기서 다시 번호를 붙이지 않는다). 이 리포가 지켜야 할 아키텍처 사실만:

- 크롤러의 종착지는 **`jobs` 공개까지**다(운영자 결정 2026-08-18 · 경계는 SPEC §8). 확인할 것이 없는 초안은 크롤러가 승인하고 직접 공개한다 — 사람이 보는 것은 `PENDING`뿐이다.
- ⚠️ **`jobs`에서 건드리는 것은 "자기가 만들었고 아직 교회 것이 아닌" 공고뿐**이다: INSERT(SPEC §4.3)와 `posted_at` 갱신(§4.2b). 그 외 모든 행은 **읽기만**(중복 대조용 앵커 · §4.2). `churches`에는 쓰지 않는다.
- **검수(`PENDING`)는 min_job 쪽 책임**이며 이 리포 밖이다. 승인은 `review_status`만 바꾸고, 공개는 다음 실행이 한다.
- `crawl_run`은 **실행 시작에 INSERT**해 `run_id`를 얻고(하위 레코드가 참조) **종료에 UPDATE**한다.

### 3층 분리 (게시판이 30곳이어도 코드는 안 늘어난다)

게시판마다 크롤러를 만들지 않는다. 차이를 **가장 얇은 층에 몰아넣는다**:

| 층 | 무엇 | 게시판별 차이 |
|---|---|---|
| **config** (소스 레지스트리) | 어디를·어떻게 접속하나 | **대부분 여기** (데이터, 코드 아님) |
| **fetch** (`fetch/`) | 바이트 가져오기 — UA·디코드·타임아웃·재시도·rate limit·`Crawl-delay`·세션 | 없음(전 소스 공유 · UA도 동일) |
| **parse** (`sources/adapters/`) | 목록 행·상세 본문 추출 | **게시판 1곳 = 파일 1개**(구현 30곳 · 124~213줄) |

→ 신규 소스는 **config 한 칸 + 어댑터 파일 1개**다.

⚠️ **CMS 계열별로 묶지 않는다**(운영자 결정 2026-08-04). 그누보드·Konnect·eGov를 쓰는 곳이 몇 개씩 있지만 **공통 부모(계열 base 클래스)를 만들지 않는다** — 게시판은 각자 따로 바뀌는데 코드가 묶여 있으면 한 곳을 고치다 나머지를 깨고, 파싱을 이해하려면 파일 두 개를 오가야 한다. **게시판을 하나씩 독립으로 관리**하는 것이 유지보수에 유리하다.

중복은 상속이 아니라 **`base.py`의 함수**로 줄인다(도구상자 · 상속 아님). 어댑터 파일이 200줄을 넘으면 일반화할 것이 섞인 신호다 — 그때 함수를 `base.py`로 올린다. 통일감은 사람의 규율이 아니라 **적합성 테스트**(등록된 모든 어댑터를 순회 검사)가 지킨다.

### 저장 seam (JSON → Supabase 무통증 전환)

파이프라인은 저장소 구현을 모른다. `Store` 프로토콜만 호출하고 구현을 갈아끼운다.

```
pipeline → Store(프로토콜) ─┬─ JsonStore(로컬 파일 · 기본)
                            └─ SupabaseStore → PostgrestClient(전송)

공개 경로 → PublishTarget(별도 프로토콜) → SupabaseJobs   ← JSON 저장소에는 없다
```

- **레코드 필드명 = SPEC §6 컬럼명(snake_case)과 동일**하게 둔다 → 전환이 "그대로 INSERT"가 된다. *(현 TS 뼈대는 camelCase + 스토어에서 매핑하는 구조다 — **Python 이식 때 snake_case로 정리**해 매핑 계층을 없앤다.)*
- ✅ **마이그레이션 작성됨**(2026-08-20 · `supabase/migrations/`). **스키마 정본은 SPEC §6이고 SQL은 그 구현**이다 — 컬럼 집합은 `models.py`, enum 허용값은 `domain.py`와 대조해 맞춘다(허용값 정본은 CONTRACT §1 · DB CHECK는 2차 방어선).
- ⚠️ **이 리포에서 `supabase db diff`를 쓰지 않는다.** min_job과 Supabase 프로젝트를 공유하는데 diff는 상대 리포의 마이그레이션을 모른다 → min_job 7테이블을 "없어야 할 것"으로 보고 `DROP TABLE jobs`를 만든다.

## Directory

**flat 레이아웃** — 패키지가 리포 루트에 있고 `src/` 껍데기를 두지 않는다. `src` 레이아웃은 PyPI에 올리는 **배포 라이브러리** 권장 배치이고, 우리는 **리포에서 그대로 실행하는 앱**이다(운영자가 CLI 실행 · `config/`·`data/`가 코드 옆). 앱에 `src/`를 끼우면 `config/` 경로 계산만 한 단계 깊어지고 얻는 게 없다.

```
minjob_ingest/                 ★ 패키지 (= import 이름)
├── cli.py                    진입점 (운영자가 실행하는 창구)
├── domain.py                 enum — CONTRACT §1 계약 미러 + 크롤러 enum
├── models.py                 레코드 dataclass — SPEC §6 4테이블
├── clock.py                  KST·ISO8601·date 생성/직렬화 단일 창구
├── paths.py                  리포 기준 경로 (한 곳에서만 계산)
├── settings.py               env 로딩 1곳 (import 시점 캡처 금지)
├── sources/{registry.py, adapters/}   소스 레지스트리 · 게시판 1곳 = 파일 1개(30곳)
├── fetch/{client.py, robots.py}   전송 단일 창구 · robots 준수
├── store/                   저장 seam — 파이프라인은 구현을 모른다
│   ├── base.py             Store·PublishTarget 프로토콜 + DTO
│   ├── serde.py            레코드 ↔ 행 변환 (컬럼 집합 엄격 대조)
│   ├── guards.py           두 구현이 공유하는 순수 판정 (write-once·단조 증가·중복 반영)
│   ├── factory.py          저장소를 여는 단 한 곳 (`MINJOB_STORE`)
│   ├── json_store.py       로컬 파일 구현 (Phase 1 기본)
│   ├── postgrest.py        PostgREST 전송 (⚠️ httpx 예외 1곳 · 페이지네이션·개수 검산)
│   ├── supabase_store.py   Store 12개의 원격 구현
│   └── jobs_gateway.py     jobs 접근 — 앵커·INSERT·posted_at (공개 테이블)
│                                              (+ supabase_store.py 예정 = 1-6)
├── lib/gemini.py             Vertex 클라이언트 (재시도는 SDK 설정)
└── pipeline/                collect·structure·extraction(프롬프트·스키마)·normalize(변환)·
                            verify(원문 대조)·denomination(교단 확정)·heresy(이단 대조)·confidence(등급)·
                            dedup(같은 자리 묶기)·publish(jobs 공개·끌어올림)·
                            media(그림·PDF 바이트)·health·snapshot
config/
├── sources.json              ★ 소스 레지스트리 (전송 정본 · 라이브 검증값)
└── heresy-ref.json           이단 참고 목록 (**커밋 금지** — 실명 자료 · 사람이 관리)
supabase/migrations/           ★ staging 4테이블 스키마 (SPEC §6의 구현 · 적용은 운영자)
scripts/                       일회성 이관·정리 스크립트 (CLI 명령이 아니다)
tests/{fixtures/ ← gitignored, test_*.py}
data/                          로컬 저장소 (gitignored)
├── source_data.json · review_data.json · source_health.json · crawl_run.json   ★ 원장 4개
├── preview/                  `--out` 결과·로그 (지워도 된다)
└── backup/                   원장 백업
```

> ⚠️ 트리는 드리프트할 수 있으니 "계약"으로 신뢰하지 말 것 — 실제 파일이 정본이다.
>
> **커밋하지 않는 것**: `.venv/`·`__pycache__/`·`minjob_ingest.egg-info/`·`.mypy_cache/`·`.ruff_cache/`·`.pytest_cache/`·`data/` (자동생성물 — 지워도 도구가 다시 만든다) + **`tests/fixtures/`**(게시판 HTML 원본 · `snapshot`으로 다시 받는다). 전부 `.gitignore`에 있다.
>

## Commands

**셋업** (`uv`는 이 환경에 없어 표준 `venv`+`pip`를 쓴다)
```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
```

**커밋 전 게이트 — 4개 전부 통과해야 한다**
```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy                 # strict + disallow_any_explicit
.venv/bin/pytest -q            # fixture만 사용 · 네트워크 금지
```

**실행**
```bash
.venv/bin/minjob-ingest list-sources [KEY]   # 등록 소스 확인 (네트워크 없음)
.venv/bin/minjob-ingest check-gemini         # Vertex 인증·연결 (유료 API 실호출 1회)
.venv/bin/minjob-ingest snapshot [--source K]  # fixture용 HTML 확보 (게시판 요청)
.venv/bin/minjob-ingest collect --months 3     # 공고 수집 (게시판 요청 · 무료)
.venv/bin/minjob-ingest structure --limit 20   # AI 구조화 (⚠️ 유료 · 범위 필수 · `--lite`로 값싼 모델)
.venv/bin/minjob-ingest structure --all --workers 8   # 전량 (게시판 8곳씩 동시 · 게시판 안은 순차)
.venv/bin/minjob-ingest dedup                  # 같은 자리 묶기 (무료 · structure 뒤 자동 실행됨)
.venv/bin/minjob-ingest publish                # jobs 에 공개 + 끌어올림 (무료 · Supabase 전용)
```
⚠️ **`structure`는 `--limit N` 또는 `--all`이 없으면 실행을 거부한다** — 유료 호출이 옵션 없이 전량으로 도는 경로를 두지 않는다. 확인용은 `--dry-run`(호출은 하되 저장 안 함).
아직 없는 명령(Phase 1): `daily`·`backfill`·`status`.

**일회성 스크립트**(CLI 명령이 아니다 · `scripts/`)
```bash
.venv/bin/python scripts/reset_structure.py --all --write   # 판정을 지워 재구조화 가능하게
.venv/bin/python scripts/migrate_posted_on.py --write       # 옛 원장(version 1) → version 2
```
⚠️ **되돌리기 스크립트 없이 전량 저장하지 않는다** — `structured_at`은 앞으로만 가서, 저장 뒤에 프롬프트 문제를 발견하면 고친 것을 적용할 방법이 없다.

> ⚠️ **CLI 명령을 추가·변경하면 `docs/RUNBOOK.md`를 같이 고친다.** 운영자는 그 파일만 보고 실행한다 — 여기에만 적으면 전달되지 않는다.


## Layer Responsibilities

### Registry (`sources/registry.py` + `config/sources.json`)
- **"어떻게 접속하나"를 데이터로** 보유. 코드에 URL·셀렉터·페이지 파라미터를 하드코딩하지 않는다.
- 현재 필드(= `minjob_ingest/sources/registry.py`): `key`(대문자) · `board_name` · `denomination_hint`(참고, 확정 아님·null 가능) · `enabled` · `fetch_tier` · `encoding` · `flags` · `list_url` · `detail_pattern`(`{id}` 치환) · `fetch_note`.
  - `flags`: `www_required` · `http_only` · `spoof_ua`(브라우저 UA 필수) · `insecure_tls` · `needs_session`(상세가 쿠키 요구) · `image_only`(본문이 이미지 — 빈 raw_text가 정상) · `soft_200`(잘못된 요청에도 200 → 본문으로 검증).
  - **2026-08-05 추가**: `list_has_dates`(목록에 게시일이 있나 · `false`면 컷오프를 만들지 않는다) · `list_page_limit`(날짜 없는 게시판의 범위 — CLI에 페이지 옵션이 없으므로 여기 적는다).
  - **추가하지 않기로 한 것**: `page_param`·`notice_marker`. 30곳을 실제로 만들어 보니 페이징이 쿼리·경로·POST 본문·행 오프셋으로 갈리고 공지 표시가 클래스·아이콘·번호칸 글자로 갈려서, **한 필드로 담으면 절반이 예외가 된다**. 어댑터 상단 상수로 두는 편이 읽기 쉽다.
- **로드 시 검증**(스타트업 assert + 테스트): key 대문자·유일 · `denomination_hint ∈ CONTRACT §1 ∪ {null}` · `flags` 키 화이트리스트 · `detail_pattern`이 있으면 `{id}` 포함. ⚠️ **예외 1곳**: `HANSEI`(비활성 — 게시판 소멸). ~~`CSU`~~는 2026-08-05에 해소됐다 — SPA라 URL이 없다고 봤지만 공유용 상세 URL(`?m1=page_ministry_detail&…&board_content_id={id}`)이 실제로 존재한다.
- **소스 추가/제외 = 이 JSON 편집.** 제외는 삭제가 아니라 `enabled: false` + `disabled_reason`(이력 보존·재활성 대비).
- `fetch_note`는 라이브 검증 메모(세션 필요·soft 실패·공지행·pagination)다. **지우거나 요약하지 말 것** — 재취득 불가.

### Adapter (`sources/adapters/*.py`) — **파싱만**
- 정확히 두 가지: `list_postings(opts) → [PostingRef]`(external_id·url·제목·게시일), `fetch_posting(ref) → RawPosting`(raw_text·이미지 URL·메타).
- **네트워크·인코딩·UA·재시도를 어댑터가 다루지 않는다** — fetch 층이 흡수. 어댑터는 받은 텍스트/JSON을 파싱만 한다.
- `external_id`의 **유일성은 어댑터 책임**(그 소스 내). 없으면 제목+게시일 해시.
- 고정공지(pinned)는 제외한다. ⚠️ **"이미 본 글을 만나면 중단" 금지** — 이유·규칙은 SPEC §4.
- **게시판 1곳 = 파일 1개.** 파일명 = `source_key` 소문자(`YTUS` → `ytus.py`). 여러 게시판을 한 파일에 담지 않고, **계열 base 클래스를 만들지 않는다**(위 3층 분리 참조). 공통은 `base.py` 함수로만 올린다.

### Fetch (`fetch/*.py`) — 전송 단일 창구
- **게시판 HTTP는 전부 여기를 지난다.** 어댑터·파이프라인이 직접 `httpx.get`을 부르지 않는다(ruff가 `httpx` import를 막는다). ⚠️ **예외는 저장소 전송 하나**(`store/postgrest.py`) — 우리 DB는 크롤 대상이 아니라서 UA 위장·`Crawl-delay`·소스별 간격이 붙으면 안 된다. 그 파일만 풀려 있고 `supabase_store.py`도 직접 HTTP를 만들 수 없다.
- 정책은 SPEC §3, 소스별 값은 config. 이 층이 단독 구현한다: **UA(31곳 전부 동일한 브라우저 UA)** + 브라우저 헤더 세트 · `encoding` **config 값 우선**(서버 헤더가 틀린 보드가 있음) · 타임아웃 · 재시도 · `Retry-After` 준수 · rate limit · robots `Crawl-delay` · 세션 쿠키(`needs_session`).
- **동시성은 SPEC §3이 정본**: **소스 간 병렬 · 소스 내 순차** — 한 호스트에는 항상 요청 1개만 흐른다(그래서 31곳 동시 실행이 예의에 어긋나지 않는다). 자원 보호용 상한이 필요하면 **정책이 아니라 실행 옵션**으로 둔다.
- **기본값(config 미지정 시)**: 요청 타임아웃 **20s** · 재시도 **3회**(지수 백오프+지터, 429·5xx·연결오류) · 같은 소스 요청 간격 **≥1.5s** · 목록 페이지 **안전 상한 100p**(⚠️ 범위를 정하는 값이 아니다 — 범위는 게시일 컷오프가 정하고, **CLI 옵션으로 노출하지 않는다**). 상수는 모듈 상단에 둔다.
- **성공을 상태코드만으로 판정하지 않는다** — 본문 내용으로 검증한다(일부 보드는 잘못된 요청에도 200을 준다 · `soft_200`). 전송 층은 **본문 길이 하한**으로 스텁 응답을 걸러내고, 내용 수준 판정은 어댑터가 한다.
- **`www_required`·`http_only`는 이 층에 코드가 없다** — 두 값은 이미 `list_url`에 반영돼 있고 레지스트리가 로드 시 강제하며, 상대 URL은 `urljoin`이 호스트·스킴을 물려준다. 아무 일도 안 하는 코드를 만들지 않는다.
- **UA는 31곳 전부 동일한 브라우저 UA**(운영자 결정 2026-08-04). 자체 UA(`minjob-ingest/...`)로는 게시판이 막는다 — **YTUS 실측: 자체 UA 403(25B) vs 브라우저 UA 200(99KB)**, 헤더를 다 갖춰도 UA 문자열만 보고 거부. 31곳 중 어디가 그런지 사전에 알 수 없고 시간이 지나며 바뀌므로 보드별 예외로 두지 않는다. → **`spoof_ua`는 코드 분기가 아니라 실측 기록**이다(`www_required`·`http_only`와 같은 취급). UA만 브라우저이고 `Accept`·`Accept-Language`가 비면 그 조합이 봇 신호라 **브라우저 헤더 세트를 함께** 보내고, JSON 티어(`CSU`·`HANIL`)는 jQuery AJAX 엔드포인트라 `X-Requested-With`까지 맞춘다.
- **robots**: **`Disallow`는 따르지 않고(`RESPECT_ROBOTS_DISALLOW=False`) `Crawl-delay`는 따른다(`RESPECT_CRAWL_DELAY=True`)** — 성격이 다르다. `Disallow`는 허락의 문제(운영자 판단 2026-07-30 · 문제없음 확인), `Crawl-delay`는 **"서버가 이 속도를 못 받는다"는 부하 신고**이므로 무시하면 IP 차단을 부른다. ⚠️ 표준 `RobotFileParser`는 **소수점 `Crawl-delay`를 조용히 버린다**(`"2.5".isdigit()`이 False) → 원문에서 직접 줍는 폴백이 있다. 값은 **늘리는 방향으로만** 적용한다.
- **`Retry-After` 준수**: 429·503이 알려준 대기 시간을 우리 백오프보다 우선한다(상한 60s). 이걸 무시하고 밀어붙이는 것이 차단의 흔한 경로다.
- **EUC-KR 선언 소스는 `cp949`로 디코드**한다(EUC-KR 순정 코덱은 확장 한글에서 예외 → 한 글자로 페이지 전체를 잃는다).

### Structure (`pipeline/structure.py`) — AI는 추출·추정만
- raw_text(+이미지·PDF 바이트)를 Gemini에 넣어 SPEC §5의 필드를 산출한다. **출력은 스키마로 강제**하고, enum 밖 값은 방어적으로 정규화한다.
- ⚠️ **모델에게 뽑기와 변환을 함께 시키지 않는다**(2026-08-12 실측). 맥락이 필요한 것만 모델이 하고, 맥락 없이 글자만 보면 되는 변환은 **`pipeline/normalize.py`** 가 한다.
  - **모델**: 게이트·`job_kind`·`position`·`department`·`employment_type`·`qualification`·요약. 직분은 "그 말이 뽑는 자리를 가리키나"를 판단해야 한다 — 키워드표로 하면 18건 중 8건이 틀리고 그중 6건이 **담임목사 오검출**(연락처의 담임목사 이름을 모집 직분으로 읽음)이다.
  - **코드**: 제목(게시판 제목에서 `(끌어올림)`류 머리표만 뗀다 — 모델은 20건 중 6건에서 끝의 마침표를 지웠다) · 사례비 환산과 월/연 판정(`pay_amount` → 만원 · 크기로 주기) · 마감 여부(게시판 상태 필드·제목) · 마감일. 같은 변환을 모델에 맡겼더니 **`연봉 3,200이상`이 Flash 3200 / Flash-Lite 267**로 갈렸다.
  - 얻는 것: 값이 실행마다 흔들리지 않고, **유료 호출 없이 테스트된다**.
  - ✅ **지역은 2026-08-16에 코드에서 모델로 옮겼다**(SPEC §5.5b · 유료 표본 450건으로 검증 · `place_of` 삭제). `location` → `region`+`city`는 게시판 지역 칸에서 100%였지만, `안동시`처럼 **도시만 적힌** 공고(11%)와 `전남광주통합특별시 북구 오치동`(그 표기 12건 중 **4건**이 광주인데 JEONNAM으로 오판)은 **글자가 아니라 지리 지식**이 필요하다. 표로 담으려면 동 이름 3,500줄이 된다 → 모델이 `region`·`city`·`address`를 내고 **코드는 검산만** 한다(`region`은 근거로 · `city`·`address`는 제 글자로). **실측(450건): 코드가 맞는데 모델이 틀린 것 0건 · 빈 광역 28→2건 · `region`은 실행 간 흔들림 0건**(`city`는 3건 갈렸고 9건은 시 대신 구를 답해 프롬프트를 조였다).
- ⚠️ **대신할 수 없는 칸은 검산한다**(`pipeline/verify.py`). 모델 답이 원문에 있는지 코드가 찾아보고, 없으면 **그 칸만 비운다**(공고는 버리지 않는다 — 운영자 기준이 "빈 칸 > 틀린 값").
  - **비운다**: 원문에서 한 조각을 그대로 옮기는 칸(`church_name`·`raw_denomination`·`contact_email`·`contact_tel`·`contact_link`·`address`) + 근거로 검산하는 대문자 값 넷 + 코드가 만든 값. 실측 42개 중 1개만 비웠고 그 1개가 진짜 오류였다(게시판 키가 교단 칸에 들어감).
  - **세기만 한다**: 프롬프트가 **조립을 시킨** 칸(`headcount`·`pay_note`·`benefit_note`·`role`·`work_days`·`start_timing`·`contact_post`·목록 5칸). 자리가 둘이면 값도 둘이라 한 칸에 이을 수밖에 없다(근무일 54건·부임시기 244건). ⚠️ 실측: 목록에서 23개가 어긋났는데 **지어낸 것은 0개**였다 — `각1통`을 `1통` 둘로 나눈 것처럼 모델이 옳았다. **시켜놓고 벌하지 않는다.** ⚠️ `housing_note`는 2026-08-16에 여기서 빠졌다 — `housing_provided`가 **있다고 해놓고 근거가 없으면** 둘을 함께 비운다(SPEC §5.5c).
  - **대문자 값 넷은 근거를 함께 받는다**(`position_evidence` 등 · 저장하지 않는다). ① 근거가 원문에 있나 ② 근거가 그 값을 뒷받침하나 ③ 직분이면 **모집한다는 말**이 있나 — ③이 없으면 `담임목사: 박은제`(실측 1,336건)가 담임 청빙이 된다.
  - ⚠️ **그림·PDF를 모델에 보낸 공고에서는 비우지 않는다**(실측 257건). 포스터가 원문이라 "본문에 없다"가 정상이고 지어낸 것과 구분할 수 없다 — 세어서 운영자에게 넘긴다.
- **AI에게 최종 확정을 위임하지 않는다**: 교단은 명시·명부는 규칙이 확정하고 근거가 없을 때만 AI 추정(`ai_guess` 표시 · 확정은 운영자), 이단 판단은 사람. 규칙은 SPEC §5.3·§5.4.
- ⚠️ **이단은 "확정된 일치"만 자동 거절한다**(운영자 결정 2026-08-19 · SPEC §5.4). 지역까지 맞았거나 단체·사람 이름이면 거절, **지역을 확인 못 한 개별 교회명은 검수로** 보낸다 — 목록 96%에 지역이 없어 이름만으로 거르면 동명이교회가 아무도 모르게 사라진다(실측: 예장합동 교회가 걸렸다). 어느 쪽이든 공개되지 않는다.
- ⚠️ **공개 게이트가 바뀌었다**(운영자 결정 2026-08-16 · ✅ 구현 2026-08-17 · SPEC §5.7). 검수는 **사람이 봐야 답이 나오는 것만** 남기고 나머지는 `review_status=APPROVED`로 만든다 — 포스터·그림 실패·승격 6칸이 빈 공고만 사람이 본다(실측 17% · 그중 96%가 그림 때문). 근거: 지어낸 값은 `verify`가 비우고, 변환은 코드가 하며, 이단·마감은 자동 거절된다. ✅ dedup이 `structure` 뒤에 자동으로 돈다(SPEC §4.1).
- **구조화 시도 후에는 반드시 `source_data.structured_at`을 기록한다** — 게이트1 탈락(review_data 미생성)도 포함. 이게 없으면 "제외된 공고"와 "구조화 실패"를 구분할 수 없어 **매 실행 재호출되는 비용 루프**가 된다(SPEC §4).
- 실패(429·파싱오류)는 삼키지 않는다 — `structured_at`을 남기지 않고 다음 run이 재구조화한다. 단 **재시도 상한**을 둬 영구 실패가 무한 재호출되지 않게 한다.
- **응답을 성공으로 오판하지 않는다**: 빈 텍스트·`finishReason` 이상은 실패로 처리한다(빈 문자열로 흘리지 말 것).

### Store (`store/*.py`) — 저장 단일 창구
- 파이프라인은 `Store` 프로토콜만 안다. **파일 경로·SQL·Supabase 클라이언트가 파이프라인에 새지 않는다.**
- `source_data`는 **write-once**(원문 증거). 일반 경로에서 갱신하지 않는다 — 수정 감지는 리비전 행 추가(Phase 3). **예외: 운영자 opt-out·법적 삭제 요청은 삭제/마스킹이 가능해야 한다**.
- 원장은 `source_data`의 `(source_key, external_id)` 유일성이 담당한다. 별도 원장 테이블을 만들지 않는다. **판정 기준은 이 두 컬럼뿐**이고, 함께 돌려주는 `title`·`posted_on`은 "그 번호가 다른 글로 바뀌었는지" 보는 경보다(둘 다 다르면 소스 실패 · SPEC §4).
- 프로토콜에 **읽기도 포함**해야 한다: 원장 조회(가능하면 **bulk** — 페이지당 1회), `source_health` 조회(연속 실패 누적·마지막 성공 보존에 필요), 미구조화 목록(상한 있는 배치).
- **JSON 구현 주의**: 쓰기는 **원자적**(임시파일 → rename)이어야 하고, 병렬 실행 시 **락 또는 append-only(JSONL)** 를 쓴다. 전체 배열 read-modify-write는 레코드 유실·파일 손상을 만든다.

### Runner (`pipeline/collect.py`·`pipeline/structure.py`) · CLI (`cli.py`)
- 소스 **간 병렬 · 소스 내 순차**(SPEC §3). **에러 격리** — 한 소스 실패가 나머지를 멈추지 않는다.
- ⚠️ **구조화도 같은 모양이다**(`structure_pending` · `--workers`): 게시판 하나를 스레드 하나가 통째로 맡는다. 그래서 그 게시판의 접속 클라이언트(요청 간격·세션)를 아무도 같이 건드리지 않아 fetch 층에 잠금이 필요 없다. 공유되는 것은 저장(`JsonStore` 락)과 집계뿐이다.
- ⚠️ **유료 상한(`--limit`)은 부르기 전에 자리를 잡는다** — 부르고 나서 세면 게시판 수만큼 넘겨 청구된다. Gemini를 부르지 않은 판정(빈 공고·그림 대기)은 자리를 돌려준다.
- ⚠️ **저장이 연속 5번 실패하면 실행을 멈춘다** — 원장이 통째로 깨지면 글 단위 격리가 독이 되어 3,000번 과금하고 아무것도 저장하지 못한다. 흩어진 손상 행은 성공 한 번이 누적을 지워 끝까지 돈다.
- 실행 요약(`crawl_run`)·소스 상태(`source_health`) 기록, **0건·급감 경보 판정도 여기**(기준은 SPEC §7).
- CLI 모드: `daily`(증분) · `backfill`(로컬 1회 · 범위는 SPEC §4) · `collect`/`structure`(단계별) · `status` · `check-gemini` · `list-sources`. **운영자용 사용법은 RUNBOOK에 기록한다.**

## 저장소·비밀 규칙

- **staging 4테이블(`source_data`·`review_data`·`source_health`·`crawl_run`)은 이 리포가 소유·마이그레이션**한다(SPEC §8). 물리적으로 min_job Supabase 프로젝트에 함께 두되, **min_job 리포의 파일을 이 작업으로 수정하지 않는다**.
- **RLS: 운영자 전용**(public 노출 없음). 크롤러는 staging 4테이블에 쓰고, `jobs`에는 **읽기 + INSERT + `posted_at` 한 칸**만 갖는다(운영자 결정 2026-08-18 · **GRANT 정본은 SPEC §8**). ⚠️ **권한을 코드 규율이 아니라 DB로 강제한다** — 운영자가 검수에서 고친 값을 크롤러가 덮는 길이 컬럼 단위 GRANT로 막힌다.
- 비밀은 **환경변수만**(`.env` 로컬 · GH Secrets CI). 코드·config·데이터·로그에 키를 남기지 않는다. `.env.example`만 커밋.
- **DB는 저장 전용** — trigger·custom function을 만들지 않는다(min_job DB 정책 승계). 로직은 파이프라인 코드에.
- env는 **`settings.py` 한 곳**에서 읽는다. import 시점에 캡처하지 말고(dotenv 로드보다 먼저 실행됨), 빈 문자열은 미설정으로 취급한다.

## Clean Code Principles

- **단일 책임**: 한 함수/모듈은 한 가지. 60줄 넘으면 분해 검토.
- **명명이 곧 문서**: 의도가 드러나는 이름. 주석은 *왜*가 필요할 때만.
- **죽은 코드 즉시 삭제**: 미사용 import·함수 남기지 않음(이식 후 TS 잔존물 포함).
- **매직 값 금지**: URL·셀렉터·페이지 파라미터는 config, 타임아웃·간격·상한은 모듈 상단 상수.
- **에러는 경계에서만**: 네트워크·AI 경계에서 처리하고 소스 단위로 격리한다. 조용히 삼키지 않는다(빈 값·no-op 반환 금지 — 실패는 실패로).
- **경계에서 검증**: 외부 입력(게시판 HTML·JSON 파일·config)은 신뢰하지 않는다. `json.load` 결과를 그대로 dataclass로 믿지 말고 검증 후 변환한다.
- **타입으로 잘못된 상태를 표현 불가능하게**: `Any` 금지. `Literal`·`StrEnum`·dataclass로 좁힌다.

## Code Conventions

**Naming**
- 파일·함수·변수: `snake_case`. 클래스: `PascalCase`. 상수: `UPPER_SNAKE_CASE`. Boolean: `is_`/`has_`/`needs_`.
- **`source_key`는 영어 대문자**(`YTUS`) — 저장값·config 키·로그 동일. 문서 산문에서 소문자로 쓰는 것은 라벨일 뿐이다.
- **저장값에 한글을 쓰지 않는다.** enum 값은 영어 대문자 key, 한글 라벨은 **min_job 소관**(여기서 라벨 맵을 만들지 않는다). 허용값 정본 = **CONTRACT §1**(min_job DATA.md는 참고 — 드리프트 시 CONTRACT를 따르고 불일치를 보고한다).

**Python**
- 타입 힌트 필수, `Any` 금지. 레코드는 `@dataclass`(write-once 테이블은 `frozen=True`).
- 표준 라이브러리 우선, 의존성은 최소.
- 시간은 **KST**·ISO8601(`+09:00`)로 한 헬퍼에서만 생성한다(포맷 드리프트 방지). ⚠️ **오프셋을 떼지 않는다** — naive KST는 DB가 서버 시간대로 해석해 9시간 어긋난다. `date` 컬럼은 시간대가 없으므로 변환 대상이 아니다(운영자 결정 2026-08-05 · `Z`↔`+09:00`은 같은 순간이라 Postgres `timestamptz`에 동일하게 저장된다).
- 공유 타입·enum은 `domain.py`·`models.py`. 한 모듈 전용 타입은 파일 상단.

**Imports**
- 패키지 내부는 절대 import(`from minjob_ingest.fetch import client`). 상대 import는 같은 서브패키지 내에서만.

## Git Workflow

- 브랜치: `prod`(배포·안정) / `dev`(작업). 평소 작업은 항상 `dev`. feature 브랜치 X.
- 릴리스는 `dev` → `prod` **fast-forward only**.
- **commit / push / merge는 사용자가 명시적으로 요청할 때만.** 자동 커밋 금지.
- 커밋 메시지: 영어, 동사 원형(Add/Fix/Update/Remove). 1 커밋 = 1 논리적 변경.

## 소통

- 사용자와의 대화는 **한국어**. 커밋 메시지·코드 식별자는 **영어**.
- **코드 주석은 한국어 허용** — 게시판 특이사항·도메인 맥락 설명이 많아 한국어가 더 명확.

## Quality Checklist

1. `ruff` + `mypy` + `pytest` 통과 · 미사용 import 없음 · `Any` 없음 · 단일 책임
2. **새 어댑터**: 파일 1개 = 게시판 1곳(파일명 = key 소문자) · config 등록(tier·encoding·flags·detail_pattern·page_param·notice_marker·fetch_note) · **fixture 확보 후 테스트 추가(네트워크 금지)** · 적합성 테스트 통과
3. **네트워크**: fetch 층 경유(직접 HTTP X) · UA 항상 송신 · config encoding 우선(EUC-KR은 cp949) · 간격·타임아웃 기본값 준수·**한 호스트 1요청** · 성공을 **본문으로** 검증
4. **저장**: `Store` 경유(직접 파일·DB X) · 필드명 = SPEC §6 snake_case · `source_data` write-once(opt-out 예외) · JSON 쓰기는 원자적
5. **증분**: 원장(`source_key`+`external_id`)으로 판정 · "이미 본 글에서 중단" 로직 없음 · 공지행 제외
6. **AI**: 출력 스키마 강제 + enum 정규화 · **`structured_at` 기록**(게이트1 탈락 포함) · 빈 응답은 실패 처리 · 모델 ID는 env
7. **경계**: `jobs`는 **자기가 만들었고 claim 전인 행**만(INSERT·`posted_at`) · `churches` 쓰기 없음 · **`../min_job` 파일 미수정** · 유료 호출·전량 수집은 운영자
8. **커밋 전**: `data/`·`.env` 미포함 · fixture 개인정보 마스킹 · Actions는 Supabase 전환 후에만
