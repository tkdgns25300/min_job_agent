# RUNBOOK — 실행 명령

> 🌐 게시판에 요청 · 💰 Gemini 유료 · 🔴 아직 없는 명령
> venv 활성화 전제(`source .venv/bin/activate`). ⚠️ **CLI를 바꾸면 이 파일도 고친다.**

## 수집

```bash
minjob-ingest collect --source YTUS --dry-run               🌐    파싱 확인 (저장 안 함)
minjob-ingest collect --source YTUS --months 2            🌐    실제 수집 (무료)
minjob-ingest collect --days 14                           🌐    최근 2주만 (반복 실행용)
minjob-ingest daily                                      🔴 🌐💰  매일 (증분 + 구조화)
minjob-ingest status                                     🔴      실행·게시판 상태
minjob-ingest list-sources [KEY]                                 등록 31곳 (요청 없음)
minjob-ingest snapshot --source KEY                         🌐    fixture용 HTML 확보 (어댑터 없어도 됨)
```

`collect` 옵션 — `--source`(기본: 어댑터 있는 전부) · `--months N`(`0`=날짜 무제한) · **`--days N`**(짧은 범위 · `--months`와 함께 못 씀) · `--dry-run` · `--verbose`

💡 **고치고 다시 돌리는 중이면 `--days 14`를 쓴다.** 2개월치는 약 4,000건이라 요청 간격
(1.5s)만으로 80분이 넘는다. 2주면 약 500건·13분이고, **원장이 이미 받은 건을 건너뛰므로**
범위를 나중에 `--months 2`로 넓혀도 받은 것을 다시 받지 않는다.

⚠️ **범위는 `--months`/`--days`가 정한다 — 페이지 옵션은 없다.** (목록에 날짜가 없는 게시판만 예외 — 그 범위는 `config/sources.json`의 `list_page_limit`에 적혀 있다.) 컷오프보다 오래된 페이지에 닿으면 스스로 멈춘다. 내부 안전 상한(100p)에 걸리면 경고가 나오는데, 그건 **게시일 파싱이 깨졌다는 뜻**이다.
⚠️ `--dry-run`은 목록 전체 + **상세 표본 1건**을 요청한다(목록만으론 상세 파싱이 검증되지 않음). 저장·실행기록 없음.
진행 상황은 게시판마다 한 줄에서 실시간 갱신된다(`⋯ 3p · 60행 · 새 글 54 · 저장 16/54`) → 끝나면 그 자리에 리포트. **로그 파일로 넘기면**(`> run.log`) 진행 줄 없이 리포트만 남는다.

게시판별 상태는 `data/source_health.json`에 **게시판당 1행**으로 갱신된다(누적 아님). 요약에 이렇게 나온다:
```
⚠ PUTS  목록 0행 3회 연속 — 셀렉터 또는 로그인벽 확인 (마지막 성공 2026-08-01)
⚠ HANSEI  3회 연속 실패 — HTTP 500
  · CSU  최신 글이 2026-04-12 (114일 전) — 게시판이 조용합니다
```
⚠(경보)는 손을 써야 하고, ·(정보)는 참고다. **신규 0건은 경보가 아니다** — 원장이 이미 본 글을 걸러낸 정상 결과다.

## 구조화 (AI) — 💰 유료 · 🌐 그림이 있는 공고는 게시판에 요청한다

수집해 둔 `source_data`를 Gemini로 읽어 검수 초안(`review_data`)을 만든다.

⚠️ **텍스트 공고는 게시판에 요청하지 않지만, 그림이 있는 공고는 요청한다** — 포스터 바이트를 받아 모델에 함께 보낸다(실측 237건 · 7.4%). 첨부 그림은 상세 페이지를 먼저 한 번 더 부른다(그누보드 4곳이 세션을 확인한다). 간격·재시도는 수집과 같은 정책이다.

✅ **저장하면 중복 묶기가 자동으로 이어진다**(아래 중복 묶기 · 무료). `--dry-run`에서는 돌지 않는다.

```bash
minjob-ingest structure --dry-run --limit 1 --source DAESHIN   💰  1건만 화면으로 확인 (저장 안 함)
minjob-ingest structure --limit 1                             💰  1건 실제 저장
minjob-ingest structure --limit 20 --source YTUS              💰  한 게시판 20건
minjob-ingest structure --all --workers 8                     💰  전량 (게시판 8곳씩 동시)
```

⚠️ **저장이 연속 5번 실패하면 실행이 스스로 멈춘다.** 원장이 통째로 깨지면 글 단위 격리가 독이 되어 공고마다 부른 뒤 저장에 실패한다 — 그대로 두면 수천 번 과금하고 아무것도 안 남는다. 흩어진 손상 행 몇 개로는 멈추지 않는다(성공 한 번이 누적을 지운다).

⚠️ **`--limit N` 또는 `--all` 이 없으면 실행을 거부한다.** 유료 호출이 옵션 없이 전량으로 도는 경로를 두지 않았다.

