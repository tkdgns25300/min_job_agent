# SPEC.md — 크롤 파이프라인 명세

> **크롤러가 어떻게 동작하는가** — 스코프·파이프라인·크롤 정책·판정 로직·데이터 모델·배포. 크롤 대상 소스는 [`SOURCES.md`](./SOURCES.md), 출력 스키마·교단 정규화는 [`CONTRACT.md`](./CONTRACT.md), 시점 핸드오프는 [`SNAPSHOT.md`](./SNAPSHOT.md).
>
> **출력(공개) 스키마 정본 = `../min_job/docs/DATA.md`**(`churches`/`jobs`). 크롤러 전용 staging 테이블은 이 문서가 소유·명세.
>
> ⚠️ 이 문서가 파이프라인 동작의 **최신 정본**이다. CONTRACT의 일부(연락처 미추출·노회 매핑표·교단 매핑 원칙)는 여기서 갱신됨 — §8 "이 리포 문서 갱신" 참조.

---

## 0. 개요

크롤러는 **개교회(지역 교회)의 채용 공고**를 공식 게시판(SOURCES 31곳)에서 수집해, 구조화 초안으로 만들어 **리뷰 큐(`review_data`)**에 쌓는다. 운영자가 min_job admin에서 검수·승인하면 공개(`jobs`)로 승격한다(⚠️ `churches`에는 쓰지 않는다 — §6 승격 목적지). 크롤러는 **절대 바로 공개하지 않는다**(사람 게이트).

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
   ▼ ⑥ 운영자 검수 (min_job admin) → 승인/수정 → jobs 승격(공개 · church_id=NULL)
