# crawler-demo — MinJob 크롤 콘솔 (프로토타입)

`min_job`(교회 사역자 청빙 플랫폼) 공고 수집 크롤러의 **동작하는 프로토타입**. 실서비스 코드가 아니라,
"소스 선택 → 크롤 → raw 확인 → 저장" 흐름과 아키텍처를 검증하기 위한 데모다.

📦 **[`minjob-crawler-demo.zip`](./minjob-crawler-demo.zip)** — 아래 압축을 풀어 실행. (소스 전체 포함, 시크릿·의존성 제외)

---

## 무엇을 하는가 (운영자 크롤 콘솔)

좌측에서 게시판(소스)을 고르고 **크롤 실행** → 파이썬 크롤러가 그 게시판 최근 공고를 **원문+메타 그대로(raw)**
추출 → 카드로 표시 → **저장**하면 리뷰 큐(`data/staging.json`)에 `PENDING`으로 쌓인다.
각 카드의 **"구조화 미리보기"**(옵션)는 Gemini로 교회명·교단·직분·부서·사례비 등으로 구조화해 보여준다(저장 안 됨).

> 본체 목적은 **raw 추출**(손실 없는 원문 수집). 구조화·DB 삽입은 저장된 raw를 나중에 검수해서 하는 별도 단계.

## 실행

요구: Python 3.10+, Node 20+

```bash
unzip minjob-crawler-demo.zip && cd minjob-crawler-demo
./setup.sh                       # .venv + 크롤러/어드민 의존성 + admin/.env
source .venv/bin/activate
cd admin && npm run dev          # http://localhost:3000 (점유 중이면 3001+)
```

크롤러만 단독:
```bash
source .venv/bin/activate
python -m crawler ytus --limit 3            # 실제 raw JSON
python -m crawler ytus --limit 3 --offline  # 네트워크 없이 샘플 HTML로
```

## 구성 (2파트)

```
crawler/   Python  — raw 추출 엔진 (소스별 어댑터, 원문+메타만)
admin/     Next.js — 콘솔 (크롤 호출·raw 표시·저장·구조화 미리보기)
```
어드민이 크롤 시 파이썬 CLI(`python -m crawler <소스> --limit N`)를 호출한다. 어댑터는 1소스=1파일
(`koreabaptist`·`prok`는 같은 솔루션이라 공용 베이스 `dimode_board.py` 공유).

## 수집 소스 (데모 4종, 공개 게시판)

| key | 게시판 | 교단 | 상태 |
|---|---|---|---|
| `ytus` | 영남신학대 취업/초빙 | 예장통합 | raw ✅ |
| `kehc` | 기독교대한성결교회 총회 구인 | 성결교 | raw ✅ |
| `koreabaptist` | 기독교한국침례회 목회자청빙 | 침례교 | raw ✅ |
| `prok` | 한국기독교장로회 교역자 청빙 | 기타(기장) | 목록만(상세 로그인벽 → 배지 표시) |

## 구조화 미리보기 (Gemini, 선택)

Vertex AI **Gemini 2.5 Flash**(`gemini-2.5-flash`, location `global`). `admin/.env`에 Vertex 서비스계정
값을 채우면 동작(없으면 raw 흐름은 정상, 구조화 버튼만 에러 표시).

## 검증된 것 (직접 실행 확인)

- 크롤 4소스 실데이터 추출(ytus 526자·kehc 515자·koreabaptist 792자, prok 목록만).
- 어드민 `npm run build` 통과, `/api/crawl`·`/api/save`(dedup)·`/api/structure` 동작.
- Gemini 구조화: 실제 공고 → 교단·직분·부서·사례비(원문 보존)·요약 정확 추출(confidence high).

## 빌드 메모 (스테이지별 검수에서 잡힌 것)

- 구조화 AI: 이 GCP 프로젝트엔 Claude 아닌 **Gemini만 활성**(2.5-flash/flash-lite) → `@google/genai`로 구현.
- `prok` 상세 로그인벽 발견 → 크래시 대신 목록 메타 + 배지로 처리.
- 어댑터 4개 되며 공통 로직 `common.py`로 추출(중복 제거).

## 실서비스로 갈 때

- `data/staging.json` → **Supabase** 스테이징 테이블(store 계층만 교체).
- 어드민 수동 실행 → **GitHub Actions 스케줄 크롤러**가 같은 큐에 자동 적재, 어드민은 검수 전용.
- 로그인 게시판은 계정·법률검토 후 인증 크롤.

> 데모 단순화: 파일 저장 동시성·토스트 등 생략(단일 운영자 전제). 상세 설계는 리포 루트 `docs/`(SOURCES·CONTRACT) 참조.