⚠️ **`config/heresy-ref.json`이 없으면 시작 전에 멈춘다.** 이단 대조는 검수 초안을 만들기 전에 끝나야 하고(SPEC §5.4), 목록 없이 돌면 이단으로 규정된 교회의 공고가 검수 큐에 그대로 올라간다. 이 파일은 **실명 자료라 커밋하지 않으므로**(`.gitignore`) 새 컴퓨터에서는 따로 받아 둔다. 실행 머리에 `이단 목록  heresy-ref.json  122건`이 찍히면 읽힌 것이다.

`structure` 옵션 — **`--limit N`**(판정할 건수 = 유료 호출 상한) · **`--all`**(남은 전부) · **`--dry-run`**(호출은 하되 저장 안 함) · `--source KEY` · **`--workers N`**(동시에 돌릴 게시판 수 · 기본 4) · `--verbose`(호출 로그) · **`--out FILE`**(결과를 JSON으로 — 모델 비교용) · **`--lite`**(값싼 모델로)

### 얼마나 빨라지나 — `--workers`

**게시판 간에만 동시에 돈다. 한 게시판 안은 언제나 한 건씩이다**(같은 게시판에 요청 두 개를 흘리지 않는다).

```
--workers 1     5시간 반      순차
--workers 8       50분
--workers 16      25분
```

⚠️ **전체 시간의 하한은 가장 큰 게시판 하나가 정한다**(`CSU` 731건 ≈ 1.6시간). 워커를 30으로 올려도 이 하한은 줄지 않고, 마지막 한 시간은 큰 게시판 혼자 돈다. **큰 곳은 따로 먼저 돌리는 편이 빠르다**:

```bash
minjob-ingest structure --source CSU --all &
minjob-ingest structure --source PUTS --all &
wait
minjob-ingest structure --all --workers 8     # 나머지 28곳
```

⚠️ **`--workers`를 올리기 전에 Vertex 분당 요청 한도를 본다.** 넘기면 429가 오고 SDK가 기다렸다 다시 걸어 **결국 한도만큼만 나간다** — 숫자만 키우면 오히려 느려진다. 8에서 시작해 429가 없으면 올린다(`--verbose`로 보인다).

⚠️ **`--source`를 주면 게시판이 하나라 `--workers`는 아무 일도 하지 않는다.** 그때는 셸에서 게시판별로 나눠 띄운다(위 예시).

⚠️ **`--out`은 연락처가 담긴다.** 커밋되지 않는 **`data/preview/` 아래**에 쓴다(`--out data/preview/오늘/CSU.json`). ⚠️ `data/` 루트에는 **원장 4개만** 둔다 — 미리보기가 섞이면 어느 파일이 진짜 데이터인지 알 수 없다.

```
data/
├── source_data.json · review_data.json · source_health.json · crawl_run.json   ← 원장
├── preview/   `--out` 결과·로그 (아무 때나 지워도 된다)
└── backup/    원장 백업
```

### 모델 두 개 — 기본과 `--lite`

```
옵션 없음    VERTEX_MODEL        기본. 품질이 필요한 실제 적재는 이쪽.
--lite       VERTEX_MODEL_LITE   값싼 대안. 입력 1/5 · 출력 1/3.6 가격.
```

⚠️ **`--lite`인데 `VERTEX_MODEL_LITE`가 비어 있으면 실행이 멈춘다.** 비싼 모델로 조용히 도는 것보다 낫다 — 20건이면 4배 차이다.

실행할 때마다 **어느 모델로 도는지 화면에 찍히고 `--out` 파일에도 `model`로 남는다.** 두 파일을 견줄 때 이름만 믿지 않아도 된다.

### 두 모델 견주기

⚠️ **`--limit`은 오래된 것부터 세므로 게시판을 지정하지 않으면 한 곳만 나온다**(실측: 앞의 40건이 전부 `DAESHIN`). 게시판을 섞으려면 `--source`를 게시판마다 준다.

```bash
# 게시판 3곳 × 10건 × 모델 2개 — ⚠️ --dry-run 이라 판정이 안 남아 두 모델이 같은 공고를 본다
for K in PCKWORLD CSU PUTS; do
  minjob-ingest structure --source $K --limit 10 --dry-run        --out data/preview/flash-$K.json
  minjob-ingest structure --source $K --limit 10 --dry-run --lite --out data/preview/lite-$K.json
done

for K in PCKWORLD CSU PUTS; do
  .venv/bin/python scripts/compare_models.py data/preview/lite-$K.json data/preview/flash-$K.json
done
```

칸별로 **몇 번 갈렸는지**가 먼저 나온다. 어느 쪽이 맞는지는 말하지 않는다 — 갈린 공고만 원문과 대조하면 된다.

💡 **게시판을 섞는다.** 게시판마다 형식이 달라 한 곳만 보면 다른 곳에서 깨지는 것을 못 본다(`PCKWORLD`는 60건 전부 그림·PDF, `CSU`는 게시판 필드형, `PUTS`는 본문형이면서 최다).

⚠️ **`--dry-run`을 빼면 비교가 성립하지 않는다.** 판정이 찍혀 두 번째 모델이 같은 공고를 보지 못한다(`structured_at`은 앞으로만 간다).

