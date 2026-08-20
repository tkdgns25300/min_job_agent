-- min_job_agent staging 스키마 — 정본은 docs/SPEC.md §6. 이 파일은 그 구현이다.
--
-- 범위: CREATE TABLE + 제약 + 인덱스만. **RLS 정책·GRANT는 다음 마이그레이션**이다.
--       RLS를 여기서 `ENABLE`하지 않는 이유: 정책 없이 켜면 접근이 전부 막힌다. 우리 쪽은
--       특히 위험하다 — `review_data`는 min_job admin 검수 화면이 읽고 쓴다. 정책 없이 켜면
--       검수 화면이 통째로 빈 화면이 되고 원인을 찾기 어렵다. 켜는 것과 정책을 한 파일에 둔다.
--       GRANT(SPEC §8)도 미룬다 — 대상이 min_job의 `jobs`이고, 크롤러가 어느 롤로 붙는지를
--       코드가 DB에 붙는 방식과 함께 정해야 한다.
--
-- ⚠️ **min_job 마이그레이션(`20260820231650_init.sql`)이 먼저 적용돼 있어야 한다** —
--    `review_data.published_job_id`가 `jobs`를 참조한다. 파일명 타임스탬프가 그 순서를 담는다.
--
-- ⚠️ **이 리포에서 `supabase db diff`를 쓰지 말 것.** 두 리포가 같은 Supabase 프로젝트를
--    공유하는데 diff는 상대 리포의 마이그레이션을 모른다 → min_job 7테이블을 "없어야 할 것"으로
--    보고 `DROP TABLE jobs`를 생성한다.
--
-- ⚠️ **적용 경로**: Phase 1은 Supabase SQL Editor에 붙여넣는다(운영자 결정 2026-08-20).
--    그러면 `supabase_migrations.schema_migrations`에 이력이 남지 않으므로, 나중에 CLI로
--    전환할 때 `supabase migration repair --status applied 20260820234505`로 등록해야
--    이 파일이 두 번 올라가지 않는다.
--
-- 설계 원칙(CLAUDE.md · SPEC §6) — 여기서 지키는 것:
--   · DB는 저장 전용. **trigger·custom function을 만들지 않는다**(min_job DB 정책 승계).
--     ID 발급·시각·집계는 전부 파이프라인 코드. 내장 기능만 쓴다(gen_random_uuid·CHECK·FK·array).
--   · enum은 별도 타입이 아니라 **text + CHECK**. native enum의 문제는 **값을 지울 수 없다는
--     것**이다 — Postgres에 `ALTER TYPE ... DROP VALUE`가 아예 없고 순서도 바꿀 수 없다.
--     우리 어휘는 3주에 6번 바뀌었고 그중 둘이 제거였다(`KIJANG` · `SEPARATE`). CHECK는
--     `DROP CONSTRAINT`/`ADD CONSTRAINT` 한 문장이고 **기존 행까지 검사해준다**.
--     ⚠️ 허용값 정본은 DB가 아니라 CONTRACT §1 + `minjob_ingest/domain.py`다. 여기 CHECK는
--     2차 방어선이다 — 어휘를 DB가 소유하지 않는다.
--   · 컬럼명 snake_case = 레코드 필드명과 1:1(SPEC §6) → 저장이 "그대로 INSERT"가 된다.
--
-- ⚠️ **시각 컬럼에 `default now()`를 두지 않는다**(`created_at` 하나만 예외 — 그 자리 주석).
--    저장값은 KST 표기이고 `minjob_ingest/clock.py` 한 창구가 만든다. DB default를 두면
--    UTC `now()`가 섞여 창구가 둘이 되고, 파이프라인이 값을 안 보낸 배선 오류를 덮어버린다.