```

- ①~⑤ 자동, ⑥ 사람. 크롤러 write = `source_data`·`review_data`·`source_health`·`crawl_run`. 크롤러는 `churches`/`jobs`를 직접 안 건드린다.
- **crawl_run은 실행 시작(①)에 INSERT**(started_at·mode, run_id 확보) → source_data/review_data가 이 run_id를 FK로 참조 → **종료(⑤)에 UPDATE**(finished_at·sources_ok/failed·new_count·error_detail).
- ⚠️ **구조화를 따로 돌리는 실행은 `crawl_run`을 만들지 않는다**(Phase 1의 `structure` 명령 — 수집이 끝난 뒤 유료 호출을 별도로 집행한다). `crawl_run`의 집계는 전부 **게시판 단위**(`mode`·`sources_ok`·`sources_failed`·`new_count`·`error_detail[source_key]`)라 **공고 단위**로 도는 구조화가 들어갈 칸이 없고, 억지로 넣으면 컬럼 이름이 뜻과 어긋난다. 그때 `review_data.run_id`는 **그 공고를 수집해온 실행**(`source_data.run_id`)을 승계한다 — 위 정의("①에서 INSERT된 crawl_run")가 그대로 유지되고, ①~⑤가 한 실행으로 이어지는 데일리에서도 같은 값이 되어 두 경로가 어긋나지 않는다. 구조화 실행의 진행·집계는 명령 출력과 `source_data`의 `structured_at`·`structure_attempts`·`last_structure_error`가 담당한다.

---

## 3. 크롤 정책 — "안전하되 빠르게"

- **동시성**: **소스 간 병렬 + 소스 내 순차.** 한 사이트를 몰아치지 않되(순차+요청 간 지연) 31곳은 동시에 → 예의 + 속도.
- **요청**: per-request **timeout + 재시도**(지수 백오프+지터). 소스별 rate limit(요청 간 지연). **서버가 준 `Retry-After`가 있으면 그것을 우선**한다(우리 추측보다 정확하고, 무시하면 차단을 부른다 · 상한 60s).
- **robots.txt**: `Disallow`는 **따르지 않고**(운영자 판단 2026-07-30 · 문제없음 확인), `Crawl-delay`는 **따른다**(2026-08-04). 전자는 허락의 문제이고 후자는 서버 용량 신고라 무시하면 IP 차단을 부른다. 스위치는 `RESPECT_ROBOTS_DISALLOW`·`RESPECT_CRAWL_DELAY`. ⚠️ **`Crawl-delay`는 우리 UA 그룹에 걸린 것만 따른다**(2026-08-05 실측): `SJS`는 `Crawl-delay: 10`을 bingbot·msnbot에만 걸어 뒀는데 그룹을 무시하고 파일 전체 최댓값을 쓰던 폴백이 그걸 가져와 **그 게시판만 6.7배 느렸다**. 사이트는 싫어하는 SEO 봇에 30~60초를 걸므로 그룹을 무시하면 한 줄이 수집을 멈춘다. 표준 `RobotFileParser`는 소수점을 버리는 것 말고도 **그룹 밖 지시자를 `*`에 붙이므로**, 지연 판정은 원문을 직접 읽는 우리 파서가 정본이고 표준 파서는 `Disallow`에만 쓴다. **실측: 30개 호스트 중 우리에게 지연을 거는 곳은 0곳**(전부 기본 1.5s).
- **User-Agent**: **31곳 전부 동일한 브라우저 UA**(2026-08-04 변경). 자체 UA는 게시판이 막는다(YTUS 실측 403). 어디가 막는지 사전에 알 수 없어 보드별 예외로 두지 않으며, `spoof_ua` 플래그는 **실측 기록으로만** 남는다. UA 단독이 아니라 브라우저 헤더 세트(`Accept`·`Accept-Language`)를 함께 보내고, JSON 티어는 AJAX 헤더(`X-Requested-With`)를 맞춘다.
- **인코딩**: **config의 `encoding` 값이 우선**(서버 헤더가 틀리게 보고하는 보드가 있음 — HEAD는 EUC-KR인데 본문은 UTF-8 등), 미지정 시 자동 감지. **EUC-KR 선언 소스는 `cp949`로 디코드**한다(순정 EUC-KR 코덱은 확장 한글에서 예외 → 한 글자 때문에 페이지 전체를 잃는다).
- **첨부는 전부 수집한다**(2026-08-04): 게시판이 첨부 목록을 미리보기 컨테이너와 **다른 곳**에 두는 경우가 있어(YTUS 실측) 미리보기만 보면 비이미지 첨부를 통째로 잃는다. `attachments`에 이름+URL을 남기고 구조화가 읽을 수 있는 것을 고른다.
- **첨부 역할은 구조화가 판단한다 — 파일명으로 미리 자르지 않는다**(2026-08-05 실측 289건). 첨부는 성격이 셋으로 갈리고 처리가 다르다:
  | 역할 | 예 | 처리 |
  |---|---|---|
  | **공고문** | `청빙공고 포스터.png` · `2027+교역자+청빙+광고.jpg` | 필드 추출의 **근거**로 읽는다 |
  | **지원 양식** | `이력서_2026….hwp` · `사역지원서(지원자_홍길동).hwp` | 내용을 공고로 읽지 않는다. **`required_docs` 근거**이고 URL은 지원자에게 노출한다 |
  | **교회 소개·홍보** | `희락교회_전경.jpg` · `전교인_사진.jpg` · `정찬수목사_소개.png` · `도슨트양성과정 홍보물.jpg` | 필드 추출 근거로 쓰지 않는다(무관한 행사 홍보물이 공고로 뽑힐 수 있다) |
  ⚠️ **파일명 규칙으로 가르면 안 된다** — 실측에서 289건 중 **24건이 "양식"과 "공고" 키워드에 동시 매치**된다(`교역자 초빙 서류.hwp` · `(진실교회)_담임목사_청빙지원서_서식_v2.hwp`). 이미지는 Gemini가 내용을 보고 판단하는 편이 정확하므로 **프롬프트로 지시**하고, 어느 첨부를 근거로 썼는지 남긴다.
  ⚠️ 다만 **지금 당장 오독 위험은 낮다** — 실측 289건 중 이미지는 49건이고 **양식은 전부 HWP/PDF라 Gemini에 들어가지 않는다**(우리는 HWP를 읽지 않는다). 실제 위험은 이미지 쪽의 **교회 소개·홍보물**이다.
- **이미지 공고**: **이미지/텍스트 구분 로직 없음.** 상세에서 텍스트 + 이미지 URL을 확보하고, **구조화 직전 이미지 URL에서 바이트를 fetch**해 Gemini 멀티모달에 텍스트와 함께 전달 → Gemini가 이미지까지 읽어 필드를 뽑는다. **별도 OCR 파이프라인 없음.** 이미지 바이트 fetch 실패 시 raw_text만으로 진행하고 `confidence=low`로 운영자에게(공고가 이미지에만 있으면 본문이 얇을 수 있음).
- **에러 격리**: 한 소스 실패해도 나머지 계속(어댑터별 try/catch → crawl_run.error_detail 기록).

---

## 4. 증분 · 중복 · 백필

- **증분 키 = `external_id` + `source_key`.** `external_id` = **어댑터가 정하는, 그 소스 내 유일한 글 식별자** — 보통 URL의 글번호, 단 REST/AJAX 소스(CSU·HANIL 등)는 JSON/REST의 content id. **유일성 보장은 어댑터 책임**(§10). 목록 앞 페이지를 훑어 **`source_data`에 없는 식별자만** 상세 수집.
  - ⚠️ "처음 본(이미 아는) 글에서 멈추기" **금지** — 고정공지·끌어올림 때문에 아래 새 글을 놓친다. 페이지를 훑고 **unseen만** 가져온다.
  - 최종 안전망: DB `UNIQUE(source_key, external_id)` + `INSERT ... ON CONFLICT DO NOTHING`.
  - ⚠️ **같은 번호가 다른 글을 가리키면 그 소스를 실패시킨다**(2026-08-04). 원장 조회가 저장된 `title`·`posted_on`을 함께 돌려주고, 목록에서 방금 읽은 값과 대조한다(**추가 요청 0건** — 양쪽 값을 이미 갖고 있다). **제목과 게시일이 둘 다** 다르면 ① 게시판이 번호를 재사용했거나 ② 사이트 개편으로 엉뚱한 칸을 읽기 시작한 것이며, 조용히 건너뛰면 그 공고를 영구히 놓친다. **하나만 다르면 정상**으로 본다 — `[끌어올림]`·`(마감)`을 붙이거나 날짜를 손보는 일이 흔해 경보로 만들면 상시 잡음이 된다. 자동으로 새 글 취급하지 않는다(원장 키 체계를 코드가 몰래 바꾸면 안 된다 — 운영자가 어댑터를 복합키로 고친다).
- **끌어올림(bump)**: 같은 글이 위로 와도 external_id 동일 → 이미 있음 → skip(중복 없음). 삭제 후 새 식별자로 재등록은 **새 글 = 재공고**(보존).
- **구조화 재처리 = `structured_at` 기준** ⭐: `source_data`는 ③(fetch·raw 저장)에서 기록되므로, ④ 구조화가 실패(429·파싱오류 등)하면 그 행은 처리되지 않은 채 남는다. 매 run은 증분 신규분에 더해 **`structured_at IS NULL`인 `source_data` 행을 재구조화**한다(원장은 재-fetch만 막고 재구조화는 허용) → 실패 공고가 영구 유실되지 않음.
  - ⚠️ **"`review_data`가 없는 행"을 기준으로 삼으면 안 된다** — 게이트1 탈락(개교회 아님·비채용)은 **의도적으로 review_data를 만들지 않으므로**(§1·§5.1), 그 기준으로는 "제외됨"과 "실패함"이 구분되지 않아 **제외 공고를 매 실행 Gemini로 재전송하는 비용 루프**가 된다(혼재 게시판에선 제외분이 다수).
  - **`structured_at` = 판정이 끝난 시각**이다(게이트1 `YES`→review 생성 / `NO`→제외, **둘 다 기록**). **구조화가 실패하면 기록하지 않는다**(NULL 유지) — 실패에도 시각을 찍으면 그 공고는 영구히 재시도되지 않는다(유실). 즉 "게이트 결과와 무관"하지만 "성공·실패와는 무관하지 않다".
  - `structure_attempts`(시도 횟수)를 함께 올리고 실패 시 `last_structure_error`에 원인을 남긴다. **상한(3회) 초과분은 재시도 대상에서 제외**하고 운영자 리포트로 돌린다(영구 실패의 무한 재호출 방지). 운영자가 원인을 고치면 시도 횟수를 리셋해 재진입시킨다.
  - 재구조화 배치는 **상한을 두고**(한 run당 N건) 오래된 것부터 처리한다 — 백필 직후 대량 backlog가 한 실행을 폭주시키지 않게.
- **백필(로컬 수동 1회)**: `mode=BACKFILL`, **게시일 최근 3개월**까지. 컷오프는 **목록 단계의 게시일**(`listPostings`가 반환하는 날짜)로 판정(구조화 전이라 posted_at 산출 이전 — 목록 날짜 사용). 목록에 날짜가 없는 소스(config `list_has_dates: false`)는 컷오프를 만들지 않고 **페이지 상한이 범위**가 된다. 그 상한은 CLI 옵션이 아니라 **config `list_page_limit`**에 적는다 — 안전 상한(100p)을 그대로 쓰면 범위가 통제되지 않는다(PCKWORLD 실측 2026-08-05: 1,200건 → `list_page_limit: 5`로 60건).
  - **어디까지 페이지를 넘길지는 컷오프가 정한다** — 페이지 전체가 컷오프 밖이면 종료. 페이지 상한은 **폭주 방지용(기본 100p)** 이며 범위를 정하는 값이 아니다(운영자가 `--months`에 맞는 페이지 수를 계산하게 만들면 안 된다). 컷오프를 줬는데 목록에 날짜가 하나도 없으면 **실패**시킨다 — 그러지 않으면 아무 행도 잘리지 않아 상한까지 걷는다.
- **데일리(GitHub Actions)**: `mode=DAILY`, 증분만.
- **수정 감지 없음(MVP)**: 한 번 수집한 스냅샷 사용. 재게시/삭제/수정 추적은 Phase 후반(§9).
### 4.1 중복 판정 (`dedup_key`) — 구조화 **뒤**에 한다

같은 공고가 **같은 게시판에 반복**(끌어올림)되거나 **여러 게시판에 교차게시**된다. 실측 3,188건에서
같은 글의 반복이 **약 42%**다(`점촌제일교회` 전임 사역자 한 자리가 31건 — CSU 23·DAESHIN 5·
KWANGSHIN 1). 그대로 승격하면 min_job 목록 절반이 중복이 된다.

```
dedup_key = 정규화교회명 : region : position : department : 라운드번호

  라운드: 같은 앞 네 요소를 시간순으로 놓고 직전 공고와 간격 ≤ 3개월이면 같은 라운드,
          넘으면 새 라운드(= 재공고 · 별개 공고로 보존)

  같은 dedup_key  →  대표 1건만 승격 · `posted_at`은 그 묶음의 **최신**
