# SOURCES.md — 교역자 청빙 수집 소스 카탈로그

> **검증 이력**: ① 전국 수집 → ② 냉정 재검증(2026-07-12) → ③ 실측 재검증(2026-07-21, 14 에이전트) → ④ Fable 교차감사(2026-07-21). **살아있는 문서** — 활동성·URL은 수시로 바뀌니 크롤 착수 전 각 소스 재확인.
>
> ⚠️ **로그인 티어**: 인증 크롤은 공개 페이지와 법적 결이 다름(약관·정보통신망법) → **실행 전 변호사 확인 게이트** + 계정은 운영자 제공.
>
> ⚠️ **커버리지**: "공식 게시판만" 정책은 실제 청빙 물량의 **일부만** 커버(교단별 편차 큼 — §5). 통합·고신은 신학교 게시판이 부교역자 1차 창구라 커버율이 높지만, 합동·감리·순복음·침례는 상업 채널 비중이 큼.

## 범례
- **판정**: ✅ 채택 · 🔸 조건부(필터·저조·기술이슈) · 🔒 로그인(게이트 후) · ❓ 확인필요 · ❌ 제외
- **접근**: 공개 / 로그인 / JS·AJAX(정적 크롤 불가 → 엔드포인트·헤드리스)
- **기술 주의**: 봇차단(UA위장) · SSL(-k/http) · EUC-KR · www필수 · OCR(이미지 공고)
- ⭐ = 데모 어댑터 구현됨

---

## 1. 교단별 채택 소스 (9대형 + 기타)

### 예장합동 (HAPDONG) — 창구 = 신학교 (총회는 저조)
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 대신대 취업정보 | `daeshin.ac.kr/html/05_community/03.php` | 공개 | 매우활발(07-18) | ✅ | SSL(-k/http) · 취업+사역 혼합(청빙 필터) |
| 칼빈대 사역취업정보 | `calvin.ac.kr/main/boardList.do?brd_mgrno=692&menu_no=2282` | 공개 | 활발 | ✅ | **http 전용** |
| 광신대 구인게시판 | `www.kwangshin.ac.kr/front/boardList.do?brd_mgrno=184&menu_no=467` | 공개 | 매우활발(07-21) | ✅ | **www 필수** |
| 총신대 사역게시판 | `csu.ac.kr/?m1=page&menu_id=1110` | 공개 | 활발(07-19) | ✅ | JS/REST — 크롤은 `getBoardContent`(board_id 178). Fable 활성 확정 |
| 예장합동 총회 | `old.gapck.org/pds/bbs_list.asp?BGNO=1101&menu=menu1` | 공개 | 저조 | 🔸 | `old.` 도메인 · EUC-KR · 물량 미확정 |

### 예장통합 (TONGHAP) — 소스 최다
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 영남신대 취업/초빙 ⭐ | `ytus.ac.kr/board/list/trXXR` | 공개 | 매우활발(07-21) | ✅ | 데모 기준점 |
| 장신대 초빙(장신Lounge) | `puts.ac.kr/www/board/list.general.asp?bd_name=jangshin_jboard04` | 공개 | 매우활발(주 50~60건) | ✅ | EUC-KR · 통합 최대 물량 |
| 호남신대 미니스트리 | `ministry.htus.ac.kr/board/board.php?b_id=ministry_009` | 공개 | 매우활발(07-21) | ✅ | EUC-KR · 상세 회원제 여부 미확정 |
| 부산장신대 청빙취업안내 | `www.bpu.ac.kr/Board/BoardList.aspx?BoardMstNo=6&CategoryNo=1` | 공개 | 활발(07-21) | ✅ | **www 필수** |
| 예장통합 총회(PCK) | `pck.or.kr/bbs/board.php?bo_table=SM05_05` | 공개 | 활발(07-21) | ✅ | 교단 공식 청빙란 |
| 서울장신대 사역구인정보 | `sjs.ac.kr/ht_ml/w_04ed/4600.php` | 공개 | 활발(07-21) | ✅ | EUC-KR · 상단 고정공지 아래로 매일 게시 |
| 한국기독공보 광고검색 | `pckworld.com/adsearch/` | 공개 | 활발 | ✅ | 지면광고 **이미지형(OCR)** |
| 한일장신대 청빙게시판 | `hanil.ac.kr/portal/default/bbs/list.do?menuId=M0004000500000000` | 공개(AJAX) | 매우활발(07-21, 12,480건) | ✅ | **Fable가 🔸→✅ 뒤집음**: 목록은 AJAX(`article_list.ajax`, boardId=`BBS…262`)로 본문까지 JSON. ⚠️ 에이전트 판정 엇갈려 최종 클릭 확인 권장 |

