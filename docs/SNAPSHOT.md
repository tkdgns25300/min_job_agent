# SNAPSHOT — min_job_agent 작업 시점 핸드오프

> **이 문서 하나로 "지금 상황" 파악.** 새 세션(다른 컴퓨터 포함)에서 이어받을 때 이 파일 + `README.md` + `docs/SPEC.md`(파이프라인 정본) + `docs/SOURCES.md` + `docs/CONTRACT.md`만 읽으면 됨.
>
> **작성 시점**: 최초 2026-07-12 · **갱신 2026-07-21**(데모 · 3차 실측 · Fable 교차감사 · 초교파) · **갱신 2026-07-27**(운영자 전수 실측 → 크롤 31 확정 §5·§7 · 파이프라인·Supabase 스키마 설계 §9) · **갱신 2026-07-30**(Python 이식 완료 · Phase 1-1 fetch 층 완료 §10) · **dev = prod = origin** (ff-only)

---

## 2026-08-05 — 어댑터 29곳 완성 (1-4)

활성 30곳 중 **29곳** 구현. 게시판 1곳 = 파일 1개(124~213줄), 등록은 파일을 놓으면 자동(모듈 발견).
`snapshot` 명령이 어댑터 없이 fixture를 받고, 적합성 테스트 1개가 29곳을 순회 검사한다.
**1124 테스트 · 게이트 4개 통과 · fixture 커버리지 29/29.**