```

**정규화는 교회명 하나뿐이다** — 괄호·대괄호 안 제거 → 공백·기호 제거
(`[군산] 개복교회(전북 군산)` → `개복교회`). `region`·`position`·`department`는 enum이라
표기 흔들림이 없다.

⚠️ **네 요소 중 하나라도 비면 병합하지 않는다**(단독 처리). 지역이 없는 공고가 실측 18.6%인데,
그것들은 각각 남는다 — **중복이 남는 것보다 다른 교회를 합치는 것이 훨씬 나쁘다**(되돌릴 수 없고
이미 공개돼 있다). 지역을 키에 넣는 이유도 같다: 교회명 894종 중 **70종이 2개 이상 지역**에
나타난다(`온누리교회` = 경기·대전·서울·인천).

⚠️ **제목은 키에 넣지 않는다.** 같은 자리인데 게시판마다 제목이 다르다
(`점촌제일교회에서 전임 사역자를 청빙합니다` / `점촌제일교회에서 전임사역자 청빙 광고` /
`전임 사역자를 청빙합니다`) → 넣으면 끌어올림이 갈린다. 반대로 제목이 같아도 다른 자리인 경우가
**24%**였다(`높은산교회` 유초등부 vs 영유아부) → 제목은 신뢰할 수 없다. `position`+`department`가
더 정확하다.

⚠️ **마감일도 키에 넣지 않는다** — 실제 날짜가 있는 공고가 **7.9%**뿐이다(`모집시까지` 19.9% ·
나머지는 표현 자체가 없음). 92%가 NULL인 값은 키 구성요소가 될 수 없다.

⚠️ **구조화 전에 하면 안 된다.** `position`·`department`는 구조화 산출물이다. 구조화 전에 쓸 수
있는 키(교회명+제목)로 미리 합치면 **24%가 다른 자리인데 합쳐진다**(위). Gemini 호출을 42% 아낄
수 있지만 공고를 잃는 대가가 더 크다.

**방법론 주석**: 교과서적 중복 제거는 ①블로킹 ②유사도 점수 ③임계값 판정인데, 우리는 ①만 쓰고
키를 정밀하게 만들었다. 이유는 **틀리는 방향이 안전**하고(요소가 없으면 안 합침), **설명·재현이
가능**하기 때문이다(왜 합쳐졌는지 항상 답할 수 있다). 유사도·임계값은 정답 데이터가 쌓인 뒤에
얹는다. 지금 넣으면 틀렸을 때 원인을 알 수 없다.

### 4.2 이미 승격된 공고의 끌어올림 (데일리 전환 후)

승격 시 `review_data.published_job_id`에 생성된 `jobs` 행이 기록된다. 새 공고가 들어오면
**우리 `review_data`만 조회**하면 된다 — min_job DB를 읽지 않는다.

```
새 공고 → dedup_key 계산
  같은 dedup_key 이면서 published_job_id 가 있는 행이 있나?
    있으면  →  기존 job 의 끌어올림  →  그 published_job_id 를 물려주고
                min_job 이 `jobs.posted_at` 만 UPDATE
    없으면  →  새 공고  →  INSERT