### 예장백석 (BAEKSEOK)
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 백석대 대학원 정보나눔터 | `community.bu.ac.kr/graduateschool/3938/subview.do` | 공개 | 매우활발(07-21) | ✅ | 월·수·금 게재 |
| 백석총회 사역자구함 | `pgak.net/sys-infra/components/board/list.asp?skin=basic&boardid=B5FF8` | 공개 | 활발 | ✅ | 봇차단(UA) · iframe |
| 백석대신총회 사역자구함 | `bsds.kr/webchon.layout/board/white2022/list.asp?boardid=9A1FD` | 공개 | 저조(2026-02) | 🔸 | 봇차단(UA) · ⚠️ 2019 분열했다 **2026 백석과 재결합 선언 → 유동적**, BAEKSEOK 유지·모니터링(raw 보존) |

### 감리교 (GAMLI) — 총회 막힘, 신학교가 창구
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 감신대 취업게시판 | `mtu.ac.kr/mtu/board/list.do?mId=162` | 공개 | 매우활발(07-20) | ✅ | 상세 `view.do?brdIdx={n}` |
| 협성대 웨슬리 교역자청빙 | `www.uhs.ac.kr/gsthe/2386/subview.do` | 공개 | 활발(07-08) | ✅ | **www 필수** |
| 목원대 신학과 사역지정보 | `mokwon.ac.kr/mt1954/html/sub06/0602.html` | 공개 | 둔화(05-18) | 🔸 | JS 렌더 |

### 순복음 (SUNBOK) — 신학교가 창구
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 한세대 대학원(영산) | `graduate.hansei.ac.kr/graduated/644/subview.do` | 공개 | 활발 | ✅ | 명칭 '모집/채용공고' |
| 순복음대학원대 청빙및취업 | `sts.ac.kr/main/sub.html?pageCode=38` | 공개 | 활발 | ✅ | — |
| 순복음총회신학교 장학/취업 | `kcc.ac.kr/main/sub.html?pageCode=58` | 공개 | 보통 | 🔸 | **장학/취업 혼합 → 청빙 필터** |

### 침례교 (BAPTIST)
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 침신대 취업지원 사역자채용 | `job.kbtus.ac.kr/job/CMS/Board/Board.do?mCode=MN014` | 공개 | 활발 | ✅ | 롤링(최신 ~20건) |
| 침례회 총회 목회자청빙 ⭐ | `koreabaptist.or.kr/Board/Index/21317` | 공개 | 매우활발 | ✅ | `21317`=보드 식별자 확인(글번호 아님) · 이미지 공고 잦음(OCR) |

### 성결교 (SEONGGYUL) — 기성·예성 **별개 교단** 공존
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 기성 총회 성결광장 구인 ⭐ | `kehc.org/home/recruit/view_list/page/0` | 공개 | 활발(07-19) | ✅ | 기독교대한성결교회(기성) · 신학교 창구(서울신대)는 §3 확인필요 |
| 예성 총회 구인/청빙 | `sungkyul.org/NOS-Board/bbs.php?idx=com9` | 공개 | 활발 | ✅ | 봇차단(UA) · 예수교대한성결교회(예성) |

### 예장고신 (GOSIN) — KTS 하나로 충분
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 고려신학대학원(KTS) 교역자초빙 | `kts.ac.kr/home/pinvit` | 공개 | 매우활발(07-18) | ✅ | SSL(-k) · 누적 대량(정확치 재실측)* |
| 고신대 신학과 자유게시판 | `best.kosin.ac.kr/th/index.php?pCode=MN6000030&mode=list` | 공개 | 저조(월 1~2건) | 🔸 | 청빙 혼재 · 타 교단 교회도 섞임 |

