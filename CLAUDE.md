# CLAUDE.md — min_job_agent

> **이 파일은 HOW** — 아키텍처·레이어 책임·코드 컨벤션·가드레일. **운영자가 타이핑하는 명령은 [`docs/RUNBOOK.md`](./docs/RUNBOOK.md)**. 파이프라인 동작·판정 규칙·스키마는 [`docs/SPEC.md`](./docs/SPEC.md), 크롤 대상 소스는 [`docs/SOURCES.md`](./docs/SOURCES.md), 출력 계약·교단 정규화는 [`docs/CONTRACT.md`](./docs/CONTRACT.md), 작업 단위는 [`docs/ROADMAP.md`](./docs/ROADMAP.md), 시점 핸드오프는 [`docs/SNAPSHOT.md`](./docs/SNAPSHOT.md).
>
> **문서 책임 분리** — 같은 사실을 두 곳에 쓰지 않는다. **여기는 "코드를 어떻게 쓰는가"만** 담고, 정책·판정 규칙·소스 목록·스키마 필드는 위 문서를 **가리킨다**(복사하지 않는다).
>
> ⚠️ **정본 순서**: 게시판 전송 사실(tier·encoding·flags·상세URL) = **`config/sources.json`**(라이브 검증값) > SOURCES §6 요약. 그 외 파이프라인·판정·스키마 = SPEC. **코드와 문서가 다르면 전송은 코드가, 정책은 SPEC이 이긴다.**

## Project

형제 리포 `../min_job`(개교회 채용 플랫폼)을 위한 **공고 수집 크롤러**. 공개 청빙 게시판(확정 목록 = SOURCES §7)에서 공고를 수집 → AI로 구조화 → **리뷰 큐(`review_data`)** 에 적재한다. 운영자가 min_job admin에서 검수·승격하면 공개된다. min_job 파이프라인에서 **fetch 한 단계만 자동화하는 델타**다.