```

⚠️ **UPDATE 경로는 min_job 승격 흐름에 아직 없다**(현재 INSERT만). 1회 백필에서는 필요 없고
**데일리(1-7) 전환 시 필요**하다.

---

## 5. 판정 로직 (④ 구조화 = Gemini 2.5 Flash)

한 공고의 raw(텍스트 + 이미지 바이트)를 Gemini에 넣어 아래를 한 번에 산출한다. **AI는 "신호 추출 + 구조화"**를 하고, 최종 확정은 규칙·운영자가 한다. 각 레코드에 **`confidence` ∈ {high, medium, low}**(구조화 전반 신뢰도)를 붙이고, **low는 운영자 우선검토**로 라우팅한다.

### 5.1 게이트 1 — `is_church_recruitment` (3값)
| 값 | 의미 | 처리 |
|---|---|---|
| `YES` | 명백한 개교회 채용 | 게이트 2로 |
| `NO` | 명백한 기관 채용(방송사·선교단체·학교 등) 또는 비채용 글 | **review_data 생성 안 함**(source_data엔 raw 남김 + **`structured_at` 기록** — §4) |
| `UNCERTAIN` | 개교회 여부 애매(원목 등 경계) | **review_data 생성(`confidence=low`)** → 운영자 |

> 값은 다른 enum과 같이 **영어 대문자 key**로 저장한다(불리언처럼 보이는 `true`/`false` 문자열을 쓰지 않는다 — 3값 필드라 타입 혼동을 만든다).

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
4. 근거 없음 → **`denomination=UNKNOWN`** · `denomination_source=unknown`.

- 출력: `denomination` · `denomination_source`(stated/registry/ai_guess/unknown/**operator**) · `denomination_evidence`(원문 근거).
- **`operator`** = 운영자가 검수에서 직접 확정한 값. 위 "승격 전 10키로 해소"가 이 근거로 남는다 — 이 값이 없으면 해소된 행이 "교단은 있는데 근거는 unknown"이 되어 되읽을 때 모순으로 걸린다.
- **공개 가능 판정**: `stated`·`registry`·`operator`만 확정으로 본다. **`ai_guess`는 값이 있어도 운영자 확인 대상**(확정 아님).
- **`UNKNOWN`은 "근거 없음"이다** — ⚠️ **승격 전 해소 규칙은 철회됐다**(2026-08-06). min_job이 `churches.denomination`을 nullable로 바꿨고(NULL=미상 또는 무소속), 실측 교단 명시가 **2.8%**뿐이라 교회 1,006곳을 사람이 채우는 것은 비현실적이다. **미상이면 미상으로 둔다.** 다만 크롤 공고는 `church_id=NULL`이라 지금은 `churches`에 아무것도 쓰지 않는다 — 교단은 교회가 claim한 뒤 그 교회가 채운다. `denomination_source`가 `stated`·`registry`·`operator`일 때만 확정으로 표시한다.
- `UNKNOWN`(근거 전무) vs `ETC`(식별됐으나 화이트리스트 밖)는 다르다 — review 단계 구분용. **저장값은 영어 key로 통일**(한글 저장 금지).
- **노회는 저장하지 않고 매핑표도 만들지 않는다**(min_job 스키마에 노회 없음). 노회명은 위 3순위(AI 추정)의 근거로만 쓴다. → 표 유지비 0, 대신 검수 의존↑(운영자가 확정).
- ⚠️ **`raw_text`만 보면 안 되는 소스가 있다 — `CSU`.** 총신대 게시판은 공고를 폼으로 받아 값을 `properties`에 담고, 본문(`body`)은 **없거나 포스터 이미지 한 장뿐일 수 있다**(실측 40건 중 2건). 그 필드가 `raw_meta`에 그대로 있고 **교단(`order_name`)·교회명·지역·주소·자격(`certification`)·지원서류(`apply_documents`)·사례·모집인원·연락처가 전부 텍스트로** 들어 있다 → CSU는 `raw_meta`를 근거로 `stated` 확정이 가능하고, **포스터 OCR보다 이 값이 정확하다**. 구조화는 `raw_text`가 비었을 때 `raw_meta`를 읽어야 한다(빈 `raw_text`를 "내용 없음"으로 보면 안 된다).

### 5.4 이단 스크리닝 — **자동 거부**(근거 기록 · 낙인 금지)
- 참고 목록 **`config/heresy-ref.json`**(교회명·교단명·키워드 + 근거 URL, 사람이 관리)과 공고의 교회명/교단을 대조.
- 매칭 시 **`heresy_flag=true` + `heresy_evidence`(어느 항목에 걸렸는지)** + **`review_status=REJECTED`** → 검수 큐에 뜨지 않고 승격되지 않는다(운영자 결정 2026-08-06 · 앞선 "플래그만"에서 변경).
- ⚠️ **정확 일치만 쓴다.** 부분 문자열 매칭은 실측에서 48건이 걸렸고 **대부분 이름만 겹친 다른 교회**였다: `송도한마음교회` ⊃ `한마음교회`(춘천) · `경주동방교회` ⊃ `동방교` · `남부산제일교회` ⊃ `부산제일교회` · `김포행복한교회` ⊃ `행복한교회`(인천). 목록 원문에도 지역이 함께 적혀 있지만(`김성로(춘천 한마음교회)`) 목록 자체엔 지역 필드가 없다.
- ⚠️ **근거는 반드시 남긴다**(운영자 검수는 안 하더라도). 없으면 "왜 이 교회 공고가 없지?"에 답할 수 없고 오판을 되돌릴 근거도 없다.
- ⚠️ **플래그 ≠ 이단 판정.** 목록은 "어느 교단이 언제 무엇이라 했다"의 모음이고 교단마다 판단이 다르거나(`김성로` 합동 참여금지 / 기침 문제없음) 해제된 건도 있다(`이명범` 통합 2021 이단 해지).
- ⚠️ **공개 이단 낙인·자동 삭제는 여전히 금지**. 공개하지 않는 것과 "이단이다"라고 표시하는 것은 다르다. 레코드는 남긴다.
- ⚠️⚠️ **`config/heresy-ref.json`은 커밋하지 않는다**(`.gitignore`). 이 리포는 공개이고 목록에 **실명 122건**이 이단·이단옹호자로 적혀 있다. Supabase 전환 시 DB로 옮기고 RLS 운영자 전용.

### 5.4b 끝난 공고 — **자동 거부**(2026-08-11)
- 공고가 스스로 끝났다고 말하면 `review_status=REJECTED` + `reject_reason=CLOSED`로 **만들면서 거절**한다. 그대로 두면 `jobs.status` 기본값이 `OPEN`이라 **이미 채워진 자리가 공개된다**.
- ⚠️ **판정 근거는 게시판 상태 필드와 제목에 명시된 것만**이다(실측 110건: 상태 필드 80 · 제목 30). 본문의 `서류는 채용 완료 후 폐기합니다`·`초빙 완료 시까지`는 **안내 문구이지 마감이 아니다** — 본문까지 세면 370건이 걸리고 대부분이 오탐이라 되돌릴 수 없는 손실이 된다.
- 레코드와 근거는 남긴다(이단과 같은 취급 · §5.4). 삭제하지 않는다.

### 5.5 지원 연락처 (contact) — 공개
- 공고에 **지원용으로 명시된 연락처**(전화·이메일·지원 링크)를 `contact`로 추출 → 승격 시 공개(지원 경로 제공).
- 본문에 우연히 있는 **무관한 제3자 개인정보는 추출하지 않는다**(프라이버시 취지 유지).
- ⚠️ 이는 min_job의 기존 방침(개인 담당자 연락처 노출 금지)을 **정제**한 것이다 — "교회가 지원받으려 공개한 연락처는 공개"로 갱신(min_job 스키마·정책 변경 pending §8).

### 5.6 나머지 필드
min_job `jobs` 미러(title·position·role·department·employment_type·qualification·headcount·start_timing·housing_provided·housing_note·pay_*·benefit_note·work_days·requirements[]·preferred[]·required_docs[]·optional_docs[]·process_steps[]·description·posted_at·deadline·**연락처 4컬럼**) + 교회 초안(church_name·region·city)을 raw에서 추출. `raw_text`(원문 전체)는 항상 보존.

---

## 6. 데이터 모델

크롤러 **staging 4테이블**(Supabase) + **참고 config 1**(GitHub JSON) + **승격 목적지 2테이블**(min_job DATA.md).

> ⚠️ **시각 컬럼은 KST 표기(`+09:00`)로 저장한다**(운영자 결정 2026-08-05). `Z`(UTC)와 같은 순간이고 Postgres `timestamptz`도 동일하게 저장하지만, 운영자·게시판·공고가 모두 한국 시간을 쓰므로 사람이 파일·로그를 열었을 때 바로 읽히는 쪽을 택했다. **오프셋을 떼면 안 된다**(naive는 서버 시간대로 해석된다). `date` 컬럼(`posted_on`·`deadline`·`last_cutoff`)은 시간대가 없어 변환 대상이 아니다.

### ① `source_data` — 원자료 + 원장 (불변 · write-once · 누적)

> **write-once 예외**: 운영자 **opt-out**(교회 요청)·법적 삭제 요청은 삭제/마스킹이 가능해야 한다(CLAUDE.md). 그 외 일반 경로에서는 갱신하지 않는다.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| `id` | uuid PK | |
| `source_key` | text | 게시판 id (대문자 enum 값: `YTUS`·`PUTS`… §10) |
| `external_id` | text | 소스 내 유일 글 식별자(§4) |
| `source_url` | text | 원문 링크 |
| `run_id` | uuid FK→crawl_run | ①에서 INSERT된 crawl_run 참조 |
| `fetched_at` | timestamptz | |
| `raw_text` | text | 확보 텍스트(이미지형은 얇거나 빈 것이 **정상** — config `image_only`) |
| `raw_html` | text | **구조만 남긴 본문 HTML**(2026-08-05 추가). `raw_text`를 대체하지 않는다 — 구조화는 `raw_text`를 읽고, 이 값은 **나중에 필요해진 것을 재수집 없이 뽑는 자리**다. ⚠️ 텍스트만 남기면 **링크의 `href`·표의 행열 대응·항목 경계가 사라진다** — 그래서 하루에 세 번 재수집을 원했다(DAESHIN 첨부·HANIL 링크·`href` 전량). `church_links`(HOMEPAGE·YOUTUBE·BAND…)는 `href` 없이 채울 수 없고, `type`이 NOT NULL이라 링크 **라벨**도 필요하다. **남기는 것**: 태그 구조 · `href`·`src`·`alt`·`colspan`/`rowspan`. **버리는 것**: `style`·`class`·`id`·`data-*`·`on*`·`<script>`·HTML 주석·꾸밈 태그 껍데기 (`span`·`font`·`o:p`) · **`data:` 이미지 바이트**(`image_urls`가 이미 갖고 있어 두 번 저장하지 않는다). 실측: 원본 평균 11KB → **797B**(93% 절감 · 3,181건 = +2.4MB). 본문 컨테이너가 없는 소스는 빈 문자열(`PCKWORLD` — 상세가 포스터 한 장) |
| `title` | text NOT NULL | 게시판 목록의 제목 **그대로**(`review_data.title`은 여기서 `(끌어올림)`류 머리표만 뗀 값이다 — 모델을 거치지 않는다). 별도 컬럼인 이유: `raw_meta`에 묻으면 운영자가 원자료 표에서 무슨 공고인지 못 보고, 원장 대조에서 Store가 어댑터 키 이름을 알아야 한다 |
| `posted_on` | date NOT NULL | 게시일. **백필 컷오프의 유일한 기준**(§4) — 구조화 전이라 `review_data.posted_at`이 없다. ⚠️ **NOT NULL이다**(2026-08-14) — 없으면 min_job이 "게시일+N개월 자동 만료"(§9)를 적용할 수 없다. 목록에 날짜 칸이 없는 소스는 **어댑터가 다른 근거로 채운다**(`PCKWORLD`는 썸네일 파일명) · 그것도 없으면 `collect`가 수집일로 둔다. ⚠️ 그런 소스는 그 값을 **컷오프에 쓰지 않는다**(config `list_has_dates: false`) — 근거가 게시판이 발행한 날짜가 아니라 관례라서, 조용히 바뀌면 공고가 잘려 나간다 |
| `image_urls` | text[] | **본문에 인라인으로 박힌** 이미지 URL. 구조화 직전 바이트 fetch용(§3). raw_meta에 섞지 않고 **별도 컬럼**. ⚠️ **`data:` URI가 들어올 수 있다**(2026-08-04 실측 · CALVIN은 본문 텍스트가 0자이고 내용이 인라인 `data:image/png;base64` 약 150KB 한 장이다) → 구조화는 **스킴을 보고 갈라야 한다**: `http(s)`는 fetch, `data:`는 **fetch하지 말고 그대로 디코드**한다. 이걸 URL로 취급해 요청하면 그 게시판 전체가 실패한다 |
| `attachments` | jsonb | **첨부파일 전부** `[{name, url}]`. 이미지만이 아니라 HWP·PDF도 담는다 — 원문 증거를 최대한 남긴다(2026-08-04 추가). 구조화는 `is_image`인 것만 Gemini에 보내고, 못 읽는 형식은 URL만 검수로 넘긴다(사람이 열 수 있다). ⚠️ **URL을 그대로 저장한다** — 인코딩하지 않는다(2026-08-05 실측). 첨부 URL에 **공백이 들어 있고**(`…/서식1. 이력서 및 개인정보 수집동의서_ts….hwp`) **한글이 NFD로 분해**돼 있는 게시판이 있다(KAICAM). 정규화하면 저장 경로와 어긋나 404가 된다 → **바이트를 받는 쪽(구조화)이 요청 직전에 경로를 quote**해야 한다. `name`은 표시용이라 NFC로 정규화하지만 `url`은 손대지 않는다. ⚠️⚠️ **바이트를 받기 전에 그 공고 상세를 같은 세션에서 먼저 GET해야 하는 게시판이 있다**(2026-08-05 실측 · 그누보드 계열 4곳 `HAPSHIN`·`HTUS`·`PCK`·`SUNGKYUL`). 그러지 않으면 파일 대신 `잘못된 접근입니다` HTML이 온다 — 그누보드가 `wr_id`별 세션 표시를 남기고 `download.php`가 그걸 검사한다. **목록을 보는 것도, 다른 글을 보는 것도 안 된다**(실측). 나머지(`KBTUS`·`KWANGSHIN`·`MTU`·`SJS`·`UHS`)는 세션 없이 받힌다. → 구조화는 **소스별 세션을 유지한 클라이언트로 `상세 → 파일` 순서**로 요청한다. ⚠️ **파일명을 함께 저장**한다 — 다운로드 URL에 파일명이 없어(`/download/…/57439f…`) 이름 없이는 종류를 알 수 없다 |
| `raw_meta` | jsonb | 작성일·조회수·첨부·게시판 원필드(비정형) |
| `structured_at` | timestamptz NULL | ⭐ **판정 완료 시각**(게이트1 YES·NO **둘 다** 기록). **실패 시 NULL 유지** → 재구조화 대상(§4). 이 컬럼이 "제외됨"과 "실패함"을 구분한다 |
| `structure_attempts` | int DEFAULT 0 | 구조화 시도 횟수. 상한(3) 초과분은 재시도 제외·운영자 리포트(§4) |
| `last_structure_error` | text NULL | 마지막 실패 원인. 상한 초과 리포트가 "왜 실패했나"를 말할 수 있게(§4) |
| `content_hash` | text NULL | *Phase 후반(수정감지)용 — MVP 미채움(§9 리비전 방식)* |
| — | **UNIQUE(`source_key`,`external_id`)** | 원장(증분·중복 방지) |

### ② `review_data` — 구조화 초안 + 검수 (가변 · 누적)
| 그룹 | 컬럼 |
|---|---|
| 링크 | `id` PK · `source_data_id` FK · `run_id` FK(**`source_data.run_id` 승계** — 구조화는 자기 `crawl_run`을 만들지 않는다 §2) · **`source_url` NOT NULL**(`source_data.source_url` 복사) |
| 분류(게이트) | `is_church_recruitment`(YES/NO/UNCERTAIN — NO는 여기 안 옴) · `job_kind`(MINISTRY/GENERAL) · `role`(GENERAL용) |
| 공고(jobs 미러) | `title`·`position`·`department`·`employment_type`·`qualification`·`headcount`·`start_timing`·`housing_provided`·`housing_note`·`pay_min`·`pay_max`·`pay_note`·`pay_period`·`benefit_note`·`work_days`·`requirements[]`·`preferred[]`·`required_docs[]`·`optional_docs[]`·`process_steps[]`·`description`·`posted_at`·`deadline` |
| 지원 연락처 | `contact_email`·`contact_tel`·`contact_link`·`contact_post` — **방법별 4컬럼**(min_job `APPLY_METHODS`가 `ETC` 없는 닫힌 4키라 1:1 대응 · 승격이 파싱 없이 INSERT). ⚠️ 대표 문자열 `contact` 하나로 두던 설계는 철회됐다(2026-08-05) |
| 교회 초안 | `church_name`·`region`·`city` |
| 교단 | `denomination`(`UNKNOWN` 가능·임시) · `denomination_source`(stated/registry/ai_guess/unknown) · `denomination_evidence` · `raw_denomination`(원표기) |
| 이단 | `heresy_flag`·`heresy_evidence` |
| 검수 메타 | `confidence`(high/medium/low) · **`dedup_key`**(§4.1) · `review_status`(PENDING/APPROVED/REJECTED) + **`reject_reason`**(DUPLICATE/HERESY/**CLOSED**/OPERATOR) · **`published_job_id`** FK→jobs(승격 결과 · §4.2가 이걸로 끌어올림을 찾는다) · `reviewed_by` · `reviewed_at` · `created_at`(큐 정렬·감사) |
| 미사용 | ~~`matched_church_id`~~ — 교회 행을 만들지 않기로 해(2026-08-06 · §6 승격 목적지) **채우지 않는다**. 컬럼은 남겨 두되 값은 항상 NULL이다 |

> 게이트1 `NO`(개교회 아님·비채용)는 review_data를 만들지 않는다(§1·§5.1) — 대신 `source_data.structured_at`이 기록돼 재구조화 대상에서 빠진다(§4). `UNCERTAIN`은 confidence=low로 여기 온다.
> ⚠️ **`source_url`을 복사해 둔다**(2026-08-05 추가). 정규화상으로는 `source_data_id`로 JOIN하면 되니 중복이지만, min_job `jobs.source_url`은 **원문 재게시 금지·출처 표기의 핵심 필드**다 — 승격 코드가 JOIN을 잊으면 출처 없이 공개된다. **승격이 이 테이블 하나만 보고 끝나게** 한다(빈 문자열도 거부).
>
> ⚠️ **`reject_reason`은 자동 거부를 되짚는 유일한 통로다**(2026-08-06 추가). 중복(§4.1)·이단(§5.4)·운영자 거절이 전부 `REJECTED` 하나로 뭉치면 "우리 dedup이 틀렸나"·"이단 오판인가"를 확인할 수 없다. 특히 이단은 **검수 큐에 뜨지 않는 자동 거부**라 이유가 없으면 잘못 걸러도 영원히 드러나지 않는다. **불변식**: `REJECTED`면 이유가 있어야 하고, 아니면 없어야 한다(레코드가 강제). 게이트1 `NO`는 여기 없다 — `review_data`를 아예 만들지 않는다. 검수 화면은 이걸로 **[검수 대기] [중복] [이단] [거절]** 탭을 나눈다.
>
> ⚠️⚠️ **마이그레이션에 CHECK 제약을 넣어야 한다** — 이 불변식은 지금 **우리 Python에만** 있다. `review_data`는 min_job admin도 쓰는 테이블이라, admin이 `UPDATE review_data SET review_status='APPROVED'`만 하고 `reject_reason`을 안 지우면 **우리가 그 행을 읽을 때 `SerdeError`가 나고 손상 행으로 건너뛴다**(실측 확인). 행이 조용히 사라진다.
> ```sql
> CHECK ((review_status = 'REJECTED') = (reject_reason IS NOT NULL))
> ```
>
> ⚠️⚠️ **승격 게이트 = 필수 4 + CHECK 2**(min_job DATA.md §3 정본 · 2026-08-05 우리 실측 3,181건으로 8개→6개로 줄였다). 크롤러가 맞춰야 하는 6개: **교회 매칭 · `title` · `job_kind` · 직분(`position`) 또는 직무(`role`) · `description` · 연락처 4컬럼 중 1개**(⚠️ `source_url`은 세지 않는다 — 세면 제약이 항상 참이 되어 무의미하다).
>
> **`denomination`·`region`은 비어도 승격된다** — 게시판이 안 주거나 원문에 없을 수 있다(실측: 교단 명시 2.8% · 지역 81%). AI가 못 뽑을 수 있고 운영자가 채우는 초안이라 nullable이 맞다.
>
> ⚠️ **`posted_at`은 예외로 NOT NULL이다**(2026-08-14). 만료 판정(§9)의 기준이라 비면 그 공고를 언제까지 보여줄지 정할 수 없다. `source_data.posted_on`을 그대로 물려받고, 그쪽이 이미 NOT NULL이다(§6 ①). PCKWORLD 게시일 0%는 **해소됐다** — 썸네일 파일명에서 읽는다(60/60).
>
> **우리 몫**: 위 6개 중 못 채운 것이 있으면 `confidence=low`로 올려 운영자가 먼저 보게 한다. ⚠️ **검수 우선순위는 교단보다 지역**이다 — 지역이 비면 min_job 지역 필터에서 무조건 탈락해 사실상 안 보이는 공고가 된다(교단 미상은 공개해도 지원에 지장 없다).
> **UNIQUE(`source_data_id`)** — 한 원자료당 초안 1개(중복 PENDING 방지). 재구조화 시 기존 행을 교체(upsert)한다. 게시판 default 교단은 `source_key`로 유도 가능(레지스트리)하므로 별도 hint 컬럼을 두지 않는다.

### ③ `source_health` — 게시판별 상태 (약 31행 · 매 실행 UPSERT)
`source_key` PK · `last_run_at` · `first_run_at` · `last_run_id`(→`crawl_run.id`) · `last_status`(OK/FAIL/**EMPTY**) · `last_success_at` · **`last_cutoff`** · **`last_rows`** · `last_new_count` · **`last_posted_on`** · `consecutive_failures` · **`consecutive_empty_runs`** · `total_collected` · `last_error`

> ⚠️ **`EMPTY`는 "목록 행이 0"이다 — "신규 0건"이 아니다**(2026-08-04 정정). 원장 증분이라 조용한 게시판은 신규가 며칠씩 0이고 **그게 정상**이다. 그걸 소프트 실패로 세면 31곳 중 조용한 곳들이 매일 경보를 울려 **경보가 잡음이 되고 정작 깨진 게시판이 묻힌다**. 목록 자체를 못 읽는 것(셀렉터 깨짐·로그인벽 전환)이 진짜 신호다. → `consecutive_empty_runs`가 소프트 실패 경보의 근거다(§7).
>
> ⚠️ **`last_cutoff`(그 실행에 적용한 기간) 없이는 다른 수치를 해석할 수 없다.** 3개월 백필 258행 다음의 데일리 18행이 "급감"으로 보인다.
>
> ⚠️ 누적값(`consecutive_failures`·`consecutive_empty_runs`·`total_collected`)과 `last_success_at`·`first_run_at` 보존에는 **직전 값 읽기가 필요**하다 → Store에 조회(read)가 있어야 한다.
>
> ⚠️ **`last_success_at`은 `OK`에서만 갱신한다.** `EMPTY`에도 갱신하면 목록 0행이 며칠 이어질 때 "마지막 성공"이 계속 오늘로 밀려 **"언제까지는 정상이었나"를 영구히 잃는다**. 실패(FAIL)는 관측값(`last_cutoff`·`last_rows`·`last_new_count`·`last_posted_on`)도 덮지 않고 직전 값을 보존한다 — 0으로 덮으면 FAIL과 EMPTY가 구분되지 않는다.

### ④ `crawl_run` — 실행별 요약 (실행마다 1행 · 누적)
`id` PK · `started_at` · `finished_at` · `mode`(BACKFILL/DAILY) · `sources_ok` · `sources_failed` · `new_count` · `error_detail` jsonb(source_key→에러)
> **실행 시작(①)에 INSERT**(started_at·mode) → 종료(⑤)에 UPDATE(finished_at·집계). run_id는 시작 시 확보돼 source_data/review_data가 참조.
> ⚠️ **수집 실행만 이 행을 만든다.** 구조화를 따로 돌리는 실행(`structure`)은 행을 만들지 않고 `review_data.run_id`로 수집 실행의 id를 승계한다(§2) — 집계가 전부 게시판 단위라 공고 단위 작업이 들어갈 칸이 없다.

### 참고 config — `config/heresy-ref.json` (GitHub, 사람 관리)
`[{ match, type(church_name|denomination|keyword), source_url, note, added_at }]` — 민감 자료라 git 이력=감사 추적.

### 승격 목적지 — `jobs` **한 테이블** (min_job DATA.md 정본)

⚠️ **`churches`에는 쓰지 않는다**(2026-08-06 확정). 크롤 공고는 `church_id = NULL`로 들어가고,
교회명은 `jobs.church_name`(텍스트)이 담는다. 그 교회가 min_job에 가입·인증한 뒤 **자기 공고를
claim하면** `church_id`가 채워진다.

**왜 교회 행을 만들지 않나** — 자동 묶기를 실측하면 95%까지는 되지만 **사각지대가 남는다**:
(교회명+광역) 묶음 1,203개 중 **67개는 일부/전 공고에 연락처가 없어 충돌을 확인할 수 없다**.
`중앙교회(서울)` 두 곳이 있는데 한쪽에 연락처가 없으면 **조용히 합쳐진다**. 두 오류의 무게가
다르다 — **다른 교회를 합치면**(B교회 페이지에 A교회 공고) 되돌리기 어렵고 이미 공개돼 있고,
**같은 교회를 나누면** 중복 행이 생길 뿐이다. 끝까지 밀어 **아예 만들지 않는** 쪽으로 갔다.

**승격 시 우리가 채우는 것(33개)**

| 그룹 | 컬럼 |
|---|---|
| 교회 | `church_id`=**NULL** · `church_name`(NOT NULL) · `region` |
| 분류 | `job_kind` · `position` 또는 `role` · `department` |
| 조건 | `employment_type` · `qualification` · `headcount` · `start_timing` |
| 처우 | `pay_min`·`pay_max`·`pay_note`·`pay_period` · `housing_provided`·`housing_note` · `benefit_note` |
| 지원 | `contact_email`·`contact_tel`·`contact_link`·`contact_post` · `required_docs[]`·`optional_docs[]`·`process_steps[]`·`work_days` · `requirements[]`·`preferred[]` |
| 본문 | `title` · `description`(**요약** · 원문 복제 금지) · `source_url` · `posted_at` · `deadline` |

**min_job이 채우는 것**: `id` · `status`(OPEN) · `source`(OPERATOR) · `featured_tier`(NONE) ·
`created_at` · `updated_at`. ⚠️ `owner_id`는 **컬럼에서 제거됐다**(2026-08-06 · `church_id`로 충분).

**승격 게이트 = 필수 4 + CHECK 2** (min_job DATA.md §3 정본): `church_name`·`title`·`job_kind`·
`description` NOT NULL · `position` 또는 `role` · **연락처 4개 중 1개**(`source_url`은 안 셈).
`region`·`denomination`·`posted_at`은 **비어도 승격된다**.

**승인 시 `review_data`에서 바뀌는 것 — 4개**

```
review_status     PENDING → APPROVED (또는 REJECTED)
reviewed_by       운영자 식별자
reviewed_at       승인 시각
published_job_id  생성된 jobs 행의 id   ← §4.2 끌어올림 판정의 열쇠
```

---

## 7. 배포 · 운영

- **배포 = GitHub Actions**(상시 서버 없음 · 무료 한도). 크롤러 코드 + `.github/workflows/crawl.yml`. ⚠️ **순서 제약**: ephemeral 러너 + JSON 저장 조합은 원장을 매 실행 잃는다 → **Supabase 전환(ROADMAP 1-6) 이후**에만 Actions를 붙인다. 그전까지 실행은 운영자 로컬.
- **트리거**: **매일 07:00 KST**(`cron: "0 22 * * *"` UTC) · `mode=DAILY`. **백필은 로컬 수동**(`mode=BACKFILL`, 최근 3개월).
- **시크릿(GH Secrets)**: Vertex(Gemini) 키 · Supabase service key. 코드/DB에 노출 X.
- **실패 감지**:
  - **하드**(비2xx·타임아웃·000): GitHub Actions 실패(빨간불) + 이메일 알림.
  - **소프트**(200인데 목록을 못 읽음): `source_health`의 `consecutive_empty_runs` ≥ 2 → 경보(셀렉터 깨짐·로그인벽). 연속 실패 ≥ 2 → 경보. 최신 글이 60일 이상 오래되거나 훑은 기간 안에 글이 없으면 **경보가 아니라 참고 정보**(방학처럼 실제로 조용한 시기가 있다). ⚠️ **"신규 0건·급감" 판정은 폐기**(2026-08-04) — 원장 증분에서는 백필 227건 → 데일리 2건이 정상이라 급감이라는 개념 자체가 성립하지 않고, 신규 0건 경보는 조용한 게시판마다 매일 울려 잡음이 된다.
- **대시보드**: min_job admin이 `crawl_run`(최근 실행) + `source_health`(게시판별) + `review_data` PENDING 카운트를 읽어 "오늘 몇 건 추가/실패·언제 긁었나" 표시.

---

## 8. min_job 연동 · 스키마 거버넌스

- **staging 4테이블(`source_data`·`review_data`·`source_health`·`crawl_run`)은 이 리포가 소유·마이그레이션.** 물리적으로 min_job Supabase 프로젝트에 함께 두되(검수·승격 단순), 정의·변경은 이 리포.
- **`churches`/`jobs`는 min_job `DATA.md`가 정본.** 크롤러는 그 모양에 맞춰 승격만.

### min_job 스키마·정책 변경 (2026-07-29 확인 — 앱 레벨은 대부분 반영됨)
1. ✅ `jobs`에 **`job_kind`(MINISTRY/GENERAL)** + **`role`** — min_job `types/domain.ts`에 반영됨. (목록 UI 필터·마이그레이션 SQL은 min_job 소관·진행 중)
2. ✅ `jobs`에 **`contact`** + **정책 갱신**("지원용 공개 연락처는 공개") — min_job `types/domain.ts`·`CLAUDE.md`에 반영됨.
3. ✅ `constants/domain.ts` **`KIJANG` 제거 완료** — min_job 교단 **10키**(9대형+ETC) 확인. → CONTRACT §1의 "11개에서 제거만 하면" 표현은 폐기.
4. ⬜ **마이그레이션 SQL**(`churches`/`jobs` + staging 4테이블)은 미작성 — staging은 **이 리포 소유**(§8 위), min_job 테이블은 min_job 소관.
   ⚠️ staging 마이그레이션에 **반드시 넣을 CHECK**: `review_data`의 `(review_status = 'REJECTED') = (reject_reason IS NOT NULL)` — 이유는 §6 ②.

### 이 리포 문서 갱신 (✅ 완료 2026-07-28 — SPEC 정본에 맞춰 반영)
4. ✅ **`source_key`**: DB 저장은 **대문자 정규화**(`YTUS`)로 규칙 명문화(CONTRACT §4 노트), 문서의 소문자는 가독용 라벨로 유지.
5. ✅ **CONTRACT.md**: §2 hierarchy(`stated`/`registry`/`ai_guess`/`unknown`) · §2b 노회 매핑표 폐기 · §3 "연락처 추출·공개"로 갱신 · §4 모드 A/B 폐기 노트.
6. ✅ **SOURCES.md**: `pckworld`·`koreabaptist` "OCR" 표기 → "Gemini 멀티모달"(§1·§6·범례). **SNAPSHOT** §2·§5·§6·§9.4도 정합.

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
- **`external_id` 유일성은 어댑터가 보장하고, 런타임이 강제한다**(2026-07-30 31곳 감사):
  - 31곳 전부 목록 단계에서 id를 얻을 수 있다(href·onclick·JSON) → **상세 요청 전에 원장 대조가 끝난다.** 상세를 열어야 id를 아는 소스가 생기면 증분이 무의미해지므로, 그런 소스는 별도 설계가 필요하다.
  - **한 페이지 안에서 중복 `external_id`가 나오면 에러로 중단한다.** 지금 확인해도 게시판이 카테고리를 추가하거나 타 게시판 행을 섞으면 그때부터 **조용히 글을 잃는다** — 중복을 에러로 만들어 그날 실행이 실패로 드러나게 한다.
  - ⚠️ **페이지 간 중복은 에러가 아니다**
- **에러 격리는 두 층이다**(2026-08-05 추가): **소스 단위**로 한 게시판 실패가 나머지를 멈추지 않고, **글 단위**로 상세 하나의 실패가 그 게시판을 멈추지 않는다. 후자가 없으면 800행 게시판에서 350번째가 이상할 때 나머지 450건에 **영구히 도달하지 못한다** — 원장이 이미 저장한 것만 건너뛰고 같은 자리에서 또 실패하기 때문이다. 실패한 글은 개수와 사유 표본을 리포트에 남긴다(조용히 넘기지 않는다). ⚠️ **단 전량 실패는 소스 실패다** — 상세를 하나도 못 가져왔으면 어댑터가 깨진 것이므로 "실패 N건·저장 0건" 경고로 흘리지 않는다.
 — 스캔 중 새 글이 올라오면 글이 아래 페이지로 밀려 같은 번호가 두 번 나온다. 정상 현상이므로 `collect`가 실행 단위로 모아 **한 번만 수집**한다(그대로 두면 한 실행에서 상세를 두 번 요청·구조화해 비용이 두 배다).
  - ⚠️ **페이지 안 중복 검사는 "한 페이지"만 본다.** 다른 하위 게시판의 같은 번호가 **과거 실행에서 이미 저장돼 있으면** 중복으로 보이지 않고 "이미 본 글"로 걸러져 유실된다(제목·날짜 대조가 경고를 내지만 차단은 아니다). 그래서 하위 게시판이 섞이는 소스는 **가드에 맡기지 않고 id 자체를 유일하게 만든다**:

  | 목록에 하위 게시판이 섞였을 때 | 처리 |
  |---|---|
  | 섞인 행이 **수집 대상이 아니면** | **버린다.** `PUTS`는 `bd_name=jangshin_jboard04`가 아닌 행(공지에 섞인 `jnotice02`)을 제외. 예상 못한 값은 조용히 버리지 않고 로그로 남긴다 |
  | 섞인 행이 **전부 수집 대상이면** | **복합키** `하위식별자:id`. `HANSEI`는 `catId:artclNo`(목록이 여러 카테고리에 걸쳐 상세 URL을 템플릿화조차 못 한다 = Konnect `artclNo`가 게시판별로 매겨진다는 신호. BU·UHS는 boardId가 고정이라 순수 `artclNo`로 충분) |
  | 섹션이 둘인데 **하나만 필요하면** | **한 섹션만 크롤**해 충돌 자체를 없앤다. `CSU`는 `menu_id=1110`(사역)만. 둘 다 필요해지면 `1110:id` 복합키로 |

  복합키의 위험(글이 카테고리를 옮기면 id가 바뀌어 중복 수집)은 **검수 큐에 두 건으로 드러나고 `dedup_key`가 후보로 묶는다** — 순수 id의 위험(조용한 유실)보다 낫다.
  - **id 재사용 탐지(추가 요청 0건)**: 원장이 "이미 본 글"로 판정할 때 목록의 제목·게시일을 저장된 `raw_meta.list_title`·`list_date`와 비교한다. 다르면 id 재사용 또는 글 수정이므로 **경고 + `source_health` 기록**. 자동으로 새 글 취급하지 않는다(제목만 고친 경우 중복을 만들면 안 됨 — 운영자 판정).
- 공통 로직(fetch·인코딩·샘플 폴백·본문 추출·이미지 바이트 fetch)은 base/common으로. 신규 소스 = 어댑터 1파일 + 레지스트리 등록.
- fetch 계층이 UA·SSL·EUC-KR·rate limit·robots를 흡수(어댑터는 파싱만). 어댑터 분기용 `crawl_mode(A/B)`는 쓰지 않는다 — 교단은 항상 공고에서 판정(§5.3), 어댑터는 `denomination_hint`(참고)만 제공. (SOURCES/CONTRACT의 "모드 B"는 '초교파=공고별 판정' **라벨**로만 잔존.)