-- ───────────────────────────────────────────────────────────────────────────────
-- ① crawl_run — 실행별 요약 (실행마다 1행 · 누적)
--    **실행 시작에 INSERT**해 run_id를 얻고(하위 레코드가 참조) **종료에 UPDATE**한다.
--    ⚠️ 수집 실행만 이 행을 만든다. 구조화를 따로 돌리는 실행은 행을 만들지 않고
--       review_data.run_id로 수집 실행의 id를 승계한다(SPEC §2) — 집계가 전부 게시판
--       단위라 공고 단위 작업이 들어갈 칸이 없다.
-- ───────────────────────────────────────────────────────────────────────────────
create table crawl_run (
  id             uuid primary key default gen_random_uuid(),
  mode           text not null check (mode in ('BACKFILL','DAILY')),
  started_at     timestamptz not null,
  finished_at    timestamptz,                      -- NULL = 진행중
  sources_ok     int not null default 0 check (sources_ok     >= 0),
  sources_failed int not null default 0 check (sources_failed >= 0),
  new_count      int not null default 0 check (new_count      >= 0),
  -- source_key → 에러 메시지. 실패한 게시판이 없으면 빈 객체다.
  error_detail   jsonb not null default '{}',

  constraint crawl_run_finished_after_started
    check (finished_at is null or finished_at >= started_at)
);

-- ───────────────────────────────────────────────────────────────────────────────
-- ② source_data — 원자료 + 원장 (불변 · write-once · 누적)
--    write-once 예외: 운영자 **opt-out**(교회 요청)·법적 삭제 요청은 삭제/마스킹이 가능해야
--    한다(CLAUDE.md). 그 외 일반 경로에서는 갱신하지 않는다 — 수정 감지는 리비전 행 추가.
-- ───────────────────────────────────────────────────────────────────────────────
create table source_data (
  id          uuid primary key default gen_random_uuid(),
  -- ⚠️ enum CHECK를 걸지 않는다 — 31곳 목록의 정본은 `config/sources.json`이다. DB에 박으면
  --    소스를 추가할 때마다 마이그레이션이 필요해져 "레지스트리는 코드가 아니라 데이터"라는
  --    3층 분리가 깨진다. 강제하는 것은 **형식**이다 — `domain.SOURCE_KEY_PATTERN`과 같은 규칙
  --    이라야 한다. `upper(x) = x`만 걸면 빈 문자열·`YTUS-1`이 통과하고, 그런 키는 저장은 되지만
  --    코드가 읽을 때 거부해 그 행이 사라진다.
  source_key  text not null check (source_key ~ '^[A-Z][A-Z0-9_]*$'),
  -- 그 소스 안에서 유일한 글 식별자. 유일성은 어댑터 책임이고, 없으면 제목+게시일 해시다(§4).
  -- ⚠️ 아래 셋에 공백 검사가 붙는 이유: 레코드가 **빈 문자열을 거부**하는데 DB가 받으면,
  --    저장은 되고 **읽을 때 SerdeError로 그 행이 조용히 건너뛰어진다**(§6 ② CHECK와 같은 이유).
  external_id text not null check (btrim(external_id) <> ''),
  source_url  text not null check (btrim(source_url)  <> ''),
  -- ⚠️ `on delete cascade`를 걸지 않는다 — write-once 원문 증거다. 실행 행을 지웠다고
  --    증거가 함께 사라지면 안 된다.
  run_id      uuid not null references crawl_run (id),
  fetched_at  timestamptz not null,
  -- ⚠️ **빈 문자열이 정상이다** — 본문이 이미지 한 장인 게시판이 있다(config `image_only`).
  raw_text    text not null,
  -- 구조만 남긴 본문 HTML. raw_text를 대체하지 않는다 — 나중에 필요해진 것을 재수집 없이
  -- 뽑는 자리다(링크의 href·표의 행열 대응·항목 경계). 본문 컨테이너가 없는 소스는 빈
  -- 문자열이다(PCKWORLD — 상세가 포스터 한 장).
  raw_html    text not null default '',
  -- 게시판 목록의 제목 **그대로**. review_data.title은 여기서 `(끌어올림)`류 머리표만 뗀
  -- 값이다(모델을 거치지 않는다). 별도 컬럼인 이유: raw_meta에 묻으면 운영자가 원자료 표에서
  -- 무슨 공고인지 못 보고, 원장 대조에서 Store가 어댑터 키 이름을 알아야 한다.
  title       text not null check (btrim(title) <> ''),
  -- ⚠️ NOT NULL이다 — 없으면 min_job이 "게시일+N개월 자동 만료"(§9)를 적용할 수 없다.
  --    목록에 날짜 칸이 없는 소스는 어댑터가 다른 근거로 채우고(PCKWORLD는 썸네일 파일명),
  --    그것도 없으면 수집일로 둔다. 그런 소스는 그 값을 컷오프에 쓰지 않는다(`list_has_dates`).
  posted_on   date not null,
  -- **본문에 인라인으로 박힌** 이미지 URL. 구조화 직전 바이트 fetch용.
  -- ⚠️ `data:` URI가 들어올 수 있다(CALVIN) → 구조화가 스킴을 보고 갈라야 한다. 이걸 URL로
  --    취급해 요청하면 그 게시판 전체가 실패한다.
  image_urls  text[] not null default '{}',
  -- **첨부파일 전부** [{name, url}]. 이미지만이 아니라 HWP·PDF도 담는다(원문 증거를 남긴다).
  -- ⚠️ URL을 정규화하지 않는다 — 공백이 들어 있고 한글이 NFD로 분해된 첨부 URL이 있다
  --    (KAICAM). 정규화하면 저장 경로와 어긋나 404가 된다. 이름만 NFC로 정규화한다.
  attachments jsonb not null default '[]',
  -- 작성일·조회수·게시판 원필드(비정형).
  raw_meta    jsonb not null default '{}',
  -- ⭐ **판정 완료 시각**(게이트1 YES·NO 둘 다 기록). NULL 유지 = 재구조화 대상(§4).
  -- 이 컬럼이 "제외됨"과 "실패함"을 구분한다 — 없으면 매 실행 재호출되는 비용 루프가 된다.
  structured_at        timestamptz,
  -- 상한(3) 초과분은 재시도에서 빠지고 운영자 리포트로 간다(§4).
  -- ⚠️ 상한값을 DB에 박지 않는다 — Python 상수(`MAX_STRUCTURE_ATTEMPTS`)다.
  structure_attempts   int not null default 0 check (structure_attempts >= 0),
  -- 마지막 실패 원인. 상한 초과 리포트가 "왜 실패했나"를 말할 수 있게.
  last_structure_error text,
  -- Phase 후반(수정 감지)용 — MVP 미채움(§9 리비전 방식).
  content_hash         text,

  -- **원장.** 증분·중복 판정의 기준은 이 두 컬럼뿐이다(§4) — 별도 원장 테이블을 만들지 않는다.
  constraint source_data_ledger unique (source_key, external_id)
);

