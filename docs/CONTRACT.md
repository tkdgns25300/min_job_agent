# CONTRACT.md — 크롤러 출력 계약 & 정규화

> 크롤러가 수집·구조화한 데이터를 **어떤 스키마로, 어떤 교단 값으로, 어떻게 min_job에 넘길지**의 계약.
> 소스 목록·활동성은 [`SOURCES.md`](./SOURCES.md), 파이프라인 동작은 [`SPEC.md`](./SPEC.md), 아키텍처는 [`../CLAUDE.md`](../CLAUDE.md).
>
> **출력 필드 스키마 정본 = `../min_job/docs/DATA.md`.** 이 문서는 그 위에 (a) 교단 정규화 (b) 크롤러 전용 스테이징 필드 (c) 소스→교단 매핑 (d) 어댑터 기술요건을 얹는다.

---

## 1. 교단 enum (확정 2026-07-12) — 9개 대형 + 기타

`HAPDONG` 예장합동 · `TONGHAP` 예장통합 · `BAEKSEOK` 예장백석 · `GAMLI` 감리교 · `SUNBOK` 순복음 · `BAPTIST` 침례교 · `SEONGGYUL` 성결교 · `GOSIN` 예장고신 · `HAPSIN` 예장합신 · `ETC` 기타

- **min_job `constants/domain.ts` 정합 작업**: 현재 11개(합신·기장 포함)에서 **`KIJANG` 제거**만 하면 10키(=9대형+ETC)로 일치. (합신은 그대로 유지)
- **기장·예장군소분파·나사렛·루터·그리스도의교회 등 → `ETC`** (+ 매핑 실패 시 운영자 플래그)

---

## 2. 교단 태깅 규칙 (소스 default + 공고별 override)

```
1) 소스 어댑터마다 defaultDenomination 지정 (아래 §4 표).
2) 크롤 시 공고 교단 = defaultDenomination.
3) AI 구조화가 공고 본문·교회명에서 교단 신호를 읽어,
   default와 명백히 다르면 override (예: 합동 신학교 게시판의 "예장 통합 ○○교회" 공고).
4) override 결과가 9개 **대형** key 밖이면 → ETC + 운영자 플래그(자동 게재 금지, 정통 화이트리스트).
5) 항상 raw_denomination(원문 표기)을 스테이징에 보존 → 검토·향후 재매핑용.
```

> 대부분 소스는 교단 고정이라 3)은 예외 처리. **혼재 소스**(고신대 신학과 자유게시판 등)만 3)이 상시 작동.

### 정규화 alias 맵 (raw 문자열 → key)
| key | 매칭 표현(부분일치) |
|---|---|
| HAPDONG | 예장합동, 합동, 합동측, 예장(합동), 대한예수교장로회(합동), gapck |
| TONGHAP | 예장통합, 통합, 예장(통합), PCK, 대한예수교장로회(통합) |
| BAEKSEOK | 예장백석, 백석, 백석대신, 합동정통(백석 전신) |
| GAMLI | 감리, 기감, 기독교대한감리회, 예수교대한감리회 |
| SUNBOK | 순복음, 하나님의성회, 기하성, 여의도순복음, AGK |
| BAPTIST | 침례, 기침, 기독교한국침례회 |
| SEONGGYUL | 성결, 기성, 예성, 기독교대한성결교회, 예수교대한성결교회 |
| GOSIN | 고신, 예장고신, 대한예수교장로회(고신), 고려신학 |
| HAPSIN | 합신, 예장합신, 합동신학, 대한예수교장로회(합신) |
| ETC | 기장, 한국기독교장로회, PROK, 예장(대신·개혁·호헌·합동보수 등 군소), 독립, 나사렛, 루터, 그리스도의교회, **그 외 전부** |

---

## 3. 크롤러 스테이징 레코드 (min_job 최종 스키마 + 크롤러 전용 필드)

크롤러는 min_job `jobs`/`churches`(DATA.md) 형태로 구조화하되, **리뷰 큐용 메타 필드**를 추가로 붙인다. 이 메타는 min_job 최종 DB엔 안 들어가고 운영자 검토·dedup·감사용:

| 필드 | 용도 |
|---|---|
| `raw_denomination` | 원문 교단 표기 (재매핑용) |
| `raw_text` | 수집 원문 전문 (구조화 전 — 감사·재구조화·법적 근거) |
| `source_key` | 어댑터 id (예: `ytus`, `kts`, `kehc`) |
| `source_url` | 공고 원문 URL (min_job `jobs.source_url`로 승계) |
| `source_board` | 게시판 식별 |
| `fetched_at` | 수집 시각 |
| `dedup_key` | 중복 판정 키 (§5) |
| `review_status` | `PENDING`/`APPROVED`/`REJECTED` (운영자 검토) |
| `confidence` | AI 구조화 신뢰도 (낮으면 운영자 우선 검토) |

> **가드레일**: 개인 담당자 연락처는 `raw_text`에 남더라도 구조화 필드로 추출·노출하지 않는다(교회 공개 채널·원문 링크만). owner_id는 크롤 공고 전부 NULL(운영자 등록, source=OPERATOR).

---

## 4. 소스 → default 교단 + 크롤 모드 + 기술요건

> 모드 A = 교단 고정(override는 예외) · B = 혼재(공고별 감지 상시). 상세 활동성은 SOURCES.md.

| source_key | 소스 | default 교단 | 모드 | 접근 | 기술요건 |
|---|---|:--:|:--:|:--:|---|
| `daeshin` | 대신대 취업정보 | HAPDONG | A | 공개 | SSL(-k), 서버렌더(curl로 확보) |
| `calvin` | 칼빈대 사역취업정보 | HAPDONG | A | 공개 | **http 전용** |
| `kwangshin` | 광신대 구인게시판 | HAPDONG | A | 공개 | — |
| `csu` | 총신대 사역게시판 | HAPDONG | A | 공개 | 공개 REST `getBoardContent`(board_id 178) · 활성 확정(Fable) |
| `gapck` | 예장합동 총회 | HAPDONG | A | 공개 | `old.` 도메인, EUC-KR |
| `ytus` | 영남신대 | TONGHAP | A | 공개 | — |
| `puts` | 장신대 초빙 | TONGHAP | A | 공개 | EUC-KR |
| `htus` | 호남신대 미니스트리 | TONGHAP | A | 공개(상세 회원?) | EUC-KR |
| `bpu` | 부산장신대 | TONGHAP | A | 공개 | **www 호스트 필수** |
| `pck` | 예장통합 총회 | TONGHAP | A | 공개 | — |
| `sjs` | 서울장신대 | TONGHAP | A | 공개 | EUC-KR |
| `pckworld` | 한국기독공보 광고검색 | TONGHAP | A | 공개 | 지면광고형 |
| `hanil` | 한일장신대 | TONGHAP | A | 공개(AJAX) | `article_list.ajax`(boardId `BBS…262`)→JSON · **매우활발(Fable가 🔸→✅)** |
| `bu` | 백석대 대학원 정보나눔터 | BAEKSEOK | A | 공개 | — |
| `pgak` | 백석총회 | BAEKSEOK | A | 공개 | **UA위장**, iframe |
| `bsds` | 백석대신총회 | BAEKSEOK | A | 공개 | UA위장(저조) · 백석대신은 2019 분열했다 **2026 백석과 재결합 선언 → 유동적**(BAEKSEOK 유지·모니터링, raw 보존) |
| `mtu` | 감신대 취업게시판 | GAMLI | A | 공개 | 상세 `view.do?brdIdx=` |
| `uhs` | 협성대 웨슬리 | GAMLI | A | 공개 | **www 호스트 필수** · 상세 `/bbs/.../artclView.do` |
| `mokwon` | 목원대 사역지정보 | GAMLI | A | 공개 | JS 렌더 |
| `kmc` | 기감 총회(KMC) | GAMLI | A | **로그인** | 인증+법률게이트 |
| `hansei` | 한세대 대학원 | SUNBOK | A | 공개 | — |
| `sts` | 순복음대학원대 | SUNBOK | A | 공개 | — |
| `kcc` | 순복음총회신학교 | SUNBOK | A | 공개 | 청빙 필터 |
| `agk` | 하나님의성회 총회 | SUNBOK | A | **로그인** | 인증+법률게이트 |
| `kbtus` | 침신대 취업지원 | BAPTIST | A | 공개 | 롤링(최신만) |
| `koreabaptist` | 침례회 총회 | BAPTIST | A | 공개 | — |
| `kehc` | 기성 총회 | SEONGGYUL | A | 공개 | — |
| `sungkyul` | 예성 총회 | SEONGGYUL | A | 공개 | **UA위장** |
| `kts` | 고려신학대학원(KTS) | GOSIN | A | 공개 | SSL(-k) |
| `kosin_th` | 고신대 신학과 자유게시판 | GOSIN | **B** | 공개 | 청빙 필터+혼재 |
| `hapdong` | 합신대 교역자초빙 | HAPSIN | A | 공개 | — |
| `prok` | 기장 총회(PROK) | ETC | A | 공개 | 신 .NET URL · 안정 인덱스 `/Board/Index/34` 권장 |
| `kaicam` | KAICAM 독립교회연합회 | ETC | A | 공개 | **UA위장** |
| `kidok` | 기독신문 구인구직 | HAPDONG* | B | **로그인** | 인증+법률게이트, 범교회 · **목록조차 전면 로그인벽 → 제외 재검토** |

