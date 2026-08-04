# RUNBOOK — 실행 명령

> 🔴 = 아직 없는 명령(해당 Phase에서 생김) · 🌐 = 게시판에 요청 · 💰 = Gemini 유료 호출
>
> ⚠️ CLI 명령을 추가·변경하면 **이 파일을 같이 고친다**. 운영자는 이 파일만 보고 실행한다.

## 수집

```bash
# ① 파싱 확인 — 저장 안 함. 목록 + 상세 표본 1건만 요청
.venv/bin/minjob-ingest collect --source YTUS --dry-run          🌐
# ② 실제 수집 — 최근 3개월 (아직 AI 안 씀 · 무료)
.venv/bin/minjob-ingest collect --source YTUS --months 3         🌐
.venv/bin/minjob-ingest collect --months 3                       🌐   # 어댑터 있는 곳 전부
# ③ 구조화 — 여기서 처음 과금
.venv/bin/minjob-ingest structure                            🔴 💰

# 이후 매일
.venv/bin/minjob-ingest daily                                🔴 🌐💰 # 새 글 수집 + 구조화

# 확인
.venv/bin/minjob-ingest status                               🔴      # 실행 요약·게시판별 상태
.venv/bin/minjob-ingest list-sources [KEY]                          # 등록 소스 31곳
```

`collect` 옵션: `--source KEY`(한 곳만 · 기본은 어댑터 있는 전부) · `--months N`(게시일 범위 · `0`이면 날짜로 안 자름) · `--pages N`(목록 페이지 상한 · 기본 3) · `--dry-run` · `--verbose`(HTTP 요청 로그)

> ⚠️ `--dry-run`은 **목록 전체 + 상세 표본 1건**을 요청한다. 목록만 보면 상세 파싱이 검증되지 않기 때문이다. 저장·실행기록은 남지 않는다.
>
> ⚠️ **`--months`만으로는 부족하다 — `--pages`가 먼저 걸린다.** 기본 3페이지는 데일리용이고, 게시판이 활발하면 3페이지가 2~4주치뿐이다. 3개월을 받으려면 페이지를 늘려야 하며, **미달이면 리포트가 필요한 페이지 수를 추정해 알려준다**:
>
> ```
> ⚠ 페이지 상한(3p)에서 멈췄습니다 — 컷오프 2026-05-04에 도달하지 않았습니다.
>   --pages 11 정도가 필요합니다(관측 속도 기준 추정)
> ```
> 실측(YTUS): 하루 약 2.2건 → 3개월 ≈ 11페이지.

## 코드를 고친 뒤 — 게이트 (4개 전부 통과해야 커밋)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest -q
```

자동 수정: `.venv/bin/ruff check --fix . && .venv/bin/ruff format .`

## 셋업 (컴퓨터마다 한 번)

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env                      # Vertex 서비스계정 값 입력(PRIVATE_KEY 개행은 \n)
.venv/bin/minjob-ingest check-gemini      # 💰 인증 확인
```

## 주의

- **`data/`를 지우면 원장을 잃는다** → 다음 실행이 31곳을 전량 재수집(예의 위반)하고 전량 재구조화한다(과금). Phase 1-6에서 Supabase로 옮기면 해소.
- `.env`는 커밋되지 않는다. 다른 컴퓨터로 옮길 때 직접 복사.
- 캐시(`.mypy_cache`·`.ruff_cache`·`.pytest_cache`·`*.egg-info`)는 지워도 된다 — 도구가 다시 만든다.

## 안 될 때

| 증상 | 확인 |
|---|---|
| `command not found` | `.venv/bin/python -m pip install -e ".[dev]"` 재실행 |
| Vertex 설정 오류 | `.env`의 값. 오류 메시지가 빠진 변수 이름을 알려준다 |
| PRIVATE_KEY 형식 오류 | 키 개행이 `\n`(백슬래시+n)으로 들어갔는지 |
| 한 게시판만 0건 | `status`로 연속 실패·0건 확인. 로그인벽으로 바뀐 것이면 **우회하지 말고 비활성화**(가드레일 #1) |

> 저장 위치·필드 의미는 [`SPEC.md`](./SPEC.md) §6, 게시판 설정은 `config/sources.json`.