**별도 리포인 이유**: min_job은 **in-repo 크롤러 코드를 두지 않는다**(min_job `CLAUDE.md` Ingest 레이어 규칙). 자동 수집 자체는 허용되며(min_job 가드레일 #1 — 공개 공식 게시판 한정·운영자 검수 전제·법률 검토 완료 2026-07-28), 그 구현체가 이 리포다.

**Stack**: Python 3.12+ · `httpx` + `beautifulsoup4`/`lxml` · `google-genai`(Vertex AI Gemini) · JSON 파일 저장(Phase 1) → Supabase · GitHub Actions(스케줄)

> ⚠️ **스택 변경(2026-07-29)**: 원래 TypeScript/Node였다(SNAPSHOT §2 "되돌리지 말 것" 항목). **Python으로 교체됨** — 크롤 생태계 성숙도 + 운영자가 직접 실행. TS를 택한 명목("min_job 타입 공유")은 별도 리포·별도 프로세스라 실제로 성립하지 않았고, enum 정합은 코드 공유가 아니라 **CONTRACT §1 계약 + 드리프트 테스트**로 지킨다.
>
> ✅ **TS 뼈대 이식 완료(2026-07-29, 0-1c).** `src/*.ts`·`package.json`·`tsconfig.json`은 삭제했다 — 되짚어야 하면 git 이력을 본다. 재취득 불가 자산이던 31곳 검증값(특히 `fetch_note`)은 `config/sources.json`으로 **문자 그대로** 이관됐다.
>
> ⚠️ **`google-genai` SDK·Gemini 모델 ID는 학습 데이터와 다를 수 있다.** 구조화 코드 작성 전 공식 문서를 확인할 것. 모델 ID는 **`VERTEX_MODEL` env에서 읽고 하드코딩하지 않는다**(운영자가 최신 Flash로 교체함). 인증은 서비스계정 4개 env(`VERTEX_AI_PROJECT_ID`·`_LOCATION`·`_CLIENT_EMAIL`·`_PRIVATE_KEY`) — `.env.example` 참조.

## Architecture Overview

### 핵심 결정: 상시 서버 없는 배치 크롤러

하루 1회 짧게 도는 배치다. 상시 서버를 두지 않고 **러너에서 실행 후 종료**한다(고정비 0). 러너는 끝나면 사라지므로(ephemeral) **다음 실행이 이어받을 상태는 전부 원격 저장소에 있어야 한다**.

```
[운영자 로컬 CLI]  ─ 백필·수동 실행 (Phase 1 기본)
[GitHub Actions cron] ─ 매일 자동 (⚠️ Supabase 전환 후에만)
        │  config(소스 레지스트리) + env(Vertex·Supabase 키)
        ▼
[크롤러 프로세스]  fetch → 구조화 → 적재      ← 끝나면 소멸
        ▼
[저장소]  source_data → review_data ──(운영자 승격)──▶ min_job churches/jobs
```

> ⚠️ **순서 제약(필수)**: **JsonStore(로컬 파일) 단계에서 GitHub Actions를 붙이지 않는다.** ephemeral 러너에선 JSON 원장이 매 실행 사라져 → 31곳 전량 재크롤(가드레일 #7 위반) + 전량 재구조화(비용) + 산출물 유실. `crawl.yml`은 **SupabaseStore 전환(ROADMAP 1-6) 이후**에 만든다. 그전까지 실행은 운영자 로컬.

### 파이프라인

단계 정의·판정 규칙은 **SPEC §2·§5가 정본**이다(여기서 다시 번호를 붙이지 않는다). 이 리포가 지켜야 할 아키텍처 사실만:

- 크롤러의 **종착지는 `review_data`(PENDING)** 다. `churches`/`jobs`를 직접 쓰지 않는다.
- 자동 구간(수집·구조화·적재) 다음의 **검수·승격은 min_job 쪽 책임**이며 이 리포 밖이다.
- `crawl_run`은 **실행 시작에 INSERT**해 `run_id`를 얻고(하위 레코드가 참조) **종료에 UPDATE**한다.

### 3층 분리 (게시판이 31개여도 코드는 안 늘어난다)

게시판마다 크롤러를 만들지 않는다. 차이를 **가장 얇은 층에 몰아넣는다**:

| 층 | 무엇 | 게시판별 차이 |
|---|---|---|
| **config** (소스 레지스트리) | 어디를·어떻게 접속하나 | **대부분 여기** (데이터, 코드 아님) |
| **fetch** (`fetch/`) | 바이트 가져오기 — UA·디코드·타임아웃·재시도·rate limit·robots·세션 | 없음(전 소스 공유) |
| **parse** (`sources/adapters/`) | 목록 행·상세 본문 추출 | CMS 계열별 1개 + 일회성 다수 |

→ 신규 소스는 **config 한 칸 + (계열이 있으면) 어댑터 0개**. 다만 31곳 중 진짜 계열(그누보드·Konnect `subview.do`·eGov `.do`)은 몇 개뿐이고 **나머지는 대부분 일회성 CMS**다 — 어댑터 개수를 목표로 삼지 말고, **새 파일 만들기 전에 기존 계열로 흡수되는지만 확인**한다.

### 저장 seam (JSON → Supabase 무통증 전환)

파이프라인은 저장소 구현을 모른다. `Store` 프로토콜만 호출하고 구현을 갈아끼운다.

```
pipeline → Store(프로토콜) → [Phase 1] JsonStore(로컬) → [1-6] SupabaseStore
```

- **레코드 필드명 = SPEC §6 컬럼명(snake_case)과 동일**하게 둔다 → 전환이 "그대로 INSERT"가 된다. *(현 TS 뼈대는 camelCase + 스토어에서 매핑하는 구조다 — **Python 이식 때 snake_case로 정리**해 매핑 계층을 없앤다.)*
- 스키마가 굳기 전까지 마이그레이션을 만들지 않는다(ROADMAP 1-6).

## Directory

**flat 레이아웃** — 패키지가 리포 루트에 있고 `src/` 껍데기를 두지 않는다. `src` 레이아웃은 PyPI에 올리는 **배포 라이브러리** 권장 배치이고, 우리는 **리포에서 그대로 실행하는 앱**이다(운영자가 CLI 실행 · `config/`·`data/`가 코드 옆). 앱에 `src/`를 끼우면 `config/` 경로 계산만 한 단계 깊어지고 얻는 게 없다.

```
minjob_ingest/                 ★ 패키지 (= import 이름)
├── cli.py                    진입점 (운영자가 실행하는 창구)
├── domain.py                 enum — CONTRACT §1 계약 미러 + 크롤러 enum
├── models.py                 레코드 dataclass — SPEC §6 4테이블
├── clock.py                  UTC·ISO8601·date 생성/직렬화 단일 창구
├── paths.py                  리포 기준 경로 (한 곳에서만 계산)
├── settings.py               env 로딩 1곳 (import 시점 캡처 금지)
├── sources/registry.py       소스 레지스트리 로드·검증  (+ adapters/ 예정 = 게시판별 파싱)
├── fetch/{client.py, robots.py}   전송 단일 창구 · robots 준수
├── store/{base.py, serde.py, json_store.py}   Store 프로토콜 · 행 변환 · JSON 구현
│                                              (+ supabase_store.py 예정 = 1-6)
├── lib/gemini.py             Vertex 클라이언트 (재시도는 SDK 설정)
└── pipeline/ 예정            run·collect·structure·denomination·dedup
config/
├── sources.json              ★ 소스 레지스트리 (전송 정본 · 라이브 검증값)
└── heresy-ref.json           이단 참고 목록 (사람이 관리 · git 이력 = 감사)
tests/{fixtures/, test_*.py}
data/                         로컬 저장소 (gitignored)
```

> ⚠️ 위 트리에서 `sources/adapters`·`fetch`·`pipeline`·`store`·`lib`는 **아직 없는 목표 구조**다. 드리프트할 수 있으니 "계약"으로 신뢰하지 말 것.
>
> **커밋하지 않는 자동생성물**: `.venv/`·`__pycache__/`·`minjob_ingest.egg-info/`(pip 메타데이터)·`.mypy_cache/`·`.ruff_cache/`·`.pytest_cache/`·`data/`. 전부 `.gitignore`에 있다 — 지워도 도구가 다시 만든다.
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
```
아직 없는 명령(Phase 1): `collect`·`structure`·`daily`·`backfill`·`status`.

> ⚠️ **CLI 명령을 추가·변경하면 `docs/RUNBOOK.md`를 같이 고친다.** 운영자는 그 파일만 보고 실행한다 — 여기에만 적으면 전달되지 않는다.


## Layer Responsibilities

### Registry (`sources/registry.py` + `config/sources.json`)
- **"어떻게 접속하나"를 데이터로** 보유. 코드에 URL·셀렉터·페이지 파라미터를 하드코딩하지 않는다.
- 현재 필드(= `minjob_ingest/sources/registry.py`): `key`(대문자) · `board_name` · `denomination_hint`(참고, 확정 아님·null 가능) · `enabled` · `fetch_tier` · `encoding` · `flags` · `list_url` · `detail_pattern`(`{id}` 치환) · `fetch_note`.
  - `flags`: `www_required` · `http_only` · `spoof_ua`(브라우저 UA 필수) · `insecure_tls` · `needs_session`(상세가 쿠키 요구) · `image_only`(본문이 이미지 — 빈 raw_text가 정상) · `soft_200`(잘못된 요청에도 200 → 본문으로 검증).
  - **이식 시 추가 예정**: `disabled_reason` · `page_param` · `notice_marker` — 지금은 `fetch_note` 산문에만 있다(구조화하면 어댑터가 코드로 안 들고 있게 된다).
- **로드 시 검증**(스타트업 assert + 테스트): key 대문자·유일 · `denomination_hint ∈ CONTRACT §1 ∪ {null}` · `flags` 키 화이트리스트 · `detail_pattern`이 있으면 `{id}` 포함. ⚠️ **예외 2곳**: `CSU`(API 호출이라 URL 템플릿 없음) · `HANSEI`(경로에 카테고리 id가 끼어 목록 href를 그대로 사용) — 사유는 `fetch_note`에.
- **소스 추가/제외 = 이 JSON 편집.** 제외는 삭제가 아니라 `enabled: false` + `disabled_reason`(이력 보존·재활성 대비).
- `fetch_note`는 라이브 검증 메모(세션 필요·soft 실패·공지행·pagination)다. **지우거나 요약하지 말 것** — 재취득 불가.

### Adapter (`sources/adapters/*.py`) — **파싱만**
- 정확히 두 가지: `list_postings(opts) → [PostingRef]`(external_id·url·제목·게시일), `fetch_posting(ref) → RawPosting`(raw_text·이미지 URL·메타).
- **네트워크·인코딩·UA·재시도를 어댑터가 다루지 않는다** — fetch 층이 흡수. 어댑터는 받은 텍스트/JSON을 파싱만 한다.
- `external_id`의 **유일성은 어댑터 책임**(그 소스 내). 없으면 제목+게시일 해시.
- 고정공지(pinned)는 제외한다. ⚠️ **"이미 본 글을 만나면 중단" 금지** — 이유·규칙은 SPEC §4.
- 새 파일 만들기 전 기존 계열 재사용 검토. 비슷한 게 둘이면 그대로 두고, **셋째에 base로 추출**한다.

### Fetch (`fetch/*.py`) — 전송 단일 창구
- 모든 HTTP는 여기를 지난다. **어댑터·파이프라인이 직접 `httpx.get`을 부르지 않는다.**
- 정책은 SPEC §3, 소스별 값은 config. 이 층이 단독 구현한다: UA(**항상 비어있지 않은 UA 송신**, `spoof_ua`면 브라우저 UA) · `encoding` **config 값 우선**(서버 헤더가 틀린 보드가 있음 · 미지정 시 자동감지) · 타임아웃 · 재시도 · rate limit · robots `Disallow` · 세션 쿠키(`needs_session`).
- **동시성은 SPEC §3이 정본**: **소스 간 병렬 · 소스 내 순차** — 한 호스트에는 항상 요청 1개만 흐른다(그래서 31곳 동시 실행이 예의에 어긋나지 않는다). 자원 보호용 상한이 필요하면 **정책이 아니라 실행 옵션**으로 둔다.
- **기본값(config 미지정 시)**: 요청 타임아웃 **20s** · 재시도 **3회**(지수 백오프+지터, 429·5xx·연결오류) · 같은 소스 요청 간격 **≥1.5s** · 목록 페이지 데일리 **≤3p**. 상수는 모듈 상단에 둔다.
- **성공을 상태코드만으로 판정하지 않는다** — 본문 내용으로 검증한다(일부 보드는 잘못된 요청에도 200을 준다 · `soft_200`). 전송 층은 **본문 길이 하한**으로 스텁 응답을 걸러내고, 내용 수준 판정은 어댑터가 한다.
- **`www_required`·`http_only`는 이 층에 코드가 없다** — 두 값은 이미 `list_url`에 반영돼 있고 레지스트리가 로드 시 강제하며, 상대 URL은 `urljoin`이 호스트·스킴을 물려준다. 아무 일도 안 하는 코드를 만들지 않는다.
- **robots**: `RESPECT_ROBOTS = False`(운영자 판단 2026-07-30) — robots.txt를 **요청조차 하지 않는다**. 켜면 호스트당 한 번 받아 `Disallow`는 `RobotsDisallowed`(=`FetchError`)로 던지고(조용히 skip하지 않는다), 못 가져오면 "제한 없음"으로 진행하고, `Crawl-delay`가 기본 간격보다 크면 사이트 값을 따른다.
- **EUC-KR 선언 소스는 `cp949`로 디코드**한다(EUC-KR 순정 코덱은 확장 한글에서 예외 → 한 글자로 페이지 전체를 잃는다).

### Structure (`pipeline/structure.py`) — AI는 추출·추정만
- raw_text(+이미지 바이트)를 Gemini에 넣어 SPEC §5의 필드를 산출한다. **출력은 스키마로 강제**하고, enum 밖 값은 방어적으로 정규화한다.
- **AI에게 최종 확정을 위임하지 않는다**: 교단은 명시·명부는 규칙이 확정하고 근거가 없을 때만 AI 추정(`ai_guess` 표시 · 확정은 운영자), 이단 판단은 사람, 공개 여부는 운영자 검수. 규칙은 SPEC §5.3·§5.4.
- **구조화 시도 후에는 반드시 `source_data.structured_at`을 기록한다** — 게이트1 탈락(review_data 미생성)도 포함. 이게 없으면 "제외된 공고"와 "구조화 실패"를 구분할 수 없어 **매 실행 재호출되는 비용 루프**가 된다(SPEC §4).
- 실패(429·파싱오류)는 삼키지 않는다 — `structured_at`을 남기지 않고 다음 run이 재구조화한다. 단 **재시도 상한**을 둬 영구 실패가 무한 재호출되지 않게 한다.
- **응답을 성공으로 오판하지 않는다**: 빈 텍스트·`finishReason` 이상은 실패로 처리한다(빈 문자열로 흘리지 말 것).

### Store (`store/*.py`) — 저장 단일 창구
- 파이프라인은 `Store` 프로토콜만 안다. **파일 경로·SQL·Supabase 클라이언트가 파이프라인에 새지 않는다.**
- `source_data`는 **write-once**(원문 증거). 일반 경로에서 갱신하지 않는다 — 수정 감지는 리비전 행 추가(Phase 3). **예외: 운영자 opt-out·법적 삭제 요청은 삭제/마스킹이 가능해야 한다**(가드레일 #4).
- 원장은 `source_data`의 `(source_key, external_id)` 유일성이 담당한다. 별도 원장 테이블을 만들지 않는다.
- 프로토콜에 **읽기도 포함**해야 한다: 원장 조회(가능하면 **bulk** — 페이지당 1회), `source_health` 조회(연속 실패 누적·마지막 성공 보존에 필요), 미구조화 목록(상한 있는 배치).
- **JSON 구현 주의**: 쓰기는 **원자적**(임시파일 → rename)이어야 하고, 병렬 실행 시 **락 또는 append-only(JSONL)** 를 쓴다. 전체 배열 read-modify-write는 레코드 유실·파일 손상을 만든다.

### Runner (`pipeline/run.py`) · CLI (`cli.py`)
- 소스 **간 병렬 · 소스 내 순차**(SPEC §3). **에러 격리** — 한 소스 실패가 나머지를 멈추지 않는다.
- 실행 요약(`crawl_run`)·소스 상태(`source_health`) 기록, **0건·급감 경보 판정도 여기**(기준은 SPEC §7).
- CLI 모드: `daily`(증분) · `backfill`(로컬 1회 · 범위는 SPEC §4) · `collect`/`structure`(단계별) · `status` · `check-gemini` · `list-sources`. **운영자용 사용법은 RUNBOOK에 기록한다.**

## 저장소·비밀 규칙

- **staging 4테이블(`source_data`·`review_data`·`source_health`·`crawl_run`)은 이 리포가 소유·마이그레이션**한다(SPEC §8). 물리적으로 min_job Supabase 프로젝트에 함께 두되, **min_job 리포의 파일을 이 작업으로 수정하지 않는다**(가드레일 #9).
- **RLS: 운영자 전용**(public 노출 없음). 크롤러는 **service-role 키로 staging에만** 쓴다. `churches`/`jobs` write 권한을 크롤러에 주지 않는다.
- 비밀은 **환경변수만**(`.env` 로컬 · GH Secrets CI). 코드·config·데이터·로그에 키를 남기지 않는다. `.env.example`만 커밋.
- **DB는 저장 전용** — trigger·custom function을 만들지 않는다(min_job DB 정책 승계). 로직은 파이프라인 코드에.
- env는 **`settings.py` 한 곳**에서 읽는다. import 시점에 캡처하지 말고(dotenv 로드보다 먼저 실행됨), 빈 문자열은 미설정으로 취급한다.

## 가드레일 (절대 위반 금지)

min_job 가드레일을 승계·구체화한다. 근거는 SPEC·CONTRACT·min_job `CLAUDE.md`.

1. **공개 게시판만 수집.** 확정 목록(SOURCES §7) 외를 임의로 늘리지 않는다. **로그인이 필요한 소스는 범위 밖** — 인증 크롤은 변호사 게이트 후 별도 단계이고 **크롤러가 가입·로그인을 자동화하지 않는다**(계정은 운영자 제공). 공개인 줄 알았던 소스가 회원벽으로 확인되면 **우회하지 말고 비활성화 + 운영자 보고**. 영리 청빙사이트는 출처로 삼지 않는다.
2. **자동 공개 절대 금지.** 종착지는 `review_data`(PENDING). `churches`/`jobs`에 직접 쓰지 않는다. 승격 시 min_job이 **`source=OPERATOR`·`owner_id=NULL`**(주인 없는 공고)로 넣는다 — min_job 가드레일 #2, 규칙은 SPEC §6 승격 항목.
3. **원문 재게시 금지 · 출처 표기.** `description`은 요약, 원문은 `source_url` 링크로. raw는 staging에만 두고 공개 필드로 흘리지 않는다.
4. **개인정보 최소 + opt-out.** 지원용으로 명시 공개된 연락처만 `contact`로 추출한다(SPEC §5.5). 제3자 개인정보는 추출하지 않고, 게시판이 가려둔 번호를 복원하지 않는다. **교회가 요청하면 해당 교회·공고를 수집 대상에서 제외(opt-out)하고 기존 수집분도 삭제할 수 있어야 한다** — write-once 원칙보다 우선한다. ⚠️ 약관·개인정보처리방침 정식 검토는 진행 중(min_job).
5. **이단은 플래그만.** 근거만 남기고 **자동 삭제·공개 낙인 금지**(명예훼손·오판 회피). 최종 판단은 사람.
6. **교단은 공고에서 확정.** 게시판 교단은 힌트일 뿐이다. 근거 없으면 `UNKNOWN`(+운영자 게이트) — 추측으로 찍지 않는다(SPEC §5.3).
7. **예의 있는 크롤.** 위 fetch 기본값(간격·타임아웃)과 **한 호스트 1요청** 원칙을 지킨다. ⚠️ **robots.txt는 따르지 않는다** — 운영자 판단(2026-07-30 · 문제없음 확인). 부하 보호는 robots가 아니라 **요청 간격·호스트당 1요청·타임아웃·페이지 상한**이 담당한다. 판정 코드(`fetch/robots.py`)와 `RESPECT_ROBOTS` 스위치는 살려둔다 — 게시판 한 곳이 요청하면 되돌릴 수 있어야 한다. 원장으로 증분해 **같은 글을 다시 긁지 않는다**. 개발 중 반복 실행으로 사이트를 두드리지 않는다 — **테스트는 fixture로, 네트워크를 타지 않는다**.
8. **재공고는 보존.** 같은 교회·자리의 다른 시점 공고를 합치지 않는다(min_job 차별점). dedup은 "같은 글의 중복 수집·교차게시"까지이며 **자동 병합이 아니라 후보 표시**다.
9. **경계를 넘지 않는다.** 이 리포에서 **`../min_job`의 파일을 수정하지 않는다**(연동 요구는 문서로 전달). min_job은 staging 마이그레이션을 만들지 않는다.
10. **프로덕션 수집은 운영자가 실행한다.** 에이전트(Claude)는 코드·config·문서를 만들고 **검증 목적의 소량 요청만** 한다(보드당 목록 1~2건 수준, 반복 금지). 전 소스 실행·백필·대량 AI 호출은 운영자가 CLI로 한다.
11. **커밋 위생.** 수집 산출물(`data/`)·`.env`를 커밋하지 않는다. **fixture는 개인정보를 마스킹**한 뒤 커밋한다(원본 HTML엔 실제 연락처가 있다). `heresy-ref.json`은 민감 자료 — 리포 공개 전 재검토.

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
- 시간은 UTC·ISO8601로 한 헬퍼에서만 생성한다(포맷 드리프트 방지).
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
2. **새 어댑터**: 기존 계열 흡수 불가 확인 · config 등록(tier·encoding·flags·detail_pattern·page_param·notice_marker·fetch_note) · **fixture 테스트 추가(네트워크 금지)**
3. **네트워크**: fetch 층 경유(직접 HTTP X) · UA 항상 송신 · config encoding 우선(EUC-KR은 cp949) · 간격·타임아웃 기본값 준수·**한 호스트 1요청** · 성공을 **본문으로** 검증
4. **저장**: `Store` 경유(직접 파일·DB X) · 필드명 = SPEC §6 snake_case · `source_data` write-once(opt-out 예외) · JSON 쓰기는 원자적
5. **증분**: 원장(`source_key`+`external_id`)으로 판정 · "이미 본 글에서 중단" 로직 없음 · 공지행 제외
6. **AI**: 출력 스키마 강제 + enum 정규화 · **`structured_at` 기록**(게이트1 탈락 포함) · 빈 응답은 실패 처리 · 모델 ID는 env
7. **가드레일 준수**: 공개 소스만 · `review_data`까지만 · 요약+출처 링크 · 지원용 연락처만·opt-out 가능 · 이단 플래그만 · 교단 근거 없으면 `UNKNOWN` · rate limit · **min_job 파일 미수정** · 프로덕션 수집은 운영자
8. **커밋 전**: `data/`·`.env` 미포함 · fixture 개인정보 마스킹 · Actions는 Supabase 전환 후에만