-- ───────────────────────────────────────────────────────────────────────────────
-- ③ review_data — 구조화 초안 + 검수 (가변 · 누적 · 53칸)
--    ⚠️ **min_job admin이 자유롭게 고치는 테이블이다**(SPEC §8). 그래서 우리 Python 불변식을
--       여기 CHECK로 한 번 더 박는다 — admin이 어긋난 값을 쓰면 우리가 그 행을 읽을 때
--       SerdeError가 나고 **행이 조용히 건너뛰어진다**(실측 확인).
--    ⚠️ ~~matched_church_id~~는 없다(2026-08-20 삭제). claim은 min_job이 `jobs.church_id`에
--       쓴다(§8) — 우리가 저장할 것이 없다.
-- ───────────────────────────────────────────────────────────────────────────────
create table review_data (
  id             uuid primary key default gen_random_uuid(),
  -- **UNIQUE = 한 원자료당 초안 1개**(중복 PENDING 방지). 재구조화는 이 행을 교체한다.
  -- ⚠️ **`on delete`를 두지 않는다(= restrict).** opt-out·법적 삭제로 원자료를 지울 때
  --    `cascade`였다면 이 행이 함께 사라지고 **published_job_id도 같이 사라져 이미 공개된
  --    `jobs` 행이 아무도 모르게 남는다**(그게 정작 지워야 하는 것이다). restrict면 삭제가
  --    실패해서, 운영자가 **공개 취소 → 초안 → 원자료** 순서를 밟게 된다(RUNBOOK).
  source_data_id uuid not null unique references source_data (id),
  -- source_data.run_id을 **승계**한다 — 구조화는 자기 crawl_run을 만들지 않는다(§2).
  run_id         uuid not null references crawl_run (id),
  -- ⚠️ source_data_id로 JOIN하면 되니 정규화상 중복이지만 **복사해 둔다**. jobs.source_url은
  --    원문 재게시 금지·출처 표기의 핵심 필드라, 승격 코드가 JOIN을 잊으면 출처 없이
  --    공개된다. **승격이 이 테이블 하나만 보고 끝나게** 한다(빈 문자열도 거부 — 아래 CHECK).
  source_url     text not null,

  -- ── 분류(게이트) ─────────────────────────────────────────────
  -- ⚠️ **`NO`가 허용값에 없다** — 개교회 채용이 아니면 review_data를 아예 만들지 않는다
  --    (§5.1). 대신 source_data.structured_at이 기록돼 재구조화 대상에서 빠진다.
  is_church_recruitment text not null check (is_church_recruitment in ('YES','UNCERTAIN')),
  -- 한 글에 자리가 여럿인 공고를 표현해야 해서 배열이다. 게이트2를 아직 안 돈 초안은 빈
  -- 배열이다 — 분류를 뽑지 않는 패스가 있고, 그건 "아직 판정 안 됨"이지 모순이 아니다.
  -- ⚠️ **배열 원소도 검사한다 — min_job jobs와 다른 선택이다.** 허용값 밖이 들어오면 우리가
  --    그 행을 읽을 때 SerdeError가 나고 그 공고가 아무 말 없이 사라진다(위 테이블 주석).
  job_kind        text[] not null default '{}'
                    check (job_kind <@ array['MINISTRY','GENERAL']),
  position        text[] not null default '{}'
                    check (position <@ array['SENIOR_PASTOR','ASSOCIATE_PASTOR',
                                             'EVANGELIST','LICENSED_MINISTER','ETC']),
  role            text,          -- 일반직 직무. 통제 목록이 아니라 자유 텍스트

  -- ── 공고 (jobs 미러) ────────────────────────────────────────
  -- NULL 가능이다 — 승격 게이트가 이 칸을 세고, 비면 confidence=low로 검수에 올린다(§5.7).
  title           text,
  department      text check (department in (
                    'INFANT','CHILDREN','YOUTH','YOUNG_ADULT','DISTRICT','WORSHIP','ADMIN','ETC')),
  -- NULL = 미상(원문 언급률 51%라 NOT NULL이면 임의값을 강요한다)
  employment_type text check (employment_type in ('FULL_TIME','SEMI_FULL_TIME','PART_TIME')),
  qualification   text check (qualification in (
                    'ANY','ENTRY','EXPERIENCED','ORDAINED','SEMINARIAN')),
  headcount       text,          -- "약간명"·"1~2명" 같은 비정형이 흔해 정수가 아니다
  start_timing    text,          -- "즉시"·"협의"·"2월 중"
  -- NULL = 언급 없음. false(명시적 미제공)와 **다르다** — 언급 없음을 미제공으로 바꾸면
  -- 틀린 정보가 된다. ⚠️ housing_provided가 있다고 해놓고 근거가 없으면 둘을 함께 비운다(§5.5c).
  housing_provided boolean,
  housing_note     text,
  -- 만원 단위. ⚠️ 사례비 환산과 월/연 판정은 **코드**가 한다(`pipeline/normalize.py`) —
  -- 모델에 맡겼더니 같은 `연봉 3,200이상`이 Flash 3200 / Flash-Lite 267로 갈렸다.
  pay_min         int check (pay_min >= 0),
  pay_max         int check (pay_max >= 0),
  pay_note        text,          -- "교회 내규에 따름" 등 비정형을 원문 그대로
  -- NULL 가능이다(min_job jobs는 NOT NULL DEFAULT 'MONTH'). 여기는 초안이라 "아직 모른다"를
  -- 담아야 하고, 값을 지어내면 승격 때 월/연이 뒤바뀐 사례비가 공개된다.
  pay_period      text check (pay_period in ('MONTH','YEAR')),
  benefit_note    text,
  work_days       text,
  requirements    text[] not null default '{}',
  preferred       text[] not null default '{}',
  required_docs   text[] not null default '{}',
  optional_docs   text[] not null default '{}',
  process_steps   text[] not null default '{}',
  description     text,          -- **요약**이다. 원문 재게시 금지
  -- ⚠️ **예외로 NOT NULL이다**(2026-08-14). 만료 판정(§9)의 기준이라 비면 그 공고를
  --    언제까지 보여줄지 정할 수 없다. source_data.posted_on을 물려받고 그쪽도 NOT NULL이다.
  posted_at       date not null,
  deadline        date,          -- NULL = 상시모집

  -- ── 교회 초안 ───────────────────────────────────────────────
  church_name     text,
  -- ⚠️ **검수 우선순위는 교단보다 지역이다** — 지역이 비면 min_job 지역 필터에서 무조건
  --    탈락해 사실상 안 보이는 공고가 된다. 그래도 nullable이다(원문에 없을 수 있다).
  region          text check (region in (
                    'SEOUL','GYEONGGI','INCHEON','GANGWON','CHUNGBUK','CHUNGNAM','DAEJEON',
                    'SEJONG','GYEONGBUK','GYEONGNAM','DAEGU','ULSAN','BUSAN','JEONBUK',
                    'JEONNAM','GWANGJU','JEJU','OVERSEAS')),
  city            text,
  address         text,          -- ⚠️ contact_post(서류 접수처)와 다른 값이다

  -- ── 교단 ────────────────────────────────────────────────────
  -- ⚠️ **11값 — min_job jobs.denomination(10값)과 다르다.** 여기는 초안이라 `UNKNOWN`(미상)을
  --    담아야 하고, 승격이 UNKNOWN을 NULL로 떨어뜨린다(`denomination_for_publish`).
  denomination        text check (denomination in (
                        'HAPDONG','TONGHAP','BAEKSEOK','GOSIN','HAPSIN',
                        'GAMLI','SEONGGYUL','BAPTIST','SUNBOK','ETC','UNKNOWN')),
  -- ⚠️ **5값이다** — `operator`(운영자가 검수에서 확정)를 빠뜨리지 말 것.
  denomination_source text not null check (denomination_source in (
                        'stated','registry','ai_guess','unknown','operator')),
  denomination_evidence text,
  raw_denomination      text,    -- 원표기

  -- ── 지원 연락처 (공개) ──────────────────────────────────────
  -- **방법별 4컬럼** — min_job APPLY_METHODS가 ETC 없는 닫힌 4키라 1:1 대응이고, 승격이
  -- 파싱 없이 INSERT한다. 대표 문자열 하나로 두던 설계는 철회됐다(2026-08-05).
  contact_email   text,
  contact_tel     text,
  contact_link    text,
  contact_post    text,

  -- ── 이단 (자동 거부 · 낙인 금지) ────────────────────────────
  -- ⚠️ **확정된 일치만 자동 거절한다**(§5.4). 지역까지 맞았거나 단체·사람 이름이면 거절,
  --    지역을 확인 못 한 개별 교회명은 검수로 보낸다 — 목록 96%에 지역이 없어 이름만으로
  --    거르면 동명이교회가 아무도 모르게 사라진다.
  heresy_flag     boolean not null default false,
  heresy_evidence text,

  -- ── 검수 메타 ───────────────────────────────────────────────
  confidence      text not null check (confidence in ('high','medium','low')),
  -- ⚠️ **파생값이다** — 언제든 다시 계산한다. 판정 결과는 dedup_state가 갖는다(§4.1).
  dedup_key       text,
  dedup_state     text check (dedup_state in ('ALONE','MASTER','DUPLICATE','UNCERTAIN')),
  review_status   text not null default 'PENDING'
                    check (review_status in ('PENDING','APPROVED','REJECTED')),
  -- **자동 거부를 되짚는 유일한 통로다.** 중복·이단·마감·운영자 거절이 REJECTED 하나로
  -- 뭉치면 "우리 dedup이 틀렸나"·"이단 오판인가"를 확인할 수 없다. 특히 이단은 검수 큐에
  -- 뜨지 않는 자동 거부라 이유가 없으면 잘못 걸러도 영원히 드러나지 않는다.
  reject_reason   text check (reject_reason in ('DUPLICATE','CLOSED','HERESY','OPERATOR')),
  -- 공개 결과. §4.2가 이걸로 앵커를 가리고, §4.2b가 끌어올림 대상을 찾고, §8이 "이 jobs 행이
  -- 우리 것인가"를 이걸로 판정한다.
  -- ⚠️ `on delete set null`이다 — 공고가 지워졌으면 연결도 없어야 한다. cascade면 우리 원장
  --    행이 함께 사라지고(증거 유실), 기본값(restrict)이면 운영자가 jobs 행을 지울 수 없다.
  published_job_id uuid references jobs (id) on delete set null,
  reviewed_by      text,
  reviewed_at      timestamptz,
  -- 검수 큐 정렬·감사. 코드가 KST로 채운다 — 이 default는 **admin이 손으로 INSERT할 때의
  -- 안전망**이다(파이프라인 경로에서는 항상 값이 온다).
  created_at       timestamptz not null default now(),

  -- ① 승격이 이 테이블만 보고 끝나므로 출처가 공백이면 안 된다.
  constraint review_data_source_url_not_blank check (btrim(source_url) <> ''),
  -- ② 게이트1 UNCERTAIN은 운영자 우선검토로 보내는 값이라 낮은 등급이어야 한다(§5.1).
  constraint review_data_uncertain_is_low_confidence
    check (is_church_recruitment <> 'UNCERTAIN' or confidence = 'low'),
  -- ③ job_kind ↔ position/role 상호 일치. min_job `jobs_kind_matches_seat`와 **같은 규칙**이나
  --    형태가 다르다 — 게이트2를 안 돈 초안(빈 배열)을 통과시켜야 한다.
  --    ⚠️ min_job이 남긴 함정 주석이 여기도 적용된다: `array_length`는 빈 배열에 NULL을
  --       반환하고 CHECK는 NULL을 통과시킨다. `cardinality`는 0을 준다.
  constraint review_data_kind_matches_seat check (
    case when cardinality(job_kind) = 0
         then cardinality(position) = 0 and role is null
         else ('MINISTRY' = any (job_kind)) = (cardinality(position) > 0)
          and ('GENERAL'  = any (job_kind)) = (role is not null)
    end
  ),
  -- ④ 사례비 범위가 뒤집히면 안 된다.
  constraint review_data_pay_range
    check (pay_min is null or pay_max is null or pay_min <= pay_max),
  -- ⑤ 근거가 값을 요구하는데 비어 있으면 거부한다(§5.3). **반대 방향은 막지 않는다** —
  --    운영자가 검수에서 해소한 행이 `operator` 근거로 다시 들어온다.
  constraint review_data_source_requires_denomination check (
    denomination_source = 'unknown'
    or (denomination is not null and denomination <> 'UNKNOWN')
  ),
  -- ⑥ 이단으로 표시했으면 근거가 있어야 한다 — 낙인만 남기지 않는다.
  constraint review_data_heresy_needs_evidence
    check (not heresy_flag or btrim(coalesce(heresy_evidence,'')) <> ''),
  -- ⑦ 거절이면 이유가 있어야 하고, 거절이 아니면 이유가 없어야 한다.
  --    ⚠️ **이 CHECK가 이 파일의 존재 이유 중 하나다**(SPEC §8). admin이
  --       `UPDATE review_data SET review_status='APPROVED'`만 하고 reject_reason을 안 지우면
  --       우리가 그 행을 읽을 때 SerdeError가 나고 행이 조용히 사라진다.
  constraint review_data_rejection_needs_reason
    check ((review_status = 'REJECTED') = (reject_reason is not null)),
  -- ⑧ 키 없이 결론만 있을 수 없다.
  constraint review_data_dedup_state_needs_key
    check (dedup_state is null or dedup_key is not null),
  -- ⑨ DUPLICATE 거절과 DUPLICATE 판정은 항상 함께다 — 판정만 있고 살아 있으면 **중복이
  --    그대로 공개되고**, 거절만 있으면 왜 거절됐는지 되짚을 수 없다.
  --    ⚠️ **`=`로 그냥 쓰면 안 된다.** 두 컬럼이 nullable이라 한쪽이 NULL이면 비교식 자체가
  --       NULL이 되고 **Postgres CHECK는 NULL을 통과시킨다**. `is not distinct from`도 안
  --       된다 — dedup_state='ALONE' + reject_reason=NULL인 정상 행을 거부한다(false vs NULL).
  --       coalesce로 양쪽을 항상 boolean으로 만든다.
  constraint review_data_duplicate_pairs_with_state check (
    (coalesce(reject_reason,'-') = 'DUPLICATE') = (coalesce(dedup_state,'-') = 'DUPLICATE')
  )
);