**`--limit`은 "판정한 건수"를 센다** — 훑은 건수가 아니다. Gemini를 부르지 않는 공고(빈 공고 등)는 세지 않으므로 `--limit 20`은 항상 **호출 20회 이하**다. 리포트의 `훑음`이 `--limit`보다 큰 것은 정상이다.

💡 **프롬프트를 다듬는 중이면 반드시 `--dry-run`.** 저장하면 그 공고에 판정이 찍혀 **다시 나오지 않으므로**, 프롬프트를 고쳐도 같은 표본으로 비교할 수 없다. `--dry-run`은 아무것도 저장하지 않아 같은 20건을 계속 볼 수 있다.

💡 **게시판을 바꿔가며 본다.** 미판정 목록이 수집 시각 순이라 `--source` 없이 돌리면 오래된 한두 게시판만 나온다. 프롬프트 **하나로 30곳**을 덮어야 하므로 한 곳 형식에 맞추면 나머지가 깨진다.

리포트는 이렇게 나온다:
```
  훑음            22건
  초안            20건   검수 대기 4건(PENDING)
    ↳ 자동 승인    15건   사람을 거치지 않고 공개된다
    ↳ 자동 거절     1건   HERESY 1 — 검수 큐에 뜨지 않는다
  제외             2건   개교회 채용이 아님 — 초안 없음
  검산에서 비움      3칸   raw_denomination 1 · contact_link 1 · contact_post 1
  본문 확인 못 함    2칸   그림·PDF 공고 — 포스터가 원문이라 비우지 않았습니다
  원문을 이어 쓴 칸  37칸   제출서류·처우처럼 여러 조각을 잇는 칸 — 잘못이 아닙니다
  ⚠ 그림을 못 읽고 텍스트만으로 판정한 공고 1건 — 포스터 공고면 내용을 못 본 채 판정된 것입니다.
      DAESHIN/1234: 그림 1/1장을 못 읽음: poster.png: HTTP 404
```
| 줄 | 뜻 |
|---|---|
| **초안** | `review_data`에 들어갔다. **전부 검수 대기는 아니다** — 옆의 숫자가 사람이 볼 건수다 |
| **자동 승인** | 확인할 것이 없어 **사람을 거치지 않고 승인됐다**(SPEC §5.7). 실측 52%가 여기 들어간다(1주치 전량 473건 · 2026-08-23). ⚠️ 이 수가 갑자기 튀면 규칙이 느슨해진 것이다. (Supabase 전환 뒤에는 이 행들이 `jobs`로 자동 공개된다 — SPEC §4.3) |
| **자동 거절** | 이단·마감이라 걸렀다. 초안은 남지만 검수 큐에 뜨지 않는다(SPEC §5.4) |
| **제외** | 개교회 채용이 아니라고 판정했다(초안 없음). **실패가 아니다** |
| **빈 공고** | 본문·이미지·첨부가 전부 없어 호출하지 않았다 |
| **검산에서 비움** | 모델 답이 원문에 없어 **그 칸만 비웠다**. 공고는 남는다. ⚠️ 이 숫자가 늘면 프롬프트를 봐야 한다 |
| **본문 확인 못 함** | 그림·PDF 공고라 대조할 글자가 없다. 비우지 않았으니 **운영자가 먼저 본다** |
| **원문을 이어 쓴 칸** | 프롬프트가 조립을 시킨 칸(제출서류·처우·모집인원)이 원문과 다르다. **정상이다** — 프롬프트를 고쳤을 때 이 숫자가 어떻게 움직이는지만 본다 |
| **그림 대기** | 그림을 가져올 수단 없이 실행됐다(프로그램 경로 전용 · CLI에서는 나오지 않는다) |
| **실패** | 판정을 남기지 않았다 → 다음 실행이 다시 시도한다(상한 3회) |

⚠️ **`그림을 못 읽고 텍스트만으로 판정한 공고 N건`** 경고가 뜨면 눈여겨본다 — 포스터 공고면 내용을 못 본 채 판정된 것이고, 판정은 되돌릴 수 없다.

⚠️ **한 번 판정된 공고는 되돌릴 수 없다**(`structured_at`은 앞으로만 간다). 되돌리려면 `scripts/reset_structure.py`를 쓴다(아래 되돌리기).

## 중복 묶기 — 무료 · 게시판에 요청하지 않는다

같은 자리가 여러 게시판에 올라오고(교차게시) 같은 게시판에 다시 올라온다(끌어올림). 실측 반복이 **약 42%**다 — 그대로 두면 공개 목록 절반이 같은 공고다.

**`structure` 뒤에 자동으로 돕니다.** 따로 칠 필요가 없고, 아래 명령은 **다시 돌릴 때** 쓴다.

```bash
minjob-ingest dedup --dry-run    무엇이 묶이는지만 본다 (저장 안 함)
minjob-ingest dedup              판정하고 저장한다
```

⚠️ **몇 번을 돌려도 같은 결과다.** 지난 실행이 내린 중복 판정도 매번 처음부터 다시 보므로, **규칙을 고쳐 다시 돌리면 잘못 거절한 공고가 되살아난다.** 그래서 되돌리기 스크립트가 따로 없다.