**구현하지 않은 2곳**
- `CSU` — 목록 API가 익명 호출에 `{"code":22000,"유효하지 않은 세션입니다"}`를 준다. 페이지 GET은 200인데 쿠키를 하나도 주지 않고, 번들의 해당 코드 처리는 "다른 곳에서 로그인" 안내 후 홈으로 리다이렉트한다 → 로그인 세션 요구로 보여 **우회하지 않았다**(가드레일 #1). enabled 유지 + 어댑터 없음 → `AdapterMissing`으로 드러난다. **운영자 판단 필요.**
- `HANSEI` — 사이트가 Konnect에서 이전되며 게시판 소멸. 후속 카테고리 0건, 이력 전체에 채용 1건(2016). `enabled: false` + 재활성 URL 기록.

**계약이 늘어난 세 가지** (전부 실물을 보고 결정)
- `list_request(source, page) → ListRequest(url, form)` — 목록이 POST인 게시판(HANIL). URL만으로는 표현 불가
- `NEEDS_DETAIL_REQUEST = False` — 목록 JSON에 본문이 든 게시판(HANIL). 상세 페이지는 JS가 채우는 빈 껍데기라 받아도 제목조차 없다
- config `list_has_dates: false` — 목록에 게시일이 없는 게시판(PCKWORLD). 컷오프를 만들면 아무 행도 안 잘려 안전 상한까지 걷는다

**실측으로 드러난 함정** (fetch_note·어댑터 docstring에 전부 기록)
- **2페이지 이상의 상세 href에 페이지 파라미터가 끼어든다**(KOSIN_TH·MTU·TTGU·WGST·HTUS·SJS) → `detail_pattern` 접두 매칭이 **전 행에서** 실패. 1페이지만 보면 절대 안 드러나고, 걸리면 그 게시판의 1페이지 말고 전부를 잃는다. id를 **쿼리 파라미터 이름**으로 뽑아 해결
- **상세 페이지가 목록을 다시 그린다**(KOREABAPTIST·KEHC·PGAK) → 본문 범위를 넓히면 남의 공고가 증거로 저장된다
- **본문 밖 링크가 첨부로 들어온다** — 사이트 공용 파일(DAESHIN 장학금기탁서·WGST 안전관리계획), 본문의 `mailto:`·교회 홈페이지(ACTS), PREV/NEXT
- CALVIN 본문은 **인라인 `data:` URI 150KB** 한 장(텍스트 0자) → SPEC §6에 "구조화는 스킴으로 갈라 `data:`는 fetch하지 말고 디코드" 못 박음
- 그누보드는 오늘 글을 `15:58`, 올해 글을 `09-26`으로 **연도 없이** 표시 → KST 기준으로 되살린다(넉넉해지는 방향이라 유실 없음)
- KEHC `page/N`은 페이지가 아니라 **행 오프셋**(0·50·100…) · 날짜가 `YY.MM.DD` · **잠긴 글**은 상세가 비어 제외(우회 안 함)
- soft_200 3곳 추가 실측(UHS·SUNGKYUL·KAICAM은 없는 id에도 200) · CALVIN `image_only` 추가

**남은 공용화 후보** (중복이지 버그는 아니다 — 다음 작업)
`external_id_from_query(url, param=)` (6곳이 각자 `parse_qs`) · 첨부 교차확인 `require_attachment_evidence` (7곳) · 페이지 쿼리 `list_request` (9곳 동일) · 첨부 파일명 크기 접미사 제거 · 2자리 연도 날짜 · `attachments_in`에 링크 셀렉터 인자

## 0. 한 문장 요약

`min_job_agent`는 형제 디렉토리 `../min_job`(교회 사역자 청빙 채용 플랫폼, Next.js)을 위한 **공고 수집 크롤러**다. **소스 정찰이 3차 실측 + Fable 교차감사 + 운영자 직접 전수 실측(2026-07-27)까지 끝나**, **크롤 대상 31곳을 최종 확정**했다(제외 6 · SOURCES §7). 교단 확정 방법도 CONTRACT §2로 결정됨. **`crawler-demo/`에 전 체인 관통 동작 프로토타입**(Python 4어댑터 + Next.js 어드민, 구조화 AI = Vertex **Gemini 2.5 Flash**)이 있고, **`docs/SPEC.md` 작성 완료**(파이프라인·staging 4테이블·판정 게이트·스코프·정책·배포 — 3렌즈 냉정검수+재검증 반영). **`docs/ROADMAP.md`·`CLAUDE.md` 작성 완료**(CLAUDE.md는 3렌즈 검수 반영). **Phase 0 뼈대 완료** — TS 스켈레톤(Store seam·`domain`·Gemini 래퍼·**31곳 레지스트리 라이브 2차검증**), `typecheck` 통과 · **Gemini 실호출 성공**(운영자 확인). **스택을 Python으로 교체(2026-07-29)하고 이식 완료** — TS 잔재 삭제, `minjob_ingest/` flat 패키지에 `domain`·`models`(SPEC §6 4레코드)·`clock`·`settings`·`sources.registry`·`store`(Protocol+JSON)·`lib.gemini` + CLI(`list-sources`·`check-gemini`), 4게이트(ruff·format·mypy strict·pytest **343**) 통과. **Phase 1-1도 착수** — `fetch/` 전송 층 완료(UA·cp949·타임아웃·재시도·간격·세션·본문 하한 · mutation 13/13). 현재 열린 핵심 = **1-1의 남은 4단계(어댑터 → 원장 확장 → `collect` → 관통)**. 상세는 **§10**.

---

## 1. 프로젝트 정체 (왜 별도 리포인가)

- `../min_job` 본체는 **in-repo 크롤러 코드를 두지 않는다**(min_job CLAUDE.md Ingest 레이어 규칙). 자동 수집 자체는 min_job 가드레일 #1로 **허용**(공개 공식 게시판 한정·운영자 검수 전제·법률 검토 완료 2026-07-28)되며, 그 구현체를 **별도 리포로 분리**한 것이 이 리포다.
- 이 크롤러는 min_job 기존 파이프라인("사람 수집 → AI 구조화 → 운영자 검토")에서 **fetch 한 단계만 자동화**하는 델타다. 나머지 가드레일(개인정보 금지·정통 화이트리스트·운영자 리뷰 게이트)은 승계.
- **스키마 정본**: 최종 공개(`churches`/`jobs`) = `../min_job/docs/DATA.md`. **크롤러 staging(`source_data`·`review_data`·`source_health`·`crawl_run`)은 이 리포 소유·마이그레이션**(SPEC §6·§8). 파이프라인 동작 정본 = `SPEC.md`.

---

## 2. 확정된 핵심 결정 (되돌리지 말 것)

| 항목 | 결정 |
|---|---|
| **스택** | **Python 3.12+** — 2026-07-29 변경. *(이전 결정 "TypeScript/Node, min_job 타입 공유" **철회**: 별도 리포·별도 프로세스라 타입 공유가 실제로 불가했고, 크롤 생태계 성숙도 + 운영자 직접 실행이 우선. enum 정합은 **CONTRACT §1 계약 + 드리프트 테스트**로 지킨다.)* |
| **출력** | **리뷰 큐** — 크롤러는 스테이징에만 적재, 운영자가 min_job admin에서 승인 후 게재 |
| **소스 범위** | **공식 게시판만**(신학교·교단·노회). 상업 청빙사이트(청빙넷 등)는 초기 제외 |
| **교단 enum** | **9개 대형 + 기타(ETC)**: HAPDONG·TONGHAP·BAEKSEOK·GAMLI·SUNBOK·BAPTIST·SEONGGYUL·GOSIN·**HAPSIN** + ETC. **기장(KIJANG)은 ETC**로 (기장 교회 공고는 ETC 태깅 · 기장 총회 PROK 게시판은 2026-07-27 크롤 제외) |
| **교단 태깅** | **공고에서 확정**(①교단 명시 ②교회 명부 ③AI 추정=`ai_guess` ④`UNKNOWN`). 근거 없으면 `UNKNOWN`+운영자 해소. 게시판 교단은 힌트만. **노회 미사용**(SPEC §5.3·CONTRACT §2). `raw_denomination` 보존 |
| **로그인 소스** | **현재 크롤 31곳에서 제외**(2026-07-27 · 공개 게시판만). 인증 크롤은 **변호사 게이트 통과 후 별도 단계**로만 검토(계정은 운영자 제공 · 크롤러가 로그인 자동화 금지) |
| **커뮤니티**(카페·밴드·페북) | 나중(Phase 후반). 지금은 공식 게시판만 |
| **robots.txt** | `Disallow`는 **따르지 않고**(운영자 판단 2026-07-30), `Crawl-delay`는 **따른다**(2026-08-04). 전자는 허락, 후자는 서버 용량 신고 — 무시하면 IP 차단. 스위치 `RESPECT_ROBOTS_DISALLOW`·`RESPECT_CRAWL_DELAY` |
| **User-Agent** | **31곳 전부 동일한 브라우저 UA**(2026-08-04). 자체 UA는 막힌다(**YTUS 실측 403 vs 브라우저 UA 200**). 어디가 막는지 사전 파악 불가 → 보드별 예외 없음. `spoof_ua`는 실측 기록으로만 잔존 |
| **브랜치** | `prod`(배포)·`dev`(작업). 릴리스 dev→prod ff-only. commit/push/merge는 명시 요청 시만 |

> 교단 8→9 변경 이유: 검증에서 **합신대 청빙 게시판이 활발**(운영자 실측 ann 30)로 확인돼 기타→독립 key로 복구.

---

## 3. 지금까지 한 작업 (히스토리)

1. **빈 리포 부트스트랩** — `.gitignore`+`README.md` 커밋(`1a0fe3f`), `prod`/`dev` 브랜치 생성(main→prod 리네임, dev 분기). 로컬만이었음.
2. **min_job 파악** — 형제 리포 전 문서·소스·mock 정독. 이 크롤러가 min_job의 어느 자리(수집 자동화)에 들어가는지 확정.
3. **교단 정리** — 한기총 66교단 목록 + 웹 통계 조사로 "대형 8~9 + 롱테일" 지형 파악. enum을 9+기타로 확정(§2).
4. **Step 1 소스 정찰** — `Workflow`로 **10개 subagent 병렬 웹조사**(교단 8 + 범교단 + 기타/커뮤니티) → **49개 소스** 수집.
5. **Step 1 냉정 검증** — `Workflow`로 **9개 subagent 병렬 재검증**(curl/WebFetch로 최신 공고일·로그인·교단·URL 실측) → 47건. **1차 조사의 환각·오판 다수 정정**(예: 합동총회 '회원제'→공개, 웨스트민스터 '청빙란 없음'→매우활발·초교파, 대신대 교단 확정).
6. **docs 작성** — `SOURCES.md`(카탈로그), `CONTRACT.md`(출력 계약·정규화 맵).
7. **자체 냉정 검수** — 두 문서를 다시 감사해 **종합 과정에서 넣은 오류 4건 정정**(집계 모순·daeshin 기술메모·정규화 용어·총신대 활동성 과장). 대신대=합동은 독립 재확인.
8. **동작 프로토타입 데모** — `crawler-demo/minjob-crawler-demo.zip`(Python 크롤러 4어댑터 + Next.js 어드민 콘솔). 소스 선택→크롤(raw 추출)→저장(리뷰 큐)→Gemini 구조화 미리보기 전 체인 검증. 구조화 AI = Vertex **Gemini 2.5 Flash**.
   - 구조화 호출부(`admin/src/lib/vertex.ts`)에 **429(RESOURCE_EXHAUSTED)·503·5xx 지수 백오프 재시도** 추가(rate limit 대응).
9. **3차 실측 재검증(2026-07-21)** — `Workflow`로 **14 subagent 병렬**(교단별 10 + 지형/완전성 4)로 34개 URL·활성·로그인·교단·최신글 실측. 결과: 대부분 유효, **URL 수정 5·불확실 2·교단 오분류 2**. "공식만" 커버리지가 실 청빙 물량의 일부뿐임을 냉정 평가. SOURCES/CONTRACT에 반영.
10. **Fable 5 교차감사(2026-07-21)** — `Workflow`로 **Fable 7 subagent**가 불확실 항목 재판정 + 문서 감사. **판정 뒤집힘**: `hanil` 🔸→✅(AJAX·매우활발 12,480건)·`csu` 활성 확정·`korea-ag` 기각(청빙판 없음)·`bsds` 2026 백석 재결합으로 유동·`kidok` 예장합동 기관지+전면 로그인벽→제외 유력. 문서 내부 모순(bsds alias·kidok enum·합신 낡은 문구·mtu=kts 중복 숫자·kosin.org 유령참조) 정리 → SOURCES 재작성.
11. **초교파·연합기관 조사(2026-07-21)** — subagent 3개로 "교단 프레임에 빠진 초교파 게시판" 발굴. **신규**: 횃불트리니티(~1,100+·매우활발·크롤 쉬움 → 즉시 추천)·아신대(ACTS, 1차 '휴면' 오판→활성 ~673 정정). 연합기관(한교총·한기총·NCCK·KWMA)·선교단체(CCC·YWAM)·방송사(CBS·극동·더미션)는 청빙판 **없음** 확인(재조사 방지 기록).
12. **운영자 전 게시판 실측 검수(2026-07-27)** — 운영자가 워크시트 전 게시판을 직접 열어 실 공고량(`ann`)·조회수 전수 확인 → **크롤 tiering: 확정 25 · 조건부 5 · 나사렛 1(저물량이나 조회 1500~3000↑ 채택) = 크롤 31 · 드롭/휴면 6**(§5). 신규 사망/드롭 확정: **PROK(기장, 2025-08 이후 공고 0)** · `old.gapck`(합동총회) · `kcc`(장학 위주) · SU · 에스라(극저) · bsds(거의 정지). 순복음 공개 물량이 얇다는 것도 드러남(→ agkdc 가치↑). 또 `hanil`은 **www 필수**(apex 무응답)로 확진해 URL 정정.

13. **Python 이식 완료(2026-07-29~30 · 커밋 5개)** — 0-1a 골격+`config/sources.json`(31곳) / 0-1b-1 레코드·시각·설정 / 0-1b-2 Store(프로토콜+JSON) / 0-1c Gemini 래퍼+TS 잔재 삭제 / 패키지명 `minjob_agent`→**`minjob_ingest`**("agent"가 배치 크롤러를 잘못 설명). 각 단계 리뷰 + **mutation 테스트**로 검증(전 변형 탐지). 발견·수정한 설계 결함: 재구조화가 운영자 교정을 덮어씀 · 판정 기록 삭제/시도횟수 감소 허용 · `source_key` 미정규화로 §7 경보 무력화 · 잘린 AI 응답을 성공 처리 · **테스트가 운영자 실제 `.env`를 읽어 private key 유입**(실측 확인 → `tests/conftest.py`로 전역 차단).
14. **`external_id` 31곳 감사(2026-07-30)** — 31곳 **전부 목록에서 id 획득 가능**(→ 상세 요청 전 원장 대조 성립). 조건부 3곳 해법 확정: `PUTS` bd_name 필터 · `CSU` 1110만 · **`HANSEI`는 `catId:artclNo` 복합키**(목록이 카테고리에 걸쳐 있어 실행 간 충돌 → 중복 에러 가드로는 못 잡음). 규칙으로 승격(SPEC §10).
15. **RUNBOOK 신설 + fetch 층 완료(2026-07-30)** — 운영자 실행 매뉴얼(56줄 · 명령 추가 시 갱신 의무). `fetch/client.py`·`robots.py` 작성, 실행으로 버그 2개 잡음(**UA에 한글 → HTTP 헤더 인코딩 실패로 31곳 전부 시작 불가** · `2**n`이 mypy Any). robots는 운영자 판단으로 미준수(§2).

> ⚠️ 4·5번의 워크플로우 결과 원본은 세션 임시폴더(`/private/tmp/...`)에 있어 **다른 컴퓨터엔 없다**. 내용은 전부 `SOURCES.md`/`CONTRACT.md`로 옮겨졌으니 그걸 정본으로 볼 것.

---

## 4. 현재 파일 상태 (`docs/`)

| 파일 | 내용 | 상태 |
|---|---|---|
| `README.md` | 프로젝트 한 줄 소개 + 브랜치/Git 규약 + 스키마 정본 위치 | ✅ |
| `docs/SNAPSHOT.md` | 이 파일 (시점 핸드오프) | ✅ |
| `docs/SOURCES.md` | 소스 카탈로그(교단별 URL·접근·활동성·판정) — 3차 실측 + Fable 감사 반영 | ✅ 재작성본 |
| `docs/CONTRACT.md` | 크롤러 출력 계약 — 교단 enum·정규화 맵·소스별 default 교단+모드+기술요건·스테이징 필드·dedup·로그인 법률게이트 | ✅ 초안 |
| `CLAUDE.md` | 아키텍처·레이어 책임·가드레일·컨벤션 | ✅ 작성 + 3렌즈 검수(일반 2 + Fable) 반영 |
| `docs/SPEC.md` | 파이프라인 명세(스코프·게이트·staging 4테이블·정책·배포) | ✅ 작성 + 3렌즈 냉정검수·재검증 |
| `docs/ROADMAP.md` | Phase별 작업 단위(0~3) | ✅ 작성(min_job 스타일) |
| `docs/RUNBOOK.md` | **운영자 실행 매뉴얼**(명령·저장위치·장애대응) | ✅ 작성 — 명령 추가 시 갱신 의무 |

> **코드 = `minjob_ingest/`**(flat 패키지 · TS 잔재는 0-1c에서 삭제, 필요하면 git 이력): `domain.py`·`models.py`·`clock.py`·`paths.py`·`settings.py`·`cli.py`·`sources/registry.py`·`store/{base,serde,json_store}.py`·`lib/gemini.py`·**`fetch/{client,robots}.py`**. 테스트 343개(네트워크 미사용). 전송 정본은 **`config/sources.json`(31곳)**. 별도로 **`crawler-demo/`에 동작 프로토타입**(Python 4어댑터 + Next.js 어드민, zip) — 전 체인 관통 검증됨(참고용).

---

## 5. ★ 운영자 실측 검수 (2026-07-27) — 크롤 타깃 tiering

> 운영자가 워크시트 **전 게시판을 직접 열어** 실 공고량(`ann`=게시판에 올라온 청빙 공고 수)·조회수를 전수 확인. **이게 카탈로그 ground-truth.** (로그인티어 KMC·AGK·기독신문·CTS, 그리고 agkdc·agkr는 이번 대상 아님 — 별도 대기)
>
> **→ 최종 선정(2026-07-27): 크롤 31 · 제외 6** (제외 = gapck·bsds·kcc·PROK·ezra·SU). 아래 A/B/C는 그 근거가 된 실측량이며, **저물량이라도 운영자가 포함한 곳 있음**(광신·나사렛·순복음대학원대 등)·**WGST는 포함**. 확정 목록은 SOURCES §7.

**A. 크롤 확정 — 활발·물량 확인 (25곳)** *(괄호 = 실측 ann)*
| 교단 | 게시판 |
|---|---|
| HAPDONG | 대신대(30) · 칼빈대(10) · 광신대(조회활발) · **총신대(240)** |
| TONGHAP | **장신대(230)** · 영남신대(70) · 호남신대(60) · 부산장신(60) · 서울장신(45) · 한일장신(40) · PCK총회(15) · 한국기독공보(10~15·이미지형) |
| BAEKSEOK | 백석대대학원(35) · 백석총회(15) |
| GOSIN | KTS(45) |
| HAPSIN | 합신대(30) |
| GAMLI | 감신대(20) · 목원대(20) |
| BAPTIST | 침신대(15) · 침례총회(4) |
| SEONGGYUL | 기성(35) · 예성(8) |
| ETC | KAICAM 독립교회연합회(10~15) |
| 초교파 | 횃불트리니티(8) · 아신대(8) |

**B. 조건부·저물량 — 넣되 후순위/필터 (5곳)**
협성대(3~5) · 한세대(3~5) · 순복음대학원대 sts(1~2) · 고신대 신학과(1~3·타교단 혼재 필터) · WGST(5·현 교단 확인 조건)

**C. 드롭·휴면 — 실 공고 거의 없음 (6곳)**
| 게시판 | 실측 | 처리 |
|---|---|---|
| 예장합동총회 `old.gapck` | ann 1 | ❌ 드롭 — 합동은 신학교 4곳으로 충분 |
| 기장총회 `PROK` | **2025-08 이후 공고 0** | ⏸ 휴면 — ETC 대표 소스 사망, 모니터링만 |
| 백석대신총회 `bsds` | ann 1 | ⏸ 거의 정지(+재결합 유동) → 모니터링만 |
| 순복음총회신학교 `kcc` | ann 1·장학 위주 | ❌ 드롭 |
| 에스라 `ezra` | 연 3~5건 | ❌ 드롭(극저) |
| 서울성경신대 `SU` | ann 1 | ❌ 드롭 |

> **나사렛(`na.or.kr/ccall`)은 ann 1이지만 조회 평균 1500~3000으로 관심이 높아 운영자가 채택** → §7 크롤 31에 ETC로 포함(드롭 아님).

**실측이 드러낸 것:**
- **순복음(SUNBOK) 공개 물량이 매우 얇다**(한세대 3~5·sts 1~2·kcc 드롭) → **`agkdc` 공개 청빙판 확인 가치↑**, AGK(로그인) 상대 중요.
- **기장(ETC) 총회 소스 `PROK` 사망** → ETC 총회급 공개 소스 없음(KAICAM은 독립연합). ETC 물량은 KAICAM + 공고별 감지 의존.
- **물량 대들보 = 장신(230)·총신(240)** 압도적, 그 뒤 영남·호남·부산장신·KTS·기성.

**크롤 기술 주의** — 전송 정본은 **`src/sources/registry.ts`**(라이브 2차검증). 요약: www 필수(hanil·bpu·uhs·kwangshin·kts·mtu) · http 전용(calvin·wgst) · **`-k` 불요**(daeshin·kts 인증서 정상) · 브라우저 UA 필수는 **mtu만**(그 외는 UA 문자열만 있으면 됨) · JSON(csu=세션 필요·hanil=`article_list.ajax`) · **헤드리스 0** · EUC-KR(puts·htus·sjs·acts → cp949 디코드) · 이미지형(pckworld·koreabaptist → Gemini 멀티모달).

---

## 6. 미해결 / 대기

**결정됨:**
- [x] **크롤 대상 최종 확정 (2026-07-27)** — **크롤 31 · 제외 6**(`gapck`·`bsds`·`kcc`·`PROK`·`ezra`·`SU`) 운영자 sign-off. 나사렛 신규 채택(ETC). SOURCES §7·CONTRACT §4 반영 → 어댑터 대상 = 31곳. (tiering 상세 §5)
- [x] **스코프 = 개교회 채용 허브** — 게이트1(개교회 채용?) → 게이트2(`job_kind` MINISTRY/GENERAL). 사역직+일반직 모두 수집, 방송사(CTS)·기관·비채용 제외. 판정=업무 내용, 경계=운영자(uncertain·low). (SPEC §1)
- [x] **교단 확정** — ① 교단 명시(stated) → ② 교회 명부(registry) → ③ AI 추정(`ai_guess`·확정 아님) → ④ 미상(승격 전 운영자가 10키로 해소). **노회 저장·매핑표 폐기.** (CONTRACT §2·SPEC §5.3)
- [x] **초교파 편입** — 기준 "활성 청빙 게시판". 횃불·아신대·WGST 채택(교단은 공고서 판정). (SOURCES §1·CONTRACT §4)
- [x] **연락처 = 지원용 명시 연락처(전화·이메일·링크) 추출·공개** — 가드레일 #3 완화. ⚠️ 개인정보·약관은 **정식 오픈 전 법률 검토 항목**. (SPEC §5.5)
- [x] **파이프라인·저장·배포** — `source_data`(불변 원자료+원장) → `review_data`(검수) → 승인 시 `churches`/`jobs`. 이미지=Gemini 멀티모달(OCR 없음). 증분=글번호 원장. 교차게시 dedup=검수 단계. **GH Actions 매일 07:00 KST(DAILY)** + 백필 로컬 수동(최근 3개월). Supabase 생성됨. `source_key` 대문자 enum. (SPEC §2~§7·§9)
- [x] **SPEC.md 작성**(3렌즈 냉정검수) **+ 이 리포 문서 정합 갱신(2026-07-28)** — CONTRACT/SOURCES/SNAPSHOT을 SPEC 정본에 맞춤.

**대기:**
- [x] **Python 이식 완료(2026-07-30)** — `config/sources.json`(31곳 문자 그대로) · 도메인/레코드/시각/설정 · Store(프로토콜+JSON) · Gemini 래퍼 · TS 잔재 제거 · 패키지명 `minjob_ingest`. (§3-13)
- [ ] **크롤 로직 구현** ⭐ — **1-1 진행 중**(fetch 층 ✅ / 어댑터·원장확장·`collect`·관통 남음 → **§10**). 이후 교단 확정(1-3)·31곳 확장(1-4)·Supabase(1-6)·GH Actions(1-7).
- [~] **min_job 연동**(별도 리포 · SPEC §8) — 2026-07-29 확인: ✅ `job_kind`·`role`·`contact` 타입 반영 · ✅ `KIJANG` 제거(**10키 완료**) · ✅ 가드레일 #1·#3 재정의 + **법률 검토 완료(2026-07-28)** · ✅ min_job ROADMAP 1-10 트랙 생성. **남은 것**: 마이그레이션 SQL · 목록 UI 필터 · `review_data` 검수 브릿지(전부 min_job 소관).
- [ ] **순복음·ETC 물량 보강 검토** — 순복음 공개 물량 얇음 → `agkdc` 확인, PROK 사망분 대체.
- [ ] **로그인 소스 법률 게이트** — KMC·AGK·기독신문 인증 크롤 전 변호사 확인 + 계정.
- [ ] **커버리지/상업 CROSS**(청빙넷·cjob·갓피플·WGST) — 법적 검토 후 재결정.
- [ ] **커뮤니티**(카페·밴드·페북) — Phase 후반.

---

## 7. ▶ 다음 작업 (이어서)

> Walking Skeleton은 **`crawler-demo`로 사실상 관통 완료**(ytus 등 4어댑터 → raw → Gemini 구조화 → 리뷰 큐). 다음은 "결정 확정 → 정식화".

0. ✅ **열린 결정 확정** — 스코프·job_kind·교단·연락처·이미지·백필/크론 전부 확정(§6 "이번 세션 확정" · SPEC).
1. **하네스 문서** — ✅ `CLAUDE.md`·`docs/SPEC.md`·`docs/ROADMAP.md` 완료(전부 다중 검수 반영).
2. ✅ **Python 이식(0-1a~0-1c) 완료** — 골격·`config/sources.json`(31곳)·도메인/레코드/시각·Store(Protocol+JSON, write-once·검수상태 보존 강제)·Gemini 래퍼(SDK 내장 재시도)·CLI 2명령. 각 단계 리뷰 + mutation 테스트로 검증(전 변형 탐지).
3. **▶ 다음: Phase 1-1의 남은 4단계** — 상세는 **§10**. 요약: ②YTUS 어댑터(파싱) → ③원장 조회 확장(제목·날짜 대조) → ④`collect` 명령+`--dry-run` → ⑤관통+fixture. 그 다음 1-2 구조화로 **1소스 전 구간 관통**.
4. **데모 → 31곳 확장** — 어댑터 **1개 검증 후 31곳**(§5·§7), **교단 확정 로직(명시/명부/AI추정 `ai_guess` + evidence + 미상 해소)**, JSON → **Supabase(§9 스키마)**는 ROADMAP 1-4·1-3·1-6.
5. **robots.txt·요청 rate limit 구현**(데모 미구현) · 로그인 소스 법률게이트 · 이미지 공고 처리(Gemini 멀티모달 — pckworld 등).

---

## 8. 실행 / 재개 방법

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"   # 첫 셋업
.venv/bin/ruff check . && .venv/bin/ruff format --check .   # 게이트 1·2
.venv/bin/mypy && .venv/bin/pytest -q                       # 게이트 3·4
.venv/bin/minjob-ingest list-sources [KEY]   # 등록 소스 31곳 확인
.venv/bin/minjob-ingest check-gemini         # Vertex 인증 스모크(.env 필요 · 유료 실호출)
git branch -vv            # prod / dev 확인
git log --oneline         # 히스토리

# 소스가 아직 살아있는지 빠른 확인(예):
curl -sL "https://www.ytus.ac.kr/board/list/trXXR" | head   # 영남신대(통합) 취업/초빙
```

- **다중 에이전트 재조사가 필요하면** `Workflow` 도구 사용(사용자가 "subagent 써도 된다" 승인함). 지난 워크플로우 스크립트는 세션 임시폴더에만 있어 재사용 불가 → 필요 시 재작성.
- **작업 대화는 한국어, 커밋·식별자는 영어.**

---

## 9. 크롤 파이프라인·데이터 설계 (2026-07-27 상세)

> 이 세션에서 확정한 파이프라인·배포·저장 설계. §6 `[x] 파이프라인·배포 방향`의 상세본. 정식화(SPEC/CLAUDE + `src/`) 시 이 설계를 기준으로 한다.

### 9.1 파이프라인 (①~⑥)
```
① 소스 레지스트리(repo config) ─┐
② 시크릿(GH Secrets) ───────────┤→ [GitHub Actions 크롤 실행]
③ 원장 조회(source_data) ───────┘   (이미 본 글번호 skip)
      ▼ fetch → raw 최대 추출 (이미지 공고는 vision으로 텍스트화)
④ source_data      (raw + 원장 · 불변)
      ▼ 구조화(Gemini 2.5 Flash) + 교단 확정(명시·명부=규칙 / 그 외 AI추정 ai_guess)
⑤ review_data      (PENDING)
      ▼ 운영자 검수(min_job admin) — 승인/수정
⑥ churches / jobs  (공개 · 요약 + source_url)
   ↳ crawl_run: 시작에 INSERT(run_id 확보) → 종료에 요약·에러 UPDATE · source_health UPSERT → 이상 시 경보
```
- ①~⑤ 자동 · ⑥ 사람 게이트. **dedup 2종**: 글번호(수집 시 skip) / 교차게시 병합(검수 시).

### 9.2 트리거·배포
- **배포 = GitHub Actions.** 크롤러 코드가 repo에 있고 `.github/workflows/crawl.yml`만 두면 GitHub이 매일 러너를 띄워 실행 후 삭제. **상시 서버 없음 · 무료 한도 내 $0.**
- **트리거**: 매일 cron **자동**(DAILY·파라미터 없음) + 수동 `workflow_dispatch`(DAILY 즉시 재실행). **백필은 로컬 수동**(mode=BACKFILL·최근 3개월 · SPEC §7).
- **증분**: 게시판별 `source_health.last_run_at` 이후 + **글번호(`external_id`) 원장 대조로 최종 판정**(날짜보다 정확). ⚠️ **초기 백필은 운영자가 크롤러로 수동 실행** → `source_data`에 원장이 채워져야 데일리가 중복을 안 만듦(엑셀로 jobs만 손입력 시 원장 비어 중복 발생).

### 9.3 저장 위치
- **GitHub(repo)**: 코드 + **소스 정의 config**(31곳 URL·셀렉터·flags·`enabled`). **공고 데이터는 없음.** 추가/제외 = config 커밋(`enabled:false`로 제외해도 이력 보존).
- **Supabase**: 실제 데이터 전부(raw→검수→공개 + 운영 상태). 무료 티어.
- **GH Secrets**: Supabase service key · Vertex 키(env — repo/DB에 노출 X).

### 9.4 Supabase 스키마 (크롤러 4테이블 + 목적지)
> ⚠️ **`SPEC.md` §6가 스키마 정본** — 아래 요약은 이미 뒤처져 있다. `source_data`의 **`structured_at`·`structure_attempts`·`image_urls`**(비용 루프 방지·이미지 fetch)와 `review_data`의 `is_church_recruitment`·`job_kind`·`role`·`contact`·`heresy_flag`·`created_at`은 **SPEC §6 참조**.

**① `source_data` — 원자료 + 원장 (불변, write-once)**
| 컬럼 | 타입 | 비고 |
|---|---|---|
| `id` | uuid PK | |
| `source_key` | text | 게시판 id(daeshin·puts…) |
| `external_id` | text | **글번호(URL서 추출)** |
| `source_url` | text | 원문 링크 |
| `fetched_at` | timestamptz | 긁은 시각 |
| `run_id` | uuid FK→crawl_run | |
| `raw_text` | text | **최대 추출 텍스트** |
| `raw_meta` | jsonb | 작성일·조회수·첨부목록 |
| `content_hash` | text | 수정/재게시 감지 |
| — | **UNIQUE(`source_key`,`external_id`)** | ← **원장** |

**② `review_data` — 구조화 초안 + 검수 (가변)**
| 그룹 | 컬럼 |
|---|---|
| 링크 | `id` PK · `source_data_id` FK · `run_id` |
| 공고(jobs 미러) | `title`·`position`·`department`·`employment_type`·`qualification`·`housing_provided`·`stipend_min/max/note/period`·`work_days`·`requirements[]`·`preferred[]`·`required_docs[]`·`description`·`posted_at`·`deadline` |
| 교회 초안 | `church_name`·`region`·`city` |
| 교단 | `denomination`·`denomination_source`·`denomination_evidence`·`raw_denomination` |
| 검수 메타 | `confidence`·`dedup_key`·`review_status`(PENDING/APPROVED/REJECTED)·`matched_church_id` FK→churches·`published_job_id` FK→jobs·`reviewed_by`·`reviewed_at` |

**③ `source_health` — 게시판별 상태 (31행)**
`source_key` PK · `last_run_at` · `last_success_at` · `last_new_count` · `consecutive_failures` · `last_status`(OK/FAIL/ZERO) · `last_error`

**④ `crawl_run` — 실행별 요약 (admin 대시보드)**
`id` PK · `started_at` · `finished_at` · `mode`(BACKFILL/DAILY) · `sources_ok` · `sources_failed` · `new_count` · `error_detail` jsonb(source_key→에러)

**목적지 = `churches`/`jobs`** (DATA.md 스키마 · 승인 시 승격 — **요약 + `source_url` · `source=OPERATOR` · `owner_id=NULL`**, 검수 메타는 넘기지 않음).

### 9.5 실패 감지
- **하드**(접근 제한·URL 변경·타임아웃): HTTP 비2xx/000 → GitHub Actions 빨간불 + 이메일(자동).
- **소프트**(사이트 리뉴얼로 셀렉터 깨짐): 200인데 **0건/급감** → `source_health` baseline 비교 경보 + 내용 sanity(리스트 자리에 로그인폼/에러 감지).

### 9.6 admin 크롤 대시보드
`crawl_run`(최근 실행: 언제·신규 N·실패 M·소요) + `source_health`(게시판별 마지막 성공·건수·연속실패) + `review_data` PENDING 카운트를 min_job admin이 읽어 표시. → "오늘 몇 건 추가/실패, 언제 긁었나" 가시화.

### 9.7 스키마 거버넌스 ★
- **크롤러 staging 스키마(`source_data`·`review_data`·`source_health`·`crawl_run`)는 `min_job_agent`가 소유·문서화·마이그레이션.** min_job 리포는 건드리지 않는다.
- **`../min_job/docs/DATA.md`는 최종 output(`churches`/`jobs`) 스키마만 정본.** 크롤러는 그 output 모양에 맞춰 승격만 하고, 그 위 staging 4테이블은 이 리포 소관.
- 물리적으로는 **min_job Supabase 프로젝트에 함께** 두되(같은 DB라 검수·승격이 단순), **정의·변경 관리는 이 리포**에서. (CONTRACT §3 "크롤러 전용 스테이징 필드"와 정합)

---

## 10. ★ Phase 1-1 현황 (2026-07-30 시점 · 여기서 이어받는다)

> **1-1의 목표**: 게시판 **1곳**에서 공고를 실제로 가져와 원문 그대로 `source_data`에 저장. AI는 안 쓴다. 이게 되면 나머지 30곳은 어댑터를 붙이는 반복 작업(1-4)이 된다.

### 진행

| | 단계 | 상태 | 파일 |
|---|---|---|---|
| ① | **fetch 층** (전송 단일 창구) | ✅ **완료** | `fetch/client.py` · `fetch/robots.py` · `tests/test_fetch_client.py`(36테스트 · mutation 21/21) |
| ② | **YTUS 어댑터** (파싱만) | ⬜ 다음 | `sources/adapters/ytus.py` |
| ③ | **원장 조회 확장** | ⬜ | `store/{base,json_store}.py` 수정 |
| ④ | **`collect` 명령 + `--dry-run`** | ⬜ | `cli.py` · `pipeline/collect.py` |
| ⑤ | **관통 확인 + fixture** | 🔸 fixture 확보 완료 | `tests/fixtures/YTUS/{list,detail}.html` — 개인정보 마스킹 완료(전화 4·이메일 1·실명 1) |

### ① fetch 층이 하는 일 (완료 · 재작업 금지)

모든 HTTP가 여기를 지난다(어댑터·파이프라인의 직접 `httpx` import는 ruff TID251로 금지).
담당: **UA 31곳 동일(브라우저) + 브라우저 헤더 세트** · **config 인코딩 우선**(EUC-KR→cp949) · 타임아웃 20s ·
재시도 3회(429·5xx·연결오류만 · 지수 백오프+지터) · 같은 소스 요청 간격 1.5s · 세션 쿠키(`needs_session`) ·
**본문 길이 하한 200자**(200 OK + 빈 본문을 성공으로 오판하지 않기).

**하지 않는 것**(의도적):
- `www_required`·`http_only` → 이미 `list_url`에 있고 레지스트리가 로드 시 강제. 상대 URL은 `urljoin`이 물려준다. **코드 넣지 말 것.**
- robots `Disallow` → `RESPECT_ROBOTS_DISALLOW=False`(운영자 판단 §2). `Crawl-delay`는 준수(`RESPECT_CRAWL_DELAY=True`).
- `spoof_ua` → UA가 31곳 동일해져 **코드 분기가 없다**(실측 기록으로만 잔존 · §2).
- `soft_200`·`image_only` → 전송이 아니라 **본문 판정**이라 어댑터·파이프라인 몫.

### ② 다음에 할 일 — YTUS 어댑터

`list_postings(opts) → [PostingRef]` + `fetch_posting(ref) → RawPosting` 두 함수뿐. **네트워크를 만지지 않는다.**

YTUS 실측값(`config/sources.json`이 정본):
```
목록   https://www.ytus.ac.kr/board/list/trXXR      pagination /board/list/trXXR/page/{n}
상세   /board/view/trXXR/{id}                       id = 경로 끝 숫자
공지   .notice-row  → 제외(고정공지는 수집 대상 아님)
```

✅ **fixture 확보 완료(2026-08-04)** — `tests/fixtures/YTUS/{list,detail}.html`. 실측으로 확인한 구조:
```
목록  tr 21개 = 헤더 1 + 공지 2(tr.notice-row) + 공고 18
      ⚠️ 목록 "번호"(16718) ≠ URL id(25581) → external_id는 href에서 뽑는다
      날짜는 4번째 칸(작성일 · 3개월 컷오프용) · 제목 2번째 · 작성자 3번째
상세  div.boardViewContent (274자) — **양식 게시판**:
      `교회명 : / 교단명 : 통합 / 담임목사 : / 교회주소 : / 노회명 : / 전화번호 : / 사역시작일 : / 모집부서 :`
      → **교단이 명시돼 있어 stated로 바로 확정**(SPEC §5.3 최상위 근거 · AI 추정 불필요)
      → 담임목사 이름은 제3자 개인정보라 추출하지 않는다(가드레일 #4)
```
⚠️ **2페이지 이후 상세 링크에 `/page/N`이 붙는다**(실측 2026-08-04): `/board/view/trXXR/25556/page/2`.
URL 마지막 조각을 id로 쓰면 **한 페이지 20행이 전부 `"2"`** 가 된다(중복 가드가 실제로 잡았다).
id 위치는 `config`의 `detail_pattern`이 알고 있으니 거기서 구한다(`external_id_from_url`).
`source_url`도 목록 링크 그대로 쓰지 않고 **정규형**으로 만든다 — 안 그러면 같은 글을 1페이지와
2페이지에서 찾았을 때 값이 달라진다.

⚠️ **fixture 마스킹 함정 3가지** (`--save-fixture`가 전부 따라야 한다):
1. BeautifulSoup으로 재직렬화하면 **DOM이 변형된다**(본문 274자→16,303자 관측) → 개인정보는
   파싱으로 **찾고** 치환은 **원문 문자열에 정확히**.
2. 렌더링 텍스트만 보면 **속성 안을 놓친다** — 이 게시판은 상세 링크 `title="…"`에 공고 본문을
   통째로 넣는다(연락처 8건이 그렇게 통과했다).
3. **마스킹과 검증이 같은 패턴을 써야 한다.** 다르면 한쪽이 놓친 것을 다른 쪽이 못 잡는다 —
   마스킹 정규식이 "다음 라벨" lookahead를 요구해서 실명 9건을 놓쳤고, 검증 패턴을 조이자
   드러났다. 조직명(`경안노회`)을 사람 이름으로 오판해 본문을 망가뜨린 일도 있다.

### ③④⑤ 설계 결정 (이미 확정 · SPEC §10에 기록)

- **`external_id` 중복은 에러** — 단 "한 실행 안"만 본다. 실행 간 충돌은 **제목·날짜 대조**로 잡는다(추가 요청 0건: 목록 값 ↔ 저장된 `raw_meta.list_title`·`list_date`). 그래서 ③에서 원장 조회가 `{id: (제목, 날짜)}`를 돌려주게 바꾼다.
- **`--dry-run`이 출력할 것**: 행 수 · 공지 제외 수 · **external_id 중복 수** · 게시일 범위 · 원장 신규 수 · 샘플 3건. 31곳 파싱 검증의 도구다.
- **3개월 컷오프 = 목록의 게시일**(구조화 전이라 `posted_at` 없음). 날짜 없는 소스는 `--months 0` 폴백(날짜가 아예 없으면 `--months`는 **실패**시킨다 — 조용히 상한까지 걷지 않는다). **컷오프가 종료를 정하고 페이지 상한(100p)은 폭주 방지용이며 CLI 옵션으로 노출하지 않는다**(운영자 결정 2026-08-04 — 무조건 기간 단위). ⚠️ "새 글 없는 페이지에서 종료"는 폐기됐다(1개월 뒤 3개월 백필이 첫 페이지에서 멈춘다) — 종료 조건은 **페이지 전체가 컷오프 밖**.
- **수집(무료)과 구조화(유료)는 별도 명령** — 파싱이 틀린 채 수백 건을 Gemini에 보내면 되돌릴 수 없다.

### 미해결 (집에서 결정할 것)

- [x] **`Crawl-delay` 준수 결정(2026-08-04)** — `Disallow`만 무시하고 `Crawl-delay`는 따른다. 표준 파서가 소수점 값을 버리는 함정까지 처리(§2).
- [x] **UA 결정(2026-08-04)** — 31곳 동일 브라우저 UA. YTUS가 자체 UA를 403으로 막은 실측이 근거(§2).
- [ ] **Scrapy 도입 여부** — 마지막 갈림길이었고 `fetch/`를 직접 쓰기로 진행했다(에이전트 권고 = 현 구조 유지: 수집·구조화가 다른 배치라 Scrapy의 item pipeline과 결이 다르고, mypy strict와 충돌). **사실상 결정됨** — 되돌리려면 지금이 마지막.
- [ ] **`collect --save-fixture`** — 가드레일 #7이 "테스트는 fixture로"를 요구하는데 fixture를 **만드는 수단이 아키텍처에 없다**. ROADMAP 1-1에 항목으로 추가됨.