### 예장합신 (HAPSIN)
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 합신대 교역자초빙 | `hapdong.ac.kr/bbs/board.php?bo_table=e03` | 공개 | 매우활발(07-21) | ✅ | ⚠️ 도메인·키가 `hapdong`이나 **교단은 예장합신(합동 아님)** — 오분류 주의 |

### 기타 (ETC) — 기장·독립
| 소스 | 게시판 URL | 접근 | 활동성 | 판정 | 주의 |
|---|---|:--:|---|:--:|---|
| 기장 총회(PROK) 교역자청빙 ⭐ | `prok.org/Board/Index/34` | 공개(목록) | 활발(07-19) | ✅ | 상세는 로그인벽 → 목록 메타만. 안정 인덱스 `/Board/Index/34`(구 `/176244`는 글번호) |
| KAICAM 독립교회연합회 청빙·청원 | `home.kaicam.org/webchon.layout/board/white2022/list.asp?boardid=D9537` | 공개 | 활발 | ✅ | 봇차단(UA) |

> \* mtu·kts의 "누적 게시물 수"는 3차 문서에서 둘 다 `10,955`로 잘못 복제돼 있던 것을 제거했다(Fable 지적). 정확 누적치는 재실측 전까지 표기하지 않는다.

---

## 2. 로그인 티어 (인증 크롤 — 법률게이트 후 실행)
| 소스 | 게시판 URL | 교단(enum) | 비고 |
|---|---|:--:|---|
| 기감 총회(KMC) | `kmc.or.kr/news-kmc/the-wants-cloumns` | GAMLI | 감리교 공식 대표 창구 · 로그인+관리자승인 · 봇차단 |
| 하나님의성회 총회(AGK) | `agk.or.kr/bbs/board.php?bo_table=sub505` | SUNBOK | 완전 로그인벽(목록·상세·RSS 전부 차단) — Fable 재확인 |
| 기독신문 구인구직 | `kidok.com/bbs/list.html?table=bbs_9` | **HAPDONG** | 예장합동 공식 기관지(Fable 확정, '기타' 아님) · **목록조차 전면 로그인벽 → 사실상 제외 후보** |

---

## 3. ❓ 확인 필요 / 재부상 후보 (미채택 — 클릭/재정찰 후 결정)
| 후보 | 힌트 | 상태 |
|---|---|---|
| **서울신학대 사역게시판** | `stc68.net/board`(iboardgroupseq=5) | **편입 유력(HIGH)** — 기성(최대 성결 교단) **신학교 창구가 통째로 누락**. Fable 활성 확인, SSL/봇 이슈로 최종 확인 필요 |
| **아신대(ACTS) 사역정보** | `acts.ac.kr` (id=`acts_csrd_guide`) | **충돌** — 1차 조사 '휴면(0건)' vs Fable '활성(수백건)'. 초교파 축 → 재확인 |
| 나사렛성결회 목회자청빙 | `na.or.kr/ccall` | 실재 확인(112건, 07-06) 저물량 → 정규화상 ETC, 우선순위 낮음 |
| 순복음통합 총회 | `agkr.org`(?) | 순복음 '다른 절반' 후보(agk는 여의도측). 게시판 실재·물량 미확인 |
| 서울성경신학대학원대(SU) | `sb.ac.kr` | 초교파 개혁, 교역자청빙 활발 추정 — 미검증 |
| 대전신학대 | `daejeon.ac.kr` | 게시판 미발견(과거 '없음' 유력) → **근거 약함**, 후순위 |

---

## 4. ❌ 제외
- **게시판 없음**: 아이굿뉴스 · 성결대 · 한국성결신문 · 대전신대(잠정)
- **휴면**: 예수교대한하나님의성회 `aogk.org`(2018)
- **실존하나 청빙 게시판 부재**: `korea-ag.com`(순복음측 총회) — Fable 전체 HTML 검증 결과 청빙판 없음 + SSL 만료·저활동 → **기각**(재조사 반복 방지용 기록)
- **중복**: 고신 총회(→ KTS)

---

