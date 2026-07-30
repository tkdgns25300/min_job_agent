# RUNBOOK — 실행 명령

> 🔴 = 아직 없는 명령(해당 Phase에서 생김) · 🌐 = 게시판에 요청 · 💰 = Gemini 유료 호출
>
> ⚠️ CLI 명령을 추가·변경하면 **이 파일을 같이 고친다**. 운영자는 이 파일만 보고 실행한다.

## 수집

```bash
# 최초 1회 — 최근 3개월. 수집만 먼저(무료), 확인 후 구조화(유료)
.venv/bin/minjob-ingest collect  --source YTUS --months 3    🔴 🌐   # 한 곳으로 먼저 검증
.venv/bin/minjob-ingest collect  --months 3                  🔴 🌐   # 31곳 전체
.venv/bin/minjob-ingest structure                            🔴 💰   # 수집분을 AI로 구조화

# 이후 매일
.venv/bin/minjob-ingest daily                                🔴 🌐💰 # 새 글 수집 + 구조화

# 확인
.venv/bin/minjob-ingest status                               🔴      # 실행 요약·게시판별 상태
.venv/bin/minjob-ingest list-sources [KEY]                          # 등록 소스 31곳
```

자주 쓰는 옵션: `--source KEY`(한 곳만) · `--months N`(수집 범위) · `--pages N`(목록 페이지 수 상한) · `--dry-run`(저장 안 하고 무엇을 가져올지만)

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