리포트는 이렇게 나온다:
```
  훑음           132건
  중복            21건   17개 자리로 줄었다 — 검수 큐에 뜨지 않는다
    ↳ 판단 못 함    2건   부서가 여럿이거나 접수 이메일이 갈렸다 — 사람이 본다
  혼자            84건   같은 자리가 없다
  견줄 수 없음      2건   교회명·지역·직분 중 하나가 비었다
  이미 결론         2건   이단·마감·운영자 거절 — 건드리지 않는다
  저장           128건 갱신
```
| 줄 | 뜻 |
|---|---|
| **중복** | 같은 자리의 재게시라 거절했다. **검수 큐에 뜨지 않는다** — 이 수가 갑자기 튀면 규칙을 봐야 한다 |
| **판단 못 함** | 부서가 **여러 값으로 갈렸거나** 접수 이메일이 서로 다르다 — 같은 자리인지 코드가 알 수 없다. **대표는 그대로 공개되고 나머지만** 검수 대기로 온다 |
| **견줄 수 없음** | 교회명·지역·직분 중 하나가 비어 아무와도 견주지 않았다. ⚠️ **중복이 남는 것보다 다른 교회를 합치는 것이 훨씬 나쁘다** |
| **이미 결론** | 이단·마감으로 이미 거절된 공고다. 마감된 글을 대표로 세우면 살아 있는 같은 자리 공고가 그 밑에 묻힌다 |

⚠️ **이미 공개된 공고는 대표 자리를 뺏기지 않는다.** 새로 온 같은 자리 글이 중복이 되고, 공개된 행에는 표시만 붙는다.

## 어댑터

게시판 1곳 = 파일 1개(`minjob_ingest/sources/adapters/<key 소문자>.py`). **파일을 놓으면 자동 등록**된다.
현재 **30곳 구현 = 활성 전부**. `HANSEI`는 게시판 소멸로 제외(31곳 등록 중 30곳 활성).

fixture(`tests/fixtures/<KEY>/`)는 **커밋되지 않는다**. 새 컴퓨터에서 어댑터 테스트를 돌리려면 먼저 받아야 한다:
```bash
minjob-ingest snapshot                     🌐  활성 전부 (게시판당 최대 2요청)
minjob-ingest snapshot --source YTUS       🌐  한 곳만
```
테스트 요약 맨 아래에 `어댑터 fixture 커버리지: N/30 검증`이 찍힌다 — 이 숫자가 낮으면 초록불이어도 검증이 건너뛰어진 것이다.

## 되돌리기·이관 — `scripts/` (CLI 명령이 아니다)

```bash
.venv/bin/python scripts/reset_structure.py --source PCKWORLD   # 무엇을 되돌릴지만
.venv/bin/python scripts/reset_structure.py --all --write       # 판정을 지워 다시 구조화 가능하게

# 공고 몇 건만 (게시판이 섞여도 된다 · --write 없이 먼저 확인)
.venv/bin/python scripts/reset_structure.py --posting BU/58590 --posting PCK/5540 --write
```

💡 **`--posting`을 쓰는 이유**: 결함 하나를 고치고 그 영향을 받은 몇 건만 다시 판정할 때, 게시판 단위로 되돌리면 **유료 호출이 게시판 크기만큼** 나간다(BU 3건을 되살리려면 40건을 다시 부른다).

⚠️ **전량 저장 전에 이게 있어야 한다.** `structured_at`은 앞으로만 가서, 수천 건을 저장한 뒤 프롬프트 문제를 발견하면 고친 것을 적용할 방법이 없다.

⚠️ **되돌아오는 것과 지키는 것** — 크롤러가 내린 거절(중복·이단·마감)은 **되돌아온다**(규칙을 고쳐 다시 판정할 수 있어야 하니까 · 2026-08-19). 지키는 것은 **운영자가 손댄 행**(값을 고쳤거나 승인·거절)과 **이미 `jobs`에 공개된 행**이다(2026-08-23에 좁혔다 — 그전에는 `APPROVED` 전부를 지켜서, 공개되지 못한 초안이 갇혀 고친 규칙이 영영 적용되지 않았다). 건너뛴 목록을 화면에 찍고, 읽을 수 없는 초안이 하나라도 있으면 **아무것도 쓰지 않고 멈춘다**.

💡 **중복 판정은 되돌릴 스크립트가 없다** — `minjob-ingest dedup`을 다시 돌리는 것이 되돌리기다. 지난 판정도 매번 처음부터 다시 보므로 잘못 거절한 공고가 되살아난다(위 중복 묶기).

```bash
.venv/bin/python scripts/migrate_posted_on.py --write   # 옛 파일(version 1) → version 2
```

```bash
.venv/bin/python scripts/drop_matched_church_id.py --write   # 없어진 칸을 원장에서 뗀다
```

⚠️ **이미 돌렸다**(2026-08-20 · 694행). `matched_church_id`를 삭제했는데(claim은 min_job이 `jobs.church_id`에 쓴다) 저장된 행에 키가 남아 있으면 **잉여 컬럼으로 거부돼 원장을 통째로 읽을 수 없다**.

