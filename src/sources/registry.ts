import type { SourceConfig } from "./types";

/**
 * 게시판 리스트(소스 레지스트리) — 크롤 대상 31곳의 정의. 매 실행의 출발점(SPEC §2 ①).
 * 값은 **2026-07-29 라이브 검수**(4 subagent가 각 URL을 실제 fetch)로 확정 — 이게 전송(fetch)
 * 특성의 최신 정본이다(SOURCES §6보다 우선; 문서와 다른 부분은 fetchNote에 표기).
 * 추가/제외 = 이 배열 편집(제외는 enabled:false로 이력 보존). 셀렉터·상세 파싱은 어댑터(Phase 1-4).
 *
 * ⚠️ 라이브 검수로 드러난 문서 정정:
 *  - spoofUA / insecureTLS(-k)는 **현재 31곳 중 필요한 곳 없음**(daeshin·kts 인증서 정상, pgak·예성·kaicam UA 불요).
 *  - MOKWON = 정적(문서의 headless 의심 오류) · ACTS = headless 확정 · KBTUS = utf-8(euc-kr 아님).
 *  - CSU = '공개 REST' 아님(세션 필요) · KAICAM = soft-404(본문 정상) · PCKWORLD 상세 = JS 팝업.
 */
export const REGISTRY: SourceConfig[] = [
  // ── 예장합동 (HAPDONG) ──
  {
    key: "DAESHIN", boardName: "대신대 취업정보", denominationHint: "HAPDONG",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://daeshin.ac.kr/html/05_community/03.php",
    detailPattern: "/html/05_community/03.php?AT=V&b_id={id}",
    fetchNote: "취업+사역 혼합(청빙 필터 필요). 목록/상세 동일 .php의 AT param. pagination b_page. 인증서 정상(-k 불요).",
  },
  {
    key: "CALVIN", boardName: "칼빈대 사역취업정보", denominationHint: "HAPDONG",
    enabled: true, fetchTier: "static", encoding: "utf-8", flags: { httpOnly: true },
    listUrl: "http://calvin.ac.kr/main/boardList.do?brd_mgrno=692&menu_no=2282",
    detailPattern: "/main/boardView.do?brd_mgrno=692&menu_no=2282&brd_no={id}",
    fetchNote: "eGov. https 연결거부→http 전용. onclick fView('{id}')→brd_no. pagination page_now. ⚠️2차검증: 상세는 **세션 쿠키 필요** — 목록 GET으로 JSESSIONID 확보 후 그 쿠키로 boardView(쿠키자 없이 cold 요청은 404).",
  },
  {
    key: "KWANGSHIN", boardName: "광신대 구인게시판", denominationHint: "HAPDONG",
    enabled: true, fetchTier: "static", encoding: "utf-8", flags: { wwwRequired: true },
    listUrl: "https://www.kwangshin.ac.kr/front/boardList.do?brd_mgrno=184&menu_no=467",
    detailPattern: "/front/boardView.do?brd_mgrno=184&menu_no=467&brd_no={id}",
    fetchNote: "eGov(calvin 계열). apex 인증서 불일치→www 필수. onclick fView('{id}')→brd_no.",
  },
  {
    key: "CSU", boardName: "총신대 사역게시판", denominationHint: "HAPDONG",
    enabled: true, fetchTier: "json", encoding: "utf-8",
    listUrl: "https://csu.ac.kr/?m1=page&menu_id=1110",
    detailPattern: "POST /api/board/getBoardContent (body {id}) — GET ?id= 는 405",
    fetchNote: "⚠️SPA(총신). '공개 REST' 아님 — 목록=POST /api/user/board/getBoardContentSummaryList(board_id)+세션쿠키(2차검증: 세션 없으면 code 22000). 상세도 POST. menu_id 1110=사역·1111=취업. 세션+POST 또는 headless.",
  },

  // ── 예장통합 (TONGHAP) ──
  {
    key: "YTUS", boardName: "영남신대 취업/초빙", denominationHint: "TONGHAP",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://www.ytus.ac.kr/board/list/trXXR",
    detailPattern: "/board/view/trXXR/{id}",
    fetchNote: "2차검증: apex도 200 → www 불요(flag 제거). .notice-row 공지 skip. id=경로 끝 숫자. pagination /board/list/trXXR/page/{n}.",
  },
  {
    key: "PUTS", boardName: "장신대 초빙(장신Lounge)", denominationHint: "TONGHAP",
    enabled: true, fetchTier: "static", encoding: "euc-kr",
    listUrl: "https://puts.ac.kr/www/board/list.general.asp?bd_name=jangshin_jboard04",
    detailPattern: "/www/board/view.general.asp?seq={id}&bd_name=jangshin_jboard04",
    fetchNote: "EUC-KR·classic ASP. 공지행에 타 게시판(jnotice02) 혼입→bd_name=jangshin_jboard04 필터. pagination page+pagesize=50.",
  },
  {
    key: "HTUS", boardName: "호남신대 미니스트리", denominationHint: "TONGHAP",
    enabled: true, fetchTier: "static", encoding: "euc-kr",
    listUrl: "https://ministry.htus.ac.kr/board/board.php?b_id=ministry_009",
    detailPattern: "/board/board.php?b_id=ministry_009&w_id={id}",
    fetchNote: "EUC-KR. 상세 공개 확정(로그인 불요 — 문서의 '회원제?' 해소). w_id=글번호.",
  },
  {
    key: "BPU", boardName: "부산장신대 청빙취업안내", denominationHint: "TONGHAP",
    enabled: true, fetchTier: "static", encoding: "utf-8", flags: { wwwRequired: true },
    listUrl: "https://www.bpu.ac.kr/Board/BoardList.aspx?BoardMstNo=6&CategoryNo=1",
    detailPattern: "/Board/BoardView.aspx?BoardNo={id}&BoardMstNo=6&CategoryNo=1",
    fetchNote: "ASP.NET. apex 404→www 필수. pagination PageNo. BoardMstNo=6이 이 게시판.",
  },
  {
    key: "PCK", boardName: "예장통합 총회(PCK)", denominationHint: "TONGHAP",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://pck.or.kr/bbs/board.php?bo_table=SM05_05",
    detailPattern: "/bbs/board.php?bo_table=SM05_05&wr_id={id}",
    fetchNote: "그누보드. bo_notice 5개 pinned skip. pagination page. apex OK(www 불요).",
  },
  {
    key: "SJS", boardName: "서울장신대 사역구인정보", denominationHint: "TONGHAP",
    enabled: true, fetchTier: "static", encoding: "euc-kr",
    listUrl: "https://sjs.ac.kr/ht_ml/w_04ed/4600.php",
    detailPattern: "/ht_ml/w_04ed/4600.php?bbs_idx={id}&pagekind=c&bbsid=main4600",
    fetchNote: "EUC-KR(charset 헤더 없음). 공지 pinned. pagination pageno.",
  },
  {
    key: "PCKWORLD", boardName: "한국기독공보 광고검색", denominationHint: "TONGHAP",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://pckworld.com/adsearch/",
    detailPattern: "/adsearch/ad_view.php?aid={id}",
    fetchNote: "2차검증: 상세 = /adsearch/ad_view.php?aid={id}(adview() JS가 여는 URL, headless 불요). 본문이 **JPG 이미지**(/upimg/adsearch/…jpg)·텍스트 없음→Gemini 멀티모달 필수. 목록 ul.grid>li: 제목 span+썸네일. id=aid.",
  },
  {
    key: "HANIL", boardName: "한일장신대 청빙게시판", denominationHint: "TONGHAP",
    enabled: true, fetchTier: "json", encoding: "utf-8", flags: { wwwRequired: true },
    listUrl: "https://www.hanil.ac.kr/portal/default/bbs/list.do?menuId=M0004000500000000",
    detailPattern: "/portal/default/bbs/view.do?menuId=M0004000500000000&boardId=BBS00000000000000262&boardSeq={id}",
    fetchNote: "⚠️apex hang→www 필수. tier=json: POST /portal/bbs/article_list.ajax (form boardId=BBS00000000000000262&menuId=M0004000500000000&pageIndex=N)→JSON{cnt,list:[{boardSeq,title,contents,noticeYn}]}. contents가 목록 JSON에 포함→상세 불요. noticeYn='Y' skip.",
  },

  // ── 예장백석 (BAEKSEOK) ──
  {
    key: "BU", boardName: "백석대 대학원 정보나눔터", denominationHint: "BAEKSEOK",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://community.bu.ac.kr/graduateschool/3938/subview.do",
    detailPattern: "/bbs/graduateschool/1110/{id}/artclView.do",
    fetchNote: "⚠️2차검증 정정: 상세 경로에 **/bbs 프리픽스 필요**(빼면 200 에러쉘=silent fail). Konnect subview.do가 bbs 1110 래핑. HEAD Content-Length:0→GET. 월·수·금.",
  },
  {
    key: "PGAK", boardName: "백석총회 사역자구함", denominationHint: "BAEKSEOK",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://pgak.net/sys-infra/components/board/list.asp?skin=basic&boardid=B5FF8",
    detailPattern: "/sys-infra/components/board/view.asp?boarddetailseq={id}&boardid=B5FF8",
    fetchNote: "⚠️문서의 spoofUA·iframe 오류(재현 안 됨). 서버렌더 tr.list. Cloudflare. HEAD→404·빈 UA→520(GET+UA 사용).",
  },

  // ── 예장고신 (GOSIN) ──
  {
    key: "KTS", boardName: "고려신학대학원(KTS) 교역자초빙", denominationHint: "GOSIN",
    enabled: true, fetchTier: "static", encoding: "utf-8", flags: { wwwRequired: true },
    listUrl: "https://www.kts.ac.kr/home/pinvit",
    detailPattern: "/home/pinvit/{id}",
    fetchNote: "그누보드(bo_table=pinvit). apex 301→www. 인증서 정상(-k 불요 — 문서 오류). bo_notice pinned. pagination page.",
  },
  {
    key: "KOSIN_TH", boardName: "고신대 신학과 자유게시판", denominationHint: "GOSIN",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://best.kosin.ac.kr/th/index.php?pCode=MN6000030&mode=list",
    detailPattern: "/th/index.php?pCode=MN6000030&mode=view&idx={id}",
    fetchNote: "IIS/PHP. 공지 pinned. 청빙+타 교단 혼재→교단은 공고별 판정. HEAD Content-Length:0→GET.",
  },

  // ── 예장합신 (HAPSIN) ──
  {
    key: "HAPSHIN", boardName: "합신대 교역자초빙", denominationHint: "HAPSIN",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://hapdong.ac.kr/bbs/board.php?bo_table=e03",
    detailPattern: "/bbs/board.php?bo_table=e03&wr_id={id}",
    fetchNote: "⚠️도메인 hapdong.ac.kr이나 교단=예장'합신'(합동 아님). source_key는 혼동 방지 위해 HAPSHIN. 그누보드. 공지 6개 pinned. apex·www 둘다 OK. pagination page(731p).",
  },

  // ── 감리교 (GAMLI) ──
  {
    key: "MTU", boardName: "감신대 취업게시판", denominationHint: "GAMLI",
    enabled: true, fetchTier: "static", encoding: "utf-8", flags: { wwwRequired: true, spoofUA: true },
    listUrl: "https://www.mtu.ac.kr/mtu/board/list.do?mId=162",
    detailPattern: "/mtu/board/view.do?mId=162&brdIdx={id}",
    fetchNote: "⚠️2차검증: 기본 UA→보안차단 스텁(0건), **브라우저 UA 필수**(spoofUA). apex 301→www. 상세에 mId=162 필요. brdIdx=글번호.",
  },
  {
    key: "UHS", boardName: "협성대 웨슬리 교역자청빙", denominationHint: "GAMLI",
    enabled: true, fetchTier: "static", encoding: "utf-8", flags: { wwwRequired: true },
    listUrl: "https://www.uhs.ac.kr/gsthe/2386/subview.do",
    detailPattern: "/bbs/gsthe/183/{id}/artclView.do",
    fetchNote: "Konnect subview. apex 000(dead)→www 필수. 목록 menu 2386·board 183. id=artclNo(경로 세그먼트).",
  },
  {
    key: "MOKWON", boardName: "목원대 신학과 사역지정보", denominationHint: "GAMLI",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://mokwon.ac.kr/mt1954/html/sub06/0602.html",
    detailPattern: "/mt1954/html/sub06/0602.html?mode=V&no={id}",
    fetchNote: "⚠️문서의 headless 의심 오류 → 실제 완전 static(tbody 서버렌더, AJAX 없음). PCMS. no=32자리 hex. 공지 class=bbs_notice.",
  },

  // ── 순복음 (SUNBOK) ──
  {
    key: "HANSEI", boardName: "한세대 대학원(영산) 모집/채용", denominationHint: "SUNBOK",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://graduate.hansei.ac.kr/graduated/644/subview.do",
    detailPattern: "/bbs/graduated/{catId}/{id}/artclView.do",
    fetchNote: "Konnect subview(서버렌더). graduate. 서브도메인 필수. id=artclNo(경로). catId 카테고리별 상이.",
  },
  {
    key: "STS", boardName: "순복음대학원대 청빙및취업", denominationHint: "SUNBOK",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://sts.ac.kr/main/sub.html?pageCode=38",
    detailPattern: "/main/sub.html?Mode=view&boardID=www38&num={id}",
    fetchNote: "anyboard. 목록 pageCode=38 / 상세 boardID=www38&Mode=view&num. num=글번호.",
  },

  // ── 침례교 (BAPTIST) ──
  {
    key: "KBTUS", boardName: "침신대 취업지원 사역자채용", denominationHint: "BAPTIST",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://job.kbtus.ac.kr/job/CMS/Board/Board.do?mCode=MN014",
    detailPattern: "/job/CMS/Board/Board.do?mCode=MN014&mode=view&mgr_seq=91&board_seq={id}",
    fetchNote: "⚠️GET 본문은 UTF-8(HEAD 헤더가 EUC-KR로 오보고). HEAD→400이라 GET 사용. 상세링크 javascript:URL_encode()이나 실제는 GET query. ~20건 롤링.",
  },
  {
    key: "KOREABAPTIST", boardName: "침례회 총회 목회자청빙", denominationHint: "BAPTIST",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://koreabaptist.or.kr/Board/Index/21317",
    detailPattern: "/Board/Detail/21317/{id}",
    fetchNote: "21317=board 식별자(글번호 아님). row onclick location.href. 이미지 공고 다수→Gemini 멀티모달. id=마지막 경로.",
  },

  // ── 성결교 (SEONGGYUL) ──
  {
    key: "KEHC", boardName: "기성 총회 성결광장 구인", denominationHint: "SEONGGYUL",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://kehc.org/home/recruit/view_list/page/0",
    detailPattern: "/home/recruit/read_post/{id}",
    fetchNote: "기독교대한성결교회(기성). read_post({id}) JS→경로. pagination /view_list/page/N(step 50). 저번호 공지 pinned.",
  },
  {
    key: "SUNGKYUL", boardName: "예성 총회 구인/청빙", denominationHint: "SEONGGYUL",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://sungkyul.org/NOS-Board/bbs.php?idx=com9",
    detailPattern: "https://www.sungkyul.org/NOS-Board/bbs.php?uid={id}&idx=com9&retype=view",
    fetchNote: "2차검증: 빈 UA→403이라 **UA 문자열 필수**(아무 UA나 OK·브라우저 위장까진 불요). 예수교대한성결교회(예성). uid=DB고유. 상세 www 절대경로. 공지행. 2026.07 활성.",
  },

  // ── 기타 (ETC) ──
  {
    key: "KAICAM", boardName: "KAICAM 독립교회연합회 청빙·청원", denominationHint: "ETC",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://home.kaicam.org/webchon.layout/board/white2022/list.asp?boardid=D9537",
    detailPattern: "/webchon.layout/board/white2022/view.asp?boardid=D9537&boardmasterseq=2726&boarddetailseq={id}",
    fetchNote: "2차검증: soft-404 아님 — 목록 HTTP 200 정상(31행). 빈 UA→520이라 UA 필수. ⚠️view.asp는 잘못된 id에도 200→상세 성공을 **본문 내용**으로 검증(상태코드 신뢰 금지). webchon ASP. 공지 pinned. boardmasterseq=2726 고정.",
  },
  {
    key: "NAZARENE", boardName: "나사렛성결회 목회자청빙", denominationHint: "ETC",
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://na.or.kr/ccall",
    detailPattern: "/ccall/{id}",
    fetchNote: "그누보드5(clean URL, 원형 /bbs/board.php?bo_table=ccall&wr_id={id}). pagination page. ⚠️일부 글 잠금(fa-lock)→상세 회원전용 가능(로그인 필요분 skip). 저물량.",
  },

  // ── 초교파 (모드 B — default 교단 없음, 공고에서 판정) ──
  {
    key: "TTGU", boardName: "횃불트리니티 Job Posting", denominationHint: null,
    enabled: true, fetchTier: "static", encoding: "utf-8",
    listUrl: "https://www.ttgu.ac.kr/index.php?mid=ttgu_board_03",
    detailPattern: "/index.php?mid=ttgu_board_03&document_srl={id}",
    fetchNote: "XpressEngine(mid=). document_srl=글번호. pagination page(~113p). www 호스트.",
  },
  {
    key: "ACTS", boardName: "아세아연합신대(아신대) 사역정보", denominationHint: null,
    enabled: true, fetchTier: "static", encoding: "euc-kr",
    listUrl: "https://www.acts.ac.kr/modules/board/bd_list.asp?id=acts_csrd_guide&ca_no=1",
    detailPattern: "/modules/board/bd_view.asp?no={id}&id=acts_csrd_guide",
    fetchNote: "⚠️2차검증 대정정: 올바른 게시판 = **bd_list.asp?…&ca_no=1(사역정보 탭)** — 정적 12행. 기존 bd_jobInfo.asp(ca_no=6 '실시간채용정보')는 headless+일반채용(사역 아님)이라 폐기 → headless 불요. no=글번호. EUC-KR.",
  },
  {
    key: "WGST", boardName: "웨스트민스터신대원 교역자청빙", denominationHint: null,
    enabled: true, fetchTier: "static", encoding: "utf-8", flags: { httpOnly: true },
    listUrl: "http://www.wgst.ac.kr/wgst_renew/board/board.asp?key=6131",
    detailPattern: "/wgst_renew/board/boardview.asp?key=6131&seq={id}",
    fetchNote: "http 전용(https는 -k로도 000). ASP. seq=글번호. pagination pageno. 2026-07-28 최신글 확인(활성).",
  },
];

export function enabledSources(): SourceConfig[] {
  return REGISTRY.filter((s) => s.enabled);
}

export function findSource(key: string): SourceConfig | undefined {
  return REGISTRY.find((s) => s.key === key);
}