\* 기독신문은 교단지지만 여러 교단 교회가 이용 → 모드 B.

**제외(크롤 안 함)**: 대전신대·아이굿뉴스·서울신대·성결대·한국성결신문(게시판 없음), 예수교대한하나님의성회·아신대(휴면), 고신총회(KTS 중복). **CROSS 상업**(청빙넷·제이웹·cjob·갓피플·WGST)은 "공식 게시판만" 정책으로 초기 제외.

> **재검증 반영(2026-07-21 · 3차 실측 + Fable 교차감사 · 상세 SOURCES)**: URL 수정 `bpu`·`uhs`(www 필수) · `prok` 안정 인덱스 `/Board/Index/34`. **판정 변경**: `hanil` 🔸→✅(AJAX로 매우활발 확인) · `csu` 활성 확정 · `kidok`=예장합동 기관지 + 전면 로그인벽 → 제외 유력 · `bsds`=2026 백석 재결합 선언으로 유동적(BAEKSEOK 유지·모니터링, 하드 재분류 보류). **기각**: `korea-ag`(청빙판 없음). **확인 필요**: 서울신대(`stc68.net`, 기성 신학교 창구 누락·편입 유력) · 아신대 ACTS(휴면 vs 활성 충돌) · 나사렛(`na.or.kr/ccall`) · SU · `agkr` · 대전신대. **⚠️ 커버리지**: "공식만" = 실물량 일부(교단별 편차 큼, 대략 ~25~55% 범위·확정불가). 상세 SOURCES §3·§5.

---

## 5. 중복 판정 (dedup) — 재공고는 보존

- **run 내 dedup**: 같은 `source_url`(또는 source_key+게시판 글번호) 이미 수집 → skip.
- **cross-source dedup**: 같은 공고가 여러 게시판에 교차 게시될 수 있음(예: 풍기제일교회가 칼빈대·광신대·대신대 동시). `dedup_key = 정규화(교회명 + 직분 + 사례비 + 연락처 or 마감일)`로 근사 판정 → 운영자 검토 시 병합 후보 제시.
- ⚠️ **재공고(같은 교회·자리의 다른 시점 공고)는 절대 합치지 않는다** — min_job 차별점(재공고 추적). dedup은 "같은 공고의 중복 게시"까지만.

---

## 6. 로그인 티어 법률 게이트 (필수 준수)

`kmc`·`agk`·`kidok` 등 로그인 소스의 **인증 크롤 실행 전** 반드시:
- [ ] 각 사이트 이용약관의 자동수집·계정 조항 확인
- [ ] 인증 뒤 자동수집이 앞선 법률검토(공개 공식 출처 기준) 범위에 포함되는지 변호사 확인
- [ ] 계정은 사용자(운영자)가 제공 — 크롤러가 가입·인증을 자동화하지 않음

통과 전까지 이 소스들은 카탈로그에만 두고 크롤 실행하지 않는다.