-- ───────────────────────────────────────────────────────────────────────────────
-- ④ source_health — 게시판별 상태 (약 31행 · 매 실행 UPSERT)
--    누적값(consecutive_*·total_collected)과 last_success_at·first_run_at 보존에는
--    **직전 값 읽기가 필요**하다 → Store에 조회가 있다.
-- ───────────────────────────────────────────────────────────────────────────────
create table source_health (
  -- 게시판 하나당 한 행. ⚠️ 조회가 저장과 **같은 정규화**를 거쳐야 한다 — 안 하면 매 실행
  -- previous=None이 되고 누적 카운터가 초기화돼 §7 경보가 영구히 울리지 않는다.
  source_key      text primary key check (source_key ~ '^[A-Z][A-Z0-9_]*$'),
  first_run_at    timestamptz not null,
  last_run_at     timestamptz not null,
  last_run_id     uuid references crawl_run (id),
  -- ⚠️ **EMPTY는 "목록 행이 0"이다 — "신규 0건"이 아니다.** 원장 증분이라 조용한 게시판은
  --    신규가 며칠씩 0이고 **그게 정상**이다. 그걸 소프트 실패로 세면 조용한 곳들이 매일
  --    경보를 울려 경보가 잡음이 되고 정작 깨진 게시판이 묻힌다(§7).
  last_status     text not null check (last_status in ('OK','FAIL','EMPTY')),
  -- ⚠️ **OK에서만 갱신한다.** EMPTY에도 갱신하면 목록 0행이 며칠 이어질 때 "마지막 성공"이
  --    계속 오늘로 밀려 "언제까지는 정상이었나"를 영구히 잃는다.
  last_success_at timestamptz,
  -- 그 실행에 적용한 기간. ⚠️ **이것 없이는 다른 수치를 해석할 수 없다** — 3개월 백필 258행
  -- 다음의 데일리 18행이 "급감"으로 보인다.
  last_cutoff     date,
  -- ⚠️ FAIL은 관측값을 덮지 않고 직전 값을 보존한다 — 0으로 덮으면 FAIL과 EMPTY가 구분되지 않는다.
  last_rows       int not null default 0 check (last_rows      >= 0),
  last_new_count  int not null default 0 check (last_new_count >= 0),
  last_posted_on  date,
  consecutive_failures   int not null default 0 check (consecutive_failures   >= 0),
  -- 소프트 실패 경보의 근거다(셀렉터 깨짐·로그인벽 전환 의심 · §7).
  consecutive_empty_runs int not null default 0 check (consecutive_empty_runs >= 0),
  total_collected        int not null default 0 check (total_collected        >= 0),
  last_error      text,

  -- 상태와 행 수가 어긋나면 §7 경보 판정이 무의미해진다 — 정의를 제약으로 못 박는다.
  constraint source_health_fail_needs_error
    check (last_status <> 'FAIL' or btrim(coalesce(last_error,'')) <> ''),
  -- OK는 정의상 이번에 목록을 읽었다 — 그 시각이 곧 마지막 성공이다.
  constraint source_health_ok_needs_success
    check (last_status <> 'OK' or last_success_at is not null),
  constraint source_health_empty_means_zero_rows
    check (last_status <> 'EMPTY' or last_rows = 0),
  constraint source_health_ok_means_some_rows
    check (last_status <> 'OK' or last_rows > 0),
  -- 신규는 목록 행의 부분집합이다. 어기면 rows 자리에 fresh를 넣은 배선 오류다.
  constraint source_health_new_within_rows check (last_new_count <= last_rows),
  constraint source_health_first_run_before_last check (first_run_at <= last_run_at)
);