⚠️ **`data/` 파일이 version 1이면 모든 명령이 거부한다.** 게시일이 필수가 되면서(2026-08-14) 옛 파일을 그냥 두면 그 공고가 수집도 구조화도 안 되는 **유령**이 되기 때문이다. 이 스크립트가 `PCKWORLD` 게시일을 썸네일 파일명에서 채우고 버전을 올린다(게시판에 요청하지 않는다).

## DB 스키마 — 한 번만 (Supabase)

```bash
# 붙여넣는 순서가 정해져 있다
#  1) ../min_job/supabase/migrations/20260820231650_init.sql        (7테이블)
#  2)   supabase/migrations/20260820234505_init.sql                 (staging 4테이블)
#  3)   supabase/migrations/20260822010000_review_page_columns.sql  (검수 화면용 칸 2개)
```

Supabase 대시보드 → SQL Editor에 **위 순서로** 붙여넣는다. 2번을 먼저 넣으면 `jobs`가 없어서 실패한다.

⚠️ **RLS·GRANT는 아직 없다**(ROADMAP 1-6). 지금 스키마는 테이블·제약·인덱스뿐이다 — 정책 없이 RLS를 켜면 min_job admin 검수 화면이 통째로 빈 화면이 된다.

⚠️ **나중에 `supabase db push`로 옮길 때** 손으로 붙여넣은 것을 이력에 등록해야 두 번 올라가지 않는다:
```bash
supabase migration repair --status applied 20260820234505
```

⚠️ **이 리포에서 `supabase db diff`를 쓰지 말 것.** min_job과 프로젝트를 공유하는데 diff는 상대 리포의 마이그레이션을 몰라서 `DROP TABLE jobs`를 만들어낸다.

### 포스터 버킷 — 한 번만

검수 화면이 포스터를 띄우려면 Storage 버킷이 있어야 한다(`docs/REVIEW_PAGE.md` §7.1). **코드가 만들지 않는다** — 한 번 하는 일이라 실패 경로를 코드에 늘리지 않는다.

Supabase 대시보드 → Storage → **New bucket**:

| 항목 | 값 | 왜 |
|---|---|---|
| Name | `postings` | 코드가 이 이름을 쓴다(`store/storage.py`). ⚠️ **만든 뒤에는 못 바꾼다** |
| Public bucket | **OFF** | 포스터에 담당자 이름·연락처가 있다. 인증 없이 읽히면 그게 그대로 공개된다 |
| Restrict file size | **ON · 8 MB** | 우리 코드 상한(`MAX_MEDIA_BYTES`)과 같은 값. ⚠️ **더 낮추지 말 것** — 스캔 포스터가 5~8MB인 경우가 있고, 낮추면 그 파일만 조용히 안 올라간다 |
| Restrict MIME types | **ON** · 아래 6개 | 우리가 만들 수 있는 형식 전부. 그 밖의 것이 올라가면 버그다 |

```
image/jpeg, image/png, image/webp, image/gif, image/bmp, application/pdf
```

⚠️ **여섯을 다 넣어야 한다.** 우리 코드는 파일 앞머리를 읽어 실제 형식을 판정하므로(헤더를 믿지 않는다) 이 여섯이 실제로 나온다. `jpeg`·`png`만 넣으면 나머지 포스터가 415로 거절된다.

**확인**: `structure`를 돌리면 화면에 `포스터 보관  함`이 찍힌다. 버킷이 없으면 **한 건도 부르기 전에** 멈춘다(유료 호출 낭비 없음).

### 공고 하나를 지울 때 (opt-out · 법적 삭제)

**순서가 정해져 있다** — 거꾸로 하면 FK가 막는다. 막는 것이 안전장치다:

```sql
-- 1) 공개돼 있으면 먼저 내린다. 이게 정작 지워야 하는 것이다
delete from jobs where id = (select published_job_id from review_data where source_data_id = '<id>');
-- 2) 초안 (연락처·교회명·주소 등 추출된 개인정보가 여기 있다)
delete from review_data where source_data_id = '<id>';
-- 3) 원자료
delete from source_data where id = '<id>';
```

⚠️ **`source_data`부터 지우려 하면 실패한다**(`review_data`가 참조 · restrict). 그게 의도다 — `cascade`였다면 초안이 조용히 사라지면서 `published_job_id`도 함께 사라져 **공개된 공고가 아무도 모르게 남는다**.

## 공개 — 승인된 공고를 목록에 올린다

```bash
.venv/bin/minjob-ingest publish --dry-run   # 무엇이 나갈지만
.venv/bin/minjob-ingest publish             # 실제로 jobs 에 넣는다
```

**무료·게시판 요청 없음·멱등**이다. 저장된 판정만 보고 움직여서 몇 번을 돌려도 안전하다.

⚠️ **`dedup`을 먼저 돌려야 한다.** 중복 판정을 안 거친 초안은 공개하지 않고 `판정 안 됨`으로 센다 — 판정 없이 내보내면 같은 자리가 여러 건 올라간다. `structure` 뒤에는 dedup이 자동으로 돌므로 보통 이미 되어 있다.

⚠️ **`dedup`처럼 자동으로 이어 돌지 않는다.** 공개 테이블에 쓰는 유일한 명령이라 운영자가 직접 부른다.