## 5. CROSS 범교단 (상업/매체 — 초기 제외 · 커버리지 참고)
| 소스 | URL | 활동성 | 주의 |
|---|---|---|---|
| 제이웹(JCWeb) | `jcweb.net/calling/` | 매우활발(누적 대량) | 봇차단 · RSS 제공 |
| 청빙넷 | `minitries.co.kr` | 매우활발 | 봇차단 · **자칭 "청빙 게시판 재게시 중개"**(공식 게시판과 상당 중복) · 철자 그대로가 실제 도메인 |
| 기독정보넷(cjob) | `cjob.co.kr/offerKG` | 활발(누적 대량) | 봇차단(403) — 물량 측정 실패 |
| 갓피플취업 | `recruit.godpeople.com` | 활발 | 무료 등록 → 중소·개척 물량 |
| CTS 채용정보 | `cts.tv/board/recruit` | 활발 | 방송사 운영, 전국 |
| 국민일보 더미션 | `themission.co.kr` | — | 지면 청빙광고 |
| 기독일보 청빙판 | `bbs.kr.christianitydaily.com` | 주 8~10건 | 비로그인 · **내용이 미주 한인교회 편중**(국내 기여 낮음) |
| 웨스트민스터신대원(WGST) | `wgst.ac.kr/.../board.asp?key=6131` | 매우활발 | 초교파 |

> **커버리지(냉정)**: "공식 게시판만" 정책이 담는 실물량은 **교단별 편차가 커 단일 수치로 못 박기 어렵다.** 통합·고신은 신학교 게시판(장신대 주 50~60건 등)이 부교역자 1차 창구라 공식 커버율이 높고, 합동·감리·순복음·침례는 상업 채널 비중↑. 전체 가중 추정은 **하한 ~25~35% ~ 상한 ~35~55%**(최대 애그리게이터 cjob·합동 공식채널 측정 실패로 **확정 불가**). 물량 극대화가 목표면 CROSS(특히 갓피플·cjob) 정책 재검토 대상.

---

## 6. 크롤 기술 요건 (어댑터 설계)
- **UA 위장 필요**(기본 UA 403): `pgak.net` · `bsds.kr` · `sungkyul.org`(예성) · `home.kaicam.org` · cjob · jcweb
- **SSL 처리**(http 또는 -k): `calvin.ac.kr`(http 전용) · `daeshin.ac.kr` · `kts.ac.kr`
- **JS/AJAX/REST 엔드포인트**(정적 크롤 불가):
  - `csu`(총신) → 공개 REST `POST /api/board/getBoardContent`(board_id 178, 세션 불필요)
  - `hanil`(한일장신) → `POST /portal/bbs/article_list.ajax`(boardId `BBS00000000000000262`) → 본문까지 JSON
  - `mokwon` · godpeople → 헤드리스
- **인코딩 EUC-KR**: `puts`(장신) · `ministry.htus`(호남) · `sjs`(서울장신) · `old.gapck`
- **이미지 공고(OCR 후보)**: `pckworld`(한국기독공보 지면광고) · `koreabaptist` 일부

---

## 7. 크롤 우선순위
- **Tier 1 (공개·활발·즉시)**: 대신대·칼빈대·광신대·총신대(합동) · 영남·장신·호남·부산장신·PCK·서울장신·한국기독공보·**한일장신**(통합) · 백석대대학원·백석총회(백석) · 감신대·협성대(감리) · 한세대·순복음대학원대(순복음) · 침신대·침례총회(침례) · 기성·예성(성결) · KTS(고신) · 합신대(합신) · 기장·KAICAM(기타)
- **Tier 2 (조건부·보완)**: 예장합동총회(저조) · 목원대(둔화) · 순복음총회신학교·고신대신학과(청빙 필터) · 백석대신총회(유동)
- **로그인 티어 (게이트·계정 후)**: KMC · AGK  (기독신문은 전면 로그인벽이라 **제외 유력**)
- **확인 필요 (§3)**: 서울신대 · 아신대 · 나사렛 · SU · agkr · 대전신대
- **초기 제외 (참고용)**: 청빙넷 · 제이웹 · cjob · 갓피플 · CTS · 더미션 · WGST