-- ───────────────────────────────────────────────────────────────────────────────
-- 인덱스 — 실제 조회 패턴만 (`minjob_ingest/store/base.py`의 Store 프로토콜)
--
-- **만들지 않은 것과 이유** (안 쓰는 인덱스는 쓰기를 느리게 하는 죽은 무게다):
--   · `seen_postings`(원장 대조)·`requeue_for_structure`(게시판별) — source_data_ledger
--     UNIQUE 인덱스가 덮는다. source_key가 선행 컬럼이다.
--   · `upsert_review_data` — UNIQUE(source_data_id)가 덮는다.
--   · id로 찾는 것 전부(`update_structure_state`·`apply_dedup`·`finish_run`) — PK.
--   · `get_health` — source_key가 PK다. source_health는 약 31행이라 다른 인덱스가 없다.
--   · run_id FK 둘 — crawl_run 행을 지우지 않으므로 FK 검사용 인덱스가 필요 없고, run_id로
--     거르는 조회도 없다.
--   · crawl_run — 실행마다 1행이라 seq scan이 더 빠르다.
--   · `dedup_candidates` — **전량을 한 번에 읽는다**(중복은 글 하나만 보고 판정할 수 없다).
--     seq scan이 맞는 접근이다.
-- ───────────────────────────────────────────────────────────────────────────────