⚠️ **로컬 파일 저장소에서는 안 된다** — `jobs`가 없다. `MINJOB_STORE=supabase`가 필요하다.

리포트 읽는 법:

| 줄 | 뜻 |
|---|---|
| `공개` | `jobs`에 새로 넣은 수 |
| `끌어올림` | 이미 공개된 자리의 날짜를 최신으로 밀었다(계속 올린다 = 아직 뽑고 있다) |
| `교회 것` | claim된 공고라 손대지 않았다 — **실패가 아니다** |
| `링크 비움` | 공개했던 공고가 사라졌다 → 다음 실행이 다시 공개한다 |
| `판정 안 됨` | dedup을 먼저 돌릴 것 |

그리고 `dedup` 리포트의 **`앵커  jobs N행 중 M건`** 을 함께 본다. ⚠️ `N`이 큰데 `M`이 0이면 노출 규칙이 어긋났다는 신호다(0건 자체는 정상일 수 있다 — 전부 마감된 경우).

## 소멸 확인 — 원문이 삭제된 공고 내리기 · 🌐 게시판 요청 · 무료

교회가 게시판에서 글을 지우면 min_job에는 그대로 떠 있다 — 지원자가 "원문 보기"를 누르면
"삭제된 게시물입니다"가 나온다. 이 명령이 목록을 2개월치 훑어 사라진 글을 찾고, **상세를
직접 열어 한 번 더 확인한 것만** 내린다(규칙 정본은 SPEC §4.4).

```bash
minjob-ingest gone --dry-run     오늘 무엇을 내릴지만 본다 (기록도 내리기도 안 함)
minjob-ingest gone               확인하고 내린다 (source_gone_at 기록 + jobs CLOSED)
minjob-ingest gone --source CSU  게시판 하나만
```

**`daily`가 자동으로 돌린다**(수집 다음 · 구조화 앞). 단독 명령은 다시 보고 싶을 때 쓴다.

리포트 읽는 법:

| 줄 | 뜻 |
|---|---|
| `삭제 N건` | 목록에 없고 상세도 죽은 글 — 내린다 |
| `목록에만 없음` | 상세는 살아 있다 — **내리지 않는다**(부산장신·침신대가 이런 게시판) |
| `보류` | 대조군 실패(개편·장애 의심)거나 후보가 30%를 넘었다 — 오늘은 판정 안 함 |
| `마감 정리` | 마감일이 지난 우리 공고를 같은 경로로 내렸다 |

⚠️ **되돌리기**: 잘못 내렸으면 `update jobs set status='OPEN' where id='…';` 후
`update review_data set source_gone_at=null where published_job_id='…';` — 크롤러는 내린 것을
자동으로 되살리지 않는다(운영자가 손으로 닫은 공고를 여는 사고 방지 · ROADMAP "되살아난 공고").

⚠️ **HANIL(한일장신)은 판정하지 않는다** — 상세를 따로 받지 않는 게시판이라 2차 확인이
불가능하다. 비활성 게시판(대신·광신·목원)도 마찬가지로 리포트에 이유가 남는다.

## 하루치 — 🌐 게시판 요청 · 💰 유료

```bash
minjob-ingest daily              수집 → 소멸 확인 → 구조화(중복 판정 포함) → 공개를 한 번에
minjob-ingest daily --dry-run    수집까지만 — 창·새 글 수 확인 (유료 호출 0회)
minjob-ingest daily --limit 200  유료 상한을 낮춰서
```

**cron이 부를 수 있는 창구는 이것 하나다.** 단계별 명령은 사람이 화면을 보며 판단하는 용도다.

| | 규칙 |
|---|---|
| **수집 범위** | 마지막 **성공한** 실행 이후 + 여유 1일 · **7일 상한** |
| **유료 상한** | 하루 500건. 넘친 건은 다음 실행이 이어서 한다(유실 아님) |
| **게시판 일부 실패** | 그냥 통과한다 — 다음 단계는 저장된 사실에서 자기 일감을 다시 찾는다 |
| **판정이 미완이면** | **공개를 건너뛴다**(종료코드 1). 다음 실행이 이어서 한다 |
| **중복 판정** | `structure`가 끝나면 무조건 돈다 — `daily`가 따로 부르지 않는다(전량을 두 번 훑게 된다) |
| **`crawl_run.mode`** | `DAILY`로 찍힌다 — 이게 없으면 "백필 3,700건"과 "데일리 18건"이 구별되지 않는다 |

⚠️ **실패한 실행은 창의 기준이 되지 못한다.** 그걸 기준으로 삼으면 못 가져온 글이 다음 실행의 창 밖으로 밀려 영구히 유실된다. 그래서 실패가 이어지면 창이 **저절로 넓어진다.**

⚠️ **공백이 7일을 넘으면 화면이 알려준다** — 그때는 안내대로 `collect --days N`을 손으로 돌린다. 조용히 7일만 훑고 끝내면 그 앞을 아무도 모른다.

⚠️ **`--dry-run`은 수집까지만 한다.** `structure --dry-run`은 **호출은 하되 저장만 안 하므로**(프롬프트 확인용) 그대로 이어붙이면 "미리보기"가 최대 500건을 과금한다. 그래서 여기서 끊는다 — 미리보기의 목적(창이 맞나·몇 건 잡히나)은 수집만으로 답이 나온다.

