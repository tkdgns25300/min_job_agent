# RUNBOOK — 실행 명령

> 🌐 게시판에 요청 · 💰 Gemini 유료 · 🔴 아직 없는 명령
> venv 활성화 전제(`source .venv/bin/activate`). ⚠️ **CLI를 바꾸면 이 파일도 고친다.**

## 수집

```bash
minjob-ingest collect --source YTUS --dry-run               🌐    파싱 확인 (저장 안 함)
minjob-ingest collect --source YTUS --months 3            🌐    실제 수집 (무료)
minjob-ingest collect --days 14                           🌐    최근 2주만 (반복 실행용)
minjob-ingest daily                                      🔴 🌐💰  매일 (증분 + 구조화)
minjob-ingest status                                     🔴      실행·게시판 상태
minjob-ingest list-sources [KEY]                                 등록 31곳 (요청 없음)
minjob-ingest snapshot --source KEY                         🌐    fixture용 HTML 확보 (어댑터 없어도 됨)
```

`collect` 옵션 — `--source`(기본: 어댑터 있는 전부) · `--months N`(`0`=날짜 무제한) · **`--days N`**(짧은 범위 · `--months`와 함께 못 씀) · `--dry-run` · `--verbose`

💡 **고치고 다시 돌리는 중이면 `--days 14`를 쓴다.** 3개월치는 약 3,200건이라 요청 간격
(1.5s)만으로 80분이 넘는다. 2주면 약 500건·13분이고, **원장이 이미 받은 건을 건너뛰므로**
범위를 나중에 `--months 3`으로 넓혀도 받은 것을 다시 받지 않는다.

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

## 구조화 (AI) — 💰 유료 · 게시판에는 요청하지 않는다

수집해 둔 `source_data`를 Gemini로 읽어 검수 초안(`review_data`)을 만든다.

```bash
minjob-ingest structure --dry-run --limit 1 --source DAESHIN   💰  1건만 화면으로 확인 (저장 안 함)
minjob-ingest structure --limit 1                             💰  1건 실제 저장
minjob-ingest structure --limit 20 --source YTUS              💰  한 게시판 20건
minjob-ingest structure --all                                 💰  전량 (약 2~3시간 · 순차)
```

⚠️ **`--limit N` 또는 `--all` 이 없으면 실행을 거부한다.** 유료 호출이 옵션 없이 전량으로 도는 경로를 두지 않았다.

`structure` 옵션 — **`--limit N`**(판정할 건수 = 유료 호출 상한) · **`--all`**(남은 전부) · **`--dry-run`**(호출은 하되 저장 안 함) · `--source KEY` · `--verbose`(호출 로그)

**`--limit`은 "판정한 건수"를 센다** — 훑은 건수가 아니다. Gemini를 부르지 않는 공고(빈 공고·이미지 대기)는 세지 않으므로 `--limit 20`은 항상 **호출 20회 이하**다. 리포트의 `훑음`이 `--limit`보다 큰 것은 정상이다.

💡 **프롬프트를 다듬는 중이면 반드시 `--dry-run`.** 저장하면 그 공고에 판정이 찍혀 **다시 나오지 않으므로**, 프롬프트를 고쳐도 같은 표본으로 비교할 수 없다. `--dry-run`은 아무것도 저장하지 않아 같은 20건을 계속 볼 수 있다.

💡 **게시판을 바꿔가며 본다.** 미판정 목록이 수집 시각 순이라 `--source` 없이 돌리면 오래된 한두 게시판만 나온다. 프롬프트 **하나로 30곳**을 덮어야 하므로 한 곳 형식에 맞추면 나머지가 깨진다.

리포트는 이렇게 나온다:
```
  훑음         63건
  초안         20건   검수 대기(PENDING)
  제외          0건   개교회 채용이 아님 — 초안 없음
  2단계 대기   43건   내용이 이미지에 있음 — 멀티모달이 붙을 때까지 손대지 않음
```
| 줄 | 뜻 |
|---|---|
| **초안** | `review_data`에 PENDING으로 들어갔다 |
| **제외** | 개교회 채용이 아니라고 판정했다(초안 없음). **실패가 아니다** |
| **빈 공고** | 본문·이미지·첨부가 전부 없어 호출하지 않았다 |
| **2단계 대기** | 내용이 이미지에 있다. 지금 프롬프트는 텍스트만 보내므로 **판정을 남기지 않고 그대로 둔다** |
| **실패** | 판정을 남기지 않았다 → 다음 실행이 다시 시도한다(상한 3회) |

⚠️ **한 번 판정된 공고는 되돌릴 수 없다**(`structured_at`은 앞으로만 간다). 전량(`--all`)을 돌리기 전에 되돌리기 스크립트를 만든다 — [ROADMAP](./ROADMAP.md) 1-2 3단계.

## 어댑터

게시판 1곳 = 파일 1개(`minjob_ingest/sources/adapters/<key 소문자>.py`). **파일을 놓으면 자동 등록**된다.
현재 **30곳 구현 = 활성 전부**. `HANSEI`는 게시판 소멸로 제외(31곳 등록 중 30곳 활성).

fixture(`tests/fixtures/<KEY>/`)는 **커밋되지 않는다**(가드레일 #11). 새 컴퓨터에서 어댑터 테스트를 돌리려면 먼저 받아야 한다:
```bash
minjob-ingest snapshot                     🌐  활성 전부 (게시판당 최대 2요청)
minjob-ingest snapshot --source YTUS       🌐  한 곳만
```
테스트 요약 맨 아래에 `어댑터 fixture 커버리지: N/30 검증`이 찍힌다 — 이 숫자가 낮으면 초록불이어도 검증이 건너뛰어진 것이다.

## 게이트 — 커밋 전 4개 통과

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest -q
```
자동 수정: `.venv/bin/ruff check --fix . && .venv/bin/ruff format .`

## 셋업 — 컴퓨터마다 1회

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env         # Vertex 서비스계정 값 입력 (PRIVATE_KEY 개행은 \n)
minjob-ingest check-gemini   # 💰 인증 확인
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
| `command not found: minjob-ingest` | venv 활성화 · 또는 `pip install -e ".[dev]"` 재실행 |
| Vertex 설정·PRIVATE_KEY 오류 | `.env` 값과 개행(`\n`) — 메시지가 빠진 변수명을 알려준다 |
| 한 게시판만 0건·실패 | 셀렉터 깨짐 또는 로그인벽. **우회 금지** — 비활성화 후 보고(가드레일 #1) |

> 저장 위치·필드 = [SPEC](./SPEC.md) §6 · 게시판 설정 = `config/sources.json`