-- `list_unstructured` — 판정이 안 끝난 것을 오래된 것부터. **부분 인덱스**다: 누적 원장에서
-- 미구조화는 소수라 전량 스캔을 피한다.
-- ⚠️ `structure_attempts < 3`을 조건에 넣지 않는다 — 3은 Python 상수(`MAX_STRUCTURE_ATTEMPTS`)라
--    DB에 박으면 상한을 고칠 때 마이그레이션이 필요해진다. NULL 집합만 좁히면 충분하다.
create index source_data_unstructured_idx
  on source_data (fetched_at) where structured_at is null;

-- 검수 큐: review_status로 거르고 created_at으로 정렬한다(오래된 것 우선). 복합으로 두면
-- 정렬까지 인덱스가 덮고, 선행 컬럼만 쓰는 조회(승격 대상 = APPROVED)도 같이 탄다.
create index review_data_queue_idx on review_data (review_status, created_at);

-- §4.2 앵커 가리기 · §4.2b 끌어올림 대상 찾기 · §8 "이 jobs 행이 우리 것인가" 판정
create index review_data_published_job_id_idx on review_data (published_job_id);

-- admin 검수 화면이 "같은 자리 묶음"을 보여줄 때(§4.1 · §8 min_job이 해야 할 일)
create index review_data_dedup_key_idx on review_data (dedup_key);