💡 **종료코드는 "일을 끝냈나"만 답한다.** 게시판 일부 실패도, 공고 몇 건의 공개 실패도 0이다(다음 실행이 이어받는다). 사람을 불러야 하는지는 아래 `status`가 정한다.

## 현황 — 무료 · 아무것도 쓰지 않는다

```bash
minjob-ingest status              무엇이 문제인지·무엇이 남았는지 한 화면에
minjob-ingest status --runs 10    최근 실행을 10개까지
```

```
── 최근 실행
  08/24 07:00     DAILY  3분 12초  게시판 29곳 성공 · 1곳 실패  신규 18건
    SJS  LedgerConflict: 같은 번호가 다른 글을 가리킴 1건
── 게시판 27곳
  ⚠ SJS  3회 연속 실패 — HTTP 500
── 남은 일
  미구조화        0건
  포기된 행       0건
  검수 대기       76건  min_job 검수 페이지
  미공개 승인     0건
```

**⚠️ 값이 있으면 사람이 손을 써야 하는 것 셋**

| 줄 | 뜻 | 무엇을 하나 |
|---|---|---|
| `끝나지 않은 실행` | 실행이 강제 종료됐다(`SIGKILL`·OOM·러너 타임아웃) | 아래 "남은 일"로 어디까지 됐는지 본다 |
| `포기된 행` | 재시도 3회를 넘겼다 — **스스로 낫지 않는다** | 원인을 고치고 `scripts/reset_structure.py --posting …` |
| `미공개 승인` | 승인됐는데 `jobs`에 없다 — **공개 경로가 막혔다** | `publish`를 돌려 이유를 본다 |

💡 **종료코드가 판정이다.** 사람이 손을 써야 하면 **1**, 아니면 0이다. GitHub Actions가 `daily` 다음에 이 명령을 돌려 그 코드로 성패를 정한다 — 게시판 한 곳이 죽는 것은 정상 상황이라 `daily` 쪽에서 실패로 세면 매일 빨간불이 된다.

⚠️ **조용한 게시판은 경보가 아니다**(SPEC §7). 방학처럼 실제로 글이 없는 시기가 있어 화면에는 보여주되 종료코드에는 넣지 않는다.

⚠️ **구조화 중에 죽은 실행은 "끝나지 않은 실행"으로 안 잡힌다.** `crawl_run`은 수집이 끝날 때 닫히고 구조화는 그 테이블을 만들지 않는다(SPEC §4). 그때는 **미구조화 건수**가 남아 있는 것이 신호다.

## 자동 실행 (GitHub Actions) — 🌐 · 💰

`.github/workflows/crawl.yml` 하나다. 러너에서 `daily`를 돌리고 **`status`의 종료코드가 워크플로의 성공/실패를 정한다.**

```
checkout → python 3.12 → 설치 → 이단 목록을 시크릿에서 러너에 쓴다
        → list-sources (무료) → check-gemini (💰 1회) → daily (🌐💰) → status
```

### 처음 켤 때 — 순서가 있다

**1. 시크릿 8개 등록** — 리포 → Settings → Secrets and variables → Actions → **Repository secrets**의 `New repository secret`. (위쪽 `Environment secrets`는 쓰지 않는다.)

`gh`가 있으면 `.env`에서 한 번에 넣을 수 있다:

```bash
for k in VERTEX_AI_PROJECT_ID VERTEX_AI_LOCATION VERTEX_AI_CLIENT_EMAIL \
         VERTEX_AI_PRIVATE_KEY VERTEX_MODEL SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY; do
  gh secret set "$k" --repo tkdgns25300/min_job_agent \
    --body "$(grep -m1 "^$k=" .env | cut -d= -f2- | sed 's/^"//; s/"$//')"
done
gh secret set HERESY_REF --repo tkdgns25300/min_job_agent < config/heresy-ref.json
```

⚠️ `MINJOB_STORE=supabase`는 비밀이 아니라 워크플로에 평문으로 들어 있다. `VERTEX_MODEL_LITE`는 `daily`가 `--lite`를 쓰지 않아 필요 없다.

**2. 수동으로 한 번** — Actions 탭 → `하루치 수집` → `Run workflow`. 확인만 하려면 `dry_run`을 켠다(유료 호출 0회).

**3. 막힌 게시판이 있나 본다** — 로그와 `status` 출력을 본다. ✅ 2026-08-26 첫 실행에서 **27곳 정상 · 3곳 실패**(`DAESHIN`·`KWANGSHIN`·`MOKWON`)였고, 그 3곳은 **서버가 해외 IP를 거부**해서 config로 우회할 수 없어 제외했다(사유는 `config/sources.json`의 `disabled_reason`).

⚠️ **그 3곳이 필요하면 로컬에서** — 게시판 자체는 살아 있고 한국 IP에서는 정상이다.

```bash
minjob-ingest collect --days 30 --source DAESHIN   # 비활성 소스도 --source면 돈다
```

수집만 하면 다음 `daily`의 구조화·공개가 알아서 이어받는다(`structure`는 `enabled`를 보지 않는다).

