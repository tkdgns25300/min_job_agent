// min_job 출력 스키마(enum) 미러 + 크롤러 전용 enum.
// 출력 정본 = ../min_job/docs/DATA.md. 크롤러는 교단 10키(KIJANG 제외 — CONTRACT §1).
// 값(영어 대문자 key)만 둔다. 한글 라벨은 min_job(constants/domain.ts) 소관.

// ── min_job 미러 ──────────────────────────────────────────────
export const DENOMINATIONS = [
  "HAPDONG", "TONGHAP", "BAEKSEOK", "GAMLI", "SUNBOK",
  "BAPTIST", "SEONGGYUL", "GOSIN", "HAPSIN", "ETC",
] as const;
export type Denomination = (typeof DENOMINATIONS)[number];

export const REGIONS = [
  "SEOUL", "GYEONGGI", "INCHEON", "GANGWON", "CHUNGBUK", "CHUNGNAM",
  "DAEJEON", "SEJONG", "GYEONGBUK", "GYEONGNAM", "DAEGU", "ULSAN",
  "BUSAN", "JEONBUK", "JEONNAM", "GWANGJU", "JEJU", "OVERSEAS",
] as const;
export type Region = (typeof REGIONS)[number];

export const POSITIONS = [
  "SENIOR_PASTOR", "ASSOCIATE_PASTOR", "EVANGELIST", "LICENSED_MINISTER", "ETC",
] as const;
export type Position = (typeof POSITIONS)[number];

export const DEPARTMENTS = [
  "INFANT", "CHILDREN", "YOUTH", "YOUNG_ADULT", "DISTRICT", "WORSHIP", "ADMIN", "ETC",
] as const;
export type Department = (typeof DEPARTMENTS)[number];

export const EMPLOYMENT_TYPES = ["FULL_TIME", "SEMI_FULL_TIME", "PART_TIME"] as const;
export type EmploymentType = (typeof EMPLOYMENT_TYPES)[number];

export const QUALIFICATIONS = ["ANY", "ENTRY", "EXPERIENCED", "ORDAINED", "SEMINARIAN"] as const;
export type Qualification = (typeof QUALIFICATIONS)[number];

export const STIPEND_PERIODS = ["MONTH", "YEAR"] as const;
export type StipendPeriod = (typeof STIPEND_PERIODS)[number];

// ── 크롤러 전용 (SPEC §5·§6) ──────────────────────────────────
export const JOB_KINDS = ["MINISTRY", "GENERAL"] as const;
export type JobKind = (typeof JOB_KINDS)[number];

/** 게이트1: 개교회 채용인가 (SPEC §5.1). UNCERTAIN은 낮은 confidence로 운영자에게. */
export const IS_CHURCH_RECRUITMENT = ["YES", "NO", "UNCERTAIN"] as const;
export type IsChurchRecruitment = (typeof IS_CHURCH_RECRUITMENT)[number];

export const DENOMINATION_SOURCES = ["stated", "registry", "ai_guess", "unknown"] as const;
export type DenominationSource = (typeof DENOMINATION_SOURCES)[number];

export const REVIEW_STATUSES = ["PENDING", "APPROVED", "REJECTED"] as const;
export type ReviewStatus = (typeof REVIEW_STATUSES)[number];

export const CONFIDENCE_LEVELS = ["high", "medium", "low"] as const;
export type Confidence = (typeof CONFIDENCE_LEVELS)[number];

export const CRAWL_MODES = ["BACKFILL", "DAILY"] as const;
export type CrawlMode = (typeof CRAWL_MODES)[number];

export const FETCH_TIERS = ["static", "json", "headless"] as const;
export type FetchTier = (typeof FETCH_TIERS)[number];

/** source_health 상태 (SPEC §6 ③). ZERO = 200인데 신규 0건(소프트 실패 후보). */
export const SOURCE_HEALTH_STATUSES = ["OK", "FAIL", "ZERO"] as const;
export type SourceHealthStatus = (typeof SOURCE_HEALTH_STATUSES)[number];

/**
 * review_data 임시 교단값 — 승격 전 운영자가 10키 중 하나로 해소(SPEC §5.3). 공개엔 안 나감.
 * 저장값은 영어 key로 통일(표시 라벨 "미상"은 min_job 소관).
 */
export const DENOMINATION_UNKNOWN = "UNKNOWN";
export type DenominationOrUnknown = Denomination | typeof DENOMINATION_UNKNOWN;