**4. 이상 없으면 cron을 켠다** — `crawl.yml`의 `schedule` 두 줄 주석을 푼다.

```yaml
  schedule:
    - cron: "0 22 * * *"   # UTC 22:00 = KST 07:00
```

### 알아둘 것

| | |
|---|---|
| **빨간불 조건** | 죽은 실행 · 게시판 경보 · 포기된 행 · 승인했는데 공개 안 된 행 |
| **빨간불이 **아닌** 것** | 검수 대기(`PENDING`) · 조용한 게시판 · 게시판 일부 실패 — 정상 운영이다 |
| **겹침** | `concurrency: crawl`로 기다린다. **취소하지 않는다** — 구조화는 글마다 저장해서 끊으면 과금한 건이 버려진다 |
| **비용** | 실측 건당 $0.0127 · 하루 평균 37건 → **약 $0.47/일 · $14/월**. Actions 자체는 공개 리포라 무료 |
| **cron 지연** | GitHub 스케줄은 부하에 따라 늦게 뜬다. 창이 "마지막 성공 이후 + 1일"이라 하루 밀려도 빠지는 글은 없다 |
| **이단 목록** | 리포에 없다(공개 리포 · 실명 자료). 시크릿에서 러너 임시 폴더로 쓰고 `MINJOB_HERESY_REF`로 넘긴다. **없으면 유료 호출 전에 멈춘다** |

## 저장소 바꾸기 — 로컬 파일 → Supabase

```bash
# .env 에 세 줄
MINJOB_STORE=supabase
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role 키>
```

명령은 하나도 바뀌지 않는다 — `collect`·`structure`·`dedup`이 그대로 돌고 결과가 DB로 간다.
**어디에 쓰는지는 실행마다 화면에 찍힌다**(`저장소` 줄).

⚠️ **명시하지 않으면 로컬 파일에 쓴다.** `MINJOB_STORE`를 빼면 지금까지와 똑같이 `data/`에 쌓인다 — 키만 넣어 두는 것으로는 넘어가지 않는다(어디에 쌓였는지 모르는 실행을 만들지 않기 위해서다).

⚠️ **오타는 멈춘다.** `MINJOB_STORE=supabse`는 기본값으로 조용히 떨어지지 않고 오류를 낸다.

⚠️ **원장을 이관하지 않는다**(운영자 결정 2026-08-21 · ROADMAP 1-6). 넘어가면 DB는 비어 있고, 2개월 전량을 새로 돌리면서 수집·구조화·중복·공개가 한 번에 된다. 로컬 `data/`는 지우지 말고 비교용으로 둔다.

## 게이트 — 커밋 전 4개 통과

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest -q
```
자동 수정: `.venv/bin/ruff check --fix . && .venv/bin/ruff format .`

⚠️ **워크플로를 고쳤으면 하나 더** — 위 4개는 YAML을 보지 못한다(파이썬만 본다).

```bash
actionlint .github/workflows/crawl.yml     # 없으면 brew install actionlint
```

## 셋업 — 컴퓨터마다 1회

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env         # Vertex 서비스계정 값 입력 (PRIVATE_KEY 개행은 \n)
minjob-ingest check-gemini          # 💰 인증 확인 (VERTEX_MODEL)
minjob-ingest check-gemini --lite   # 💰 VERTEX_MODEL_LITE 도 실제로 부를 수 있나
```

## 주의

- **`data/`를 지우면 원장을 잃는다** → 31곳 전량 재수집 + 전량 재과금. 컴퓨터 옮길 땐 `.env`와 함께 복사(둘 다 커밋 안 됨).
- 캐시(`.mypy_cache`·`.ruff_cache`·`.pytest_cache`·`*.egg-info`)는 지워도 된다.

## 안 될 때

| 증상 | 확인 |
|---|---|
| `command not found: collect` | 하위 명령이다 — `minjob-ingest collect` |
| `one of the arguments --limit --all is required` | `structure`는 범위를 반드시 받는다(유료) |
| 구조화가 `처리할 공고가 없습니다` | 전부 판정됐거나 · 시도 상한(3) 초과 · `--source`에 남은 것 없음 |
| 구조화가 `⛔ 저장이 연속 …번 실패해 멈췄다` | **원장 파일이 깨졌다.** 그 상태로 계속 돌면 공고마다 유료 호출을 하고 저장은 못 한다 → 실행이 스스로 멈춘 것이다. `data/*.json`을 확인하고(백업은 `data/backup/`) 고친 뒤 다시 돌린다 — 남은 공고는 다음 실행이 다시 잡는다 |
| `command not found: minjob-ingest` | venv 활성화 · 또는 `pip install -e ".[dev]"` 재실행 |
| Vertex 설정·PRIVATE_KEY 오류 | `.env` 값과 개행(`\n`) — 메시지가 빠진 변수명을 알려준다 |
| 한 게시판만 0건·실패 | 셀렉터 깨짐 또는 로그인벽. **우회 금지** — 비활성화 후 보고 |

> 저장 위치·필드 = [SPEC](./SPEC.md) §6 · 게시판 설정 = `config/sources.json`
